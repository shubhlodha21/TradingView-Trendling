"""CurrencyService — FX cross-rate cache + multi-currency conversion.

The base-currency convergence point for multi-asset risk and P&L.

Every Money in the system can be converted to any other Money via:

    eur_money.to(Currency.USD, fx_service)

This module's job is to source the `Currency → Currency` rates that
power that conversion.

Day-1 scope: a STUB. The class exists with the right interface so
callers (sizing policies, risk gate) can wire to it without churn.
Concrete rate sourcing (subscribe to live IBKR FX crosses, daily
snapshot fallback, staleness alerts) lands in Day-2 PM when we
wire the engine.

Why a class (not module-level dict): rates are stateful (cache TTL,
last-known-good tracking), and the production version subscribes to
ib_async tickers which is async lifecycle. Encapsulating in a class
makes the wiring testable.

Future shape (annotated for D2):
    async def warm(self, currencies: set[Currency]) -> None:
        '''On engine startup, subscribe to every non-base currency's
        cross-rate against base. Cheap: 1 ticker subscription per ccy.'''

    def rate(self, from_ccy: Currency, to_ccy: Currency) -> Decimal:
        '''Sync lookup. Returns latest cached rate.
        Raises StaleRate if cache age > TTL.'''
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from .types import Currency, DEFAULT_BASE_CURRENCY


# ────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────

class StaleRate(RuntimeError):
    """Raised when the cached FX rate is older than the service's
    staleness tolerance. Callers should fall back to last-known-good
    OR refuse the trade — pick one explicitly, don't silently use the
    stale value."""

    def __init__(self, from_ccy: Currency, to_ccy: Currency, age_seconds: float):
        super().__init__(
            f"FX rate {from_ccy.name}→{to_ccy.name} is {age_seconds:.0f}s old, "
            f"exceeds staleness tolerance. Refresh or refuse the trade."
        )
        self.from_ccy = from_ccy
        self.to_ccy = to_ccy
        self.age_seconds = age_seconds


class NoRateAvailable(RuntimeError):
    """Raised when the service has never seen a rate for this pair.
    Typically because warm() wasn't called with this currency, or the
    initial subscription is still pending."""

    def __init__(self, from_ccy: Currency, to_ccy: Currency):
        super().__init__(
            f"No FX rate cached for {from_ccy.name}→{to_ccy.name}. "
            f"Call CurrencyService.warm() with this currency on startup, "
            f"or check that the IBKR feed for {from_ccy.name}{to_ccy.name} is up."
        )
        self.from_ccy = from_ccy
        self.to_ccy = to_ccy


# ────────────────────────────────────────────────────────────────────
# Cached rate record
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CachedRate:
    """One entry in the rate cache. Includes the timestamp so we can
    answer "how stale is this?" cheaply."""

    rate: Decimal
    fetched_at: datetime  # UTC, tz-aware

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(tz=timezone.utc)
        return (now - self.fetched_at).total_seconds()


# ────────────────────────────────────────────────────────────────────
# Service
# ────────────────────────────────────────────────────────────────────

class CurrencyService:
    """FX rate cache + conversion. Day-1 is a stub that supports
    same-currency conversion (no-op) only.

    Real rates come in Day-2 PM when we wire it to ib_async tickers.

    __slots__ saves ~150B per instance and shaves a few ns per attr
    access. Negligible in practice (one instance per engine) but
    consistent with the rest of the module.
    """

    __slots__ = ("_base", "_cache", "_staleness_tolerance")

    def __init__(
        self,
        base: Currency = DEFAULT_BASE_CURRENCY,
        staleness_tolerance_seconds: float = 60.0,
    ):
        self._base = base
        self._cache: dict[tuple[Currency, Currency], CachedRate] = {}
        self._staleness_tolerance = staleness_tolerance_seconds

    @property
    def base(self) -> Currency:
        return self._base

    def rate(self, from_ccy: Currency, to_ccy: Currency) -> Decimal:
        """Get the cached FX rate `from_ccy → to_ccy`.

        Day-1 behavior:
          * from_ccy == to_ccy → returns Decimal("1") (no-op)
          * any cross-currency request → raises NoRateAvailable

        Day-2 behavior:
          * Same-currency → 1
          * Looks up cache; if fresh, returns the rate
          * If stale → raises StaleRate
          * If missing → raises NoRateAvailable
        """
        if from_ccy is to_ccy:
            return Decimal("1")

        # Try direct
        key = (from_ccy, to_ccy)
        cached = self._cache.get(key)
        if cached is not None:
            age = cached.age_seconds()
            if age > self._staleness_tolerance:
                raise StaleRate(from_ccy, to_ccy, age)
            return cached.rate

        # Try inverse (we may have cached EURUSD but caller asks for USDEUR)
        inv = self._cache.get((to_ccy, from_ccy))
        if inv is not None:
            age = inv.age_seconds()
            if age > self._staleness_tolerance:
                raise StaleRate(from_ccy, to_ccy, age)
            return Decimal("1") / inv.rate

        # Day-1: no other paths. Day-2 will add cross-via-base
        # (EUR→JPY computed as EUR→USD * USD→JPY).
        raise NoRateAvailable(from_ccy, to_ccy)

    def update(self, from_ccy: Currency, to_ccy: Currency, rate: Decimal) -> None:
        """Update the cache with a fresh rate. Called by the Day-2
        live-ticker subscription handler. Available now for tests
        that need to seed rates without touching IBKR."""
        if from_ccy is to_ccy:
            return  # no-op
        self._cache[(from_ccy, to_ccy)] = CachedRate(
            rate=rate,
            fetched_at=datetime.now(tz=timezone.utc),
        )
