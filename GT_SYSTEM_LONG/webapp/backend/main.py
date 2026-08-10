"""
FastAPI app — REST endpoints + WebSocket.

REST surface:
  GET  /api/health           liveness
  GET  /api/gateway          one-shot TWS probe (the WS stream pushes
                             this every ~2s anyway; REST is for the
                             initial render before WS attaches)
  GET  /api/processes        list current run_live.py procs
  POST /api/processes        launch a new run_live.py
  DELETE /api/processes/{key}  kill one
  GET  /api/snapshot         all SymbolSnapshots (one-shot)
  POST /api/orders           manual order (returns 501 today;
                             pass ?dry_run=true to exercise the form)
  WS   /ws                   live stream of frames

Static SPA:
  /            serves frontend/dist if present (production build)
  /assets/*    Vite's hashed asset bundle

CORS is configured for `http://localhost:5173` (Vite dev) and any
origin the user puts in GT_WEBAPP_ALLOWED_ORIGINS (comma-separated).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .audit_reader import AuditReader
from .gateway_probe import probe
from .ibkr_proxy import submit, synthetic_response, validate
from .process_manager import ProcessManager
from .schemas import (AlertCounts, AlertEntry, AuditOrder, AuditPnl, AuditState,
                      LaunchRequest, ManualOrderRequest, ManualOrderResponse,
                      ProcessInfo, RestoreResult, SavedSession, StopAllResult,
                      SymbolSnapshot)
from .session_manager import SessionManager, restore_one, restore_session, stop_all
from .state_reader import StateReader
from .ws import WSBroadcaster


# Where the bots' state files live. Defaults to cwd of THIS process
# — typically the repo root, same place you'd run `python run_live.py`
# from. Override with GT_WEBAPP_CWD if the webapp runs from a service
# directory that isn't the repo root.
CWD = Path(os.environ.get("GT_WEBAPP_CWD", ".")).resolve()

_reader = StateReader(cwd=CWD)
_session = SessionManager(cwd=CWD)
_pm = ProcessManager(cwd=CWD, session=_session)
_audit = AuditReader(cwd=CWD)
_broadcaster = WSBroadcaster(_reader, _pm)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _broadcaster.start()
    try:
        yield
    finally:
        await _broadcaster.stop()


app = FastAPI(title="GT Webapp", version="0.1.0", lifespan=lifespan)

_origins = os.environ.get("GT_WEBAPP_ALLOWED_ORIGINS",
                          "http://localhost:5173,http://127.0.0.1:5173").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── REST ─────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "cwd": str(CWD)}


@app.get("/api/gateway")
async def gateway_status() -> dict:
    gw = await probe()
    return gw.model_dump()


@app.get("/api/snapshot", response_model=list[SymbolSnapshot])
async def snapshot() -> list[SymbolSnapshot]:
    return _reader.snapshot()


@app.get("/api/processes", response_model=list[ProcessInfo])
async def list_processes() -> list[ProcessInfo]:
    return _pm.list()


@app.post("/api/processes", response_model=ProcessInfo, status_code=201)
async def launch_process(req: LaunchRequest) -> ProcessInfo:
    try:
        return await _pm.launch(req)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        # python or run_live.py missing in CWD
        raise HTTPException(500, f"Cannot spawn: {e}")


@app.delete("/api/processes/{key}", response_model=ProcessInfo)
async def kill_process(
    key: str, force: bool = False, forget: bool = False,
) -> ProcessInfo:
    """Kill one bot.

    `forget=true` also removes the bot from the saved session file so
    the next Restore Session won't bring it back. Default (forget=false)
    preserves it — matches the "Stop All for the night" workflow where
    you want everything to come back Monday morning.
    """
    try:
        info = await _pm.kill(key, force=force)
    except KeyError:
        raise HTTPException(404, f"No process with key {key}")
    if forget:
        try:
            await _session.remove(info.symbol, info.client_id)
        except Exception as e:
            print(f"[session] forget failed for {key}: {type(e).__name__}: {e}")
    return info


# ── Session lifecycle (Stop All / Save / Restore) ───────────────────
@app.get("/api/sessions/saved", response_model=Optional[SavedSession])
async def get_saved_session() -> Optional[SavedSession]:
    return _session.load()


@app.post("/api/sessions/save", response_model=SavedSession)
async def save_session() -> SavedSession:
    """Snapshot the CURRENTLY-RUNNING bots into .gt_session.json,
    replacing whatever was there. Useful if you want to overwrite
    the session with the live state (e.g., after manually killing
    a misconfigured bot)."""
    bots = []
    for p in _pm.list():
        if p.status != "running":
            continue
        # ProcessInfo doesn't carry the original LaunchRequest, but we
        # do — it's on the internal _Proc record. Reach through the
        # manager to rebuild the request from the stored argv would be
        # brittle; instead pull from the manager's _procs map directly.
        proc = _pm._procs.get(p.key)  # noqa: SLF001 — internal but stable
        if proc is not None:
            bots.append(proc.req)
    return await _session.save(bots)


@app.post("/api/sessions/restore", response_model=RestoreResult)
async def restore() -> RestoreResult:
    return await restore_session(_session, _pm)


@app.post("/api/sessions/restore/{key}", response_model=ProcessInfo)
async def restore_single(key: str) -> ProcessInfo:
    """Relaunch ONE bot from the saved session — same flags it was
    launched with originally. Used for the per-row 'Recover' button
    so the operator can resurrect a single exited/killed bot without
    touching its peers."""
    return await restore_one(_session, _pm, key)


@app.post("/api/sessions/stop-all", response_model=StopAllResult)
async def stop_all_route(force: bool = False) -> StopAllResult:
    """SIGTERM every running bot. `force=true` upgrades to SIGKILL.

    Does NOT touch the session file — bots stay in it so the next
    Restore Session brings them back. The bot's engine flushes state
    to its .gt_state_*.json on SIGTERM, and any GTC stop-limit it
    placed at IBKR remains active so the position is protected
    overnight.
    """
    return await stop_all(_pm, force=force)


@app.post("/api/orders", response_model=ManualOrderResponse)
async def place_order(req: ManualOrderRequest, dry_run: bool = False) -> ManualOrderResponse:
    if dry_run:
        validate(req)
        return synthetic_response(req)
    return await submit(req)  # returns 501 until wired


# ── Audit + alerts (read-only views over the bot's CSV/JSONL streams) ─
# Keys throughout are the same "<SYMBOL>_<CLIENT_ID>" identifier the
# process manager uses, but only the SYMBOL part is needed to locate
# the audit files. Client-id matters for state files but the bot's
# audit writer keys CSVs by symbol (across all client_ids on that
# symbol — operators rarely run two bots on the same ticker anyway).
@app.get("/api/audit/orders/{key}", response_model=list[AuditOrder])
async def audit_orders(key: str, limit: int = 100) -> list[AuditOrder]:
    """Walk back daily order CSVs for this symbol, return cycle-cleaned
    list. SUBMITTED rows that were superseded or cancelled before fill
    are dropped (matches the bot's internal `_filter_to_current_cycles`)
    so the History tab shows real activity, not phantoms."""
    symbol = key.rsplit("_", 1)[0]
    return [AuditOrder(**r) for r in _audit.orders(symbol, limit=limit)]


@app.get("/api/audit/open-orders/{key}", response_model=list[AuditOrder])
async def audit_open_orders(key: str) -> list[AuditOrder]:
    """Subset of /audit/orders: currently working SUBMITTEDs with no
    terminal event yet — what's actually resting at the broker right now."""
    symbol = key.rsplit("_", 1)[0]
    return [AuditOrder(**r) for r in _audit.open_orders(symbol)]


@app.get("/api/audit/state/{key}", response_model=list[AuditState])
async def audit_state(key: str, limit: int = 50) -> list[AuditState]:
    """State machine transitions + SNAPSHOT events for the timeline view."""
    symbol = key.rsplit("_", 1)[0]
    return [AuditState(**r) for r in _audit.state_transitions(symbol, limit=limit)]


@app.get("/api/audit/pnl/{key}", response_model=list[AuditPnl])
async def audit_pnl(key: str, limit: int = 2000) -> list[AuditPnl]:
    """P&L snapshots (~5s cadence) — the equity-curve data source."""
    symbol = key.rsplit("_", 1)[0]
    return [AuditPnl(**r) for r in _audit.pnl_snapshots(symbol, limit=limit)]


@app.get("/api/alerts", response_model=list[AlertEntry])
async def list_alerts(
    limit: int = 200,
    severity: Optional[str] = None,   # CRITICAL/HIGH/MEDIUM/LOW
    symbol: Optional[str] = None,     # filter context.ticker
) -> list[AlertEntry]:
    """Tail global alerts JSONL across last `days_back` days, filtered."""
    rows = _audit.alerts(limit=limit, severity=severity, symbol=symbol)
    return [AlertEntry(**r) for r in rows]


@app.get("/api/alerts/counts", response_model=AlertCounts)
async def alert_counts() -> AlertCounts:
    """Per-severity counts for today only — powers the top-bar badge."""
    return AlertCounts(**_audit.alert_counts())


# ── WebSocket ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    await _broadcaster.serve(ws)


# ── Static SPA (production build) ────────────────────────────────────
_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_DIST / "index.html")

    @app.get("/{path:path}")
    async def spa_fallback(path: str) -> FileResponse:
        # SPA routing — anything not under /api or /assets returns the
        # SPA shell so client-side router can handle the URL.
        if path.startswith(("api/", "assets/", "ws")):
            raise HTTPException(404)
        candidate = _DIST / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
else:
    @app.get("/")
    async def index_placeholder() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "msg": "Frontend dist/ not built. Run `npm install && npm run build` in webapp/frontend/.",
        })
