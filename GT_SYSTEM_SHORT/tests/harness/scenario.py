"""Scenario — declarative test case definition.

A scenario answers FIVE questions:
    1. WHAT instrument? (logical_ticker)
    2. WHAT initial state? (FRESH / restored from saved state file)
    3. WHAT happens in the market? (MarketScript — tick sequence/regime)
    4. WHAT does the operator do? (commands: restart, force-cancel, etc.)
    5. WHAT must be true throughout? (invariant set + expected outcome)

Then the runner glues it together:
    SimulatedClock + MockBroker (or paper) + Engine + Invariant set
    → execute → verify

THE GOAL is that SAME scenario object runs identically on MockBroker,
IBKRPaperBackend, IBKRLiveBackend. The only difference is the backend.
That's how we get sim-vs-paper-vs-live divergence detection for free.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════════════
# OPERATOR COMMANDS — things the scenario can do TO the engine mid-run
# ════════════════════════════════════════════════════════════════════════════
#
# These let scenarios stress edge cases that pure market-driven tests
# can't reach: restart at specific times, force-cancel, network drop, etc.

class OperatorCommand(abc.ABC):
    """Abstract base for operator commands. Each concrete subclass is
    a frozen dataclass with at minimum an `at_seconds: float` field
    (when, relative to scenario start, the command fires) and an
    async `execute(env)` method that drives the scenario.

    Note: we don't declare `at_seconds` as an abstract property here —
    that would shadow the field on dataclass subclasses (Python 3.14
    dataclass machinery sees the property descriptor as a "default"
    value and reorders fields incorrectly). The runner just reads
    `cmd.at_seconds` duck-typed; each concrete subclass declares it
    as a field.
    """

    @abc.abstractmethod
    async def execute(self, env: "ScenarioEnv") -> None: ...


@dataclass(frozen=True, slots=True)
class RestartEngine(OperatorCommand):
    """Kill the engine, then restart it. Tests state-restoration paths.
    The state file is preserved across the restart (just like real life)."""
    at_seconds: float
    skip_disk_persist: bool = False  # if True, simulate state-file corruption

    async def execute(self, env: "ScenarioEnv") -> None:
        await env.restart_engine(skip_disk_persist=self.skip_disk_persist)


@dataclass(frozen=True, slots=True)
class ForceDisconnect(OperatorCommand):
    """Simulate broker disconnect. Engine should recover."""
    at_seconds: float
    reconnect_after_seconds: float = 3.0

    async def execute(self, env: "ScenarioEnv") -> None:
        await env.force_disconnect(reconnect_after_seconds=self.reconnect_after_seconds)


@dataclass(frozen=True, slots=True)
class ForceCancelOrder(OperatorCommand):
    """Cancel a specific resting order at the broker, without telling
    the engine. Simulates manual TWS cancel by the operator."""
    at_seconds: float
    engine_id: str

    async def execute(self, env: "ScenarioEnv") -> None:
        await env.force_cancel(self.engine_id)


@dataclass(frozen=True, slots=True)
class InjectOrderRejection(OperatorCommand):
    """The NEXT order the engine places will be rejected by the broker.
    Simulates IBKR returning error 110 or similar."""
    at_seconds: float
    rejection_message: str = "Simulated broker rejection"

    async def execute(self, env: "ScenarioEnv") -> None:
        await env.inject_next_order_rejection(self.rejection_message)


@dataclass(frozen=True, slots=True)
class InjectClockSkew(OperatorCommand):
    """Jump the simulated clock forward (or backward, for testing
    weird-broker scenarios where exec times are out of order)."""
    at_seconds: float
    skew_seconds: float

    async def execute(self, env: "ScenarioEnv") -> None:
        await env.skew_clock(self.skew_seconds)


# ════════════════════════════════════════════════════════════════════════════
# MARKET SCRIPT — what the market does during the scenario
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class MarketScript:
    """Specification of synthetic market behavior for a scenario.

    The actual tick generation happens in tests/harness/market/tick_stream.py
    (built in task A4). This is the DECLARATIVE part — the scenario tells
    the simulator what regime to run.
    """
    initial_bid: Decimal
    initial_ask: Decimal
    regime: str                       # 'brownian' / 'trending' / 'mean_revert' / 'gap' / 'halt' / 'flash_crash' / 'fast_market' / 'quote_only'
    duration_seconds: float           # how long to generate ticks for
    tick_rate_hz: float = 5.0         # ticks per second
    # Regime-specific knobs (used selectively by the generator):
    drift_per_second: Decimal = Decimal("0")   # for trending
    volatility_per_second: Decimal = Decimal("0.0001")  # for brownian/all
    gap_at_seconds: Optional[float] = None     # for 'gap' / 'flash_crash'
    gap_magnitude: Optional[Decimal] = None
    halt_at_seconds: Optional[float] = None    # for 'halt'
    halt_duration_seconds: Optional[float] = None
    has_trades: bool = True                    # False for quote-only (CFD-style)


# ════════════════════════════════════════════════════════════════════════════
# EXPECTED OUTCOME — what the scenario asserts at the end
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class ExpectedOutcome:
    """Optional end-of-scenario assertions. None of these fields are
    required — invariants do most of the verification. This is for
    scenario-specific final-state checks."""
    final_state: Optional[str] = None              # 'MONITORING' / 'IN_POSITION' / ...
    final_position_qty: Optional[Decimal] = None
    final_realized_pnl_range: Optional[tuple[Decimal, Decimal]] = None
    must_have_completed_round_trips: Optional[int] = None
    must_not_have_violated: tuple[str, ...] = field(default_factory=tuple)


# ════════════════════════════════════════════════════════════════════════════
# SCENARIO — the test case object
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class Scenario:
    """A single test case. Pure data — runs identically against any
    backend that implements the Backend Protocol."""

    name: str
    description: str
    instrument: str                           # logical ticker
    asset_class: str                          # for capability/coverage tracking
    engine_config: dict                       # kwargs for Config(...)
    initial_state: Optional[dict] = None      # if non-None, persisted to state file before engine start
    market: Optional[MarketScript] = None     # None = use real broker's market data (paper/live)
    operator_commands: tuple[OperatorCommand, ...] = field(default_factory=tuple)
    invariant_names: tuple[str, ...] = field(default_factory=tuple)  # empty = all
    expected: Optional[ExpectedOutcome] = None
    tags: tuple[str, ...] = field(default_factory=tuple)  # for filtering
    seed: int = 0                             # deterministic seed for synthetic backend
    start_at: Optional[datetime] = None       # simulated clock starting point
    max_duration_seconds: float = 300.0       # hard timeout

    @property
    def scenario_id(self) -> str:
        """Stable id for logging + replay: name + seed."""
        return f"{self.name}#{self.seed}"


# ════════════════════════════════════════════════════════════════════════════
# SCENARIO ENV — the API operator commands use
# ════════════════════════════════════════════════════════════════════════════
#
# Defined here as a Protocol so commands don't import the concrete
# runner module (avoids circular deps). The runner provides the
# concrete impl.

class ScenarioEnv:
    """API surface that OperatorCommand instances use to drive the
    scenario. Concrete implementation lives in env.py."""

    async def restart_engine(self, skip_disk_persist: bool = False) -> None: ...
    async def force_disconnect(self, reconnect_after_seconds: float = 3.0) -> None: ...
    async def force_cancel(self, engine_id: str) -> None: ...
    async def inject_next_order_rejection(self, message: str) -> None: ...
    async def skew_clock(self, skew_seconds: float) -> None: ...


__all__ = [
    "OperatorCommand", "RestartEngine", "ForceDisconnect",
    "ForceCancelOrder", "InjectOrderRejection", "InjectClockSkew",
    "MarketScript", "ExpectedOutcome", "Scenario", "ScenarioEnv",
]
