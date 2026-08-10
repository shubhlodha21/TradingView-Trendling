"""
IBKR Connection Manager

Handles connection lifecycle, heartbeats, and automatic reconnection.
Uses asyncio for non-blocking operations.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from abc import ABC, abstractmethod


class ConnectionState(Enum):
    """Connection lifecycle states."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"


@dataclass(slots=True)
class ConnectionConfig:
    """Connection configuration parameters."""
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    readonly: bool = True
    request_timeout: float = 30.0
    heartbeat_interval: float = 5.0
    max_reconnect_attempts: int = 5
    initial_backoff: float = 1.0
    max_backoff: float = 10.0  # 1→2→4→8→10→10… — reconnects every 10s at steady state


class ConnectionObserver(ABC):
    """
    Observer interface for connection events.

    All methods have default no-op implementations for ergonomic usage.
    Subclass only the methods you care about.
    """

    def on_connect(self, connected_at: datetime) -> None:
        """Called when connection is established."""
        pass

    def on_disconnect(self, reason: Optional[str] = None) -> None:
        """Called when connection is lost or disconnected."""
        pass

    def on_reconnecting(self, attempt: int, max_attempts: int) -> None:
        """Called before each reconnection attempt."""
        pass

    def on_error(self, error: Exception) -> None:
        """Called when an error occurs."""
        pass

    def on_heartbeat(self, timestamp: datetime) -> None:
        """Called periodically when connection is healthy."""
        pass


