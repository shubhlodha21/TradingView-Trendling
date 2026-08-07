"""The RTH clock: time arithmetic where only open-market seconds count.

Wall-clock time and market time are different quantities. Between Friday 15:00
and Monday 11:00 there are 236,400 wall seconds but only 7,800 RTH seconds for a
US equity. Every calculation downstream -- trendline slope, projected price,
crossing checks -- has to use the second number, or the line drifts by the
entire weekend.

``RthClock`` wraps a :class:`~rth.sessions.SessionProvider` and exposes that
second number:

* :meth:`elapsed`   -- signed market seconds between two instants
* :meth:`advance`   -- inverse of ``elapsed``: t0 plus N market seconds
* :meth:`grid`      -- every valid timestamp in a range, at a chosen step
* :meth:`is_open`   -- is the market open at this instant

Sessions are cached and flattened into sorted int64 arrays with a cumulative
duration index, so ``elapsed`` is an O(log n) searchsorted rather than a scan.
That matters: it runs on every incoming tick.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .sessions import Interval, SessionProvider, to_utc

NS = 1_000_000_000  # nanoseconds per second


def _as_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Integer nanoseconds for a DatetimeIndex.

    pandas 3 builds DatetimeIndex at *microsecond* resolution by default, so
    ``.asi8`` yields microseconds -- while ``Timestamp.value`` is always
    nanoseconds. Mixing the two silently scales results by 1000, so every
    conversion goes through here.
    """
    return index.as_unit("ns").asi8


@dataclass
class RangeStats:
    """Summary of a [start, end] window, for display."""

    rth_seconds: float
    wall_seconds: float
    session_count: int
    first_open: pd.Timestamp | None
    last_close: pd.Timestamp | None

    @property
    def rth_hours(self) -> float:
        return self.rth_seconds / 3600.0

    @property
    def closed_seconds(self) -> float:
        return max(self.wall_seconds - self.rth_seconds, 0.0)

    @property
    def closed_fraction(self) -> float:
        return self.closed_seconds / self.wall_seconds if self.wall_seconds > 0 else 0.0


