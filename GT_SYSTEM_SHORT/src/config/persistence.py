import csv
import json
import os
import queue
import sys
import threading
from datetime import datetime
from typing import Optional


class StateStore:
    """File-based state persistence with a background writer thread.

    Why a thread: save() is called from the asyncio hot path (engine state
    transitions, new-high updates). Doing json.dump + os.replace inline
    blocks the event loop on disk I/O, which spikes order-placement latency
    whenever the disk has any contention. The audit subsystem uses the same
    pattern — keep the hot path lock-free, drain the queue on a worker.

    Queue depth is intentionally small (8). State is a single snapshot
    object: if we get behind by more than 8 writes, dropping stale ones
    is the right behavior (the next save will carry the latest state).
    """

    __slots__ = ('path', '_queue', '_thread', '_running', '_dropped', '_written', '_last_state')

    def __init__(self, path: str = ".gt_state.json"):
        self.path = path
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._running = True
        self._dropped = 0
        self._written = 0
        # Keep a copy of the most recent state so callers can read what's
        # in flight without racing the writer thread.
        self._last_state: Optional[dict] = None
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="StateWriter",
            daemon=True,
        )
        self._thread.start()

    def save(self, state: dict) -> bool:
        """Queue a state snapshot for the writer thread. Non-blocking.

        If the queue is full, the OLDEST pending write is discarded and
        the new state takes its slot — we always want disk to converge
        on the latest snapshot, not stale ones.
        """
        self._last_state = state
        try:
            self._queue.put_nowait(state)
            return True
        except queue.Full:
            # Drop the oldest stale snapshot; enqueue the fresh one.
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(state)
                return True
            except queue.Full:
                self._dropped += 1
                return False

    def _writer_loop(self):
        """Drain the queue and write to disk one snapshot at a time."""
        while self._running:
            try:
                state = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._write_to_disk(state)
                self._written += 1
            except Exception as e:
                print(f"[StateStore] write error: {e}", file=sys.stderr)

        # Flush whatever's left on shutdown
        while not self._queue.empty():
            try:
                state = self._queue.get_nowait()
                self._write_to_disk(state)
                self._written += 1
            except queue.Empty:
                break
            except Exception as e:
                print(f"[StateStore] flush error: {e}", file=sys.stderr)

    def _write_to_disk(self, state: dict) -> None:
        """Atomic + durable write: temp file → fsync → os.replace → fsync parent.

        Three guarantees on top of the basic temp+rename pattern:

        1. **f.flush() + os.fsync(fd)** before rename. Without fsync, the
           temp file's contents live only in the OS page cache. A power
           loss / kernel panic between the rename and the fsync would
           leave us with a renamed-but-empty file — `_load_state` returns
           `{}`-ish, the engine boots into IDLE, and reconciliation tries
           to re-place orders against a position it doesn't know about.
           fsync forces the bytes through to the device before we rename.

        2. **os.replace** is atomic on POSIX and Windows — readers see
           either the old file or the new one, never a half-written one.

        3. **Directory fsync** after rename, so a power loss can't lose
           the rename itself even if it survived the data. Some filesystems
           (ext4 default, xfs) keep directory entries in a separate journal;
           without this fsync, the rename can be reverted on crash recovery.

        Cost: ~1-3 ms per save on local SSD, ~10-50 ms on EBS gp3. Saves
        only fire on state transitions (fills, new highs, daily reset) —
        not on every tick — so worst-case rate is ~10/min. Acceptable for
        the audit-grade integrity we want.
        """
        temp = self.path + ".tmp"
        with open(temp, "w") as f:
            json.dump(state, f, indent=2, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                # fsync may not be supported on every fs (rare). Atomic
                # rename below still gives us "old or new, never partial",
                # we just lose the power-loss guarantee. Don't fail the
                # write over a missing fsync — log silently and continue.
                pass
        os.replace(temp, self.path)
        # Sync the parent directory so the rename itself is durable.
        # Skip silently on platforms where directory fsync isn't supported
        # (Windows raises PermissionError).
        try:
            dir_fd = os.open(os.path.dirname(os.path.abspath(self.path)) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, PermissionError, AttributeError):
            pass

    def load(self) -> Optional[dict]:
        """Load state from JSON file."""
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def clear(self) -> None:
        """Delete state file."""
        if os.path.exists(self.path):
            os.remove(self.path)

    def close(self, timeout: float = 2.0) -> dict:
        """Stop the writer thread and flush remaining queue. Call on shutdown."""
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return {"written": self._written, "dropped": self._dropped, "path": self.path}


class AuditLog:
    """Lean audit log - CSV file append-only."""

    __slots__ = ('path', '_ts')

    LOG_FILE = ".gt_audit.csv"
    HEADER = "timestamp,event,trade_id,data\n"

    def __init__(self, path: str = LOG_FILE):
        self.path = path
        self._ts = datetime.now
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(self.HEADER)

    def append(self, event: str, trade_id: str = "", data: dict = None) -> None:
        """Append audit entry."""
        with open(self.path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                self._ts().isoformat(),
                event,
                trade_id,
                json.dumps(data or {}),
            ])

    def read(self, limit: int = 100) -> list[dict]:
        """Read recent audit entries."""
        entries = []
        with open(self.path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                entries.append(row)
        return entries[-limit:]