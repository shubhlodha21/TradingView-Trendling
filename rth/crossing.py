"""Crossing detection: turn "price met the line" into a trade signal.

A cross is a *sign change* in ``price - line_price``, not a comparison against a
level. Checking ``price > line`` alone re-fires on every tick once price is on
the far side; only the transition is tradable.

Two trigger modes:

``last``   the traded/last price of the second must close through the line.
           Conservative, fewer false starts from a one-tick spike.

``touch``  the second's high (for UP) or low (for DOWN) reaching the line is
           enough. Matches how a resting stop order would actually fill.

The detector is streaming -- feed it one tick at a time via :meth:`update` for
live use -- and also batch, via :meth:`scan`, for replaying history. Both share
the same state machine, so a backtest and the live session agree.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .sessions import to_utc
from .trendline import DOWN, UP, Trendline


@dataclass(frozen=True)
class CrossEvent:
    """A trade signal."""

    time: pd.Timestamp            # the bar on which the cross was confirmed
    exact_time: pd.Timestamp      # interpolated instant the line was touched
    price: float                  # market price at confirmation
    line_price: float             # trendline price at confirmation
    direction: str                # UP (long) / DOWN (short)
    trigger: str                  # "last" or "touch"
    rth_seconds: float            # market seconds since the start anchor
    gap: float                    # price - line_price at confirmation

    def as_row(self) -> dict:
        row = asdict(self)
        row["side"] = "BUY" if self.direction == UP else "SELL"
        return row


class CrossDetector:
    """Stateful cross detector for one trendline."""

    def __init__(
        self,
        trendline: Trendline,
        mode: str = "last",
        repeat: bool = False,
        cooldown_seconds: float = 0.0,
    ):
        if mode not in ("last", "touch"):
            raise ValueError("mode must be 'last' or 'touch'")
        self.trendline = trendline
        self.mode = mode
        self.repeat = repeat            # keep signalling on re-crosses?
        self.cooldown = cooldown_seconds
        self.events: list[CrossEvent] = []

        self._prev_diff: float | None = None
        self._prev_offset: float = 0.0
        self._last_signal_offset: float | None = None

    # -- state --------------------------------------------------------------

    @property
    def triggered(self) -> bool:
        return bool(self.events)

    def reset(self) -> None:
        self.events.clear()
        self._prev_diff = None
        self._prev_offset = 0.0
        self._last_signal_offset = None

    # -- streaming ----------------------------------------------------------

    def update(
        self,
        t,
        price: float,
        high: float | None = None,
        low: float | None = None,
    ) -> CrossEvent | None:
        """Feed one observation. Returns an event if this tick crossed."""
        ts = to_utc(t)
        line = self.trendline
        if not line.contains(ts) or not line.clock.is_open(ts):
            return None                     # outside the window, or market shut

        offset = line.clock.elapsed(line.start.time, ts)
        line_price = line.price_at_offset(offset)
        diff = float(price) - line_price

        prev_diff, prev_offset = self._prev_diff, self._prev_offset
        self._prev_diff, self._prev_offset = diff, offset
        if prev_diff is None:
            return None                     # first tick only arms the detector

        if self.triggered and not self.repeat:
            return None
        if (
            self._last_signal_offset is not None
            and offset - self._last_signal_offset < self.cooldown
        ):
            return None

        probe = diff
        if self.mode == "touch":
            if self.trendline.direction == UP and high is not None:
                probe = float(high) - line_price
            elif self.trendline.direction == DOWN and low is not None:
                probe = float(low) - line_price

        if self.trendline.direction == UP:
            crossed = prev_diff <= 0 < probe
        else:
            crossed = prev_diff >= 0 > probe
        if not crossed:
            return None

        event = self._build(ts, offset, price, line_price, diff, prev_diff, prev_offset)
        self.events.append(event)
        self._last_signal_offset = offset
        return event

    def _build(self, ts, offset, price, line_price, diff, prev_diff, prev_offset):
        # Where between the two observations did price actually meet the line?
        # Interpolating in RTH-second space keeps the answer honest across a
        # session boundary.
        denom = diff - prev_diff
        if denom != 0:
            frac = float(np.clip(-prev_diff / denom, 0.0, 1.0))
            cross_offset = prev_offset + frac * (offset - prev_offset)
        else:
            cross_offset = offset
        return CrossEvent(
            time=ts,
            exact_time=self.trendline.time_at_offset(cross_offset),
            price=float(price),
            line_price=float(line_price),
            direction=self.trendline.direction,
            trigger=self.mode,
            rth_seconds=float(offset),
            gap=float(diff),
        )

    # -- batch --------------------------------------------------------------

    def scan(self, bars: pd.DataFrame) -> list[CrossEvent]:
        """Replay a bar frame through the detector.

        ``bars`` needs a UTC ``timestamp`` column (or DatetimeIndex) and a
        ``price`` column; ``high``/``low`` are used in touch mode when present.
        Rows outside the trendline window or outside market hours are dropped
        first, so closed-market prints can never generate a signal.
        """
        if bars is None or bars.empty:
            return []

        frame = bars.copy()
        if "timestamp" not in frame.columns:
            frame = frame.reset_index().rename(columns={frame.index.name or "index": "timestamp"})
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame.sort_values("timestamp")

        line = self.trendline
        frame = frame[
            (frame["timestamp"] >= line.start.time) & (frame["timestamp"] <= line.end.time)
        ]
        if frame.empty:
            return []

        stamps = pd.DatetimeIndex(frame["timestamp"])

        # Drop closed-market prints outright. Checking the offset alone is not
        # enough: a Saturday print carries the same offset as Friday's close,
        # which is strictly greater than the previous bar's, so it would survive
        # a monotonicity filter and signal a cross the market never saw.
        frame = frame[line.clock.is_open_array(stamps)]
        if frame.empty:
            return []

        stamps = pd.DatetimeIndex(frame["timestamp"])
        offsets = line.clock.elapsed_from(line.start.time, stamps)

        capped = np.clip(offsets, 0.0, line.span_seconds)
        line_prices = line.start.price + line.slope * capped
        prices = frame["price"].to_numpy(dtype=float)
        highs = frame["high"].to_numpy(dtype=float) if "high" in frame else prices
        lows = frame["low"].to_numpy(dtype=float) if "low" in frame else prices

        found: list[CrossEvent] = []
        for i in range(len(frame)):
            diff = prices[i] - line_prices[i]
            prev_diff, prev_offset = self._prev_diff, self._prev_offset
            self._prev_diff, self._prev_offset = diff, offsets[i]
            if prev_diff is None:
                continue
            if self.triggered and not self.repeat:
                break
            if (
                self._last_signal_offset is not None
                and offsets[i] - self._last_signal_offset < self.cooldown
            ):
                continue

            probe = diff
            if self.mode == "touch":
                probe = (
                    highs[i] - line_prices[i]
                    if line.direction == UP
                    else lows[i] - line_prices[i]
                )
            crossed = (
                prev_diff <= 0 < probe if line.direction == UP else prev_diff >= 0 > probe
            )
            if not crossed:
                continue

            event = self._build(
                pd.Timestamp(frame["timestamp"].iloc[i]),
                float(offsets[i]), prices[i], line_prices[i], diff, prev_diff, prev_offset,
            )
            found.append(event)
            self.events.append(event)
            self._last_signal_offset = offsets[i]

        return found


def events_to_frame(events: list[CrossEvent]) -> pd.DataFrame:
    """Trade log, ready for display or CSV export."""
    if not events:
        return pd.DataFrame(
            columns=["exact_time", "time", "side", "direction", "price",
                     "line_price", "gap", "rth_seconds", "trigger"]
        )
    frame = pd.DataFrame([e.as_row() for e in events])
    return frame[["exact_time", "time", "side", "direction", "price",
                  "line_price", "gap", "rth_seconds", "trigger"]]
