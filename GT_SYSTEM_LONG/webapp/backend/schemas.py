"""
Pydantic schemas — single source of truth for the REST + WebSocket payloads.

Every field below maps 1:1 to either:
  * a flag on `run_live.py` (the existing CLI we shell out to), or
  * a key in `.gt_state_<SYM>_<CID>.json` / `.gt_live_<SYM>_<CID>.json`
    (the existing artifacts written by run_live.py, which we ONLY read).

Names match the on-disk JSON exactly so we can `Model(**json.load(f))`
without an adapter layer. Fields are Optional where the writer may omit
them (older session files, paper mode, pre-tick state).
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ── Process lifecycle (POST /api/processes) ─────────────────────────────────
class LaunchRequest(BaseModel):
    """Mirror of `run_live.py` argparse. Every flag is here, typed.

    A subset goes into env vars (paper → GT_PAPER), the rest become argv.
    The construction logic lives in process_manager.build_command; this
    model only validates shape + types so the form on the frontend can
    use the same definitions (generated via openapi).
    """
    symbol: str = Field(..., min_length=1, max_length=8, pattern=r"^[A-Z][A-Z0-9.]{0,7}$")
    trigger: float = Field(..., gt=0, le=100_000)
    qty: int = Field(..., gt=0, le=100_000)
    stop: float = Field(0.01, gt=0, lt=1.0, description="Stop loss fraction, e.g. 0.01 = 1%")
    port: int = Field(7496, ge=1, le=65535)
    client_id: int = Field(..., ge=0, le=999)
    paper: bool = False
    uvloop: bool = True
    # Buffer scaling — exactly one of these forms should be set
    sl_limit_offset: Optional[float] = Field(None, ge=0, le=10)
    offset_stop_fraction: Optional[float] = Field(None, ge=0, le=1)
    offset_entry_pct: Optional[float] = Field(None, ge=0, le=1)
    offset_fixed: Optional[float] = Field(None, ge=0, le=10)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class ProcessInfo(BaseModel):
    """One running run_live.py instance.

    `pid` is the OS PID. `key` is "<SYMBOL>_<CLIENT_ID>" — the tuple that
    uniquely identifies a bot instance (and matches the state filenames).
    """
    key: str
    pid: int
    symbol: str
    client_id: int
    port: int
    paper: bool
    started_at: str  # ISO 8601
    cmd: list[str]
    status: Literal["running", "exited", "killed", "error"]
    exit_code: Optional[int] = None
    log_tail: list[str] = Field(default_factory=list)


# ── Per-symbol state (read from .gt_state_*.json) ───────────────────────────
class SymbolState(BaseModel):
    """Mirror of `.gt_state_<SYM>_<CID>.json`. All Optional because the
    writer is event-driven — a freshly-launched bot has only a few keys
    populated until the first tick / state transition.
    """
    state: Optional[str] = None
    cycle_id: Optional[str] = None
    position_open: bool = False
    entry_price: Optional[float] = None
    highest_price: Optional[float] = None
    stop_loss: Optional[float] = None
    previous_breakout_level: Optional[float] = None
    quantity: int = 0
    trades_today: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    total_commission: float = 0.0


# ── Per-symbol live snapshot (read from .gt_live_*.json) ────────────────────
class SymbolLive(BaseModel):
    """Mirror of `.gt_live_<SYM>_<CID>.json`, written at ~5Hz by
    LiveTrader.write_live_snapshot."""
    ts: Optional[str] = None
    symbol: Optional[str] = None
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_size: int = 0
    ask_size: int = 0
    volume: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    vwap: float = 0.0
    trigger_price: float = 0.0
    quantity: int = 0
    equity: float = 0.0
    buying_power: float = 0.0
    position_notional: float = 0.0
    exposure_pct: float = 0.0
    bp_used_pct: float = 0.0
    rate: float = 0.0
    tick_rate: float = 0.0
    bbo_rate: float = 0.0
    trade_rate: float = 0.0
    buy_pct: float = 0.0
    sell_pct: float = 0.0
    connected: bool = False
    paused: bool = False
    heartbeat_age: float = 0.0
    latency: dict = Field(default_factory=dict)
    tape: list[dict] = Field(default_factory=list)


class SymbolSnapshot(BaseModel):
    """One combined row: state + live + computed derivatives.

    This is what the frontend's positions table + header pills consume.
    Built by state_reader.combine() so the frontend never has to merge.
    """
    key: str
    symbol: str
    client_id: int
    is_active: bool          # state file mtime within STALENESS_S
    last_seen: str           # ISO 8601 of state file mtime
    state: SymbolState
    live: SymbolLive
    spread: float = 0.0
    spread_bps: float = 0.0
    change_pct: float = 0.0   # (last - open) / open * 100


# ── Manual order ticket (POST /api/orders) ──────────────────────────────────
class ManualOrderRequest(BaseModel):
    """Manual order punched from the right-panel ticket.

    Routes through a dedicated IBKR client_id distinct from any running
    run_live.py bot, so it never collides with a bot's own order IDs.
    """
    symbol: str = Field(..., min_length=1, max_length=8, pattern=r"^[A-Z][A-Z0-9.]{0,7}$")
    side: Literal["BUY", "SELL"]
    qty: int = Field(..., gt=0, le=100_000)
    order_type: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    limit_price: Optional[float] = Field(None, gt=0)
    stop_price: Optional[float] = Field(None, gt=0)
    tif: Literal["DAY", "GTC", "IOC"] = "DAY"
    # Sanity gate — the user has to acknowledge a notional > $X for real
    # money orders. Frontend computes qty × ref_price and asks for an
    # explicit checkbox. Backend re-checks here to defend against tampered
    # clients.
    acknowledged_notional: float = Field(..., ge=0)

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class ManualOrderResponse(BaseModel):
    order_id: str
    status: str
    submitted_at: str  # ISO 8601


# ── Session save / restore ──────────────────────────────────────────────────
class SavedSession(BaseModel):
    """Snapshot of the desired bot pool, persisted to .gt_session.json.

    Auto-updated on every successful `launch()`. Persists across
    webapp restarts so a Monday-morning "Restore Session" relaunches
    exactly what was running Friday afternoon — same flags, same
    client_ids, so the bot's existing state files + GTC orders at
    IBKR are picked up by the engine's normal reconcile flow.
    """
    saved_at: str
    bots: list[LaunchRequest] = Field(default_factory=list)


class RestoreResult(BaseModel):
    """What happened on a /api/sessions/restore call."""
    launched: list[ProcessInfo] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)   # already running / errors
    error: Optional[str] = None


class StopAllResult(BaseModel):
    stopped: list[str] = Field(default_factory=list)    # keys SIGTERM'd
    already_done: list[str] = Field(default_factory=list)


# ── Audit + alerts (read-only views over data/audit/* + data/alerts/*) ─────
class AuditOrder(BaseModel):
    """One row of `data/audit/order_<SYM>_<YYYYMMDD>.csv`.

    Field names mirror the on-disk header exactly. Numeric fields stay
    as `Optional[str]` on the wire because the CSV writer formats them
    differently per column (4 decimals for price, 2 for P&L). The
    frontend parses to float at render time so display precision is
    preserved."""
    timestamp: Optional[str] = None
    event: Optional[str] = None            # SUBMITTED, FILLED, REJECTED, CANCELLED
    order_id: Optional[str] = None
    side: Optional[str] = None             # BUY / SELL
    qty: Optional[str] = None
    order_type: Optional[str] = None
    limit_price: Optional[str] = None
    stop_price: Optional[str] = None
    signal_price: Optional[str] = None
    fill_price: Optional[str] = None
    slippage: Optional[str] = None
    commission: Optional[str] = None
    pnl: Optional[str] = None
    reason: Optional[str] = None
    exchange: Optional[str] = None
    state_at_time: Optional[str] = None
    position_at_time: Optional[str] = None


class AuditState(BaseModel):
    """One row of `state_<SYM>_<YYYYMMDD>.csv` — every state machine
    transition + periodic SNAPSHOT events."""
    timestamp: Optional[str] = None
    event: Optional[str] = None
    state: Optional[str] = None
    position_open: Optional[str] = None
    entry_price: Optional[str] = None
    highest_price: Optional[str] = None
    stop_loss: Optional[str] = None
    trigger_price: Optional[str] = None
    breakout_level: Optional[str] = None
    prev_ltp: Optional[str] = None
    ltp: Optional[str] = None
    pnl: Optional[str] = None
    trades_today: Optional[str] = None
    wins: Optional[str] = None
    losses: Optional[str] = None
    config_trigger: Optional[str] = None
    config_stop_pct: Optional[str] = None
    config_qty: Optional[str] = None


class AuditPnl(BaseModel):
    """One row of `pnl_<SYM>_<YYYYMMDD>.csv`. Used by the equity-curve view."""
    timestamp: Optional[str] = None
    state: Optional[str] = None
    position_open: Optional[str] = None
    entry_price: Optional[str] = None
    current_price: Optional[str] = None
    unrealized_pnl: Optional[str] = None
    realized_pnl: Optional[str] = None
    total_pnl: Optional[str] = None
    wins: Optional[str] = None
    losses: Optional[str] = None
    trades_today: Optional[str] = None
    comm_today: Optional[str] = None
    highest_price: Optional[str] = None
    stop_loss: Optional[str] = None


class AlertEntry(BaseModel):
    """One line of `alerts_<YYYYMMDD>.jsonl`.

    Mirrors src/infra/alerts.py:Alert.to_dict() exactly. `context` is
    free-form per-code: ORDER_REJECTED carries `order_id` + `reason`,
    PRICE_JUMP carries `prev_price`/`new_price`/`jump_pct`, etc."""
    code: str
    severity: str        # CRITICAL | HIGH | MEDIUM | LOW
    message: str
    timestamp: str
    context: dict = Field(default_factory=dict)
    correlation_id: Optional[str] = ""


class AlertCounts(BaseModel):
    """Per-severity totals for today's alerts. Powers the top-bar badge."""
    CRITICAL: int = 0
    HIGH: int = 0
    MEDIUM: int = 0
    LOW: int = 0


# ── Gateway / TWS health ────────────────────────────────────────────────────
class GatewayStatus(BaseModel):
    """Health of the IB Gateway / TWS instance on the same host.

    Probed by a TCP connect to (host, port). No login attempt — that
    would burn an IBKR session slot. We just confirm something is
    listening on the API port.
    """
    host: str
    port: int
    reachable: bool
    last_checked: str
    error: Optional[str] = None


# ── WebSocket frames ────────────────────────────────────────────────────────
class WSFrame(BaseModel):
    """Wrapper for every WebSocket message so the frontend can dispatch
    on `type` without sniffing fields."""
    type: Literal["snapshot", "process_list", "gateway", "tick", "log", "error"]
    payload: dict
