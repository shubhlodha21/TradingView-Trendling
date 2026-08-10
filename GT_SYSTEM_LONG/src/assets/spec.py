"""AssetSpec — the composed bundle of policies for one asset class.

This is THE central object. Every engine instance carries exactly
one AssetSpec (resolved once at __init__ from its symbol). Every
place in the engine that previously hardcoded a US-equity assumption
delegates here via `self._spec.<policy>.<method>(...)`.

Why a frozen dataclass:
  * Frozen = immutable. The spec doesn't change during the bot's
    lifetime (no asset-class hot-swapping). Immutability gives us
    free hashability, easy diffing across restarts, and the ability
    to log the entire spec into audit at order time.
  * Dataclass = readable definition. The 8 policy fields make the
    composition explicit at construction.

What does NOT live here:
  * Symbol — that's per-bot config, not asset-class metadata.
    `engine.config.ticker` stays in Config.
  * Cycle-specific state (entry_price, quantity, etc.) — stays on
    the engine.
  * Network connections, IB client handles — those live on the
    Gateway. AssetSpec is data; Gateway is connection.

Examples:
    spec = make_us_equity_spec("PLTR")    # composed from us_stock.py
    spec.tick.round_to_tick(Price("151.713"))         # → Price("151.71")
    spec.sizing.notional(shares(30), Price("151.71")) # → Money(4551.30, USD)
    spec.session.is_open_at(datetime.now(tz=utc))     # → True/False
    spec.risk_overlay.check(intent, portfolio)        # → RiskVerdict
"""

from __future__ import annotations

from dataclasses import dataclass

from .enum import AssetClass
from .policies import (
    ContractPolicy, PricePolicy, TickPolicy, SizingPolicy,
    CommissionPolicy, SessionPolicy, LifecyclePolicy, RiskOverlay,
)
from .types import Currency


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """Composed bundle of asset-class-dependent policies + metadata.

    Every field is required (no defaults) so every concrete factory
    has to make a deliberate choice about every dimension. If a
    factory needs "default behavior" for a policy, it should
    explicitly pick the "noop" implementation rather than letting
    omission be the default — silent defaults are how the equity
    assumption leaked into 12,000 lines in the first place.
    """

    # ── Identity ────────────────────────────────────────────────────
    asset_class:    AssetClass
    quote_currency: Currency
    venue:          str  # "SMART" / "IDEALPRO" / "GLOBEX" / etc.

    # ── Policies (composition over inheritance) ─────────────────────
    contract:       ContractPolicy
    price:          PricePolicy
    tick:           TickPolicy
    sizing:         SizingPolicy
    commission:     CommissionPolicy
    session:        SessionPolicy
    lifecycle:      LifecyclePolicy
    risk_overlay:   RiskOverlay

    def __post_init__(self):
        # Cross-policy consistency check at construction time. If the
        # sizing policy says it returns Money in EUR but our spec
        # quote_currency is USD, somebody composed wrong.
        if self.sizing.quote_currency is not self.quote_currency:
            raise ValueError(
                f"AssetSpec consistency: sizing.quote_currency = "
                f"{self.sizing.quote_currency.name} but spec.quote_currency = "
                f"{self.quote_currency.name}. Pick one and match the other."
            )

    def describe(self) -> str:
        """Human-readable spec summary for audit / logs."""
        return (
            f"AssetSpec[{self.asset_class.name}] "
            f"venue={self.venue} ccy={self.quote_currency.name} "
            f"contract={type(self.contract).__name__} "
            f"price={type(self.price).__name__} "
            f"tick={type(self.tick).__name__} "
            f"sizing={type(self.sizing).__name__} "
            f"commission={type(self.commission).__name__} "
            f"session={type(self.session).__name__} "
            f"lifecycle={type(self.lifecycle).__name__} "
            f"risk_overlay={type(self.risk_overlay).__name__}"
        )

    def to_audit_dict(self) -> dict:
        """Serializable snapshot for the audit log. Captured at order
        placement time so post-mortem can answer "what asset config
        was active when this order fired?"
        """
        return {
            "asset_class": self.asset_class.value,
            "quote_currency": self.quote_currency.value,
            "venue": self.venue,
            "policies": {
                "contract": type(self.contract).__name__,
                "price": type(self.price).__name__,
                "tick": type(self.tick).__name__,
                "sizing": type(self.sizing).__name__,
                "commission": type(self.commission).__name__,
                "session": type(self.session).__name__,
                "lifecycle": type(self.lifecycle).__name__,
                "risk_overlay": type(self.risk_overlay).__name__,
            },
        }
