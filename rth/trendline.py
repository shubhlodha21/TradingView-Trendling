"""The trendline itself: a straight line drawn through *market* time.

Given two anchors -- (t_past, price_past) and (t_future, price_future) -- the
line is defined in RTH-second space:

    span   = clock.elapsed(t_past, t_future)          # market seconds only
    slope  = (price_future - price_past) / span       # price per market second
    line(t) = price_past + slope * clock.elapsed(t_past, t)

Because ``elapsed`` freezes over weekends, holidays and closed hours, the line
holds its price across a gap and resumes exactly where it left off at the next
open. That is what a trader drawing on a session-compressed chart actually sees,
and it is what a naive wall-clock interpolation gets wrong.

The line is defined only on ``[t_past, t_future]``; past the end anchor it is
finished and stops producing signals.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .clock import RthClock
from .sessions import to_utc

UP = "UP"
DOWN = "DOWN"


@dataclass(frozen=True)
class Anchor:
    """One end of the trendline."""

    time: pd.Timestamp
    price: float

    def __post_init__(self):
        object.__setattr__(self, "time", to_utc(self.time))
        object.__setattr__(self, "price", float(self.price))


class Trendline:
    """A trendline parameterised by elapsed market seconds."""

    def __init__(
        self,
        clock: RthClock,
        start: Anchor,
        end: Anchor,
        direction: str = UP,
    ):
        if end.time <= start.time:
            raise ValueError("The future anchor must be later than the past anchor.")

        self.clock = clock
        self.direction = direction.upper().strip()
        if self.direction not in (UP, DOWN):
            raise ValueError(f"direction must be {UP!r} or {DOWN!r}, got {direction!r}")

        # Anchors that land on a weekend/holiday/closed hour are moved to the
        # next open. The intervening dead time measures zero market seconds, so
        # this changes no arithmetic -- it just makes the displayed anchor a real
        # tradable instant instead of one the market never saw.
        self.raw_start, self.raw_end = start, end
        self.start = Anchor(clock.snap_forward(start.time), start.price)
        self.end = Anchor(clock.snap_forward(end.time), end.price)

        self.span_seconds = clock.elapsed(self.start.time, self.end.time)
        if self.span_seconds <= 0:
            raise ValueError(
                "The two anchors enclose zero open-market time -- the whole range "
                "falls on weekends, holidays or closed hours for this instrument. "
                "Check the asset class and session mode."
            )

        self.slope = (self.end.price - self.start.price) / self.span_seconds

    # -- introspection ------------------------------------------------------

    @property
    def start_was_snapped(self) -> bool:
        return self.start.time != self.raw_start.time

    @property
    def end_was_snapped(self) -> bool:
        return self.end.time != self.raw_end.time

    @property
    def slope_per_hour(self) -> float:
        return self.slope * 3600.0

    @property
    def slope_per_session_day(self) -> float:
        """Price move per 6.5h session -- a more legible number than per-second."""
        return self.slope * 6.5 * 3600.0

    @property
    def rises(self) -> bool:
        return self.slope > 0

    @property
    def direction_matches_slope(self) -> bool:
        """Whether the requested trade direction agrees with the line's tilt.

        Not an error either way -- a DOWN signal on a rising line is a valid
        breakdown trade -- but worth surfacing in the UI as a typo check.
        """
        return (self.direction == UP) == self.rises

    # -- evaluation ---------------------------------------------------------

    def contains(self, t) -> bool:
        ts = to_utc(t)
        return self.start.time <= ts <= self.end.time

    def price_at(self, t) -> float:
        """Line price at ``t``. Clamped to the anchors outside the window, so
        the line is flat before it starts and after it ends rather than
        extrapolating off to infinity."""
        ts = to_utc(t)
        if ts <= self.start.time:
            return self.start.price
        if ts >= self.end.time:
            return self.end.price
        return self.start.price + self.slope * self.clock.elapsed(self.start.time, ts)

    def price_at_offset(self, rth_seconds: float) -> float:
        """Line price N market seconds after the start anchor."""
        capped = min(max(rth_seconds, 0.0), self.span_seconds)
        return self.start.price + self.slope * capped

    def time_at_offset(self, rth_seconds: float) -> pd.Timestamp:
        return self.clock.advance(self.start.time, rth_seconds)

    def series(
        self,
        step_seconds: float = 1.0,
        max_points: int = 2_000_000,
    ) -> pd.DataFrame:
        """The full point set for the line: one row per open-market timestamp.

        Columns:
            ``timestamp``    UTC wall-clock instant (weekends/holidays absent)
            ``rth_seconds``  market seconds elapsed since the start anchor
            ``line_price``   trendline price at that instant
        """
        stamps, offsets = self.clock.grid(
            self.start.time, self.end.time, step_seconds, max_points=max_points
        )
        prices = self.start.price + self.slope * offsets
        frame = pd.DataFrame(
            {"timestamp": stamps, "rth_seconds": offsets, "line_price": prices}
        )
        # grid() is half-open, so append the closing anchor to make the line
        # visibly terminate on its end point.
        tail = pd.DataFrame([{
            "timestamp": self.end.time,
            "rth_seconds": self.span_seconds,
            "line_price": self.end.price,
        }])
        return pd.concat([frame, tail], ignore_index=True)

    def summary(self) -> dict:
        stats = self.clock.stats(self.start.time, self.end.time)
        return {
            "start_utc": self.start.time,
            "end_utc": self.end.time,
            "start_price": self.start.price,
            "end_price": self.end.price,
            "direction": self.direction,
            "rth_seconds": self.span_seconds,
            "rth_hours": self.span_seconds / 3600.0,
            "wall_seconds": stats.wall_seconds,
            "closed_seconds": stats.closed_seconds,
            "closed_pct": stats.closed_fraction * 100.0,
            "sessions": stats.session_count,
            "slope_per_second": self.slope,
            "slope_per_hour": self.slope_per_hour,
        }
