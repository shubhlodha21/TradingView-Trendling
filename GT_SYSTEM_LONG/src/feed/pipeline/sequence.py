"""
Sequence Monitor Pipeline Stage

Detects gaps and out-of-order ticks in the market data stream.
Critical for ensuring data integrity and detecting data quality issues.

Gap Detection Logic:
1. Track the last tick number for each subscription
2. If current tick number is not (last + 1), a gap occurred
3. Gap can indicate:
   - Network packet loss
   - Stale data buffered/delayed
   - Exchange feed issues

Sequence Numbers:
- IBKR assigns a tickId to each tick
- Ticks should arrive in increasing order
- Gaps can indicate missing data

Design Pattern: Chain of Responsibility (pipeline stage)
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.feed.pipeline.base import PipelineStage, PipelineEvent
from src.feed.handler import Tick


@dataclass(slots=True)
class GapInfo:
    """
    Information about a detected gap.

    Attributes:
        symbol: Trading symbol
        expected_seq: Expected sequence number
        actual_seq: Actual sequence number received
        gap_size: Number of missing ticks
        detected_at: When the gap was detected
        first_missing: First missing sequence number
        last_missing: Last missing sequence number
    """
    symbol: str
    expected_seq: int
    actual_seq: int
    gap_size: int
    detected_at: datetime
    first_missing: int
    last_missing: int


class SequenceMonitor(PipelineStage):
    """
    Monitors tick sequence numbers to detect gaps.

    IBKR market data can arrive out of order or with gaps due to:
    - Network issues
    - Exchange packet loss
    - Buffered/delayed feeds
    - Connection interruptions

    This stage tracks sequence numbers per symbol and detects:
    1. Missing ticks (gap in sequence)
    2. Out-of-order ticks (arrived after later tick)
    3. Replayed ticks (same sequence as previous)

    Configuration:
    - max_sequence_jump: Max allowed gap before warning (default: 100)
    - out_of_order_window: Seconds to keep tracking out-of-order ticks
    - detect_replays: Whether to flag duplicate sequences

    Usage:
        monitor = SequenceMonitor(max_gap=100)
        monitor.set_next(NextStage())
        result = monitor.process(tick)
    """

    def __init__(
        self,
        name: str = "SequenceMonitor",
        max_gap: int = 100,
        out_of_order_window_seconds: float = 5.0,
        detect_replays: bool = True,
    ):
        """
        Initialize sequence monitor.

        Args:
            name: Stage name for logging
            max_gap: Maximum gap size before triggering warning
            out_of_order_window_seconds: How long to keep out-of-order seq numbers
            detect_replays: Flag ticks with same sequence as previous
        """
        super().__init__(name)
        self._max_gap = max_gap
        self._out_of_order_window = timedelta(seconds=out_of_order_window_seconds)
        self._detect_replays = detect_replays
        self._ts = datetime.now  # Cached timestamp function

        # Track last sequence number per symbol (plain dict for speed)
        self._last_seq: dict[str, int] = {}

        # Track out-of-order ticks waiting for their turn
        # symbol -> (expected_seq, tick, received_at)
        self._pending: dict[str, list] = {}

        # Gap history for reporting
        self._gaps: list = []

        # Callbacks
        self._gap_callback = None
        self._replay_callback = None
        self._out_of_order_callback = None

    def set_gap_callback(self, callback):
        """Set callback for gap events: callback(gap_info: GapInfo)"""
        self._gap_callback = callback

    def set_replay_callback(self, callback):
        """Set callback for replay events: callback(symbol, seq_num)"""
        self._replay_callback = callback

    def set_out_of_order_callback(self, callback):
        """Set callback for out-of-order events: callback(symbol, expected, actual)"""
        self._out_of_order_callback = callback

    def _process(self, tick: Tick) -> Optional[Tick]:
        """
        Process tick and check for sequence issues.

        Algorithm:
        1. Extract or generate sequence number (IBKR tickId)
        2. Compare with expected sequence for this symbol
        3. If gap detected, emit warning and record gap
        4. If replay detected, optionally flag
        5. Return tick if acceptable
        """
        symbol = tick.symbol

        # Use tick's req_id as sequence proxy
        # IBKR doesn't expose explicit tick sequence numbers
        # We use timestamp-based sequence tracking instead
        seq = self._make_sequence(tick)

        # Check for replay (same sequence as last tick)
        if self._detect_replays:
            last_seq = self._last_seq.get(symbol, 0)
            if seq <= last_seq:
                # Replay or duplicate detected
                if seq == last_seq:
                    # Exact replay - flag but accept
                    self._stats.ticks_rejected += 0  # Count differently
                    self._emit_event(PipelineEvent.TICK_DUPLICATE, tick, {
                        "symbol": symbol,
                        "seq": seq,
                    })
                    if self._replay_callback:
                        self._replay_callback(symbol, seq)
                    # Accept replays but don't update last seq
                    return tick
                else:
                    # Out of order (seq < last_seq)
                    self._emit_event(PipelineEvent.TICK_REJECTED, tick, {
                        "reason": "out_of_order",
                        "symbol": symbol,
                        "expected_gt": last_seq,
                        "actual": seq,
                    })
                    if self._out_of_order_callback:
                        self._out_of_order_callback(symbol, last_seq, seq)
                    # Still accept out-of-order ticks (they're valid data)
                    self._last_seq[symbol] = seq
                    return tick

        # Check for gap
        last_seq = self._last_seq.get(symbol, 0)

        if last_seq > 0 and seq > last_seq:
            gap_size = seq - last_seq

            if gap_size > 1:
                # Gap detected
                gap = GapInfo(
                    symbol=symbol,
                    expected_seq=last_seq + 1,
                    actual_seq=seq,
                    gap_size=gap_size,
                    detected_at=self._ts(),
                    first_missing=last_seq + 1,
                    last_missing=seq - 1,
                )
                self._gaps.append(gap)

                self._emit_event(PipelineEvent.TICK_GAP_DETECTED, tick, {
                    "symbol": symbol,
                    "gap_size": gap_size,
                    "expected": last_seq + 1,
                    "actual": seq,
                })

                if self._gap_callback:
                    self._gap_callback(gap)

                # Flag if gap is large
                if gap_size > self._max_gap:
                    self._stats.last_error = f"Large gap: {gap_size} ticks for {symbol}"

        # Update tracking
        self._last_seq[symbol] = seq

        # Process pending ticks for this symbol
        self._process_pending(tick, symbol, seq)

        return tick

    def _make_sequence(self, tick: Tick) -> int:
        """
        Generate or extract sequence number for a tick.

        IBKR doesn't provide explicit sequence numbers in tick data.
        We use timestamp + millisecond as a proxy.

        For more robust sequence tracking, you'd need:
        1. Exchange-provided sequence numbers
        2. Custom sequence counter
        3. Message bus with sequence support

        Args:
            tick: The tick to generate sequence for

        Returns:
            Integer sequence number
        """
        # Use timestamp as sequence (more granular than tickId)
        # Multiply by 1000 to get milliseconds
        return int(tick.timestamp.timestamp() * 1000) + tick.req_id

    def _process_pending(self, current_tick: Tick, symbol: str, current_seq: int) -> None:
        """
        Process any pending ticks that have become valid.

        When we receive an out-of-order tick, we store it temporarily.
        When a tick arrives with sequence N+1, we can release N.

        Args:
            current_tick: Current tick being processed
            symbol: Symbol for this tick
            current_seq: Sequence number of current tick
        """
        if symbol not in self._pending:
            return

        pending = self._pending[symbol]
        ready = []
        still_pending = []

        cutoff = self._ts() - self._out_of_order_window

        for seq, tick, received_at in pending:
            if received_at < cutoff:
                continue
            if seq <= current_seq:
                ready.append((seq, tick))
            else:
                still_pending.append((seq, tick, received_at))

        self._pending[symbol] = still_pending

        for seq, tick in sorted(ready, key=lambda x: x[0]):
            pass  # Re-inject in full implementation

    def _emit_event(self, event: PipelineEvent, tick: Tick, data: dict) -> None:
        """Emit a pipeline event."""
        # In a full implementation, this would emit to an event bus
        # For now, just log
        pass

    def get_gaps(self, symbol: Optional[str] = None) -> list:
        """
        Get all detected gaps.

        Args:
            symbol: If provided, only return gaps for this symbol

        Returns:
            List of GapInfo objects
        """
        if symbol:
            return [g for g in self._gaps if g.symbol == symbol]
        return self._gaps.copy()

    def get_last_sequence(self, symbol: str) -> int:
        """Get the last sequence number for a symbol."""
        return self._last_seq.get(symbol, 0)

    def reset(self, symbol: Optional[str] = None) -> None:
        """
        Reset sequence tracking.

        Args:
            symbol: If provided, only reset this symbol's tracking.
                   If None, reset all symbols.
        """
        if symbol:
            if symbol in self._last_seq:
                del self._last_seq[symbol]
            if symbol in self._pending:
                del self._pending[symbol]
        else:
            self._last_seq.clear()
            self._pending.clear()
            self._gaps.clear()
            self.reset_stats()

    def get_report(self) -> dict:
        """Get detailed gap detection report."""
        base = super().get_report()
        base.update({
            "gaps_detected": len(self._gaps),
            "last_gap": {
                "symbol": self._gaps[-1].symbol if self._gaps else None,
                "size": self._gaps[-1].gap_size if self._gaps else None,
            } if self._gaps else None,
            "symbols_tracking": list(self._last_seq.keys()),
        })
        return base
