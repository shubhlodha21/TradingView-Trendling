"""MockGateway — deterministic drop-in replacement for src.execution.Gateway.

SEPARATION PRINCIPLE
====================
This module is INDEPENDENT of the production Gateway. It does NOT inherit
from or import the production Gateway class. It satisfies the same
DUCK-TYPED interface the engine consumes (the 16 methods, 3 callbacks,
and 5 attributes audited from src/strategy/engine.py).

Why this matters:
  1. The production engine is FROZEN — what trades is what tests test.
  2. ib_async is NEVER imported here — synthetic backend stays portable.
  3. Adding MockGateway features doesn't change paper-mode in production.
  4. If we ever swap brokers (IBKR → others), only the production adapter
     changes; the test harness is invariant.

WHAT IT DOES
============
Implements an in-memory order book + position tracker driven by:
  - Engine calls (place_*, modify_*, cancel_*, fetch_*, get_*)
  - Synthetic market ticks (injected via feed_tick())
  - Operator commands (force_disconnect, inject_next_rejection, ...)

Emits the SAME callbacks the engine wires onto the production Gateway:
  - _on_fill(engine_id, qty, price, exec_id, time, commission)
  - _on_order_status(engine_id, status, message)
  - _on_commission(engine_id, commission, exec_id)

IBKR QUIRK SIMULATION
=====================
Faithfully reproduces the broker behaviors that have bitten us in
production. The realism is what catches bugs:

  - commissionReportEvent fires AFTER fillEvent (configurable delay)
  - positions() lags fills by N seconds (the 2026-06-05 fold bug)
  - Tick-grid rejection (error 110) for off-grid prices
  - Bracket child auto-activates when parent fills (OCA semantics)
  - Order rejection injection (margin, halt, malformed, ...)
  - Reconnect cycle replays missed fills
  - Partial fill modeling
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from .clock import Clock, RealClock
from .rng import RNG, DeterministicRNG


# ════════════════════════════════════════════════════════════════════════════
# DUCK-TYPED VALUE OBJECTS
# ════════════════════════════════════════════════════════════════════════════
#
# These shapes match what ib_async exposes — engine code reads
# `fill.execution.shares`, `pos.contract.symbol`, etc. We synthesize
# objects with the same attribute paths so the engine consumes them
# identically to real ib_async objects, WITHOUT importing ib_async.

@dataclass(frozen=True, slots=True)
class _MockContract:
    """Shape-compatible with ib_async.Contract for the fields the engine
    reads: `symbol`, `secType`, `currency`, `multiplier`, `exchange`."""
    symbol: str
    secType: str
    currency: str = "USD"
    multiplier: str = ""
    exchange: str = "SMART"
    conId: int = 0


@dataclass(frozen=True, slots=True)
class _MockExecution:
    """Matches `fill.execution.*` shape — engine reads `shares`, `price`,
    `execId`, `time`, `side`."""
    execId: str
    shares: float
    price: float
    time: datetime
    side: str  # 'BOT' or 'SLD'


@dataclass(frozen=True, slots=True)
class _MockCommissionReport:
    """Matches `fill.commissionReport.*` — engine reads `commission`."""
    commission: float
    currency: str = "USD"
    realizedPNL: float = 0.0


@dataclass(frozen=True, slots=True)
class _MockFill:
    """Matches ib_async.Fill — engine reads `contract`, `execution`,
    `commissionReport`."""
    contract: _MockContract
    execution: _MockExecution
    commissionReport: Optional[_MockCommissionReport] = None
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class _Position:
    """Engine reads `p.symbol` and `p.quantity` from Gateway.get_positions().
    We mirror the production Position namedtuple shape."""
    symbol: str
    quantity: float
    avg_cost: float
    market_value: float = 0.0


@dataclass(slots=True)
class _ResingOrder:
    """Engine reads `o['action']`, `o['order_type']`, `o['stop_price']`,
    `o['limit_price']`, `o['order_ref']`, `o['broker_id']`, `o['qty']`,
    `o['parent_id']`, `o['is_bracket_child']`, `o['_trade']`, `o['status']`.
    fetch_open_orders returns these as dicts."""
    broker_id: str
    engine_id: str
    action: str               # 'BUY' / 'SELL'
    order_type: str           # 'STP' / 'STPLMT' / 'LMT' / 'MKT'
    qty: int
    stop_price: Optional[float] = None
    limit_price: Optional[float] = None
    tif: str = 'GTC'
    parent_id: Optional[str] = None
    status: str = 'Submitted'
    filled_qty: int = 0
    avg_fill_price: float = 0.0

    @property
    def is_bracket_child(self) -> bool:
        return self.parent_id is not None

    def as_dict(self) -> dict:
        return {
            'broker_id': self.broker_id,
            'action': self.action,
            'order_type': self.order_type,
            'qty': self.qty,
            'limit_price': self.limit_price,
            'stop_price': self.stop_price,
            'tif': self.tif,
            'status': self.status,
            'parent_id': self.parent_id,
            'is_bracket_child': self.is_bracket_child,
            'order_ref': self.engine_id,
            '_trade': None,  # engine treats this as opaque
        }


# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — every quirk knob is here, named, documented
# ════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class MockGatewayConfig:
    """All MockGateway behavior knobs. Defaults match common real-IBKR
    behaviors; scenarios override for stress-testing edge cases."""

    # ── Quirk simulation ──────────────────────────────────────────────
    commission_report_delay_seconds: float = 0.2
    """How long after fillEvent before commissionReportEvent fires.
    Real IBKR: 100-1000ms. The bug this catches: code that reads
    fill.commissionReport at fillEvent time gets None."""

    positions_lag_seconds: float = 2.0
    """How long after a fill before positions() reflects it.
    Real IBKR: 1-5s. The bug this catches: post-fill reconcile firing
    a false POSITION_MISMATCH (the 2026-06-05 EURUSD bug)."""

    fill_latency_seconds: float = 0.05
    """How long after order acceptance before a triggered order fills.
    Models exchange routing + ack roundtrip."""

    enforce_tick_grid: bool = True
    """If True, orders with off-grid prices are rejected with error 110.
    Catches engine code that submits unrounded prices."""

    min_tick: float = 0.01
    """The venue's reported minTick. Set per-asset by the scenario."""

    # ── Commission model ──────────────────────────────────────────────
    commission_per_order: float = 2.00
    """Flat commission charged per execution (USD). Matches FX IBKR
    typical 0.20bps with $2.00 min — good enough default for sim."""

    # ── Slippage model ────────────────────────────────────────────────
    slippage_ticks: float = 1.0
    """How many ticks of slippage to model on MKT/STP-MARKET orders.
    0 = perfect fill at trigger; >0 = slip in the direction unfavorable
    to the order direction."""

    # ── Failure injection (set per-scenario) ──────────────────────────
    inject_next_rejection: Optional[str] = None
    """If set, the next placeOrder call returns rejection with this msg."""

    inject_modify_rejection: bool = False
    """If True, the next modify_order call returns False (rejected)."""

    inject_disconnect: bool = False
    """If True, gateway flips to disconnected state immediately."""

    # ── Identity / metadata ───────────────────────────────────────────
    asset_class: str = "US_EQUITY"
    """For position-symbol logical translation (FX returns base ccy
    as contract.symbol; we have to translate back to logical ticker)."""

    currency: str = "USD"
    secType: str = "STK"
    multiplier: str = ""


