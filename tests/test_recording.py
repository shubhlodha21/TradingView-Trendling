"""Checks for live-feed recording.

The point of a recording is that it comes back: the last section reads a
recorded file through CsvFeed and confirms the engine sees the same prices it
wrote, because a recording that cannot be replayed is just a log.

Run with:  python -m tests.test_recording
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from rth.feeds import CsvFeed
from rth.recording import FIELDS, TickRecorder

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        got={got!r} want={want!r}")


def truthy(name, got):
    (PASS if got else FAIL).append(name)
    print(f"[{'PASS' if got else 'FAIL'}] {name}  -> {got!r}")


WORK = pathlib.Path(tempfile.mkdtemp(prefix="rth-rec-"))
T = pd.Timestamp


def observation(ticker="AAPL", price=205.5, **extra) -> dict:
    return {
        "ticker": ticker, "price": price, "high": price + 0.05, "low": price - 0.05,
        "rth_seconds": 1800.0, "line_price": 205.0, "gap": price - 205.0,
        "state": "above" if price > 205 else "below", "beyond_line": price > 205,
        "direction": "UP", "bid": price - 0.01, "ask": price + 0.01, "last": price,
        "volume": 1000.0, "quote_time": T("2025-01-06 15:00:00", tz="UTC"),
        "age_seconds": 0.2, "market": "EQ:XNYS", "source": "ibkr",
        # Fields the recorder should ignore rather than choke on.
        "decimals": 4, "epoch": 1736175600.0, "time": "2025-01-06 15:00:00Z",
        **extra,
    }


# --------------------------------------------------------------------------- #
print("\n=== a directory gets one file per instrument per UTC day ===")

folder = WORK / "ticks"
rec = TickRecorder(folder)
rec.write(T("2025-01-06 15:00:00", tz="UTC"), observation("AAPL"))
rec.write(T("2025-01-06 15:00:01", tz="UTC"), observation("MSFT", price=410.0))
rec.write(T("2025-01-06 23:59:59", tz="UTC"), observation("AAPL", price=206.0))
rec.write(T("2025-01-07 00:00:00", tz="UTC"), observation("AAPL", price=207.0))
rec.close()

names = sorted(p.name for p in folder.iterdir())
check("one file per instrument per day", names,
      ["AAPL-20250106.csv", "AAPL-20250107.csv", "MSFT-20250106.csv"])
check("rows land in the right day",
      len(pd.read_csv(folder / "AAPL-20250106.csv")), 2)
check("the new day starts a new file",
      len(pd.read_csv(folder / "AAPL-20250107.csv")), 1)
check("rows are counted", rec.rows_written, 4)

# Restarting must extend the day's file, not truncate what was collected.
again = TickRecorder(folder)
again.write(T("2025-01-06 15:00:02", tz="UTC"), observation("AAPL", price=208.0))
again.close()
resumed = pd.read_csv(folder / "AAPL-20250106.csv")
check("a restart appends rather than truncating", len(resumed), 3)
check("...and writes no second header", list(resumed.columns), FIELDS)

flat = TickRecorder(WORK / "flat", rotate_daily=False)
flat.write(T("2025-01-06 15:00:00", tz="UTC"), observation("AAPL"))
flat.write(T("2025-01-09 15:00:00", tz="UTC"), observation("AAPL"))
flat.close()
check("--no-rotate keeps one file per instrument",
      sorted(p.name for p in (WORK / "flat").iterdir()), ["AAPL.csv"])
check("...holding every day", len(pd.read_csv(WORK / "flat" / "AAPL.csv")), 2)


# --------------------------------------------------------------------------- #
print("\n=== a filename gets one combined file ===")

combined = WORK / "all-ticks.csv"
one = TickRecorder(combined)
one.write(T("2025-01-06 15:00:00", tz="UTC"), observation("AAPL"))
one.write(T("2025-01-07 15:00:00", tz="UTC"), observation("MSFT", price=410.0))
one.close()
frame = pd.read_csv(combined)
check("both instruments share the file", sorted(frame["ticker"]), ["AAPL", "MSFT"])
check("no rotation despite the date change", len(frame), 2)

lines_path = WORK / "ticks.jsonl"
js = TickRecorder(lines_path)
check("the format follows the extension", js.fmt, "jsonl")
js.write(T("2025-01-06 15:00:00", tz="UTC"), observation("AAPL"))
js.write(T("2025-01-06 15:00:01", tz="UTC"), observation("EURUSD", price=1.0857))
js.close()
records = [json.loads(line) for line in lines_path.read_text().splitlines()]
check("one JSON object per line", len(records), 2)
check("with the full field set", set(records[0]) , set(FIELDS))
check("timestamps are ISO 8601 with an offset", records[0]["timestamp"],
      "2025-01-06T15:00:00+00:00")
truthy("a Timestamp quote_time is serialised too",
       records[0]["quote_time"].startswith("2025-01-06T15:00:00"))

try:
    TickRecorder(WORK / "bad.txt", fmt="parquet")
    truthy("an unsupported format is rejected", False)
except ValueError as exc:
    truthy("an unsupported format is rejected", "csv or jsonl" in str(exc))

slashed = TickRecorder(WORK / "fx")
slashed.write(T("2025-01-06 15:00:00", tz="UTC"), observation("EUR/USD", price=1.08))
slashed.close()
check("a slash in the ticker cannot escape the folder",
      [p.name for p in (WORK / "fx").iterdir()], ["EUR-USD-20250106.csv"])


# --------------------------------------------------------------------------- #
print("\n=== the recording replays ===")

check("CsvFeed's columns come first", FIELDS[:4],
      ["timestamp", "price", "high", "low"])

session = WORK / "session"
live = TickRecorder(session)
written = []
for i in range(10):
    price = 205.0 + i * 0.25
    stamp = T("2025-01-06 15:00:00", tz="UTC") + pd.Timedelta(seconds=i)
    written.append((stamp, price))
    live.write(stamp, observation("AAPL", price=price))
live.close()

# Exactly how live.py rebuilds a feed from a recording.
replay = CsvFeed(session / "AAPL-20250106.csv", high_col="high", low_col="low")
check("every observation comes back", len(replay.data), 10)
check("prices survive the round trip",
      [round(p, 4) for p in replay.data["price"]],
      [round(p, 4) for _, p in written])
check("timestamps survive the round trip",
      list(replay.data["timestamp"]), [s for s, _ in written])
truthy("the wicks survive too -- touch mode still works on a replay",
       bool((replay.data["high"] > replay.data["price"]).all()
            and (replay.data["low"] < replay.data["price"]).all()))

tick = replay.tick(T("2025-01-06 15:00:05", tz="UTC"))
check("a replayed tick is the latest at or before that instant",
      round(tick["price"], 4), round(205.0 + 5 * 0.25, 4))


shutil.rmtree(WORK, ignore_errors=True)

print("\n" + "=" * 60)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  FAILED: {name}")
sys.exit(1 if FAIL else 0)