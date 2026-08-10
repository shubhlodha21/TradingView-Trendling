"""ForexSpec — concrete AssetSpec for spot Forex via IDEALPRO.

DESIGN NOTES:

  Forex is the asset class most different from equity. The 2026-06-03
  EURUSD diagnostic dump confirmed:
    * `feed.last` is STALE (7 seconds old in the snapshot) and
      `lastSize` is 0 — IDEALPRO doesn't broadcast trade prints
    * bid / ask DO populate reliably with 1-pip spreads
    * minTick = 0.00005 (half-pip on EURUSD)
    * Quantities are in BASE_UNITS (e.g. 25,000 EUR), not "shares"
    * No daily close — continuous Sunday 22:00 UTC → Friday 22:00 UTC

  Therefore ForexSpec composes:
    IDEALPROForexContract  — Forex(pair) auto-routes to IDEALPRO
    BidAskComparePricing   — buy_compare=ask, sell_compare=bid,
                             reference=mid; raises on last (it's lying)
    PipTickPolicy          — 0.00005 for most pairs, 0.005 for JPY pairs
    FXBaseCurrencySizing   — notional = qty(base) × price(rate) in quote ccy
    IBKRFXCommission       — 0.20 bps of notional, min $2, capped
    ForexContinuousSession — Sun 22:00 UTC → Fri 22:00 UTC
    NoLifecycle            — cash FX doesn't roll/expire (T+2 settlement)
    FXRiskOverlay          — weekend-gap awareness + swap accrual flag

The pair string convention is the 6-char ISO form: "EURUSD" means
"EUR/USD" (base=EUR, quote=USD). We parse base/quote and derive
sizing currency from the quote side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from .enum import AssetClass
from .policies import (
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
    Currency, Money, Price, Quantity, QuantityUnit, base_units,
)
from .us_stock import NoLifecycle  # reuse — FX cash also doesn't roll

if TYPE_CHECKING:
    from ib_async import Contract, IB


# ────────────────────────────────────────────────────────────────────
# Constants — Forex-specific
# ────────────────────────────────────────────────────────────────────

UTC = timezone.utc
NY_TZ = ZoneInfo("America/New_York")

# Pairs whose quote currency is JPY use 3-decimal ticks instead of 5.
# Per IBKR IDEALPRO: minTick = 0.005 (half-pip in JPY's smaller grid).
JPY_PAIR_TICK = Decimal("0.005")
DEFAULT_PAIR_TICK = Decimal("0.00005")  # 5dp (half-pip) for non-JPY

# IBKR IDEALPRO FX commission: 0.20 bps of trade value
# (= 0.00002 = 0.00002 fraction), minimum $2 USD per order.
FX_COMMISSION_BPS = Decimal("0.20")  # 0.20 basis points = 0.00002 fraction
FX_COMMISSION_MIN_USD = Decimal("2.00")

# Most IDEALPRO pairs require a minimum 25,000 base-unit order.
# (Some venues let you down to 1; safer default is 25k.)
FX_MIN_BASE_UNITS = Decimal("25000")
# Increment is 1 base unit at IBKR (no rounded lots required).
FX_QTY_INCREMENT = Decimal("1")

# Pair shape: 6 uppercase letters, no dot, with both halves resolving
# to a known Currency.
_PAIR_PATTERN = re.compile(r"^[A-Z]{6}$")


def _split_pair(pair: str) -> tuple[Currency, Currency]:
    """Split 'EURUSD' → (EUR, USD). Raises ValueError if either half
    isn't a known Currency."""
    p = pair.upper()
    if not _PAIR_PATTERN.match(p):
        raise ValueError(f"Forex pair must be 6 uppercase letters, got '{pair}'")
    base_s, quote_s = p[:3], p[3:]
    try:
        return Currency(base_s), Currency(quote_s)
    except ValueError as e:
        raise ValueError(f"Unknown currency in pair '{pair}': {e}") from None


def _tick_for_pair(pair: str) -> Decimal:
    """JPY-quoted pairs use 3dp; everything else 5dp."""
    _, quote = _split_pair(pair)
    return JPY_PAIR_TICK if quote is Currency.JPY else DEFAULT_PAIR_TICK


