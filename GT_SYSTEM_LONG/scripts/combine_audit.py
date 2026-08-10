#!/usr/bin/env python3
"""Combine per-ticker audit CSVs into single per-day combined files.

Reads:
    data/audit/<YYYYMMDD>/<TICKER>/order.csv  (or .gz)
    data/audit/<YYYYMMDD>/<TICKER>/state.csv  (or .gz)
    data/audit/<YYYYMMDD>/<TICKER>/pnl.csv    (or .gz)

Writes:
    data/audit/<YYYYMMDD>/_combined/order.csv
    data/audit/<YYYYMMDD>/_combined/state.csv
    data/audit/<YYYYMMDD>/_combined/pnl.csv

Sort order
----------
order.csv  : timestamp (ascending)  →  ticker (A-Z, tiebreak)  →  event lifecycle (tiebreak)
             lifecycle = SUBMITTED, BRACKET_SUBMITTED, PARTIAL_FILL, FILLED,
                         CHILD_STOP_MODIFIED, ...other engine events,
                         CANCELLED, REJECTED

state.csv  : timestamp (ascending)  →  ticker (A-Z, tiebreak)
pnl.csv    : timestamp (ascending)  →  ticker (A-Z, tiebreak)

The combined file reads as a true chronological audit feed across all
tickers. Ticker only matters when two events at multiple symbols share
an identical timestamp (rare but possible at sub-millisecond resolution).

Dependencies
------------
Standard library only (csv, gzip, pathlib, argparse, datetime, sys).
No pandas, no third-party packages. Safe to run on any minimal Python 3.8+
install. Reads gzipped audit files transparently via the `gzip` module.

Usage
-----
    python3 scripts/combine_audit.py                       # today
    python3 scripts/combine_audit.py --date 2026-05-27     # specific date
    python3 scripts/combine_audit.py --date 20260527       # YYYYMMDD also OK
    python3 scripts/combine_audit.py --date 20260527 --all # all dates
    python3 scripts/combine_audit.py --out-dir _combined   # custom subfolder

The script is READ-ONLY against the per-ticker audit files. It only WRITES
new files under <DATE>/_combined/. Safe to run during market hours or
after close — no engine state, no broker, no working orders touched.
"""

import argparse
import csv
import gzip
import sys
from datetime import datetime
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_ROOT = REPO_ROOT / "data" / "audit"


# ── Order-event lifecycle priority for sorting ────────────────────────────
# Lower number = earlier in the lifecycle = appears earlier in the combined
# file when ticker + (event_priority) tie-break against timestamp.
EVENT_PRIORITY = {
    "SUBMITTED": 0,
    "BRACKET_SUBMITTED": 1,
    "PARTIAL_FILL": 2,
    "FILLED": 3,
    "CHILD_STOP_MODIFIED": 4,
    "CHILD_STOP_MODIFY_FAILED": 5,
    "BRACKET_CHILD_CANCELLED": 6,
    "STOP_LOSS_MARKET_FALLBACK": 7,
    "ORPHAN_CANCEL_VERIFY_TIMEOUT": 8,
    "PHANTOM_SELL_REJECTED": 9,
    "SHORTING_PREVENTED": 10,
    "CIRCUIT_BREAK": 11,
    "CANCELLED": 12,
    "REJECTED": 13,
}
DEFAULT_EVENT_PRIORITY = 99  # unknown events sort after everything known


# ── CLI helpers ───────────────────────────────────────────────────────────
def parse_date_arg(s: str) -> str:
    """Accept 'today', YYYY-MM-DD, or YYYYMMDD. Return canonical YYYYMMDD."""
    s = s.strip()
    if s.lower() == "today":
        return datetime.now().strftime("%Y%m%d")
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")
    if len(s) == 8 and s.isdigit():
        return s
    raise ValueError(
        f"Unrecognized date format: {s!r}. Use YYYY-MM-DD, YYYYMMDD, or 'today'."
    )


