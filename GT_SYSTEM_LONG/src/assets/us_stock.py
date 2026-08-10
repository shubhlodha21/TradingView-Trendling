"""USEquitySpec — concrete AssetSpec for US-listed equities.

THE INVARIANT THIS FILE ENFORCES:

  An engine running on US equity via this spec must produce
  byte-identical behavior to today's nabi branch on:
    * order placement (same prices, same qty, same routing)
    * audit log output (order.csv, state.csv, pnl.csv content)
    * state file shape (.gt_state_TKR_cid.json fields unchanged)
    * dashboard output (POSVAL, exposure, P&L numbers identical)

  In other words: USEquitySpec is the REGRESSION BASELINE. If a test
  on PLTR with the spec produces different output than the same test
  on PLTR without the spec, that's a bug — fix this file, not the
  engine.

  Why this matters: equity is live trading real money. The whole
  multi-asset abstraction is worth less than zero if it accidentally
  breaks equity. The 8 policy implementations here MUST mirror the
  existing equity hardcoded behavior exactly.

What's composed here:
  SMARTStockContract       — Stock(symbol, "SMART", "USD")
  LastPricePolicy          — feed.last for all three compare points
                             (equity NBBO is reliable; last is the
                             canonical price for trigger / stop / track)
  DecimalTickPolicy(2)     — round_to_tick = round(price, 2)
  SimpleSizing             — notional = qty(shares) × price, in USD
  IBKRTieredEquityCommission — IBKR's Tiered fee model approximation
  USEquitySession          — NYSE 09:30-16:00 ET, NYSE holiday calendar
  NoLifecycle              — equity doesn't roll/expire/settle in-loop
                             (T+1 settlement is accounting, not engine concern)
  NoExtraRisk              — universal RiskGate is sufficient for equity
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Optional
from zoneinfo import ZoneInfo

from .enum import AssetClass
from .policies import (
    ContractPolicy, PricePolicy, TickPolicy, SizingPolicy,
    CommissionPolicy, SessionPolicy, LifecyclePolicy, RiskOverlay,
    FeedSnapshot, RoundDirection, SessionWindow,
    OrderIntent, PortfolioView, RiskVerdict,
)
from .policies.commission import Side
from .policies.contract import ContractNotFound
from .policies.price import NoUsablePrice
from .policies.sizing import SizingMismatch
from .policies.tick import round_to_grid
from .resolver import SpecRegistry
from .spec import AssetSpec
from .types import (
    Currency, Money, Price, Quantity, QuantityUnit, shares,
)

if TYPE_CHECKING:
    from ib_async import Contract, IB


# ────────────────────────────────────────────────────────────────────
# Constants — equity-specific
# ────────────────────────────────────────────────────────────────────

NY_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

# Penny ticks since 2001 SEC rule.
EQUITY_TICK_SIZE = Decimal("0.01")

# IBKR Tiered: per-share, with min/max per order.
# These are the published Tier-1 (under 300k shares/month) values.
# Engine doesn't track monthly volume so we use Tier-1 as the
# realistic estimate.
TIERED_PER_SHARE = Decimal("0.0035")
TIERED_MIN_PER_ORDER = Decimal("0.35")
TIERED_MAX_PCT_OF_TRADE = Decimal("0.01")  # 1% cap

# NYSE 2024 — 2026 holidays. Hardcoded because pandas_market_calendars
# is a heavyweight dependency we don't otherwise need. Add forward
# years here as the calendar is published. Format: ISO date strings.
NYSE_HOLIDAYS_2024_2026: frozenset[date] = frozenset({
    # 2024
    date(2024, 1, 1),    # New Year's Day
    date(2024, 1, 15),   # MLK Day
    date(2024, 2, 19),   # Presidents Day
    date(2024, 3, 29),   # Good Friday
    date(2024, 5, 27),   # Memorial Day
    date(2024, 6, 19),   # Juneteenth
    date(2024, 7, 4),    # Independence Day
    date(2024, 9, 2),    # Labor Day
    date(2024, 11, 28),  # Thanksgiving
    date(2024, 12, 25),  # Christmas
    # 2025
    date(2025, 1, 1),    # New Year's Day
    date(2025, 1, 9),    # Carter National Day of Mourning
    date(2025, 1, 20),   # MLK Day
    date(2025, 2, 17),   # Presidents Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; Jul 4 is Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
})

# Standard NYSE session (RTH only — extended hours handled separately
# by the venue config, not by SessionPolicy).
NYSE_OPEN_LOCAL = time(9, 30)
NYSE_CLOSE_LOCAL = time(16, 0)


# ────────────────────────────────────────────────────────────────────
# Contract policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SMARTStockContract:
    """Builds Stock(symbol, "SMART", "USD") — the IBKR default for
    US-listed equity. SMART routes the order across exchanges to
    find best execution."""

    primary_exchange: Optional[str] = None  # rare override (e.g. "NASDAQ" for ETFs)

    def make(self, symbol: str) -> "Contract":
        # Late import: keeps the policies importable without ib_async
        # installed (useful for unit-testing types/specs in isolation).
        from ib_async import Stock
        contract = Stock(symbol.upper(), "SMART", "USD")
        if self.primary_exchange:
            contract.primaryExchange = self.primary_exchange
        return contract

    @staticmethod
    def identify(ib_contract) -> Optional[str]:
        """Reverse-translate an ib_async Contract to its logical ticker.
        For US equity the broker's `contract.symbol` already IS the
        logical ticker — match on secType to avoid claiming non-equity
        contracts. See IDEALPROForexContract.identify for the why.
        """
        sec_type = getattr(ib_contract, 'secType', None)
        if sec_type != 'STK':
            return None
        return (getattr(ib_contract, 'symbol', '') or '').upper() or None

    async def qualify(self, ib: "IB", contract: "Contract") -> "Contract":
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ContractNotFound(
                f"IBKR returned no match for {contract.symbol} on "
                f"{contract.exchange}/{contract.currency}. Symbol typo "
                f"or asset not available on this account?"
            )
        return qualified[0]


# ────────────────────────────────────────────────────────────────────
# Price policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LastPricePolicy:
    """Equity feeds carry a reliable `last` (most recent trade) with
    tick-by-tick frequency. All three semantic prices return last
    because the bid/ask spread is usually 1 cent and reliable.

    Fallback chain when `last` is None/missing:
      * (bid + ask) / 2 — mid
      * `close` — yesterday's close (last resort, may be stale)
      * raise NoUsablePrice
    """

    # When the spread is wider than this fraction of mid, treat the
    # feed as not actionable (probably an extended-hours quote or a
    # halted name). 50 bps = 0.50% — generous for liquid equity,
    # catches obvious junk.
    max_actionable_spread_bps: Decimal = Decimal("50")

    # Tick-age tolerance — if the feed's ts is older than this, treat
    # as stale. Equity feeds usually update many times per second
    # during RTH.
    max_actionable_age_seconds: float = 30.0

    def reference(self, feed: FeedSnapshot) -> Price:
        return self._best_available(feed, prefer="last")

    def buy_compare(self, feed: FeedSnapshot) -> Price:
        # For equity, the spread is typically a penny — using `last`
        # is the historical behavior we must preserve.
        return self._best_available(feed, prefer="last")

    def sell_compare(self, feed: FeedSnapshot) -> Price:
        return self._best_available(feed, prefer="last")

    def is_actionable(self, feed: FeedSnapshot) -> bool:
        # Need at least bid AND ask AND last — anything less is junk.
        if feed.bid is None or feed.ask is None or feed.last is None:
            return False
        # Crossed market
        if feed.bid >= feed.ask:
            return False
        # Spread sanity (bps of mid)
        mid = (feed.bid + feed.ask) / 2
        if mid > 0:
            spread_bps = (feed.ask - feed.bid) / mid * Decimal("10000")
            if spread_bps > self.max_actionable_spread_bps:
                return False
        # Staleness
        if feed.ts is not None:
            now = datetime.now(tz=UTC)
            age = (now - feed.ts).total_seconds()
            if age > self.max_actionable_age_seconds:
                return False
        return True

    def _best_available(self, feed: FeedSnapshot, prefer: str) -> Price:
        """Pull the preferred field with reasonable fallbacks."""
        for candidate in (
            getattr(feed, prefer),
            (feed.bid + feed.ask) / 2 if (feed.bid is not None and feed.ask is not None) else None,
        ):
            if candidate is None:
                continue
            return Price(candidate)
        raise NoUsablePrice(
            f"Equity feed has no usable price (bid={feed.bid}, "
            f"ask={feed.ask}, last={feed.last})"
        )


# ────────────────────────────────────────────────────────────────────
# Tick policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class DecimalTickPolicy:
    """Constant tick-size policy parameterized by decimal places.

    For US equity: DecimalTickPolicy(decimals=2) → tick=0.01 always.
    For LSE penny stocks: DecimalTickPolicy(decimals=4) → tick=0.0001.

    This policy reuses across multiple asset classes whose tick is a
    simple power-of-10 with no per-price-tier grain rules.
    """

    decimals: int = 2

    def __post_init__(self):
        if self.decimals < 0:
            raise ValueError(f"decimals must be >= 0, got {self.decimals}")

    def tick_size(self, price: Price) -> Decimal:
        return Decimal("1").scaleb(-self.decimals)

    def round_to_tick(self, price: Price, direction: RoundDirection = RoundDirection.NEAREST) -> Price:
        return round_to_grid(price, self.tick_size(price), direction)

    def decimals_for_display(self, price: Price) -> int:
        return self.decimals


# ────────────────────────────────────────────────────────────────────
# Sizing policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SimpleSizing:
    """notional = qty × price, in `quote_currency`.

    Used for US equity (qty in SHARES, price in USD → notional in USD)
    and for CFDs on shares (where the 1:1 sizing matches the
    underlying).
    """

    expected_unit: QuantityUnit
    quote_currency: Currency
    _min_qty: Quantity
    _qty_increment: Quantity

    @classmethod
    def for_us_equity(cls) -> "SimpleSizing":
        return cls(
            expected_unit=QuantityUnit.SHARES,
            quote_currency=Currency.USD,
            _min_qty=shares(1),
            _qty_increment=shares(1),
        )

    def notional(self, qty: Quantity, price: Price) -> Money:
        if qty.unit is not self.expected_unit:
            raise SizingMismatch(self.expected_unit, qty.unit)
        return Money(qty.value * price, self.quote_currency)

    def min_qty(self) -> Quantity:
        return self._min_qty

    def qty_increment(self) -> Quantity:
        return self._qty_increment

    def is_valid_qty(self, qty: Quantity) -> bool:
        if qty.unit is not self.expected_unit:
            return False
        if qty < self._min_qty:
            return False
        if self._qty_increment.is_zero:
            return True
        return ((qty - self._min_qty).value % self._qty_increment.value) == 0


# ────────────────────────────────────────────────────────────────────
# Commission policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class IBKRTieredEquityCommission:
    """IBKR Tiered fee schedule for US equity, Tier-1 bracket.

    Per-share: $0.0035
    Minimum:   $0.35 per order
    Maximum:   1% of trade value

    Plus exchange + clearing + regulatory fees (typically ~$0.0002/sh)
    which we currently approximate into the per-share rate. Good
    enough for a pre-trade estimate; the actual fee comes back on the
    fill and the engine reconciles.
    """

    per_share: Decimal = TIERED_PER_SHARE
    min_per_order: Decimal = TIERED_MIN_PER_ORDER
    max_pct_of_trade: Decimal = TIERED_MAX_PCT_OF_TRADE

    def estimate(self, qty: Quantity, price: Price, side: Side, venue: str = "SMART") -> Money:
        if qty.unit is not QuantityUnit.SHARES:
            raise SizingMismatch(QuantityUnit.SHARES, qty.unit)
        n_shares = abs(qty.value)
        raw_fee = n_shares * self.per_share
        capped_max = abs(qty.value * price) * self.max_pct_of_trade
        # Apply tier rules: at least min, at most max
        fee = max(self.min_per_order, min(raw_fee, capped_max))
        return Money(fee, Currency.USD)


# ────────────────────────────────────────────────────────────────────
# Session policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class USEquitySession:
    """NYSE RTH (09:30-16:00 ET) Monday-Friday, US federal holiday
    calendar.

    Returns SessionWindows in UTC so downstream code never has to
    think about Eastern Time edge cases (DST transitions, etc.). The
    conversion uses `zoneinfo` which respects DST automatically.

    Half-day sessions (Christmas Eve, day after Thanksgiving) close at
    13:00 ET. For Day-1 we treat these as full sessions — operator
    should manually halt at 13:00 if they care. Half-day support
    lands in week 2 when we add the full NYSE calendar.
    """

    open_local: time = NYSE_OPEN_LOCAL
    close_local: time = NYSE_CLOSE_LOCAL
    holidays: frozenset[date] = NYSE_HOLIDAYS_2024_2026

    def is_open_at(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware (UTC)")
        windows = self.windows_for_date(ts)
        return any(w.contains(ts) for w in windows)

    def windows_for_date(self, ts: datetime) -> list[SessionWindow]:
        # Convert to ET to check the local date (avoids midnight-UTC
        # bugs around the date boundary).
        local = ts.astimezone(NY_TZ)
        local_date = local.date()
        # Weekend
        if local_date.weekday() >= 5:  # 5=Sat, 6=Sun
            return []
        # Holiday
        if local_date in self.holidays:
            return []
        open_local = datetime.combine(local_date, self.open_local, tzinfo=NY_TZ)
        close_local = datetime.combine(local_date, self.close_local, tzinfo=NY_TZ)
        return [SessionWindow(
            open_utc=open_local.astimezone(UTC),
            close_utc=close_local.astimezone(UTC),
        )]

    def next_open(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        # Walk forward day-by-day. Bounded by ~10 days (longest possible
        # gap = Thanksgiving-week or holiday adjacent to weekend).
        check_date = ts.astimezone(NY_TZ).date()
        for _ in range(14):
            windows = self.windows_for_date(
                datetime.combine(check_date, time(12, 0), tzinfo=NY_TZ).astimezone(UTC)
            )
            for w in windows:
                if w.open_utc > ts:
                    return w.open_utc
            check_date += timedelta(days=1)
        raise RuntimeError(f"Could not find next open within 14 days of {ts}")

    def next_close(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        # If currently in a session, return its close.
        windows = self.windows_for_date(ts)
        for w in windows:
            if w.contains(ts):
                return w.close_utc
        # Otherwise the next session's close
        next_o = self.next_open(ts)
        next_windows = self.windows_for_date(next_o)
        return next_windows[0].close_utc

    def is_within_n_minutes_of_close(self, ts: datetime, minutes: int) -> bool:
        if not self.is_open_at(ts):
            return False
        close = self.next_close(ts)
        return (close - ts) <= timedelta(minutes=minutes)

    def time_to_close(self, ts: datetime) -> Optional[int]:
        if not self.is_open_at(ts):
            return None
        return int((self.next_close(ts) - ts).total_seconds())


# ────────────────────────────────────────────────────────────────────
# Lifecycle policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NoLifecycle:
    """Default lifecycle for assets that don't roll, expire, or
    require special settlement handling at the engine level.

    Used by:
      * US equity (T+1 settlement is accounting, not engine concern)
      * Forex cash (continuous settlement)
      * Index CFDs (continuous; financing handled separately by spec)
    """

    settlement_days_: int = 1  # T+1 for US equity since May 2024

    def needs_roll(self, contract: "Contract", ts: datetime) -> bool:
        return False  # nothing to roll

    def expiry(self, contract: "Contract") -> Optional[date]:
        return None  # non-expiring

    def settlement_days(self) -> int:
        return self.settlement_days_

    def has_overnight_financing(self) -> bool:
        return False


# ────────────────────────────────────────────────────────────────────
# Risk overlay — equity uses no extra overlay
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class USEquityRiskOverlay:
    """For US equity, the universal RiskGate (exposure cap, daily loss,
    consec losses, max trades) is sufficient. This overlay is a
    pass-through that lets the engine call `spec.risk_overlay.check()`
    uniformly without an `if asset_class == EQUITY: skip overlay`
    branch.
    """

    def check(self, intent: OrderIntent, portfolio: PortfolioView) -> RiskVerdict:
        return RiskVerdict.ok(reason="US equity uses universal risk gates only")


# ────────────────────────────────────────────────────────────────────
# Spec factory
# ────────────────────────────────────────────────────────────────────

def make_us_equity_spec(_symbol: Optional[str] = None) -> AssetSpec:
    """Construct the US equity AssetSpec.

    Argument exists for API symmetry with asset-class factories that
    DO branch on symbol (e.g. Forex pair → tick size). For US equity,
    every symbol gets the same spec.
    """
    return AssetSpec(
        asset_class=AssetClass.US_EQUITY,
        quote_currency=Currency.USD,
        venue="SMART",
        contract=SMARTStockContract(),
        price=LastPricePolicy(),
        tick=DecimalTickPolicy(decimals=2),
        sizing=SimpleSizing.for_us_equity(),
        commission=IBKRTieredEquityCommission(),
        session=USEquitySession(),
        lifecycle=NoLifecycle(settlement_days_=1),
        risk_overlay=USEquityRiskOverlay(),
    )


# ────────────────────────────────────────────────────────────────────
# Registry hook
# ────────────────────────────────────────────────────────────────────

# US equity symbol heuristic: 1-5 uppercase letters, optional dot-suffix
# (BRK.B, BF.B). NOT FX pairs (those are 6 chars, no dot) or futures
# roots (those typically need a contract month).
import re
_US_EQUITY_PATTERN = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _us_equity_resolver(symbol: str, hint: Optional[AssetClass]) -> Optional[AssetSpec]:
    """SpecRegistry resolver for US equity.

    Matches: PLTR, AAPL, MSFT, BRK.B, etc.
    Doesn't match: EURUSD (6 letters, no dot), ES (would match shape
    but should be claimed by FuturesSpec first via lower priority).

    The hint mechanism lets the operator force a different
    interpretation: passing hint=AssetClass.SHARE_CFD with "AAPL"
    would skip this resolver in favor of the share-CFD one.
    """
    if hint is not None and hint is not AssetClass.US_EQUITY:
        return None
    if not _US_EQUITY_PATTERN.match(symbol.upper()):
        return None
    return make_us_equity_spec(symbol)


# Register at module import time. Priority 100 = default; FX and
# Futures resolvers will register at higher priority (lower number)
# so they win for symbols that match both shapes (e.g. "ES" as a
# futures root vs ES the ticker — futures should claim it).
SpecRegistry.register(_us_equity_resolver, priority=100)