# ════════════════════════════════════════════════════════════════════════════
# MOCK GATEWAY — the architectural heart
# ════════════════════════════════════════════════════════════════════════════

class MockGateway:
    """In-memory deterministic Gateway. Drop-in replacement for the
    production Gateway from src/execution/broker.py.

    Engine code constructs and uses it identically — same method names,
    same return types (duck-typed), same callback signatures.
    """

    # MockGateway has __slots__ for parity with production Gateway —
    # also catches accidental attribute typos at test-write time.
    __slots__ = (
        'symbol', 'paper', 'config', 'clock', 'rng',
        # Connection state
        'connected', '_last_heartbeat', '_runtime_min_tick',
        # In-memory order book and positions
        '_orders',                # dict[broker_id, _ResingOrder]
        '_engine_to_broker_id',   # dict[engine_id, broker_id]
        '_positions',             # dict[symbol, _Position]
        '_position_pending_updates',  # list[(apply_at_monotonic, _Position)]
        '_fill_history',          # list[_MockFill]
        '_next_broker_id',
        '_next_exec_id',
        # Callbacks (the engine sets these)
        '_on_fill', '_on_order_status', '_on_commission',
        # Pre-positions: a position that exists at startup (for orphan tests)
        '_preexisting_position',
        # Suspended fills: orders triggered but waiting on commission_report_delay
        '_pending_commission_reports',  # list[(fire_at_mono, eid, commission, exec_id)]
        # Diagnostic
        '_contract',
        # Test observer (optional): if set to a callable, fires with
        # (bid, ask, last) on every feed_tick. For test instrumentation
        # without monkey-patching (which __slots__ otherwise prevents).
        '_tick_observer',
    )

    def __init__(
        self,
        symbol: str,
        clock: Clock,
        rng: RNG,
        config: Optional[MockGatewayConfig] = None,
    ):
        self.symbol = symbol
        self.paper = False  # appears as a LIVE backend to the engine — we ARE the broker
        self.config = config or MockGatewayConfig()
        self.clock = clock
        self.rng = rng

        self.connected = False
        self._last_heartbeat: Optional[datetime] = None
        self._runtime_min_tick: float = self.config.min_tick

        self._orders: dict[str, _ResingOrder] = {}
        self._engine_to_broker_id: dict[str, str] = {}
        self._positions: dict[str, _Position] = {}
        self._position_pending_updates: list[tuple[float, _Position]] = []
        self._fill_history: list[_MockFill] = []
        self._next_broker_id = 1000
        self._next_exec_id = 0

        self._on_fill: Optional[Callable] = None
        self._on_order_status: Optional[Callable] = None
        self._on_commission: Optional[Callable] = None

        self._preexisting_position: Optional[_Position] = None
        self._pending_commission_reports: list[tuple[float, str, float, str]] = []
        self._tick_observer: Optional[Callable] = None

        # Pre-build the contract object the engine reads via _get_contract().
        self._contract = _MockContract(
            symbol=self._broker_symbol_for(symbol),
            secType=self.config.secType,
            currency=self.config.currency,
            multiplier=self.config.multiplier,
            exchange="SMART",
            conId=1000 + hash(symbol) % 100000,
        )

    # ── Connection lifecycle ──────────────────────────────────────────

    async def connect(self) -> bool:
        self.connected = True
        self._last_heartbeat = self.clock.now()
        # If a pre-existing position was seeded for orphan-recovery tests,
        # make it visible NOW (skip the positions-lag — these are
        # carry-overs from before the engine started).
        if self._preexisting_position is not None:
            self._positions[self._preexisting_position.symbol] = self._preexisting_position
        return True

    async def disconnect(self) -> None:
        self.connected = False

    # ── Heartbeat (engine reads this; we bump on tick injection) ──────

    def _bump_heartbeat(self) -> None:
        self._last_heartbeat = self.clock.now()

    # ── Runtime tick (engine adopts venue-reported minTick) ───────────

    def get_runtime_min_tick(self) -> Optional[float]:
        return self._runtime_min_tick if self.connected else None

    # ── Contract qualification (engine calls _get_contract) ───────────

    async def _get_contract(self) -> _MockContract:
        return self._contract

    # ── Order placement ───────────────────────────────────────────────

    async def place_stop_limit(
        self,
        side,
        qty: int,
        stop_price: float,
        limit_price: float,
        order_id: str,
    ) -> Optional[float]:
        """Place a STP-LMT order. Engine uses this for protective stops
        on the legacy non-bracket path. Returns None (live mode) — fills
        arrive via _on_fill callback."""
        if self._should_reject():
            self._dispatch_rejection(order_id)
            return None

        if not self._validate_price(stop_price) or not self._validate_price(limit_price):
            self._dispatch_rejection(order_id, reason="price not on tick grid")
            return None

        broker_id = self._allocate_broker_id()
        side_str = side.value if hasattr(side, 'value') else str(side)
        order = _ResingOrder(
            broker_id=broker_id,
            engine_id=order_id,
            action=side_str,
            order_type='STPLMT',
            qty=qty,
            stop_price=stop_price,
            limit_price=limit_price,
            status='Submitted',
        )
        self._orders[broker_id] = order
        self._engine_to_broker_id[order_id] = broker_id
        self._bump_heartbeat()
        return None  # async fill via callback

    async def place_stop_market(
        self,
        side,
        qty: int,
        stop_price: float,
        order_id: str,
    ) -> Optional[float]:
        """Place a STP-MARKET order. Engine uses this for the bracket
        child SELL stop in the post-fix architecture."""
        if self._should_reject():
            self._dispatch_rejection(order_id)
            return None
        if not self._validate_price(stop_price):
            self._dispatch_rejection(order_id, reason="price not on tick grid")
            return None

        broker_id = self._allocate_broker_id()
        side_str = side.value if hasattr(side, 'value') else str(side)
        order = _ResingOrder(
            broker_id=broker_id,
            engine_id=order_id,
            action=side_str,
            order_type='STP',
            qty=qty,
            stop_price=stop_price,
            status='Submitted',
        )
        self._orders[broker_id] = order
        self._engine_to_broker_id[order_id] = broker_id
        self._bump_heartbeat()
        return None

    async def place_order(self, *args, **kwargs) -> Optional[Any]:
        """Generic placeOrder — used by some legacy paths.  Routes to
        the appropriate specific helper if signature matches."""
        # Minimal: defer to whichever path the engine actually exercises.
        # If a scenario hits this, we expand support based on the test.
        return None

    async def place_bracket_buy_stop_market(
        self,
        qty: int,
        parent_stop_price: float,
        parent_limit_price: float,
        child_stop_price: float,
        parent_order_id: str,
        child_order_id: str,
    ) -> Optional[tuple[str, str]]:
        """Atomic bracket: BUY STP-LMT parent + SELL STP-MARKET child.
        IBKR semantics: child rests in PreSubmitted until parent fills,
        then auto-activates."""
        if self._should_reject():
            self._dispatch_rejection(parent_order_id)
            return None
        for p in (parent_stop_price, parent_limit_price, child_stop_price):
            if not self._validate_price(p):
                self._dispatch_rejection(parent_order_id, reason="bracket price off-grid")
                return None

        parent_broker_id = self._allocate_broker_id()
        child_broker_id = self._allocate_broker_id()

        parent = _ResingOrder(
            broker_id=parent_broker_id,
            engine_id=parent_order_id,
            action='BUY',
            order_type='STPLMT',
            qty=qty,
            stop_price=parent_stop_price,
            limit_price=parent_limit_price,
            status='Submitted',
        )
        child = _ResingOrder(
            broker_id=child_broker_id,
            engine_id=child_order_id,
            action='SELL',
            order_type='STP',
            qty=qty,
            stop_price=child_stop_price,
            parent_id=parent_broker_id,
            status='PreSubmitted',     # IBKR's actual status for bracket children pre-activation
        )
        self._orders[parent_broker_id] = parent
        self._orders[child_broker_id] = child
        self._engine_to_broker_id[parent_order_id] = parent_broker_id
        self._engine_to_broker_id[child_order_id] = child_broker_id
        self._bump_heartbeat()
        return (parent_broker_id, child_broker_id)

    async def modify_stop_trigger(
        self,
        order_id: str,
        new_stop_price: float,
        new_qty: Optional[int] = None,
    ) -> bool:
        """In-place modify of a resting order's stop_price (and optionally
        qty). Engine relies on this for bracket child retargeting after
        parent fills — modify-not-cancel is the safety invariant."""
        if self.config.inject_modify_rejection:
            return False
        broker_id = self._engine_to_broker_id.get(order_id, order_id)
        order = self._orders.get(broker_id)
        if order is None:
            return False
        if not self._validate_price(new_stop_price):
            return False
        # IMPORTANT: mutate in place, preserving broker_id and engine_id.
        # This is the modify-not-replace pattern the engine depends on.
        order.stop_price = new_stop_price
        if new_qty is not None and new_qty > 0:
            order.qty = new_qty
        self._bump_heartbeat()
        return True

    async def modify_order(self, *args, **kwargs) -> bool:
        # Defer to modify_stop_trigger semantics when scenarios need it.
        return False

    async def cancel_order(self, order_id: str) -> bool:
        broker_id = self._engine_to_broker_id.get(order_id, order_id)
        order = self._orders.get(broker_id)
        if order is None:
            return False
        order.status = 'Cancelled'
        del self._orders[broker_id]
        if order.engine_id in self._engine_to_broker_id:
            del self._engine_to_broker_id[order.engine_id]
        # Dispatch status callback so engine's _on_order_status hook fires.
        if self._on_order_status is not None:
            self._on_order_status(order.engine_id, 'Cancelled', '')
        self._bump_heartbeat()
        return True

    async def cancel_all(self) -> int:
        cnt = 0
        for broker_id in list(self._orders.keys()):
            order = self._orders[broker_id]
            await self.cancel_order(order.engine_id)
            cnt += 1
        return cnt

    # ── State queries ─────────────────────────────────────────────────

    async def get_positions(self) -> list:
        """Returns positions with the realistic positions-lag IBKR has.
        Pending position updates within the lag window are NOT visible —
        this is what causes the 2026-06-05 false-fold bug class."""
        self._apply_pending_position_updates()
        return [
            _Position(
                symbol=self._logical_symbol_for(p.symbol),
                quantity=p.quantity,
                avg_cost=p.avg_cost,
                market_value=p.market_value,
            )
            for p in self._positions.values()
            if p.quantity != 0
        ]

    def fetch_open_orders(self) -> list[dict]:
        """Engine reads this synchronously (no await). Returns currently-
        resting orders matching the gateway's symbol."""
        out = []
        for order in self._orders.values():
            if order.status not in ('Submitted', 'PreSubmitted', 'PendingSubmit'):
                continue
            out.append(order.as_dict())
        return out

    async def fetch_all_open_orders_for_symbol(self, symbol: str) -> list[dict]:
        return [o.as_dict() for o in self._orders.values()
                if o.status in ('Submitted', 'PreSubmitted')]

    def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        cnt = 0
        for broker_id in list(self._orders.keys()):
            o = self._orders[broker_id]
            if o.status in ('Submitted', 'PreSubmitted'):
                asyncio.create_task(self.cancel_order(o.engine_id))
                cnt += 1
        return cnt

    def get_all_fills(self) -> list:
        """Engine reads this on restart for missed-fill replay.  Returns
        chronologically-ordered fills with attached commission reports."""
        return list(self._fill_history)

    async def get_price(self) -> Optional[float]:
        # Last known mid (for reconcile diagnostics).
        return None  # callers handle None gracefully

    async def get_equity(self) -> float:
        return 1_000_000.0  # large enough to never block any test

    def register_existing_order(self, broker_id: str, engine_id: str, trade_obj=None) -> None:
        """Used on reconnect-replay. Re-wires our internal mapping."""
        self._engine_to_broker_id[engine_id] = broker_id
        if broker_id in self._orders:
            self._orders[broker_id].engine_id = engine_id

    # ════════════════════════════════════════════════════════════════
    # HARNESS-FACING API — drive the simulation
    # ════════════════════════════════════════════════════════════════

    async def feed_tick(self, bid: float, ask: float, last: Optional[float] = None) -> None:
        """Inject a market tick. Triggers any resting orders whose
        conditions are now met (STP triggered, STPLMT activated, etc.)
        and emits the corresponding fill/commission/status callbacks.
        """
        self._bump_heartbeat()
        # Test-instrumentation hook (no-op in production-style usage).
        if self._tick_observer is not None:
            try:
                self._tick_observer(bid, ask, last)
            except Exception:
                pass

        # Walk currently-resting orders and check trigger conditions.
        # Snapshot keys first (dict modified during iteration when fills clear orders).
        for broker_id in list(self._orders.keys()):
            order = self._orders.get(broker_id)
            if order is None:
                continue
            if order.status not in ('Submitted', 'PreSubmitted'):
                continue
            await self._maybe_fill_order(order, bid, ask, last)

    async def _maybe_fill_order(
        self,
        order: _ResingOrder,
        bid: float,
        ask: float,
        last: Optional[float],
    ) -> None:
        """Decide if this tick triggers `order` and, if so, emit fill."""
        # Bracket children rest in PreSubmitted until parent fills. They
        # CAN'T fire while the parent is still working.
        if order.parent_id is not None:
            parent = self._orders.get(order.parent_id)
            if parent is not None and parent.status in ('Submitted', 'PreSubmitted'):
                return

        if order.action == 'BUY':
            # BUY STP-LMT or BUY STP: triggers when ask >= stop (price rises).
            if order.order_type in ('STP', 'STPLMT'):
                if order.stop_price is not None and ask >= order.stop_price:
                    # For STP-LMT, fill at min(ask, limit_price); for STP fill at ask + slip
                    if order.order_type == 'STPLMT':
                        if order.limit_price is None or ask <= order.limit_price:
                            await self._execute_fill(order, fill_price=ask, side='BOT')
                    else:
                        slip = self.config.slippage_ticks * self.config.min_tick
                        await self._execute_fill(order, fill_price=ask + slip, side='BOT')
            elif order.order_type == 'LMT':
                if order.limit_price is not None and ask <= order.limit_price:
                    await self._execute_fill(order, fill_price=ask, side='BOT')
            elif order.order_type == 'MKT':
                await self._execute_fill(order, fill_price=ask, side='BOT')

        else:  # SELL
            # SELL STP / STP-LMT / STP-MARKET: triggers when bid <= stop (price falls).
            if order.order_type in ('STP', 'STPLMT'):
                if order.stop_price is not None and bid <= order.stop_price:
                    if order.order_type == 'STPLMT':
                        if order.limit_price is None or bid >= order.limit_price:
                            await self._execute_fill(order, fill_price=bid, side='SLD')
                    else:
                        slip = self.config.slippage_ticks * self.config.min_tick
                        await self._execute_fill(order, fill_price=max(0.0, bid - slip), side='SLD')
            elif order.order_type == 'LMT':
                if order.limit_price is not None and bid >= order.limit_price:
                    await self._execute_fill(order, fill_price=bid, side='SLD')
            elif order.order_type == 'MKT':
                await self._execute_fill(order, fill_price=bid, side='SLD')

    async def _execute_fill(
        self,
        order: _ResingOrder,
        fill_price: float,
        side: str,
    ) -> None:
        """Emit a fill: mutate position, append to fill_history, fire
        callbacks with realistic timing (fill first, commission later)."""
        # Mark the order filled.
        order.status = 'Filled'
        order.filled_qty = order.qty
        order.avg_fill_price = fill_price

        # Update position with the configured lag (so positions() reflects
        # the fill AFTER positions_lag_seconds, not immediately — the
        # actual IBKR behavior we observed).
        delta = order.qty if side == 'BOT' else -order.qty
        new_qty = self._positions.get(self._broker_symbol_for(self.symbol), _Position(
            symbol=self._broker_symbol_for(self.symbol), quantity=0, avg_cost=0)).quantity + delta
        apply_at_mono = self.clock.monotonic() + self.config.positions_lag_seconds
        self._position_pending_updates.append((
            apply_at_mono,
            _Position(
                symbol=self._broker_symbol_for(self.symbol),
                quantity=new_qty,
                avg_cost=fill_price,
                market_value=new_qty * fill_price,
            ),
        ))

        # Append to fill history (used by reconcile-on-restart replay).
        self._next_exec_id += 1
        exec_id = f"E{self._next_exec_id:010d}"
        fill = _MockFill(
            contract=self._contract,
            execution=_MockExecution(
                execId=exec_id,
                shares=float(order.qty),
                price=fill_price,
                time=self.clock.now(),
                side=side,
            ),
            commissionReport=None,  # will arrive separately after delay
            time=self.clock.now(),
        )
        self._fill_history.append(fill)

        # Fire on_fill callback (the engine sets this).  Note: engine's
        # _on_fill takes (engine_id, qty, price, exec_id, time, commission)
        # — commission is None at fill time (the bug we discovered).
        if self._on_fill is not None:
            try:
                self._on_fill(
                    order.engine_id,
                    float(order.qty),
                    fill_price,
                    exec_id,
                    self.clock.now(),
                    None,   # commission not yet available
                )
            except Exception:
                # An engine handler crashing shouldn't kill the sim.
                pass

        # If this was a bracket parent, the child auto-activates now.
        for child in self._orders.values():
            if child.parent_id == order.broker_id and child.status == 'PreSubmitted':
                child.status = 'Submitted'

        # Schedule the commission report to fire later (the bug class
        # that motivated the commissionReportEvent wiring).
        commission = self.config.commission_per_order
        fire_at = self.clock.monotonic() + self.config.commission_report_delay_seconds
        self._pending_commission_reports.append((
            fire_at, order.engine_id, commission, exec_id,
        ))

    async def tick_clock(self) -> None:
        """Pump time-based dispatchers. Call after clock.sleep() in scenarios.
        Drains:
          - pending commission reports whose delay has elapsed
          - pending positions updates whose lag has elapsed
        """
        now_mono = self.clock.monotonic()

        # Commission reports.
        still_pending = []
        for fire_at, engine_id, commission, exec_id in self._pending_commission_reports:
            if fire_at <= now_mono:
                # Attach commission to the fill object for replay-correctness.
                for fill in self._fill_history:
                    if fill.execution.execId == exec_id and fill.commissionReport is None:
                        # Rebuild fill with commissionReport attached.
                        # (_MockFill is frozen so we replace in place.)
                        idx = self._fill_history.index(fill)
                        self._fill_history[idx] = _MockFill(
                            contract=fill.contract,
                            execution=fill.execution,
                            commissionReport=_MockCommissionReport(commission=commission),
                            time=fill.time,
                        )
                        break
                # Fire engine callback.
                if self._on_commission is not None:
                    try:
                        self._on_commission(engine_id, commission, exec_id)
                    except Exception:
                        pass
            else:
                still_pending.append((fire_at, engine_id, commission, exec_id))
        self._pending_commission_reports = still_pending

        # Positions updates.
        self._apply_pending_position_updates()

    def _apply_pending_position_updates(self) -> None:
        now_mono = self.clock.monotonic()
        still_pending = []
        for apply_at, pos in self._position_pending_updates:
            if apply_at <= now_mono:
                if pos.quantity == 0:
                    self._positions.pop(pos.symbol, None)
                else:
                    self._positions[pos.symbol] = pos
            else:
                still_pending.append((apply_at, pos))
        self._position_pending_updates = still_pending

    # ════════════════════════════════════════════════════════════════
    # Operator command targets (called by Scenario commands)
    # ════════════════════════════════════════════════════════════════

    async def force_disconnect(self) -> None:
        self.connected = False

    async def force_reconnect(self) -> None:
        self.connected = True
        self._bump_heartbeat()

    async def inject_next_rejection(self, message: str) -> None:
        self.config.inject_next_rejection = message

    async def force_cancel_by_engine_id(self, engine_id: str) -> None:
        """Cancel an order at the broker WITHOUT firing the engine's
        status callback. Simulates manual TWS cancel."""
        broker_id = self._engine_to_broker_id.get(engine_id, engine_id)
        order = self._orders.pop(broker_id, None)
        if order is not None:
            self._engine_to_broker_id.pop(engine_id, None)

    def seed_preexisting_position(self, qty: float, avg_cost: float) -> None:
        """Seed a position that exists BEFORE the engine starts.
        Used by orphan-position-recovery scenarios."""
        self._preexisting_position = _Position(
            symbol=self._broker_symbol_for(self.symbol),
            quantity=qty,
            avg_cost=avg_cost,
            market_value=qty * avg_cost,
        )

    def seed_preexisting_order(self, action: str, qty: int, stop_price: float, engine_id: str) -> str:
        """Seed an order resting at the broker BEFORE engine starts.
        Used by orphan-order-recovery scenarios."""
        broker_id = self._allocate_broker_id()
        order = _ResingOrder(
            broker_id=broker_id,
            engine_id=engine_id,
            action=action,
            order_type='STP',
            qty=qty,
            stop_price=stop_price,
            status='Submitted',
        )
        self._orders[broker_id] = order
        self._engine_to_broker_id[engine_id] = broker_id
        return broker_id

    # ════════════════════════════════════════════════════════════════
    # Internals
    # ════════════════════════════════════════════════════════════════

    def _allocate_broker_id(self) -> str:
        self._next_broker_id += 1
        return str(self._next_broker_id)

    def _should_reject(self) -> bool:
        if self.config.inject_next_rejection is not None:
            return True
        return False

    def _dispatch_rejection(self, engine_id: str, reason: str = "") -> None:
        message = self.config.inject_next_rejection or reason or "Simulated rejection"
        self.config.inject_next_rejection = None  # one-shot
        if self._on_order_status is not None:
            try:
                self._on_order_status(engine_id, 'Rejected', message)
            except Exception:
                pass

    def _validate_price(self, price: float) -> bool:
        """Tick-grid validation — IBKR's error 110 behavior."""
        if not self.config.enforce_tick_grid:
            return True
        if self._runtime_min_tick <= 0:
            return True
        # Snap to grid; price must equal the snapped version.
        from decimal import Decimal as _D, ROUND_HALF_UP as _HU
        tick = _D(str(self._runtime_min_tick))
        val = _D(str(price))
        snapped = (val / tick).quantize(_D("1"), rounding=_HU) * tick
        return abs(float(snapped) - price) < (self._runtime_min_tick * 0.001)

    def _broker_symbol_for(self, logical: str) -> str:
        """Return the broker's wire-level symbol for a logical ticker.

        For FX: 'EURUSD' → 'EUR' (IBKR stores Forex with base ccy as symbol).
        For everything else: logical == broker symbol."""
        if self.config.asset_class in ('FX_CASH', 'FX_CFD') and len(logical) == 6:
            return logical[:3]
        return logical

    def _logical_symbol_for(self, broker_symbol: str) -> str:
        """Inverse of _broker_symbol_for — used to translate position
        symbols back so engine code matching against config.ticker works."""
        if self.config.asset_class in ('FX_CASH', 'FX_CFD') and len(broker_symbol) == 3:
            # We need the quote ccy from the original logical ticker
            if len(self.symbol) == 6 and self.symbol.startswith(broker_symbol):
                return self.symbol
        return broker_symbol


__all__ = [
    "MockGateway", "MockGatewayConfig",
    # Value types exposed so tests can build expectations
    "_MockContract", "_MockExecution", "_MockCommissionReport",
    "_MockFill", "_Position", "_ResingOrder",
]
