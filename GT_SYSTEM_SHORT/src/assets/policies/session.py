"""SessionPolicy — when this asset is tradable, in the asset's exchange timezone.

The 156 references to RTH / market hours / session timing in
engine.py and health_check_loop all assume US equity RTH:
09:30-16:00 ET, Monday-Friday, US federal holiday calendar. Every
other asset has different rules:

  US equity:   09:30-16:00 ET, M-F, NYSE holiday calendar
  Forex:       Continuous Sunday 22:00 UTC → Friday 22:00 UTC
               (no daily close, just a weekly close)
  Futures (CME): 23h/day with a daily 17:00-18:00 ET maintenance halt,
                 plus Saturday-Sunday closed
  LSE equity:   08:00-16:30 GMT, M-F, UK bank holidays
  TSE equity:   00:00-02:30 + 03:30-06:00 UTC (with a 1-hour lunch break),
                 M-F, Japanese holidays
  CFDs:         Track the underlying instrument's session
                (most index CFDs are 23h with venue-specific halts)

SessionPolicy must answer:
  * "Is the market open RIGHT NOW?" — for tick processing decisions
  * "When does the next session open / close?" — for end-of-day logic
  * "Are we within N minutes of close?" — for don't-enter-near-close
    risk gates

Timezone-correct via stdlib `zoneinfo` (Python 3.9+). Holiday
calendars are policy-internal — equity uses pandas_market_calendars
or a hardcoded list; FX uses none (only weekly halt); futures use
exchange-specific.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """One contiguous trading window. A trading day may have one
    (equity RTH) or multiple (TSE morning + afternoon).

    `open_utc` and `close_utc` are both UTC, timezone-aware. Naive
    datetimes are a bug source we refuse to ship.
    """

    open_utc: datetime
    close_utc: datetime

    def __post_init__(self):
        if self.open_utc.tzinfo is None or self.close_utc.tzinfo is None:
            raise ValueError(
                "SessionWindow times must be timezone-aware (UTC). "
                "Naive datetimes lead to silent off-by-hour bugs."
            )
        if self.close_utc <= self.open_utc:
            raise ValueError(
                f"close_utc {self.close_utc} must be after open_utc {self.open_utc}"
            )

    def contains(self, ts: datetime) -> bool:
        """Is `ts` within this window (inclusive open, exclusive close)?"""
        return self.open_utc <= ts < self.close_utc

    def duration_seconds(self) -> int:
        return int((self.close_utc - self.open_utc).total_seconds())


@runtime_checkable
class SessionPolicy(Protocol):
    """Trading-session awareness for one asset class.

    Implementations are typically stateless (pure functions over the
    given timestamp) but may cache holiday calendars internally for
    speed.
    """

    def is_open_at(self, ts: datetime) -> bool:
        """Is the market open at `ts`?

        `ts` must be UTC timezone-aware. Returns True iff `ts` falls
        inside any of today's SessionWindows for this asset.
        """
        ...

    def windows_for_date(self, ts: datetime) -> list[SessionWindow]:
        """All trading windows that overlap the calendar date of `ts`
        (in the asset's local timezone). Most assets have 0 or 1
        windows per date; TSE has 2 (morning + afternoon).

        Returns empty list on weekends / holidays.
        """
        ...

    def next_open(self, ts: datetime) -> datetime:
        """The next time the market opens after `ts`. If `ts` is
        already inside a session, this returns the NEXT session's open
        (not the current one)."""
        ...

    def next_close(self, ts: datetime) -> datetime:
        """The next time the market closes after `ts`. If currently
        in a session, returns that session's close."""
        ...

    def is_within_n_minutes_of_close(self, ts: datetime, minutes: int) -> bool:
        """Useful gate: 'don't enter a new position within 5 minutes
        of close.' Returns False if not in a session at all.
        """
        ...

    def time_to_close(self, ts: datetime) -> Optional[int]:
        """Seconds until the current session closes, or None if not
        currently in a session."""
        ...
