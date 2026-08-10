"""
GT System - Production Feed

Wires FeedHandler → PipelineChain → Strategy Engine.
Throttled display updates. Latency tracking built-in.

Architecture:
    IBKR → FeedHandler → [Sequence → Dedup → FastValidator] → Strategy Engine
                                         ↕ Dashboard (10Hz throttled)
"""
import asyncio
import bisect
import time
from collections import deque
from typing import Callable, Optional

from src.feed.handler import FeedHandler, Tick, TickHandler
from src.feed.pipeline.base import PipelineChain
from src.feed.pipeline.sequence import SequenceMonitor
from src.feed.pipeline.deduplicator import Deduplicator
from src.feed.pipeline.fast_validator import FastValidator


class PipelineEntryHandler(TickHandler):
    """
    Entry point handler that routes ticks from FeedHandler into PipelineChain.

    Measures pipeline-stage latency: time from `on_tick` entry through the
    validator chain. Reports p50/p95/p99 percentiles + max over a rolling
    window of recent ticks (1024 samples) — more useful than avg/max for
    spotting tail outliers in a quant context.
    """
    __slots__ = ('pipeline', '_perf', '_samples', '_max_latency',
                 '_strategy_cb', '_dashboard_cb', '_last_display', '_pending_pipeline',
                 '_forward_quote_ticks')

    # Rolling window of recent pipeline latencies (ms). Bounded so memory
    # is fixed and percentile computation stays O(n log n) over a constant n.
    _WINDOW = 1024

    def __init__(self, pipeline: PipelineChain,
                 strategy_callback: Callable[[Tick], None] = None,
                 dashboard_callback: Optional[Callable[[Tick], None]] = None,
                 forward_quote_ticks: bool = False):
        super().__init__("PipelineEntry")
        self.pipeline = pipeline
        self._perf = time.perf_counter
        self._samples: deque = deque(maxlen=self._WINDOW)
        self._max_latency = 0.0  # sticky max-since-start
        self._strategy_cb = strategy_callback
        self._dashboard_cb = dashboard_callback
        # Quote-driven assets (index/FX CFDs, spot FX) have NO trade prints —
        # `last` stays 0, so the default `last > 0` strategy gate would starve
        # the engine of ticks entirely (no on_tick, no heartbeat, no feed log).
        # When True, forward ticks that carry a valid bid/ask book even with
        # last == 0. Default False keeps equity behaviour byte-identical.
        self._forward_quote_ticks = forward_quote_ticks
        self._last_display = 0.0
        self._pending_pipeline = None

    def on_tick(self, tick: Tick):
        # FAST PATH — validation FIRST, then dispatch.
        #
        # Previously the strategy callback ran synchronously while the
        # pipeline was scheduled fire-and-forget; that meant the engine
        # acted on bad ticks (zero LTP, crossed book, out-of-sequence,
        # duplicates) before the validators saw them. The validators are
        # all sub-microsecond (FastValidator: bid<=ask, ltp>0; Deduplicator:
        # dict lookup; SequenceMonitor: int compare), so running them
        # inline here costs negligible latency and gates everything.
        t0 = self._perf()
        try:
            validated = self.pipeline.process(tick)
        except Exception as e:
            import sys
            print(f"[Pipeline] Error processing tick: {e}", file=sys.stderr)
            validated = None

        if validated is None:
            return None  # Bad tick — drop before strategy/dashboard see it

        # Strategy gets validated trade ticks (last > 0). For quote-driven
        # assets (index/FX CFDs, spot FX) there are NO trade prints, so also
        # forward ticks that carry a valid two-sided book (bid & ask > 0).
        # Without this the engine never sees a CFD tick: no on_tick, no
        # heartbeat refresh (→ "Price stale" blocks), no feed.csv rows.
        strategy_ok = validated.last > 0 or (
            self._forward_quote_ticks and validated.bid > 0 and validated.ask > 0
        )
        if self._strategy_cb and strategy_ok:
            self._strategy_cb(validated)

        # Dashboard update — no throttle, also gated by validation
        if self._dashboard_cb:
            self._dashboard_cb(validated)

        # Record pipeline-stage latency in ms for percentile reporting.
        # Measures only this handler's work (validation + sync dispatch);
        # excludes async work scheduled downstream.
        elapsed_ms = (self._perf() - t0) * 1000.0
        self._samples.append(elapsed_ms)
        if elapsed_ms > self._max_latency:
            self._max_latency = elapsed_ms

        return validated

    async def _run_pipeline(self, tick: Tick):
        """Kept for backwards compatibility; on_tick now runs the pipeline
        synchronously upstream of the strategy callback. Safe no-op."""
        return None

    def on_error(self, error: Exception):
        pass

    def get_latency_stats(self) -> dict:
        """Return p50/p95/p99/max latency over the rolling window."""
        n = len(self._samples)
        if n == 0:
            return {
                'p50_ms': 0.0, 'p95_ms': 0.0, 'p99_ms': 0.0,
                'max_ms': 0.0, 'count': 0,
            }
        # Sort a snapshot — bounded at _WINDOW samples so this is O(W log W)
        # per call, called at the dashboard's 300ms cadence not per-tick.
        sorted_samples = sorted(self._samples)
        def _pct(p):
            idx = min(int(p * n), n - 1)
            return sorted_samples[idx]
        return {
            'p50_ms': round(_pct(0.50), 4),
            'p95_ms': round(_pct(0.95), 4),
            'p99_ms': round(_pct(0.99), 4),
            'max_ms': round(self._max_latency, 4),
            'count': n,
            # Compat with older display code that read avg_ms
            'avg_ms': round(sum(self._samples) / n, 4),
        }


class ProductionFeed:
    """
    Production-ready feed pipeline.

    Usage:
        feed = ProductionFeed(feed_handler, engine.on_tick, dashboard.on_tick)
        feed.subscribe("NVDA")  # Symbol passed from run_live.py
        await feed.start()
    """
    __slots__ = ('feed', 'pipeline', 'entry_handler')

    def __init__(
        self,
        feed_handler: FeedHandler,
        strategy_callback: Callable[[Tick], None],
        dashboard_callback: Optional[Callable[[Tick], None]] = None,
        forward_quote_ticks: bool = False,
    ):
        self.feed = feed_handler

        # Build pipeline: Sequence → Dedup → FastValidator
        self.pipeline = PipelineChain()
        self.pipeline.add_stage(SequenceMonitor())
        self.pipeline.add_stage(Deduplicator())
        self.pipeline.add_stage(FastValidator())

        # Create entry handler with callbacks (subscribed to FeedHandler)
        # Entry handler handles: pipeline → strategy → dashboard (10Hz throttled)
        self.entry_handler = PipelineEntryHandler(
            self.pipeline,
            strategy_callback=strategy_callback,
            dashboard_callback=dashboard_callback,
            forward_quote_ticks=forward_quote_ticks,
        )
        self.feed.subscribe(self.entry_handler)

    async def subscribe(self, symbol: str):
        await self.feed.subscribe_symbol(symbol)

    async def start(self):
        await self.feed.start()

    async def stop(self):
        await self.feed.stop()

    def get_latency_stats(self) -> dict:
        return self.entry_handler.get_latency_stats()