class RthClock:
    """Market-time arithmetic over one instrument's session calendar."""

    def __init__(self, provider: SessionProvider, pad_days: int = 45):
        self.provider = provider
        self._pad = pd.Timedelta(days=pad_days)
        self._lo: pd.Timestamp | None = None
        self._hi: pd.Timestamp | None = None
        self._starts = np.empty(0, dtype=np.int64)
        self._ends = np.empty(0, dtype=np.int64)
        self._cum = np.zeros(1, dtype=np.int64)   # cum[i] = ns of market time before session i

    # -- cache management ---------------------------------------------------

    def _ensure(self, start: pd.Timestamp, end: pd.Timestamp) -> None:
        start, end = to_utc(start), to_utc(end)
        if start > end:
            start, end = end, start
        if self._lo is not None and self._lo <= start and end <= self._hi:
            return
        lo = min(start, self._lo) - self._pad if self._lo is not None else start - self._pad
        hi = max(end, self._hi) + self._pad if self._hi is not None else end + self._pad
        self._load(lo, hi)

    def _load(self, lo: pd.Timestamp, hi: pd.Timestamp) -> None:
        sessions = self.provider.sessions(lo, hi)
        if sessions:
            starts = np.array([iv[0].value for iv in sessions], dtype=np.int64)
            ends = np.array([iv[1].value for iv in sessions], dtype=np.int64)
        else:
            starts = np.empty(0, dtype=np.int64)
            ends = np.empty(0, dtype=np.int64)
        self._starts, self._ends = starts, ends
        self._cum = np.concatenate([[0], np.cumsum(ends - starts)]).astype(np.int64)
        self._lo, self._hi = lo, hi

    # -- core primitives ----------------------------------------------------

    def _measure_ns(self, t_ns: int) -> int:
        """Market nanoseconds from the cache start up to ``t_ns`` (monotone)."""
        if self._starts.size == 0:
            return 0
        if t_ns <= self._starts[0]:
            return 0
        if t_ns >= self._ends[-1]:
            return int(self._cum[-1])
        i = int(np.searchsorted(self._starts, t_ns, side="right") - 1)
        if i < 0:
            return 0
        if t_ns >= self._ends[i]:          # in a gap after session i
            return int(self._cum[i + 1])
        return int(self._cum[i] + (t_ns - self._starts[i]))

    def elapsed(self, t_from, t_to) -> float:
        """Signed market seconds from ``t_from`` to ``t_to``.

        Time while the market is shut contributes exactly zero, which is what
        makes a Friday-to-Monday trendline behave.
        """
        a, b = to_utc(t_from), to_utc(t_to)
        sign = 1.0
        if b < a:
            a, b = b, a
            sign = -1.0
        self._ensure(a, b)
        delta_ns = self._measure_ns(b.value) - self._measure_ns(a.value)
        return sign * delta_ns / NS

    def _measure_ns_array(self, t_ns: np.ndarray) -> np.ndarray:
        """Vectorised :meth:`_measure_ns` -- used to price a whole bar series."""
        if self._starts.size == 0:
            return np.zeros(t_ns.shape, dtype=np.int64)
        clipped = np.clip(t_ns, self._starts[0], self._ends[-1])
        idx = np.searchsorted(self._starts, clipped, side="right") - 1
        idx = np.clip(idx, 0, self._starts.size - 1)
        inside = clipped < self._ends[idx]
        return np.where(
            inside,
            self._cum[idx] + (clipped - self._starts[idx]),
            self._cum[idx + 1],
        )

    def elapsed_from(self, t_from, timestamps) -> np.ndarray:
        """Market seconds from ``t_from`` to each of ``timestamps``.

        The array form of :meth:`elapsed`. Values before ``t_from`` come back
        negative, matching the scalar version.
        """
        stamps = pd.DatetimeIndex(timestamps)
        if stamps.tz is None:
            stamps = stamps.tz_localize("UTC")
        else:
            stamps = stamps.tz_convert("UTC")
        if len(stamps) == 0:
            return np.empty(0)

        origin = to_utc(t_from)
        self._ensure(min(origin, stamps.min()), max(origin, stamps.max()))
        values = _as_ns(stamps)
        return (self._measure_ns_array(values) - self._measure_ns(origin.value)) / NS

    def is_open_array(self, timestamps) -> np.ndarray:
        """Vectorised :meth:`is_open` -- filters closed-market prints in bulk."""
        stamps = pd.DatetimeIndex(timestamps)
        stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
        if len(stamps) == 0:
            return np.empty(0, dtype=bool)

        self._ensure(stamps.min(), stamps.max())
        if self._starts.size == 0:
            return np.zeros(len(stamps), dtype=bool)
        values = _as_ns(stamps)
        idx = np.searchsorted(self._starts, values, side="right") - 1
        valid = idx >= 0
        safe = np.clip(idx, 0, self._starts.size - 1)
        return valid & (values < self._ends[safe])

    def advance(self, t_from, seconds: float) -> pd.Timestamp:
        """Inverse of :meth:`elapsed` -- the instant N market seconds after
        ``t_from``. Lands on the next open if it would fall in a closed gap."""
        a = to_utc(t_from)
        # Market seconds are at most wall seconds, so this window always covers
        # the answer; the +2d absorbs weekend padding at the boundary.
        self._ensure(a, a + pd.Timedelta(seconds=abs(seconds)) + pd.Timedelta(days=2))
        if self._starts.size == 0:
            return a
        target = self._measure_ns(a.value) + int(round(seconds * NS))
        target = max(0, min(target, int(self._cum[-1])))
        i = int(np.searchsorted(self._cum, target, side="right") - 1)
        i = min(max(i, 0), self._starts.size - 1)
        return pd.Timestamp(self._starts[i] + (target - self._cum[i]), unit="ns", tz="UTC")

    def is_open(self, t) -> bool:
        ts = to_utc(t)
        self._ensure(ts, ts)
        if self._starts.size == 0:
            return False
        i = int(np.searchsorted(self._starts, ts.value, side="right") - 1)
        return bool(i >= 0 and ts.value < self._ends[i])

    def snap_forward(self, t) -> pd.Timestamp:
        """Move a closed-market instant to the next open. A no-op when open.

        This is how weekend/holiday anchors are handled: the dead time measures
        zero market seconds anyway, so snapping forward changes nothing about the
        arithmetic -- it just gives the anchor an honest timestamp to display.
        """
        ts = to_utc(t)
        self._ensure(ts, ts + pd.Timedelta(days=10))
        if self._starts.size == 0:
            return ts
        i = int(np.searchsorted(self._starts, ts.value, side="right") - 1)
        if i >= 0 and ts.value < self._ends[i]:
            return ts
        nxt = int(np.searchsorted(self._starts, ts.value, side="left"))
        if nxt >= self._starts.size:
            return ts
        return pd.Timestamp(self._starts[nxt], unit="ns", tz="UTC")

    def snap_backward(self, t) -> pd.Timestamp:
        """Move a closed-market instant back to the previous close."""
        ts = to_utc(t)
        self._ensure(ts - pd.Timedelta(days=10), ts)
        if self._starts.size == 0:
            return ts
        i = int(np.searchsorted(self._starts, ts.value, side="right") - 1)
        if i < 0:
            return ts
        if ts.value < self._ends[i]:
            return ts
        return pd.Timestamp(self._ends[i], unit="ns", tz="UTC")

    # -- range helpers ------------------------------------------------------

    def sessions_between(self, t_from, t_to) -> list[Interval]:
        """Open intervals overlapping [t_from, t_to], clipped to the window."""
        a, b = to_utc(t_from), to_utc(t_to)
        if b < a:
            a, b = b, a
        self._ensure(a, b)
        out: list[Interval] = []
        lo = int(np.searchsorted(self._ends, a.value, side="right"))
        for i in range(lo, self._starts.size):
            if self._starts[i] >= b.value:
                break
            start = max(self._starts[i], a.value)
            end = min(self._ends[i], b.value)
            if end > start:
                out.append((
                    pd.Timestamp(start, unit="ns", tz="UTC"),
                    pd.Timestamp(end, unit="ns", tz="UTC"),
                ))
        return out

    def stats(self, t_from, t_to) -> RangeStats:
        a, b = to_utc(t_from), to_utc(t_to)
        if b < a:
            a, b = b, a
        spans = self.sessions_between(a, b)
        return RangeStats(
            rth_seconds=self.elapsed(a, b),
            wall_seconds=(b - a).total_seconds(),
            session_count=len(spans),
            first_open=spans[0][0] if spans else None,
            last_close=spans[-1][1] if spans else None,
        )

    def grid(
        self,
        t_from,
        t_to,
        step_seconds: float = 1.0,
        max_points: int = 2_000_000,
    ) -> tuple[pd.DatetimeIndex, np.ndarray]:
        """Every open-market timestamp in [t_from, t_to) at ``step_seconds``.

        Returns the timestamps *and* their cumulative market-second offsets from
        ``t_from``. The offsets are the trendline's x-axis: consecutive points
        are always ``step_seconds`` apart in market time even where the wall
        clock jumps across a weekend.

        Raises ``ValueError`` past ``max_points`` rather than quietly thinning
        the grid -- a silently coarsened grid misses crossings.
        """
        if step_seconds <= 0:
            raise ValueError("step_seconds must be positive")

        a, b = to_utc(t_from), to_utc(t_to)
        if b <= a:
            return pd.DatetimeIndex([], tz="UTC"), np.empty(0)

        total = self.elapsed(a, b)
        estimate = int(total // step_seconds) + 1
        if estimate > max_points:
            raise ValueError(
                f"Grid would hold ~{estimate:,} points at a {step_seconds:g}s step "
                f"(limit {max_points:,}). Widen the step or shorten the range."
            )

        step_ns = int(round(step_seconds * NS))
        chunks: list[np.ndarray] = []
        offsets: list[np.ndarray] = []
        carry = 0            # market ns already consumed by earlier sessions
        for start, end in self.sessions_between(a, b):
            s, e = start.value, end.value
            # Keep points phase-aligned to the market clock, not to each session
            # start, so offsets stay exact multiples of the step where possible.
            first = s + ((-carry) % step_ns)
            if first >= e:
                carry += e - s
                continue
            points = np.arange(first, e, step_ns, dtype=np.int64)
            chunks.append(points)
            offsets.append(carry + (points - s))
            carry += e - s

        if not chunks:
            return pd.DatetimeIndex([], tz="UTC"), np.empty(0)

        stamps = pd.DatetimeIndex(np.concatenate(chunks), tz="UTC")
        secs = np.concatenate(offsets).astype(np.float64) / NS
        return stamps, secs
