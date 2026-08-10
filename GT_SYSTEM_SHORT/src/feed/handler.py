"""
IBKR Feed Handler

Routes incoming market data messages from IBKR to appropriate handlers.
Implements the Observer pattern for downstream consumers.

Architecture:
    IBKR API -> FeedHandler -> Pipeline Stage 1 -> Pipeline Stage 2 -> ... -> Consumer

The feed handler serves as the central dispatcher:
1. Receives raw ticks from IBKR subscription
2. Routes to registered handlers based on message type
3. Provides a clean interface for market data consumers

Design Pattern: Observer/Pub-Sub
- Multiple consumers can subscribe to the same feed
- Feed handler maintains list of subscribers
- Each subscriber receives all matching messages
"""
import asyncio
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, Any
from collections import defaultdict


def _safe_int(val) -> int:
    """Safely convert to int, handling NaN and None."""
    if val is None:
        return 0
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return 0
        return int(v)
    except (ValueError, TypeError):
        return 0


def _safe_float(val) -> float:
    """Safely convert to float, handling NaN and None."""
    if val is None:
        return 0.0
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return v
    except (ValueError, TypeError):
        return 0.0


class MessageType(Enum):
    """Market data message types from IBKR."""
    TICK = "TICK"           # BBO price/volume tick
    TRADE = "TRADE"         # Tick-by-tick trade (LTP)
    TICK_STRING = "TICK_STRING"  # String-based tick (e.g., last trade time)
    TICK_SIZE = "TICK_SIZE"     # Volume update
    TICK_OPTION_COMPUTATION = "TICK_OPTION_COMPUTATION"
    TICK_GENERIC = "TICK_GENERIC"
    TICK_EFP = "TICK_EFP"
    TICK_PRICE = "TICK_PRICE"   # Bid/ask price update
    QUOTE = "QUOTE"             # Tick-by-tick BidAsk (FX/IDEALPRO — no trade prints)
    ORDER_UPDATE = "ORDER_UPDATE"
    ERROR = "ERROR"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    TIMEOUT = "TIMEOUT"


@dataclass(slots=True)
class Tick:
    """
    Normalized tick data structure with LTP-first design.

    Represents a single market data tick with all relevant fields.
    Uses __slots__ for memory efficiency and faster attribute access.

    LTP (Last Trade Price) is the PRIMARY signal for strategy entry/exit.
    BBO (Bid/Ask) is used for spread/liquidity assessment.
    """
    # Core identification
    timestamp: datetime           # When received (UTC)
    symbol: str                   # "AAPL"

    # LTP - PRIMARY SIGNAL PRICE (from tick-by-tick)
    last: float = 0.0            # Last trade price
    last_size: float = 0.0      # Trade quantity
    last_exchange: str = ""       # "NASDAQ", "FINRA", "BATS"
    last_conditions: str = ""    # "F" (regular), "I" (odd lot), "F I"

    # BBO - for spread/liquidity assessment
    bid: float = 0.0
    ask: float = 0.0
    bid_size: int = 0
    ask_size: int = 0

    # Cumulative volume (from tickType=8)
    volume: int = 0

    # Daily reference (for strategy context)
    open: float = 0.0            # Session open
    high: float = 0.0            # Session high
    low: float = 0.0             # Session low

    # Previous values (for delta tracking, crossing detection)
    prev_last: float = 0.0
    prev_bid: float = 0.0
    prev_ask: float = 0.0

    # Meta
    tick_type: MessageType = MessageType.TICK
    req_id: int = 0

    def spread(self) -> float:
        """Bid-ask spread."""
        return self.ask - self.bid if self.ask > 0 and self.bid > 0 else 0.0

    def mid_price(self) -> float:
        """Mid price (midpoint of bid/ask)."""
        return (self.ask + self.bid) / 2 if self.ask > 0 and self.bid > 0 else self.last

    def is_complete(self) -> bool:
        """Tick has all essential price data."""
        return self.last > 0 or (self.bid > 0 and self.ask > 0)

    def is_odd_lot(self) -> bool:
        """Trade was an odd lot (< 100 shares)."""
        return 'I' in self.last_conditions

    def is_regular_trade(self) -> bool:
        """Trade was a regular sale."""
        return 'F' in self.last_conditions

    def ltp_change(self) -> float:
        """Change in LTP since last tick."""
        return self.last - self.prev_last if self.prev_last > 0 else 0.0

    def __repr__(self) -> str:
        """Compact representation for debugging."""
        return f"Tick({self.symbol} LTP={self.last} B={self.bid}/A={self.ask} vol={self.volume})"


