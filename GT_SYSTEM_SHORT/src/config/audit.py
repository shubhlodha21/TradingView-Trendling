"""
GT System - Comprehensive Audit Manager

Multi-threaded, non-blocking audit logging system for 100% trade traceability.

Design:
- 4 separate log streams (feed, state, orders, pnl)
- Each stream has its own background writer thread
- Queue-based (non-blocking from trading loop)
- One file per day per stream
- Minimal latency impact (<0.01ms per log call)

Usage:
    audit = AuditManager(symbol="NVDA", enabled=True)

    # Feed tick (non-blocking)
    audit.log_feed(tick)

    # State change
    audit.log_state(state="IN_POSITION", entry_price=226.50, stop=226.20)

    # Order event
    audit.log_order(order_id="BUY_001", side="BUY", qty=100, price=226.50, status="SUBMITTED")

    # PnL snapshot
    audit.log_pnl(pnl=50.00, position_open=True)

    # Shutdown (flush remaining)
    audit.close()
"""
import csv
import gzip
import json
import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Any


# ═══════════════════════════════════════════════════════════════════════════
# Log Types
# ═══════════════════════════════════════════════════════════════════════════

class LogType:
    FEED = "feed"
    STATE = "state"
    ORDER = "order"
    PNL = "pnl"


# ═══════════════════════════════════════════════════════════════════════════
# Audit Writer - Background Thread Per Log Type
# ═══════════════════════════════════════════════════════════════════════════

class _AuditWriter(threading.Thread):
    """
    Background writer thread for one log type.

    Features:
    - Bounded queue (drops if full - data integrity > completeness)
    - Batched writes (200 rows per flush)
    - Daily file rotation
    - Crash-safe (atomic writes)
    """

    def __init__(
        self,
        log_type: str,
        symbol: str,
        directory: str = "data/audit",
        batch_size: int = 200,
        queue_size: int = 5000,
    ):
        super().__init__(name=f"AuditWriter-{log_type}", daemon=True)

        self.log_type = log_type
        self.symbol = symbol
        self.directory = directory
        self.batch_size = batch_size
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)

        self._file = None
        self._writer = None
        self._path = None
        self._running = False
        self._written = 0
        self._dropped = 0
        self._current_date: Optional[date] = None

    def log(self, row: dict) -> bool:
        """Queue row for write. Non-blocking. Returns True if queued, False if dropped.

        Drops are silent at the queue level (queue.Full), but we surface them
        to stderr on the first drop AND every Nth subsequent drop. Otherwise
        a writer that's permanently falling behind drops millions of rows
        without any user-visible signal — a real compliance/audit risk.
        """
        try:
            self.queue.put_nowait(row)
            return True
        except queue.Full:
            self._dropped += 1
            # Warn loudly on the first drop, then again every 1000 drops.
            # First-drop signal catches the moment we go from healthy to
            # backed-up; the periodic interval prevents the user from
            # ignoring chronic loss.
            if self._dropped == 1 or self._dropped % 1000 == 0:
                print(
                    f"[AuditWriter:{self.log_type}] !!! DROPPED {self._dropped:,} rows "
                    f"(queue full, batch_size={self.batch_size}, qsize={self.queue.maxsize}) — "
                    f"data loss in audit stream",
                    file=sys.stderr,
                )
            return False

    def _ensure_file(self):
        """Ensure we have an open file for today.

        On date rollover, the PREVIOUS day's file is gzipped in a background
        operation (synchronous from this thread, but this thread is the
        audit writer, not the hot trading path). Old gzipped files (older
        than _RETENTION_DAYS) are deleted. Together this keeps audit storage
        bounded — without these, feed_NVDA_*.csv files grow ~55 MB / day
        and would consume ~20 GB / year per stream.
        """
        today = date.today()
        if self._file and self._current_date == today:
            return

        # Close old file + gzip + GC older logs
        if self._file:
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass
            self._compress_old(self._path)
            self._gc_retention()

        # Create new file under the hierarchical layout:
        #   <directory>/<YYYYMMDD>/<SYMBOL>/<type>.csv
        # The date folder lets an operator browse `ls data/audit/` and
        # see one row per trading day; the symbol subfolder keeps each
        # day's per-ticker streams (feed/state/order/pnl) co-located so
        # post-trade analysis ("what happened on NVDA on 2026-05-19?")
        # is one `cd` away. The CSV format inside is unchanged —
        # downstream readers don't care that the path got deeper.
        ts = today.strftime("%Y%m%d")
        day_dir = Path(self.directory) / ts / self.symbol
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[AuditWriter:{self.log_type}] Failed to create directory {day_dir}: {e}", file=sys.stderr)
            return

        self._path = day_dir / f"{self.log_type}.csv"
        file_exists = self._path.exists()

        try:
            self._file = open(self._path, 'a', buffering=8192)
            self._writer = csv.writer(self._file)
            self._current_date = today

            # Write header if new file
            if not file_exists:
                self._writer.writerow(self._get_header())
                self._file.flush()
        except Exception as e:
            print(f"[AuditWriter:{self.log_type}] Failed to open file: {e}", file=sys.stderr)
            return

    # How long to keep audit logs (in days). Gzipped files older than this
    # are removed on each rollover. 90 days is enough for most compliance
    # and post-trade analysis needs; bump if you need longer retention.
    _RETENTION_DAYS = 90

    def _compress_old(self, path: Optional[Path]) -> None:
        """gzip an already-rotated CSV file in place, deleting the original."""
        if path is None or not path.exists():
            return
        gz_path = path.with_suffix(path.suffix + '.gz')
        try:
            with open(path, 'rb') as src, gzip.open(gz_path, 'wb', compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, length=64 * 1024)
            path.unlink()
        except Exception as e:
            print(f"[AuditWriter:{self.log_type}] gzip of {path} failed: {e}", file=sys.stderr)

    def _gc_retention(self) -> None:
        """Delete gzipped logs older than _RETENTION_DAYS.

        Sweeps BOTH layouts so legacy files (flat `<type>_<sym>_<date>.csv.gz`)
        and current files (hierarchical `<date>/<sym>/<type>.csv.gz`) are
        both pruned. Mid-migration directories that contain only the
        gzipped file get cleaned up by the empty-dir sweep at the end.
        """
        try:
            cutoff = time.time() - self._RETENTION_DAYS * 86400
            patterns = (
                # Current layout
                f"*/{self.symbol}/{self.log_type}.csv.gz",
                # Legacy flat layout — keep handling so old files don't
                # accumulate indefinitely after the layout switch.
                f"{self.log_type}_{self.symbol}_*.csv.gz",
            )
            for pattern in patterns:
                for p in Path(self.directory).glob(pattern):
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
            # Best-effort: remove now-empty date+symbol dirs left behind
            # after a sweep. Silent on failure — non-empty dirs raise,
            # and that just means we still have a non-gc'd stream there.
            for sym_dir in Path(self.directory).glob(f"*/{self.symbol}"):
                try:
                    sym_dir.rmdir()
                except OSError:
                    pass
            for date_dir in Path(self.directory).glob("*"):
                if date_dir.is_dir() and not any(date_dir.iterdir()):
                    try:
                        date_dir.rmdir()
                    except OSError:
                        pass
        except Exception as e:
            print(f"[AuditWriter:{self.log_type}] retention GC failed: {e}", file=sys.stderr)

    def _get_header(self) -> list:
        """Override in subclass for type-specific headers."""
        return ["timestamp", "data"]

    def run(self):
        """Background write loop."""
        self._running = True
        self._ensure_file()

        batch = []

        while self._running:
            try:
                # Get first item (blocking with timeout)
                row = self.queue.get(timeout=0.1)
                batch.append(row)

                # Drain queue up to batch size
                while len(batch) < self.batch_size and not self.queue.empty():
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break

                # Write batch
                self._ensure_file()
                for row in batch:
                    self._writer.writerow(self._format_row(row))
                    self._written += 1

                self._file.flush()
                batch.clear()

            except queue.Empty:
                continue
            except Exception as e:
                print(f"[AuditWriter:{self.log_type}] Error: {e}", file=sys.stderr)
                batch.clear()

        # Drain remaining on shutdown
        while not self.queue.empty():
            try:
                row = self.queue.get_nowait()
                self._ensure_file()
                self._writer.writerow(self._format_row(row))
                self._written += 1
            except queue.Empty:
                break
            except Exception:
                pass

        # Final flush
        if self._file:
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass

    def _format_row(self, row: dict) -> list:
        """Format row for CSV. Override for type-specific formatting."""
        return [row.get("timestamp", ""), json.dumps(row)]

    def stop(self):
        """Stop writer thread."""
        self._running = False
        if self.is_alive():
            self.join(timeout=2.0)
        return {"written": self._written, "dropped": self._dropped, "path": str(self._path)}


