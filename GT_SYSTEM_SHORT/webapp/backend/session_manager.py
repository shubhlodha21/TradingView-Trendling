"""
Session save / restore — captures the "desired bot pool" to disk.

Workflow this serves:
  * Operator launches bots through the UI on Monday. Every successful
    launch is auto-appended to `.gt_session.json` in cwd.
  * Friday 4pm ET: operator clicks "Stop All". Every running bot gets
    SIGTERM. The engine's signal handler flushes state to its
    `.gt_state_<SYM>_<CID>.json` and exits cleanly. The session file
    is NOT modified — the desired pool stays intact.
  * Monday 9:30am ET: operator clicks "Restore Session". For each
    LaunchRequest in the session file that isn't already running, we
    spawn run_live.py with the same flags. The engine reads its
    state file on startup, sees the prior `entry_price` /
    `position_open` / `stop_loss`, and the reconcile pass adopts any
    GTC orders still resting at IBKR. Continuity preserved.

Storage:
  * One JSON file at `<cwd>/.gt_session.json` — same directory the
    bots write their state files to.
  * Atomic writes via temp+rename so a half-written file never confuses
    the next read.
  * Stable order: bots are kept in the file in launch order, not
    re-sorted, so the operator can read it like a runbook.

NOT in scope here:
  * Scheduling (auto-stop at 4pm, auto-restart at 9:30am). Easy add
    later via an asyncio task that calls stop_all/restore on the
    right wall-clock triggers. Left out of v1 so the operator owns
    the button explicitly.
  * Cancelling working orders. The engine's own --reset flow handles
    that case (and is a separate, deliberate action). "Stop All"
    leaves working orders at IBKR (GTC stop-limit) so the position
    stays protected overnight.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .schemas import LaunchRequest, SavedSession

if TYPE_CHECKING:
    from .process_manager import ProcessManager


SESSION_FILENAME = ".gt_session.json"


class SessionManager:
    """Reads + writes the session file. Stateless across calls."""
    __slots__ = ("_path", "_lock")

    def __init__(self, cwd: Optional[Path] = None) -> None:
        base = Path(cwd) if cwd else Path(os.environ.get("GT_WEBAPP_CWD", "."))
        self._path = base / SESSION_FILENAME
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ── Read ────────────────────────────────────────────────────────────
    def load(self) -> Optional[SavedSession]:
        """Return the persisted session or None if the file is missing /
        corrupt. Never raises — a corrupt file is logged on stderr and
        treated as 'no session' so the UI keeps working."""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_bytes())
            return SavedSession(**data)
        except Exception as e:
            print(f"[session] failed to parse {self._path.name}: {type(e).__name__}: {e}")
            return None

    # ── Write ───────────────────────────────────────────────────────────
    async def save(self, bots: list[LaunchRequest]) -> SavedSession:
        """Atomic full write. Bots list is the new desired pool."""
        sess = SavedSession(
            saved_at=datetime.now(timezone.utc).isoformat(),
            bots=list(bots),
        )
        async with self._lock:
            self._atomic_write(sess)
        return sess

    async def upsert(self, bot: LaunchRequest) -> SavedSession:
        """Insert / replace one bot by (symbol, client_id). Preserves
        launch order — existing entries keep their position, new ones
        append. Called from process_manager.launch() on success."""
        async with self._lock:
            sess = self.load() or SavedSession(
                saved_at=datetime.now(timezone.utc).isoformat(), bots=[],
            )
            key = (bot.symbol, bot.client_id)
            replaced = False
            for i, b in enumerate(sess.bots):
                if (b.symbol, b.client_id) == key:
                    sess.bots[i] = bot
                    replaced = True
                    break
            if not replaced:
                sess.bots.append(bot)
            sess.saved_at = datetime.now(timezone.utc).isoformat()
            self._atomic_write(sess)
            return sess

    async def remove(self, symbol: str, client_id: int) -> SavedSession:
        """Drop one bot from the session — used when the operator
        explicitly kills a bot they don't want resurrected on next
        Restore Session."""
        async with self._lock:
            sess = self.load() or SavedSession(
                saved_at=datetime.now(timezone.utc).isoformat(), bots=[],
            )
            sess.bots = [
                b for b in sess.bots
                if not (b.symbol == symbol and b.client_id == client_id)
            ]
            sess.saved_at = datetime.now(timezone.utc).isoformat()
            self._atomic_write(sess)
            return sess

    # ── Internal ────────────────────────────────────────────────────────
    def _atomic_write(self, sess: SavedSession) -> None:
        """Write to a temp file in the same dir, then rename. Same-volume
        rename is atomic on POSIX — readers always see either the old
        complete file or the new complete file, never a partial."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile with delete=False so we control the rename.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=parent, prefix=".gt_session.", suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(sess.model_dump(), tmp, indent=2, default=str)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        try:
            tmp_path.replace(self._path)
        except Exception:
            # Cleanup tmp if rename fails so we don't leak files.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise


# ─── High-level operations that span SessionManager + ProcessManager ──────
async def restore_session(
    sm: "SessionManager", pm: "ProcessManager",
) -> "RestoreResult":
    """Re-launch every bot in the saved session that isn't currently
    running. Returns a structured summary the frontend can render."""
    from .schemas import RestoreResult
    sess = sm.load()
    if sess is None or not sess.bots:
        return RestoreResult(error="No saved session.")

    launched, skipped = [], []
    running_keys = {p.key for p in pm.list() if p.status == "running"}
    for bot in sess.bots:
        key = f"{bot.symbol}_{bot.client_id}"
        if key in running_keys:
            skipped.append({"key": key, "reason": "already running"})
            continue
        try:
            info = await pm.launch(bot)
            launched.append(info)
        except Exception as e:
            skipped.append({"key": key, "reason": f"{type(e).__name__}: {e}"})
    return RestoreResult(launched=launched, skipped=skipped)


async def restore_one(
    sm: "SessionManager", pm: "ProcessManager", key: str,
) -> "ProcessInfo":
    """Relaunch a single bot by `<SYMBOL>_<CLIENT_ID>` key, pulling its
    flags from the saved session. Raises if:
      * the key isn't in the saved session (nothing to recover from),
      * a bot with that key is already running.

    Use case: an individual bot exited / was killed mid-day, and the
    operator wants it back without disturbing peers. Same code path
    as the full restore, just scoped to one entry.
    """
    from fastapi import HTTPException
    from .schemas import ProcessInfo
    sess = sm.load()
    if sess is None:
        raise HTTPException(404, "No saved session.")
    target = next(
        (b for b in sess.bots if f"{b.symbol}_{b.client_id}" == key),
        None,
    )
    if target is None:
        raise HTTPException(404, f"{key} not in saved session.")
    # If a process record exists and is still running, surface 409 —
    # matches the behavior of ProcessManager.launch() so callers handle
    # one error shape.
    existing = next((p for p in pm.list() if p.key == key), None)
    if existing is not None and existing.status == "running":
        raise HTTPException(409, f"{key} already running.")
    info: ProcessInfo = await pm.launch(target)
    return info


async def stop_all(pm: "ProcessManager", force: bool = False) -> "StopAllResult":
    """SIGTERM (or SIGKILL with force=True) every running bot. Returns
    the keys that were active when called vs already terminated."""
    from .schemas import StopAllResult
    stopped, already = [], []
    for p in pm.list():
        if p.status == "running":
            try:
                await pm.kill(p.key, force=force)
                stopped.append(p.key)
            except Exception:
                already.append(p.key)
        else:
            already.append(p.key)
    return StopAllResult(stopped=stopped, already_done=already)
