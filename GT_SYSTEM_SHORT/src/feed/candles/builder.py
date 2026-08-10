"""
Candle Builder - Multi-Timeframe OHLCV Aggregation

Builds OHLCV (Open-High-Low-Close-Volume) candles from tick data.
Supports multiple timeframes: 1m, 5m, 15m, 1h, 4h, 1d

Design Patterns:
- Factory: Creates timeframe-specific builders
- Observer: Emits bar completion events
- Strategy: Different bar-building algorithms (tick-based, time-based)

Architecture:
    Tick -> TickAggregator -> TimeframeBuilder -> CompletedBar
                    |
    +------------+------------+------------+
    |            |            |            |
   1m          5m          15m          1h
   builders    builders     builders     builders

Usage:
    builder = CandleBuilder()

    # Subscribe to timeframe bars
    builder.on_bar("1m", lambda bar: process_bar(bar))
    builder.on_bar("5m", lambda bar: process_bar(bar))

    # Add ticks
    builder.add_tick(tick)

    # Get current/recent bars
    bars = builder.get_bars("5m", count=100)
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional
import threading


@dataclass(slots=True)
class Candle:
    """
    OHLCV Candlestick data.

    Immutable once complete. The 'complete' flag indicates if this is
    a finished bar or still being built.
    """
    timestamp: datetime      # Bar start time
    symbol: str
    timeframe: str          # "1m", "5m", "15m", "1h", "4h", "1d"
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int = 0
    complete: bool = False

    @property
    def range(self) -> float:
        """High - Low."""
        return self.high - self.low

    @property
    def body(self) -> float:
        """Absolute difference between open and close."""
        return abs(self.close - self.open)

    @property
    def direction(self) -> str:
        """Bullish, Bearish, or Doji."""
        if self.close > self.open:
            return "bullish"
        elif self.close < self.open:
            return "bearish"
        return "doji"

    @property
    def hl_mid(self) -> float:
        """Midpoint of high-low range."""
        return (self.high + self.low) / 2

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp.isoformat(),
            "sym": self.symbol,
            "tf": self.timeframe,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "n": self.tick_count,
        }


class TimeframeConfig:
    """
    Configuration for a single timeframe.

    Defines how bars are aligned and when they complete.
    """
    def __init__(
        self,
        name: str,
        duration: timedelta,
        align_to: Optional[datetime] = None,
    ):
        self.name = name
        self.duration = duration
        self.align_fn = align_fn_for(name) if align_fn is None else align_fn

    @property
    def seconds(self) -> int:
        return int(self.duration.total_seconds())


# Timeframe registry
TIMEFRAMES = {
    "1s": timedelta(seconds=1),
    "30s": timedelta(seconds=30),
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}


def align_to_timeframe(ts: datetime, duration: timedelta) -> datetime:
    """
    Align timestamp to the start of its bar period.

    Examples:
        - 1m at 10:05:32 -> 10:05:00
        - 5m at 10:07:32 -> 10:05:00
        - 1h at 10:37:32 -> 10:00:00
        - 1d at any time -> midnight
    """
    if duration >= timedelta(days=1):
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)

    if duration >= timedelta(hours=1):
        hours = int(duration.total_seconds() // 3600)
        return ts.replace(minute=0, second=0, microsecond=0).replace(
            hour=(ts.hour // hours) * hours
        )

    # Minutes or seconds
    minutes = int(duration.total_seconds() // 60)
    if minutes == 0:
        minutes = 1

    return ts.replace(second=0, microsecond=0).replace(
        minute=(ts.minute // minutes) * minutes
    )


def align_fn_for(timeframe: str) -> Callable[[datetime], datetime]:
    """Get alignment function for timeframe."""
    duration = TIMEFRAMES.get(timeframe, timedelta(minutes=1))
    return lambda ts: align_to_timeframe(ts, duration)


class BarBuilder:
    """
    Builds a single OHLCV bar from ticks.

    Accumulates price/volume data until the bar completes,
    then emits the completed bar.
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.start_time = start_time
        self.end_time = start_time + TIMEFRAMES.get(timeframe, timedelta(minutes=1))

        # OHLCV accumulation
        self.open: Optional[float] = None
        self.high: Optional[float] = None
        self.low: Optional[float] = None
        self.close: Optional[float] = None
        self.volume: int = 0
        self.tick_count: int = 0

        # First/last tick tracking
        self.first_tick_time: Optional[datetime] = None
        self.last_tick_time: Optional[datetime] = None

    def update(self, tick) -> Optional[Candle]:
        """
        Update bar with new tick data.

        Returns completed Candle if bar just finished, None otherwise.
        """
        self.tick_count += 1
        price = tick.last
        vol = tick.volume or 0
        ts = tick.timestamp

        # Update first tick
        if self.first_tick_time is None:
            self.first_tick_time = ts

        self.last_tick_time = ts

        # Initialize or update OHLC
        if self.open is None:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)

        self.close = price
        self.volume += vol

        # Check if bar is complete
        if ts >= self.end_time:
            return self.build()

        return None

    def build(self) -> Candle:
        """Build and return the completed candle."""
        return Candle(
            timestamp=self.start_time,
            symbol=self.symbol,
            timeframe=self.timeframe,
            open=self.open or 0.0,
            high=self.high or 0.0,
            low=self.low or 0.0,
            close=self.close or 0.0,
            volume=self.volume,
            tick_count=self.tick_count,
            complete=True,
        )

    def get_current(self) -> Candle:
        """Get the current (incomplete) bar snapshot."""
        return Candle(
            timestamp=self.start_time,
            symbol=self.symbol,
            timeframe=self.timeframe,
            open=self.open or 0.0,
            high=self.high or 0.0,
            low=self.low or 0.0,
            close=self.close or 0.0,
            volume=self.volume,
            tick_count=self.tick_count,
            complete=False,
        )


