"""MockGateway test suite — verify deterministic, complete, IBKR-faithful.

These tests prove the MockGateway is fit-for-purpose as the synthetic
backend. The bar is HIGH because every bug here means false-positive
or false-negative findings in the full harness.

Coverage:
  1. INTERFACE — every method and attribute the engine reads exists with the right shape
  2. DETERMINISM — same seed + same scenario → identical outputs
  3. IBKR QUIRKS — commission delay, positions lag, tick grid, bracket OCA semantics
  4. CALLBACK FIRING — _on_fill / _on_commission / _on_order_status fire correctly
  5. ORDER LIFECYCLE — submit → trigger → fill → cleanup
  6. MODIFY-IN-PLACE — bracket child stop modify preserves broker_id
  7. OPERATOR COMMANDS — force_disconnect, inject_rejection, seed_preexisting_*
  8. FX SYMBOL TRANSLATION — broker symbol "EUR" → logical "EURUSD"
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import pytest

from tests.harness import SimulatedClock, DeterministicRNG
from tests.harness.mock_gateway import (
    MockGateway, MockGatewayConfig,
    _Position, _ResingOrder,
)


# ════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def clock():
    return SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))


@pytest.fixture
def rng():
    return DeterministicRNG(seed=42)


@pytest.fixture
def equity_config():
    return MockGatewayConfig(
        asset_class="US_EQUITY",
        currency="USD",
        secType="STK",
        min_tick=0.01,
    )


@pytest.fixture
def fx_config():
    return MockGatewayConfig(
        asset_class="FX_CASH",
        currency="USD",
        secType="CASH",
        min_tick=0.00005,
    )


# ════════════════════════════════════════════════════════════════════════════
# INTERFACE — every method the engine calls exists
# ════════════════════════════════════════════════════════════════════════════

class TestInterfaceContract:
    """The 16 methods + callbacks audited from src/strategy/engine.py."""

    def test_has_all_required_methods(self, clock, rng, equity_config):
        gw = MockGateway("AAPL", clock, rng, equity_config)
        required = [
            'connect', 'disconnect',
            'place_stop_limit', 'place_stop_market', 'place_order',
            'place_bracket_buy_stop_market',
            'modify_stop_trigger', 'modify_order',
            'cancel_order', 'cancel_all',
            'get_positions', 'fetch_open_orders',
            'fetch_all_open_orders_for_symbol',
            'cancel_open_orders_for_symbol',
            'get_all_fills', 'get_price', 'get_equity',
            'register_existing_order',
            '_get_contract', 'get_runtime_min_tick',
        ]
        for m in required:
            assert hasattr(gw, m), f"MockGateway missing method: {m}"

    def test_has_all_required_attributes(self, clock, rng, equity_config):
        gw = MockGateway("AAPL", clock, rng, equity_config)
        assert hasattr(gw, 'paper')
        assert hasattr(gw, 'connected')
        assert hasattr(gw, 'symbol')
        assert hasattr(gw, '_last_heartbeat')
        assert hasattr(gw, '_contract')
        assert hasattr(gw, '_runtime_min_tick')

    def test_callback_attributes_writable_by_engine(self, clock, rng, equity_config):
        gw = MockGateway("AAPL", clock, rng, equity_config)

        async def on_fill(*args): pass
        async def on_status(*args): pass
        async def on_commission(*args): pass

        gw._on_fill = on_fill
        gw._on_order_status = on_status
        gw._on_commission = on_commission

        assert gw._on_fill is on_fill
        assert gw._on_order_status is on_status
        assert gw._on_commission is on_commission


# ════════════════════════════════════════════════════════════════════════════
# DETERMINISM
# ════════════════════════════════════════════════════════════════════════════

class TestDeterminism:
    """Same seed + same scenario → identical outputs. The whole harness
    rests on this."""

    @pytest.mark.asyncio
    async def test_same_seed_produces_same_fill_sequence(self, equity_config):
        clock_a = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        clock_b = SimulatedClock(start=datetime(2026, 6, 6, 14, 30, tzinfo=timezone.utc))
        rng_a = DeterministicRNG(seed=42)
        rng_b = DeterministicRNG(seed=42)
        gw_a = MockGateway("AAPL", clock_a, rng_a, equity_config)
        gw_b = MockGateway("AAPL", clock_b, rng_b, equity_config)

        await gw_a.connect()
        await gw_b.connect()

        await gw_a.place_stop_market(_FakeSide("BUY"), 100, 230.50, "BUY_TEST")
        await gw_b.place_stop_market(_FakeSide("BUY"), 100, 230.50, "BUY_TEST")

        # Same tick sequence
        for ask in (230.30, 230.40, 230.55, 230.60):
            await gw_a.feed_tick(bid=ask-0.05, ask=ask)
            await gw_b.feed_tick(bid=ask-0.05, ask=ask)

        # Fill histories must match exactly
        assert len(gw_a._fill_history) == len(gw_b._fill_history)
        for fa, fb in zip(gw_a._fill_history, gw_b._fill_history):
            assert fa.execution.price == fb.execution.price
            assert fa.execution.shares == fb.execution.shares
            assert fa.execution.side == fb.execution.side


# ════════════════════════════════════════════════════════════════════════════
# IBKR QUIRK SIMULATION
# ════════════════════════════════════════════════════════════════════════════

class TestIBKRQuirks:

    @pytest.mark.asyncio
    async def test_commission_arrives_after_fill_with_delay(self, clock, rng, equity_config):
        """The 2026-06-05 bug class: code that reads fill.commissionReport
        at fillEvent time gets None. We must reproduce that."""
        equity_config.commission_report_delay_seconds = 0.5
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()

        fills_seen = []
        commissions_seen = []
        gw._on_fill = _make_async(lambda *args: fills_seen.append(args))
        gw._on_commission = _make_async(lambda *args: commissions_seen.append(args))

        await gw.place_stop_market(_FakeSide("BUY"), 100, 230.50, "BUY1")
        await gw.feed_tick(bid=230.45, ask=230.55)  # triggers BUY

        # At this exact moment, fill has fired but commission has NOT.
        assert len(fills_seen) == 1
        assert len(commissions_seen) == 0
        assert fills_seen[0][5] is None  # commission arg should be None

        # Advance clock past the delay; commission should fire.
        await clock.sleep(0.6)
        await gw.tick_clock()
        assert len(commissions_seen) == 1
        engine_id, commission, exec_id = commissions_seen[0]
        assert engine_id == "BUY1"
        assert commission == equity_config.commission_per_order

    @pytest.mark.asyncio
    async def test_positions_lag_after_fill(self, clock, rng, equity_config):
        """The 2026-06-05 fold-bug class: positions() lags fills, code that
        immediately reconciles sees a false 0 and folds the engine."""
        equity_config.positions_lag_seconds = 3.0
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()

        await gw.place_stop_market(_FakeSide("BUY"), 100, 230.50, "BUY1")
        await gw.feed_tick(bid=230.45, ask=230.55)  # triggers

        # Immediately after fill — positions still show 0 (the bug surface).
        positions = await gw.get_positions()
        assert positions == []

        # Advance past lag.
        await clock.sleep(3.1)
        await gw.tick_clock()
        positions = await gw.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == 100

    @pytest.mark.asyncio
    async def test_tick_grid_rejection(self, clock, rng, equity_config):
        """Off-grid prices get rejected with the right status callback."""
        equity_config.enforce_tick_grid = True
        equity_config.min_tick = 0.01
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()

        statuses = []
        gw._on_order_status = _make_async(lambda *args: statuses.append(args))

        # 230.456 is NOT on the 0.01 grid
        await gw.place_stop_market(_FakeSide("BUY"), 100, 230.456, "BADBUY")

        assert len(statuses) == 1
        engine_id, status, msg = statuses[0]
        assert engine_id == "BADBUY"
        assert status == "Rejected"
        assert "grid" in msg.lower()

    @pytest.mark.asyncio
    async def test_bracket_child_waits_for_parent_fill(self, clock, rng, equity_config):
        """Bracket child rests in PreSubmitted until parent fills, then
        auto-activates. Hitting the child's stop BEFORE parent fills
        should NOT fire the child."""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()

        fills = []
        gw._on_fill = _make_async(lambda *args: fills.append(args))

        result = await gw.place_bracket_buy_stop_market(
            qty=100,
            parent_stop_price=230.50,
            parent_limit_price=230.55,
            child_stop_price=228.50,
            parent_order_id="PARENT",
            child_order_id="CHILD",
        )
        assert result is not None
        parent_id, child_id = result

        # A tick that would trigger the child stop (price falls to 228)
        # BUT the parent hasn't fired yet. Child should NOT fill.
        await gw.feed_tick(bid=228.00, ask=228.20)
        assert len(fills) == 0

        # Now move price up to trigger the parent.
        await gw.feed_tick(bid=230.45, ask=230.55)
        assert len(fills) == 1
        assert fills[0][0] == "PARENT"

        # Child is now active. Move price down — should fire the SELL.
        await gw.feed_tick(bid=228.00, ask=228.20)
        assert len(fills) == 2
        assert fills[1][0] == "CHILD"

    @pytest.mark.asyncio
    async def test_modify_in_place_preserves_broker_id(self, clock, rng, equity_config):
        """Modify-not-replace is the safety invariant the engine depends on
        for bracket child retargeting. Same broker_id before and after."""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()

        await gw.place_stop_market(_FakeSide("SELL"), 100, 228.50, "SELL1")
        broker_id_before = gw._engine_to_broker_id["SELL1"]
        order_before = gw._orders[broker_id_before]
        assert order_before.stop_price == 228.50

        ok = await gw.modify_stop_trigger("SELL1", new_stop_price=229.00)
        assert ok

        broker_id_after = gw._engine_to_broker_id["SELL1"]
        assert broker_id_after == broker_id_before, "broker_id MUST be preserved across modify"
        order_after = gw._orders[broker_id_after]
        assert order_after is order_before, "Same object — modify is in-place"
        assert order_after.stop_price == 229.00

    @pytest.mark.asyncio
    async def test_modify_rejection_injection(self, clock, rng, equity_config):
        """When modify_stop_trigger fails, engine must keep the original
        SL — verified by inject_modify_rejection knob."""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        gw.config.inject_modify_rejection = True
        await gw.connect()

        await gw.place_stop_market(_FakeSide("SELL"), 100, 228.50, "SELL1")
        ok = await gw.modify_stop_trigger("SELL1", new_stop_price=229.00)
        assert not ok
        # Original stop preserved
        assert gw._orders[gw._engine_to_broker_id["SELL1"]].stop_price == 228.50


# ════════════════════════════════════════════════════════════════════════════
# CALLBACK FIRING — engine wires these; we must fire them correctly
# ════════════════════════════════════════════════════════════════════════════

class TestCallbacks:

    @pytest.mark.asyncio
    async def test_on_fill_receives_correct_signature(self, clock, rng, equity_config):
        """Engine's _on_fill signature: (engine_id, qty, price, exec_id, time, commission)"""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()
        captured = []
        gw._on_fill = _make_async(lambda *args: captured.append(args))

        await gw.place_stop_market(_FakeSide("BUY"), 100, 230.50, "BUY1")
        await gw.feed_tick(bid=230.45, ask=230.55)

        assert len(captured) == 1
        engine_id, qty, price, exec_id, time, commission = captured[0]
        assert engine_id == "BUY1"
        assert qty == 100.0
        assert price > 230.50      # filled at or above trigger
        assert exec_id.startswith("E")
        assert isinstance(time, datetime)
        assert commission is None  # IBKR's commissionReport not yet arrived

    @pytest.mark.asyncio
    async def test_cancel_fires_status_callback(self, clock, rng, equity_config):
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()
        statuses = []
        gw._on_order_status = _make_async(lambda *args: statuses.append(args))

        await gw.place_stop_market(_FakeSide("SELL"), 100, 228.50, "SELL1")
        await gw.cancel_order("SELL1")

        assert len(statuses) == 1
        engine_id, status, message = statuses[0]
        assert engine_id == "SELL1"
        assert status == "Cancelled"


