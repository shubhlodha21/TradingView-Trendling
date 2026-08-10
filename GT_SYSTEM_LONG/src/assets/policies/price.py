"""PricePolicy — which feed field to read when the engine needs a price.

The 17 `feed.last` reads scattered across engine.py all route here.
Different asset classes have different "current price" semantics:

  US equity (NYSE):  feed.last is the most recent trade. Tight, real.
  Forex (IDEALPRO):  feed.last is STALE or nan — no trade prints.
                     Use mid = (bid + ask) / 2 instead.
  Futures (GLOBEX):  feed.last is the most recent trade, usually fine.
  CFDs (IBKR):       feed.last is a synthetic mark from the underlying;
                     PrefMark policy uses contract markPrice when present.

Why three methods (reference / buy_compare / sell_compare):

  The engine has three semantically different uses of "current price":

  1. Reference price — for tracking (highest_price, trailing stops).
     Use the symmetric mid when bid/ask are present; falls back to
     last for equity feeds where mid is noisy.

  2. Buy-compare price — for trigger crosses on the BUY side. Should
     be the price you'd ACTUALLY PAY: the ask. Comparing trigger to
     the bid would arm too early.

  3. Sell-compare price — for stop crosses on the SELL side. Should
     be the price you'd ACTUALLY RECEIVE: the bid. Comparing stop to
     the ask would fire too early.

  For equity feeds with reliable `last`, all three return `last`.
  For FX feeds without `last`, they return mid / ask / bid respectively.
  This isolates the asymmetry to one file per asset class.

`is_actionable(feed)`: a gate that says "the feed is fresh and the
spread is reasonable — okay to make trading decisions on this tick."
Asset classes implement it differently (FX cares about pip spread,
equity cares about NBBO age).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from ..types import Price


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    """The minimal subset of feed data PricePolicy reads.

    A concrete asset's feed (in handler.py / production.py) emits
    these per tick. We keep this struct narrow so the policy doesn't
    couple to the full Ticker shape; each backend can adapt its
    native object to FeedSnapshot at the boundary.

    All Optional[] because not every feed populates every field on
    every tick — Forex has no `last`, options have no `volume`, etc.
    Policies must handle None gracefully (typically by falling back
    to mid or raising IfNoUsableValue).
    """

    bid: Optional[Price]
    ask: Optional[Price]
    last: Optional[Price]
    bid_size: Optional[int]
    ask_size: Optional[int]
    last_size: Optional[int]
    volume: Optional[int]
    high: Optional[Price]
    low: Optional[Price]
    vwap: Optional[Price]
    # The feed's timestamp for this snapshot (UTC). Used by
    # is_actionable() staleness checks.
    ts: Optional[datetime] = None


class NoUsablePrice(ValueError):
    """Raised by PricePolicy.* when the feed snapshot has no value
    the policy can use (e.g. all bid/ask/last are None or nan).

    Engine callers must catch and skip the tick rather than crash;
    a fresh tick is usually right behind.
    """


@runtime_checkable
class PricePolicy(Protocol):
    """How to extract trading-relevant prices from a feed snapshot."""

    def reference(self, feed: FeedSnapshot) -> Price:
        """The price the engine treats as the 'current value' of the
        instrument. Used for:
            * highest_price tracking
            * trailing stop adjustments
            * dashboard display
            * P&L mark-to-market

        Should be the most stable / unbiased estimate. For quote-
        driven assets (FX): mid. For trade-driven assets (equity):
        last with mid fallback.

        Raises NoUsablePrice if no usable value present.
        """
        ...

    def buy_compare(self, feed: FeedSnapshot) -> Price:
        """The price to compare against a BUY trigger.

        Conservative: this should be the price the engine would
        ACTUALLY PAY if the BUY fires right now. For FX, that's the
        ask. For equity (where bid/ask spread is usually tight) we
        can return last as an approximation.

        Engine compares this >= trigger to fire the entry.

        Raises NoUsablePrice if no usable value.
        """
        ...

    def sell_compare(self, feed: FeedSnapshot) -> Price:
        """The price to compare against a SELL stop.

        Symmetric to buy_compare: the price the engine would
        ACTUALLY RECEIVE if the SELL fires now. For FX, the bid.
        For equity, last.

        Engine compares this <= stop to fire the exit.

        Raises NoUsablePrice if no usable value.
        """
        ...

    def is_actionable(self, feed: FeedSnapshot) -> bool:
        """Is this snapshot fresh enough and well-formed enough to
        base a trading decision on?

        Returns False on:
            * stale feed (timestamp too old per asset's tolerance)
            * crossed/locked market (bid >= ask) — broken state
            * absurd spread (asset-specific threshold)
            * missing required fields per asset

        Engine treats False as "skip this tick" without alerting;
        repeated False over a window of ticks should trigger
        STALE_FEED alert at the engine level.
        """
        ...