class CandleBuilder:
    """
    Multi-timeframe candle builder.

    Takes ticks and builds OHLCV candles for multiple timeframes simultaneously.
    Emits completed candles via callbacks.

    Features:
    - Multiple timeframes from single tick stream
    - Configurable timeframes
    - Rolling window of recent bars
    - Callback hooks for bar completion

    Usage:
        builder = CandleBuilder(symbol="AAPL", timeframes=["1m", "5m", "15m"])

        # Subscribe to bar completion
        builder.on_bar("5m", lambda c: print(f"5m close: {c.close}"))

        # Add ticks
        builder.add_tick(tick)

        # Get recent bars
        bars = builder.get_bars("5m", count=100)
    """

    def __init__(
        self,
        symbol: str,
        timeframes: Optional[list[str]] = None,
        bar_window: int = 500,
    ):
        """
        Initialize candle builder.

        Args:
            symbol: Trading symbol
            timeframes: List of timeframes to build (default: ["1m", "5m", "15m", "1h", "1d"])
            bar_window: Max bars to keep in history per timeframe
        """
        self.symbol = symbol
        self.timeframes = timeframes or ["1m", "5m", "15m", "1h", "1d"]
        self.bar_window = bar_window
        self._ts = datetime.now  # Cached timestamp function

        # Callbacks: timeframe -> [callbacks]
        self._callbacks: dict[str, list[Callable]] = {}

        # Current builders: timeframe -> BarBuilder
        self._builders: dict[str, BarBuilder] = {}

        # Completed bars: timeframe -> deque of Candle
        self._bars: dict[str, deque] = {
            tf: deque(maxlen=bar_window)
            for tf in self.timeframes
        }

        # Statistics
        self._tick_count = 0
        self._bar_counts: dict[str, int] = {tf: 0 for tf in self.timeframes}

        # Lock for thread safety
        self._lock = threading.Lock()

    def on_bar(self, timeframe: str, callback: Callable[[Candle], None]) -> None:
        """
        Subscribe to bar completion events.

        Args:
            timeframe: "1m", "5m", "15m", "1h", etc.
            callback: Function called with completed Candle
        """
        if timeframe not in self._callbacks:
            self._callbacks[timeframe] = []
        self._callbacks[timeframe].append(callback)

    def add_tick(self, tick) -> list[Candle]:
        """
        Add a tick and build/complete bars.

        Args:
            tick: Tick object with timestamp, last, volume

        Returns:
            List of completed Candle objects
        """
        self._tick_count += 1
        completed = []

        for tf in self.timeframes:
            candle = self._maybe_update_bar(tick, tf)
            if candle:
                completed.append(candle)

        return completed

    def _maybe_update_bar(self, tick, timeframe: str) -> Optional[Candle]:
        """Update or create bar for timeframe."""
        duration = TIMEFRAMES.get(timeframe)
        if duration is None:
            return None

        ts = tick.timestamp

        with self._lock:
            builder = self._builders.get(timeframe)

            if builder is None:
                # Create new builder
                start_time = align_to_timeframe(ts, duration)
                builder = BarBuilder(self.symbol, timeframe, start_time)
                self._builders[timeframe] = builder
            elif ts >= builder.end_time:
                # Complete current bar
                candle = builder.build()
                self._bars[timeframe].append(candle)
                self._bar_counts[timeframe] += 1
                self._emit_callbacks(timeframe, candle)

                # Start new bar
                start_time = align_to_timeframe(ts, duration)
                builder = BarBuilder(self.symbol, timeframe, start_time)
                self._builders[timeframe] = builder

            # Update builder with tick
            completed = builder.update(tick)

            if completed:
                self._bars[timeframe].append(completed)
                self._bar_counts[timeframe] += 1
                return completed

        return None

    def _emit_callbacks(self, timeframe: str, candle: Candle) -> None:
        """Emit bar completion callbacks."""
        callbacks = self._callbacks.get(timeframe, [])
        for cb in callbacks:
            try:
                cb(candle)
            except Exception as e:
                print(f"[CandleBuilder] Callback error: {e}")

    def get_bars(
        self,
        timeframe: str,
        count: int = 100,
        include_current: bool = False,
    ) -> list[Candle]:
        """
        Get recent bars for timeframe.

        Args:
            timeframe: Timeframe to query
            count: Max bars to return
            include_current: Include the in-progress bar

        Returns:
            List of Candle objects, most recent last
        """
        with self._lock:
            bars = list(self._bars.get(timeframe, []))

            if include_current:
                builder = self._builders.get(timeframe)
                if builder and builder.tick_count > 0:
                    bars.append(builder.get_current())

            return bars[-count:]

    def get_current_bar(self, timeframe: str) -> Optional[Candle]:
        """Get the currently forming bar (not yet complete)."""
        with self._lock:
            builder = self._builders.get(timeframe)
            if builder and builder.tick_count > 0:
                return builder.get_current()
        return None

    def get_stats(self) -> dict:
        """Get builder statistics."""
        with self._lock:
            return {
                "symbol": self.symbol,
                "tick_count": self._tick_count,
                "timeframes": {
                    tf: {
                        "bars_built": self._bar_counts[tf],
                        "current_bar_ticks": (
                            self._builders[tf].tick_count
                            if tf in self._builders else 0
                        ),
                    }
                    for tf in self.timeframes
                },
            }

    def reset(self) -> None:
        """Reset all bars and builders."""
        with self._lock:
            self._builders.clear()
            for tf in self._bars:
                self._bars[tf].clear()
            self._tick_count = 0
            for tf in self._bar_counts:
                self._bar_counts[tf] = 0


