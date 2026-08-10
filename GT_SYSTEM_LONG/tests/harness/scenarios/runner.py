"""Scenario runner — executes a scenario end-to-end against MockGateway.

This is the lightweight runner that doesn't require the production
engine to be in the loop. Scenarios drive MockGateway directly via
their `setup_fn` and `actions`, with the market simulator providing
ticks and the invariant suite asserting correctness throughout.

Once the engine integration adapter is added (deferred to A6b), the
same Scenario class will run against the real engine — only the runner
changes.

Architecture:

    Scenario (data: instrument, market, operator commands)
        ↓
    SimpleRunner.run(scenario):
        - Build MockGateway + SimulatedClock + DeterministicRNG
        - Apply scenario.initial_state (seed preexisting orders/positions)
        - Spawn:
            * MarketSimulator coroutine (drives ticks)
            * Scenario.actions coroutine (place/modify/cancel orders)
            * Invariant evaluator (fires after every fill/modify/etc.)
        - Wait for completion or timeout
        - Return ScenarioResult (pass/fail + violations + coverage)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from ..clock import SimulatedClock
from ..invariant import (
    CoverageTracker, EventKind, Invariant, REGISTRY, Severity, Violation,
)
from ..mock_gateway import MockGateway, MockGatewayConfig
from ..rng import DeterministicRNG
from ..scenario import MarketScript, Scenario
from ..market.tick_stream import MarketSimulator


# ════════════════════════════════════════════════════════════════════════════
# SimpleScenario — purely backend-driven (no engine)
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class SimpleScenario:
    """Scenario form that drives MockGateway directly (no production engine).

    Used to validate the harness itself + run scenario classes that don't
    need the engine in the loop (e.g., MockGateway behavior tests).

    For engine-in-the-loop scenarios, the Scenario class from scenario.py
    is used with a different runner (A6b — engine integration).
    """
    name: str
    description: str
    instrument: str
    asset_class: str
    mock_gateway_config: MockGatewayConfig
    market_script: MarketScript
    actions: Callable[[MockGateway, SimulatedClock], Awaitable[None]]
    invariant_names: tuple[str, ...] = field(default_factory=tuple)  # empty = all
    seed: int = 42
    max_duration_seconds: float = 60.0
    pre_setup: Optional[Callable[[MockGateway], None]] = None  # seed orphans, etc.


# ════════════════════════════════════════════════════════════════════════════
# ScenarioResult — pass/fail + violations + diagnostics
# ════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class ScenarioResult:
    scenario_name: str
    seed: int
    passed: bool
    violations: list[Violation] = field(default_factory=list)
    duration_seconds: float = 0.0
    event_count: int = 0
    fill_count: int = 0
    coverage: Optional[CoverageTracker] = None
    error: Optional[str] = None

    @property
    def critical_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == Severity.CRITICAL]


# ════════════════════════════════════════════════════════════════════════════
# SimpleRunner — executes one SimpleScenario
# ════════════════════════════════════════════════════════════════════════════

class SimpleRunner:
    """Runs SimpleScenarios against MockGateway. Suitable for harness
    self-tests + scenario library development before engine integration."""

    def __init__(self, registry=REGISTRY):
        self.registry = registry

    async def run(self, scenario: SimpleScenario) -> ScenarioResult:
        """Execute a single scenario; return pass/fail + violations."""
        clock = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        rng = DeterministicRNG(seed=scenario.seed)
        gateway = MockGateway(scenario.instrument, clock, rng, scenario.mock_gateway_config)
        coverage = CoverageTracker()
        violations: list[Violation] = []

        # Pull invariants from registry
        if scenario.invariant_names:
            invariants = [self.registry.get(n) for n in scenario.invariant_names]
        else:
            invariants = self.registry.all()

        # Pre-setup (seed orphans, positions, etc.)
        if scenario.pre_setup is not None:
            scenario.pre_setup(gateway)

        # Connect gateway
        await gateway.connect()

        # Wire instrumentation: every MockGateway callback also triggers
        # invariant evaluation. We snapshot what the gateway dispatches.
        seq = [0]  # closure-mutable

        async def _eval_invariants(kind: EventKind, payload: dict) -> None:
            seq[0] += 1
            env_view = _RunnerInvariantEnv(gateway, clock, rng, scenario.name, scenario.seed)
            for inv in invariants:
                if kind not in inv.scope:
                    continue
                try:
                    v = inv.check(env_view, kind, seq[0], payload)
                except Exception as e:
                    v = Violation(
                        invariant_name=inv.name, severity=Severity.CRITICAL,
                        scenario_id=scenario.name, event_seq=seq[0], event_kind=kind,
                        timestamp=clock.now(), expected="no exception",
                        actual=f"{type(e).__name__}: {e}",
                        diagnostic=f"Invariant {inv.name} crashed during check()",
                        seed=scenario.seed,
                    )
                coverage.record_evaluation(
                    inv.name, kind, scenario.asset_class,
                    violated=(v is not None),
                )
                if v is not None:
                    violations.append(v)

        # Hook MockGateway callbacks so engine-style events trigger invariants.
        def _on_fill(*args):
            asyncio.create_task(_eval_invariants(EventKind.EXECUTION, {'args': args}))

        def _on_status(*args):
            asyncio.create_task(_eval_invariants(EventKind.STATUS, {'args': args}))

        def _on_commission(*args):
            asyncio.create_task(_eval_invariants(EventKind.COMMISSION, {'args': args}))

        gateway._on_fill = _on_fill
        gateway._on_order_status = _on_status
        gateway._on_commission = _on_commission

        # Also evaluate invariants on every tick (so PRICE_ON_VENUE_GRID
        # + SELL_STOP_BELOW_ENTRY get checked while the market moves).
        def _on_tick(bid, ask, last):
            asyncio.create_task(_eval_invariants(EventKind.TICK, {'bid': bid, 'ask': ask, 'last': last}))
        gateway._tick_observer = _on_tick

        # Build market simulator
        simulator = MarketSimulator(gateway, clock, rng, scenario.market_script)

        # Run market simulator + scenario actions concurrently
        wall_start = clock.monotonic()
        error: Optional[str] = None
        try:
            scenario_task = asyncio.create_task(scenario.actions(gateway, clock))
            simulator_task = asyncio.create_task(simulator.run())
            await asyncio.wait_for(
                asyncio.gather(scenario_task, simulator_task, return_exceptions=True),
                timeout=scenario.max_duration_seconds,
            )
        except asyncio.TimeoutError:
            error = f"scenario exceeded max_duration_seconds={scenario.max_duration_seconds}"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        # Final reconcile-style invariant pass at end-of-scenario
        await _eval_invariants(EventKind.RECONCILE, {})

        await gateway.disconnect()
        duration = clock.monotonic() - wall_start

        critical_violations = [v for v in violations if v.severity == Severity.CRITICAL]
        passed = (error is None) and not critical_violations

        return ScenarioResult(
            scenario_name=scenario.name,
            seed=scenario.seed,
            passed=passed,
            violations=violations,
            duration_seconds=duration,
            event_count=seq[0],
            fill_count=len(gateway._fill_history),
            coverage=coverage,
            error=error,
        )


# ════════════════════════════════════════════════════════════════════════════
# Concrete InvariantEnv impl for the runner
# ════════════════════════════════════════════════════════════════════════════

class _RunnerInvariantEnv:
    """Minimal InvariantEnv for the SimpleRunner. Invariants get this
    instead of the full TestEnv (which requires the production engine)."""

    __slots__ = ('_backend', '_clock', '_rng', '_sid', '_seed')

    def __init__(self, backend, clock, rng, scenario_id, seed):
        self._backend = backend
        self._clock = clock
        self._rng = rng
        self._sid = scenario_id
        self._seed = seed

    @property
    def engine(self):
        return None  # SimpleRunner has no engine

    @property
    def backend(self):
        return self._backend

    @property
    def scenario_id(self) -> str:
        return self._sid

    @property
    def seed(self) -> Optional[int]:
        return self._seed

    @property
    def now(self) -> datetime:
        return self._clock.now()

    async def broker_positions(self) -> list:
        return await self._backend.get_positions()

    async def broker_open_orders(self) -> list:
        return self._backend.fetch_open_orders()

    async def broker_executions_since(self, since: datetime) -> list:
        return [
            f for f in self._backend.get_all_fills()
            if since is None or f.execution.time >= since
        ]

    async def broker_commission_reports_since(self, since: datetime) -> list:
        # Synthetic: not directly stored separately; could be derived from fills.
        return []


__all__ = ["SimpleScenario", "SimpleRunner", "ScenarioResult"]
