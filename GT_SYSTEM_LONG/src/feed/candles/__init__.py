"""
Candle Builder Package

Multi-timeframe OHLCV candle aggregation from tick data.

Classes:
    Candle: OHLCV candlestick data
    BarBuilder: Single bar aggregation
    CandleBuilder: Multi-timeframe builder
    AggregatedCandleBuilder: Higher -> lower timeframe aggregation

Usage:
    from src.feed.candles import CandleBuilder, Candle

    builder = CandleBuilder(symbol="AAPL", timeframes=["1m", "5m"])
    builder.on_bar("5m", lambda c: print(f"5m: {c}"))

    for tick in ticks:
        builder.add_tick(tick)

    bars = builder.get_bars("5m")
"""

from src.feed.candles.builder import (
    Candle,
    BarBuilder,
    CandleBuilder,
    AggregatedCandleBuilder,
    TIMEFRAMES,
    align_to_timeframe,
)

__all__ = [
    "Candle",
    "BarBuilder",
    "CandleBuilder",
    "AggregatedCandleBuilder",
    "TIMEFRAMES",
    "align_to_timeframe",
]