class AggregatedCandleBuilder:
    """
    Aggregates bars from a larger timeframe to a smaller one.

    Example: Build 1m bars from 5m bars (useful for backtesting
    when you only have hourly data).

    Usage:
        agg = AggregatedCandleBuilder(target_tf="1m", source_tf="5m")
        agg.add_bar(hourly_bar)
        m1_bars = agg.get_target_bars()
    """

    def __init__(
        self,
        target_tf: str,
        source_tf: str,
        max_bars: int = 500,
    ):
        self.target_tf = target_tf
        self.source_tf = source_tf
        self.max_bars = max_bars

        # Target bars being built
        self._current_target: Optional[Candle] = None
        self._completed: deque = deque(maxlen=max_bars)

        # For tracking which source bar we're in
        self._current_source: Optional[Candle] = None

        target_dur = TIMEFRAMES.get(target_tf, timedelta(minutes=1))
        self._target_dur_seconds = int(target_dur.total_seconds())

    def add_source_bar(self, source: Candle) -> list[Candle]:
        """
        Add a source bar and break it down to target timeframe.

        Returns list of completed target candles.
        """
        completed = []

        if self._current_source is None:
            # Start of new source bar
            self._current_source = source
            self._current_target = Candle(
                timestamp=source.timestamp,
                symbol=source.symbol,
                timeframe=self.target_tf,
                open=source.open,
                high=source.high,
                low=source.low,
                close=source.open,  # Will update
                volume=0,
                tick_count=0,
                complete=False,
            )

        # Calculate how many target periods this source bar covers
        source_start = source.timestamp
        source_end = source_start + TIMEFRAMES.get(self.source_tf, timedelta(minutes=5))

        # Find where we are in target periods
        if self._current_target:
            target_ts = self._current_target.timestamp
            target_end = target_ts + timedelta(seconds=self._target_dur_seconds)

            # Update current target bar with source data
            self._current_target.high = max(self._current_target.high, source.high)
            self._current_target.low = min(self._current_target.low, source.low)
            self._current_target.close = source.close
            self._current_target.volume += source.volume
            self._current_target.tick_count += source.tick_count

            # Check if we need to complete this target bar
            while target_end <= source_end:
                # Complete the target bar
                completed_bar = Candle(
                    timestamp=target_ts,
                    symbol=source.symbol,
                    timeframe=self.target_tf,
                    open=self._current_target.open,
                    high=self._current_target.high,
                    low=self._current_target.low,
                    close=self._current_target.close,
                    volume=self._current_target.volume,
                    tick_count=self._current_target.tick_count,
                    complete=True,
                )
                self._completed.append(completed_bar)
                completed.append(completed_bar)

                # Start next target bar
                target_ts = target_end
                target_end = target_ts + timedelta(seconds=self._target_dur_seconds)
                self._current_target = Candle(
                    timestamp=target_ts,
                    symbol=source.symbol,
                    timeframe=self.target_tf,
                    open=source.close,  # Open at last close
                    high=source.close,
                    low=source.close,
                    close=source.close,
                    volume=0,
                    tick_count=0,
                    complete=False,
                )

        self._current_source = source
        return completed

    def get_target_bars(self, count: int = 100) -> list[Candle]:
        """Get recent target bars."""
        return list(self._completed)[-count:]
