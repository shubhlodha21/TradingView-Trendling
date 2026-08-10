"""
Spawn / kill / monitor `run_live.py` subprocesses.

Responsibilities:
  1. Build the argv from a typed LaunchRequest (no string-formatting
     soup — every flag goes through a known mapping).
  2. Spawn the process with its own working dir + env, capturing stdout
     for a ring-buffer log tail.
  3. Tag each process by `(symbol, client_id)` — the same key the state
     files use — and refuse duplicates so we can't accidentally run two
     bots on the same IBKR client_id and tie-break their orders.
  4. Reap on exit (poll + transition to "exited"/"killed"/"error").

What we explicitly DON'T do:
  * Read or modify any `.gt_state_*.json` / `.gt_live_*.json` content.
    StateReader owns that.
  * Touch the bot's internals via Python import. We talk to it only via
    argv + the JSON artifacts it writes. The bot's process boundary is
    its contract; we respect it strictly.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .schemas import LaunchRequest, ProcessInfo
from .session_manager import SessionManager


# How many tail lines we keep per process for the log panel.
LOG_TAIL_LINES = int(os.environ.get("GT_WEBAPP_LOG_TAIL", "200"))


def build_command(req: LaunchRequest, python: str = sys.executable,
                  script: str = "run_live.py") -> tuple[list[str], dict[str, str]]:
    """Return (argv, env_overrides).

    Mirrors `run_live.py`'s argparse exactly. Optional buffer flags are
    only emitted when the request actually set them — emitting `None`
    would override CLI defaults with garbage.
    """
    argv: list[str] = [python, script, req.symbol,
                       "--trigger", f"{req.trigger}",
                       "--qty", str(req.qty),
                       "--stop", f"{req.stop}",
                       "--port", str(req.port),
                       "--client-id", str(req.client_id)]
    if req.uvloop:
        argv.append("--uvloop")
    if req.sl_limit_offset is not None:
        argv += ["--sl-limit-offset", f"{req.sl_limit_offset}"]
    if req.offset_stop_fraction is not None:
        argv += ["--offset-stop-fraction", f"{req.offset_stop_fraction}"]
    if req.offset_entry_pct is not None:
        argv += ["--offset-entry-pct", f"{req.offset_entry_pct}"]
    if req.offset_fixed is not None:
        argv += ["--offset-fixed", f"{req.offset_fixed}"]

    # Live trading is signalled to run_live.py via GT_PAPER=false in the
    # environment — argparse's `--paper` only flips ON paper mode.
    env: dict[str, str] = {"GT_PAPER": "true" if req.paper else "false"}
    return argv, env


@dataclass(slots=True)
class _Proc:
    """In-memory record of one launched subprocess."""
    key: str
    req: LaunchRequest
    proc: asyncio.subprocess.Process
    started_at: datetime
    cmd: list[str]
    log: deque  # bounded ring buffer of stdout lines
    status: str = "running"  # running | exited | killed | error
    exit_code: Optional[int] = None
    reader_task: Optional[asyncio.Task] = None


class ProcessManager:
    """Owns the set of running `run_live.py` instances."""
    __slots__ = ("_cwd", "_procs", "_lock", "_session")

    def __init__(self, cwd: Optional[Path] = None,
                 session: Optional[SessionManager] = None) -> None:
        self._cwd = Path(cwd) if cwd else Path(os.environ.get("GT_WEBAPP_CWD", "."))
        self._procs: dict[str, _Proc] = {}
        self._lock = asyncio.Lock()
        # Optional session sink — when present, every successful launch
        # auto-persists to .gt_session.json. Kept Optional so
        # ProcessManager remains usable in tests / out-of-band scripts
        # without a session file appearing in the working directory.
        self._session = session

    @property
    def cwd(self) -> Path:
        return self._cwd

    def list(self) -> list[ProcessInfo]:
        return [self._to_info(p) for p in self._procs.values()]

    def _to_info(self, p: _Proc) -> ProcessInfo:
        return ProcessInfo(
            key=p.key,
            pid=p.proc.pid,
            symbol=p.req.symbol,
            client_id=p.req.client_id,
            port=p.req.port,
            paper=p.req.paper,
            started_at=p.started_at.isoformat(),
            cmd=p.cmd,
            status=p.status,
            exit_code=p.exit_code,
            log_tail=list(p.log),
        )

    async def launch(self, req: LaunchRequest) -> ProcessInfo:
        """Spawn a new run_live.py. Refuses if (symbol, client_id) is in use."""
        key = f"{req.symbol}_{req.client_id}"
        async with self._lock:
            existing = self._procs.get(key)
            if existing and existing.status == "running":
                raise RuntimeError(
                    f"Process {key} already running (pid={existing.proc.pid}). "
                    f"Stop it first or use a different client_id."
                )

            argv, env_overrides = build_command(req)
            env = {**os.environ, **env_overrides}
            # Force unbuffered stdout so log tail keeps up with the bot in
            # real time. The bot already uses print()/file=sys.stderr; this
            # just disables the libc line-buffer when stdout is a pipe.
            env["PYTHONUNBUFFERED"] = "1"

            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self._cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Detach into its own process group so a Ctrl+C in the
                # webapp's terminal doesn't take the bots with it.
                start_new_session=True,
            )

            rec = _Proc(
                key=key,
                req=req,
                proc=proc,
                started_at=datetime.now(timezone.utc),
                cmd=argv,
                log=deque(maxlen=LOG_TAIL_LINES),
            )
            rec.reader_task = asyncio.create_task(self._drain_stdout(rec))
            self._procs[key] = rec
            info = self._to_info(rec)

        # Auto-save outside the lock — SessionManager has its own lock,
        # and we don't want to hold both at once. Failure here is
        # non-fatal: the bot is running, the launch return value is
        # correct, the session file just won't include it. Logged so
        # the operator can re-trigger Save Session manually if needed.
        if self._session is not None:
            try:
                await self._session.upsert(req)
            except Exception as e:
                print(f"[session] auto-save failed for {key}: {type(e).__name__}: {e}")
        return info

    async def kill(self, key: str, force: bool = False) -> ProcessInfo:
        """SIGTERM (or SIGKILL) the process. Returns its post-kill record."""
        async with self._lock:
            rec = self._procs.get(key)
            if rec is None:
                raise KeyError(f"No process with key {key}")
            if rec.status == "running":
                try:
                    if force:
                        rec.proc.send_signal(signal.SIGKILL)
                    else:
                        rec.proc.terminate()
                except ProcessLookupError:
                    # already gone — let the drain task transition status
                    pass
                rec.status = "killed"
            return self._to_info(rec)

    async def _drain_stdout(self, rec: _Proc) -> None:
        """Consume the bot's stdout line-by-line into the ring buffer.

        Exits when stdout closes (process gone). Transitions status on
        the way out so the next /api/processes call reflects reality."""
        assert rec.proc.stdout is not None
        try:
            while True:
                line = await rec.proc.stdout.readline()
                if not line:
                    break
                try:
                    rec.log.append(line.decode("utf-8", "replace").rstrip())
                except Exception:
                    rec.log.append("<undecodable>")
        finally:
            rc = await rec.proc.wait()
            rec.exit_code = rc
            if rec.status == "running":
                rec.status = "exited" if rc == 0 else "error"
