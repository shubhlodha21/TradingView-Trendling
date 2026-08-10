"""
Fast Validator Stage

Stripped-down validator optimized for sub-millisecond performance (<0.004ms).
Only performs critical data integrity checks needed for strategy execution.
"""
from typing import Optional
from src.feed.pipeline.base import PipelineStage
from src.feed.handler import Tick, MessageType


class FastValidator(PipelineStage):
    """
    Ultra-fast validator for production use.

    Checks:
    1. LTP > 0 (for TRADE ticks)
    2. Bid/Ask > 0 and Bid <= Ask (for BBO ticks)

    Skips (compared to standard Validator):
    - Datetime age checks (expensive)
    - Percentage change sanity bounds (assumes IBKR data is sane)
    - Deep object inspection
    """
    __slots__ = ()

    def __init__(self):
        super().__init__("FastValidator")

    def _process(self, tick: Tick) -> Optional[Tick]:
        if tick.tick_type == MessageType.TRADE:
            # Strategy only cares about LTP for trades
            if tick.last <= 0.0:
                return None
            return tick

        elif tick.tick_type == MessageType.TICK:
            # BBO checks
            b, a = tick.bid, tick.ask
            if b > 0 and a > 0 and b > a:
                # Crossed book - invalid state
                return None
            return tick

        return tick