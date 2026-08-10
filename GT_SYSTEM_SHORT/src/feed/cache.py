"""
Market Data Cache and Tick Logger

Stores ticks and bars in a rolling window for:
- Recent tick history (for strategy lookback)
- Aggregated OHLCV bars (1m, 5m, 15m, 1h, 1d)
- Tick persistence to disk (CSV/JSON)

Design Pattern:
- Observer: Receives ticks from pipeline
- Rolling Window: Keeps last N ticks/bars per symbol
- Aggregator: Builds OHLCV bars from ticks
"""
import csv
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

from src.feed.handler import Tick


@dataclass(slots=True)
class OHLCVBar:
    """
    Open-High-Low-Close-Volume bar.

    Represents aggregated price/volume data for a time period.
    """
    timestamp: datetime
    symbol: str
    timeframe: str  # "1m", "5m", "15m", "1h", "1d"
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int = 0

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "tf": self.timeframe,
            "o": self.open,
            "h": self.high,
            "l": self.low,
            "c": self.close,
            "v": self.volume,
            "n": self.tick_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OHLCVBar":
        return cls(
            timestamp=datetime.fromisoformat(d["ts"]),
            symbol=d["symbol"],
            timeframe=d["tf"],
            open=float(d["o"]),
            high=float(d["h"]),
            low=float(d["l"]),
            close=float(d["c"]),
            volume=int(d["v"]),
            tick_count=int(d.get("n", 0)),
        )


@dataclass(slots=True)
class TickRecord:
    """Persistent tick record."""
    timestamp: datetime
    symbol: str
    bid: float
    ask: float
    last: float
    volume: int

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp.isoformat(),
            "sym": self.symbol,
            "b": self.bid,
            "a": self.ask,
            "l": self.last,
            "v": self.volume,
        }


class TickLogger:
    """
    Writes ticks to disk in CSV format.

    Thread-safe for concurrent writes from multiple sources.
    """

    def __init__(self, directory: str = "data/ticks"):
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._files: dict[str, tuple] = {}  # symbol -> (file, writer)
        self._ts = datetime.now  # Cached timestamp function

    def log(self, tick) -> None:
        """Log a tick to CSV."""
        sym = tick.symbol
        ts = self._ts()

        with self._lock:
            if sym not in self._files:
                path = self._directory / f"{sym}_{ts:%Y%m%d}.csv"
                file = open(path, 'a', buffering=1)
                writer = csv.writer(file)
                if file.tell() == 0:
                    writer.writerow(["timestamp", "symbol", "bid", "ask", "last", "volume"])
                self._files[sym] = (file, writer)

            _, writer = self._files[sym]
            writer.writerow([
                tick.timestamp.isoformat(),
                tick.symbol,
                tick.bid,
                tick.ask,
                tick.last,
                tick.volume,
            ])

    def close(self) -> None:
        """Close all files."""
        with self._lock:
            for file, _ in self._files.values():
                file.close()
            self._files.clear()