@dataclass(slots=True)
class TickFilter:
    """
    Filter criteria for tick subscriptions.

    Allows consumers to specify which types of ticks they want to receive.
    Reduces unnecessary processing for uninterested consumers.
    """
    symbols: set[str] = field(default_factory=set)
    tick_types: set[MessageType] = field(default_factory=set)
    min_bid_size: int = 0  # Minimum bid size to pass filter
    min_ask_size: int = 0
    require_full_tick: bool = False  # Only ticks with bid/ask/last

    def matches(self, tick: Tick) -> bool:
        """Check if tick matches filter criteria."""
        # Symbol filter
        if self.symbols and tick.symbol not in self.symbols:
            return False

        # Type filter
        if self.tick_types and tick.tick_type not in self.tick_types:
            return False

        # Size filters
        if tick.bid_size < self.min_bid_size:
            return False
        if tick.ask_size < self.min_ask_size:
            return False

        # Require full tick
        if self.require_full_tick and not tick.is_complete:
            return False

        return True


class TickHandler(ABC):
    """
    Abstract base class for tick handlers.

    All components that want to receive ticks must implement this interface.
    The FeedHandler will call on_tick() for each matching tick.

    Usage:
        class MyHandler(TickHandler):
            def on_tick(self, tick: Tick):
                # Process tick
                pass

        handler = MyHandler(symbols={"AAPL"}, tick_types={MessageType.TICK_PRICE})
        feed_handler.subscribe(handler)
    """

    __slots__ = ('name', 'filter', '_enabled')

    def __init__(
        self,
        name: str,
        filter: Optional[TickFilter] = None,
    ):
        """
        Initialize handler.

        Args:
            name: Identifier for this handler (for logging/debugging)
            filter: Optional filter to limit which ticks are received
        """
        self.name = name
        self.filter = filter or TickFilter()
        self._enabled = True

    @abstractmethod
    def on_tick(self, tick: Tick) -> None:
        """
        Called for each tick matching this handler's filter.

        Args:
            tick: Normalized tick data
        """
        pass

    @abstractmethod
    def on_error(self, error: Exception) -> None:
        """
        Called when an error occurs processing ticks.

        Args:
            error: The exception that occurred
        """
        pass

    def enable(self) -> None:
        """Enable this handler."""
        self._enabled = True

    def disable(self) -> None:
        """Disable this handler (ticks will be dropped)."""
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled


class ErrorHandler(TickHandler):
    """
    Default error handler that just logs errors.

    Can be subclassed or replaced with custom error handling.
    """

    __slots__ = ()

    def __init__(self, name: str = "ErrorHandler"):
        super().__init__(name)

    def on_tick(self, tick: Tick) -> None:
        pass  # Ignore ticks

    def on_error(self, error: Exception) -> None:
        """Log the error."""
        print(f"[{self.name}] Error: {error}")