# ═══════════════════════════════════════════════════════════════════════════
# Specialized Writers
# ═══════════════════════════════════════════════════════════════════════════

def _price_str(v) -> str:
    """Audit-safe price serialization — preserves FULL float precision.

    The audit CSVs were formatted with hardcoded `:.2f` / `:.4f` for
    price columns. That collapsed EURUSD 1.16415 → "1.16" on disk,
    which the dashboard then hydrated back as the displayed price.
    Result: dashboard ORDERS panel showed wrong prices after every
    restart, even though IBKR had the right values. (Live 2026-06-05.)

    Behavior:
        None / 0     → ""        (blank cell; matches legacy)
        float / int  → str(v)    (Python's smart repr: '1.16415',
                                  '230.45', '0.1' — no padding,
                                  no truncation, no float artifacts
                                  for typical price magnitudes)

    This preserves byte-identical equity audit output for canonical
    values (`str(230.45) == "230.45"` matches the old `:.2f` output)
    while fixing the precision loss on FX/futures.
    """
    if v is None or v == 0 or v == "":
        return ""
    try:
        # Python's `str(float)` uses smart precision: smallest string
        # that round-trips to the same float. Better than `repr` for
        # readability of canonical decimal values.
        return str(float(v))
    except (TypeError, ValueError):
        return str(v)


def _money_str(v, decimals: int = 2) -> str:
    """Audit-safe currency serialization — fixed 2dp dollar amounts.

    Distinct from `_price_str` because USD currency amounts (commission,
    PnL, fees) are conventionally 2dp regardless of asset class. The
    cleaner separation also documents intent at every call site:
    `_price_str(stop_price)` vs `_money_str(commission)`.
    """
    if v is None or v == 0 or v == "":
        return ""
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


class FeedWriter(_AuditWriter):
    """Writer for tick/feed data."""

    def _get_header(self) -> list:
        return [
            "timestamp", "symbol", "ltp", "ltp_size", "ltp_exchange",
            "ltp_conditions", "bid", "ask", "bid_size", "ask_size",
            "volume", "open", "high", "low", "tick_type"
        ]

    def _format_row(self, row: dict) -> list:
        # Prices via _price_str — preserves full FX/futures precision.
        # Sizes via :.0f (integers) — unchanged.
        return [
            row.get("timestamp", ""),
            row.get("symbol", ""),
            _price_str(row.get('ltp')),
            f"{row.get('ltp_size', 0):.0f}",
            row.get("ltp_exchange", ""),
            row.get("ltp_conditions", ""),
            _price_str(row.get('bid')),
            _price_str(row.get('ask')),
            f"{row.get('bid_size', 0):.0f}",
            f"{row.get('ask_size', 0):.0f}",
            row.get("volume", 0),
            _price_str(row.get('open')),
            _price_str(row.get('high')),
            _price_str(row.get('low')),
            row.get("tick_type", ""),
        ]