class ConnectionManager:
    """
    Manages IBKR connection lifecycle with automatic reconnection.

    Features:
    - Connection state machine
    - Exponential backoff reconnection
    - Heartbeat monitoring
    - Observer pattern for events
    """

    __slots__ = (
        '_config', '_state', '_ib', '_connected_at', '_last_heartbeat',
        '_reconnect_attempts', '_running', '_heartbeat_task', '_observers', '_ts',
        # Supervisor-mode plumbing: callbacks the outer system provides so
        # ConnectionManager can re-establish the connection without owning
        # the IB() instance. Gateway stays the sole owner of `_ib`; we just
        # watch it for liveness and ask the caller to repair it.
        '_supervise_connect', '_supervise_on_reconnect',
    )

    def __init__(self, config: ConnectionConfig):
        self._config = config
        self._state = ConnectionState.DISCONNECTED
        self._ib = None
        self._connected_at: Optional[datetime] = None
        self._last_heartbeat: Optional[datetime] = None
        self._reconnect_attempts = 0
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._observers: list[ConnectionObserver] = []
        self._ts = datetime.now
        self._supervise_connect = None
        self._supervise_on_reconnect = None

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == ConnectionState.CONNECTED

    @property
    def ib(self):
        """Returns the ib_async IB instance if connected."""
        return self._ib

    @property
    def connected_at(self) -> Optional[datetime]:
        return self._connected_at

    @property
    def last_heartbeat(self) -> Optional[datetime]:
        return self._last_heartbeat

    @property
    def heartbeat_age_seconds(self) -> float:
        """Seconds since last heartbeat."""
        if not self._last_heartbeat:
            return float('inf')
        return (self._ts() - self._last_heartbeat).total_seconds()

    def add_observer(self, observer: ConnectionObserver):
        """Register an observer for connection events."""
        if observer not in self._observers:
            self._observers.append(observer)

    def remove_observer(self, observer: ConnectionObserver):
        """Unregister an observer."""
        if observer in self._observers:
            self._observers.remove(observer)

    async def connect(self) -> bool:
        """
        Establish connection to IBKR Gateway.
        Returns True if connected successfully.
        """
        if self._state == ConnectionState.CONNECTED:
            return True

        self._state = ConnectionState.CONNECTING
        self._setup_async_compat()

        try:
            from ib_async import IB

            self._ib = IB()
            self._ib.RequestTimeout = self._config.request_timeout

            # Connect with configured parameters
            self._ib.connect(
                host=self._config.host,
                port=self._config.port,
                clientId=self._config.client_id,
                readonly=self._config.readonly,
            )

            # Connection successful
            self._state = ConnectionState.CONNECTED
            self._connected_at = self._ts()
            self._last_heartbeat = self._connected_at
            self._reconnect_attempts = 0

            # Notify observers
            self._notify("connect", self._connected_at)

            # Start heartbeat monitoring
            self._running = True
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            return True

        except Exception as e:
            self._state = ConnectionState.FAILED
            self._notify("error", e)
            return False

    async def disconnect(self, reason: Optional[str] = None):
        """Gracefully disconnect from IBKR.

        Stops the heartbeat task properly: cancel then AWAIT until it
        unwinds, so it doesn't outlive disconnect() as an orphan.
        Previously we cancelled without awaiting, leaving Task-N pending
        through the rest of the shutdown sequence — that became the
        "Task was destroyed but it is pending" warnings the user saw.
        """
        self._running = False

        # Cancel + await the heartbeat task so it's truly gone before we proceed.
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        self._heartbeat_task = None

        # In supervisor mode we don't own _ib. In owner mode we do — disconnect it.
        if self._supervise_connect is None and self._ib and self._ib.isConnected():
            try:
                self._ib.disconnect()
            except Exception:
                pass
            self._ib = None

        self._state = ConnectionState.DISCONNECTED
        self._notify("disconnect", reason)

    async def reconnect(self) -> bool:
        """
        Attempt to reconnect with exponential backoff.
        Uses a lock to prevent concurrent reconnection attempts.
        """
        if self._state == ConnectionState.RECONNECTING:
            return False  # Already reconnecting

        self._state = ConnectionState.RECONNECTING

        while (self._config.max_reconnect_attempts <= 0 or
               self._reconnect_attempts < self._config.max_reconnect_attempts):
            self._reconnect_attempts += 1

            # Notify observers of reconnect attempt
            self._notify("reconnecting", self._reconnect_attempts, self._config.max_reconnect_attempts)

            # Calculate backoff delay
            exponent = min(self._reconnect_attempts - 1, 30)
            backoff = min(
                self._config.initial_backoff * (2 ** exponent),
                self._config.max_backoff
            )

            await asyncio.sleep(backoff)

            # Attempt connection
            if await self.connect():
                return True

        # All reconnection attempts failed
        self._state = ConnectionState.FAILED
        return False

    async def _heartbeat_loop(self):
        """
        Monitor connection health via heartbeats.
        Uses a consistent timestamp rather than calling datetime.now() repeatedly.

        Two modes:
            owner-mode  : `_ib` was created by ConnectionManager.connect().
                          On loss, calls `self.reconnect()` which spins a new
                          IB() inside this manager.
            supervisor-mode : `_ib` is owned externally (Gateway). On loss,
                          calls `_supervise_connect()` to re-establish via the
                          caller, then `_supervise_on_reconnect()` to let
                          run_live re-subscribe / reconcile.
        """
        interval = self._config.heartbeat_interval
        connected_state = ConnectionState.CONNECTED

        while self._running:
            await asyncio.sleep(interval)

            if not self._running:
                break

            # Check connection state once per iteration
            if self._state != connected_state:
                break

            # Check if ib is still connected
            ib = self._ib
            if ib and ib.isConnected():
                ts = self._ts()
                self._last_heartbeat = ts
                self._notify("heartbeat", ts)
            else:
                # Connection lost — branch on whether we own _ib or supervise it
                self._state = ConnectionState.RECONNECTING
                self._notify("disconnect", "Lost connection")
                if self._supervise_connect is not None:
                    asyncio.create_task(self._supervise_reconnect_loop())
                else:
                    asyncio.create_task(self.reconnect())
                break

    async def supervise(
        self,
        ib,
        on_connect_async,
        on_reconnect_async=None,
    ) -> None:
        """Watch an externally-owned IB instance and orchestrate reconnects.

        Use this when something else (typically `Gateway`) owns the `IB()`
        instance. ConnectionManager will:
            1. Poll `ib.isConnected()` every `heartbeat_interval` seconds.
            2. On loss, call `on_connect_async()` with exponential backoff
               (1s → 60s, up to `max_reconnect_attempts`). The callback is
               responsible for actually re-establishing the underlying
               connection (e.g. `gateway.connect()`).
            3. On a successful reconnect, call `on_reconnect_async()` if
               provided. This is where run_live.py re-subscribes to market
               data and tells the engine to re-reconcile resting orders.

        Args:
            ib: The externally-owned IB instance to monitor. Re-pointed to
                the live instance by the caller after each successful
                reconnect via `set_ib()`.
            on_connect_async: Async callable that re-establishes the
                connection. Must return True on success, False otherwise.
            on_reconnect_async: Optional async callable invoked after a
                successful reconnect. Use to re-attach feeds / reconcile
                resting orders.
        """
        self._ib = ib
        self._supervise_connect = on_connect_async
        self._supervise_on_reconnect = on_reconnect_async
        self._state = ConnectionState.CONNECTED
        self._connected_at = self._ts()
        self._last_heartbeat = self._connected_at
        self._reconnect_attempts = 0
        self._running = True
        self._notify("connect", self._connected_at)
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def set_ib(self, ib) -> None:
        """Replace the supervised IB reference (after a successful reconnect)."""
        self._ib = ib

    def request_repair(self, reason: str = "unspecified") -> bool:
        """Force the full supervisor repair even though the socket looks alive.

        The heartbeat loop only repairs when `ib.isConnected()` goes False.
        A DEAF connection — socket up, zero ticks — never trips that check,
        so the bot sat connected-but-blind for 23h on 2026-06-09 while the
        engine's STALE_FEED probe fired into the void. This is the entry
        point that probe needs: it drives the SAME path the heartbeat's
        disconnect branch drives (`_supervise_connect` → re-establish, then
        `_supervise_on_reconnect` → re-subscribe feed + reconcile), so a
        deaf feed heals exactly like a dropped one.

        Idempotent: a repair already in flight (state != CONNECTED) is a
        no-op, so a caller polling every 30s can't stack repairs.

        Returns True if a repair was started, False if declined.
        """
        if self._supervise_connect is None:
            return False  # owner-mode, or supervise() was never called
        if not self._running:
            return False
        if self._state != ConnectionState.CONNECTED:
            return False  # repair already running (or we're already FAILED)

        # Flip state BEFORE spawning so a concurrent request_repair declines.
        self._state = ConnectionState.RECONNECTING

        # Cancel the heartbeat explicitly rather than relying on it to
        # notice the state flip. It's parked in `await sleep(interval)`;
        # by the time it wakes, _supervise_reconnect_loop may have already
        # restored state to CONNECTED and started a FRESH heartbeat task —
        # the old one would then see CONNECTED, decline to break, and we'd
        # run two heartbeat loops that both spawn repairs on the next
        # genuine disconnect. (The disconnect branch doesn't need this: it
        # breaks synchronously.)
        hb = self._heartbeat_task
        if hb is not None and not hb.done():
            hb.cancel()
        self._heartbeat_task = None

        self._reconnect_attempts = 0
        self._notify("disconnect", f"Repair requested: {reason}")
        asyncio.create_task(self._supervise_reconnect_loop())
        return True

    async def _supervise_reconnect_loop(self) -> bool:
        """Backoff reconnect loop for supervisor mode.

        Calls the user-supplied `on_connect_async` until it returns True or
        we exhaust `max_reconnect_attempts`. On success, invokes
        `on_reconnect_async` (if provided) and restarts the heartbeat loop.
        """
        while (self._config.max_reconnect_attempts <= 0 or
               self._reconnect_attempts < self._config.max_reconnect_attempts):
            self._reconnect_attempts += 1
            self._notify(
                "reconnecting",
                self._reconnect_attempts,
                self._config.max_reconnect_attempts,
            )
            exponent = min(self._reconnect_attempts - 1, 30)
            backoff = min(
                self._config.initial_backoff * (2 ** exponent),
                self._config.max_backoff,
            )
            await asyncio.sleep(backoff)
            try:
                ok = await self._supervise_connect()
            except Exception as e:
                self._notify("error", e)
                ok = False
            if ok:
                self._state = ConnectionState.CONNECTED
                self._connected_at = self._ts()
                self._last_heartbeat = self._connected_at
                self._reconnect_attempts = 0
                self._notify("connect", self._connected_at)
                if self._supervise_on_reconnect is not None:
                    try:
                        await self._supervise_on_reconnect()
                    except Exception as e:
                        self._notify("error", e)
                # Restart heartbeat loop
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                return True
        # All attempts exhausted — caller should treat this as fatal
        self._state = ConnectionState.FAILED
        return False

    def _setup_async_compat(self):
        """No-op. Previously called nest_asyncio.apply().

        nest_asyncio's last asyncio-compat update was for Python 3.9; on
        Python 3.14 the patched event loop cancels the main task within
        ~1s of startup (eager-tasks rewrite). ib_async supports 3.14
        natively — the patch isn't needed. Method kept as a no-op so
        existing call sites are unaffected.
        """
        return

    def _notify(self, event: str, *args, **kwargs):
        """
        Unified notification dispatcher for all observer events.

        Catches observer exceptions and logs them to stderr to avoid
        one broken observer from crashing the notification chain.
        """
        for observer in self._observers:
            try:
                handler = getattr(observer, f"on_{event}", None)
                if handler:
                    handler(*args, **kwargs)
            except Exception as e:
                import sys
                print(f"[ConnectionManager] Observer error in on_{event}: {e}", file=sys.stderr)
