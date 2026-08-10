"""TickPolicy — minimum price increment + rounding to a valid tick.

The 22 `round(price, 2)` sites across engine.py/broker.py/risk.py
all route here. Two-decimal rounding is a US-equity assumption that
silently breaks every other asset class:

  Equity (NYSE):   minTick = 0.01     → round(p, 2)
  EURUSD (IDEALPRO): minTick = 0.00005 → round to 5dp BUT must also be
                                          a multiple of 5 in the 5th dp
  USDJPY (IDEALPRO): minTick = 0.005  → 3dp with 0.005 grain
  ES futures (CME): minTick = 0.25    → 2dp BUT must be multiple of 0.25
  GC futures (CME): minTick = 0.10    → 2dp BUT 0.10 grain
  CFDs:            varies by underlying

Note: "decimals" is NOT the right abstraction. ES at 4500.30 is
valid float math but INVALID order price (CME rejects). The right
question is: "is `price` an integer multiple of tick_size?" The
TickPolicy enforces that by snapping to the grid.

RoundDirection matters at order time:
  * NEAREST — for display, mid prices
  * DOWN    — for BUY limits (don't bid through the offer accidentally)
  * UP      — for SELL limits (don't ask below the bid accidentally)
  * AWAY_FROM_REF — for protective stops (give a little more breathing
                    room than the calculated stop)
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_EVEN
from enum import Enum
from typing import Protocol, runtime_checkable

from ..types import Price


class RoundDirection(Enum):
    """Direction to round when a calculated price doesn't sit exactly
    on a valid tick.

    NEAREST       — round to closest tick (banker's rounding on ties)
    DOWN          — round toward zero (always floor on positive prices)
    UP            — round away from zero (always ceiling on positive prices)
    """

    NEAREST = "NEAREST"
    DOWN = "DOWN"
    UP = "UP"


@runtime_checkable
class TickPolicy(Protocol):
    """Per-asset tick-size rules and rounding."""

    def tick_size(self, price: Price) -> Decimal:
        """The minimum valid price increment AT THIS PRICE LEVEL.

        Most assets have a constant tick_size regardless of price
        (e.g. equity = 0.01 everywhere). But some have tiered ticks
        (LSE pence-level grains, certain options chains). Taking
        `price` as input lets those cases work without a separate
        method.

        Returns Decimal so it composes cleanly with our Decimal
        prices.
        """
        ...

    def round_to_tick(self, price: Price, direction: RoundDirection = RoundDirection.NEAREST) -> Price:
        """Snap `price` to the nearest valid tick grid line.

        Required invariant: the returned Price MUST be an integer
        multiple of self.tick_size(price). The engine must NEVER
        place an order with a price off-grid; brokers reject and
        the rejection lands at fill time, not order time, which is
        confusing.

        For NEAREST: ties break via banker's rounding (ROUND_HALF_EVEN)
        which is what numpy/pandas use and what most quant code
        expects.

        For DOWN/UP: respect sign. DOWN on positive price → floor.
        """
        ...

    def decimals_for_display(self, price: Price) -> int:
        """How many decimal places to show in the UI for this asset
        at this price level. Equity = 2; FX = 5 (or 3 for JPY pairs);
        futures depend on root.

        Pure presentation helper — doesn't affect order math. Used by
        dashboard / audit logs to format prices consistently per asset.
        """
        ...


# ────────────────────────────────────────────────────────────────────
# Shared helper — the rounding math, generic over tick_size
# ────────────────────────────────────────────────────────────────────

def round_to_grid(price: Price, tick_size: Decimal, direction: RoundDirection) -> Price:
    """Round `price` to the nearest multiple of `tick_size`.

    This is the math every TickPolicy implementation calls in its
    round_to_tick(). Hoisting it here so policies can be very small.

        round_to_grid(Price("151.713"), Decimal("0.01"), NEAREST) → Price("151.71")
        round_to_grid(Price("4500.30"), Decimal("0.25"), NEAREST) → Price("4500.25")
        round_to_grid(Price("4500.30"), Decimal("0.25"), UP)      → Price("4500.50")
        round_to_grid(Price("1.161725"), Decimal("0.00005"), NEAREST) → Price("1.16170")
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")
    # Number of ticks at this price
    n_ticks = price / tick_size
    if direction is RoundDirection.NEAREST:
        rounded = n_ticks.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    elif direction is RoundDirection.DOWN:
        rounded = n_ticks.quantize(Decimal("1"), rounding=ROUND_DOWN)
    elif direction is RoundDirection.UP:
        rounded = n_ticks.quantize(Decimal("1"), rounding=ROUND_UP)
    else:
        raise ValueError(f"Unknown RoundDirection: {direction}")
    return Price(rounded * tick_size)
