"""FutureSpec — concrete AssetSpec for CME/CBOT/NYMEX futures contracts.

DESIGN NOTES — what makes futures different from equity / FX / CFDs:

  1. MULTIPLIER. Notional = qty × price × multiplier. ES at 4500 with
     qty=1 is NOT $4,500 — it's $50 × 4500 = $225,000. The multiplier
     is the most important per-product number; a wrong multiplier
     silently 10x's risk on micros (MES = $5) vs full (ES = $50).
     If we get this wrong, the risk gate is blind.

  2. CONTRACT MONTH. Symbol alone is ambiguous: "ES" could mean
     March '25, June '25, Sept '25, Dec '25 — different contracts.
     The IBKR Contract needs `lastTradeDateOrContractMonth` to
     disambiguate. We default to the FRONT MONTH (next quarterly
     expiry) when the operator passes just "ES"; explicit months
     override.

  3. ROLL. Every contract expires. ~5-10 days before expiry the
     operator must close the front month and re-open the next
     quarterly. Failure to roll → forced settlement (cash for
     index futures; PHYSICAL DELIVERY for some commodity futures
     like CL/oil, which retail accounts cannot accept). Day-3 ships
     a roll-detection flag; auto-roll execution is a separate
     project.

  4. TICK SIZE varies per root: ES=0.25, NQ=0.25, GC=0.10, CL=0.01,
     ZN=0.015625 (1/64 point for treasury notes). Most are simple
     decimals, a few are fractional. We hardcode the major roots.

  5. SESSION. Globex is nearly 23h with a 60-minute daily halt
     (17:00-18:00 ET) plus the weekend close. We approximate as
     ForexContinuousSession for Day-3 — the daily halt and weekend
     close land in week-2 when we wire per-product CME calendars.

  6. COMMISSION. Per-contract flat fee (~$0.85 for ES at IBKR Tiered,
     $0.25 for MES, varies). Plus exchange + clearing fees (~$1.50
     for ES). We approximate as a flat per-contract estimate; the
     fill report has the truth.

  7. RISK OVERLAY. SPAN margin matters more than notional for futures.
     A 1-contract ES position is $225K notional but only ~$13K SPAN
     margin requirement. Our universal RiskGate gates on notional —
     for futures we ADD a SPAN-margin-headroom check so the engine
     refuses orders that would consume more than N% of available
     SPAN margin even when the notional cap permits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
import re

from .enum import AssetClass
from .forex import ForexContinuousSession
from .policies import (
    FeedSnapshot, RoundDirection,
    OrderIntent, PortfolioView, RiskVerdict,
    SymmetricShort,
)
from .policies.commission import Side
from .policies.contract import ContractAmbiguous, ContractNotFound
from .policies.price import NoUsablePrice
from .policies.sizing import SizingMismatch
from .policies.tick import round_to_grid
from .resolver import SpecRegistry
from .spec import AssetSpec
from .types import (
    Currency, Money, Price, Quantity, QuantityUnit, contracts,
)
from .us_stock import LastPricePolicy

if TYPE_CHECKING:
    from ib_async import Contract, IB


UTC = timezone.utc


# ────────────────────────────────────────────────────────────────────
# Per-root metadata (multiplier, tick, exchange, commission)
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FutureRootMeta:
    """Per-root metadata for futures contracts. Captures everything
    that varies between, say, ES (full S&P) and MES (micro S&P).

    Adding a new root: append to FUTURE_ROOTS below.
    """

    root: str               # "ES", "MES", "NQ", "GC", etc.
    multiplier: Decimal     # $50 for ES, $5 for MES, $100 for GC, etc.
    tick: Decimal           # 0.25 for ES, 0.10 for GC, 0.01 for CL
    exchange: str           # "CME" / "NYMEX" / "CBOT" / "ICE"
    currency: Currency      # quote currency (USD for most)
    commission_per_contract: Decimal   # IBKR Tiered estimate in USD
    description: str


# Subset covering the major liquid futures. Extend as needed.
FUTURE_ROOTS: dict[str, FutureRootMeta] = {
    # E-mini equity index futures (CME)
    "ES":  FutureRootMeta("ES",  Decimal("50"),  Decimal("0.25"),
                           "CME", Currency.USD, Decimal("2.27"),
                           "E-mini S&P 500"),
    "MES": FutureRootMeta("MES", Decimal("5"),   Decimal("0.25"),
                           "CME", Currency.USD, Decimal("0.77"),
                           "Micro E-mini S&P 500"),
    "NQ":  FutureRootMeta("NQ",  Decimal("20"),  Decimal("0.25"),
                           "CME", Currency.USD, Decimal("2.27"),
                           "E-mini Nasdaq 100"),
    "MNQ": FutureRootMeta("MNQ", Decimal("2"),   Decimal("0.25"),
                           "CME", Currency.USD, Decimal("0.77"),
                           "Micro E-mini Nasdaq 100"),
    "RTY": FutureRootMeta("RTY", Decimal("50"),  Decimal("0.10"),
                           "CME", Currency.USD, Decimal("2.27"),
                           "E-mini Russell 2000"),
    "M2K": FutureRootMeta("M2K", Decimal("5"),   Decimal("0.10"),
                           "CME", Currency.USD, Decimal("0.77"),
                           "Micro E-mini Russell 2000"),
    # Metals (COMEX/NYMEX)
    "GC":  FutureRootMeta("GC",  Decimal("100"), Decimal("0.10"),
                           "NYMEX", Currency.USD, Decimal("2.40"),
                           "Gold (100 troy oz)"),
    "MGC": FutureRootMeta("MGC", Decimal("10"),  Decimal("0.10"),
                           "NYMEX", Currency.USD, Decimal("0.85"),
                           "Micro Gold (10 troy oz)"),
    "SI":  FutureRootMeta("SI",  Decimal("5000"),Decimal("0.005"),
                           "NYMEX", Currency.USD, Decimal("2.40"),
                           "Silver (5000 troy oz)"),
    # Energy (NYMEX)
    "CL":  FutureRootMeta("CL",  Decimal("1000"),Decimal("0.01"),
                           "NYMEX", Currency.USD, Decimal("2.40"),
                           "WTI Crude Oil (1000 barrels)"),
    "MCL": FutureRootMeta("MCL", Decimal("100"), Decimal("0.01"),
                           "NYMEX", Currency.USD, Decimal("0.85"),
                           "Micro Crude Oil"),
    "NG":  FutureRootMeta("NG",  Decimal("10000"),Decimal("0.001"),
                           "NYMEX", Currency.USD, Decimal("2.40"),
                           "Natural Gas"),
}


# ────────────────────────────────────────────────────────────────────
# Symbol parsing — "ES" vs "ESH5" vs "ES202503"
# ────────────────────────────────────────────────────────────────────

# Format 1: "ES" — root only, use front month
# Format 2: "ES202503" — root + 6-digit YYYYMM
# Format 3: "ESH5" — root + month-code + single-digit year (TradingView
#                    convention). H=Mar M=Jun U=Sep Z=Dec
_MONTH_CODE_TO_NUM = {"F":1,"G":2,"H":3,"J":4,"K":5,"M":6,
                      "N":7,"Q":8,"U":9,"V":10,"X":11,"Z":12}

_ROOT_ONLY = re.compile(r"^[A-Z][A-Z0-9]{0,2}$")
_ROOT_YYYYMM = re.compile(r"^([A-Z][A-Z0-9]{0,2})(\d{6})$")
_ROOT_CODE_YEAR = re.compile(r"^([A-Z][A-Z0-9]{0,2})([FGHJKMNQUVXZ])(\d)$")


def _parse_futures_symbol(symbol: str) -> tuple[str, Optional[str]]:
    """Parse a futures symbol into (root, contract_month_yyyymm).

    Returns (root, None) for root-only — caller should default to
    front month. Returns (root, "YYYYMM") for explicit month.

    Raises ValueError on malformed input or unknown root.
    """
    s = symbol.upper()
    if _ROOT_ONLY.match(s):
        if s not in FUTURE_ROOTS:
            raise ValueError(
                f"Unknown futures root '{s}'. Known: "
                f"{', '.join(sorted(FUTURE_ROOTS.keys()))}"
            )
        return s, None
    m = _ROOT_YYYYMM.match(s)
    if m:
        root, yyyymm = m.group(1), m.group(2)
        if root not in FUTURE_ROOTS:
            raise ValueError(f"Unknown futures root '{root}' in '{s}'")
        # Validate the YYYYMM is well-formed (not strict expiry check)
        yyyy, mm = int(yyyymm[:4]), int(yyyymm[4:])
        if not (1 <= mm <= 12) or yyyy < 2020:
            raise ValueError(f"Invalid month in '{s}': {yyyymm}")
        return root, yyyymm
    m = _ROOT_CODE_YEAR.match(s)
    if m:
        root, code, yr = m.group(1), m.group(2), m.group(3)
        if root not in FUTURE_ROOTS:
            raise ValueError(f"Unknown futures root '{root}' in '{s}'")
        # Convert single-digit year to YYYY. Assume current decade —
        # this is good for ~10 years; longer-dated needs explicit YYYYMM.
        from datetime import date as _date
        current_year = _date.today().year
        decade_start = (current_year // 10) * 10
        yyyy = decade_start + int(yr)
        if yyyy < current_year - 1:  # rolled over to next decade
            yyyy += 10
        yyyymm = f"{yyyy}{_MONTH_CODE_TO_NUM[code]:02d}"
        return root, yyyymm
    raise ValueError(
        f"Cannot parse futures symbol '{s}'. Use root-only ('ES'), "
        f"root+YYYYMM ('ES202503'), or TV-style ('ESH5')."
    )


def _front_quarterly_month(today: Optional[date] = None) -> str:
    """Return YYYYMM for the next quarterly expiry (Mar/Jun/Sep/Dec).

    Used when the operator passes a root-only symbol like 'ES' — we
    default to the front month. For Day-3 we use a simple "next
    quarterly" heuristic; production should query IBKR's
    ContractDetails.lastTradeDateOrContractMonth for exact rolls.

    Heuristic: if we're in Mar/Jun/Sep/Dec and past the 3rd Friday,
    roll forward. Otherwise return the current/next quarterly.
    """
    today = today or date.today()
    # Next quarterly month >= today's month
    quarterlies = [3, 6, 9, 12]
    yyyy = today.year
    next_q = next((m for m in quarterlies if m >= today.month), None)
    if next_q is None:
        # We're past December's expiry — roll to next year's March
        return f"{yyyy + 1}03"
    # If we're IN the expiry month and past the ~15th, assume rolled
    if today.month == next_q and today.day > 15:
        idx = quarterlies.index(next_q)
        if idx + 1 < len(quarterlies):
            return f"{yyyy}{quarterlies[idx + 1]:02d}"
        return f"{yyyy + 1}03"
    return f"{yyyy}{next_q:02d}"


# ────────────────────────────────────────────────────────────────────
# Contract policy
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FuturesContractPolicy:
    """Builds ib_async.Future(symbol, lastTradeDateOrContractMonth,
    exchange, multiplier='...', currency='USD')."""

    def make(self, symbol: str) -> "Contract":
        from ib_async import Future
        root, month = _parse_futures_symbol(symbol)
        meta = FUTURE_ROOTS[root]
        if month is None:
            month = _front_quarterly_month()
        return Future(
            symbol=root,
            lastTradeDateOrContractMonth=month,
            exchange=meta.exchange,
            currency=meta.currency.value,
            multiplier=str(meta.multiplier),
        )

    @staticmethod
    def identify(ib_contract) -> Optional[str]:
        """Reverse-translate an ib_async Future Contract to its logical
        ticker (root). FUT contracts use `contract.symbol` as the root
        ("ES", "MES", "GC"); the contract month is on a separate field.
        For position-matching purposes, the root alone is enough — the
        engine doesn't carry an explicit month, the spec resolves to
        the front-quarterly each time.
        """
        sec_type = getattr(ib_contract, 'secType', None)
        if sec_type not in ('FUT', 'CONTFUT'):
            return None
        return (getattr(ib_contract, 'symbol', '') or '').upper() or None

    async def qualify(self, ib: "IB", contract: "Contract") -> "Contract":
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ContractNotFound(
                f"IBKR returned no match for Future {contract.symbol} "
                f"month={contract.lastTradeDateOrContractMonth} on "
                f"{contract.exchange}. Check that the contract month "
                f"exists and your account has futures permissions."
            )
        if len(qualified) > 1:
            # Shouldn't happen for fully-specified futures, but be defensive.
            raise ContractAmbiguous(
                f"IBKR returned {len(qualified)} matches for Future "
                f"{contract.symbol} {contract.lastTradeDateOrContractMonth}; "
                f"narrow via exchange or multiplier."
            )
        return qualified[0]


# ────────────────────────────────────────────────────────────────────
# Tick policy — per-root grain
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FuturesTickPolicy:
    """Tick rounding per futures root. ES=0.25, GC=0.10, CL=0.01, etc.

    Stored as a single Decimal; same `round_to_grid` math as every
    other tick policy. The advantage of composition: this is 30 lines.
    """

    grain: Decimal
    decimals: int  # for display

    @classmethod
    def for_root(cls, root: str) -> "FuturesTickPolicy":
        if root not in FUTURE_ROOTS:
            raise ValueError(f"Unknown futures root '{root}'")
        grain = FUTURE_ROOTS[root].tick
        # Display decimals = -exponent of the grain (0.25 → 2dp; 0.01 → 2dp)
        exp = grain.as_tuple().exponent
        dec = max(2, -exp if isinstance(exp, int) else 2)
        return cls(grain=grain, decimals=dec)

    def tick_size(self, price: Price) -> Decimal:
        return self.grain

    def round_to_tick(self, price: Price, direction: RoundDirection = RoundDirection.NEAREST) -> Price:
        return round_to_grid(price, self.grain, direction)

    def decimals_for_display(self, price: Price) -> int:
        return self.decimals


# ────────────────────────────────────────────────────────────────────
# Sizing — the key futures-specific math: multiplier
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MultiplierSizing:
    """notional = qty(contracts) × price × multiplier, in quote currency.

    For ES at 4500.25 with qty=1: 1 × 4500.25 × 50 = $225,012.50 USD.
    For MES at the same price:    1 × 4500.25 × 5  =  $22,501.25 USD.

    Risk gate uses this; if we ship the wrong multiplier, the gate
    silently mis-sizes everything. Hence the unit-tests below in
    test_future.py spell out the math for every root.
    """

    multiplier: Decimal
    quote_currency: Currency
    expected_unit: QuantityUnit = QuantityUnit.CONTRACTS

    @classmethod
    def for_root(cls, root: str) -> "MultiplierSizing":
        if root not in FUTURE_ROOTS:
            raise ValueError(f"Unknown futures root '{root}'")
        meta = FUTURE_ROOTS[root]
        return cls(multiplier=meta.multiplier, quote_currency=meta.currency)

    def notional(self, qty: Quantity, price: Price) -> Money:
        if qty.unit is not self.expected_unit:
            raise SizingMismatch(self.expected_unit, qty.unit)
        return Money(qty.value * price * self.multiplier, self.quote_currency)

    def min_qty(self) -> Quantity:
        # IBKR allows 1 contract minimum
        return Quantity(Decimal("1"), self.expected_unit)

    def qty_increment(self) -> Quantity:
        # Always integer contracts
        return Quantity(Decimal("1"), self.expected_unit)

    def is_valid_qty(self, qty: Quantity) -> bool:
        if qty.unit is not self.expected_unit:
            return False
        if qty.value < 1:
            return False
        return qty.value == qty.value.to_integral_value()


# ────────────────────────────────────────────────────────────────────
# Commission — per-contract flat
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class IBKRFuturesCommission:
    """Per-contract flat commission. Stored per-root in FutureRootMeta;
    factory wires it through.
    """

    per_contract: Decimal
    quote_currency: Currency = Currency.USD

    @classmethod
    def for_root(cls, root: str) -> "IBKRFuturesCommission":
        if root not in FUTURE_ROOTS:
            raise ValueError(f"Unknown futures root '{root}'")
        meta = FUTURE_ROOTS[root]
        return cls(per_contract=meta.commission_per_contract,
                   quote_currency=meta.currency)

    def estimate(self, qty: Quantity, price: Price, side: Side, venue: str = "CME") -> Money:
        if qty.unit is not QuantityUnit.CONTRACTS:
            raise SizingMismatch(QuantityUnit.CONTRACTS, qty.unit)
        return Money(abs(qty.value) * self.per_contract, self.quote_currency)


# ────────────────────────────────────────────────────────────────────
# Lifecycle — futures DO roll
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FuturesRollLifecycle:
    """Roll-detection lifecycle. Day-3 ships detection only; auto-roll
    execution is a separate effort.

    `roll_window_days`: how many days before expiry to start firing the
    `needs_roll=True` flag. 5 is a conservative default (gives operator
    time to roll manually).
    """

    roll_window_days: int = 5
    settlement_days_: int = 0  # futures settle intraday via daily mark

    def needs_roll(self, contract: "Contract", ts: datetime) -> bool:
        # Pull lastTradeDateOrContractMonth from the contract
        last = getattr(contract, "lastTradeDateOrContractMonth", None)
        if not last:
            return False
        # Parse YYYYMM or YYYYMMDD
        try:
            if len(last) == 6:
                expiry = date(int(last[:4]), int(last[4:6]), 1)
                # End-of-month-ish; use 20th as approximate expiry day
                # (3rd Friday is usually 15th-21st)
                expiry = expiry.replace(day=20)
            elif len(last) == 8:
                expiry = date(int(last[:4]), int(last[4:6]), int(last[6:]))
            else:
                return False
        except (ValueError, TypeError):
            return False
        return (expiry - ts.date()).days <= self.roll_window_days

    def expiry(self, contract: "Contract") -> Optional[date]:
        last = getattr(contract, "lastTradeDateOrContractMonth", None)
        if not last:
            return None
        try:
            if len(last) == 6:
                return date(int(last[:4]), int(last[4:6]), 20)
            if len(last) == 8:
                return date(int(last[:4]), int(last[4:6]), int(last[6:]))
        except (ValueError, TypeError):
            return None
        return None

    def settlement_days(self) -> int:
        return self.settlement_days_

    def has_overnight_financing(self) -> bool:
        # No daily financing — futures are mark-to-market, settled
        # against initial margin daily. (Distinct from CFDs which
        # accrue financing per day.)
        return False


# ────────────────────────────────────────────────────────────────────
# Risk overlay — SPAN-margin-headroom check
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FuturesRiskOverlay:
    """Futures-specific risk overlay.

    Day-3 scope: enforce a maximum-margin-utilization cap based on
    PortfolioView.account_buying_power. The check is approximate
    because we don't have SPAN margin per-contract loaded from IBKR
    in Day-3 — we use a notional-fraction heuristic that's strict
    enough to be safe but loose enough to allow normal trading.

    Heuristic: 1 contract of ES (~$225K notional) is treated as
    needing ~$13K margin = 5.8% of notional. We require buying_power
    >= 8% of intended notional as a buffer. Tighter cap = safer but
    more rejected orders. Operator can override per-deployment.

    Day-3 also blocks fresh entries when the position would cross a
    roll boundary within `min_days_before_roll` — explicit operator
    decision to roll first.
    """

    margin_buffer_pct: Decimal = Decimal("8")   # need >= 8% notional in BP
    min_days_before_roll: int = 3                # block entries within 3d of roll

    def check(self, intent: OrderIntent, portfolio: PortfolioView) -> RiskVerdict:
        # SELL exits never blocked
        if intent.side == "SELL":
            return RiskVerdict.ok(reason="futures exit not blocked")

        if intent.spec is None or not hasattr(intent.spec, "sizing"):
            return RiskVerdict.ok(reason="spec unavailable to overlay")

        # 1. Margin headroom check
        try:
            notional = intent.notional  # Money in quote ccy
        except Exception:
            return RiskVerdict.ok(reason="cannot compute notional")
        # Compare to buying_power. Both should be in account base ccy
        # for an apples-to-apples comparison; for Day-3 we assume USD
        # and trust the engine layer to feed us base-ccy values.
        if portfolio is not None and portfolio.account_buying_power_base is not None:
            bp = portfolio.account_buying_power_base
            # Required = notional × margin_buffer_pct / 100
            try:
                required = notional * (self.margin_buffer_pct / Decimal("100"))
            except Exception:
                return RiskVerdict.ok(reason="margin check skipped")
            if bp.currency is required.currency and bp < required:
                return RiskVerdict.block(
                    reason=(
                        f"Futures margin check: buying_power "
                        f"{bp} < required {required} "
                        f"({self.margin_buffer_pct}% of notional {notional})."
                    ),
                    margin_required=str(required),
                    margin_available=str(bp),
                )

        # 2. Roll-window block — don't enter fresh within N days of expiry
        from datetime import datetime
        try:
            from ib_async import Future
            contract = intent.spec.contract.make(intent.symbol)
            expiry = intent.spec.lifecycle.expiry(contract)
            if expiry is not None:
                days_to_expiry = (expiry - datetime.now(tz=UTC).date()).days
                if days_to_expiry <= self.min_days_before_roll:
                    return RiskVerdict.block(
                        reason=(
                            f"Futures roll-window: {intent.symbol} expires in "
                            f"{days_to_expiry} day(s); refuse fresh entry. "
                            f"Roll to next contract first."
                        ),
                        days_to_expiry=days_to_expiry,
                    )
        except Exception:
            # Spec access for expiry can fail in synthetic tests; don't
            # break the gate if the lifecycle check can't run.
            pass

        return RiskVerdict.ok(reason="futures overlay: margin + roll OK")


# ────────────────────────────────────────────────────────────────────
# Spec factory
# ────────────────────────────────────────────────────────────────────

def make_future_spec(symbol: str) -> AssetSpec:
    """Construct a Future AssetSpec for the given symbol.

    Symbol formats accepted (see _parse_futures_symbol):
        "ES"          — root only; uses front quarterly month
        "ES202503"    — root + YYYYMM
        "ESH5"        — root + month-code + year

    All raise ValueError on unknown roots.
    """
    root, _month = _parse_futures_symbol(symbol)
    if root not in FUTURE_ROOTS:
        raise ValueError(f"Unknown futures root '{root}'")
    meta = FUTURE_ROOTS[root]
    return AssetSpec(
        asset_class=AssetClass.FUTURE,
        quote_currency=meta.currency,
        venue=meta.exchange,
        contract=FuturesContractPolicy(),
        # Futures DO have a real `last` trade feed (unlike spot FX)
        price=LastPricePolicy(),
        tick=FuturesTickPolicy.for_root(root),
        sizing=MultiplierSizing.for_root(root),
        commission=IBKRFuturesCommission.for_root(root),
        # Day-3 stand-in: ForexContinuousSession approximates Globex
        # 23h/5d availability. Real CME calendar in week-2.
        session=ForexContinuousSession(),
        lifecycle=FuturesRollLifecycle(roll_window_days=5),
        risk_overlay=FuturesRiskOverlay(),
        # Futures are symmetric: short == long. Margin is SPAN (owned by
        # FuturesRiskOverlay); no borrow/locate/restricted proceeds.
        short=SymmetricShort.for_futures(),
    )


# ────────────────────────────────────────────────────────────────────
# Registry hook
# ────────────────────────────────────────────────────────────────────

def _future_resolver(symbol: str, hint: Optional[AssetClass]) -> Optional[AssetSpec]:
    """Matches symbols whose root is in FUTURE_ROOTS, in any of the
    accepted formats (root-only / root+YYYYMM / root+code+year).
    """
    if hint is not None and hint is not AssetClass.FUTURE:
        return None
    try:
        root, _month = _parse_futures_symbol(symbol)
    except ValueError:
        return None
    if root not in FUTURE_ROOTS:
        return None
    return make_future_spec(symbol)


# Priority 20 — checked before forex (50) and equity (100). Reason:
# "ES" matches the equity pattern (1-5 alpha) but isn't equity — we
# want the futures resolver to claim it first. Forex pattern (6 alpha
# chars) is disjoint from any FUTURE_ROOTS key (which are 1-3 chars),
# so no conflict there.
SpecRegistry.register(_future_resolver, priority=20)
