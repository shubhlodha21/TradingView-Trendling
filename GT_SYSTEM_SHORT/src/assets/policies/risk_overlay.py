"""RiskOverlay — asset-class-specific risk gates layered on top of
the universal RiskGate.

The current `src/strategy/risk.py` ships universal checks:
  - portfolio combined exposure cap
  - daily-loss circuit breaker
  - consecutive-loss circuit breaker
  - max-trades-per-day

These work for equity. They're necessary but NOT SUFFICIENT for
other asset classes:

  Futures:   Need SPAN margin headroom check. A 1-contract ES position
             is $225K notional but only ~$13K SPAN margin requirement.
             Exposure-cap math based on notional will silently block
             the trade even when margin is fine.

  CFDs:      Overnight financing cost can dwarf intraday P&L. Need a
             check on "if I hold this overnight, what's the daily cost
             relative to my expected edge?"

  FX:        Need a swap-rate awareness for held-overnight positions.
             Plus FX-specific: weekend gap risk if held into Friday close.

  Options:   Need delta-exposure (sum of |delta| × notional) cap. A
             portfolio of "small" options positions can carry massive
             delta exposure invisible to a notional-only check.

The pattern: each AssetSpec ships its own RiskOverlay. The engine's
RiskGate runs universal checks first, then `spec.risk_overlay.check()`
for asset-specific. Both must pass for the order to proceed.

This keeps the universal gate simple and stable while letting each
asset class evolve its own risk model independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from ..types import Money, Price, Quantity


# ────────────────────────────────────────────────────────────────────
# Order intent — what we're about to do
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Structured description of an order BEFORE submission. The risk
    overlay sees this and decides allow/block.

    `spec` is the originating AssetSpec — the overlay can ask it any
    questions it needs (multiplier, expiry, etc.) without us pre-
    flattening attributes here.
    """

    symbol: str
    side: Literal["BUY", "SELL"]
    qty: Quantity
    intended_price: Price
    order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    spec: Any  # forward ref to AssetSpec — avoid circular type import

    @property
    def notional(self) -> Money:
        """Convenience: compute notional via the spec's sizing policy."""
        return self.spec.sizing.notional(self.qty, self.intended_price)


# ────────────────────────────────────────────────────────────────────
# Portfolio view — what's already on the books
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Snapshot of the portfolio at the moment of the risk check.

    Held minimal on purpose — risk overlays should request what they
    need explicitly so we don't pre-compute expensive things every
    check. Add fields as new overlays need them.
    """

    # Filled + pending notional across all peers, in account base ccy.
    # Sourced from the existing PortfolioReader.
    total_open_notional_base: Money

    # Daily realized P&L across all peers, in account base ccy.
    daily_pnl_base: Money

    # Account equity (latest IBKR snapshot), in account base ccy.
    account_equity_base: Money

    # Account buying power (latest IBKR snapshot), in account base ccy.
    account_buying_power_base: Money

    # Per-currency exposure — useful for FX policies that care about
    # net long/short per currency for swap-rate calc.
    by_currency: dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────
# Risk verdict — the overlay's decision
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """Result of a risk check. Structured so callers can audit
    consistently and the dashboard can surface the reason.

    `allow=True` = order may proceed. `allow=False` = block, surface
    `reason` to operator. `circuit_break=True` = additionally pause
    new entries on this ticker until manual reset.

    `details` is a free-form dict the overlay can populate with
    metrics that went into the decision — useful for post-mortem.
    """

    allow: bool
    reason: str
    circuit_break: bool = False
    details: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, reason: str = "all gates pass", **details) -> "RiskVerdict":
        return cls(allow=True, reason=reason, details=dict(details))

    @classmethod
    def block(cls, reason: str, *, circuit_break: bool = False, **details) -> "RiskVerdict":
        return cls(
            allow=False, reason=reason,
            circuit_break=circuit_break, details=dict(details),
        )


# ────────────────────────────────────────────────────────────────────
# The protocol
# ────────────────────────────────────────────────────────────────────

@runtime_checkable
class RiskOverlay(Protocol):
    """Asset-class-specific risk overlay. Called by RiskGate AFTER
    universal checks have passed.

    Stateless by convention — pass state in via PortfolioView, return
    decision via RiskVerdict. Overlays MUST NOT mutate any global
    state, write files, or place orders. They are pure decision
    functions.
    """

    def check(self, intent: OrderIntent, portfolio: PortfolioView) -> RiskVerdict:
        """Decide whether `intent` should proceed.

        Return RiskVerdict.ok(...) to allow, RiskVerdict.block(...)
        to refuse. The reason string is logged to audit and shown to
        the operator on rejection.
        """
        ...


# ────────────────────────────────────────────────────────────────────
# Trivial impl that allows everything — useful as a default for
# asset classes that don't need extra overlay (e.g. US equity, FX
# cash — the universal gate is enough for those)
# ────────────────────────────────────────────────────────────────────

class NoExtraRisk:
    """Default overlay that always passes. Used for asset classes
    where the universal RiskGate suffices.

    Implements RiskOverlay structurally (no inheritance needed —
    that's the point of Protocol).
    """

    __slots__ = ()

    def check(self, intent: OrderIntent, portfolio: PortfolioView) -> RiskVerdict:
        return RiskVerdict.ok(reason="no asset-specific overlay")