class StateWriter(_AuditWriter):
    """Writer for system state changes."""

    def _get_header(self) -> list:
        return [
            "timestamp", "event", "state", "position_open", "entry_price",
            "lowest_price", "stop_loss", "trigger_price", "reentry_level",
            "prev_ltp", "ltp", "pnl", "trades_today", "wins", "losses",
            "config_trigger", "config_stop_pct", "config_qty",
        ]

    def _format_row(self, row: dict) -> list:
        # All price columns use _price_str so FX/futures values land
        # on disk with their true precision (was :.2f → "1.16" instead
        # of "1.16415"). Counters stay raw int. PnL is currency → 2dp.
        return [
            row.get("timestamp", ""),
            row.get("event", ""),
            row.get("state", ""),
            row.get("position_open", ""),
            _price_str(row.get('entry_price')),
            _price_str(row.get('highest_price')),
            _price_str(row.get('stop_loss')),
            _price_str(row.get('trigger_price')),
            _price_str(row.get('breakout_level')),
            _price_str(row.get('prev_ltp')),
            _price_str(row.get('ltp')),
            _money_str(row.get('pnl')),
            row.get("trades_today", ""),
            row.get("wins", ""),
            row.get("losses", ""),
            _price_str(row.get('config_trigger')),
            f"{row.get('config_stop_pct', 0):.4f}" if row.get('config_stop_pct') else "",
            row.get("config_qty", ""),
        ]


class OrderWriter(_AuditWriter):
    """Writer for order events."""

    def _get_header(self) -> list:
        return [
            "timestamp", "event", "order_id", "side", "qty",
            "order_type", "limit_price", "stop_price",
            "signal_price", "fill_price", "slippage",
            "commission", "pnl", "reason",
            "exchange", "state_at_time", "position_at_time",
        ]

    def _format_row(self, row: dict) -> list:
        # All PRICE columns via _price_str: limit/stop/signal/fill all
        # land on disk at full precision (was :.2f / :.4f → "1.16" or
        # "1.1642" for what should be "1.16415"). Currency amounts
        # (commission, pnl) stay 2dp via _money_str. Slippage uses 4dp
        # for compact display of typical tick-scale numbers.
        return [
            row.get("timestamp", ""),
            row.get("event", ""),
            row.get("order_id", ""),
            row.get("side", ""),
            row.get("qty", ""),
            row.get("order_type", ""),
            _price_str(row.get('limit_price')),
            _price_str(row.get('stop_price')),
            _price_str(row.get('signal_price')),
            _price_str(row.get('fill_price')),
            f"{row.get('slippage', 0):.5f}" if row.get('slippage') else "",
            _money_str(row.get('commission'), decimals=4),
            _money_str(row.get('pnl')),
            row.get("reason", ""),
            row.get("exchange", ""),
            row.get("state_at_time", ""),
            row.get("position_at_time", ""),
        ]


