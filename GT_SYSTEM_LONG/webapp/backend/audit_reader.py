"""
Reads the bot's audit streams + alerts feed into typed records the
frontend can render.

What lives on disk:
  data/audit/order_<SYM>_<YYYYMMDD>.csv   — order events (full lifecycle)
  data/audit/state_<SYM>_<YYYYMMDD>.csv   — state machine transitions
  data/audit/pnl_<SYM>_<YYYYMMDD>.csv     — P&L snapshots (~5s cadence)
  data/audit/feed_<SYM>_<YYYYMMDD>.csv    — every tick (NOT exposed: too
                                            large, dedicated bars endpoint
                                            later)
  data/alerts/alerts_<YYYYMMDD>.jsonl     — global alerts (cross-symbol)

Walk-back semantics: every reader walks N daily files backward from
today, accumulating rows until it has `limit` or runs out of days. This
gives Monday-morning continuity for Friday's trades without an extra
"merged history" file.

Cache: each path is cached by mtime so repeated polls of unchanged
files cost ~5 µs (one stat() call). The cache keys on absolute path
so symbol/date doesn't matter — same file, same cached parse.

We deliberately DON'T import from src/strategy/ or src/execution/.
The audit format is the contract; tracking it via files keeps the
webapp insulated from internal refactors.
"""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional


# ── Defaults ────────────────────────────────────────────────────────
AUDIT_DIR = os.environ.get("GT_WEBAPP_AUDIT_DIR", "data/audit")
ALERTS_DIR = os.environ.get("GT_WEBAPP_ALERTS_DIR", "data/alerts")
DAYS_BACK = int(os.environ.get("GT_WEBAPP_DAYS_BACK", "7"))


@dataclass(slots=True)
class _Cached:
    """One parsed file + the mtime it was parsed at."""
    rows: list = field(default_factory=list)
    mtime: float = 0.0


class AuditReader:
    """Polls audit CSVs + alerts JSONL into the frontend. Stateless from
    the caller's POV: every method returns a fresh list. Internal mtime
    cache is opaque optimization."""

    __slots__ = ("_audit_dir", "_alerts_dir", "_days_back",
                 "_order_cache", "_state_cache", "_pnl_cache", "_alert_cache")

    def __init__(self, cwd: Optional[Path] = None,
                 audit_dir: Optional[str] = None,
                 alerts_dir: Optional[str] = None,
                 days_back: int = DAYS_BACK) -> None:
        base = Path(cwd) if cwd else Path(os.environ.get("GT_WEBAPP_CWD", "."))
        self._audit_dir = base / (audit_dir or AUDIT_DIR)
        self._alerts_dir = base / (alerts_dir or ALERTS_DIR)
        self._days_back = max(1, days_back)
        self._order_cache: dict[str, _Cached] = {}
        self._state_cache: dict[str, _Cached] = {}
        self._pnl_cache: dict[str, _Cached] = {}
        self._alert_cache: dict[str, _Cached] = {}

    # ── Order events (with cycle-aware filtering) ──────────────────
    def orders(self, symbol: str, limit: int = 100) -> list[dict]:
        """Walk back daily order CSVs, return chronological newest-last.

        Applies the same superseded-SUBMITTED filter the bot uses
        internally (see src/config/audit.py `_filter_to_current_cycles`)
        so the dashboard doesn't show cancelled entry attempts as if
        they were real orders. Implemented inline to avoid the import
        boundary into src/."""
        rows = self._walk_back("order", symbol, self._order_cache, limit * 3)
        # Newest-first → walk that way to dedup
        rows = list(reversed(rows))
        cleaned = _filter_orders(rows)
        # cleaned is in newest→oldest; flip back to chronological
        chrono = list(reversed(cleaned))
        return chrono[-limit:] if limit > 0 else chrono

    def open_orders(self, symbol: str) -> list[dict]:
        """Subset of `orders()`: rows currently working at the broker —
        SUBMITTED without a matching FILLED/CANCELLED/REJECTED. The
        `_filter_orders` pass leaves these as the lone SUBMITTED for
        their order_id when no terminal event follows."""
        all_rows = self.orders(symbol, limit=200)
        # Group by order_id, keep only those whose latest event is SUBMITTED
        latest: dict[str, dict] = {}
        for r in all_rows:
            oid = (r.get("order_id") or "").strip()
            if not oid:
                continue
            latest[oid] = r
        return [r for r in latest.values()
                if (r.get("event") or "").upper() == "SUBMITTED"]

    # ── State machine transitions ──────────────────────────────────
    def state_transitions(self, symbol: str, limit: int = 50) -> list[dict]:
        rows = self._walk_back("state", symbol, self._state_cache, limit)
        return rows[-limit:] if limit > 0 else rows

    # ── P&L snapshots (for equity curve) ───────────────────────────
    def pnl_snapshots(self, symbol: str, limit: int = 2000) -> list[dict]:
        return self._walk_back("pnl", symbol, self._pnl_cache, limit)

    # ── Alerts (global, JSONL) ─────────────────────────────────────
    def alerts(self, limit: int = 200,
               severity: Optional[str] = None,
               symbol: Optional[str] = None) -> list[dict]:
        """Walk back daily alerts_<DATE>.jsonl files. Optional filters."""
        collected: list[dict] = []
        today = date.today()
        for d in range(self._days_back):
            day = today - timedelta(days=d)
            path = self._alerts_dir / f"alerts_{day:%Y%m%d}.jsonl"
            if not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cache = self._alert_cache.setdefault(str(path), _Cached())
            if mtime != cache.mtime:
                cache.rows = _parse_jsonl(path)
                cache.mtime = mtime
            # Newest-last within each file (writer appends), so iterate
            # in reverse to fill newest-first.
            for row in reversed(cache.rows):
                if severity and (row.get("severity") or "") != severity:
                    continue
                if symbol:
                    # Symbol filter — look in context dict for `ticker`.
                    ctx = row.get("context") or {}
                    if ctx.get("ticker") != symbol:
                        continue
                collected.append(row)
                if len(collected) >= limit:
                    break
            if len(collected) >= limit:
                break
        # Caller expects chronological (oldest → newest), so flip.
        return list(reversed(collected))

    def alert_counts(self) -> dict[str, int]:
        """Per-severity counts for today's alerts. Used by the top-bar
        badge to show the number + severity color of pending alerts."""
        out = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        today = date.today()
        path = self._alerts_dir / f"alerts_{today:%Y%m%d}.jsonl"
        if not path.exists():
            return out
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return out
        cache = self._alert_cache.setdefault(str(path), _Cached())
        if mtime != cache.mtime:
            cache.rows = _parse_jsonl(path)
            cache.mtime = mtime
        for row in cache.rows:
            sev = row.get("severity") or "LOW"
            if sev in out:
                out[sev] += 1
        return out

    # ── Internal walk-back primitive ───────────────────────────────
    def _walk_back(self, stream: str, symbol: str,
                   cache: dict[str, _Cached], target: int) -> list[dict]:
        """Generic walker for the per-symbol per-day CSVs. Returns rows
        in chronological order (oldest → newest), at most ~ `target`.

        Layout precedence:
          1. `<audit_dir>/<YYYYMMDD>/<SYMBOL>/<stream>.csv`   (current)
          2. `<audit_dir>/<stream>_<SYMBOL>_<YYYYMMDD>.csv`   (legacy)
        First match wins per-day so a partial migration still reads
        correctly across the boundary.
        """
        collected: list[dict] = []
        today = date.today()
        for d in range(self._days_back):
            day = today - timedelta(days=d)
            new_path = self._audit_dir / f"{day:%Y%m%d}" / symbol / f"{stream}.csv"
            legacy_path = self._audit_dir / f"{stream}_{symbol}_{day:%Y%m%d}.csv"
            path = new_path if new_path.exists() else legacy_path
            if not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            c = cache.setdefault(str(path), _Cached())
            if mtime != c.mtime:
                c.rows = _parse_csv(path)
                c.mtime = mtime
            # Prepend earlier days' rows so final order is chronological.
            collected = c.rows + collected
            if len(collected) >= target:
                break
        return collected