# ────────────────────────────────────────────────────────────────────
# Contract policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class IDEALPROForexContract:
    """ib_async.Forex(pair) builds a Contract with secType='CASH',
    exchange='IDEALPRO', symbol=base, currency=quote. We don't need
    to set primary_exchange — IDEALPRO is unique."""

    def make(self, symbol: str) -> "Contract":
        from ib_async import Forex
        # ib_async.Forex accepts the 6-char string and parses base/quote
        # internally. Validate here too so the error is clear at our
        # boundary, not buried in IBKR's RPC reply.
        _split_pair(symbol)  # raises ValueError if shape's wrong
        return Forex(symbol.upper())

    @staticmethod
    def identify(ib_contract) -> Optional[str]:
        """Reverse-translate an ib_async Contract back to its logical FX
        pair symbol ("EURUSD", "USDJPY", etc.).

        WHY THIS EXISTS: ib_async stores Forex("EURUSD") with
        `contract.symbol="EUR"` (the base currency) and
        `contract.currency="USD"` (the quote currency) — the literal
        pair string is NOT preserved on the contract. When the engine
        asks IBKR for positions, the position's contract carries the
        base ccy as `symbol`, so a naive `pos.symbol == "EURUSD"` match
        returns False and the engine wrongly concludes it's FLAT —
        triggering POSITION_MISMATCH folds that kill the protective
        stop. (Observed live 2026-06-05 on EURUSD.)

        Returns:
            The 6-char pair string if `ib_contract` is an IDEALPRO CASH
            contract with both base+quote set. None otherwise — the
            caller (Gateway.get_positions) treats None as "this is not
            an FX position; let another policy claim it."
        """
        sec_type = getattr(ib_contract, 'secType', None)
        if sec_type != 'CASH':
            return None
        base = (getattr(ib_contract, 'symbol', '') or '').upper()
        quote = (getattr(ib_contract, 'currency', '') or '').upper()
        if len(base) == 3 and len(quote) == 3:
            return base + quote
        return None

    async def qualify(self, ib: "IB", contract: "Contract") -> "Contract":
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ContractNotFound(
                f"IBKR returned no match for Forex {contract.symbol}{contract.currency}. "
                f"Check that IDEALPRO is enabled on your account and the pair is supported."
            )
        return qualified[0]


# ────────────────────────────────────────────────────────────────────
# Price policy — the key difference from equity
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class BidAskComparePricing:
    """Forex-correct price extraction:

      reference   = (bid + ask) / 2     — unbiased mid, used for tracking
      buy_compare = ask                  — what you ACTUALLY PAY on a BUY
      sell_compare = bid                 — what you ACTUALLY RECEIVE on a SELL

    Never reads feed.last because IDEALPRO's `last` is stale/lying
    (confirmed via diagnostic dump 2026-06-03: lastSize=0, lastTimestamp
    7 seconds older than the snapshot's bid update).

    is_actionable: requires both bid and ask present, spread <= configurable
    cap (default 5 pips for liquid majors), bid < ask (not crossed),
    feed age < 5s during FX hours.
    """

    # Max acceptable bid-ask spread in pips (1 pip = 0.0001 for most
    # pairs, 0.01 for JPY). Wider than this means the feed is
    # disclaiming itself (illiquid window, news halt, etc.) and we
    # shouldn't act on it.
    max_actionable_spread_pips: Decimal = Decimal("5")

    # FX feeds update many times per second during liquid hours; 5s
    # is generous.
    max_actionable_age_seconds: float = 5.0

    def reference(self, feed: FeedSnapshot) -> Price:
        if feed.bid is None or feed.ask is None:
            raise NoUsablePrice(
                f"Forex feed missing bid/ask (bid={feed.bid}, ask={feed.ask}); "
                f"never falls back to last on IDEALPRO."
            )
        return Price((feed.bid + feed.ask) / 2)

    def buy_compare(self, feed: FeedSnapshot) -> Price:
        if feed.ask is None:
            raise NoUsablePrice("Forex feed missing ask; cannot compare BUY trigger")
        return Price(feed.ask)

    def sell_compare(self, feed: FeedSnapshot) -> Price:
        if feed.bid is None:
            raise NoUsablePrice("Forex feed missing bid; cannot compare SELL stop")
        return Price(feed.bid)

    def is_actionable(self, feed: FeedSnapshot) -> bool:
        if feed.bid is None or feed.ask is None:
            return False
        if feed.bid >= feed.ask:
            return False  # crossed/locked
        spread = feed.ask - feed.bid
        # Detect pair scale: JPY pairs quote prices like 153.25 so a
        # 5-pip spread is 0.05, not 0.0005. Heuristic: if mid > 50,
        # assume JPY-style (10^-2 pip).
        mid = (feed.bid + feed.ask) / 2
        pip_value = Decimal("0.01") if mid > 50 else Decimal("0.0001")
        spread_pips = spread / pip_value
        if spread_pips > self.max_actionable_spread_pips:
            return False
        if feed.ts is not None:
            age = (datetime.now(tz=UTC) - feed.ts).total_seconds()
            if age > self.max_actionable_age_seconds:
                return False
        return True


