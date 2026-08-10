"""Strong-typed primitives for multi-asset trading.

This module exists because Python's `float` and bare `int` were how
the 2026-06-02 PLTR shorting bug entered the system. The engine
silently treated `30 PLTR shares` and `30 EURUSD-base-units` and
`30 MES-contracts` as the same value. They are not.

What lives here
---------------
* `Currency` — an enum of ISO-4217 currency codes we trade in.
* `QuantityUnit` — the unit a `Quantity.value` is denominated in:
  shares, futures-contracts, FX-base-units, option-contracts.
* `Quantity` — a `Decimal` value tagged with its `QuantityUnit`.
  Arithmetic across mismatched units RAISES at runtime. Negative
  values are explicitly representable but flagged to the caller.
* `Money` — a `Decimal` amount tagged with its `Currency`.
  Arithmetic across mismatched currencies RAISES. Conversion is
  explicit via `Money.to(target_currency, currency_service)` so the
  caller always sees which FX rate was used and when.
* `Price` — a `Decimal` price; thin wrapper exists so callers
  can't accidentally pass a `Money` where a `Price` is expected
  (notional vs unit price are different).

Why Decimal
-----------
IEEE-754 binary floats are NOT representable for prices like
`1.16175` (typical EURUSD quote) or `151.50` (typical equity
trigger). Multiplying through fills, P&L attribution, and
commission accruals on `float` accumulates drift that's invisible
in a single trade but real money over a quarter. `decimal.Decimal`
is the standard quant remediation; it's roughly 5x slower per
operation but at our tick rate that's nanoseconds we'll never miss.

Float is only allowed at the IBKR API boundary, where ib_async
itself uses float. We convert in/out at that boundary and stay
Decimal everywhere else.

Why these are dataclasses with `__add__` etc.
---------------------------------------------
We could subclass `Decimal` but that loses the unit/currency tag.
We could use plain tuples but lose arithmetic ergonomics. The
frozen-dataclass approach gives both: immutable values, typed
operations, and the unit/currency stays attached through every
calculation.

Examples
--------
    Quantity(Decimal("30"), QuantityUnit.SHARES) + \
        Quantity(Decimal("100"), QuantityUnit.SHARES)
        # → Quantity(Decimal("130"), QuantityUnit.SHARES) — OK

    Quantity(Decimal("30"), QuantityUnit.SHARES) + \
        Quantity(Decimal("1"), QuantityUnit.CONTRACTS)
        # → raises QuantityUnitMismatch("SHARES vs CONTRACTS")

    Money(Decimal("4545.00"), Currency.USD) + Money(Decimal("100"), Currency.EUR)
        # → raises CurrencyMismatch — must call .to(target, fx_service)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    # Avoid circular import. CurrencyService is in a sibling module
    # that imports `Money` from this file.
    from .currency_service import CurrencyService


# ────────────────────────────────────────────────────────────────────
# Currency
# ────────────────────────────────────────────────────────────────────

class Currency(Enum):
    """ISO-4217 currency codes we transact in.

    Members chosen to cover Day-1 scope (FX majors + account base) plus
    common quote currencies on the asset classes we'll add later.
    Add new currencies sparingly: every new value here implies a feed
    subscription requirement in CurrencyService.warm().
    """

    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    CHF = "CHF"
    CAD = "CAD"
    AUD = "AUD"
    NZD = "NZD"
    HKD = "HKD"
    SGD = "SGD"
    INR = "INR"  # for NSE/BSE if we ever wire global equity
    CNH = "CNH"  # offshore yuan, common FX cross

    def __repr__(self) -> str:
        return f"Currency.{self.name}"


# Account base currency convention. Engine config can override via
# AccountConfig.base_currency, but every default assumes this so
# greenfield bots Just Work.
DEFAULT_BASE_CURRENCY: Final[Currency] = Currency.USD


# ────────────────────────────────────────────────────────────────────
# Quantity
# ────────────────────────────────────────────────────────────────────

class QuantityUnit(Enum):
    """What a Quantity.value is denominated in.

    SHARES           — equity (1 share PLTR)
    CONTRACTS        — futures (1 ES contract = $50 × index)
    BASE_UNITS       — FX cash (25,000 EUR for EURUSD)
    OPTION_CONTRACTS — equity options (1 contract = 100 shares; deferred to options work)
    CFD_UNITS        — CFDs (semantics varies by venue; treat as 1:1 with underlying for now)
    """

    SHARES = "SHARES"
    CONTRACTS = "CONTRACTS"
    BASE_UNITS = "BASE_UNITS"
    OPTION_CONTRACTS = "OPTION_CONTRACTS"
    CFD_UNITS = "CFD_UNITS"


class QuantityUnitMismatch(TypeError):
    """Raised when arithmetic crosses incompatible unit boundaries.

    Carries both operands' units so the trace is actionable.
    """

    def __init__(self, left: QuantityUnit, right: QuantityUnit, op: str = "+") -> None:
        super().__init__(
            f"Cannot {op} Quantity[{left.name}] and Quantity[{right.name}]; "
            f"explicit conversion required or this is a real bug."
        )
        self.left = left
        self.right = right
        self.op = op


@dataclass(frozen=True, slots=True)
class Quantity:
    """A typed quantity. `value` is Decimal so we can express fractional
    FX base units (e.g. partial 25,000.50 EUR) without float drift.

    Negative values are allowed (a SELL fill reduces position by negative
    delta in some accounting models) but conventionally the engine keeps
    Quantity non-negative and tracks side separately. If you find yourself
    constructing a negative Quantity, check whether your math is right.
    """

    value: Decimal
    unit: QuantityUnit

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            # Help callers who pass int/float accidentally. We could
            # silently coerce; choosing to raise so the bug is named at
            # construction site, not 6 calls deep.
            raise TypeError(
                f"Quantity.value must be Decimal, got {type(self.value).__name__}. "
                f"Wrap as Decimal(str(value)) to preserve precision."
            )

    def __add__(self, other: "Quantity") -> "Quantity":
        if not isinstance(other, Quantity):
            return NotImplemented
        if self.unit is not other.unit:
            raise QuantityUnitMismatch(self.unit, other.unit, "+")
        return Quantity(self.value + other.value, self.unit)

    def __sub__(self, other: "Quantity") -> "Quantity":
        if not isinstance(other, Quantity):
            return NotImplemented
        if self.unit is not other.unit:
            raise QuantityUnitMismatch(self.unit, other.unit, "-")
        return Quantity(self.value - other.value, self.unit)

    def __mul__(self, scalar: int | Decimal) -> "Quantity":
        # Scaling a Quantity by a unitless scalar is well-defined.
        # Multiplying two Quantities together is NOT (units don't compose
        # that way) so we don't implement Quantity * Quantity.
        if isinstance(scalar, Quantity):
            raise QuantityUnitMismatch(self.unit, scalar.unit, "×")
        if isinstance(scalar, int):
            scalar = Decimal(scalar)
        if not isinstance(scalar, Decimal):
            return NotImplemented
        return Quantity(self.value * scalar, self.unit)

    __rmul__ = __mul__

    def __neg__(self) -> "Quantity":
        return Quantity(-self.value, self.unit)

    def __abs__(self) -> "Quantity":
        return Quantity(abs(self.value), self.unit)

    def __lt__(self, other: "Quantity") -> bool:
        if self.unit is not other.unit:
            raise QuantityUnitMismatch(self.unit, other.unit, "<")
        return self.value < other.value

    def __le__(self, other: "Quantity") -> bool:
        if self.unit is not other.unit:
            raise QuantityUnitMismatch(self.unit, other.unit, "<=")
        return self.value <= other.value

    def __gt__(self, other: "Quantity") -> bool:
        if self.unit is not other.unit:
            raise QuantityUnitMismatch(self.unit, other.unit, ">")
        return self.value > other.value

    def __ge__(self, other: "Quantity") -> bool:
        if self.unit is not other.unit:
            raise QuantityUnitMismatch(self.unit, other.unit, ">=")
        return self.value >= other.value

    @property
    def is_zero(self) -> bool:
        return self.value == 0

    @property
    def is_positive(self) -> bool:
        return self.value > 0

    @property
    def is_negative(self) -> bool:
        return self.value < 0

    def to_int(self) -> int:
        """For IBKR API boundary calls that need a Python int (shares,
        contracts). Raises if value isn't representable as int."""
        if self.value != self.value.to_integral_value():
            raise ValueError(
                f"Quantity {self} is fractional; cannot convert to int. "
                f"Likely caller is wrong about asset class (e.g. treating "
                f"FX BASE_UNITS as SHARES)."
            )
        return int(self.value)

    def to_float(self) -> float:
        """For IBKR API boundary calls that take a float. Used for FX
        base units where qty=25_000.50 is valid."""
        return float(self.value)

    def __repr__(self) -> str:
        return f"Quantity({self.value} {self.unit.name})"