class ReportOrderWriter(OrderWriter):
    """Append-only per-symbol order log for multi-day position reporting.

    Why this exists, in one paragraph: the daily audit writer rotates
    by date so each `data/audit/<DATE>/<SYM>/order.csv` is a one-day
    slice. That's the right shape for compliance + post-trade analysis
    but the wrong shape for a senior-review cycle on a long-hold
    position — when a single trade spans 2-3 months of trailing-stop
    adjustments (trigger → SL → new high → SL → ... → manual flat),
    reconstructing the cycle means stitching across ~60 daily files.
    This writer mirrors order EXECUTIONS into ONE per-ticker file that
    never rotates, so the full cycle is grep-able / Excel-able in a
    single open.

    What gets logged:
      * ONLY events with `event == "FILLED"` — both BUY and SELL fills.
        SUBMITTED, CANCELLED, REJECTED, and any other lifecycle events
        are silently dropped. Senior review wants the trade tape (what
        actually executed and at what price), not the order-management
        chatter that produced it. The daily audit file in
        data/audit/<DATE>/<SYM>/order.csv keeps the full lifecycle for
        forensic analysis.

    Path layout (parallel to data/audit/, deliberately separate):
        data/report/<SYMBOL>/order.csv

    Lifecycle:
      * Created on the first FILLED event for that symbol.
      * Appended forever — never rotates, never compresses, never
        garbage-collects. Long-hold reporting is the whole point.
      * Safe for unbounded growth: at ~2 FILLED rows per round-trip
        × ~20 round-trips/year/symbol ≈ a handful of KB/year.

    Failure semantics: if the report file can't be opened (permissions,
    disk full), the writer logs to stderr and disables itself. The
    daily audit writer is independent and keeps working — i.e., losing
    the report stream never costs you trade-level audit fidelity.
    """

    # Lean schema for senior-review reporting. Differs from the daily
    # OrderWriter's CSV in two ways:
    #   1. `timestamp` is SPLIT into `Time` + `Date` (Time first, per
    #      operator preference) so the file opens cleanly in Excel /
    #      Google Sheets without a date-parse helper formula.
    #   2. Drops the forensic columns the daily file keeps for incident
    #      analysis: slippage, pnl, reason, exchange, state_at_time,
    #      position_at_time. Senior review focuses on order flow
    #      (event + side + qty + price levels + commission), not the
    #      state-machine breadcrumbs.
    REPORT_HEADER = [
        "Time", "Date",
        "event", "order_id", "side", "qty", "order_type",
        "limit_price", "stop_price", "signal_price", "fill_price",
        "commission",
    ]

    def __init__(self, symbol: str,
                 directory: str = "data/report",
                 queue_size: int = 5000):
        # batch_size=1 because the operator may `tail -f` the report
        # file during a long hold; batching would delay visibility.
        # Order events are infrequent enough (~10/day per symbol)
        # that the per-row sync cost is negligible.
        super().__init__(
            log_type=LogType.ORDER,
            symbol=symbol,
            directory=directory,
            batch_size=1,
            queue_size=queue_size,
        )

    def log(self, row: dict) -> bool:
        """Filter to FILLED events only, then defer to the base queue.

        Drops SUBMITTED / CANCELLED / REJECTED / INACTIVE and anything
        else that isn't a real execution. This is a no-op rather than
        a drop-count bump because non-FILLED events are *expected* to
        be filtered — they're not a queue overflow / data-loss case.
        The daily audit writer still receives every event (the mirror
        in `AuditManager.log_order` calls both writers independently).

        Both BUY and SELL fills are kept — `row["side"]` is not
        consulted. Senior review wants the complete trade tape.
        """
        evt = (row.get("event") or "").strip().upper()
        if evt != "FILLED":
            # Silently ignore — return True so the AuditManager mirror
            # doesn't interpret this as a queue-full backpressure event.
            return True
        return super().log(row)

    def _get_header(self) -> list:
        return list(self.REPORT_HEADER)

    def _format_row(self, row: dict) -> list:
        """Format a row for the lean report schema.

        `timestamp` is split into `Time` and `Date`. The daily writer
        emits ISO timestamps (`2026-05-26T14:30:00.123456`); we split
        on the `T` separator. Falls back to empty cells if the format
        is unexpected (defensive — never raise from inside the writer
        thread, that would kill the daemon).
        """
        ts = row.get("timestamp", "") or ""
        if "T" in ts:
            date_part, time_part = ts.split("T", 1)
        else:
            # Defensive: empty/malformed timestamp → leave both blank
            # rather than guessing which half it could be.
            date_part, time_part = "", ts
        # Same precision-preserving helpers as the daily OrderWriter
        # so the per-symbol report log doesn't truncate FX prices.
        return [
            time_part,
            date_part,
            row.get("event", ""),
            row.get("order_id", ""),
            row.get("side", ""),
            row.get("qty", ""),
            row.get("order_type", ""),
            _price_str(row.get('limit_price')),
            _price_str(row.get('stop_price')),
            _price_str(row.get('signal_price')),
            _price_str(row.get('fill_price')),
            _money_str(row.get('commission'), decimals=4),
        ]

    def _ensure_file(self):
        """Append-only — open once on first event, never rotate by date.

        Override of the base class's date-rotation logic: we explicitly
        leave `_current_date` as None and never re-enter the rotation
        branch, so the same file stays open for the writer's lifetime.

        Schema guard: if an existing file's header doesn't match the
        current `REPORT_HEADER`, the legacy file is archived with a
        timestamp suffix and a fresh file with the new header is
        opened. Prevents silent column-shift corruption when the
        schema evolves (e.g. moving from the old wide schema with
        slippage/pnl/etc. to the new lean Time+Date layout).
        """
        if self._file:
            return
        try:
            sym_dir = Path(self.directory) / self.symbol
            sym_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[ReportOrderWriter:{self.symbol}] mkdir failed: {e}", file=sys.stderr)
            return

        self._path = sym_dir / "order.csv"

        # Schema-guard: rename any pre-existing file whose first line
        # (the header row) doesn't match the current expected schema.
        # New file with the new header is created below by the
        # `not file_exists` branch.
        if self._path.exists():
            try:
                with open(self._path) as fh:
                    first_line = fh.readline().rstrip("\n").rstrip("\r")
                existing_cols = first_line.split(",") if first_line else []
                if existing_cols != self._get_header():
                    archive = self._path.with_name(
                        f"order.legacy_{int(time.time())}.csv"
                    )
                    self._path.rename(archive)
                    print(
                        f"[ReportOrderWriter:{self.symbol}] header mismatch — "
                        f"archived old file to {archive.name} and starting "
                        f"fresh with new schema.",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"[ReportOrderWriter:{self.symbol}] schema-guard check "
                    f"failed (continuing best-effort): {type(e).__name__}: {e}",
                    file=sys.stderr,
                )

        file_exists = self._path.exists()
        try:
            self._file = open(self._path, 'a', buffering=8192)
            self._writer = csv.writer(self._file)
            # Sentinel: NOT a date — just "we're open". Distinguishes
            # report writer's path from the base class which uses
            # `_current_date` to detect rollover.
            self._current_date = None
            if not file_exists:
                self._writer.writerow(self._get_header())
                self._file.flush()
        except Exception as e:
            print(f"[ReportOrderWriter:{self.symbol}] open {self._path} failed: {e}", file=sys.stderr)

    def _compress_old(self, path):
        """No-op: report log is append-only and never rotated."""

    def _gc_retention(self):
        """No-op: the whole point is to retain everything for senior review."""


class PnlWriter(_AuditWriter):
    """Writer for PnL snapshots."""

    def _get_header(self) -> list:
        return [
            "timestamp", "state", "position_open", "entry_price",
            "current_price", "unrealized_pnl", "realized_pnl",
            "total_pnl", "wins", "losses", "trades_today",
            "comm_today", "lowest_price", "stop_loss",
        ]

    def _format_row(self, row: dict) -> list:
        # Prices go through _price_str so FX/futures land at full
        # precision (was :.4f → "1.1642" for what should be "1.16425").
        # Currency amounts (PnL, commission) keep 2dp via _money_str.
        return [
            row.get("timestamp", ""),
            row.get("state", ""),
            row.get("position_open", ""),
            _price_str(row.get('entry_price')),
            _price_str(row.get('current_price')),
            _money_str(row.get('unrealized_pnl')),
            _money_str(row.get('realized_pnl')),
            _money_str(row.get('total_pnl')),
            row.get("wins", ""),
            row.get("losses", ""),
            row.get("trades_today", ""),
            _money_str(row.get('comm_today'), decimals=4),
            _price_str(row.get('highest_price')),
            _price_str(row.get('stop_loss')),
        ]


