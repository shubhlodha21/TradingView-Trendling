"""
GT Feed System

Modular market data feed with:
- IBKR connection management
- Pipeline processing (sequence, deduplication, normalization, validation)
- Market data caching with OHLCV aggregation
- Candle builders for multi-timeframe charts
- Tick logging to CSV

Usage:
    from src.feed import (
        ConnectionManager,
        PipelineChain,
        SequenceMonitor,
        Deduplicator,
        Normalizer,
        Validator,
        MarketDataCache,
        CandleBuilder,
    )

    # Build pipeline
    pipeline = PipelineChain()
    pipeline.add_stage(SequenceMonitor())
    pipeline.add_stage(Deduplicator())
    pipeline.add_stage(Normalizer())
    pipeline.add_stage(Validator())

    # Create candle builder
    builder = CandleBuilder(symbol="AAPL", timeframes=["1m", "5m", "15m"])
    builder.on_bar("5m", lambda c: print(f"5m close: {c.close}"))

    # Process ticks
    for tick in ticks:
        result = pipeline.process(tick)
        if result:
            builder.add_tick(result)
"""

# Connection
from src.feed.connection import (
    ConnectionManager,
    ConnectionConfig,
    ConnectionObserver,
    ConnectionState,
)

# Handler
from src.feed.handler import (
    FeedHandler,
    TickHandler,
    Tick,
    MessageType,
    TickFilter,
)

# Pipeline
from src.feed.pipeline.base import PipelineChain, PipelineStage, PipelineStats
from src.feed.pipeline.sequence import SequenceMonitor
from src.feed.pipeline.deduplicator import Deduplicator
from src.feed.pipeline.normalizer import Normalizer
from src.feed.pipeline.validator import Validator

# Cache
from src.feed.cache import MarketDataCache, TickLogger, OHLCVBar, TickRecord

# Candles
from src.feed.candles import Candle, CandleBuilder, TIMEFRAMES

__all__ = [
    # Connection
    "ConnectionManager",
    "ConnectionConfig",
    "ConnectionObserver",
    "ConnectionState",
    # Handler
    "FeedHandler",
    "TickHandler",
    "Tick",
    "MessageType",
    "TickFilter",
    # Pipeline
    "PipelineChain",
    "PipelineStage",
    "PipelineStats",
    "SequenceMonitor",
    "Deduplicator",
    "Normalizer",
    "Validator",
    # Cache
    "MarketDataCache",
    "TickLogger",
    "OHLCVBar",
    "TickRecord",
    # Candles
    "Candle",
    "CandleBuilder",
    "TIMEFRAMES",
]