# ────────────────────────────────────────────────────────────────────
# Tick policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PipTickPolicy:
    """Forex tick policy parameterized by pair (because JPY pairs use
    a coarser grid than other majors).

    EURUSD, GBPUSD, AUDUSD, etc.: 0.00005 (5dp, half-pip)
    USDJPY, EURJPY, etc.:          0.005   (3dp, half-pip on JPY's scale)
    """

    tick: Decimal  # store the resolved tick so tick_size is O(1)
    decimals: int  # display precision derived from tick

    @classmethod
    def for_pair(cls, pair: str) -> "PipTickPolicy":
        t = _tick_for_pair(pair)
        # Number of decimal places = -log10(tick) when tick is a power
        # of 10 * single digit. For 0.00005 → 5; for 0.005 → 3.
        d = abs(t.adjusted()) + (1 if t.as_tuple()[1][0] == 5 else 0)
        # Cleaner: 0.00005 has exponent -5 (5dp); 0.005 has exponent -3 (3dp).
        d = -t.as_tuple().exponent
        return cls(tick=t, decimals=d)

    def tick_size(self, price: Price) -> Decimal:
        return self.tick

    def round_to_tick(self, price: Price, direction: RoundDirection = RoundDirection.NEAREST) -> Price:
        return round_to_grid(price, self.tick, direction)

    def decimals_for_display(self, price: Price) -> int:
        return self.decimals


# ────────────────────────────────────────────────────────────────────
# Sizing policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FXBaseCurrencySizing:
    """notional = qty(base_units) × price(rate) → Money(quote_currency).

    For EURUSD at 1.16175 with qty=25000 (EUR):
      notional = 25000 × 1.16175 = 29043.75 USD

    For USDJPY at 153.25 with qty=10000 (USD):
      notional = 10000 × 153.25 = 1,532,500 JPY

    Note that the QUOTE currency is what comes out of notional. The
    risk gate sums across pairs in JPY, USD, EUR etc., so the
    CurrencyService converts to base later.
    """

    expected_unit: QuantityUnit = QuantityUnit.BASE_UNITS
    quote_currency: Currency = Currency.USD  # set per-pair at construction

    @classmethod
    def for_pair(cls, pair: str) -> "FXBaseCurrencySizing":
        _, quote = _split_pair(pair)
        return cls(expected_unit=QuantityUnit.BASE_UNITS, quote_currency=quote)

    def notional(self, qty: Quantity, price: Price) -> Money:
        if qty.unit is not self.expected_unit:
            raise SizingMismatch(self.expected_unit, qty.unit)
        return Money(qty.value * price, self.quote_currency)

    def min_qty(self) -> Quantity:
        return Quantity(FX_MIN_BASE_UNITS, QuantityUnit.BASE_UNITS)

    def qty_increment(self) -> Quantity:
        return Quantity(FX_QTY_INCREMENT, QuantityUnit.BASE_UNITS)

    def is_valid_qty(self, qty: Quantity) -> bool:
        if qty.unit is not self.expected_unit:
            return False
        if qty < self.min_qty():
            return False
        # IBKR allows 1-unit increments on FX so any integer-quantity >= 25k
        # passes. Allow fractional too — rare but legal.
        return True


