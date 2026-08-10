"""TestEnv — the runtime context that binds harness components together.

Lifecycle:
    env = TestEnv.build(scenario, backend, clock, rng, invariants)
    await env.setup()
        # - persist scenario.initial_state to in-memory state file
        # - wire engine to backend
        # - subscribe market data
    await env.run()
        # - kick off engine's start()
        # - drive market simulator
        # - schedule operator commands
        # - evaluate invariants on every relevant event
        # - capture full event log
    result = env.teardown()
        # - close engine
        # - return list of violations + final state

Concurrency model:
    One scenario = one asyncio event loop. All components share it.
    Parallelism across scenarios = multiprocessing.Pool (runner.py).
    Inside a scenario, all I/O is async via the injected Clock.

Determinism:
    Given (scenario, seed), same outcome every run. This is the property
    the entire harness rests on.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .backend import Backend, BackendEvent
from .clock import Clock
from .invariant import (
    CoverageTracker, EventKind, Invariant, InvariantRegistry, REGISTRY,
    Severity, Violation,
)
from .rng import RNG
from .scenario import Scenario, ScenarioEnv


@dataclass(slots=True)
class TestEnv:
    """The runtime context for one scenario execution.

    Holds references to every component (engine, backend, clock, rng,
    invariants) and orchestrates them. Implements ScenarioEnv so
    OperatorCommands can drive it.
    """

    scenario: Scenario
    backend: Backend
    clock: Clock
    rng: RNG
    invariants: list[Invariant]
    coverage: CoverageTracker
    state_dir: Path                      # in-memory or temp dir for state files

    # Populated during run()
    engine: Any = None                   # Engine instance (set in setup)
    event_log: list[BackendEvent] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    _event_seq: int = 0
    _pending_rejection_message: Optional[str] = None
    _disconnect_until_seconds: Optional[float] = None

    # ── Build path ────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        scenario: Scenario,
        backend: Backend,
        clock: Clock,
        rng: RNG,
        state_dir: Path,
        registry: InvariantRegistry = REGISTRY,
    ) -> "TestEnv":
        """Construct an env. Invariants are pulled from the scenario's
        opt-in list (empty list = all invariants)."""
        if scenario.invariant_names:
            invariants = [registry.get(n) for n in scenario.invariant_names]
        else:
            invariants = registry.all()
        return cls(
            scenario=scenario,
            backend=backend,
            clock=clock,
            rng=rng,
            invariants=invariants,
            coverage=CoverageTracker(),
            state_dir=state_dir,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def setup(self) -> None:
        """Prepare engine + backend before the scenario clock starts.

        - If scenario.initial_state is set, persist it to a temp state
          file so the engine's _load_state restores it on start.
        - Wire backend callbacks to our event-log recorder + invariant
          evaluator.
        """
        if self.scenario.initial_state is not None:
            await self._write_initial_state_file(self.scenario.initial_state)

        # Wire backend callbacks. Every backend event triggers
        # (a) event-log append, (b) invariant evaluation, (c) coverage
        # tracking. ALL invariants run on EVERY event — the scope filter
        # in invariant.check() is the per-invariant gate.
        self.backend.on_execution(self._on_execution)
        self.backend.on_commission(self._on_commission)
        self.backend.on_status(self._on_status)

        # Engine construction happens here (deferred so we can pass clock
        # and rng). Implementation depends on engine refactor (task A2) —
        # for now we keep this as a stub; A2 will fill in.
        self.engine = self._construct_engine()

        # Connect backend (synthetic = instant; paper/live = network call).
        await self.backend.connect()

    async def run(self) -> None:
        """Execute the scenario.

        Launches three concurrent tasks:
          1. Engine main loop (start()).
          2. Market simulator (feeds ticks to backend).
          3. Operator-command scheduler.

        Returns when ALL three are done OR scenario.max_duration_seconds
        wall-clock has passed (hard timeout).
        """
        engine_task = asyncio.create_task(self._run_engine())
        market_task = asyncio.create_task(self._run_market())
        ops_task = asyncio.create_task(self._run_operator_commands())

        try:
            await asyncio.wait_for(
                asyncio.gather(engine_task, market_task, ops_task),
                timeout=self.scenario.max_duration_seconds,
            )
        except asyncio.TimeoutError:
            self._record_violation(Violation(
                invariant_name="SCENARIO_TIMEOUT",
                severity=Severity.CRITICAL,
                scenario_id=self.scenario.scenario_id,
                event_seq=self._event_seq,
                event_kind=EventKind.TICK_TIMER,
                timestamp=self.clock.now(),
                expected=f"completion within {self.scenario.max_duration_seconds}s",
                actual="timeout",
                diagnostic="Scenario exceeded max_duration_seconds wall clock",
                seed=self.rng.seed,
            ))
        finally:
            for t in (engine_task, market_task, ops_task):
                if not t.done():
                    t.cancel()

    async def teardown(self) -> tuple[list[Violation], list[BackendEvent]]:
        """Clean shutdown — disconnect backend, stop engine, return results."""
        if self.engine is not None:
            await self._stop_engine()
        await self.backend.disconnect()
        return self.violations, self.event_log

    # ── ScenarioEnv impl (operator command targets) ───────────────────

    async def restart_engine(self, skip_disk_persist: bool = False) -> None:
        """Kill engine, optionally corrupt state, restart it.

        IMPORTANT: state-file persistence respects scenario semantics —
        a clean restart preserves disk; a `skip_disk_persist=True`
        restart simulates state-file truncation/corruption."""
        if self.engine is None:
            return
        await self._stop_engine()
        if skip_disk_persist:
            await self._corrupt_state_file()
        # New engine instance; it'll load state from disk via _load_state.
        self.engine = self._construct_engine()
        self._record_backend_event(EventKind.RESTART, {})
        await self._evaluate_invariants(EventKind.RESTART, payload={})

    async def force_disconnect(self, reconnect_after_seconds: float = 3.0) -> None:
        """Simulate broker disconnect. Backend's connection drops; engine
        should detect and recover."""
        # MockBroker supports this directly; paper/live can't (the LIVE
        # connection drop is the actual scenario we're simulating).
        if hasattr(self.backend, 'force_disconnect'):
            await self.backend.force_disconnect()
            self._disconnect_until_seconds = self.clock.monotonic() + reconnect_after_seconds
            asyncio.create_task(self._reconnect_later(reconnect_after_seconds))

    async def _reconnect_later(self, after: float) -> None:
        await self.clock.sleep(after)
        if hasattr(self.backend, 'force_reconnect'):
            await self.backend.force_reconnect()

    async def force_cancel(self, engine_id: str) -> None:
        """Cancel the order at the broker without telling the engine."""
        if hasattr(self.backend, 'force_cancel_by_engine_id'):
            await self.backend.force_cancel_by_engine_id(engine_id)

    async def inject_next_order_rejection(self, message: str) -> None:
        """The next place_order call to the backend will fail."""
        self._pending_rejection_message = message
        if hasattr(self.backend, 'inject_next_rejection'):
            await self.backend.inject_next_rejection(message)

    async def skew_clock(self, skew_seconds: float) -> None:
        """Jump the simulated clock forward (or backward)."""
        if hasattr(self.clock, 'advance'):
            self.clock.advance(skew_seconds)

    # ── Internal: backend callback handlers ───────────────────────────

    async def _on_execution(self, execution) -> None:
        self._record_backend_event(EventKind.EXECUTION, {"execution": execution})
        await self._evaluate_invariants(EventKind.EXECUTION, payload={"execution": execution})

    async def _on_commission(self, commission_report) -> None:
        self._record_backend_event(EventKind.COMMISSION, {"commission": commission_report})
        await self._evaluate_invariants(EventKind.COMMISSION, payload={"commission": commission_report})

    async def _on_status(self, status_update) -> None:
        self._record_backend_event(EventKind.STATUS, {"status": status_update})
        await self._evaluate_invariants(EventKind.STATUS, payload={"status": status_update})

    # ── Internal: invariant evaluation ────────────────────────────────

    async def _evaluate_invariants(self, event_kind: EventKind, payload: dict) -> None:
        """Run every in-scope invariant against the current state."""
        env_view = _InvariantEnvView(self)
        for inv in self.invariants:
            if event_kind not in inv.scope:
                continue
            try:
                violation = inv.check(env_view, event_kind, self._event_seq, payload)
            except Exception as e:
                # An invariant that crashes is itself a violation.
                violation = Violation(
                    invariant_name=inv.name,
                    severity=Severity.CRITICAL,
                    scenario_id=self.scenario.scenario_id,
                    event_seq=self._event_seq,
                    event_kind=event_kind,
                    timestamp=self.clock.now(),
                    expected="no exception",
                    actual=f"{type(e).__name__}: {e}",
                    diagnostic=f"Invariant '{inv.name}' raised during check()",
                    seed=self.rng.seed,
                )
            self.coverage.record_evaluation(
                inv.name, event_kind, self.scenario.asset_class,
                violated=(violation is not None),
            )
            if violation is not None:
                self._record_violation(violation)

    def _record_violation(self, v: Violation) -> None:
        self.violations.append(v)

    def _record_backend_event(self, kind: EventKind, payload: dict) -> None:
        self._event_seq += 1
        self.event_log.append(BackendEvent(
            seq=self._event_seq,
            timestamp=self.clock.now(),
            kind=kind.value,
            payload=payload,
        ))

    # ── Internal: engine construction (to be filled in A2) ────────────

    def _construct_engine(self) -> Any:
        """Build an Engine instance wired to our backend, clock, rng.

        STUB FOR A1 — actual implementation pending A2 (engine determinism
        refactor). For now, returns None and the runner short-circuits.
        Once A2 lands, this becomes:

            from src.strategy.engine import Engine
            from src.config.models import Config

            cfg = Config(**self.scenario.engine_config)
            return Engine(
                config=cfg,
                gateway=self._gateway_adapter(),  # wraps Backend
                clock=self.clock,
                rng=self.rng,
                storage=self._storage_adapter(),
            )
        """
        return None  # filled in A2

    async def _run_engine(self) -> None:
        """Stub — A2 fills this."""
        return

    async def _run_market(self) -> None:
        """Stub — A4 fills this. Calls into tests/harness/market/."""
        return

    async def _run_operator_commands(self) -> None:
        """Schedule operator commands at their `at_seconds` offsets."""
        commands = sorted(self.scenario.operator_commands, key=lambda c: c.at_seconds)
        start_monotonic = self.clock.monotonic()
        for cmd in commands:
            target = start_monotonic + cmd.at_seconds
            delta = target - self.clock.monotonic()
            if delta > 0:
                await self.clock.sleep(delta)
            await cmd.execute(self)

    async def _stop_engine(self) -> None:
        """Stub — A2 fills this."""
        return

    async def _write_initial_state_file(self, state: dict) -> None:
        """Write a state file the engine will _load_state from."""
        path = self.state_dir / f".gt_state_{self.scenario.instrument}_{self.scenario.engine_config.get('client_id', 1)}.json"
        path.write_text(json.dumps(state))

    async def _corrupt_state_file(self) -> None:
        """Truncate the state file mid-write — simulates a crash during
        the periodic save."""
        for path in self.state_dir.glob(".gt_state_*.json"):
            content = path.read_text()
            # Truncate to 30% of length — produces invalid JSON.
            truncated = content[: max(10, len(content) // 3)]
            path.write_text(truncated)


# ════════════════════════════════════════════════════════════════════════════
# InvariantEnvView — narrow read-only window onto TestEnv for invariants
# ════════════════════════════════════════════════════════════════════════════

class _InvariantEnvView:
    """Implements `InvariantEnv` Protocol. Read-only view over TestEnv.

    Invariants get this — not the whole TestEnv — so they can't mutate
    anything by accident. Pure observer pattern."""

    __slots__ = ('_env',)

    def __init__(self, env: TestEnv) -> None:
        self._env = env

    @property
    def engine(self) -> Any:
        return self._env.engine

    @property
    def backend(self) -> Any:
        return self._env.backend

    @property
    def scenario_id(self) -> str:
        return self._env.scenario.scenario_id

    @property
    def seed(self) -> Optional[int]:
        return self._env.rng.seed

    @property
    def now(self) -> datetime:
        return self._env.clock.now()

    async def broker_positions(self) -> list:
        return await self._env.backend.positions()

    async def broker_open_orders(self) -> list:
        return await self._env.backend.open_orders()

    async def broker_executions_since(self, since: datetime) -> list:
        return await self._env.backend.executions(since=since)

    async def broker_commission_reports_since(self, since: datetime) -> list:
        return await self._env.backend.commission_reports(since=since)


__all__ = ["TestEnv"]