# ════════════════════════════════════════════════════════════════════════════
# OPERATOR COMMANDS — scenarios drive these
# ════════════════════════════════════════════════════════════════════════════

class TestOperatorCommands:

    @pytest.mark.asyncio
    async def test_force_disconnect_reconnect(self, clock, rng, equity_config):
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()
        assert gw.connected
        await gw.force_disconnect()
        assert not gw.connected
        await gw.force_reconnect()
        assert gw.connected

    @pytest.mark.asyncio
    async def test_seed_preexisting_position_for_orphan_recovery(self, clock, rng, equity_config):
        """Used by orphan-position-on-startup scenarios."""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        gw.seed_preexisting_position(qty=200, avg_cost=230.00)
        await gw.connect()  # connect makes it visible

        positions = await gw.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "AAPL"
        assert positions[0].quantity == 200

    @pytest.mark.asyncio
    async def test_seed_preexisting_order_for_orphan_recovery(self, clock, rng, equity_config):
        """Used by orphan-SELL-on-startup scenarios."""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        gw.seed_preexisting_order(
            action="SELL", qty=100, stop_price=228.50, engine_id="ORPHAN_SELL_1",
        )
        await gw.connect()

        orders = gw.fetch_open_orders()
        assert len(orders) == 1
        assert orders[0]['action'] == 'SELL'
        assert orders[0]['stop_price'] == 228.50
        assert orders[0]['order_ref'] == 'ORPHAN_SELL_1'

    @pytest.mark.asyncio
    async def test_inject_rejection_one_shot(self, clock, rng, equity_config):
        """Injection is one-shot: rejects the NEXT order, then resets."""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()
        statuses = []
        gw._on_order_status = _make_async(lambda *args: statuses.append(args))

        await gw.inject_next_rejection("Simulated margin failure")
        await gw.place_stop_market(_FakeSide("BUY"), 100, 230.50, "BUY1")
        assert len(statuses) == 1
        assert statuses[0][1] == 'Rejected'

        # Next placement should succeed.
        await gw.place_stop_market(_FakeSide("BUY"), 100, 230.50, "BUY2")
        assert len(statuses) == 1  # no new rejection

    @pytest.mark.asyncio
    async def test_force_cancel_does_not_fire_engine_callback(self, clock, rng, equity_config):
        """Simulates manual TWS cancel — engine shouldn't see it directly.
        The next reconcile is what surfaces the divergence."""
        gw = MockGateway("AAPL", clock, rng, equity_config)
        await gw.connect()
        statuses = []
        gw._on_order_status = _make_async(lambda *args: statuses.append(args))

        await gw.place_stop_market(_FakeSide("SELL"), 100, 228.50, "SELL1")
        await gw.force_cancel_by_engine_id("SELL1")

        # No callback should have fired — engine still thinks order is resting
        assert len(statuses) == 0
        # But broker-side it's gone
        assert "SELL1" not in gw._engine_to_broker_id


