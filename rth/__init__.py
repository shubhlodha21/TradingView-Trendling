"""RTH trendline engine.

Measures elapsed time in *market* seconds (regular trading hours only, weekends
and holidays excluded), projects a trendline through that compressed time axis,
and fires trade signals when live price crosses the line.
"""

from .sessions import (
    ASSET_CLASSES,
    MarketDef,
    SessionProvider,
    get_market,
    list_markets,
    guess_market,
)
from .clock import RthClock
from .trendline import Anchor, Trendline
from .crossing import CrossDetector, CrossEvent
from .feeds import PriceFeed, SimulatedFeed, YFinanceFeed, get_feed
# Safe to import unconditionally: the IB client library is only imported when a
# feed is actually started.
from .ibkr import IBKRFeed

__all__ = [
    "ASSET_CLASSES",
    "MarketDef",
    "SessionProvider",
    "get_market",
    "list_markets",
    "guess_market",
    "RthClock",
    "Anchor",
    "Trendline",
    "CrossDetector",
    "CrossEvent",
    "PriceFeed",
    "SimulatedFeed",
    "YFinanceFeed",
    "IBKRFeed",
    "get_feed",
]