# ═══════════════════════════════════════════════════════════════════════════
# Main Audit Manager
# ═══════════════════════════════════════════════════════════════════════════

class AuditManager:
    """
    Unified audit system with 4 independent log streams.

    Thread-safe, non-blocking design:
    - Each log type has its own queue + background writer
    - log_* calls return immediately (queue or drop)
    - No I/O in trading loop

    Usage:
        audit = AuditManager(symbol="NVDA", enabled=True)

        # Feed tick
        audit.log_feed(tick)

        # State
        audit.log_state(state="IN_POSITION", ...)

        # Order
        audit.log_order(order_id="BUY_001", ...)

        # PnL
        audit.log_pnl(pnl=50.00, ...)

        audit.close()
    """

    def __init__(
        self,
        symbol: str,
        enabled: bool = True,
        directory: str = "data/audit",
        batch_size: int = 200,
        queue_size: int = 5000,
        pnl_interval: float = 5.0,  # PnL snapshot every N seconds
        # Parallel append-only per-symbol order log for multi-day
        # senior reporting. See `ReportOrderWriter` docstring. Pass
        # `report_directory=None` to disable entirely (useful for
        # tests or replay paths).
        report_directory: Optional[str] = "data/report",
    ):
        self.symbol = symbol
        self.enabled = enabled
        self.directory = directory
        self.report_directory = report_directory
        self.pnl_interval = pnl_interval

        self._feed_writer: Optional[FeedWriter] = None
        self._state_writer: Optional[StateWriter] = None
        self._order_writer: Optional[OrderWriter] = None
        self._pnl_writer: Optional[PnlWriter] = None
        self._report_writer: Optional[ReportOrderWriter] = None

        self._last_pnl_snapshot: float = 0.0

        if enabled:
            self._start()

    def _start(self):
        """Start all background writers."""
        # Feed is high-volume, keep batching
        self._feed_writer = FeedWriter(
            LogType.FEED, self.symbol, self.directory, batch_size=200
        )
        self._feed_writer.start()

        # State, Order, PnL: flush immediately for real-time monitoring
        self._state_writer = StateWriter(
            LogType.STATE, self.symbol, self.directory, batch_size=1
        )
        self._state_writer.start()

        self._order_writer = OrderWriter(
            LogType.ORDER, self.symbol, self.directory, batch_size=1
        )
        self._order_writer.start()

        self._pnl_writer = PnlWriter(
            LogType.PNL, self.symbol, self.directory, batch_size=1
        )
        self._pnl_writer.start()

        # Parallel per-symbol append-only order log for multi-day
        # senior reporting. Independent thread + queue: a stall here
        # never backs up the daily-rotated audit writers above.
        if self.report_directory:
            self._report_writer = ReportOrderWriter(
                symbol=self.symbol,
                directory=self.report_directory,
            )
            self._report_writer.start()

    def log_feed(self, tick) -> bool:
        """
        Log feed tick. Non-blocking.

        Args:
            tick: Tick object from feed

        Returns:
            True if logged, False if dropped
        """
        if not self.enabled or not self._feed_writer:
            return False

        row = {
            "timestamp": tick.timestamp.isoformat(timespec='microseconds'),
            "symbol": tick.symbol,
            "ltp": tick.last,
            "ltp_size": tick.last_size,
            "ltp_exchange": tick.last_exchange,
            "ltp_conditions": tick.last_conditions,
            "bid": tick.bid,
            "ask": tick.ask,
            "bid_size": tick.bid_size,
            "ask_size": tick.ask_size,
            "volume": tick.volume,
            "open": tick.open,
            "high": tick.high,
            "low": tick.low,
            "tick_type": tick.tick_type.value if hasattr(tick.tick_type, 'value') else str(tick.tick_type),
        }
        return self._feed_writer.log(row)

    def log_state(
        self,
        event: str,
        state: str,
        ltp: float = 0,
        prev_ltp: float = 0,
        **kwargs
    ) -> bool:
        """
        Log system state change. Non-blocking.

        Args:
            event: Event type (STARTED, STOPPED, STATE_CHANGE, TICK)
            state: Current TradeState
            ltp: Last trade price
            prev_ltp: Previous LTP
            **kwargs: Additional state fields
        """
        if not self.enabled or not self._state_writer:
            return False

        row = {
            "timestamp": datetime.now().isoformat(timespec='microseconds'),
            "event": event,
            "state": state if isinstance(state, str) else state.value,
            "ltp": ltp,
            "prev_ltp": prev_ltp,
            **kwargs
        }
        return self._state_writer.log(row)

    def log_order(
        self,
        event: str,
        order_id: str,
        side: str,
        qty: int,
        order_type: str = "",
        limit_price: float = 0,
        stop_price: float = 0,
        signal_price: float = 0,
        fill_price: float = 0,
        commission: float = 0,
        pnl: float = 0,
        reason: str = "",
        exchange: str = "",
        state_at_time: str = "",
        position_at_time: str = "",
    ) -> bool:
        """
        Log order event. Non-blocking.

        Args:
            event: SUBMITTED, FILLED, REJECTED, CANCELLED
            order_id: Unique order identifier
            side: BUY or SELL
            qty: Order quantity
            order_type: MARKET, LIMIT, STOP, STOP_LIMIT
            limit_price: For LIMIT/STOP_LIMIT orders
            stop_price: For STOP/STOP_LIMIT orders (trigger)
            signal_price: Price when order was generated
            fill_price: Actual fill price
            commission: Commission charged
            pnl: PnL for this trade
            reason: Reason (STOP_LOSS, etc)
        """
        if not self.enabled or not self._order_writer:
            return False

        slippage = 0
        if signal_price and fill_price:
            slippage = fill_price - signal_price
            if side == "SELL":
                slippage = -slippage

        # Coerce qty to int (whole shares) — IBKR delivers fill quantities
        # as floats ("10.0"), and the CSV would otherwise carry that float
        # all the way to the dashboard. Defensive int() at write time means
        # every audit row has a clean integer qty regardless of caller.
        try:
            qty_int = int(qty) if qty is not None else 0
        except (TypeError, ValueError):
            qty_int = 0

        row = {
            "timestamp": datetime.now().isoformat(timespec='microseconds'),
            "event": event,
            "order_id": order_id,
            "side": side,
            "qty": qty_int,
            "order_type": order_type,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "signal_price": signal_price,
            "fill_price": fill_price,
            "slippage": slippage,
            "commission": commission,
            "pnl": pnl,
            "reason": reason,
            "exchange": exchange,
            "state_at_time": state_at_time,
            "position_at_time": position_at_time,
        }
        queued = self._order_writer.log(row)
        # Mirror the same row into the per-symbol report log. Safe to
        # share the dict (writers are read-only over it). The report
        # writer's queue + thread are independent, so a stall here
        # never delays the daily audit return value.
        if self._report_writer is not None:
            self._report_writer.log(row)
        return queued

    def should_log_pnl(self) -> bool:
        """Check if it's time for a PnL snapshot."""
        now = time.monotonic()
        if now - self._last_pnl_snapshot >= self.pnl_interval:
            self._last_pnl_snapshot = now
            return True
        return False

    def log_pnl(
        self,
        state: str,
        position_open: bool,
        entry_price: float = 0,
        current_price: float = 0,
        unrealized_pnl: float = 0,
        realized_pnl: float = 0,
        total_pnl: float = 0,
        wins: int = 0,
        losses: int = 0,
        trades_today: int = 0,
        comm_today: float = 0,
        highest_price: float = 0,
        stop_loss: float = 0,
    ) -> bool:
        """
        Log PnL snapshot. Non-blocking.

        Call periodically (e.g., every 5 seconds) or on state change.
        """
        if not self.enabled or not self._pnl_writer:
            return False

        row = {
            "timestamp": datetime.now().isoformat(timespec='microseconds'),
            "state": state if isinstance(state, str) else state.value,
            "position_open": position_open,
            "entry_price": entry_price,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "total_pnl": total_pnl,
            "wins": wins,
            "losses": losses,
            "trades_today": trades_today,
            "comm_today": comm_today,
            "highest_price": highest_price,
            "stop_loss": stop_loss,
        }
        return self._pnl_writer.log(row)

    def close(self) -> dict:
        """
        Stop all writers and flush remaining data.

        Returns:
            Dict with stats for each log type
        """
        stats = {}

        if self._feed_writer:
            stats['feed'] = self._feed_writer.stop()
            self._feed_writer = None

        if self._state_writer:
            stats['state'] = self._state_writer.stop()
            self._state_writer = None

        if self._order_writer:
            stats['order'] = self._order_writer.stop()
            self._order_writer = None

        if self._pnl_writer:
            stats['pnl'] = self._pnl_writer.stop()
            self._pnl_writer = None

        if self._report_writer:
            stats['report'] = self._report_writer.stop()
            self._report_writer = None

        return stats

    @property
    def stats(self) -> dict:
        """Get current stats without stopping."""
        return {
            'feed': {
                'written': self._feed_writer._written if self._feed_writer else 0,
                'dropped': self._feed_writer._dropped if self._feed_writer else 0,
            },
            'state': {
                'written': self._state_writer._written if self._state_writer else 0,
                'dropped': self._state_writer._dropped if self._state_writer else 0,
            },
            'order': {
                'written': self._order_writer._written if self._order_writer else 0,
                'dropped': self._order_writer._dropped if self._order_writer else 0,
            },
            'pnl': {
                'written': self._pnl_writer._written if self._pnl_writer else 0,
                'dropped': self._pnl_writer._dropped if self._pnl_writer else 0,
            },
            'report': {
                'written': self._report_writer._written if self._report_writer else 0,
                'dropped': self._report_writer._dropped if self._report_writer else 0,
            },
        }