class FeedHandler:
    """
    Central dispatcher for IBKR market data.

    Responsibilities:
    1. Manage IBKR ticker subscriptions
    2. Dispatch ticks to registered handlers
    3. Apply filters before dispatching
    4. Track subscription state

    Thread Safety:
        All methods are designed to be called from a single asyncio event loop.
        For multi-threaded access, add explicit locking.

    Example:
        # Create feed handler
        feed = FeedHandler(ib, connection_manager)

        # Subscribe handlers
        feed.subscribe(MyHandler("handler1"))
        feed.subscribe(AnotherHandler("handler2"))

        # Subscribe to symbols
        feed.subscribe_symbol("AAPL")
        feed.subscribe_symbol("INFY")

        # Start receiving ticks
        await feed.start()

        # Later: cleanup
        await feed.stop()
    """

    __slots__ = (
        '_ib', '_conn', '_ts', '_handlers',
        '_symbol_to_reqid', '_reqid_to_symbol', '_next_req_id',
        '_subscriptions', '_tbto_tickers',  # tick-by-tick tickers
        '_last_ticks',  # Track last tick for prev_* fields
        '_ticks_received', '_ticks_dispatched',
        '_errors', '_running', '_tick_queue', '_dispatch_task',
    )

    def __init__(
        self,
        ib,  # ib_async.IB instance
        connection_manager,  # ConnectionManager for heartbeat
    ):
        self._ib = ib
        self._conn = connection_manager

        # Cached timestamp function
        self._ts = datetime.now

        # Subscribed handlers (in order of subscription)
        self._handlers: list[TickHandler] = []

        # Symbol -> set of reqIds (IBKR uses reqId for subscriptions)
        self._symbol_to_reqid: dict[str, int] = {}
        self._reqid_to_symbol: dict[int, str] = {}

        # Next request ID to use
        self._next_req_id = 1

        # Active subscriptions
        self._subscriptions: dict[int, Any] = {}  # reqId -> BBO ticker
        self._tbto_tickers: dict[int, Any] = {}  # reqId -> tick-by-tick ticker

        # Track last tick per symbol for prev_* fields
        self._last_ticks: dict[str, Tick] = {}

        # Statistics
        self._ticks_received = 0
        self._ticks_dispatched = 0
        self._errors = 0

        # Running state
        self._running = False

        # Queue-based dispatch (created in start() to bind to correct event loop)
        self._tick_queue = None
        self._dispatch_task: Optional[asyncio.Task] = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        """Return feed statistics."""
        return {
            "ticks_received": self._ticks_received,
            "ticks_dispatched": self._ticks_dispatched,
            "errors": self._errors,
            "handlers": len(self._handlers),
            "subscriptions": len(self._subscriptions),
        }

    def subscribe(self, handler: TickHandler) -> None:
        """
        Subscribe a handler to receive ticks.

        Args:
            handler: TickHandler implementation
        """
        if handler not in self._handlers:
            self._handlers.append(handler)
            print(f"[FeedHandler] Subscribed handler: {handler.name}")

    def unsubscribe(self, handler: TickHandler) -> None:
        """
        Unsubscribe a handler.

        Args:
            handler: Previously subscribed handler
        """
        if handler in self._handlers:
            self._handlers.remove(handler)
            print(f"[FeedHandler] Unsubscribed handler: {handler.name}")

    async def subscribe_symbol(self, symbol: str) -> int:
        """
        Subscribe to market data for a symbol.

        Creates BOTH:
        1. BBO subscription via reqMktData (bid/ask/last/cumulative volume)
        2. Tick-by-tick subscription via reqTickByTickData (individual trades)

        Args:
            symbol: Trading symbol (e.g., "AAPL", "INFY")

        Returns:
            req_id: IBKR request ID for this subscription
        """
        if symbol in self._symbol_to_reqid:
            return self._symbol_to_reqid[symbol]  # Already subscribed

        from ib_async import Stock

        req_id = self._next_req_id
        self._next_req_id += 1

        # ── Multi-asset contract construction (D2-PM) ─────────────────
        # Spec-driven contract for FX / CFDs / Futures / global equity.
        # Falls back to the legacy hardcoded equity construction if the
        # symbol doesn't resolve (e.g. INFY which is NSE/INR — a stub
        # the existing code special-cased before AssetSpec arrived).
        contract = None
        try:
            from src.assets import resolve as _resolve_spec
            spec = _resolve_spec(symbol)
            contract = spec.contract.make(symbol)
        except Exception:
            contract = None

        if contract is None:
            # Legacy fallback for symbols outside the AssetSpec registry.
            if symbol in ("INFY",):
                contract = Stock(symbol, "NSE", "INR")
            else:
                contract = Stock(symbol, "SMART", "USD")

        # Qualify contract — must be async. The sync wrapper uses
        # `loop.run_until_complete` internally, which raises
        # "This event loop is already running" when called inside our
        # async path. nest_asyncio used to paper over this but was
        # incompatible with Python 3.14 and has been removed.
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            # IBKR could not resolve the contract — almost always a bad/
            # misspelled ticker (e.g. 'MFST' instead of 'MSFT'). Raise a
            # clear, actionable error instead of returning a sentinel that
            # the caller ignores (which left the engine running with no feed)
            # or falling through to reqMktData(None) (the confusing
            # 'NoneType has no attribute secType' crash).
            raise ValueError(
                f"Unknown ticker '{symbol}': IBKR could not qualify contract "
                f"{contract!r}. Check the symbol is spelled correctly and is "
                f"tradable on your account (e.g. 'MSFT', not 'MFST')."
            )

        contract = qualified[0]

        # 1. BBO subscription - bid/ask/last + cumulative volume
        ticker = self._ib.reqMktData(contract, "", False, False)

        # 2. Tick-by-tick subscription — type depends on asset class.
        # Stocks/futures use 'AllLast' (every trade prints). FX on
        # IDEALPRO is quote-driven, has NO last-trade feed, and IBKR
        # rejects 'AllLast' for it with error 10189. Use 'BidAsk' for
        # FX so we still get tick-by-tick quote updates.
        #
        # Resolve via AssetSpec so the choice is asset-aware without
        # FeedHandler needing per-asset hardcodes.
        tbt_mode = 'AllLast'
        try:
            from src.assets import resolve as _resolve_spec
            from src.assets.enum import AssetClass as _AC
            _spec = _resolve_spec(symbol)
            if _spec.asset_class in (_AC.FX_CASH, _AC.FX_CFD):
                tbt_mode = 'BidAsk'
        except Exception:
            # Unknown symbol — keep AllLast default (the legacy behavior).
            pass

        # GT_DISABLE_TICKBYTICK — operator lever to SKIP the scarce
        # tick-by-tick subscription, escaping IBKR Error 10190 ("Max number
        # of tick-by-tick requests has been reached") at fleet scale (>~30
        # bots). ONLY applied to 'AllLast' mode (equities/futures): there,
        # reqMktData's streaming `last` (trade prints) already drives the
        # breakout engine and passes the pipeline's last>0 gate
        # (production.py). NEVER applied to FX/CFD ('BidAsk' mode) — spot FX
        # has NO last-trade price, so its ticks come solely from the
        # tick-by-tick BidAsk stream; dropping it would blind the currency
        # strategy. Default OFF → behaviour unchanged unless explicitly set.
        _disable_tbt = False
        try:
            import os as _os
            if _os.environ.get("GT_DISABLE_TICKBYTICK", "").strip() and tbt_mode == 'AllLast':
                _disable_tbt = True
        except Exception:
            _disable_tbt = False

        tbto_ticker = None
        if _disable_tbt:
            print(f"[FeedHandler] Tick-by-tick: SKIPPED for {symbol} "
                  f"(GT_DISABLE_TICKBYTICK set; BBO-only — would have been {tbt_mode})")
        else:
            try:
                tbto_ticker = self._ib.reqTickByTickData(contract, tbt_mode, 0, True)
                print(f"[FeedHandler] Tick-by-tick: ENABLED for {symbol} (mode={tbt_mode})")
            except Exception as e:
                print(f"[FeedHandler] Tick-by-tick: DISABLED ({e}) - using BBO only")
                tbto_ticker = None

        # Store mappings
        self._symbol_to_reqid[symbol] = req_id
        self._reqid_to_symbol[req_id] = symbol
        self._subscriptions[req_id] = ticker
        self._tbto_tickers[req_id] = tbto_ticker

        mode = "BBO + Tick-by-Tick" if tbto_ticker else "BBO Only"
        print(f"[FeedHandler] Subscribed {symbol} with reqId={req_id} ({mode})")
        return req_id

    def unsubscribe_symbol(self, symbol: str) -> bool:
        """
        Unsubscribe from a symbol.

        Args:
            symbol: Trading symbol to unsubscribe

        Returns:
            True if unsubscribed, False if wasn't subscribed
        """
        if symbol not in self._symbol_to_reqid:
            return False

        req_id = self._symbol_to_reqid[symbol]

        # Remove BBO ticker from IBKR
        if req_id in self._subscriptions:
            ticker = self._subscriptions[req_id]
            try:
                self._ib.cancelMktData(ticker)
            except:
                pass

        # Remove tick-by-tick ticker from IBKR
        if req_id in self._tbto_tickers:
            tbto = self._tbto_tickers[req_id]
            try:
                self._ib.cancelTickByTickData(tbto)
            except:
                pass

        # Clean up mappings
        del self._symbol_to_reqid[symbol]
        del self._reqid_to_symbol[req_id]
        del self._subscriptions[req_id]
        if req_id in self._tbto_tickers:
            del self._tbto_tickers[req_id]
        if symbol in self._last_ticks:
            del self._last_ticks[symbol]

        print(f"[FeedHandler] Unsubscribed {symbol}")
        return True

    def get_ticker(self, symbol: str):
        """
        Get the IBKR ticker object for a symbol.

        Args:
            symbol: Trading symbol

        Returns:
            IBKR Ticker object or None if not subscribed
        """
        req_id = self._symbol_to_reqid.get(symbol)
        if req_id is None:
            return None
        return self._subscriptions.get(req_id)

    async def start(self) -> None:
        """
        Start the feed handler.

        DIRECT DISPATCH: IBKR callbacks spawn async tasks directly.
        No queue - callbacks use create_task to dispatch to async handlers.
        """
        if self._running:
            return

        self._running = True

        print(f"[FeedHandler] Started with {len(self._subscriptions)} subscriptions")

        # Set up callbacks for BBO tickers - direct dispatch via create_task
        for req_id, ticker in self._subscriptions.items():
            symbol = self._reqid_to_symbol[req_id]
            self._setup_bbo_callback(req_id, ticker, symbol)

        # Set up callbacks for tick-by-tick tickers (if available)
        for req_id, tbto_ticker in self._tbto_tickers.items():
            if tbto_ticker is None:
                continue
            symbol = self._reqid_to_symbol[req_id]
            self._setup_tbto_callback(req_id, tbto_ticker, symbol)

        print("[FeedHandler] All callbacks registered (event-driven mode)")

        # Keep alive - callbacks handle everything via create_task
        while self._running:
            await asyncio.sleep(1.0)

    async def stop(self) -> None:
        """Stop the feed handler."""
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        print(f"[FeedHandler] Stopped")

    def _setup_bbo_callback(self, req_id: int, ticker, symbol: str) -> None:
        """
        Set up IBKR BBO ticker event callbacks.

        IBKR tickers have an updateEvent that fires when data changes.
        Hooks into this to capture bid/ask/last/volume updates.
        """
        def on_bbo_update(ticker):
            """Called by ib_async when BBO ticker updates."""
            self._ticks_received += 1
            # Direct dispatch to async handler using running loop
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._dispatch_bbo_tick(req_id, ticker, symbol))
            except RuntimeError:
                pass  # No event loop in sync context

        try:
            ticker.updateEvent -= on_bbo_update
        except:
            pass
        ticker.updateEvent += on_bbo_update

    def _setup_tbto_callback(self, req_id: int, tbto_ticker, symbol: str) -> None:
        """
        Set up IBKR tick-by-tick ticker event callbacks.

        Tick-by-tick data comes via updateEvent with tickByTicks list populated.
        Each entry in tickByTicks is a TickByTickAllLast object with trade details.
        """
        def on_tbto_update(tbto_ticker):
            """Called by ib_async when tick-by-tick data arrives."""
            if tbto_ticker.tickByTicks:
                # Direct dispatch to async handler using running loop
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._dispatch_trade_ticks(req_id, tbto_ticker, symbol))
                except RuntimeError:
                    pass  # No event loop

        try:
            tbto_ticker.updateEvent -= on_tbto_update
        except:
            pass
        tbto_ticker.updateEvent += on_tbto_update

    async def _process_tick_queue(self) -> None:
        """
        Process ticks from queue in async context.

        This runs in the event loop and processes ticks that were
        queued from sync IBKR callbacks.
        """
        while self._running:
            try:
                tick_type, req_id, data, symbol = await self._tick_queue.get()
                if tick_type == "bbo":
                    await self._dispatch_bbo_tick(req_id, data, symbol)
                elif tick_type == "tbto":
                    await self._dispatch_trade_ticks(req_id, data, symbol)
                self._tick_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._errors += 1
                print(f"[FeedHandler] Queue error: {e}")

    async def _dispatch_bbo_tick(self, req_id: int, ticker, symbol: str) -> None:
        """
        Dispatch a BBO tick to all matching handlers.

        Creates a normalized Tick from IBKR BBO ticker data and sends to
        all handlers whose filters match. Includes prev_* fields for
        crossing detection.
        """
        # Get previous tick for delta tracking
        last_tick = self._last_ticks.get(symbol)
        prev_last = last_tick.last if last_tick else 0.0
        prev_bid = last_tick.bid if last_tick else 0.0
        prev_ask = last_tick.ask if last_tick else 0.0

        # Build normalized tick
        tick = Tick(
            timestamp=self._ts(),
            symbol=symbol,
            last=_safe_float(ticker.last),
            last_size=0.0,  # BBO tick doesn't have trade size
            last_exchange="",
            last_conditions="",
            bid=_safe_float(ticker.bid),
            ask=_safe_float(ticker.ask),
            bid_size=_safe_int(ticker.bidSize),
            ask_size=_safe_int(ticker.askSize),
            volume=_safe_int(ticker.volume),
            open=_safe_float(ticker.open),
            high=_safe_float(ticker.high),
            low=_safe_float(ticker.low),
            prev_last=prev_last,
            prev_bid=prev_bid,
            prev_ask=prev_ask,
            tick_type=MessageType.TICK,
            req_id=req_id,
        )

        # Store for next delta
        self._last_ticks[symbol] = tick

        # Dispatch to matching handlers
        for handler in self._handlers:
            if not handler.is_enabled:
                continue

            try:
                if handler.filter.matches(tick):
                    handler.on_tick(tick)
                    self._ticks_dispatched += 1
            except Exception as e:
                self._errors += 1
                handler.on_error(e)

    async def _dispatch_trade_ticks(self, req_id: int, tbto_ticker, symbol: str) -> None:
        """
        Dispatch tick-by-tick data to all matching handlers.

        Handles BOTH tick-by-tick modes:

        - 'AllLast' (equity/futures): TickByTickAllLast objects with
          .price/.size/.exchange/.specialConditions/.tickAttribLast.
          We emit MessageType.TRADE ticks with `last` populated.

        - 'BidAsk' (IDEALPRO FX): TickByTickBidAsk objects with
          .bidPrice/.askPrice/.bidSize/.askSize/.tickAttribBidAsk. FX
          has no "trade" stream — every event is a quote update. We
          emit MessageType.QUOTE ticks with `last` carried over from
          the previous tick (so FX consumers using spec.price.*
          comparators read bid/ask, not zero).
        """
        # Get previous tick for delta tracking
        last_tick = self._last_ticks.get(symbol)
        prev_last = last_tick.last if last_tick else 0.0
        prev_bid_seed = last_tick.bid if last_tick else 0.0
        prev_ask_seed = last_tick.ask if last_tick else 0.0

        for tbto in tbto_ticker.tickByTicks:
            is_bidask = hasattr(tbto, 'bidPrice') and not hasattr(tbto, 'price')

            if is_bidask:
                # BidAsk: per-tick bid/ask, no trade price.
                cur_bid = _safe_float(tbto.bidPrice)
                cur_ask = _safe_float(tbto.askPrice)
                tick = Tick(
                    timestamp=tbto.time,
                    symbol=symbol,
                    last=prev_last,            # carry over — FX has no LTP
                    last_size=0.0,
                    last_exchange="",
                    last_conditions="",
                    bid=cur_bid,
                    ask=cur_ask,
                    bid_size=_safe_int(tbto.bidSize),
                    ask_size=_safe_int(tbto.askSize),
                    volume=_safe_int(tbto_ticker.volume),
                    open=_safe_float(tbto_ticker.open),
                    high=_safe_float(tbto_ticker.high),
                    low=_safe_float(tbto_ticker.low),
                    prev_last=prev_last,
                    prev_bid=prev_bid_seed,
                    prev_ask=prev_ask_seed,
                    tick_type=MessageType.QUOTE,
                    req_id=req_id,
                )
                prev_bid_seed = cur_bid
                prev_ask_seed = cur_ask
            else:
                # AllLast: actual trade prints (equity/futures path).
                tick = Tick(
                    timestamp=tbto.time,
                    symbol=symbol,
                    last=_safe_float(tbto.price),
                    last_size=_safe_float(tbto.size),
                    last_exchange=tbto.exchange or "",
                    last_conditions=tbto.specialConditions or "",
                    bid=_safe_float(tbto_ticker.bid),
                    ask=_safe_float(tbto_ticker.ask),
                    bid_size=_safe_int(tbto_ticker.bidSize),
                    ask_size=_safe_int(tbto_ticker.askSize),
                    volume=_safe_int(tbto_ticker.volume),
                    open=_safe_float(tbto_ticker.open),
                    high=_safe_float(tbto_ticker.high),
                    low=_safe_float(tbto_ticker.low),
                    prev_last=prev_last,
                    prev_bid=0.0,
                    prev_ask=0.0,
                    tick_type=MessageType.TRADE,
                    req_id=req_id,
                )
                prev_last = tick.last

            self._last_ticks[symbol] = tick

            # Dispatch to matching handlers (SINGLE pass — there used to be
            # a duplicate copy of this loop here, which made every trade tick
            # fire downstream handlers twice. That corrupted prev_ltp/cross
            # detection, doubled audit rows, and double-counted ticks.)
            for handler in self._handlers:
                if not handler.is_enabled:
                    continue
                try:
                    if handler.filter.matches(tick):
                        handler.on_tick(tick)
                        self._ticks_dispatched += 1
                except Exception as e:
                    self._errors += 1
                    # Suppress event loop errors - not actionable
                    if "event loop" not in str(e).lower():
                        handler.on_error(e)
