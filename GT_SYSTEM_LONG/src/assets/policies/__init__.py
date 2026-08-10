"""Policy protocols for AssetSpec composition.

Each protocol owns ONE asset-class-dependent behavior. AssetSpec is a
bundle of policies; concrete asset classes (us_stock, forex, future,
index_cfd, share_cfd, fx_cfd) pick an implementation of each policy.

Why protocols (PEP 544) instead of abstract base classes:
- Structural typing — any object with the right method signatures
  satisfies the protocol, no `class X(SomeABC)` inheritance required.
- Lets us write tiny in-line policies for tests (`class NoOpRisk: def
  check(self, intent, portfolio): return RiskVerdict(True, "ok")`)
  without ceremony.
- Enables third-party / user-supplied policies in the future without
  forcing them into our class hierarchy.

Order of presentation (also the order an AssetSpec composes them):

  1. ContractPolicy     — "what kind of IBKR contract am I?"
  2. PricePolicy        — "where do I read prices from? bid? ask? last? mid?"
  3. TickPolicy         — "what's the smallest valid price increment?"
  4. SizingPolicy       — "how do I convert (qty, price) → notional in quote ccy?"
  5. CommissionPolicy   — "what fee does IBKR charge for this fill?"
  6. SessionPolicy      — "am I tradable right now? when's the next open/close?"
  7. LifecyclePolicy    — "do I roll? expire? settle T+N? handle corporate actions?"
  8. RiskOverlay        — "are there asset-class-specific risk gates beyond universal?"
"""

from .contract import ContractPolicy
from .price import PricePolicy, FeedSnapshot
from .tick import TickPolicy, RoundDirection
from .sizing import SizingPolicy
from .commission import CommissionPolicy
from .session import SessionPolicy, SessionWindow
from .lifecycle import LifecyclePolicy
from .risk_overlay import RiskOverlay, RiskVerdict, OrderIntent, PortfolioView

__all__ = [
    "ContractPolicy",
    "PricePolicy", "FeedSnapshot",
    "TickPolicy", "RoundDirection",
    "SizingPolicy",
    "CommissionPolicy",
    "SessionPolicy", "SessionWindow",
    "LifecyclePolicy",
    "RiskOverlay", "RiskVerdict", "OrderIntent", "PortfolioView",
]