class MarketDataCache:
    """
    Rolling window cache for ticks and aggregated bars.

    Features:
    - Rolling tick history (configurable size)
    - Multi-timeframe OHLCV bars (1m, 5m, 15m, 1h, 1d)
    - Callback for bar completion events
    - Thread-safe operations

    Usage:
        cache = MarketDataCache(symbols=["AAPL", "INFY"])

        # Add tick
        cache.add_tick(tick)

        # Get recent ticks
        recent = cache.get_ticks("AAPL", count=100)

        # Get bars
        bars_1m = cache.get_bars("AAPL", "1m", count=100)

        # Subscribe to bar completion
        cache.on_bar_completed("1m", lambda bar: print(f"1m bar: {bar}"))
    """

    # Supported timeframes
    TIMEFRAMES = {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "1d": timedelta(days=1),
    }

    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        tick_window: int = 1000,
        bar_window: int = 500,
        logger: Optional["TickLogger"] = None,
    ):
        """
        Initialize cache.

        Args:
            symbols: List of symbols to track
            tick_window: Max ticks to keep per symbol
            bar_window: Max bars to keep per timeframe per symbol
            logger: Optional tick logger for persistence
        """
        self._symbols = set(symbols) if symbols else set()
        self._tick_window = tick_window
        self._bar_window = bar_window
        self._logger = logger
        self._ts = datetime.now  # Cached timestamp function

        # Rolling tick history: symbol -> deque of ticks
        self._ticks: dict[str, deque] = {}

        # OHLCV bars: (symbol, timeframe) -> deque of bars
        self._bars: dict[tuple, deque] = {}

        # Current bar being built: (symbol, timeframe) -> OHLCVBar
        self._current_bar: dict[tuple, Optional[OHLCVBar]] = {}

        # Bar completion callbacks: timeframe -> [callbacks]
        self._bar_callbacks: dict[str, list[Callable]] = {}

        # Lock for thread safety
        self._lock = threading.Lock()

    def add_symbol(self, symbol: str) -> None:
        """Add a symbol to track."""
        with self._lock:
            if symbol not in self._symbols:
                self._symbols.add(symbol)
                self._ticks[symbol] = deque(maxlen=self._tick_window)

    def add_tick(self, tick) -> None:
        """
        Add a tick and update OHLCV bars.

        Args:
            tick: Tick object from the pipeline
        """
        sym = tick.symbol

        # Ensure symbol is tracked
        with self._lock:
            if sym not in self._symbols:
                self.add_symbol(sym)

        # Log to file if logger is set
        if self._logger:
            self._logger.log(tick)

        # Store tick
        with self._lock:
            if sym not in self._ticks:
                self._ticks[sym] = deque(maxlen=self._tick_window)
            self._ticks[sym].append(tick)

        # Update bars
        self._update_bars(tick)

    def _update_bars(self, tick) -> None:
        """Update all timeframes with new tick."""
        sym = tick.symbol
        ts = tick.timestamp

        for tf_name, tf_delta in self.TIMEFRAMES.items():
            key = (sym, tf_name)

            # Calculate bar start time (aligned to timeframe)
            bar_start = self._align_to_bar(ts, tf_delta)

            with self._lock:
                # Check if we need a new bar
                current = self._current_bar.get(key)

                if current is None or bar_start > current.timestamp:
                    # Complete the old bar if exists
                    if current is not None:
                        self._complete_bar(key, current)

                    # Start new bar
                    self._current_bar[key] = OHLCVBar(
                        timestamp=bar_start,
                        symbol=sym,
                        timeframe=tf_name,
                        open=tick.last,
                        high=tick.last,
                        low=tick.last,
                        close=tick.last,
                        volume=tick.volume or 0,
                        tick_count=1,
                    )
                else:
                    # Update current bar
                    current.high = max(current.high, tick.last)
                    current.low = min(current.low, tick.last)
                    current.close = tick.last
                    current.volume += tick.volume or 0
                    current.tick_count += 1

    def _align_to_bar(self, ts: datetime, delta: timedelta) -> datetime:
        """Align timestamp to bar boundary."""
        if delta == timedelta(days=1):
            return ts.replace(hour=0, minute=0, second=0, microsecond=0)
        elif delta >= timedelta(hours=1):
            hours = int(delta.total_seconds() // 3600)
            return ts.replace(minute=0, second=0, microsecond=0).replace(
                hour=(ts.hour // hours) * hours
            )
        else:
            minutes = int(delta.total_seconds() // 60)
            return ts.replace(second=0, microsecond=0).replace(
                minute=(ts.minute // minutes) * minutes
            )

    def _complete_bar(self, key: tuple, bar: OHLCVBar) -> None:
        """Complete and store a bar."""
        with self._lock:
            if key not in self._bars:
                self._bars[key] = deque(maxlen=self._bar_window)
            self._bars[key].append(bar)

        # Notify callbacks
        tf_name = key[1]
        if tf_name in self._bar_callbacks:
            for callback in self._bar_callbacks[tf_name]:
                try:
                    callback(bar)
                except Exception as e:
                    print(f"[Cache] Callback error: {e}")

    def on_bar_completed(self, timeframe: str, callback: Callable) -> None:
        """
        Subscribe to bar completion events.

        Args:
            timeframe: "1m", "5m", "15m", "1h", "1d"
            callback: Function(bar) called when bar completes
        """
        if timeframe not in self._bar_callbacks:
            self._bar_callbacks[timeframe] = []
        self._bar_callbacks[timeframe].append(callback)

    def get_ticks(self, symbol: str, count: int = 100) -> list:
        """Get recent ticks for symbol."""
        with self._lock:
            if symbol not in self._ticks:
                return []
            return list(self._ticks[symbol])[-count:]

    def get_bars(self, symbol: str, timeframe: str, count: int = 100) -> list[OHLCVBar]:
        """Get recent bars for symbol and timeframe."""
        key = (symbol, timeframe)
        with self._lock:
            if key not in self._bars:
                return []
            return list(self._bars[key])[-count:]

    def get_latest_bar(self, symbol: str, timeframe: str) -> Optional[OHLCVBar]:
        """Get the most recent completed bar."""
        bars = self.get_bars(symbol, timeframe, count=1)
        return bars[-1] if bars else None

    def get_current_bar(self, symbol: str, timeframe: str) -> Optional[OHLCVBar]:
        """Get the currently forming bar (not yet complete)."""
        key = (symbol, timeframe)
        with self._lock:
            return self._current_bar.get(key)

    def get_stats(self) -> dict:
        """Get cache statistics."""
        with self._lock:
            stats = {
                "symbols": len(self._symbols),
                "ticks_total": sum(len(t) for t in self._ticks.values()),
                "bars_total": sum(len(b) for b in self._bars.values()),
                "timeframes": list(self.TIMEFRAMES.keys()),
            }
            for tf in self.TIMEFRAMES:
                stats[f"bars_{tf}"] = len(self._bars.get((None, tf), []))
            return stats

    def close(self) -> None:
        """Close resources."""
        if self._logger:
            self._logger.close()

        # Complete all current bars
        with self._lock:
            for key, bar in list(self._current_bar.items()):
                if bar:
                    self._complete_bar(key, bar)
