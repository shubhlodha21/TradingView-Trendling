"""
Parallel Structured Logging for Quant Trading

Non-blocking async logging with:
- Buffer flush every 100ms or 50 records
- Fire-and-forget emit (doesn't block engine)
- High/low frequency log separation
- ELK-compatible JSON output

Architecture:
    Engine → emit() → buffer → asyncio task → stdout/file
"""
import asyncio
import json
import sys
from collections import deque
from datetime import datetime
from typing import Optional, TextIO
from enum import Enum


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class QuantLogger:
    """
    Non-blocking structured JSON logger.

    Uses a deque buffer + async flush task so logging never blocks the engine.
    Logs are written in batches every 100ms or when buffer reaches 50 records.
    """

    __slots__ = (
        'output', 'trade_cycle_id', 'buffer_size', 'flush_interval',
        '_ts', '_order_latencies', '_buffer', '_lock', '_flush_task', '_running',
        # Overflow diagnostics. deque(maxlen=...) silently evicts the oldest
        # entry when full — for an audit log that's bad. Now we count it
        # and emit a throttled warning so a stuck flush task is visible.
        '_dropped', '_last_drop_warn',
    )

    def __init__(
        self,
        output: TextIO = sys.stdout,
        trade_cycle_id: str = "",
        buffer_size: int = 50,
        flush_interval_ms: int = 100,
    ):
        self.output = output
        self.trade_cycle_id = trade_cycle_id
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval_ms / 1000.0

        # Pre-cached timestamp function
        self._ts = datetime.now

        # Order latency tracking (sync, fast)
        self._order_latencies: dict[str, float] = {}

        # Async buffer
        self._buffer: deque[str] = deque(maxlen=buffer_size * 2)
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        # Drop counter — set whenever _emit observes the deque was already
        # at maxlen before its append (meaning the oldest entry was evicted).
        self._dropped: int = 0
        self._last_drop_warn: float = 0.0

    def start(self):
        """Start the async flush task. Call once at startup."""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self):
        """Stop and flush remaining logs. Call at shutdown."""
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def _flush_loop(self):
        """Background task that flushes buffer periodically."""
        while self._running:
            await asyncio.sleep(self.flush_interval)
            await self._flush()

    async def _flush(self):
        """Flush buffer to output."""
        if not self._buffer:
            return

        async with self._lock:
            lines = list(self._buffer)
            self._buffer.clear()

        if lines:
            self.output.write('\n'.join(lines) + '\n')
            self.output.flush()

    async def _emit(self, level: LogLevel, event: str, **kwargs):
        """Async emit - adds to buffer without blocking.

        Detects deque overflow by comparing length to maxlen before append.
        On overflow the oldest line is silently evicted by deque itself — we
        record the count so the engine can surface it on shutdown and emit
        a rate-limited warning so a stuck flush task is visible immediately.
        """
        record = {
            "ts": self._ts().isoformat(),
            "level": level.value,
            "event": event,
            "cycle_id": self.trade_cycle_id,
            **kwargs
        }
        line = json.dumps(record)

        async with self._lock:
            if len(self._buffer) >= self._buffer.maxlen:
                self._dropped += 1
                now = self._ts().timestamp()
                if now - self._last_drop_warn > 5.0:
                    self._last_drop_warn = now
                    sys.stderr.write(
                        f"[QuantLogger] buffer overflow — dropped={self._dropped} "
                        f"(maxlen={self._buffer.maxlen}); flush task may be stuck\n"
                    )
                    sys.stderr.flush()
            self._buffer.append(line)

    @property
    def dropped(self) -> int:
        """Total async log lines evicted due to buffer overflow."""
        return self._dropped

    # === Sync emit for critical logs (always immediate) ===
    def _emit_sync(self, level: LogLevel, event: str, **kwargs):
        """Sync emit - writes immediately. Use for errors/exits only."""
        record = {
            "ts": self._ts().isoformat(),
            "level": level.value,
            "event": event,
            "cycle_id": self.trade_cycle_id,
            **kwargs
        }
        print(json.dumps(record), file=self.output, flush=True)

    # === Sync methods for critical events (don't buffer) ===
    def log(self, event: str, **kwargs):
        """Sync log - immediate write. Use for trade events."""
        self._emit_sync(LogLevel.INFO, event, **kwargs)

    def debug(self, event: str, **kwargs):
        self._emit_sync(LogLevel.DEBUG, event, **kwargs)

    def warn(self, event: str, **kwargs):
        self._emit_sync(LogLevel.WARN, event, **kwargs)

    def error(self, event: str, **kwargs):
        self._emit_sync(LogLevel.ERROR, event, **kwargs)

    # === Async methods for high-frequency events (buffered) ===
    async def log_async(self, event: str, **kwargs):
        """Async log - buffered. Use for tick/high-frequency events."""
        await self._emit(LogLevel.INFO, event, **kwargs)

    async def debug_async(self, event: str, **kwargs):
        await self._emit(LogLevel.DEBUG, event, **kwargs)

    # === Order lifecycle events ===
    def order_submitted(self, order_id: str, symbol: str, side: str, qty: int,
                       order_type: str, limit_price: Optional[float] = None):
        """Sync - critical path, log immediately."""
        self._order_latencies[order_id] = self._ts().timestamp()
        self.log(
            "ORDER_SUBMITTED",
            order_id=order_id, symbol=symbol, side=side,
            qty=qty, order_type=order_type, limit_price=limit_price,
        )

    def order_filled(self, order_id: str, qty: int, price: float, commission: float = 0.0):
        """Sync - critical path, log immediately."""
        submit_time = self._order_latencies.get(order_id, 0)
        latency_ms = 0.0
        if submit_time:
            latency_ms = (self._ts().timestamp() - submit_time) * 1000
        self.log(
            "ORDER_FILLED",
            order_id=order_id, qty=qty, price=price,
            commission=commission, latency_ms=round(latency_ms, 2),
        )

    def order_cancelled(self, order_id: str):
        self.log("ORDER_CANCELLED", order_id=order_id)

    def order_rejected(self, order_id: str, reason: str):
        self.warn("ORDER_REJECTED", order_id=order_id, reason=reason)

    # === Strategy lifecycle events ===
    def strategy_started(self, ticker: str, trigger_price: float):
        self.log("STRATEGY_STARTED", ticker=ticker, trigger_price=trigger_price)

    def strategy_stopped(self, trades: int, pnl: float):
        self.log("STRATEGY_STOPPED", trades=trades, pnl=pnl)

    def strategy_state_change(self, from_state: str, to_state: str):
        self.log("STATE_CHANGE", from_state=from_state, to_state=to_state)

    # === Trade lifecycle events ===
    def trade_entry(self, price: float, qty: int, stop_loss: float, order_id: str = ""):
        self.log("TRADE_ENTRY", price=price, qty=qty, stop_loss=stop_loss, order_id=order_id)

    def trade_exit(self, price: float, pnl: float, reason: str, order_id: str = ""):
        self.log("TRADE_EXIT", price=price, pnl=pnl, reason=reason, order_id=order_id)

    def risk_rejected(self, reason: str, order_value: float = 0.0):
        self.warn("RISK_REJECTED", reason=reason, order_value=order_value)

    # === Connection events ===
    def connected(self, host: str, port: int):
        self.log("CONNECTED", host=host, port=port)

    def disconnected(self):
        self.log("DISCONNECTED")

    def connection_error(self, error: str):
        self.error("CONNECTION_ERROR", error=error)

    def set_cycle_id(self, cycle_id: str):
        self.trade_cycle_id = cycle_id