# ════════════════════════════════════════════════════════════════════════════
# FX SYMBOL TRANSLATION
# ════════════════════════════════════════════════════════════════════════════

class TestFXSymbolTranslation:
    """ib_async stores Forex("EURUSD") as contract.symbol='EUR', currency='USD'.
    Our MockGateway must do the same so symbology-translation tests are realistic."""

    @pytest.mark.asyncio
    async def test_fx_contract_symbol_is_base_ccy(self, clock, rng, fx_config):
        gw = MockGateway("EURUSD", clock, rng, fx_config)
        await gw.connect()
        assert gw._contract.symbol == "EUR"
        assert gw._contract.currency == "USD"
        assert gw._contract.secType == "CASH"

    @pytest.mark.asyncio
    async def test_fx_position_returns_logical_ticker(self, clock, rng, fx_config):
        """get_positions() should translate broker_symbol back to logical."""
        gw = MockGateway("EURUSD", clock, rng, fx_config)
        gw.seed_preexisting_position(qty=25000, avg_cost=1.16415)
        await gw.connect()

        positions = await gw.get_positions()
        # Logical ticker = "EURUSD", NOT "EUR"
        assert positions[0].symbol == "EURUSD"


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

class _FakeSide:
    """Mimics OrderSide.BUY / OrderSide.SELL with the .value accessor."""
    def __init__(self, v: str): self.value = v


def _make_async(fn):
    """Wrap a sync callable so it works as the engine's expected callback shape.
    Engine sets callbacks expecting sync OR async — we test the sync variant."""
    return fn