# ── Parsers (file-level helpers, free functions for testability) ─────
def _parse_csv(path: Path) -> list[dict]:
    """Tolerant CSV reader: skips malformed rows, never raises."""
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            return [dict(r) for r in reader]
    except (OSError, csv.Error):
        return []


def _parse_jsonl(path: Path) -> list[dict]:
    """One JSON object per line; bad lines silently skipped."""
    out: list[dict] = []
    try:
        with open(path, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _filter_orders(rows_newest_first: list[dict]) -> list[dict]:
    """Drop SUBMITTED rows that were superseded or cancelled before fill.

    Mirrors src/config/audit.py `_filter_to_current_cycles` so the
    dashboard sees the same view the engine considers "current cycles":
    SUBMITTED-FILLED pairs kept; abandoned SUBMITTEDs and their
    CANCELLED/REJECTED rows dropped; currently-pending SUBMITTEDs kept.

    Walks newest-first because the writer appends chronologically;
    iterating reverse means the "latest event for each order_id" is
    the first one seen.
    """
    seen_terminal: set[str] = set()   # order_ids with a terminal event already seen
    pending_index: dict[str, int] = {}   # order_id → index of newest SUBMITTED
    drop: set[int] = set()
    rows = rows_newest_first

    for i, r in enumerate(rows):
        oid = (r.get("order_id") or "").strip()
        if not oid:
            continue
        evt = (r.get("event") or "").upper()
        if evt in ("FILLED",):
            seen_terminal.add(oid)
            # Discard any older SUBMITTED tracked as pending — its work is done.
            pending_index.pop(oid, None)
        elif evt in ("CANCELLED", "REJECTED", "INACTIVE"):
            # Drop the CANCELLED row + its superseded SUBMITTED (if any).
            drop.add(i)
            prior = pending_index.pop(oid, None)
            if prior is not None:
                drop.add(prior)
            seen_terminal.add(oid)
        elif evt == "SUBMITTED":
            if oid in seen_terminal:
                # A newer terminal event already won — this SUBMITTED is
                # historic but cycle was completed; keep it (so the pair
                # SUBMITTED+FILLED renders together in History).
                continue
            existing = pending_index.get(oid)
            if existing is not None:
                # Older duplicate — drop it (superseded).
                drop.add(existing)
            pending_index[oid] = i
    return [r for i, r in enumerate(rows) if i not in drop]
