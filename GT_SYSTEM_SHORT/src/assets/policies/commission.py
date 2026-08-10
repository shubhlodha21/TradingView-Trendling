"""CommissionPolicy — IBKR fee estimate per fill, asset-class-aware.

IBKR's commission model is different for every asset class:

  US equity:   Tiered (per-share with min/max) OR Fixed (flat per trade).
               Default IBKR retail = Tiered: $0.0035/share, min $0.35,
               max 1% of notional.
  Forex:       Per-notional: 0.20 bps of trade value, min $2.
  Futures:     Per-contract flat: ~$0.85/contract (ES), $0.25/contract
               (micros like MES), plus exchange + clearing fees.
  CFDs:        Varies by venue — some have explicit commission, some
               build it into the spread + overnight financing.
  Options:     Per-contract: $0.65/contract, min $1.

We estimate at order-placement time so the risk gate and the order
ticket UI can show the expected fee BEFORE the fill. The estimate is
not authoritative — the actual fee comes back on the fill — but
should be within ~5% so it's useful for sizing decisions.

This policy does NOT track the cumulative commission paid per cycle
(the engine's `_total_commission` field handles that). It only
estimates the fee for a single fill.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from ..types import Money, Price, Quantity

Side = Literal["BUY", "SELL"]


@runtime_checkable
class CommissionPolicy(Protocol):
    """Estimate the IBKR commission for a single fill of this asset."""

    def estimate(
        self,
        qty: Quantity,
        price: Price,
        side: Side,
        venue: str = "SMART",
    ) -> Money:
        """Estimated commission in the asset's QUOTE currency.

        Note: venue matters because the same instrument can route to
        different venues (PEARL vs ARCA for US equity; IDEALPRO vs
        ARCAFX for FX) and the fee schedule differs slightly. For
        Day-1 we accept a string and let each policy decide whether
        to branch on it.

        Returns Money. May be Money.zero in unusual cases (some
        zero-commission promotions, internal crosses, etc.) but
        callers should not rely on this being non-zero either way.
        """
        ...