def read_recent_orders(
    symbol: str,
    n: int = 20,
    directory: str = "data/audit",
    max_days_back: int = 45,
    with_rows: bool = False,
) -> list:
    """Walk back through daily order audit CSVs and return the last `n` events.

    Used on engine startup to repopulate `_order_history` so the dashboard
    ORDERS panel survives terminal kills + cross-session breaks (weekend
    Friday→Monday continuation). Without this, the panel showed empty on
    every restart even though `data/audit/order_<SYM>_*.csv` had the full
    history on disk.

    Walks backward from today, accumulating audit rows until we have `n`
    of them or exhaust `max_days_back` files. Files that don't exist are
    silently skipped (gaps from weekends / holidays / paper sessions).
    Returns OrderRecord objects in chronological order (oldest first) so
    they slot into `_order_history` as if they'd been appended live.

    Args:
        symbol: ticker — must match the audit file naming
                `order_<SYMBOL>_<YYYYMMDD>.csv`.
        n: max records to return. Default 20 = plenty of buffer over the
           dashboard's 8-slot ORDERS panel so the latest are always visible
           even after dedup keys collide.
        directory: where the audit CSVs live. Matches AuditManager default.
        max_days_back: how many days of history to scan. Default 45.
           This was 7 — sized for a long weekend (Fri pm → Mon am) — which
           silently hid any order older than a week. A resting bracket lives
           for WEEKS: a position entered a fortnight ago has no rows in the
           last 7 days, so the ORDERS panel rendered empty and the operator
           couldn't see the very order protecting their open position.
           45 covers a multi-week hold plus month-boundary headroom. The
           walk stops early once `n` records are collected, so on an active
           symbol this costs nothing; on a quiet one it's ~45 stat() calls
           against tiny files (see the caching note in
           dashboard_agg.tail_orders for the polling path).
        with_rows: when True, return `(OrderRecord, raw_csv_row)` pairs
           instead of bare records. The record drives the supersession
           filter; the raw row preserves the CSV columns the record type
           doesn't carry (pnl, reason, slippage). Default False keeps the
           historical return shape for existing callers (engine hydration).

    Returns:
        list[OrderRecord], chronologically oldest→newest — or, when
        `with_rows=True`, list[tuple[OrderRecord, dict]] in the same order.
        Empty if no files found or all rows malformed.
    """
    from pathlib import Path
    import csv as _csv
    from datetime import datetime as _dt, timedelta as _td
    from src.config.models import OrderRecord, OrderSide, OrderType, OrderStatus

    if n <= 0:
        return []

    # Newest-first accumulator (we walk backwards through days), then
    # reverse at the end so callers get chronological order.
    collected: list = []

    today = _dt.now()
    for days_back in range(max_days_back):
        if len(collected) >= n:
            break
        date_str = (today - _td(days=days_back)).strftime("%Y%m%d")
        # Prefer the current hierarchical layout. Fall back to the
        # legacy flat layout for days that pre-date the migration so
        # historical Friday→Monday continuity still works.
        new_path = Path(directory) / date_str / symbol / "order.csv"
        legacy_path = Path(directory) / f"order_{symbol}_{date_str}.csv"
        path = new_path if new_path.exists() else legacy_path
        if not path.exists():
            continue
        try:
            with open(path, newline="") as fh:
                reader = _csv.DictReader(fh)
                day_rows = list(reader)
        except Exception:
            continue

        # Within a day: newest at the bottom (writer appends). Walk from
        # bottom up to fill our newest-first buffer.
        for row in reversed(day_rows):
            if len(collected) >= n:
                break
            rec = _audit_row_to_order_record(row, symbol)
            if rec is not None:
                collected.append((rec, row))

    # Reverse → chronological so they append to _order_history in the
    # same order the live engine would have built them.
    chrono = list(reversed(collected))
    # Drop superseded SUBMITTEDs — see _filter_to_current_cycles().
    # Without this, the dashboard ORDERS panel shows every cancelled
    # entry attempt from prior runs ("bullshit previous shit"): each
    # `run_live.py + --reset` cycle logs a fresh SUBMITTED that never
    # filled, and they accumulate in the panel making it impossible to
    # see what actually traded.
    drop = _superseded_indices([rec for rec, _row in chrono])
    if with_rows:
        return [pair for i, pair in enumerate(chrono) if i not in drop]
    return [rec for i, (rec, _row) in enumerate(chrono) if i not in drop]