# ────────────────────────────────────────────────────────────────────
# Price
# ────────────────────────────────────────────────────────────────────

class Price(Decimal):
    # Decimal subclasses need `__slots__ = ()` to avoid a per-instance
    # __dict__ that would otherwise undo Decimal's compact C layout.
    # Empty tuple = no new slots, just suppress the implicit dict.
    __slots__ = ()

    """A price in the asset's quote currency, as a Decimal.

    Subclass of Decimal so it slots into existing math operators
    naturally; the new type exists primarily as a documentation /
    type-checking hint that "this is a per-unit price, not a notional
    amount." Use `Money` for amounts (qty × price).

    Why subclass rather than wrap: we want `Price * Decimal → Decimal`
    to Just Work for downstream calculations without manual unboxing.
    Type-hint as `Price` at API boundaries; rely on duck-typing inside.

    Construct from string when possible to avoid float→Decimal noise:
        Price("151.71")    # exact
        Price(151.71)      # NOT recommended — float baggage
    """

    def __repr__(self) -> str:
        return f"Price({Decimal.__str__(self)})"


# ────────────────────────────────────────────────────────────────────
# Money
# ────────────────────────────────────────────────────────────────────

class CurrencyMismatch(TypeError):
    """Raised when arithmetic crosses incompatible currency boundaries
    without an explicit FX conversion.
    """

    def __init__(self, left: Currency, right: Currency, op: str = "+") -> None:
        super().__init__(
            f"Cannot {op} Money[{left.name}] and Money[{right.name}] directly; "
            f"call left.to({right.name}, fx_service) first, then operate."
        )
        self.left = left
        self.right = right
        self.op = op


