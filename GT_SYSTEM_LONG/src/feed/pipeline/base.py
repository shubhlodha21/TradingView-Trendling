"""
Data Pipeline Base

Chain of Responsibility pattern for processing market data.

Each stage in the pipeline processes ticks and passes them to the next stage.
If a stage rejects a tick (e.g., invalid, duplicate), it can stop propagation.

Architecture:
    [Tick] -> [Stage 1] -> [Stage 2] -> ... -> [Consumer]
                |           |
              reject      reject

Usage:
    stage1 = SequenceMonitor()
    stage2 = Deduplicator()
    stage3 = Normalizer()

    chain = PipelineChain([stage1, stage2, stage3])
    chain.process(tick)

Design Pattern: Chain of Responsibility
- Each stage has a single responsibility
- Stages are independent and testable
- Easy to add/remove/reorder stages
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Callable

from src.feed.handler import Tick, TickHandler


class PipelineEvent(Enum):
    """Events emitted by pipeline stages."""
    TICK_ACCEPTED = "TICK_ACCEPTED"
    TICK_REJECTED = "TICK_REJECTED"
    TICK_DUPLICATE = "TICK_DUPLICATE"
    TICK_GAP_DETECTED = "TICK_GAP_DETECTED"
    TICK_INVALID = "TICK_INVALID"
    TICK_STALE = "TICK_STALE"
    PIPELINE_ERROR = "PIPELINE_ERROR"


@dataclass(slots=True)
class PipelineStats:
    """Statistics for a pipeline stage."""
    ticks_processed: int = 0
    ticks_accepted: int = 0
    ticks_rejected: int = 0
    last_processed: Optional[datetime] = None
    last_error: Optional[str] = None


class PipelineStage(ABC):
    """
    Abstract base class for pipeline stages.

    A pipeline stage receives ticks, processes them, and optionally
    passes them to the next stage. Stages can reject ticks by
    returning None.

    Responsibilities:
    1. Process incoming ticks
    2. Maintain statistics
    3. Log events
    4. Pass valid ticks to next stage

    Usage:
        class MyStage(PipelineStage):
            def _process(self, tick: Tick) -> Optional[Tick]:
                # Custom processing logic
                return tick  # or None to reject
    """

    def __init__(self, name: str):
        self.name = name
        self._stats = PipelineStats()
        self._next: Optional['PipelineStage'] = None
        self._enabled = True
        self._error_callback: Optional[Callable[[str, Exception], None]] = None
        self._ts = datetime.now  # Cached reference to avoid global lookup

    @property
    def stats(self) -> PipelineStats:
        return self._stats

    @property
    def next_stage(self) -> Optional['PipelineStage']:
        """Get the next stage in the chain."""
        return self._next

    def set_next(self, stage: 'PipelineStage') -> 'PipelineStage':
        """
        Set the next stage in the chain.

        Returns the next stage so you can chain calls:
            stage1.set_next(stage2).set_next(stage3)
        """
        self._next = stage
        return stage

    def set_error_callback(self, callback: Callable[[str, Exception], None]) -> None:
        """Set callback for errors."""
        self._error_callback = callback

    def process(self, tick: Tick) -> Optional[Tick]:
        """
        Process a tick through this stage.

        This is the main entry point. It handles statistics tracking
        and error catching. The actual processing is delegated to _process().

        Args:
            tick: The tick to process

        Returns:
            The processed tick if accepted, None if rejected
        """
        if not self._enabled:
            return tick  # Bypass this stage

        self._stats.ticks_processed += 1

        try:
            result = self._process(tick)
            self._stats.last_processed = self._ts()

            if result is not None:
                self._stats.ticks_accepted += 1
                # Pass to next stage
                if self._next:
                    return self._next.process(result)
                return result
            else:
                self._stats.ticks_rejected += 1
                return None

        except Exception as e:
            self._stats.last_error = str(e)
            self._stats.ticks_rejected += 1
            if self._error_callback:
                self._error_callback(self.name, e)
            return None

    @abstractmethod
    def _process(self, tick: Tick) -> Optional[Tick]:
        """
        Stage-specific processing logic.

        Override this method to implement custom processing.

        Args:
            tick: The tick to process

        Returns:
            The processed tick if accepted, None to reject
        """
        pass

    def enable(self) -> None:
        """Enable this stage."""
        self._enabled = True

    def disable(self) -> None:
        """Disable this stage (ticks pass through)."""
        self._enabled = False

    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self._stats = PipelineStats()

    def get_report(self) -> dict:
        """Get detailed stage report."""
        return {
            "name": self.name,
            "enabled": self._enabled,
            "ticks_processed": self._stats.ticks_processed,
            "ticks_accepted": self._stats.ticks_accepted,
            "ticks_rejected": self._stats.ticks_rejected,
            "last_processed": self._stats.last_processed.isoformat() if self._stats.last_processed else None,
            "last_error": self._stats.last_error,
        }


class PipelineChain:
    """
    Manages a chain of pipeline stages.

    Provides a simple interface to add stages and process ticks.

    Usage:
        chain = PipelineChain()
        chain.add_stage(SequenceMonitor())
        chain.add_stage(Deduplicator())
        chain.add_stage(Normalizer())

        result = chain.process(tick)
    """

    def __init__(self):
        self._stages: list[PipelineStage] = []
        self._head: Optional[PipelineStage] = None
        self._tail: Optional[PipelineStage] = None

    def add_stage(self, stage: PipelineStage) -> 'PipelineChain':
        """
        Add a stage to the end of the chain.

        Args:
            stage: The stage to add

        Returns:
            self for chaining
        """
        self._stages.append(stage)

        if self._head is None:
            self._head = stage
        else:
            self._tail.set_next(stage)  # type: ignore

        self._tail = stage
        return self

    def process(self, tick: Tick) -> Optional[Tick]:
        """
        Process a tick through all stages.

        Args:
            tick: The tick to process

        Returns:
            The processed tick if accepted by all stages, None otherwise
        """
        if not self._head:
            return tick
        return self._head.process(tick)

    def get_stage(self, name: str) -> Optional[PipelineStage]:
        """Get a stage by name."""
        for stage in self._stages:
            if stage.name == name:
                return stage
        return None

    def enable_all(self) -> None:
        """Enable all stages."""
        for stage in self._stages:
            stage.enable()

    def disable_all(self) -> None:
        """Disable all stages (bypass)."""
        for stage in self._stages:
            stage.disable()

    def reset_all_stats(self) -> None:
        """Reset statistics for all stages."""
        for stage in self._stages:
            stage.reset_stats()

    def get_full_report(self) -> dict:
        """Get report for all stages."""
        return {
            "stages": [stage.get_report() for stage in self._stages],
            "total_stages": len(self._stages),
        }