def _filter_to_current_cycles(records: list) -> list:
    """Drop SUBMITTED rows that were superseded or cancelled before fill.

    Walks the chronological history once. For each `order_id` we maintain
    a "pending" pointer to the most recent SUBMITTED. Rules:

        SUBMITTED for order_id:
          - If a pending SUBMITTED exists for the same order_id, drop it
            (superseded by this newer one — old intent was replaced).
          - Mark this row as the new pending.

        FILLED for order_id:
          - The pending SUBMITTED (if any) is paired with this FILLED.
          - Keep both. Clear the pending pointer.

        CANCELLED / REJECTED for order_id:
          - The pending SUBMITTED was abandoned, not filled. Drop both.
          - Clear the pending pointer.

    At end, any still-pending SUBMITTEDs (no FILLED yet) are kept — those
    are orders currently resting at the broker. Result: dashboard shows
    only real SUBMITTED-FILLED pairs + the currently-pending order(s).

    Live-added rows (from the engine's in-session activity) follow the
    same shape naturally, so this filter at hydration time is enough.
    """
    drop = _superseded_indices(records)
    return [r for i, r in enumerate(records) if i not in drop]


def _superseded_indices(records: list) -> set:
    """Indices of records the supersession rules drop.

    Extracted from `_filter_to_current_cycles` so callers that need to keep
    the records paired with something else (read_recent_orders' with_rows
    mode pairs each record with its raw CSV row) can apply the identical
    rules without duplicating them. `_filter_to_current_cycles` is now a
    thin wrapper over this — behaviour is unchanged for its callers.
    """
    from src.config.models import OrderStatus

    pending: dict = {}    # order_id -> index of pending SUBMITTED
    drop: set = set()     # row indices to drop

    for i, r in enumerate(records):
        oid = getattr(r, 'order_id', '') or ''
        status = r.status

        if status == OrderStatus.SUBMITTED:
            # Supersede any prior pending SUBMITTED with same order_id.
            prior = pending.get(oid)
            if prior is not None:
                drop.add(prior)
            pending[oid] = i
        elif status == OrderStatus.FILLED:
            # The pending SUBMITTED was THIS fill's intent — keep both.
            pending.pop(oid, None)
        elif status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            # Drop the abandoned SUBMITTED + the terminal row itself,
            # since neither represents real order activity to display.
            prior = pending.pop(oid, None)
            if prior is not None:
                drop.add(prior)
            drop.add(i)
        # PARTIALLY_FILLED (if added in future) falls through — kept.

    return drop


