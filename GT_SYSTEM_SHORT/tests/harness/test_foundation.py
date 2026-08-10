"""Foundation smoke tests — verify A1's primitives are coherent.

These tests check the *architecture* — types, protocols, determinism
guarantees. They don't test the engine yet (that's A2). They DO
guarantee that everything we built in A1 is internally consistent
and ready for the next layers to plug into.
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from tests.harness import (
    Backend, BackendEvent, ContractRef,
    Clock, RealClock, SimulatedClock, FrozenClock,
    RNG, DeterministicRNG, NullRNG,
    EventKind, Severity, Violation,
    REGISTRY, InvariantRegistry, CoverageTracker,
    Scenario, MarketScript, RestartEngine, ForceDisconnect,
)


# ════════════════════════════════════════════════════════════════════════════
# Determinism: same seed → same RNG sequence
# ════════════════════════════════════════════════════════════════════════════

class TestDeterminism:

    def test_same_seed_same_rng_sequence(self):
        a = DeterministicRNG(seed=42)
        b = DeterministicRNG(seed=42)
        seq_a = [a.uniform(0, 1) for _ in range(100)]
        seq_b = [b.uniform(0, 1) for _ in range(100)]
        assert seq_a == seq_b, "Same seed must produce identical RNG sequence"

    def test_different_seeds_different_sequence(self):
        a = DeterministicRNG(seed=42)
        b = DeterministicRNG(seed=43)
        # Should be different (with overwhelming probability for 100 samples)
        seq_a = [a.uniform(0, 1) for _ in range(100)]
        seq_b = [b.uniform(0, 1) for _ in range(100)]
        assert seq_a != seq_b, "Different seeds should produce different sequences"

    def test_derive_independent_streams(self):
        """Critical: rng.derive('a') and rng.derive('b') must be
        independent of each other AND of the parent rng's sequence."""
        parent = DeterministicRNG(seed=100)
        child_a = parent.derive("paper_slippage")
        child_b = parent.derive("market_jitter")

        seq_a = [child_a.uniform(0, 1) for _ in range(50)]
        seq_b = [child_b.uniform(0, 1) for _ in range(50)]

        # Children should be different from each other
        assert seq_a != seq_b

        # Children should be reproducible
        child_a_again = DeterministicRNG(seed=100).derive("paper_slippage")
        seq_a_again = [child_a_again.uniform(0, 1) for _ in range(50)]
        assert seq_a == seq_a_again

    def test_derive_does_not_disturb_parent(self):
        """If derive() consumed parent state, adding a derive() call
        would shift every subsequent parent.uniform() call. That's the
        bug we're guarding against."""
        parent_1 = DeterministicRNG(seed=200)
        seq_1 = [parent_1.uniform(0, 1) for _ in range(10)]

        parent_2 = DeterministicRNG(seed=200)
        _ = parent_2.derive("any_label")          # extra derive call
        seq_2 = [parent_2.uniform(0, 1) for _ in range(10)]

        # Sequences MUST be identical despite the derive() between them.
        assert seq_1 == seq_2, (
            "derive() must not disturb parent RNG state — otherwise "
            "adding a derive() call anywhere shifts every test's seed."
        )

    def test_null_rng_always_returns_lo(self):
        rng = NullRNG()
        for _ in range(20):
            assert rng.uniform(5.0, 10.0) == 5.0
            assert rng.randint(1, 100) == 1
            assert rng.random() == 0.0


# ════════════════════════════════════════════════════════════════════════════
# Clock semantics
# ════════════════════════════════════════════════════════════════════════════

class TestClocks:

    def test_real_clock_advances(self):
        c = RealClock()
        a = c.now()
        # advance natural time
        import time as _t
        _t.sleep(0.001)
        b = c.now()
        assert b >= a

    def test_real_clock_returns_utc(self):
        c = RealClock()
        now = c.now()
        assert now.tzinfo is not None
        assert now.utcoffset().total_seconds() == 0

    @pytest.mark.asyncio
    async def test_simulated_clock_advances_via_sleep(self):
        start = datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc)
        c = SimulatedClock(start=start)
        assert c.now() == start
        await c.sleep(60)
        delta = (c.now() - start).total_seconds()
        assert delta == 60.0

    @pytest.mark.asyncio
    async def test_simulated_clock_monotonic_strictly_non_decreasing(self):
        c = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        a = c.monotonic()
        await c.sleep(5)
        b = c.monotonic()
        assert b >= a + 5.0

    def test_simulated_clock_requires_tz_aware(self):
        with pytest.raises(ValueError):
            SimulatedClock(start=datetime(2026, 6, 6, 14, 30))  # naive

    @pytest.mark.asyncio
    async def test_simulated_clock_runs_fast(self):
        """Critical perf property: simulated sleep is sub-millisecond
        even for huge advances. This is what makes 100k scenarios fast."""
        import time as _t
        c = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        wall_start = _t.monotonic()
        await c.sleep(86400)  # 1 day of simulated time
        wall_elapsed = _t.monotonic() - wall_start
        assert wall_elapsed < 0.01, f"Simulated 1-day sleep took {wall_elapsed}s wall"

    @pytest.mark.asyncio
    async def test_frozen_clock_never_advances(self):
        at = datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc)
        c = FrozenClock(at=at)
        await c.sleep(60)
        assert c.now() == at


