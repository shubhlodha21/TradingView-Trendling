"""
Deduplicator Pipeline Stage

Removes duplicate ticks from the market data stream.
Critical for preventing double processing and incorrect signals.

Duplicate Detection Logic:
1. Generate a unique key for each tick based on:
   - Symbol
   - Price (bid, ask, last)
   - Volume
   - Timestamp (rounded to millisecond)
2. Track seen keys in a rolling window
3. Reject ticks with duplicate keys

Why Deduplicates Matter:
- IBKR can send the same tick multiple times
- Network retransmissions can cause duplicates
- Candle builders would have incorrect volume if duplicates not filtered
- Strategy signals could fire twice for same price move

Design Pattern: Chain of Responsibility (pipeline stage)
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.feed.pipeline.base import PipelineStage, PipelineEvent
from src.feed.handler import Tick


@dataclass(slots=True)
class DedupConfig:
    """Configuration for deduplication."""
    window_seconds: float = 1.0       # How long to remember ticks
    max_cache_size: int = 10000       # Max ticks to cache
    timestamp_precision_ms: int = 100  # Round timestamps to this precision
    compare_prices: bool = True        # Include prices in dedup key
    compare_volume: bool = True        # Include volume in dedup key
    allow_immediate_retransmit: bool = False  # Allow same tick after n seconds


class Deduplicator(PipelineStage):
    """
    Removes duplicate ticks from the stream.

    Duplicates can occur due to:
    - IBKR sending the same tick multiple times
    - Network retransmissions
    - Reconnection leading to replay of recent ticks

    Algorithm:
    1. Generate a unique key for the tick
    2. Check if we've seen this key recently
    3. If yes, reject the tick
    4. If no, add to cache and pass through

    Performance:
    - O(1) lookup using set for seen keys
    - O(n) cleanup of expired entries (done periodically)
    - Configurable cache size to limit memory

    Usage:
        dedup = Deduplicator(window_seconds=1.0)
        dedup.set_next(NextStage())
        result = dedup.process(tick)
    """

    def __init__(
        self,
        name: str = "Deduplicator",
        config: Optional[DedupConfig] = None,
    ):
        """
        Initialize deduplicator.

        Args:
            name: Stage name for logging
            config: Deduplication configuration
        """
        super().__init__(name)
        self._config = config or DedupConfig()
        self._ts = datetime.now  # Cached timestamp function

        # Cache: key -> expiry timestamp
        self._seen: dict[tuple[str, str], datetime] = {}

        # Statistics
        self._duplicates = 0
        self._unique = 0

        # Callbacks
        self._duplicate_callback = None

        # Last cleanup time
        self._last_cleanup = self._ts()

    def set_duplicate_callback(self, callback):
        """Set callback for duplicate events: callback(tick, key)"""
        self._duplicate_callback = callback

    def _process(self, tick: Tick) -> Optional[Tick]:
        """
        Check for and remove duplicate ticks.

        Algorithm:
        1. Generate dedup key from tick
        2. Check if key exists in cache
        3. If exists, reject (return None)
        4. If not exists, add to cache and pass through
        5. Periodically clean up expired entries
        """
        # Periodic cleanup
        self._maybe_cleanup()

        # Generate key for this tick
        key = self._make_key(tick)

        # Check for duplicate
        cache_key = (tick.symbol, key)
        seen = self._seen

        if cache_key in seen:
            # Duplicate detected
            self._duplicates += 1
            self._emit_duplicate_event(tick, key)
            if self._duplicate_callback:
                self._duplicate_callback(tick, key)
            return None

        # Not a duplicate - add to cache
        seen[cache_key] = self._ts()
        self._unique += 1

        return tick

    def _make_key(self, tick: Tick) -> str:
        """
        Generate a unique key for deduplication.

        The key should uniquely identify a "logical" tick to avoid
        false duplicates (different data but same symbol/time).

        Args:
            tick: The tick to generate key for

        Returns:
            String key for deduplication
        """
        parts = []

        # Timestamp (rounded to reduce sensitivity to exact timing)
        ts = tick.timestamp.timestamp()
        ts_rounded = int(ts * 1000 // self._config.timestamp_precision_ms)
        parts.append(f"t{ts_rounded}")

        # Price data (if configured)
        if self._config.compare_prices:
            if tick.last > 0:
                parts.append(f"l{tick.last}")
            if tick.bid > 0:
                parts.append(f"b{tick.bid}")
            if tick.ask > 0:
                parts.append(f"a{tick.ask}")

        # Volume (if configured)
        if self._config.compare_volume:
            parts.append(f"v{tick.volume}")

        return "|".join(parts)

    def _maybe_cleanup(self) -> None:
        """
        Periodically clean up expired entries from cache.

        Runs cleanup every second to avoid excessive overhead.
        Removes entries older than the configured window.
        """
        ts = self._ts()
        window = timedelta(seconds=self._config.window_seconds)

        # Only cleanup once per second
        if (ts - self._last_cleanup).total_seconds() < 1.0:
            return

        self._last_cleanup = ts
        cutoff = ts - window

        # Remove expired entries
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

    def _emit_duplicate_event(self, tick: Tick, key: str) -> None:
        """Emit event for duplicate tick."""
        self._emit_event(PipelineEvent.TICK_DUPLICATE, tick, {
            "key": key,
            "symbol": tick.symbol,
            "timestamp": tick.timestamp.isoformat(),
        })

    def _emit_event(self, event: PipelineEvent, tick: Tick, data: dict) -> None:
        """Emit pipeline event."""
        pass

    def get_duplicate_count(self) -> int:
        """Get count of duplicates detected."""
        return self._duplicates

    def get_unique_count(self) -> int:
        """Get count of unique ticks passed through."""
        return self._unique

    def get_duplicate_rate(self) -> float:
        """Get duplicate rate as percentage."""
        total = self._duplicates + self._unique
        if total == 0:
            return 0.0
        return (self._duplicates / total) * 100

    def reset(self) -> None:
        """Reset deduplication cache and statistics."""
        self._seen.clear()
        self._duplicates = 0
        self._unique = 0

    def get_report(self) -> dict:
        """Get detailed deduplication report."""
        base = super().get_report()
        base.update({
            "duplicates": self._duplicates,
            "unique": self._unique,
            "duplicate_rate_pct": round(self.get_duplicate_rate(), 2),
            "cache_size": len(self._seen),
            "cache_max": self._config.max_cache_size,
        })
        return base
