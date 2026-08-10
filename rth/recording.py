"""Recording the live feed to disk as it arrives.

Every comparison the engine makes -- the live quote, the trendline's price at
that instant, and the gap between them -- is appended to a file. Two reasons
that matters beyond having a log:

* **It is replayable.** The CSV columns lead with ``timestamp, price, high,
  low``, which is exactly what :class:`~rth.feeds.CsvFeed` reads. Today's
  recording is tomorrow's backtest, run through the same engine that produced
  it, so a signal can be reproduced exactly rather than argued about.
* **Quotes are perishable.** IB serves no history at second resolution beyond a
  short window, and none at all for what your particular subscription saw. If
  it is not written down as it arrives, it is gone.

Files rotate per instrument per UTC day, so a long-running session produces
``AAPL-20260803.csv``, ``AAPL-20260804.csv`` and so on rather than one
ever-growing file. Point ``--record`` at a filename ending in ``.csv`` or
``.jsonl`` instead to get a single combined file.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

# Leading four columns are the CsvFeed contract -- keep them first and named
# exactly this, or a recording stops being replayable.
FIELDS = [
    "timestamp", "price", "high", "low",
    "ticker", "rth_seconds", "line_price", "gap", "state", "beyond_line",
    "direction", "bid", "ask", "last", "volume", "quote_time", "age_seconds",
    "market", "source",
]

CSV, JSONL = "csv", "jsonl"


class TickRecorder:
    """Appends every observation to CSV or JSON lines.

    ``target`` ending in ``.csv``/``.jsonl`` is one combined file; anything
    else is treated as a directory and gets one file per instrument per UTC
    day. Handles stay open and every row is flushed, so a kill -9 loses
    nothing but the row in flight.
    """

    def __init__(self, target: str | Path, fmt: str | None = None,
                 rotate_daily: bool = True):
        path = Path(target).expanduser()
        suffix = path.suffix.lower().lstrip(".")

        self.single_file = suffix in (CSV, JSONL)
        self.fmt = (fmt or (suffix if self.single_file else CSV)).lower()
        if self.fmt not in (CSV, JSONL):
            raise ValueError(f"record format must be csv or jsonl, got {self.fmt!r}")

        self.target = path
        self.rotate_daily = rotate_daily and not self.single_file
        self.directory = path.parent if self.single_file else path
        self.directory.mkdir(parents=True, exist_ok=True)

        self._handles: dict[Path, object] = {}
        self._writers: dict[Path, object] = {}
        self.rows_written = 0

    # -- routing ------------------------------------------------------------

    def path_for(self, ticker: str, when: pd.Timestamp) -> Path:
        if self.single_file:
            return self.target
        stem = ticker.replace("/", "-").replace("\\", "-")
        if self.rotate_daily:
            stem = f"{stem}-{when.strftime('%Y%m%d')}"
        return self.directory / f"{stem}.{self.fmt}"

    @property
    def files(self) -> list[Path]:
        return sorted(self._handles)

    # -- writing ------------------------------------------------------------

    def write(self, when: pd.Timestamp, row: dict) -> None:
        record = self._record(when, row)
        path = self.path_for(str(row.get("ticker", "FEED")), when)
        handle = self._handles.get(path)

        if handle is None:
            # Append, so a restart continues the day's file instead of
            # truncating what was already collected.
            fresh = not path.exists() or path.stat().st_size == 0
            handle = open(path, "a", newline="", encoding="utf-8")
            self._handles[path] = handle
            if self.fmt == CSV:
                writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
                self._writers[path] = writer
                if fresh:
                    writer.writeheader()

        if self.fmt == CSV:
            self._writers[path].writerow(record)
        else:
            handle.write(json.dumps(record, default=str) + "\n")
        handle.flush()
        self.rows_written += 1

    @staticmethod
    def _record(when: pd.Timestamp, row: dict) -> dict:
        quote_time = row.get("quote_time")
        record = {key: row.get(key) for key in FIELDS}
        # ISO 8601 with an explicit offset: unambiguous for pandas, Excel and
        # anything else that later reads this back.
        record["timestamp"] = when.isoformat()
        if isinstance(quote_time, pd.Timestamp):
            record["quote_time"] = quote_time.isoformat()
        return record

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        for handle in self._handles.values():
            try:
                handle.flush()
                handle.close()
            except OSError:
                pass
        self._handles.clear()
        self._writers.clear()

    def __enter__(self) -> "TickRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def describe(self) -> str:
        where = (str(self.target) if self.single_file
                 else f"{self.directory}{Path('/').as_posix()}"
                      f"TICKER{'-YYYYMMDD' if self.rotate_daily else ''}.{self.fmt}")
        return f"{self.fmt} -> {where}"