# ════════════════════════════════════════════════════════════════════════════
# Invariant DSL — registry + coverage tracking
# ════════════════════════════════════════════════════════════════════════════

class _FakeInvariant:
    """Test stub — satisfies the Invariant Protocol structurally."""
    def __init__(self, name: str, scope: frozenset[EventKind]):
        self._name = name
        self._scope = scope

    @property
    def name(self) -> str: return self._name
    @property
    def description(self) -> str: return "test invariant"
    @property
    def severity(self) -> Severity: return Severity.CRITICAL
    @property
    def scope(self) -> frozenset[EventKind]: return self._scope

    def check(self, env, event_kind, event_seq, event_payload):
        return None  # always passes


class TestInvariantRegistry:

    def test_registry_holds_invariants(self):
        reg = InvariantRegistry()
        inv = _FakeInvariant("test_inv_1", frozenset([EventKind.EXECUTION]))
        reg.register(inv)
        assert reg.get("test_inv_1") is inv

    def test_registry_rejects_duplicates(self):
        reg = InvariantRegistry()
        reg.register(_FakeInvariant("dup", frozenset([EventKind.TICK])))
        with pytest.raises(ValueError):
            reg.register(_FakeInvariant("dup", frozenset([EventKind.TICK])))

    def test_registry_by_scope(self):
        reg = InvariantRegistry()
        reg.register(_FakeInvariant("a", frozenset([EventKind.EXECUTION])))
        reg.register(_FakeInvariant("b", frozenset([EventKind.TICK])))
        reg.register(_FakeInvariant("c", frozenset([EventKind.EXECUTION, EventKind.STATUS])))

        exec_invs = reg.by_scope(EventKind.EXECUTION)
        names = {i.name for i in exec_invs}
        assert names == {"a", "c"}

    def test_registry_by_tag(self):
        reg = InvariantRegistry()
        reg.register(_FakeInvariant("x", frozenset([EventKind.TICK])), tags=("position", "critical"))
        reg.register(_FakeInvariant("y", frozenset([EventKind.TICK])), tags=("position",))
        names = {i.name for i in reg.by_tag("position")}
        assert names == {"x", "y"}
        assert {i.name for i in reg.by_tag("critical")} == {"x"}


class TestCoverageTracker:

    def test_records_evaluations(self):
        c = CoverageTracker()
        c.record_evaluation("inv_1", EventKind.EXECUTION, "EURUSD", violated=False)
        c.record_evaluation("inv_1", EventKind.EXECUTION, "EURUSD", violated=True)
        cells = c.cells()
        assert len(cells) == 1
        assert cells[0].evaluations == 2
        assert cells[0].violations == 1

    def test_separate_cells_per_asset_class(self):
        c = CoverageTracker()
        c.record_evaluation("inv_1", EventKind.EXECUTION, "EURUSD", violated=False)
        c.record_evaluation("inv_1", EventKind.EXECUTION, "AAPL",   violated=False)
        assert len(c.cells()) == 2

    def test_merge_aggregates_across_processes(self):
        """Critical: parallel runs produce separate CoverageTrackers
        that must merge correctly to a single campaign-wide view."""
        a = CoverageTracker()
        a.record_evaluation("inv_1", EventKind.TICK, "EURUSD", violated=False)
        a.record_evaluation("inv_1", EventKind.TICK, "EURUSD", violated=False)

        b = CoverageTracker()
        b.record_evaluation("inv_1", EventKind.TICK, "EURUSD", violated=True)
        b.record_evaluation("inv_2", EventKind.STATUS, "AAPL", violated=False)

        a.merge(b)
        cells = {(c.invariant_name, c.event_kind, c.asset_class): c for c in a.cells()}
        assert cells[("inv_1", EventKind.TICK, "EURUSD")].evaluations == 3
        assert cells[("inv_1", EventKind.TICK, "EURUSD")].violations == 1
        assert cells[("inv_2", EventKind.STATUS, "AAPL")].evaluations == 1

    def test_gaps_identifies_unexercised_combinations(self):
        reg = InvariantRegistry()
        reg.register(_FakeInvariant("inv_1", frozenset([EventKind.EXECUTION])))
        reg.register(_FakeInvariant("inv_2", frozenset([EventKind.TICK])))

        cov = CoverageTracker()
        cov.record_evaluation("inv_1", EventKind.EXECUTION, "EURUSD", violated=False)

        gaps = cov.gaps(reg, asset_classes=["EURUSD", "AAPL"])
        # inv_1 × EXECUTION × AAPL is a gap, inv_2 × TICK × {EURUSD, AAPL} are gaps
        assert ("inv_1", EventKind.EXECUTION, "AAPL") in gaps
        assert ("inv_2", EventKind.TICK, "EURUSD") in gaps
        assert ("inv_2", EventKind.TICK, "AAPL") in gaps
        assert ("inv_1", EventKind.EXECUTION, "EURUSD") not in gaps


