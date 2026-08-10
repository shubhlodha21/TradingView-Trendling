"""
GT System - Order Manager

Senior quant grade order management layer.
Separates order logic from execution for clean architecture.

Design:
- Order lifecycle management (signal → submission → fill)
- Stop-limit order support
- Slippage tracking per order
- Non-blocking (async-first)

Usage:
    om = OrderManager(broker, audit)

    # Entry: MARKET or LIMIT
    order = await om.entry_order(side=BUY, qty=100, signal_price=226.50)

    # Exit: STOP_LIMIT
    order = await om.stop_order(side=SELL, qty=100,
                                 trigger_price=226.30,
                                 stop_offset=0.10)  # Limit = trigger - 0.10

    # Cancel all
    await om.cancel_all()
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from enum import Enum

from src.config.models import OrderRecord, OrderSide, OrderType, OrderStatus

if TYPE_CHECKING:
    from src.execution.broker import Gateway


class OrderEvent(Enum):
    """Order lifecycle events for audit."""
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(slots=True)
class OrderRequest:
    """Immutable order request - senior quant grade."""
    order_id: str
    symbol: str
    side: OrderSide
    qty: int
    order_type: OrderType
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None      # Trigger for STOP orders
    signal_price: Optional[float] = None    # LTP when signal generated
    algo: str = "MANUAL"                   # Order origin (manual/algo)
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        # Validate
        if self.order_type == OrderType.LIMIT and not self.limit_price:
            raise ValueError("LIMIT order requires limit_price")
        if self.order_type == OrderType.STOP and not self.stop_price:
            raise ValueError("STOP order requires stop_price")
        if self.order_type == OrderType.STOP_LIMIT:
            if not self.stop_price:
                raise ValueError("STOP_LIMIT requires stop_price")
            if not self.limit_price:
                raise ValueError("STOP_LIMIT requires limit_price")


class OrderManager:
    """
    Senior quant order management.

    Responsibilities:
    1. Generate order IDs
    2. Build OrderRecords from requests
    3. Track order lifecycle
    4. Calculate slippage metrics
    5. Audit all order events

    Does NOT:
    - Execute orders (Gateway handles that)
    - Manage positions (Engine handles that)
    """

    __slots__ = (
        'broker', 'audit', '_order_registry', '_ts',
        '_order_counter', '_symbol',
    )

    def __init__(self, broker: "Gateway", audit=None):
        self.broker = broker
        self.audit = audit
        self._order_registry: dict[str, OrderRecord] = {}
        self._ts = datetime.now
        self._order_counter = 0
        self._symbol = broker.symbol if broker else ""

    def _next_id(self, prefix: str) -> str:
        """Generate unique order ID."""
        self._order_counter += 1
        return f"{prefix}_{self._order_counter}_{self._symbol}"

    # === Order Request Builders ===

    def market_entry(self, side: OrderSide, qty: int, signal_price: float) -> OrderRequest:
        """
        Market order for entry.
        Fastest execution, accepts market slippage.
        """
        return OrderRequest(
            order_id=self._next_id("MKT"),
            symbol=self._symbol,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            signal_price=signal_price,
            algo="BREAKOUT",
        )

    def limit_entry(self, side: OrderSide, qty: int, limit_price: float, signal_price: float) -> OrderRequest:
        """
        Limit order for entry.
        Price certainty, no slippage, but might not fill.
        """
        return OrderRequest(
            order_id=self._next_id("LMT"),
            symbol=self._symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            signal_price=signal_price,
            algo="BREAKOUT",
        )

    def stop_limit_exit(
        self,
        side: OrderSide,
        qty: int,
        entry_price: float,
        stop_pct: float = 0.001,
        trigger_offset: float = 0.05,
    ) -> OrderRequest:
        """
        Stop-Limit order for exit.

        Args:
            side: SELL (for long exit)
            qty: Position size
            entry_price: Where we entered
            stop_pct: Stop % below entry (e.g., 0.001 = 0.1%)
            trigger_offset: Cents above trigger for limit (slippage buffer)

        Example:
            entry=226.50, stop_pct=0.001, trigger_offset=0.05
            → trigger @ 226.27 (entry * (1 - 0.001))
            → limit @ 226.22 (trigger - 0.05)
        """
        trigger = round(entry_price * (1 - stop_pct), 2)
        limit = round(trigger - trigger_offset, 2)

        return OrderRequest(
            order_id=self._next_id("SL"),
            symbol=self._symbol,
            side=side,
            qty=qty,
            order_type=OrderType.STOP_LIMIT,
            stop_price=trigger,
            limit_price=limit,
            signal_price=trigger,
            algo="STOP_LOSS",
        )

    def limit_reentry(self, side: OrderSide, qty: int, breakout_price: float, signal_price: float) -> OrderRequest:
        """
        Limit order for re-entry at breakout level.

        Args:
            side: BUY
            qty: Position size
            breakout_price: Previous high (re-entry trigger)
            signal_price: Current LTP
        """
        return OrderRequest(
            order_id=self._next_id("RE"),
            symbol=self._symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=breakout_price,
            signal_price=signal_price,
            algo="REENTRY",
        )

    # === Order Execution ===

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        """
        Execute order request via broker.

        Returns OrderRecord with filled data (or pending for async fills).
        """
        # Build OrderRecord
        record = OrderRecord(
            order_id=request.order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=request.signal_price,
        )

        # Track in registry
        self._order_registry[request.order_id] = record

        # Audit: SUBMITTED
        if self.audit:
            self.audit.log_order(
                event="SUBMITTED",
                order_id=record.order_id,
                side=record.side.value,
                qty=record.qty,
                signal_price=record.signal_price,
                state_at_time="ORDER_ENTRY",
                position_at_time="FLAT",
            )

        # Execute via broker
        if request.order_type == OrderType.STOP_LIMIT:
            # For stop-limit, we need special handling
            # In paper mode, simulate stop-limit behavior
            # In live mode, pass to IBKR
            filled_price = await self.broker.place_stop_limit(
                side=request.side,
                qty=request.qty,
                stop_price=request.stop_price,
                limit_price=request.limit_price,
                order_id=request.order_id,
            )
        else:
            filled_price = await self.broker.place_order(
                side=request.side,
                qty=request.qty,
                order_type=request.order_type,
                limit_price=request.limit_price,
            )

        # Update record on fill
        if filled_price:
            record.status = OrderStatus.FILLED
            record.filled_at = self._ts()
            record.avg_fill_price = filled_price
            record.filled_qty = request.qty

            # Audit: FILLED
            if self.audit:
                slippage = 0
                if record.signal_price:
                    slippage = filled_price - record.signal_price
                    if request.side == OrderSide.SELL:
                        slippage = -slippage

                self.audit.log_order(
                    event="FILLED",
                    order_id=record.order_id,
                    side=record.side.value,
                    qty=record.qty,
                    signal_price=record.signal_price,
                    fill_price=filled_price,
                    slippage=slippage,
                    commission=record.calculate_commission(),
                )

        return record

    async def cancel_all(self) -> int:
        """Cancel all pending orders. Returns count cancelled."""
        cancelled = 0
        for order_id, record in self._order_registry.items():
            if record.status == OrderStatus.SUBMITTED:
                await self.broker.cancel_order(order_id)
                record.status = OrderStatus.CANCELLED
                cancelled += 1

                if self.audit:
                    self.audit.log_order(
                        event="CANCELLED",
                        order_id=order_id,
                        side=record.side.value,
                        qty=record.qty,
                    )

        return cancelled

    # === Query ===

    def get_order(self, order_id: str) -> Optional[OrderRecord]:
        """Get order by ID."""
        return self._order_registry.get(order_id)

    def get_pending_orders(self) -> list[OrderRecord]:
        """Get all pending (non-terminal) orders."""
        return [
            o for o in self._order_registry.values()
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)
        ]

    def get_filled_orders(self) -> list[OrderRecord]:
        """Get all filled orders."""
        return [
            o for o in self._order_registry.values()
            if o.status == OrderStatus.FILLED
        ]

    @property
    def stats(self) -> dict:
        """Order statistics."""
        total = len(self._order_registry)
        filled = len([o for o in self._order_registry.values() if o.status == OrderStatus.FILLED])
        pending = len([o for o in self._order_registry.values() if o.status == OrderStatus.SUBMITTED])

        return {
            "total_orders": total,
            "filled": filled,
            "pending": pending,
            "cancelled": len([o for o in self._order_registry.values() if o.status == OrderStatus.CANCELLED]),
        }