# ────────────────────────────────────────────────────────────────────
# Commission policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class IBKRFXCommission:
    """0.20 bps of notional, min $2 per order. Always charged in USD
    by IBKR regardless of the pair; we report it as Money(USD) so
    cross-pair commission accumulates cleanly.

    For non-USD-quoted pairs the engine should pass the converted USD
    notional in `price * qty` form via the cross-currency adapter (D2-PM
    wires this). For Day-2 AM we just compute against the raw notional
    in quote ccy and label as USD — close enough for risk gate
    estimates; final fee comes back on fill.
    """

    bps: Decimal = FX_COMMISSION_BPS
    min_usd: Decimal = FX_COMMISSION_MIN_USD

    def estimate(self, qty: Quantity, price: Price, side: Side, venue: str = "IDEALPRO") -> Money:
        if qty.unit is not QuantityUnit.BASE_UNITS:
            raise SizingMismatch(QuantityUnit.BASE_UNITS, qty.unit)
        # Notional in quote ccy. For risk estimation we assume USD-equivalent
        # (cross-currency conversion proper happens in D2-PM wiring).
        notional_quote = abs(qty.value * price)
        # 0.20 bps = 0.20 / 10000 = 0.00002
        raw_fee = notional_quote * (self.bps / Decimal("10000"))
        fee = max(raw_fee, self.min_usd)
        return Money(fee, Currency.USD)


