"""Correctness checks for the RTH clock, trendline and crossing detector.

Run with:  python -m tests.test_engine
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from rth import Anchor, CrossDetector, RthClock, SimulatedFeed, Trendline, get_market

PASS, FAIL = [], []


def check(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol if isinstance(want, (int, float)) else got == want
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}\n        got={got!r} want={want!r}")


def truthy(name, got):
    (PASS if got else FAIL).append(name)
    print(f"[{'PASS' if got else 'FAIL'}] {name}  -> {got!r}")


T = pd.Timestamp

# --------------------------------------------------------------------------- #
print("\n=== US equities (XNYS) ===")
nyse = RthClock(get_market("EQ:XNYS").provider("RTH (regular session)"))

# A regular winter session is 09:30-16:00 ET == 14:30-21:00 UTC == 6.5h.
check("full session = 6.5h",
      nyse.elapsed(T("2025-01-06 14:30", tz="UTC"), T("2025-01-06 21:00", tz="UTC")),
      23400)

# Friday 20:00 UTC -> Monday 15:00 UTC. Wall clock says 67 hours; the market
# says 1h Friday + 0.5h Monday. This is the whole point of the exercise.
fri, mon = T("2025-01-03 20:00", tz="UTC"), T("2025-01-06 15:00", tz="UTC")
check("weekend gap contributes zero", nyse.elapsed(fri, mon), 5400)
check("...vs wall clock", (mon - fri).total_seconds(), 241200)

# Thanksgiving 2024: Thu 28th closed entirely, Fri 29th closes early at 13:00 ET.
check("Thanksgiving Thursday is zero",
      nyse.elapsed(T("2024-11-28 00:00", tz="UTC"), T("2024-11-29 00:00", tz="UTC")), 0)
check("half-day Friday = 3.5h",
      nyse.elapsed(T("2024-11-29 00:00", tz="UTC"), T("2024-11-30 00:00", tz="UTC")),
      12600)

# Overnight and pre-market prints must not count.
check("closed hours contribute zero",
      nyse.elapsed(T("2025-01-06 21:00", tz="UTC"), T("2025-01-07 14:30", tz="UTC")), 0)

# DST: July session is 13:30-20:00 UTC, still 6.5h of market time.
check("summer session is still 6.5h",
      nyse.elapsed(T("2025-07-07 13:30", tz="UTC"), T("2025-07-07 20:00", tz="UTC")),
      23400)

print("\n--- advance() is the inverse of elapsed() ---")
for secs in (0, 1, 3600, 23400, 23401, 100_000):
    landed = nyse.advance(fri, secs)
    check(f"advance {secs}s then measure back", nyse.elapsed(fri, landed), float(secs))
truthy("advance skips the weekend",
       nyse.advance(fri, 3601) == T("2025-01-06 14:30:01", tz="UTC"))

print("\n--- snapping ---")
sat = T("2025-01-04 12:00", tz="UTC")
truthy("Saturday snaps to Monday open",
       nyse.snap_forward(sat) == T("2025-01-06 14:30", tz="UTC"))
truthy("Saturday snaps back to Friday close",
       nyse.snap_backward(sat) == T("2025-01-03 21:00", tz="UTC"))
truthy("open instant is unchanged by snapping",
       nyse.snap_forward(T("2025-01-06 15:00", tz="UTC")) == T("2025-01-06 15:00", tz="UTC"))
truthy("is_open agrees", nyse.is_open(T("2025-01-06 15:00", tz="UTC")) is True
       and nyse.is_open(sat) is False)

print("\n--- grid ---")
stamps, offsets = nyse.grid(fri, mon, step_seconds=1.0)
check("grid point count", len(stamps), 5400)
check("offsets are contiguous market seconds", float(np.diff(offsets).max()), 1.0)
truthy("no weekend timestamps in grid",
       not any(ts.dayofweek >= 5 for ts in stamps))
truthy("grid jumps Fri 21:00 -> Mon 14:30",
       stamps[3599] == T("2025-01-03 20:59:59", tz="UTC")
       and stamps[3600] == T("2025-01-06 14:30", tz="UTC"))

stamps60, _ = nyse.grid(fri, mon, step_seconds=60.0)
check("60s step count", len(stamps60), 90)

print("\n--- range stats ---")
stats = nyse.stats(fri, mon)
check("sessions spanned", stats.session_count, 2)
check("closed seconds", stats.closed_seconds, 235800)

# --------------------------------------------------------------------------- #
print("\n=== FX (24x5) ===")
fx = RthClock(get_market("FX:SPOT").provider("24x5 (Sun 17:00 - Fri 17:00 ET)"))
check("a full weekday is 24h",
      fx.elapsed(T("2025-01-07 00:00", tz="UTC"), T("2025-01-08 00:00", tz="UTC")), 86400)
check("Saturday is closed",
      fx.elapsed(T("2025-01-04 00:00", tz="UTC"), T("2025-01-05 00:00", tz="UTC")), 0)
# Friday 17:00 ET = 22:00 UTC in winter; Sunday reopen 17:00 ET = 22:00 UTC.
check("weekend break is 48h exactly",
      fx.elapsed(T("2025-01-03 22:00", tz="UTC"), T("2025-01-05 22:00", tz="UTC")), 0)
# OTC holidays are modelled as whole local calendar days (New York for FX), so
# Christmas Day New York is shut. The 00:00-05:00 UTC sliver still belongs to
# Christmas Eve NY and stays open -- brokers differ by a few hours here, which
# is why otc_holidays() is overridable.
check("FX shut through Christmas Day (NY)",
      fx.elapsed(T("2025-12-25 12:00", tz="UTC"), T("2025-12-25 20:00", tz="UTC")), 0)
truthy("...but Christmas Eve still trades",
       fx.elapsed(T("2025-12-24 12:00", tz="UTC"), T("2025-12-24 20:00", tz="UTC")) == 28800)

print("\n=== Tokyo lunch break ===")
tokyo = RthClock(get_market("EQ:XTKS").provider("RTH (regular session)"))
# 09:00-11:30 and 12:30-15:00 JST == 5h of market time, lunch excluded.
check("XTKS day excludes the lunch break",
      tokyo.elapsed(T("2025-01-06 00:00", tz="UTC"), T("2025-01-06 06:00", tz="UTC")),
      18000)

# --------------------------------------------------------------------------- #
print("\n=== Trendline ===")
line = Trendline(
    nyse,
    Anchor(T("2025-01-03 20:00", tz="UTC"), 100.0),
    Anchor(T("2025-01-06 15:00", tz="UTC"), 105.0),
    direction="UP",
)
check("span in market seconds", line.span_seconds, 5400)
check("slope per market second", line.slope, 5.0 / 5400)
check("start anchor price", line.price_at(T("2025-01-03 20:00", tz="UTC")), 100.0)
check("end anchor price", line.price_at(T("2025-01-06 15:00", tz="UTC")), 105.0)

# Friday's close is 3600 of the 5400 seconds, so the line must sit at 100 + 5*2/3.
check("price at Friday close", line.price_at(T("2025-01-03 21:00", tz="UTC")),
      100.0 + 5.0 * 3600 / 5400)
# ...and it must be at exactly the same price when Monday opens, having held
# flat all weekend. A wall-clock line would be far past its end price by here.
check("price holds flat over the weekend",
      line.price_at(T("2025-01-06 14:30", tz="UTC")),
      line.price_at(T("2025-01-03 21:00", tz="UTC")))
check("Saturday reads the same too",
      line.price_at(T("2025-01-04 12:00", tz="UTC")),
      100.0 + 5.0 * 3600 / 5400)

naive = 100.0 + (105.0 - 100.0) * (
    (T("2025-01-06 14:30", tz="UTC") - T("2025-01-03 20:00", tz="UTC")).total_seconds()
    / (mon - fri).total_seconds()
)
print(f"        (a wall-clock line would say {naive:.4f} at Monday's open "
      f"vs {line.price_at(T('2025-01-06 14:30', tz='UTC')):.4f} -- "
      f"{abs(naive - line.price_at(T('2025-01-06 14:30', tz='UTC'))):.4f} of drift)")

check("line is clamped before it starts",
      line.price_at(T("2025-01-02 15:00", tz="UTC")), 100.0)
check("line is clamped after it ends",
      line.price_at(T("2025-01-08 15:00", tz="UTC")), 105.0)

frame = line.series(step_seconds=1.0)
check("series row count (grid + closing anchor)", len(frame), 5401)
truthy("series has no weekend rows",
       not any(pd.Timestamp(ts).dayofweek >= 5 for ts in frame["timestamp"]))
check("series ends on the end anchor", float(frame["line_price"].iloc[-1]), 105.0)

print("\n--- anchors landing on a weekend get snapped ---")
snapped = Trendline(
    nyse,
    Anchor(T("2025-01-04 12:00", tz="UTC"), 100.0),   # a Saturday
    Anchor(T("2025-01-06 16:00", tz="UTC"), 101.0),
    direction="UP",
)
truthy("start anchor moved to Monday open", snapped.start_was_snapped)
check("snapped start", snapped.start.time, T("2025-01-06 14:30", tz="UTC"))
check("span after snapping", snapped.span_seconds, 5400)

print("\n--- a range with no open market at all is rejected ---")
try:
    Trendline(nyse, Anchor(T("2025-01-04 10:00", tz="UTC"), 1.0),
              Anchor(T("2025-01-04 20:00", tz="UTC"), 2.0))
    truthy("weekend-only range raises", False)
except ValueError as exc:
    truthy("weekend-only range raises", "open-market time" in str(exc))

# --------------------------------------------------------------------------- #
print("\n=== Crossing detection ===")
det = CrossDetector(line, mode="last")
truthy("no signal while price stays below",
       det.update(T("2025-01-03 20:30", tz="UTC"), 99.0) is None)
truthy("still nothing just under the line",
       det.update(T("2025-01-03 20:45", tz="UTC"), line.price_at(T("2025-01-03 20:45", tz="UTC")) - 0.01) is None)

event = det.update(T("2025-01-06 14:45", tz="UTC"),
                   line.price_at(T("2025-01-06 14:45", tz="UTC")) + 0.05)
truthy("UP cross fires", event is not None and event.direction == "UP")
truthy("cross is not repeated by default",
       det.update(T("2025-01-06 14:50", tz="UTC"), 200.0) is None)

print("\n--- closed-market prints cannot signal ---")
det2 = CrossDetector(line, mode="last")
det2.update(T("2025-01-03 20:30", tz="UTC"), 99.0)
truthy("Saturday print is ignored",
       det2.update(T("2025-01-04 12:00", tz="UTC"), 999.0) is None)

print("\n--- batch scan matches a full sweep ---")
det3 = CrossDetector(line, mode="last", repeat=True)
bars = pd.DataFrame({
    "timestamp": [T("2025-01-03 20:10", tz="UTC"), T("2025-01-03 20:50", tz="UTC"),
                  T("2025-01-04 12:00", tz="UTC"),                       # weekend, dropped
                  T("2025-01-06 14:40", tz="UTC"), T("2025-01-06 14:55", tz="UTC")],
    "price": [99.0, 99.5, 500.0, 104.0, 100.0],
})
found = det3.scan(bars)
truthy("scan finds the one genuine up-cross", len(found) == 1)
truthy("scan ignored the weekend row",
       all(e.time.dayofweek < 5 for e in found))

print("\n--- touch mode triggers on the wick ---")
det4 = CrossDetector(line, mode="touch")
det4.update(T("2025-01-03 20:10", tz="UTC"), 99.0, high=99.1, low=98.9)
at = T("2025-01-06 14:40", tz="UTC")
lp = line.price_at(at)
ev = det4.update(at, lp - 0.10, high=lp + 0.02, low=lp - 0.20)
truthy("wick through the line fires while last price is still below", ev is not None)

print("\n--- DOWN direction ---")
down = Trendline(nyse, Anchor(fri, 105.0), Anchor(mon, 100.0), direction="DOWN")
truthy("slope agrees with a DOWN signal", down.direction_matches_slope)
det5 = CrossDetector(down, mode="last")
det5.update(T("2025-01-03 20:30", tz="UTC"), 110.0)
truthy("DOWN cross fires on break below",
       det5.update(T("2025-01-06 14:45", tz="UTC"),
                   down.price_at(T("2025-01-06 14:45", tz="UTC")) - 0.5) is not None)

# --------------------------------------------------------------------------- #
print("\n=== Simulated feed ===")
feed = SimulatedFeed(line, volatility=1.0, seed=3)
sim = feed.bars(line.start.time, line.end.time, step_seconds=1.0)
check("feed emits one bar per market second", len(sim), 5400)
truthy("feed never prints on a weekend",
       not any(pd.Timestamp(ts).dayofweek >= 5 for ts in sim["timestamp"]))
det6 = CrossDetector(line, mode="last", repeat=True)
sim_events = det6.scan(sim)
truthy(f"simulated path crosses the line ({len(sim_events)} times)", len(sim_events) > 0)
truthy("feed is deterministic for a fixed seed",
       np.allclose(SimulatedFeed(line, seed=3).bars(line.start.time, line.end.time)["price"],
                   sim["price"]))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 60)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print("  FAILED:", name)
sys.exit(1 if FAIL else 0)
