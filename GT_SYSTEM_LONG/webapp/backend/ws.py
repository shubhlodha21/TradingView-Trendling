"""
WebSocket broadcaster.

One background loop polls every source (state files, process manager,
gateway probe) and pushes diffs to all connected clients. Frontend
subscribes to a single `/ws` endpoint and dispatches on `frame.type`.

Why one loop, not one per source: the data sets are small (handful of
symbols, a couple of processes, one gateway). Fanning out to multiple
producer tasks would add coordination cost (deduping, ordering) for no
throughput gain — file polls take ~30 µs even with no cache hits.

Tick rate is fixed at FRAME_HZ frames/sec. Bumping it costs CPU and
WebSocket framing overhead without giving the eye more information —
the bots themselves write their live snapshots at 5 Hz, so anything
above 10 Hz here is duplicate work.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect

from .gateway_probe import probe
from .process_manager import ProcessManager
from .state_reader import StateReader


FRAME_HZ = float(os.environ.get("GT_WEBAPP_FRAME_HZ", "5"))
GATEWAY_PROBE_EVERY = int(os.environ.get("GT_WEBAPP_GATEWAY_EVERY", "10"))


class WSBroadcaster:
    """Holds the set of connected clients + the broadcast loop task."""
    __slots__ = ("_clients", "_lock", "_task", "_reader", "_pm")

    def __init__(self, reader: StateReader, pm: ProcessManager) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._reader = reader
        self._pm = pm

    # ── Lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ── Client management ───────────────────────────────────────────────
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        # Immediately push a snapshot so the frontend doesn't render an
        # empty UI for up to 1/FRAME_HZ seconds after connect.
        await self._send_one(ws, self._build_snapshot_frame())
        await self._send_one(ws, self._build_processes_frame())

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def serve(self, ws: WebSocket) -> None:
        """Top-level coroutine for a single WebSocket. FastAPI awaits
        this until the client disconnects."""
        await self.connect(ws)
        try:
            while True:
                # Drain client messages (we don't expect any commands
                # over WS — REST handles writes — but readers block on
                # incoming frames to detect disconnect promptly).
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await self.disconnect(ws)

    # ── Broadcast loop ──────────────────────────────────────────────────
    async def _loop(self) -> None:
        period = 1.0 / FRAME_HZ
        tick = 0
        while True:
            try:
                await self._broadcast(self._build_snapshot_frame())
                await self._broadcast(self._build_processes_frame())
                if tick % GATEWAY_PROBE_EVERY == 0:
                    gw = await probe()
                    await self._broadcast({"type": "gateway", "payload": gw.model_dump()})
                tick += 1
            except Exception as e:
                # Never let a bad frame kill the broadcaster. Surface
                # via an error frame so the frontend can show "backend
                # error" instead of going silent.
                err = {"type": "error", "payload": {"where": "ws._loop",
                                                     "error": f"{type(e).__name__}: {e}"}}
                await self._broadcast(err)
            await asyncio.sleep(period)

    def _build_snapshot_frame(self) -> dict:
        snaps = self._reader.snapshot()
        return {"type": "snapshot",
                "payload": {"symbols": [s.model_dump() for s in snaps]}}

    def _build_processes_frame(self) -> dict:
        procs = self._pm.list()
        return {"type": "process_list",
                "payload": {"processes": [p.model_dump() for p in procs]}}

    async def _broadcast(self, frame: dict) -> None:
        if not self._clients:
            return
        text = json.dumps(frame, default=str)
        # Snapshot to a list under the lock so we don't mutate during iteration.
        async with self._lock:
            targets = list(self._clients)
        # Send fan-out concurrently. A slow client can't block fast ones.
        await asyncio.gather(
            *(self._send_one_raw(ws, text) for ws in targets),
            return_exceptions=True,
        )

    async def _send_one(self, ws: WebSocket, frame: dict) -> None:
        await self._send_one_raw(ws, json.dumps(frame, default=str))

    async def _send_one_raw(self, ws: WebSocket, text: str) -> None:
        try:
            await ws.send_text(text)
        except Exception:
            # Client gone or queue full — disconnect it cleanly.
            await self.disconnect(ws)
