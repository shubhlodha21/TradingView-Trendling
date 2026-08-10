"""CFD specs — Index CFD, Share CFD, FX CFD.

DESIGN NOTES:

  CFDs are settlement-by-difference: you never own the underlying,
  the broker pays/charges the daily mark-to-market. IBKR offers three
  CFD families:

    INDEX_CFD  — e.g. IBUS500 (S&P), IBDE40 (DAX), IBGB100 (FTSE)
                 24h venue tracking the underlying index
                 USD/EUR/GBP quote depending on the index
                 1 unit = $1 per index point typically

    SHARE_CFD  — e.g. AAPL, MSFT, TSLA as CFD
                 Hours = underlying equity exchange RTH
                 Quote currency matches underlying (USD for US shares)
                 1 unit = 1 share equivalent (sizing is 1:1)

    FX_CFD     — e.g. EUR.USD as CFD (different from spot Forex on
                 IDEALPRO — uses CFD routing, different commission +
                 financing model)
                 Continuous like spot FX

  All three share:
    * Overnight financing charge (the big difference from equity/futures)
    * CFDContract construction (secType='CFD' in ib_async)
    * No corporate action handling (CFDs don't get dividends/splits the
      same way the underlying does — broker adjusts via mark)

  What varies per sub-type:
    * Tick size (varies by underlying)
    * Pricing convention (index has mark; share has bid/ask/last like equity)
    * Sizing math (1:1 for share CFDs; per-point for index CFDs)
    * Quote currency (mixed across all)
    * Session (varies — share CFDs follow equity RTH, index 23h)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from .enum import AssetClass
from .forex import (
    BidAskComparePricing,
    ForexContinuousSession,
    PipTickPolicy,
    _PAIR_PATTERN,
    _split_pair,
)
from .policies import (
    FeedSnapshot, RoundDirection,
    OrderIntent, PortfolioView, RiskVerdict,
    CFDShort, SymmetricShort,
)
from .policies.commission import Side
from .policies.contract import ContractNotFound
from .policies.sizing import SizingMismatch
from .policies.tick import round_to_grid
from .resolver import SpecRegistry
from .spec import AssetSpec
from .types import (
    Currency, Money, Price, Quantity, QuantityUnit,
    base_units, cfd_units, shares,
)
from .us_stock import (
    DecimalTickPolicy, LastPricePolicy, NoLifecycle,
    SimpleSizing, USEquitySession,
    _US_EQUITY_PATTERN,
)

if TYPE_CHECKING:
    from ib_async import Contract, IB


# ────────────────────────────────────────────────────────────────────
# Shared constants
# ────────────────────────────────────────────────────────────────────

UTC = timezone.utc

# IBKR's CFD daily financing rate: benchmark (overnight risk-free) +
# spread. We don't compute it precisely here — that's the operator's
# concern at MD-discussion time. We just FLAG that the position will
# accrue financing if held overnight.

# Known index CFDs (subset; extend as needed). Maps symbol → metadata.
INDEX_CFD_METADATA: dict[str, dict] = {
    "IBUS500": {"currency": Currency.USD, "tick": Decimal("0.25"), "venue": "SMART", "underlying": "S&P 500"},
    "IBUS30":  {"currency": Currency.USD, "tick": Decimal("1"),    "venue": "SMART", "underlying": "Dow Jones"},
    "IBUST100":{"currency": Currency.USD, "tick": Decimal("0.25"), "venue": "SMART", "underlying": "Nasdaq 100"},
    "IBUSM2000":{"currency": Currency.USD,"tick": Decimal("0.1"),  "venue": "SMART", "underlying": "Russell 2000"},
    "IBDE40":  {"currency": Currency.EUR, "tick": Decimal("0.5"),  "venue": "SMART", "underlying": "DAX 40"},
    "IBGB100": {"currency": Currency.GBP, "tick": Decimal("0.5"),  "venue": "SMART", "underlying": "FTSE 100"},
    "IBJP225": {"currency": Currency.JPY, "tick": Decimal("5"),    "venue": "SMART", "underlying": "Nikkei 225"},
    "IBEU50":  {"currency": Currency.EUR, "tick": Decimal("1"),    "venue": "SMART", "underlying": "Euro Stoxx 50"},
    # Eurozone
    "IBFR40":  {"currency": Currency.EUR, "tick": Decimal("0.5"),  "venue": "SMART", "underlying": "CAC 40"},
    "IBES35":  {"currency": Currency.EUR, "tick": Decimal("1"),    "venue": "SMART", "underlying": "IBEX 35"},
    "IBNL25":  {"currency": Currency.EUR, "tick": Decimal("0.05"), "venue": "SMART", "underlying": "AEX 25"},
    "IBIT40":  {"currency": Currency.EUR, "tick": Decimal("1"),    "venue": "SMART", "underlying": "FTSE MIB 40"},
    # Switzerland
    "IBCH20":  {"currency": Currency.CHF, "tick": Decimal("0.5"),  "venue": "SMART", "underlying": "SMI 20"},
    # APAC
    "IBAU200": {"currency": Currency.AUD, "tick": Decimal("1"),    "venue": "SMART", "underlying": "ASX 200"},
    "IBHK50":  {"currency": Currency.HKD, "tick": Decimal("1"),    "venue": "SMART", "underlying": "Hang Seng 50"},
    # Energy — tick/currency below are safe defaults; cross_validate adopts
    # the broker's real minTick at qualify time (see broker._build_contract).
    # Symbols vary by IBKR region/entitlement — confirm each qualifies on
    # your account (an unknown symbol fails safe at qualify: Error 200, no trade).
    "IBUSOIL":  {"currency": Currency.USD, "tick": Decimal("0.01"),  "venue": "SMART", "underlying": "WTI Crude Oil"},
    "IBUKOIL":  {"currency": Currency.USD, "tick": Decimal("0.01"),  "venue": "SMART", "underlying": "Brent Crude Oil"},
    "IBNATGAS": {"currency": Currency.USD, "tick": Decimal("0.001"), "venue": "SMART", "underlying": "Natural Gas (Henry Hub)"},
    # Metals — spot metal codes ("XAU"/"XAG"/… are not ISO currencies, so
    # the forex resolver never claims them; safe to list here as INDEX_CFD).
    "XAUUSD":  {"currency": Currency.USD, "tick": Decimal("0.01"),  "venue": "SMART", "underlying": "Gold Spot oz"},
    "XAGUSD":  {"currency": Currency.USD, "tick": Decimal("0.001"), "venue": "SMART", "underlying": "Silver Spot oz"},
    "XPTUSD":  {"currency": Currency.USD, "tick": Decimal("0.1"),   "venue": "SMART", "underlying": "Platinum Spot oz"},
    "XPDUSD":  {"currency": Currency.USD, "tick": Decimal("0.1"),   "venue": "SMART", "underlying": "Palladium Spot oz"},
    # Commodities
    "XCUUSD":  {"currency": Currency.USD, "tick": Decimal("0.0005"),"venue": "SMART", "underlying": "Copper Spot lb"},
    # ── Forex CFDs are intentionally NOT listed here ──────────────────
    # An entry in this dict is claimed as INDEX_CFD by _index_cfd_resolver
    # at priority 30, which runs BEFORE the spot-forex resolver (priority
    # 50). Listing e.g. "EURUSD" here would hijack a plain resolve("EURUSD")
    # away from spot FX. FX-pair CFDs already have a first-class path:
    # make_fx_cfd_spec via hint=FX_CFD (or the generic --cfd route), which
    # preserves the pip grid + quote-currency handling. Add FX there, not here.
}

# ────────────────────────────────────────────────────────────────────
# CFD contract construction
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CFDContract:
    """Builds an IBKR CFD contract. ib_async ships a CFD class which
    just sets secType='CFD' on a Stock-shaped object.

    `currency` and `exchange` vary per CFD family — caller supplies.
    """

    currency: Currency
    exchange: str = "SMART"

    def make(self, symbol: str) -> "Contract":
        from ib_async import CFD
        return CFD(symbol.upper(), self.exchange, self.currency.value)

    @staticmethod
    def identify(ib_contract) -> Optional[str]:
        """Reverse-translate an ib_async CFD Contract to its logical
        ticker. CFDs preserve the symbol on the contract (unlike FX),
        so `contract.symbol` is the logical name. Match on secType to
        avoid claiming stocks/futures with the same ticker.
        """
        sec_type = getattr(ib_contract, 'secType', None)
        if sec_type != 'CFD':
            return None
        return (getattr(ib_contract, 'symbol', '') or '').upper() or None

    async def qualify(self, ib: "IB", contract: "Contract") -> "Contract":
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ContractNotFound(
                f"IBKR returned no match for CFD {contract.symbol} on "
                f"{contract.exchange}/{contract.currency}. "
                f"Confirm CFDs are enabled on your account and the symbol "
                f"is supported (some CFDs require region-specific entitlements)."
            )
        return qualified[0]


# ────────────────────────────────────────────────────────────────────
# Pricing — index CFDs use a "mark" model
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CFDMarkPricing:
    """Index CFDs have continuous mark-to-market prices. The feed
    populates bid/ask normally; we treat them like FX (mid for
    tracking, ask for buy, bid for sell). `last` is sometimes
    populated by the underlying's print stream — we accept it as a
    secondary fallback.
    """

    max_actionable_spread_bps: Decimal = Decimal("20")
    max_actionable_age_seconds: float = 10.0

    def reference(self, feed: FeedSnapshot) -> Price:
        if feed.bid is not None and feed.ask is not None:
            return Price((feed.bid + feed.ask) / 2)
        if feed.last is not None:
            return Price(feed.last)
        from .policies.price import NoUsablePrice
        raise NoUsablePrice("CFD feed missing bid/ask/last")

    def buy_compare(self, feed: FeedSnapshot) -> Price:
        if feed.ask is not None:
            return Price(feed.ask)
        if feed.last is not None:
            return Price(feed.last)
        from .policies.price import NoUsablePrice
        raise NoUsablePrice("CFD feed missing ask")

    def sell_compare(self, feed: FeedSnapshot) -> Price:
        if feed.bid is not None:
            return Price(feed.bid)
        if feed.last is not None:
            return Price(feed.last)
        from .policies.price import NoUsablePrice
        raise NoUsablePrice("CFD feed missing bid")

    def is_actionable(self, feed: FeedSnapshot) -> bool:
        if feed.bid is None or feed.ask is None:
            return False
        if feed.bid >= feed.ask:
            return False
        mid = (feed.bid + feed.ask) / 2
        if mid > 0:
            spread_bps = (feed.ask - feed.bid) / mid * Decimal("10000")
            if spread_bps > self.max_actionable_spread_bps:
                return False
        if feed.ts is not None:
            age = (datetime.now(tz=UTC) - feed.ts).total_seconds()
            if age > self.max_actionable_age_seconds:
                return False
        return True


# ────────────────────────────────────────────────────────────────────
# Tick — fixed grain for index CFDs (per-CFD), reuses DecimalTick for shares
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FixedGrainTickPolicy:
    """Tick = fixed grain (e.g. 0.25 for S&P 500 CFD). Display
    decimals derived from grain.
    """

    grain: Decimal
    decimals: int  # for display

    @classmethod
    def for_grain(cls, grain: Decimal) -> "FixedGrainTickPolicy":
        # Compute display decimals: 0.25 → 2dp; 1 → 0dp; 0.5 → 1dp.
        exp = grain.as_tuple().exponent
        dec = max(0, -exp if isinstance(exp, int) else 0)
        return cls(grain=grain, decimals=dec)

    def tick_size(self, price: Price) -> Decimal:
        return self.grain

    def round_to_tick(self, price: Price, direction: RoundDirection = RoundDirection.NEAREST) -> Price:
        return round_to_grid(price, self.grain, direction)

    def decimals_for_display(self, price: Price) -> int:
        return self.decimals


# ────────────────────────────────────────────────────────────────────
# Sizing — CFDs use 1:1 with underlying units
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CFDSizing:
    """CFD sizing — notional = qty × price in quote currency.

    For share CFDs: qty is in CFD_UNITS (1:1 with underlying shares),
    price is per share. Math identical to SimpleSizing.

    For index CFDs: qty is in CFD_UNITS, price is index level,
    notional = qty × index_level (in quote currency). The "$X per
    point" implicit multiplier IS the price — index CFDs at IBKR are
    priced such that one unit moves $1 per 1-point underlying move
    (for USD-quoted indices).
    """

    quote_currency: Currency
    _min_qty_value: Decimal = Decimal("1")
    expected_unit: QuantityUnit = QuantityUnit.CFD_UNITS

    def notional(self, qty: Quantity, price: Price) -> Money:
        if qty.unit is not self.expected_unit:
            raise SizingMismatch(self.expected_unit, qty.unit)
        return Money(qty.value * price, self.quote_currency)

    def min_qty(self) -> Quantity:
        return Quantity(self._min_qty_value, self.expected_unit)

    def qty_increment(self) -> Quantity:
        return Quantity(Decimal("1"), self.expected_unit)

    def is_valid_qty(self, qty: Quantity) -> bool:
        if qty.unit is not self.expected_unit:
            return False
        if qty < self.min_qty():
            return False
        # Integer increment
        return qty.value == qty.value.to_integral_value()


# ────────────────────────────────────────────────────────────────────
# Commission — CFDs have explicit per-trade commission + financing
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class IBKRCFDCommission:
    """IBKR's published CFD fee schedule (Tier-1 retail):

      Share CFD (US):  0.005 USD per share (min $1)
      Index CFD (US):  0.005% of trade value (min $1)
      Index CFD (EU):  0.005% of trade value (min EUR 1)
      FX CFD:          built into spread; commission line = 0

    We approximate as a per-notional bps for simplicity. Engine
    reconciles the actual fee from the fill commission report.
    """

    bps: Decimal = Decimal("5")      # 0.05% = 5 bps
    min_fee: Decimal = Decimal("1")  # $1 minimum
    quote_currency: Currency = Currency.USD

    def estimate(self, qty: Quantity, price: Price, side: Side, venue: str = "SMART") -> Money:
        if qty.unit not in (QuantityUnit.CFD_UNITS, QuantityUnit.BASE_UNITS):
            raise SizingMismatch(QuantityUnit.CFD_UNITS, qty.unit)
        notional = abs(qty.value * price)
        raw_fee = notional * (self.bps / Decimal("10000"))
        fee = max(raw_fee, self.min_fee)
        return Money(fee, self.quote_currency)


# ────────────────────────────────────────────────────────────────────
# CFD lifecycle — has_overnight_financing=True (the key flag)
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CFDLifecycle:
    """Same as NoLifecycle for roll/expiry — CFDs don't roll. But the
    overnight-financing flag flips True so risk overlays can warn
    when an operator wants to hold a CFD overnight.
    """

    settlement_days_: int = 0  # continuous mark-to-market

    def needs_roll(self, contract: "Contract", ts: datetime) -> bool:
        return False

    def expiry(self, contract: "Contract") -> Optional[object]:
        return None

    def settlement_days(self) -> int:
        return self.settlement_days_

    def has_overnight_financing(self) -> bool:
        return True


# ────────────────────────────────────────────────────────────────────
# Risk overlay — daily financing cost awareness
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CFDRiskOverlay:
    """CFD-specific risk:

      1. Overnight-financing flag — if holding intent is for hours/days
         (we can't know from the order alone), warn but don't block.
         The operator should sanity-check that expected edge > daily
         financing cost. Day-1: surfaced as a note, not a block.

      2. Spread sanity — already covered by PricePolicy.is_actionable.

    Block conditions: NONE in Day-1. The overlay is informational.
    """

    def check(self, intent: OrderIntent, portfolio: PortfolioView) -> RiskVerdict:
        # Always allow; just annotate that financing applies
        return RiskVerdict.ok(
            reason="CFD allow (financing accrues overnight; track separately)",
            has_overnight_financing=True,
        )


# ────────────────────────────────────────────────────────────────────
# Spec factories
# ────────────────────────────────────────────────────────────────────

def make_index_cfd_spec(symbol: str) -> AssetSpec:
    """Construct an Index CFD AssetSpec. Symbol must be in the
    INDEX_CFD_METADATA registry."""
    s = symbol.upper()
    if s not in INDEX_CFD_METADATA:
        raise ValueError(
            f"Unknown index CFD symbol '{symbol}'. Known: "
            f"{', '.join(sorted(INDEX_CFD_METADATA.keys()))}. "
            f"Extend INDEX_CFD_METADATA to add more."
        )
    meta = INDEX_CFD_METADATA[s]
    return AssetSpec(
        asset_class=AssetClass.INDEX_CFD,
        quote_currency=meta["currency"],
        venue=meta["venue"],
        contract=CFDContract(currency=meta["currency"], exchange=meta["venue"]),
        price=CFDMarkPricing(),
        tick=FixedGrainTickPolicy.for_grain(meta["tick"]),
        sizing=CFDSizing(quote_currency=meta["currency"]),
        commission=IBKRCFDCommission(quote_currency=meta["currency"]),
        # Index CFDs trade nearly 23h (tracking the underlying); for
        # Day-1 we use ForexContinuousSession as the 24/5 stand-in.
        # Per-index trading hours land in week-2.
        session=ForexContinuousSession(),
        lifecycle=CFDLifecycle(),
        risk_overlay=CFDRiskOverlay(),
        # Index CFD: synthetic, ~5% margin, financing carry. No borrow.
        short=CFDShort(margin_rate=Decimal("0.05")),
    )


def make_share_cfd_spec(symbol: str) -> AssetSpec:
    """Construct a Share CFD AssetSpec. Symbol matches the underlying
    (e.g. AAPL CFD has symbol 'AAPL'). Behaves like equity but with
    CFD lifecycle / risk overlay.

    Default currency is USD; international share CFDs would need
    extension (out of Day-1 scope).
    """
    s = symbol.upper()
    return AssetSpec(
        asset_class=AssetClass.SHARE_CFD,
        quote_currency=Currency.USD,
        venue="SMART",
        contract=CFDContract(currency=Currency.USD, exchange="SMART"),
        # Shares of underlying have real trade prints — last is usable
        price=LastPricePolicy(),
        # Share CFDs use penny ticks like the underlying
        tick=DecimalTickPolicy(decimals=2),
        # CFD_UNITS sizing 1:1 with underlying shares
        sizing=CFDSizing(quote_currency=Currency.USD),
        commission=IBKRCFDCommission(quote_currency=Currency.USD),
        # Share CFDs trade during the underlying's RTH (US equity hours)
        session=USEquitySession(),
        lifecycle=CFDLifecycle(),
        risk_overlay=CFDRiskOverlay(),
        # Share CFD: synthetic, ~20% margin, financing carry. No borrow
        # of real shares (unlike the equity short it mirrors).
        short=CFDShort(margin_rate=Decimal("0.20")),
    )


def make_fx_cfd_spec(pair: str) -> AssetSpec:
    """Construct an FX CFD AssetSpec (different from spot Forex on
    IDEALPRO). Same pricing/tick as spot FX but commission + lifecycle
    are CFD-style (financing accrues, fee built into spread).
    """
    pair = pair.upper()
    _, quote = _split_pair(pair)
    return AssetSpec(
        asset_class=AssetClass.FX_CFD,
        quote_currency=quote,
        venue="SMART",  # FX CFDs route through SMART, not IDEALPRO
        contract=CFDContract(currency=quote, exchange="SMART"),
        # Same bid/ask semantics as spot FX
        price=BidAskComparePricing(),
        # Same pip grid
        tick=PipTickPolicy.for_pair(pair),
        # CFD-style sizing (CFD_UNITS not BASE_UNITS)
        sizing=CFDSizing(quote_currency=quote),
        # FX CFDs typically have commission baked into spread
        commission=IBKRCFDCommission(
            bps=Decimal("0"),       # zero explicit commission
            min_fee=Decimal("0"),
            quote_currency=quote,
        ),
        # Continuous like spot FX
        session=ForexContinuousSession(),
        lifecycle=CFDLifecycle(),
        risk_overlay=CFDRiskOverlay(),
        # FX CFD is symmetric like spot FX (no borrow/locate/restricted
        # proceeds); leverage margin + carry.
        short=SymmetricShort.for_fx(),
    )


def make_generic_cfd_spec(symbol: str) -> AssetSpec:
    """GENERIC CFD — the broker is the source of truth.

    Works for ANY symbol IBKR offers as a CFD (index, share, FX, metal,
    commodity) with NO per-symbol registry. The contract is just
    CFD(SYMBOL, "SMART", <currency>); `qualifyContracts` resolves the real
    instrument and `SpecRegistry.cross_validate` adopts the broker's actual
    minTick at startup. If the symbol isn't a CFD at IBKR, qualify fails
    safe (Error 200, no trade).

    Currency is *seeded* offline (only to initialise sizing / P&L):
      • listed in INDEX_CFD_METADATA → that currency + tick
      • looks like an FX pair (BBBQQQ)  → the quote currency + pip grid
      • otherwise                        → USD + a 0.01 default grid
    Broker-currency adoption (for a non-USD instrument that's neither
    listed nor an FX pair) is the one remaining refinement; USD is the seed.
    """
    s = symbol.upper()
    if s in INDEX_CFD_METADATA:
        ccy = INDEX_CFD_METADATA[s]["currency"]
        tick_policy = FixedGrainTickPolicy.for_grain(INDEX_CFD_METADATA[s]["tick"])
    elif _PAIR_PATTERN.match(s):
        try:
            _, ccy = _split_pair(s)
            tick_policy = PipTickPolicy.for_pair(s)
        except ValueError:
            ccy = Currency.USD
            tick_policy = FixedGrainTickPolicy.for_grain(Decimal("0.01"))
    else:
        ccy = Currency.USD
        tick_policy = FixedGrainTickPolicy.for_grain(Decimal("0.01"))
    return AssetSpec(
        asset_class=AssetClass.CFD,
        quote_currency=ccy,
        venue="SMART",
        contract=CFDContract(currency=ccy, exchange="SMART"),
        price=CFDMarkPricing(),           # bid/ask mid (works for every CFD kind)
        tick=tick_policy,                 # offline seed; broker minTick adopted at qualify
        sizing=CFDSizing(quote_currency=ccy),
        commission=IBKRCFDCommission(quote_currency=ccy),
        session=ForexContinuousSession(), # ~24/5; the feed gates entries when shut
        lifecycle=CFDLifecycle(),
        risk_overlay=CFDRiskOverlay(),
        # Generic CFD is synthetic: margin % + financing carry, no share
        # borrow. Conservative 20% seed (share-CFD-like); IBKR's whatIf is
        # the authoritative margin at order time.
        short=CFDShort(margin_rate=Decimal("0.20")),
    )


# ────────────────────────────────────────────────────────────────────
# Registry hooks
# ────────────────────────────────────────────────────────────────────

def _index_cfd_resolver(symbol: str, hint: Optional[AssetClass]) -> Optional[AssetSpec]:
    """Matches symbols in INDEX_CFD_METADATA. Always requires the
    hint OR an exact metadata match — we don't want SMART-route
    equity symbols accidentally claimed.
    """
    if hint is not None and hint is not AssetClass.INDEX_CFD:
        return None
    s = symbol.upper()
    if s in INDEX_CFD_METADATA:
        return make_index_cfd_spec(s)
    return None


def _share_cfd_resolver(symbol: str, hint: Optional[AssetClass]) -> Optional[AssetSpec]:
    """Share CFDs are claimed ONLY when the operator explicitly hints.
    Otherwise the same symbol routes to US_EQUITY by default (the
    underlying behaves identically; CFD path is opt-in for accounts
    that want CFD routing).
    """
    if hint is not AssetClass.SHARE_CFD:
        return None
    s = symbol.upper()
    if not _US_EQUITY_PATTERN.match(s):
        return None
    return make_share_cfd_spec(s)


def _fx_cfd_resolver(symbol: str, hint: Optional[AssetClass]) -> Optional[AssetSpec]:
    """FX CFDs require the hint — otherwise FX pairs route to spot
    Forex (IDEALPRO) by default."""
    if hint is not AssetClass.FX_CFD:
        return None
    s = symbol.upper()
    if not _PAIR_PATTERN.match(s):
        return None
    try:
        _split_pair(s)
    except ValueError:
        return None
    return make_fx_cfd_spec(s)


def _generic_cfd_resolver(symbol: str, hint: Optional[AssetClass]) -> Optional[AssetSpec]:
    """Catch-all CFD. Claims ANY symbol when the operator explicitly asks
    for a generic CFD (hint=CFD, set by run_live's --cfd). The broker is
    the source of truth — qualify resolves the real contract + minTick, so
    no per-symbol registry is needed (index / share / FX / metal / commodity
    all route here). Fires ONLY on hint=CFD, so default equity/FX/spot
    resolution is completely untouched."""
    if hint is not AssetClass.CFD:
        return None
    return make_generic_cfd_spec(symbol.upper())


# Priorities:
#   Index CFD — priority 30 (lower than forex's 50). Index CFD
#               symbols (e.g. IBUS500) are 6-7 chars but include
#               digits; they wouldn't match forex's strict alpha
#               pattern. Low priority is safety in case the metadata
#               grows.
#   Share CFD — priority 80 (between forex 50 and equity 100). Only
#               fires when hint=SHARE_CFD, so priority is mostly
#               cosmetic.
#   FX CFD    — priority 40 (just below forex 50). Only fires when
#               hint=FX_CFD; otherwise FX_CASH wins.
SpecRegistry.register(_index_cfd_resolver, priority=30)
SpecRegistry.register(_fx_cfd_resolver, priority=40)
SpecRegistry.register(_share_cfd_resolver, priority=80)
#   Generic CFD — priority 35. Hint-gated to AssetClass.CFD (set by --cfd),
#   which no other resolver returns, so priority is cosmetic; it's the
#   catch-all that claims any symbol and lets the broker qualify it.
SpecRegistry.register(_generic_cfd_resolver, priority=35)
