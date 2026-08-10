"""Invariant DSL — declarative correctness assertions over engine state.

WHY THIS EXISTS:
    The codebase currently expresses correctness assertions imperatively,
    scattered through engine.py (`if self._position_open and ...`). That
    style has three problems for hedge-fund-grade testing:

    1. NOT REUSABLE — same logical invariant gets re-implemented in
       multiple places, drift between copies is silent.
    2. NOT INTROSPECTABLE — can't ask "which invariants are evaluated
       at which states?" because they're if-statements buried in code.
    3. NOT TRACKED — no coverage metric for "has scenario X exercised
       invariant Y in state Z?".

THE FIX (the Citadel pattern):
    Invariants as data. Each invariant is a self-contained object with:
        - identity (name, description, severity)
        - scope (which events trigger evaluation)
        - predicate (does the system state satisfy the invariant?)
        - diagnostic (rich detail when it fails)

    The harness collects them in a registry. Scenarios opt into a subset
    (or all). The runner fires them after every relevant event and
    aggregates violations + coverage.

WHAT YOU CAN DO WITH THIS:
    - Run the same invariant suite against MockBroker AND IBKRPaperBackend
      — divergence between sim and reality surfaces immediately.
    - Run invariants LIVE in production as runtime tripwires — same
      code, just a different evaluation context.
    - Generate a coverage matrix: which (invariant × scenario × asset
      class × market regime) combinations have been exercised at all.
    - Bug-class regression certificate: "this invariant catches bug-class
      X; here's the scenario where it would fire."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional, Protocol, runtime_checkable


# ════════════════════════════════════════════════════════════════════════════
# CORE TYPES
# ════════════════════════════════════════════════════════════════════════════

class Severity(str, Enum):
    """Severity of an invariant violation.

    INFO     — informational; doesn't fail the scenario.
    WARN     — fails the scenario in strict mode; logged otherwise.
    CRITICAL — always fails the scenario; this would lose real money.
    """
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class EventKind(str, Enum):
    """Categories of events that can trigger invariant evaluation.

    An invariant declares its `scope` — the kinds it cares about. The
    runner only fires it after those event kinds, which keeps the per-
    event overhead bounded.
    """
    TICK         = "TICK"          # market data update
    EXECUTION    = "EXECUTION"     # fill arrived from broker
    COMMISSION   = "COMMISSION"    # commission report arrived
    STATUS       = "STATUS"        # order status change (cancel/reject)
    PLACED       = "PLACED"        # engine submitted an order
    MODIFIED     = "MODIFIED"      # engine modified an order in-place
    CANCELLED    = "CANCELLED"     # engine cancelled an order
    STATE_CHANGE = "STATE_CHANGE"  # engine state machine transitioned
    RESTART      = "RESTART"       # engine restarted (state restored from disk)
    RECONCILE    = "RECONCILE"     # reconcile loop completed
    TICK_TIMER   = "TICK_TIMER"    # periodic — e.g., every 30s health check


@dataclass(frozen=True, slots=True)
class Violation:
    """A single invariant violation, with all info needed for triage.

    Replay: `seed + scenario_id + event_seq` uniquely identifies the
    state of the world at violation time. The harness saves these to
    disk so any failure can be replayed with one command.
    """
    invariant_name: str
    severity: Severity
    scenario_id: str            # which scenario was running
    event_seq: int              # sequence number within the scenario
    event_kind: EventKind       # what kind of event triggered the check
    timestamp: datetime         # simulated/real time of the violation
    expected: Any               # what the invariant expected to see
    actual: Any                 # what it actually saw
    diagnostic: str             # human-readable explanation
    context: dict = field(default_factory=dict)  # structured detail (qty, prices, ids, ...)
    seed: Optional[int] = None  # the RNG seed for replay


@runtime_checkable
class Invariant(Protocol):
    """An invariant is a callable that observes engine+backend state
    after a specific event kind and returns a Violation if it fails."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def severity(self) -> Severity: ...

    @property
    def scope(self) -> frozenset[EventKind]:
        """Event kinds this invariant cares about. Runner only invokes
        check() after one of these kinds."""
        ...

    def check(
        self,
        env: "InvariantEnv",  # forward ref, defined below
        event_kind: EventKind,
        event_seq: int,
        event_payload: dict,
    ) -> Optional[Violation]:
        """Evaluate the invariant. Returns None if satisfied, Violation
        if not. MUST NOT mutate env."""
        ...


class InvariantEnv(Protocol):
    """The slice of the world an invariant can observe.

    Deliberately narrow — invariants see the engine + the backend's
    most-recent snapshot. They CAN'T cause side effects (no place_order,
    no time advance). This keeps invariants pure observers; they can be
    run in any order, multiple times, without affecting outcomes.
    """

    @property
    def engine(self) -> Any:
        """The Engine instance under test."""
        ...

    @property
    def backend(self) -> Any:
        """The Backend (MockBroker, IBKRPaperBackend, ...)."""
        ...

    @property
    def scenario_id(self) -> str: ...

    @property
    def seed(self) -> Optional[int]: ...

    @property
    def now(self) -> datetime:
        """Current simulated/real time, via the harness Clock."""
        ...

    async def broker_positions(self) -> list:
        """Snapshot the broker's current positions (logical-ticker form)."""
        ...

    async def broker_open_orders(self) -> list:
        """Snapshot the broker's currently-resting orders."""
        ...

    async def broker_executions_since(self, since: datetime) -> list: ...
    async def broker_commission_reports_since(self, since: datetime) -> list: ...


