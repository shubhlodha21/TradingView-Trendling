"""Hedge-fund-grade testing harness for the GT trading engine.

Architectural pillars (Citadel/JS/HRT pattern):

  1. Backend Protocol      — single seam between engine and the world.
                             MockBroker (synthetic), IBKRPaperBackend (paper),
                             IBKRLiveBackend (canary) all implement it.
                             Engine binds to the protocol; backend swaps
                             with one line.

  2. Clock + RNG injection — engine becomes a pure function of
                             (initial_state, clock_advance_sequence,
                              broker_events, rng_calls). Same inputs →
                             same outputs every run.

  3. Invariant DSL         — correctness assertions as first-class data.
                             Same invariants run against synthetic, paper,
                             and live. Coverage trackable.

  4. Scenario framework    — declarative test cases. Same scenario code
                             runs against any backend.

  5. Parallel runner       — multiprocessing.Pool, 100k scenarios in
                             ~30 min on a 16-core machine. Coverage
                             matrix + failure triage built in.

  6. Hypothesis fuzzer     — generative state-machine testing. Shrinks
                             to minimal failing command sequence.

For the architecture rationale + bug-class regression coverage rationale,
see the build plan in tasks A1-A9.
"""

# Core types and protocols (the architectural surface).
from .backend import (
    Backend, BackendCapabilities, BackendEvent,
    BracketRequest, CommissionReport, ContractRef, ExecutionRef,
    MarketTick, OrderRef, OrderRequest, OrderStatusUpdate, PositionRef,
)
from .clock import Clock, FrozenClock, RealClock, SimulatedClock
from .env import TestEnv
from .invariant import (
    CampaignResult, CoverageRecord, CoverageTracker, EventKind,
    Invariant, InvariantEnv, InvariantRegistry, REGISTRY,
    Severity, Violation,
)
from .rng import RNG, DeterministicRNG, NullRNG
from .scenario import (
    ExpectedOutcome, ForceCancelOrder, ForceDisconnect,
    InjectClockSkew, InjectOrderRejection,
    MarketScript, OperatorCommand, RestartEngine,
    Scenario, ScenarioEnv,
)

__all__ = [
    # Backend layer
    "Backend", "BackendCapabilities", "BackendEvent",
    "BracketRequest", "CommissionReport", "ContractRef", "ExecutionRef",
    "MarketTick", "OrderRef", "OrderRequest", "OrderStatusUpdate", "PositionRef",
    # Determinism layer
    "Clock", "FrozenClock", "RealClock", "SimulatedClock",
    "RNG", "DeterministicRNG", "NullRNG",
    # Invariant layer
    "CampaignResult", "CoverageRecord", "CoverageTracker", "EventKind",
    "Invariant", "InvariantEnv", "InvariantRegistry", "REGISTRY",
    "Severity", "Violation",
    # Scenario layer
    "ExpectedOutcome", "ForceCancelOrder", "ForceDisconnect",
    "InjectClockSkew", "InjectOrderRejection",
    "MarketScript", "OperatorCommand", "RestartEngine",
    "Scenario", "ScenarioEnv",
    # Glue
    "TestEnv",
]