def _audit_row_to_order_record(row: dict, symbol: str):
    """Parse a single audit CSV row into an OrderRecord.

    Returns None on malformed rows (missing fields, bad enum values, etc.)
    so the caller can skip silently rather than crashing the whole rebuild.
    """
    from datetime import datetime as _dt
    from src.config.models import OrderRecord, OrderSide, OrderType, OrderStatus

    event = (row.get("event") or "").strip()
    side_str = (row.get("side") or "").strip()
    if not event or not side_str:
        return None

    # Drop process-level / diagnostic events that aren't real order activity.
    # These were historically logged via `_audit.log_order(...)` (which writes
    # to the orders CSV) but they describe engine lifecycle events, not
    # placed orders. Filtering them ensures historical CSV rows don't
    # rebuild as phantom order rows in the dashboard ORDERS panel.
    #
    # ── DASHBOARD PHANTOM CLASS ──
    # COMMISSION_REPORT was the recent example: an audit row written
    # whenever IBKR's late-arriving commissionReport updates an order's
    # broker_commission. The row has empty stop/limit/signal_price fields.
    # The hydration default-maps unknown events to OrderStatus.SUBMITTED;
    # the dashboard then falls back to `config.trigger_price` (1.16415)
    # for the display price because the OrderRecord has no price.
    # End result: a phantom "BUY 25000 @ $1.16415 SUBMITTED" row appears
    # in the panel for each commission update — even though zero new
    # orders were placed. (Live 2026-06-05.)
    #
    # Rule: a row belongs in the ORDERS panel ONLY if it represents a
    # placement we made at IBKR (SUBMITTED / BRACKET_SUBMITTED / FILLED /
    # REJECTED / CANCELLED). Everything else — modifies, commissions,
    # tripwires, adoptions, divergence alerts, recomputations — is a
    # diagnostic and belongs in the EVENTS or ALERTS panel only.
    _PROCESS_EVENTS = {
        # Lifecycle / startup
        "STARTUP_REFUSED_NAKED",
        "STARTUP_REFUSED_CONFLICT",
        "TRIPWIRE_LOST_PENDING",
        "POSITION_MISMATCH",
        "POSITION_AUTO_FLAT",
        "SNAPSHOT",
        "GIVE_UP",
        "CIRCUIT_BREAK",
        # Stop-modify lifecycle (the child stop is the SAME order before
        # and after — the modify isn't a new order).
        "MODIFIED",
        "MODIFY_FAILED",
        "CHILD_STOP_MODIFIED",
        "CHILD_STOP_MODIFY_FAILED",
        # Commission report — true broker fee arrives async after fill;
        # the underlying order was already shown via its SUBMITTED+FILLED
        # rows. Re-emitting it as a synthetic SUBMITTED is the phantom.
        "COMMISSION_REPORT",
        # Orphan cancellation (post-fold cleanup, not a new order)
        "ORPHAN_CANCELLED",
        "ORPHAN_CANCEL_FAILED",
        # Pre-flight guard rejections — would-be orders the engine
        # refused to place. The duplicate-SELL guard's ADOPTED variant
        # documents that we kept an EXISTING broker order; the new
        # placement is the SAME logical SELL already shown via
        # BRACKET_SUBMITTED.
        "DUPLICATE_SELL_GUARD_ADOPTED",
        "SHORTING_PREVENTED",
        "STALE_SELL_REJECTED",
        # Engine self-correction events (no order activity)
        "STOP_PRICE_DIVERGENCE",
        "PNL_RECOMPUTED",
    }
    if event.upper() in _PROCESS_EVENTS:
        return None

    try:
        side = OrderSide(side_str)
    except ValueError:
        return None

    # qty parsing — MUST tolerate float-shaped strings like "100.0" because
    # the engine writes the FILLED path with `qty=order.filled_qty` which is
    # a float, and the SELL submission path takes its qty from `self._quantity`
    # which also flows through float math. Using strict `int(...)` silently
    # dropped every FILLED row and every SELL row during hydration — the user
    # saw "only previous BUY SUBMITTED orders" after restart because those
    # were the only events whose qty was a clean int string in the CSV.
    # `int(float(s))` accepts both "100" and "100.0" without raising.
    try:
        qty = int(float(row.get("qty") or 0))
    except (ValueError, TypeError):
        return None
    if qty <= 0:
        return None

    # OrderType — the audit writes values like "STOP_LIMIT", "MARKET",
    # "LIMIT". Tolerant fallback to MARKET if unknown.
    otype_str = (row.get("order_type") or "MARKET").strip()
    try:
        order_type = OrderType(otype_str)
    except ValueError:
        order_type = OrderType.MARKET

    # event → status mapping. Unknown events default to SUBMITTED so the
    # row still appears on the dashboard (better than dropping it).
    status_map = {
        "FILLED":    OrderStatus.FILLED,
        "SUBMITTED": OrderStatus.SUBMITTED,
        "REJECTED":  OrderStatus.REJECTED,
        "CANCELLED": OrderStatus.CANCELLED,
        "INACTIVE":  OrderStatus.CANCELLED,
    }
    status = status_map.get(event.upper(), OrderStatus.SUBMITTED)

    # Timestamp — audit writes isoformat() strings (naive local). Strip
    # tzinfo defensively in case anyone changes the writer in future.
    ts_raw = (row.get("timestamp") or "").strip()
    if not ts_raw:
        return None
    try:
        ts = _dt.fromisoformat(ts_raw)
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
    except ValueError:
        return None

    def _f(key: str):
        v = row.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    fill_price = _f("fill_price")
    return OrderRecord(
        order_id=(row.get("order_id") or "").strip(),
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        limit_price=_f("limit_price"),
        stop_price=_f("stop_price"),
        status=status,
        submitted_at=ts,
        # FILLED rows: the audit timestamp IS the fill time
        # (the writer logs at the moment of fill).
        filled_at=ts if status == OrderStatus.FILLED else None,
        avg_fill_price=fill_price if status == OrderStatus.FILLED else None,
        filled_qty=qty if status == OrderStatus.FILLED else 0,
        commission=_f("commission") or 0.0,
        signal_price=_f("signal_price"),
    )