# ════════════════════════════════════════════════════════════════════════════
# REGISTRY — central catalog of all known invariants
# ════════════════════════════════════════════════════════════════════════════

class InvariantRegistry:
    """Process-wide registry. Concrete invariants register themselves
    on import; scenarios pull subsets by name or by tag.

    Why central registry: enables coverage tracking ("which of the N
    invariants have any scenario ever exercised?") and bulk operations
    ("run ALL invariants on this scenario").
    """

    __slots__ = ('_by_name', '_by_tag')

    def __init__(self) -> None:
        self._by_name: dict[str, Invariant] = {}
        self._by_tag: dict[str, set[str]] = {}

    def register(self, invariant: Invariant, tags: tuple[str, ...] = ()) -> None:
        if invariant.name in self._by_name:
            raise ValueError(f"Invariant '{invariant.name}' already registered")
        self._by_name[invariant.name] = invariant
        for tag in tags:
            self._by_tag.setdefault(tag, set()).add(invariant.name)

    def get(self, name: str) -> Invariant:
        return self._by_name[name]

    def all(self) -> list[Invariant]:
        return list(self._by_name.values())

    def by_tag(self, tag: str) -> list[Invariant]:
        return [self._by_name[n] for n in self._by_tag.get(tag, ())]

    def by_scope(self, event_kind: EventKind) -> list[Invariant]:
        return [inv for inv in self._by_name.values() if event_kind in inv.scope]


# Module-level default registry. Concrete invariants register here.
REGISTRY = InvariantRegistry()


# ════════════════════════════════════════════════════════════════════════════
# COVERAGE TRACKER — answers "what have we tested?"
# ════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class CoverageRecord:
    """A single (invariant × event-kind × asset-class) coverage cell.

    Aggregated across all scenarios in a campaign to produce a coverage
    matrix. Gaps in the matrix are bugs in the test suite, not the code.
    """
    invariant_name: str
    event_kind: EventKind
    asset_class: str
    evaluations: int = 0
    violations: int = 0


class CoverageTracker:
    """Accumulates coverage across a campaign. Mergeable across processes
    (multiprocessing parallel runs)."""

    __slots__ = ('_cells',)

    def __init__(self) -> None:
        self._cells: dict[tuple[str, EventKind, str], CoverageRecord] = {}

    def record_evaluation(
        self,
        invariant_name: str,
        event_kind: EventKind,
        asset_class: str,
        violated: bool,
    ) -> None:
        key = (invariant_name, event_kind, asset_class)
        cell = self._cells.get(key)
        if cell is None:
            cell = CoverageRecord(invariant_name, event_kind, asset_class)
            self._cells[key] = cell
        cell.evaluations += 1
        if violated:
            cell.violations += 1

    def merge(self, other: "CoverageTracker") -> None:
        for key, other_cell in other._cells.items():
            cell = self._cells.get(key)
            if cell is None:
                self._cells[key] = CoverageRecord(
                    invariant_name=other_cell.invariant_name,
                    event_kind=other_cell.event_kind,
                    asset_class=other_cell.asset_class,
                    evaluations=other_cell.evaluations,
                    violations=other_cell.violations,
                )
            else:
                cell.evaluations += other_cell.evaluations
                cell.violations += other_cell.violations

    def cells(self) -> list[CoverageRecord]:
        return list(self._cells.values())

    def gaps(self, registry: InvariantRegistry, asset_classes: list[str]) -> list[tuple[str, EventKind, str]]:
        """Return (invariant, event_kind, asset_class) tuples that have
        NEVER been evaluated. These are coverage holes worth filling."""
        seen = set(self._cells.keys())
        gaps = []
        for inv in registry.all():
            for kind in inv.scope:
                for ac in asset_classes:
                    if (inv.name, kind, ac) not in seen:
                        gaps.append((inv.name, kind, ac))
        return gaps


# ════════════════════════════════════════════════════════════════════════════
# RESULT AGGREGATION — for the campaign report
# ════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class CampaignResult:
    """Aggregate result of one campaign run (potentially 100k+ scenarios).

    Sliceable by invariant, by scenario, by asset class, etc. for the
    final report and for triage queues.
    """
    total_scenarios: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    violations: list[Violation] = field(default_factory=list)
    coverage: CoverageTracker = field(default_factory=CoverageTracker)
    duration_seconds: float = 0.0
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    @property
    def pass_rate(self) -> float:
        if self.total_scenarios == 0:
            return 1.0
        return self.passed / self.total_scenarios

    def violations_by_invariant(self) -> dict[str, list[Violation]]:
        out: dict[str, list[Violation]] = {}
        for v in self.violations:
            out.setdefault(v.invariant_name, []).append(v)
        return out

    def violations_by_severity(self) -> dict[Severity, list[Violation]]:
        out: dict[Severity, list[Violation]] = {Severity.CRITICAL: [], Severity.WARN: [], Severity.INFO: []}
        for v in self.violations:
            out[v.severity].append(v)
        return out


__all__ = [
    "Severity", "EventKind",
    "Violation", "Invariant", "InvariantEnv",
    "InvariantRegistry", "REGISTRY",
    "CoverageRecord", "CoverageTracker",
    "CampaignResult",
]
