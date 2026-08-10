"""Backend Protocol — the single seam between engine and the outside world.

Three concrete backends implement this:

  MockBroker        — deterministic synthetic, the fuzz target
  IBKRPaperBackend  — real IBKR paper account
  IBKRLiveBackend   — real IBKR live account (small-size canary)

The engine is structured so it consumes ONLY this interface, never reaches
into `ib_async` directly. That makes the simulator/paper/live swap one line:

    engine = Engine(config, backend=MockBroker(seed=42, ...))      # synthetic
    engine = Engine(config, backend=IBKRPaperBackend(port=7497))   # real paper
    engine = Engine(config, backend=IBKRLiveBackend(port=7496))    # canary

Why a Protocol (structural) rather than ABC (nominal):
    Python's typing.Protocol checks structural conformance — anything that
    walks like the protocol IS the protocol. No inheritance requirement,
    so the existing production `Gateway` class can be wrapped/adapted into
    the protocol without modification. Also: type checker (mypy/pyright)
    flags any missing method at compile time.

Why this is THE Citadel-style architectural commitment:
    At top firms the trading engine has NO direct dependency on the broker
    SDK. The broker SDK lives behind an adapter that maps to a stable,
    versioned interface. Engine logic is tested with a deterministic mock
    of that interface. The same scenario suite runs against mock, paper,
    and live by swapping the adapter — that's how you reach 99.9% confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import (
    AsyncIterator, Awaitable, Callable, Optional, Protocol, Union,
    runtime_checkable,
)


# ════════════════════════════════════════════════════════════════════════════
# VALUE TYPES — the language the Backend speaks
# ════════════════════════════════════════════════════════════════════════════
#
# These are the engine's view of broker truth. Every field is typed,
# documented, and intentional. NO raw `ib_async.Trade` / `ib_async.Order`
# objects leak across this boundary — the broker adapter translates.
# Reason: ib_async types are version-unstable and IBKR-specific. We keep
# our own canonical types so any backend can satisfy them without
# inheriting IBKR's quirks.

@dataclass(frozen=True, slots=True)
class ContractRef:
    """Broker-neutral instrument reference.

    `logical_ticker` is what the engine uses everywhere ("EURUSD", "AAPL",
    "ES", "IBUS500"). It matches `config.ticker`. The broker-specific
    contract details (conId, exchange routing, etc.) are kept opaque
    behind the backend — the engine never inspects them.
    """
    logical_ticker: str
    asset_class: str          # 'US_EQUITY' / 'FX_CASH' / 'FX_CFD' / 'INDEX_CFD' / 'FUTURE' / ...
    currency: str             # 'USD' / 'EUR' / 'JPY' / ...
    min_tick: Decimal         # venue-reported minimum price increment
    multiplier: Optional[Decimal] = None  # futures only


@dataclass(frozen=True, slots=True)
class PositionRef:
    """A position currently held at the broker, in engine-canonical form."""
    logical_ticker: str
    quantity: Decimal          # signed: positive long, negative short
    average_cost: Decimal      # broker's avg cost basis
    market_value: Decimal      # qty × current_price (last broker mark)


@dataclass(frozen=True, slots=True)
class OrderRef:
    """An order currently resting at the broker, in engine-canonical form.

    Carries the engine_id we stamped on it at placement time (via
    IBKR's orderRef field), so reconcile can match broker truth back
    to engine memory by id.
    """
    broker_id: str             # IBKR's orderId, as string for stability
    engine_id: str             # our engine_id stashed in orderRef
    logical_ticker: str
    action: str                # 'BUY' / 'SELL'
    order_type: str            # 'STP' / 'STPLMT' / 'LMT' / 'MKT'
    quantity: Decimal
    stop_price: Optional[Decimal] = None    # auxPrice for STP/STPLMT
    limit_price: Optional[Decimal] = None   # lmtPrice for LMT/STPLMT
    tif: str = 'GTC'
    status: str = 'Submitted'  # 'Submitted'/'PreSubmitted'/'PendingSubmit'/...
    parent_id: Optional[str] = None          # bracket parent's broker_id
    is_bracket_child: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionRef:
    """A single execution (fill) returned by the broker.

    One order can produce multiple ExecutionRefs (partial fills).
    `exec_id` is the broker's unique-per-execution identifier — used
    by the engine for replay dedup.
    """
    exec_id: str
    broker_id: str             # the order's broker_id
    engine_id: str             # engine_id stashed at placement
    logical_ticker: str
    side: str                  # 'BOT' (bought) / 'SLD' (sold) — IBKR taxonomy
    shares: Decimal
    price: Decimal
    timestamp: datetime        # broker's authoritative fill time (UTC)


@dataclass(frozen=True, slots=True)
class CommissionReport:
    """The broker's true post-execution commission, arriving asynchronously
    AFTER the corresponding execution. Engine accumulates these as the
    authoritative source for PnL math (not the modeled estimate)."""
    exec_id: str               # links to the ExecutionRef
    commission: Decimal
    currency: str
    realized_pnl: Optional[Decimal] = None   # populated on closing trades


@dataclass(frozen=True, slots=True)
class OrderStatusUpdate:
    """A non-fill terminal state update from the broker (Cancelled,
    Rejected, Inactive). The engine clears _pending_stop, re-arms, etc."""
    broker_id: str
    engine_id: str
    status: str                # 'Cancelled' / 'Rejected' / 'Inactive' / 'ApiCancelled'
    message: str = ""          # broker-supplied reason text


@dataclass(frozen=True, slots=True)
class MarketTick:
    """A single market data update. Carries enough to drive both
    quote-driven (FX/CFD) and trade-driven (equity/futures) strategies.

    `tick_type` discriminator tells the engine whether `last` is a real
    trade print (`TRADE`) or a quote update with last carried over
    (`QUOTE`/`BBO`).
    """
    timestamp: datetime
    logical_ticker: str
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    bid_size: Optional[Decimal] = None
    ask_size: Optional[Decimal] = None
    last: Optional[Decimal] = None
    last_size: Optional[Decimal] = None
    tick_type: str = 'BBO'     # 'BBO' / 'TRADE' / 'QUOTE'


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """An order the engine wants to place. The Backend translates this
    into broker-specific calls."""
    logical_ticker: str
    action: str                # 'BUY' / 'SELL'
    order_type: str            # 'STP' / 'STPLMT' / 'LMT' / 'MKT'
    quantity: Decimal
    stop_price: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    tif: str = 'GTC'
    engine_id: str = ""        # stamped into orderRef
    parent_engine_id: Optional[str] = None    # bracket parent's engine_id
    outside_rth: bool = True


@dataclass(frozen=True, slots=True)
class BracketRequest:
    """A bracket: parent + child placed atomically. The Backend ensures
    the OCA semantics IBKR uses (transmit=False on parent, transmit=True
    on child, parentId wiring)."""
    parent: OrderRequest
    child: OrderRequest


# ════════════════════════════════════════════════════════════════════════════
# CALLBACK TYPES — how the Backend pushes events to the engine
# ════════════════════════════════════════════════════════════════════════════
#
# Async-callable signatures. Engine registers handlers; Backend invokes
# them in response to broker events (or, in the synthetic case, in
# response to scheduled-clock events).

OnTickHandler          = Callable[[MarketTick], Awaitable[None]]
OnExecutionHandler     = Callable[[ExecutionRef], Awaitable[None]]
OnCommissionHandler    = Callable[[CommissionReport], Awaitable[None]]
OnStatusHandler        = Callable[[OrderStatusUpdate], Awaitable[None]]
OnConnectHandler       = Callable[[], Awaitable[None]]
OnDisconnectHandler    = Callable[[], Awaitable[None]]


# ════════════════════════════════════════════════════════════════════════════
# BACKEND PROTOCOL — the contract every backend implements
# ════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class Backend(Protocol):
    """The single seam between engine and the outside world.

    LIFECYCLE:
        connect()       — establish session with broker (or initialize sim).
        qualify(...)    — resolve a logical_ticker into a usable ContractRef.
                          For MockBroker this synthesizes from spec; for
                          IBKR backends this calls qualifyContractsAsync +
                          reqContractDetailsAsync.
        subscribe(...)  — start receiving MarketTick events for a ticker.
        unsubscribe(...)— stop.
        place(...)      — submit an order; returns the broker_id once accepted.
        place_bracket(.)— atomic parent+child placement.
        modify(...)     — in-place modify (preserves broker_id). The engine
                          relies on this for the bracket-child stop retargeting
                          — modify-not-cancel is the safety invariant.
        cancel(...)     — cancel a specific resting order.
        positions()     — current held positions, with the realistic lag
                          quirks the broker actually has (MockBroker
                          simulates IBKR's 1-5s positions() lag).
        open_orders()   — currently-resting orders at the broker.
        executions()    — historical fills (used by reconcile-on-restart).
        disconnect()    — clean teardown.

    CALLBACK REGISTRATION:
        on_tick / on_execution / on_commission / on_status — engine
        registers handlers; backend invokes them async.

    INVARIANT: This interface is the WHOLE surface the engine sees.
    Any backend that implements every method is a valid backend.
    No leakage of ib_async types across this boundary.
    """

    # ── Lifecycle ──────────────────────────────────────────────────────
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...

    # ── Instrument qualification ──────────────────────────────────────
    async def qualify(self, logical_ticker: str) -> ContractRef:
        """Resolve a logical ticker into a fully-qualified ContractRef.

        This is where venue-reported minTick discovery happens — the
        engine reads `ContractRef.min_tick` for all rounding. Spec's
        hardcoded value is only the offline default; the broker's
        value is authoritative at runtime.
        """
        ...

    # ── Market data ───────────────────────────────────────────────────
    async def subscribe_market_data(
        self, logical_ticker: str, on_tick: OnTickHandler
    ) -> None: ...
    async def unsubscribe_market_data(self, logical_ticker: str) -> None: ...

    # ── Order placement ───────────────────────────────────────────────
    async def place_order(self, request: OrderRequest) -> str:
        """Submit a single order. Returns the broker_id once IBKR accepts.
        Fill events arrive via the registered `on_execution` handler.
        """
        ...

    async def place_bracket(self, request: BracketRequest) -> tuple[str, str]:
        """Submit a bracket (parent + child) atomically. Returns
        (parent_broker_id, child_broker_id). MUST be atomic — neither
        leg is live at the broker until BOTH have been transmitted.
        """
        ...

    async def modify_order(
        self,
        broker_id: str,
        new_stop_price: Optional[Decimal] = None,
        new_limit_price: Optional[Decimal] = None,
        new_quantity: Optional[Decimal] = None,
    ) -> bool:
        """In-place modify of an existing order — same broker_id, updated
        fields. Returns True on success, False if the broker rejects.

        CRITICAL INVARIANT: this is modify-IN-PLACE, never cancel+replace.
        The engine relies on this to retarget bracket child stops with
        zero unprotected window after parent fills. The MODIFY_NOT_REPLACE
        invariant verifies this is honored.
        """
        ...

    async def cancel_order(self, broker_id: str) -> bool: ...

    # ── State queries (snapshots) ─────────────────────────────────────
    async def positions(self) -> list[PositionRef]:
        """Currently-held positions. Includes the realistic broker-side
        lag — for IBKR there's a 1-5s window after a fill before positions()
        reflects it. MockBroker simulates this lag faithfully.
        """
        ...

    async def open_orders(self) -> list[OrderRef]:
        """Currently-resting orders at the broker."""
        ...

    async def executions(
        self,
        since: Optional[datetime] = None,
    ) -> list[ExecutionRef]:
        """Historical executions, optionally filtered to >= `since`.
        Used by engine reconcile-on-restart to replay missed fills.
        """
        ...

    async def commission_reports(
        self,
        since: Optional[datetime] = None,
    ) -> list[CommissionReport]:
        """Historical commission reports. Used by the PnL recompute
        path that adopts broker-truth commissions over modeled estimates.
        """
        ...

    # ── Callback registration ─────────────────────────────────────────
    def on_execution(self, handler: OnExecutionHandler) -> None: ...
    def on_commission(self, handler: OnCommissionHandler) -> None: ...
    def on_status(self, handler: OnStatusHandler) -> None: ...
    def on_connect(self, handler: OnConnectHandler) -> None: ...
    def on_disconnect(self, handler: OnDisconnectHandler) -> None: ...

    # ── Diagnostics ───────────────────────────────────────────────────
    @property
    def heartbeat_age_seconds(self) -> float:
        """Seconds since the last sign-of-life from the broker. Engine
        uses this to detect dead connections; the harness asserts it
        stays <5s during scenarios that should have flowing data.
        """
        ...


# ════════════════════════════════════════════════════════════════════════════
# BACKEND-AGNOSTIC EVENT (for invariant evaluation + replay)
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class BackendEvent:
    """A single observable event from the backend, captured to the
    event log for invariant evaluation and scenario replay.

    Every backend emits these in the same shape. The harness's event log
    is therefore portable — a scenario captured against MockBroker can be
    replayed against IBKRPaperBackend (and any divergence is a bug).
    """
    seq: int                   # monotonic per-scenario sequence number
    timestamp: datetime
    kind: str                  # 'TICK' / 'EXECUTION' / 'COMMISSION' / 'STATUS' / 'CONNECT' / 'DISCONNECT' / 'PLACED' / 'MODIFIED' / 'CANCELLED'
    payload: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
# CAPABILITY FLAGS (for backend-specific scenario filtering)
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """What this backend can do that others can't.

    The scenario runner uses these to skip scenarios that require
    capabilities the current backend doesn't support. E.g., chaos
    injection only works on MockBroker; live shadow only works on
    IBKRLiveBackend.
    """
    supports_chaos_injection: bool = False        # MockBroker only
    supports_time_warp: bool = False              # MockBroker only
    supports_real_market_data: bool = False       # IBKR paper/live only
    supports_real_fills: bool = False             # IBKR paper/live only
    can_force_partial_fills: bool = False         # MockBroker only
    can_force_disconnect: bool = False            # MockBroker + sometimes IBKR paper
    deterministic: bool = False                   # MockBroker only
    name: str = ""                                # 'mock' / 'paper' / 'live'


__all__ = [
    # Types
    "ContractRef", "PositionRef", "OrderRef", "ExecutionRef",
    "CommissionReport", "OrderStatusUpdate", "MarketTick",
    "OrderRequest", "BracketRequest", "BackendEvent", "BackendCapabilities",
    # Callbacks
    "OnTickHandler", "OnExecutionHandler", "OnCommissionHandler",
    "OnStatusHandler", "OnConnectHandler", "OnDisconnectHandler",
    # Protocol
    "Backend",
]