@dataclass(frozen=True, slots=True)
class Money:
    """A monetary amount tagged with its currency.

    Arithmetic with a Money of the same currency is direct. Cross-
    currency arithmetic must go through `.to(target, fx_service)`.
    There's no implicit fallback — callers always see the conversion.

    Why no implicit conversion: the FX rate matters. Mark-to-market
    vs end-of-day vs trade-time rates can each be the right answer
    depending on context. We refuse to guess.
    """

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise TypeError(
                f"Money.amount must be Decimal, got {type(self.amount).__name__}. "
                f"Wrap as Decimal(str(amount))."
            )

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency is not other.currency:
            raise CurrencyMismatch(self.currency, other.currency, "+")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency is not other.currency:
            raise CurrencyMismatch(self.currency, other.currency, "-")
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, scalar: int | Decimal) -> "Money":
        # Scaling a Money by a unitless scalar is well-defined.
        # Money × Money has no meaning here (notional² isn't anything
        # we trade).
        if isinstance(scalar, Money):
            raise CurrencyMismatch(self.currency, scalar.currency, "×")
        if isinstance(scalar, int):
            scalar = Decimal(scalar)
        if not isinstance(scalar, Decimal):
            return NotImplemented
        return Money(self.amount * scalar, self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __abs__(self) -> "Money":
        return Money(abs(self.amount), self.currency)

    def __lt__(self, other: "Money") -> bool:
        if self.currency is not other.currency:
            raise CurrencyMismatch(self.currency, other.currency, "<")
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        if self.currency is not other.currency:
            raise CurrencyMismatch(self.currency, other.currency, "<=")
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        if self.currency is not other.currency:
            raise CurrencyMismatch(self.currency, other.currency, ">")
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        if self.currency is not other.currency:
            raise CurrencyMismatch(self.currency, other.currency, ">=")
        return self.amount >= other.amount

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def to(self, target: Currency, fx: "CurrencyService") -> "Money":
        """Convert to `target` currency using the FX service's current
        rate. If `target == self.currency`, returns self (no service
        call). Raises StaleRate if the cached rate is too old per the
        service's TTL policy.
        """
        if self.currency is target:
            return self
        # Late import to avoid circular dependency at module import time.
        rate = fx.rate(self.currency, target)
        return Money(self.amount * rate, target)

    def to_float(self) -> float:
        """For IBKR API boundary or display. Loses precision; use
        sparingly."""
        return float(self.amount)

    def __repr__(self) -> str:
        # Format with currency code suffix; helps debugging
        # when staring at audit logs.
        return f"Money({self.amount} {self.currency.name})"


# ────────────────────────────────────────────────────────────────────
# Convenience constructors
# ────────────────────────────────────────────────────────────────────

def shares(n: int | str | Decimal) -> Quantity:
    """Construct a SHARES Quantity. Accepts int/str/Decimal; strings
    are preferred for non-integer values to avoid float noise.

        shares(30)        # 30 shares
        shares("30.5")    # NOT typical for equities but allowed
    """
    return Quantity(_to_decimal(n), QuantityUnit.SHARES)


def contracts(n: int | str | Decimal) -> Quantity:
    """Construct a CONTRACTS Quantity (futures). Always integer in
    practice; raises if fractional."""
    q = Quantity(_to_decimal(n), QuantityUnit.CONTRACTS)
    if q.value != q.value.to_integral_value():
        raise ValueError(f"Futures contracts must be integer; got {n}")
    return q


def base_units(n: int | str | Decimal) -> Quantity:
    """Construct a FX BASE_UNITS Quantity (e.g., 25,000.50 EUR for an
    EURUSD trade)."""
    return Quantity(_to_decimal(n), QuantityUnit.BASE_UNITS)


def cfd_units(n: int | str | Decimal) -> Quantity:
    """Construct a CFD_UNITS Quantity."""
    return Quantity(_to_decimal(n), QuantityUnit.CFD_UNITS)


def usd(amount: int | str | Decimal) -> Money:
    """Construct a Money in USD."""
    return Money(_to_decimal(amount), Currency.USD)


def money(amount: int | str | Decimal, currency: Currency) -> Money:
    """Construct a Money in the named currency."""
    return Money(_to_decimal(amount), currency)


def price(p: int | str | float | Decimal) -> Price:
    """Construct a Price. Accepts float for ergonomics at IBKR API
    boundary but converts via str() to avoid binary-float drift."""
    if isinstance(p, float):
        # str(1.16175) → '1.16175' (Python's float-to-str uses shortest
        # representation that round-trips; this is what we want).
        return Price(str(p))
    if isinstance(p, (int, str)):
        return Price(str(p))
    if isinstance(p, Decimal):
        return Price(p)
    raise TypeError(f"Cannot make Price from {type(p).__name__}")


def _to_decimal(n: int | str | float | Decimal) -> Decimal:
    """Coerce common numeric types to Decimal without float drift.

    Float input goes through str() first because Decimal(0.1) ==
    Decimal('0.1000000000000000055511151231257827021181583404541015625')
    which is correct but unhelpful.
    """
    if isinstance(n, Decimal):
        return n
    if isinstance(n, (int, str)):
        return Decimal(str(n))
    if isinstance(n, float):
        return Decimal(str(n))
    raise TypeError(f"Cannot make Decimal from {type(n).__name__}")


__all__ = [
    # Currencies
    "Currency",
    "DEFAULT_BASE_CURRENCY",
    # Quantity
    "QuantityUnit",
    "Quantity",
    "QuantityUnitMismatch",
    # Money
    "Money",
    "CurrencyMismatch",
    # Price
    "Price",
    # Convenience constructors
    "shares",
    "contracts",
    "base_units",
    "cfd_units",
    "usd",
    "money",
    "price",
]