# ────────────────────────────────────────────────────────────────────
# Session policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ForexContinuousSession:
    """24/5 Forex hours.

    Open:  Sunday 22:00 UTC  (17:00 ET — Auckland session begins)
    Close: Friday 22:00 UTC  (17:00 ET — NY close)

    No intraday breaks. No US holidays observed (the global FX market
    routes through London/Tokyo/Sydney during US holidays). We
    deliberately DON'T flag US holidays here — operator can layer
    holiday halts via the risk gate if they care.

    Daylight-saving caveat: the 22:00 UTC anchor stays fixed
    year-round because IBKR pegs IDEALPRO hours to UTC, not local
    DST. ET observers see the open/close shift by an hour twice a
    year — that's the venue's design, not ours.
    """

    # Day-of-week the weekly session opens. Sunday=6 in Python's
    # datetime.weekday() (Mon=0).
    open_dow: int = 6        # Sunday
    open_time_utc: time = time(22, 0)
    close_dow: int = 4       # Friday
    close_time_utc: time = time(22, 0)

    def is_open_at(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        # Find this week's open and close
        windows = self.windows_for_date(ts)
        return any(w.contains(ts) for w in windows)

    def windows_for_date(self, ts: datetime) -> list[SessionWindow]:
        """Return the weekly open→close window that contains or
        starts on the same week as `ts`. Forex has ONE continuous
        weekly window — not per-day windows like equity."""
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        ts_utc = ts.astimezone(UTC)
        # Find the Sunday 22:00 UTC at or before this ts
        # (or the next Sunday if ts is on Saturday).
        days_back_to_sunday = (ts_utc.weekday() - self.open_dow) % 7
        if days_back_to_sunday == 0 and ts_utc.time() < self.open_time_utc:
            # Same Sunday, before 22:00 → use last week's Sunday
            days_back_to_sunday = 7
        sunday = (ts_utc - timedelta(days=days_back_to_sunday)).replace(
            hour=self.open_time_utc.hour,
            minute=self.open_time_utc.minute,
            second=0, microsecond=0,
        )
        # Close: the following Friday 22:00 UTC (5 days later)
        friday = sunday + timedelta(days=5)  # Sun→Fri = 5 days
        try:
            return [SessionWindow(open_utc=sunday, close_utc=friday)]
        except ValueError:
            return []

    def next_open(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        ts_utc = ts.astimezone(UTC)
        # If we're inside the current weekly window, next open is NEXT week
        for w in self.windows_for_date(ts_utc):
            if w.open_utc > ts_utc:
                return w.open_utc
            if w.contains(ts_utc):
                # Inside this week's window → next open is next week
                return w.open_utc + timedelta(days=7)
        # Otherwise: scan forward until we find the next Sunday 22:00
        days_ahead = (self.open_dow - ts_utc.weekday()) % 7
        if days_ahead == 0 and ts_utc.time() >= self.open_time_utc:
            days_ahead = 7
        return (ts_utc + timedelta(days=days_ahead)).replace(
            hour=self.open_time_utc.hour, minute=self.open_time_utc.minute,
            second=0, microsecond=0,
        )

    def next_close(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        ts_utc = ts.astimezone(UTC)
        for w in self.windows_for_date(ts_utc):
            if ts_utc < w.close_utc:
                return w.close_utc
        # Inside a future window's close
        return self.next_open(ts_utc) + timedelta(days=5)

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
# Risk overlay
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FXRiskOverlay:
    """FX-specific risk overlay.

    What it adds beyond the universal RiskGate:

      1. Weekend-gap awareness — entering a fresh BUY within N minutes
         of the Friday 22:00 UTC weekly close adds Sunday-open gap
         risk (the FX market re-opens at a potentially very different
         price). Refuses new entries within this window.

      2. Spread-of-mid sanity — if the snapshot's spread is unusually
         wide (already gated by PricePolicy.is_actionable but worth
         double-checking here in case the operator overrides), refuse.

    Day-1 scope: just weekend-gap. Spread check covered by
    PricePolicy. Swap-rate accrual (held-overnight FX positions)
    parked for week 2.
    """

    # Refuse fresh BUY entries within this many minutes of Friday close.
    no_entry_minutes_before_close: int = 30

    def check(self, intent: OrderIntent, portfolio: PortfolioView) -> RiskVerdict:
        # We only block entries; exits (SELL) should always work.
        if intent.side == "SELL":
            return RiskVerdict.ok(reason="FX exits never blocked by overlay")
        # Check weekend-gap window. We need the session, which the
        # intent's spec carries.
        if intent.spec is None or not hasattr(intent.spec, "session"):
            # Defensive: if spec or session missing, don't block. The
            # universal gate already covers the core checks.
            return RiskVerdict.ok(reason="spec/session unavailable to overlay")
        now = datetime.now(tz=UTC)
        if intent.spec.session.is_within_n_minutes_of_close(
            now, self.no_entry_minutes_before_close,
        ):
            return RiskVerdict.block(
                reason=(
                    f"FX weekend-gap guard: within "
                    f"{self.no_entry_minutes_before_close} min of weekly close. "
                    f"Sunday-open gap risk; refuse new entries."
                ),
                blocked_by_overlay="FXRiskOverlay.weekend_gap",
            )
        return RiskVerdict.ok(reason="FX overlay: all checks pass")


# ────────────────────────────────────────────────────────────────────
# Spec factory
# ────────────────────────────────────────────────────────────────────

def make_forex_spec(pair: str) -> AssetSpec:
    """Construct the Forex AssetSpec for the given pair.

    Examples:
        make_forex_spec("EURUSD") → quote=USD, tick=0.00005
        make_forex_spec("USDJPY") → quote=JPY, tick=0.005
        make_forex_spec("GBPJPY") → quote=JPY, tick=0.005
    """
    pair = pair.upper()
    _, quote = _split_pair(pair)  # validates + extracts quote ccy

    return AssetSpec(
        asset_class=AssetClass.FX_CASH,
        quote_currency=quote,
        venue="IDEALPRO",
        contract=IDEALPROForexContract(),
        price=BidAskComparePricing(),
        tick=PipTickPolicy.for_pair(pair),
        sizing=FXBaseCurrencySizing.for_pair(pair),
        commission=IBKRFXCommission(),
        session=ForexContinuousSession(),
        lifecycle=NoLifecycle(settlement_days_=2),  # FX cash T+2
        risk_overlay=FXRiskOverlay(),
    )


# ────────────────────────────────────────────────────────────────────
# Registry hook
# ────────────────────────────────────────────────────────────────────

def _forex_resolver(symbol: str, hint: Optional[AssetClass]) -> Optional[AssetSpec]:
    """SpecRegistry resolver for spot Forex pairs.

    Matches: 6 uppercase alpha, both halves resolve to known
    Currency. Examples: EURUSD, GBPUSD, USDJPY.

    Does NOT match: PLTR (4 chars), EURUSDT (7 chars), EUR.USD (with dot).
    """
    if hint is not None and hint is not AssetClass.FX_CASH:
        return None
    p = symbol.upper()
    if not _PAIR_PATTERN.match(p):
        return None
    # Validate both halves are known currencies (without raising on
    # mismatch — we just return None so the next resolver gets a chance)
    try:
        _split_pair(p)
    except ValueError:
        return None
    return make_forex_spec(p)


# Priority 50 — checked BEFORE US equity (priority 100) because a
# 6-letter symbol like "EURUSD" could theoretically match equity
# shape (≤5 letters... actually no, equity is ≤5 chars so 6 wouldn't
# match the equity regex). The priority is here for safety if equity
# pattern ever loosens.
SpecRegistry.register(_forex_resolver, priority=50)
