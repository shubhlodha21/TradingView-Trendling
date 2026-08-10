"""SizingPolicy — quantity semantics + notional math per asset.

Routes for the hardcoded `qty * price` math currently scattered in
risk.py and engine.py. Different asset classes compute notional
totally differently:

  US equity:        notional = qty(shares) * price                                  [USD]
  Futures (ES):     notional = qty(contracts) * price * 50                          [USD]
  Futures (MES):    notional = qty(contracts) * price * 5                           [USD]
  Futures (GC):     notional = qty(contracts) * price * 100                         [USD]
  Forex (EURUSD):   notional = qty(EUR base units) * price                          [USD]
  Forex (USDJPY):   notional = qty(USD base units) * 1                              [USD] (qty IS the quote ccy notional)
                    (display in USD: qty / price gives the JPY quote-side)
  CFD (index):      notional = qty * price * cfd_multiplier (1 for share CFDs)      [quote ccy]

The PLTR -30 bug came partly from "qty + qty" being unit-blind.
SizingPolicy makes the math typed:

    sizing.notional(Quantity(30, SHARES), Price("151.71"))
      → Money(4551.30, USD)

    sizing.notional(Quantity(25000, BASE_UNITS), Price("1.16175"))
      → Money(29043.75, USD)    # USD because EURUSD is USD-quoted

    sizing.notional(Quantity(1, CONTRACTS), Price("4500.25"))
      → Money(225012.50, USD)   # ES = $50 multiplier
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..types import Currency, Money, Price, Quantity, QuantityUnit


class SizingMismatch(TypeError):
    """Raised when SizingPolicy receives a Quantity whose unit is
    wrong for the asset class.

    Example: passing Quantity(30, SHARES) to a Forex sizing policy
    that expects BASE_UNITS. The math would silently compute the
    wrong thing; we'd rather crash here than mis-size an order.
    """

    def __init__(self, expected: QuantityUnit, received: QuantityUnit):
        super().__init__(
            f"SizingPolicy expects Quantity[{expected.name}], "
            f"received Quantity[{received.name}]. Check that the engine's "
            f"AssetSpec matches the symbol it's trading."
        )
        self.expected = expected
        self.received = received


@runtime_checkable
class SizingPolicy(Protocol):
    """Asset-class-specific quantity → notional math."""

    expected_unit: QuantityUnit
    quote_currency: Currency

    def notional(self, qty: Quantity, price: Price) -> Money:
        """Compute the capital outlay (in the asset's QUOTE currency)
        for `qty` of the asset at `price`.

        Must raise SizingMismatch if qty.unit != self.expected_unit.

        Returns Money in self.quote_currency. Callers needing base-
        currency totals must explicitly convert via Money.to(base, fx).
        """
        ...

    def min_qty(self) -> Quantity:
        """Smallest valid order quantity for this asset. Used by the
        risk gate (refuse orders below) and the order-ticket UI
        (clamp inputs).

        Equity:        1 share
        Forex (most):  25,000 base units (IBKR's IDEALPRO minimum)
        Futures:       1 contract
        CFDs:          venue-specific
        """
        ...

    def qty_increment(self) -> Quantity:
        """The grid increment for valid quantities (must be a multiple
        of this from min_qty). Almost always 1 for stocks/futures/
        contracts; can be fractional for FX (some venues let you
        increment by 1 base unit, others by 1,000).
        """
        ...

    def is_valid_qty(self, qty: Quantity) -> bool:
        """Convenience: validate that `qty` is at or above min and is
        an integer multiple of qty_increment. Default impl below
        works for most policies."""
        if qty.unit is not self.expected_unit:
            return False
        if qty < self.min_qty():
            return False
        increment = self.qty_increment()
        if increment.is_zero:
            return True
        return ((qty - self.min_qty()).value % increment.value) == 0