# ════════════════════════════════════════════════════════════════════════════
# Scenario primitives — declarative test cases compose cleanly
# ════════════════════════════════════════════════════════════════════════════

class TestScenarioComposition:

    def test_scenario_id_includes_seed(self):
        s = Scenario(
            name="bracket_lifecycle",
            description="test",
            instrument="EURUSD",
            asset_class="FX_CASH",
            engine_config={},
            seed=42,
        )
        assert s.scenario_id == "bracket_lifecycle#42"

    def test_market_script_has_required_fields(self):
        m = MarketScript(
            initial_bid=Decimal("1.16410"),
            initial_ask=Decimal("1.16420"),
            regime="brownian",
            duration_seconds=120.0,
        )
        assert m.tick_rate_hz == 5.0
        assert m.has_trades is True

    def test_operator_commands_are_sortable_by_time(self):
        commands = [
            ForceDisconnect(at_seconds=10.0),
            RestartEngine(at_seconds=5.0),
        ]
        ordered = sorted(commands, key=lambda c: c.at_seconds)
        assert ordered[0].at_seconds == 5.0


# ════════════════════════════════════════════════════════════════════════════
# Protocol structural conformance
# ════════════════════════════════════════════════════════════════════════════

class TestProtocolConformance:
    """Confirm our Protocol definitions accept structurally-conforming
    implementations. This is what makes the architecture work — anyone
    can write a Backend without inheriting from a base class."""

    def test_real_clock_satisfies_clock_protocol(self):
        c = RealClock()
        assert isinstance(c, Clock)

    def test_simulated_clock_satisfies_clock_protocol(self):
        c = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        assert isinstance(c, Clock)

    def test_frozen_clock_satisfies_clock_protocol(self):
        c = FrozenClock(at=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        assert isinstance(c, Clock)

    def test_deterministic_rng_satisfies_rng_protocol(self):
        r = DeterministicRNG(seed=1)
        assert isinstance(r, RNG)

    def test_null_rng_satisfies_rng_protocol(self):
        r = NullRNG()
        assert isinstance(r, RNG)


# ════════════════════════════════════════════════════════════════════════════
# Bug-class regression coverage (forward-looking)
# ════════════════════════════════════════════════════════════════════════════
#
# These tests don't pass yet — they document which historical bug class
# each upcoming invariant will catch as a regression. Each is marked
# xfail until the corresponding invariant lands in A5.

class TestBugClassRegressionCoverage:
    """Forward-looking — confirms our planned invariants will catch
    every bug class we've discovered. Uncomment as A5 lands."""

    @pytest.mark.skip(reason="POSITION_QTY_MATCH lands in A5")
    def test_position_qty_match_catches_fx_symbol_mismatch_bug(self): ...

    @pytest.mark.skip(reason="PRICE_ON_VENUE_GRID lands in A5")
    def test_price_on_grid_catches_round_to_2_bug(self): ...

    @pytest.mark.skip(reason="COMMISSION_TRUTH lands in A5")
    def test_commission_truth_catches_equity_formula_leak(self): ...

    @pytest.mark.skip(reason="MODIFY_NOT_REPLACE lands in A5")
    def test_modify_not_replace_catches_orphan_bracket_class(self): ...

    @pytest.mark.skip(reason="STOP_PRICE_DIVERGENCE lands in A5")
    def test_stop_price_divergence_catches_wire_truncation(self): ...
