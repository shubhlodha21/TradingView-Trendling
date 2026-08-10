"""Market simulator tests — every regime + the MarketSimulator driver loop.

Verifies:
  1. Each generator produces deterministic output from same seed
  2. Each regime's defining property (drift direction, gap occurrence, etc.)
  3. All emitted prices snap to the venue's tick grid
  4. MarketSimulator drives MockGateway correctly
  5. Halt regime produces actual silence (tested via tick counter)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from tests.harness import (
    DeterministicRNG, SimulatedClock,
    MarketScript,
)
from tests.harness.mock_gateway import MockGateway, MockGatewayConfig
from tests.harness.market.tick_stream import (
    BrownianGenerator, TrendingGenerator, MeanRevertGenerator,
    GapGenerator, FlashCrashGenerator, HaltGenerator, QuoteOnlyGenerator,
    make_generator, MarketSimulator,
)


# ════════════════════════════════════════════════════════════════════════════
# GENERATOR DETERMINISM
# ════════════════════════════════════════════════════════════════════════════

class TestGeneratorDeterminism:
    """Same generator + same seed → identical tick sequence."""

    def test_brownian_deterministic(self):
        rng_a = DeterministicRNG(seed=100)
        rng_b = DeterministicRNG(seed=100)
        gen_a = BrownianGenerator(initial_mid=Decimal("100"), volatility_per_second=Decimal("0.01"), spread=Decimal("0.02"))
        gen_b = BrownianGenerator(initial_mid=Decimal("100"), volatility_per_second=Decimal("0.01"), spread=Decimal("0.02"))
        seq_a = [gen_a.next_tick(i * 0.2, rng_a, Decimal("100"), Decimal("100.02")) for i in range(20)]
        seq_b = [gen_b.next_tick(i * 0.2, rng_b, Decimal("100"), Decimal("100.02")) for i in range(20)]
        assert seq_a == seq_b

    def test_trending_drift_direction(self):
        """Trending generator should consistently move mid in drift direction."""
        rng = DeterministicRNG(seed=1)
        gen = TrendingGenerator(
            initial_mid=Decimal("100"),
            drift_per_second=Decimal("0.05"),       # +5¢/sec
            noise_per_second=Decimal("0.005"),      # small noise
            spread=Decimal("0.02"),
        )
        prior_bid, prior_ask = Decimal("99.99"), Decimal("100.01")
        for i in range(20):
            bid, ask, _ = gen.next_tick(i * 0.5, rng, prior_bid, prior_ask)
            prior_bid, prior_ask = bid, ask
        # After 20 steps with +0.05 drift per call, mid should be well above 100.
        final_mid = (prior_bid + prior_ask) / 2
        assert final_mid > Decimal("100.5"), f"Trending generator failed to trend; final mid={final_mid}"


# ════════════════════════════════════════════════════════════════════════════
# GAP & FLASH-CRASH (event-triggered regimes)
# ════════════════════════════════════════════════════════════════════════════

class TestEventRegimes:

    def test_gap_only_applies_once(self):
        rng = DeterministicRNG(seed=1)
        gen = GapGenerator(
            initial_mid=Decimal("100"),
            volatility_per_second=Decimal("0.001"),
            spread=Decimal("0.02"),
            gap_at_seconds=5.0,
            gap_magnitude=Decimal("-2.0"),  # 2-point gap down
        )
        prior_bid, prior_ask = Decimal("99.99"), Decimal("100.01")
        # Before the gap
        for i in range(10):  # t=0,0.5,1.0,...,4.5 — all before gap_at=5.0
            bid, ask, _ = gen.next_tick(i * 0.5, rng, prior_bid, prior_ask)
            prior_bid, prior_ask = bid, ask
        pre_gap_mid = (prior_bid + prior_ask) / 2
        assert abs(pre_gap_mid - Decimal("100")) < Decimal("0.1"), "Pre-gap mid should hover near 100"

        # At the gap
        bid, ask, _ = gen.next_tick(5.0, rng, prior_bid, prior_ask)
        post_gap_mid = (bid + ask) / 2
        assert post_gap_mid < Decimal("99"), f"Gap-down should drop mid sharply; got {post_gap_mid}"

        # After the gap — no additional gap should re-apply
        for i in range(5):
            prior_bid, prior_ask = bid, ask
            bid, ask, _ = gen.next_tick(5.5 + i * 0.5, rng, prior_bid, prior_ask)
        post_post_mid = (bid + ask) / 2
        # Should NOT have dropped another 2 points
        assert post_post_mid > post_gap_mid - Decimal("0.5")

    def test_flash_crash_drops_then_recovers_intensity(self):
        """The crash adjustment should diminish linearly during recovery
        window — verified by sampling the adjustment value at multiple
        times within the recovery window."""
        rng = DeterministicRNG(seed=1)
        gen = FlashCrashGenerator(
            initial_mid=Decimal("100"),
            volatility_per_second=Decimal("0"),       # NO noise for this check
            spread=Decimal("0.02"),
            crash_at_seconds=2.0,
            crash_magnitude=Decimal("5.0"),
            recovery_seconds=4.0,
        )
        # Same prior_mid every call so we isolate the crash adjustment.
        # At t=2.0: phase=0, adj=-5.0
        # At t=4.0: phase=0.5, adj=-2.5
        # At t=6.0: phase=1.0, adj=0  (boundary — treat as past)
        # At t=7.0: past recovery, adj=0
        prior = Decimal("100.00")
        bid_at_2, ask_at_2, _ = gen.next_tick(2.0, rng, prior, prior)
        mid_at_2 = (bid_at_2 + ask_at_2) / 2

        # Reset (fresh generator) because next_tick may have stateful effects
        gen2 = FlashCrashGenerator(
            initial_mid=Decimal("100"), volatility_per_second=Decimal("0"),
            spread=Decimal("0.02"), crash_at_seconds=2.0,
            crash_magnitude=Decimal("5.0"), recovery_seconds=4.0,
        )
        bid_at_4, ask_at_4, _ = gen2.next_tick(4.0, rng, prior, prior)
        mid_at_4 = (bid_at_4 + ask_at_4) / 2

        gen3 = FlashCrashGenerator(
            initial_mid=Decimal("100"), volatility_per_second=Decimal("0"),
            spread=Decimal("0.02"), crash_at_seconds=2.0,
            crash_magnitude=Decimal("5.0"), recovery_seconds=4.0,
        )
        bid_at_7, ask_at_7, _ = gen3.next_tick(7.0, rng, prior, prior)
        mid_at_7 = (bid_at_7 + ask_at_7) / 2

        # mid_at_2 should be ~95 (100 - 5)
        # mid_at_4 should be ~97.5 (100 - 2.5)
        # mid_at_7 should be ~100 (no crash adj)
        assert abs(mid_at_2 - Decimal("95")) < Decimal("0.01")
        assert abs(mid_at_4 - Decimal("97.5")) < Decimal("0.01")
        assert abs(mid_at_7 - Decimal("100")) < Decimal("0.01")


# ════════════════════════════════════════════════════════════════════════════
# QUOTE-ONLY REGIME (CFD/FX behavior)
# ════════════════════════════════════════════════════════════════════════════

class TestQuoteOnly:

    def test_quote_only_emits_no_last(self):
        """The 2026-06-05 bug class: CFD/FX feeds have no trade prints."""
        rng = DeterministicRNG(seed=1)
        gen = QuoteOnlyGenerator(
            initial_mid=Decimal("100"),
            volatility_per_second=Decimal("0.01"),
            spread=Decimal("0.02"),
        )
        for i in range(10):
            _, _, last = gen.next_tick(i * 0.5, rng, None, None)
            assert last is None, f"QuoteOnly emitted a last={last} — bug!"


# ════════════════════════════════════════════════════════════════════════════
# FACTORY (script → generator)
# ════════════════════════════════════════════════════════════════════════════

class TestFactory:

    def test_factory_routes_all_regimes(self):
        cases = [
            ("brownian", BrownianGenerator),
            ("trending", TrendingGenerator),
            ("mean_revert", MeanRevertGenerator),
            ("ranging", MeanRevertGenerator),
            ("quote_only", QuoteOnlyGenerator),
        ]
        for regime, expected_class in cases:
            script = MarketScript(
                initial_bid=Decimal("99.99"),
                initial_ask=Decimal("100.01"),
                regime=regime,
                duration_seconds=10.0,
            )
            gen = make_generator(script)
            assert isinstance(gen, expected_class), f"{regime} → wrong generator"

    def test_factory_rejects_unknown_regime(self):
        script = MarketScript(
            initial_bid=Decimal("99.99"),
            initial_ask=Decimal("100.01"),
            regime="hypergonic_supersymmetric_oscillator",
            duration_seconds=10.0,
        )
        with pytest.raises(ValueError, match="Unknown market regime"):
            make_generator(script)

    def test_factory_requires_gap_params_for_gap_regime(self):
        script = MarketScript(
            initial_bid=Decimal("99.99"),
            initial_ask=Decimal("100.01"),
            regime="gap",
            duration_seconds=10.0,
            # missing gap_at_seconds + gap_magnitude
        )
        with pytest.raises(ValueError, match="gap_at_seconds"):
            make_generator(script)


# ════════════════════════════════════════════════════════════════════════════
# MARKET SIMULATOR (end-to-end with MockGateway)
# ════════════════════════════════════════════════════════════════════════════

class TestMarketSimulator:

    @pytest.fixture
    def setup(self):
        clock = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        rng = DeterministicRNG(seed=42)
        cfg = MockGatewayConfig(asset_class="US_EQUITY", min_tick=0.01)
        gw = MockGateway("AAPL", clock, rng, cfg)
        return clock, rng, gw

    @pytest.mark.asyncio
    async def test_simulator_feeds_gateway(self, setup):
        clock, rng, gw = setup
        await gw.connect()

        script = MarketScript(
            initial_bid=Decimal("99.99"),
            initial_ask=Decimal("100.01"),
            regime="brownian",
            duration_seconds=5.0,
            tick_rate_hz=10.0,         # 50 ticks total
            volatility_per_second=Decimal("0.005"),
        )
        sim = MarketSimulator(gw, clock, rng, script)
        await sim.run()

        # Heartbeat should have updated (ticks were fed)
        assert gw._last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_simulator_triggers_resting_order(self, setup):
        """End-to-end: place a BUY STP, run trending market, verify fill."""
        clock, rng, gw = setup
        await gw.connect()

        fills = []
        gw._on_fill = lambda *args: fills.append(args)

        # Place BUY STP-MARKET above current market.
        from tests.harness.test_mock_gateway import _FakeSide
        await gw.place_stop_market(_FakeSide("BUY"), qty=100, stop_price=100.50, order_id="BUY1")

        # Up-trending market — should trigger BUY when ask >= 100.50.
        script = MarketScript(
            initial_bid=Decimal("99.99"),
            initial_ask=Decimal("100.01"),
            regime="trending",
            duration_seconds=20.0,
            tick_rate_hz=5.0,
            drift_per_second=Decimal("0.05"),
            volatility_per_second=Decimal("0.005"),
        )
        sim = MarketSimulator(gw, clock, rng, script)
        await sim.run()

        assert len(fills) >= 1, "Trending market should have triggered the BUY"
        engine_id = fills[0][0]
        fill_price = fills[0][2]
        assert engine_id == "BUY1"
        assert fill_price >= 100.50, f"Fill should be at-or-above trigger; got {fill_price}"

    @pytest.mark.asyncio
    async def test_simulator_snaps_prices_to_grid(self, setup):
        """Every emitted bid/ask must be a multiple of the venue's minTick."""
        clock, rng, gw = setup
        await gw.connect()
        # Use a coarser grid to make the test more sensitive.
        gw._runtime_min_tick = 0.05

        captured = []
        gw._tick_observer = lambda bid, ask, last: captured.append((bid, ask, last))

        script = MarketScript(
            initial_bid=Decimal("99.90"),
            initial_ask=Decimal("100.00"),
            regime="brownian",
            duration_seconds=3.0,
            tick_rate_hz=10.0,
            volatility_per_second=Decimal("0.02"),
        )
        sim = MarketSimulator(gw, clock, rng, script)
        await sim.run()

        # Every captured price must be on the 0.05 grid (float division
        # may have tiny error; tolerance of 1e-9 is enough).
        assert len(captured) > 5, "Should have captured many ticks"
        for bid, ask, _last in captured:
            for px in (bid, ask):
                n = round(px / 0.05)
                assert abs(n * 0.05 - px) < 1e-9, f"Off-grid price emitted: {px}"

    @pytest.mark.asyncio
    async def test_halt_regime_produces_silence(self, setup):
        """During the halt window, no ticks should be fed to gateway."""
        clock, rng, gw = setup
        await gw.connect()
        ticks_seen: list[float] = []  # records monotonic times of each tick
        gw._tick_observer = lambda bid, ask, last: ticks_seen.append(clock.monotonic())

        # Set start point so monotonic 0 == script start.
        script_start = clock.monotonic()
        script = MarketScript(
            initial_bid=Decimal("99.99"),
            initial_ask=Decimal("100.01"),
            regime="halt",
            duration_seconds=10.0,
            tick_rate_hz=10.0,
            volatility_per_second=Decimal("0.001"),
            halt_at_seconds=3.0,
            halt_duration_seconds=4.0,   # silent from t=3 to t=7
        )
        sim = MarketSimulator(gw, clock, rng, script)
        await sim.run()

        assert len(ticks_seen) > 5, "Simulator should have produced ticks"

        # Compute tick offsets from script start.
        offsets = [t - script_start for t in ticks_seen]
        # No tick should land STRICTLY INSIDE the halt window (3 < t < 7).
        # Allow small boundary tolerance for the tick exactly at t=3.0 (just before halt).
        halt_violations = [t for t in offsets if 3.05 < t < 6.95]
        assert not halt_violations, (
            f"Halt window violated: ticks landed during silence period: {halt_violations}"
        )