# ── I/O helpers ───────────────────────────────────────────────────────────
def open_csv_text(path: Path):
    """Open path as a text-mode CSV stream. Transparent .csv / .csv.gz."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", newline="", encoding="utf-8", errors="replace")
    return open(path, "r", newline="", encoding="utf-8", errors="replace")


def find_audit_csv(ticker_dir: Path, name: str):
    """Find <ticker_dir>/<name> as either plain .csv or .csv.gz.

    Returns the existing Path or None. Plain .csv preferred when both exist
    (.gz is the archived/older variant in this codebase).
    """
    plain = ticker_dir / name
    gz = ticker_dir / f"{name}.gz"
    if plain.exists():
        return plain
    if gz.exists():
        return gz
    return None


def read_ticker_csv(csv_path: Path, ticker: str):
    """Read one per-ticker CSV. Returns (fieldnames_list, list_of_row_dicts).

    Each row dict gets `ticker` injected so the combined file is
    self-identifying. Rows with malformed content are skipped with a warning.
    """
    fieldnames = []
    rows = []
    try:
        with open_csv_text(csv_path) as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            if "ticker" not in fieldnames:
                fieldnames.append("ticker")
            for row in reader:
                if row is None:
                    continue
                row["ticker"] = ticker
                rows.append(row)
    except Exception as e:
        print(f"[COMBINE] WARN: failed to read {csv_path}: {e}", file=sys.stderr)
    return fieldnames, rows


# ── Sort keys ─────────────────────────────────────────────────────────────
# Primary key everywhere is the ISO-8601 timestamp string. Lexicographic
# sort on ISO timestamps equals chronological sort (one of ISO-8601's
# defining properties), so we don't need to parse them into datetimes.
def order_sort_key(row: dict):
    """Composite key for order.csv: (timestamp, ticker A-Z, event lifecycle).

    Timestamp is the primary sort. Ticker is the tiebreaker when two
    events at different symbols share an identical timestamp. Event
    lifecycle is the final tiebreaker for the rare case of same
    timestamp AND same ticker.
    """
    return (
        row.get("timestamp") or "",
        (row.get("ticker") or "").upper(),
        EVENT_PRIORITY.get((row.get("event") or "").strip(), DEFAULT_EVENT_PRIORITY),
    )


def simple_sort_key(row: dict):
    """Composite key for state.csv / pnl.csv: (timestamp, ticker A-Z)."""
    return (
        row.get("timestamp") or "",
        (row.get("ticker") or "").upper(),
    )


# ── Core combiner ─────────────────────────────────────────────────────────
COMBINABLE = (
    # (filename, sort key function, label)
    ("order.csv", order_sort_key, "order"),
    ("state.csv", simple_sort_key, "state"),
    ("pnl.csv",   simple_sort_key, "pnl"),
)
# feed.csv is deliberately excluded: per-ticker feed CSVs are 5-50MB each,
# combining 6+ tickers would produce a single huge file with low utility.


def combine_for_date(date_folder: Path, out_subdir_name: str) -> int:
    """Combine all per-ticker CSVs under date_folder into out_subdir_name.

    Returns the number of output files written (0-3).
    Skips silently if the date folder has no ticker subdirectories.
    """
    if not date_folder.exists():
        print(f"[COMBINE] ERROR: date folder does not exist: {date_folder}",
              file=sys.stderr)
        return 0

    out_dir = date_folder / out_subdir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # Discover ticker folders. Skip anything starting with '_' so we don't
    # recurse into our own output folder on subsequent runs.
    ticker_dirs = sorted(
        [d for d in date_folder.iterdir()
         if d.is_dir() and not d.name.startswith("_")],
        key=lambda p: p.name.upper(),
    )
    if not ticker_dirs:
        print(f"[COMBINE] No ticker folders found in {date_folder}",
              file=sys.stderr)
        return 0

    print(f"[COMBINE] Date: {date_folder.name}")
    print(f"[COMBINE] Tickers: {', '.join(d.name for d in ticker_dirs)}")
    print(f"[COMBINE] Output:  {out_dir}")

    files_written = 0

    for filename, sort_key, label in COMBINABLE:
        # Collect rows + union of fieldnames across all tickers.
        # Different tickers may have different audit schema versions, so the
        # union preserves all columns; missing values become empty strings
        # via DictWriter's default behavior.
        union_fieldnames = []
        seen = set()
        all_rows = []

        for tdir in ticker_dirs:
            csv_path = find_audit_csv(tdir, filename)
            if csv_path is None:
                continue
            fieldnames, rows = read_ticker_csv(csv_path, tdir.name)
            for fn in fieldnames:
                if fn not in seen:
                    union_fieldnames.append(fn)
                    seen.add(fn)
            all_rows.extend(rows)

        if not all_rows:
            print(f"[COMBINE]   {label:6s}: no rows across any ticker — skipping")
            continue

        # Ensure 'ticker' is always the FIRST column for readability.
        if "ticker" in union_fieldnames:
            union_fieldnames.remove("ticker")
            union_fieldnames.insert(0, "ticker")

        # Sort with the appropriate composite key. Python's sort is stable,
        # so for keys that tie at every level, original file order is preserved.
        all_rows.sort(key=sort_key)

        out_path = out_dir / filename
        try:
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=union_fieldnames,
                    extrasaction="ignore",      # drop any unexpected keys
                    restval="",                 # empty string for missing values
                    quoting=csv.QUOTE_MINIMAL,
                )
                writer.writeheader()
                for row in all_rows:
                    writer.writerow(row)
        except Exception as e:
            print(f"[COMBINE]   {label:6s}: write FAILED: {e}", file=sys.stderr)
            continue

        print(f"[COMBINE]   {label:6s}: {len(all_rows):>6d} rows, "
              f"{len(union_fieldnames):>2d} cols → {out_path.name}")
        files_written += 1

    return files_written


def iter_all_dates():
    """Yield every date folder under data/audit/ that looks like YYYYMMDD."""
    if not AUDIT_ROOT.exists():
        return
    for d in sorted(AUDIT_ROOT.iterdir()):
        if d.is_dir() and len(d.name) == 8 and d.name.isdigit():
            yield d


# ── CLI ───────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description=("Combine per-ticker audit CSVs (order / state / pnl) into "
                     "a single _combined/ folder per date. Pure stdlib, no pandas."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--date", default="today",
        help="Session date: YYYY-MM-DD, YYYYMMDD, or 'today'. Default: today.",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Process every date folder under data/audit/ (ignores --date).",
    )
    p.add_argument(
        "--out-dir", default="_combined",
        help="Subfolder name under the date folder. Default: '_combined'.",
    )
    args = p.parse_args()

    if args.all:
        date_folders = list(iter_all_dates())
        if not date_folders:
            print(f"[COMBINE] No date folders found under {AUDIT_ROOT}",
                  file=sys.stderr)
            sys.exit(1)
        total_written = 0
        for df in date_folders:
            total_written += combine_for_date(df, args.out_dir)
            print("")  # blank line between dates
        print(f"[COMBINE] Done. Wrote {total_written} file(s) "
              f"across {len(date_folders)} date(s).")
    else:
        try:
            yyyymmdd = parse_date_arg(args.date)
        except ValueError as e:
            print(f"[COMBINE] ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        date_folder = AUDIT_ROOT / yyyymmdd
        files = combine_for_date(date_folder, args.out_dir)
        if files == 0:
            sys.exit(1)
        print(f"[COMBINE] Done. Wrote {files} file(s).")


if __name__ == "__main__":
    main()
