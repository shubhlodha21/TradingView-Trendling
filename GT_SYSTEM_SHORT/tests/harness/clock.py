"""Clock port — abstract time source for the engine + harness.

WHY THIS EXISTS:
    Production code calls `datetime.now()` directly. That's fine for live
    trading (we want real time) but FATAL for deterministic testing:
        - Same scenario run twice produces different timestamps
        - Can't fast-forward simulated time
        - Can't replay a failure exactly
        - Timing-sensitive bugs (e.g., "commission report arrives 200ms
          after fill") can't be reproduced

THE FIX:
    Engine accepts a `Clock` via constructor injection. Production wires
    `RealClock` (delegates to datetime.now). Tests wire `SimulatedClock`
    (advances manually, fully deterministic).

    Same code, two time sources. The whole engine becomes a pure function
    of (initial_state, clock_advance_sequence, broker_events).

DESIGN NOTES:
    - Clock is a Protocol so any concrete implementation works.
    - Async-aware: `sleep()` is the async primitive that scenarios use
      to advance time. RealClock delegates to asyncio.sleep; SimulatedClock
      advances its internal clock and yields control.
    - All datetimes are tz-aware UTC. No naive datetimes ever.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Abstract time source. Engine + harness use ONLY this — never
    datetime.now() directly."""

    def now(self) -> datetime:
        """Current time as tz-aware UTC datetime."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Yield control for `seconds` of clock time. Async-aware so
        the engine's existing `await asyncio.sleep(...)` patterns work.
        """
        ...

    def monotonic(self) -> float:
        """Monotonic seconds since clock epoch. For latency measurement.
        Must be strictly non-decreasing across calls.
        """
        ...


class RealClock:
    """Production clock — delegates to system time. Use everywhere in
    live/paper code. Tests should NEVER use this directly."""

    __slots__ = ('_started_at',)

    def __init__(self) -> None:
        import time
        self._started_at = time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def monotonic(self) -> float:
        import time
        return time.monotonic()


class SimulatedClock:
    """Test clock — advances manually under scenario control.

    `now()` returns the simulated wall-clock. `sleep(s)` advances the
    simulated clock by `s` seconds and yields control to the event loop
    (so any pending tasks scheduled at the new time can fire).

    Scenarios drive time explicitly:

        clock = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=tz.utc))
        await scenario_run(env, clock)
        # scenario calls clock.sleep(30) to advance 30s of "market time"
        # while running in <1ms of wall time.

    DETERMINISM: the entire scenario produces the same outputs every run
    given the same (initial_state, clock_script, broker_events).
    """

    __slots__ = ('_now', '_monotonic_base')

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimulatedClock requires a tz-aware start datetime")
        self._now: datetime = start.astimezone(timezone.utc)
        self._monotonic_base: float = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic_base

    async def sleep(self, seconds: float) -> None:
        """Advance simulated time and yield. The yield lets other tasks
        scheduled to run "before" the new clock time get their chance.
        """
        if seconds < 0:
            raise ValueError("Cannot sleep negative seconds")
        self._now += timedelta(seconds=seconds)
        self._monotonic_base += seconds
        # Yield once so any tasks awaiting on this clock's sleep can
        # observe the advance. Critically: we do NOT await real time —
        # the await asyncio.sleep(0) just hands control back to the loop.
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        """Synchronous variant of sleep — advance time without yielding.
        Useful for setup code that needs to position the clock before
        starting a scenario.
        """
        if seconds < 0:
            raise ValueError("Cannot advance negative seconds")
        self._now += timedelta(seconds=seconds)
        self._monotonic_base += seconds


class FrozenClock:
    """Pathological-case clock that NEVER advances. Useful for testing
    code paths that assume time doesn't move (e.g., what happens if the
    system clock is wedged?)."""

    __slots__ = ('_at',)

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FrozenClock requires a tz-aware datetime")
        self._at = at.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._at

    async def sleep(self, seconds: float) -> None:
        # Time doesn't advance, but we still yield so async machinery works.
        await asyncio.sleep(0)

    def monotonic(self) -> float:
        return 0.0


__all__ = ["Clock", "RealClock", "SimulatedClock", "FrozenClock"]
