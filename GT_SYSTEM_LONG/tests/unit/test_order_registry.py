"""
Unit tests for OrderRegistry and OrderRecord.
"""
from src.config.models import (
    OrderRecord, OrderRegistry, OrderSide, OrderType,
    OrderStatus, COMMISSION_PER_SHARE, MIN_COMMISSION,
)
from datetime import datetime


class TestOrderRecord:
    """Test OrderRecord dataclass."""

    def test_order_record_creation(self):
        """Order record created with correct defaults."""
        order = OrderRecord(
            order_id="1",
            symbol="INFY",
            side=OrderSide.BUY,
            qty=100,
            order_type=OrderType.MARKET,
        )
        assert order.order_id == "1"
        assert order.symbol == "INFY"
        assert order.side == OrderSide.BUY
        assert order.qty == 100
        assert order.status == OrderStatus.PENDING
        assert order.filled_qty == 0
        assert order.avg_fill_price is None
        assert order.commission == 0.0

    # These tests previously asserted the IBKR Fixed-rate formula
    # (qty × $0.005, $1 min) but the engine actually uses Tiered
    # (qty × $0.0035, $0.35 min, 1% cap) plus reg/clearing fees.
    # Tests now match the production formula. `avg_fill_price` MUST be
    # set — `calculate_commission` returns 0 without it because reg
    # fees + the 1% cap can't be computed without a trade price.
    # `pytest.approx` because the reg-fee math has small float artifacts.
    def test_commission_calculation_base_above_min(self):
        """200 sh × $0.0035 = $0.70 base; > $0.35 minimum, so base wins."""
        import pytest
        order = OrderRecord(
            order_id="1", symbol="INFY", side=OrderSide.BUY, qty=200,
            order_type=OrderType.MARKET,
        )
        order.filled_qty = 200
        order.avg_fill_price = 100.0
        # base = max(0.35, min(200×0.0035=0.70, 200×100×0.01=200)) = $0.70
        # reg  = 200 × (0.000003 + 0.00020) = $0.0406  (BUY: no SEC/TAF)
        assert order.calculate_commission() == pytest.approx(0.7406, rel=1e-3)

    def test_commission_calculation_minimum_floor(self):
        """50 sh × $0.0035 = $0.175 base → $0.35 minimum kicks in."""
        import pytest
        order = OrderRecord(
            order_id="1", symbol="INFY", side=OrderSide.BUY, qty=50,
            order_type=OrderType.MARKET,
        )
        order.filled_qty = 50
        order.avg_fill_price = 100.0
        # base = max(0.35, min(0.175, 50)) = $0.35
        # reg  = 50 × 0.000203 = $0.01015
        assert order.calculate_commission() == pytest.approx(0.3601, rel=1e-3)

    def test_commission_calculation_large_order(self):
        """1000 sh × $0.0035 = $3.50 base."""
        import pytest
        order = OrderRecord(
            order_id="1", symbol="INFY", side=OrderSide.BUY, qty=1000,
            order_type=OrderType.MARKET,
        )
        order.filled_qty = 1000
        order.avg_fill_price = 100.0
        # base = $3.50, reg = 1000 × 0.000203 = $0.203
        assert order.calculate_commission() == pytest.approx(3.703, rel=1e-3)

    def test_commission_sell_includes_sec_and_taf(self):
        """SELL side adds SEC + FINRA TAF; BUY does not."""
        import pytest
        buy = OrderRecord(order_id="B", symbol="INFY", side=OrderSide.BUY,
                          qty=1000, order_type=OrderType.MARKET)
        buy.filled_qty = 1000; buy.avg_fill_price = 100.0
        sell = OrderRecord(order_id="S", symbol="INFY", side=OrderSide.SELL,
                           qty=1000, order_type=OrderType.MARKET)
        sell.filled_qty = 1000; sell.avg_fill_price = 100.0
        # SELL extras: SEC = 100000 × 0.0000206 = $2.06; TAF = 1000 × 0.000195 = $0.195
        diff = sell.calculate_commission() - buy.calculate_commission()
        assert diff == pytest.approx(2.255, rel=1e-3)

    def test_commission_cap_wins_over_floor_on_penny_stock(self):
        """Edge case: when 1% of trade value < $0.35 minimum, the cap
        wins. IBKR's "Maximum per order: 1% of Trade Value" is a hard
        ceiling — it can't be exceeded even by the per-order floor.
        Previous code did floor-then-cap inverted (`max(MIN, min(raw, cap))`)
        which billed $0.35 here; this test pins the correct behavior.
        """
        import pytest
        order = OrderRecord(
            order_id="P", symbol="PENNY", side=OrderSide.BUY, qty=5,
            order_type=OrderType.MARKET,
        )
        order.filled_qty = 5
        order.avg_fill_price = 0.50  # $2.50 trade, 1% cap = $0.025
        # base = min(max(0.35, 5×0.0035=0.0175), 0.025) = min(0.35, 0.025) = $0.025
        # reg  = 5 × 0.000203 = $0.001015 → $0.026015
        assert order.calculate_commission() == pytest.approx(0.026, abs=0.001)

    def test_commission_zero_without_avg_fill_price(self):
        """Returns 0.0 when avg_fill_price isn't set yet — caller's
        responsibility to wait for fill before reading commission."""
        order = OrderRecord(
            order_id="1", symbol="INFY", side=OrderSide.BUY, qty=200,
            order_type=OrderType.MARKET,
        )
        order.filled_qty = 200  # filled but no price → return 0
        assert order.calculate_commission() == 0.0

    def test_broker_commission_overrides_model(self):
        """When IBKR's commissionReport arrives via broker_commission,
        calculate_commission() returns the broker number, not the model.
        Locks in the precedence rule in OrderRecord.calculate_commission."""
        import pytest
        order = OrderRecord(
            order_id="L", symbol="NVDA", side=OrderSide.BUY, qty=50,
            order_type=OrderType.MARKET,
        )
        order.filled_qty = 50
        order.avg_fill_price = 200.0
        # Without broker_commission → modeled formula (~$0.36)
        assert order.calculate_commission() == pytest.approx(0.3601, rel=1e-3)
        # IBKR reports the exact number → broker wins.
        order.broker_commission = 0.42
        assert order.calculate_commission() == 0.42

    def test_broker_commission_accumulates_across_partial_fills(self):
        """Partial fills each carry their own commissionReport; the
        registry sums them into order.broker_commission so the running
        total tracks IBKR's exact charge as the order works through."""
        registry = OrderRegistry()
        order = OrderRecord(
            order_id="P", symbol="NVDA", side=OrderSide.BUY, qty=100,
            order_type=OrderType.MARKET,
        )
        registry.submit(order)
        registry.on_fill("P", qty=40, price=200.0, exec_id="P1",
                         broker_commission=0.35)
        registry.on_fill("P", qty=60, price=200.5, exec_id="P2",
                         broker_commission=0.41)
        o = registry.get("P")
        assert o.broker_commission == 0.76
        assert o.calculate_commission() == 0.76

    def test_paper_mode_falls_back_to_modeled_commission(self):
        """No broker_commission arg → order.broker_commission stays None
        → calculate_commission() uses the formula. Paper trading + replay
        + the brief pre-commissionReport window all hit this path."""
        import pytest
        registry = OrderRegistry()
        order = OrderRecord(
            order_id="PA", symbol="NVDA", side=OrderSide.BUY, qty=50,
            order_type=OrderType.MARKET,
        )
        registry.submit(order)
        registry.on_fill("PA", qty=50, price=200.0)   # no broker_commission
        o = registry.get("PA")
        assert o.broker_commission is None
        assert o.calculate_commission() == pytest.approx(0.3601, rel=1e-3)

    def test_is_complete(self):
        """is_complete() returns True when fully filled."""
        order = OrderRecord(
            order_id="1",
            symbol="INFY",
            side=OrderSide.BUY,
            qty=100,
            order_type=OrderType.MARKET,
        )
        assert not order.is_complete()
        order.filled_qty = 100
        order.status = OrderStatus.FILLED
        assert order.is_complete()


class TestOrderRegistry:
    """Test OrderRegistry."""

    def test_submit_order(self):
        """Orders can be submitted and retrieved."""
        registry = OrderRegistry()
        order = OrderRecord(
            order_id="1",
            symbol="INFY",
            side=OrderSide.BUY,
            qty=100,
            order_type=OrderType.MARKET,
        )
        registry.submit(order)
        assert registry.get("1") == order

    def test_get_missing_order(self):
        """Get returns None for missing order."""
        registry = OrderRegistry()
        assert registry.get("999") is None

    def test_partial_fill(self):
        """Partial fills update quantity and avg price."""
        registry = OrderRegistry()
        order = OrderRecord(
            order_id="1",
            symbol="INFY",
            side=OrderSide.BUY,
            qty=100,
            order_type=OrderType.MARKET,
        )
        registry.submit(order)

        # First fill: 50 @ 1800
        registry.on_fill("1", 50, 1800.0)
        assert order.filled_qty == 50
        assert order.avg_fill_price == 1800.0

        # Second fill: 50 @ 1810
        registry.on_fill("1", 50, 1810.0)
        assert order.filled_qty == 100
        assert order.avg_fill_price == 1805.0  # (50*1800 + 50*1810) / 100

    def test_full_fill(self):
        """Full fill marks order as FILLED and calculates commission."""
        registry = OrderRegistry()
        order = OrderRecord(
            order_id="1",
            symbol="INFY",
            side=OrderSide.BUY,
            qty=100,
            order_type=OrderType.MARKET,
        )
        registry.submit(order)

        registry.on_fill("1", 100, 1800.0)

        assert order.status == OrderStatus.FILLED
        assert order.filled_at is not None
        assert order.commission > 0  # $1.00 minimum

    def test_on_cancel(self):
        """Cancel marks order as CANCELLED."""
        registry = OrderRegistry()
        order = OrderRecord(
            order_id="1",
            symbol="INFY",
            side=OrderSide.BUY,
            qty=100,
            order_type=OrderType.MARKET,
        )
        registry.submit(order)

        registry.on_cancel("1")

        assert order.status == OrderStatus.CANCELLED

    def test_get_filled_orders(self):
        """Get filled orders filters correctly."""
        registry = OrderRegistry()

        # Add some orders
        order1 = OrderRecord(order_id="1", symbol="INFY", side=OrderSide.BUY, qty=100, order_type=OrderType.MARKET)
        order2 = OrderRecord(order_id="2", symbol="INFY", side=OrderSide.SELL, qty=100, order_type=OrderType.MARKET)
        order3 = OrderRecord(order_id="3", symbol="AAPL", side=OrderSide.BUY, qty=50, order_type=OrderType.MARKET)

        for o in [order1, order2, order3]:
            registry.submit(o)
            registry.on_fill(o.order_id, o.qty, 100.0)

        infy_fills = registry.get_filled_orders("INFY")
        assert len(infy_fills) == 2

        all_fills = registry.get_filled_orders()
        assert len(all_fills) == 3

    def test_total_commission(self):
        """Total commission sums correctly."""
        registry = OrderRegistry()

        order1 = OrderRecord(order_id="1", symbol="INFY", side=OrderSide.BUY, qty=100, order_type=OrderType.MARKET)
        order2 = OrderRecord(order_id="2", symbol="INFY", side=OrderSide.BUY, qty=200, order_type=OrderType.MARKET)

        for o in [order1, order2]:
            registry.submit(o)
            registry.on_fill(o.order_id, o.qty, 100.0)

        total = registry.total_commission("INFY")
        # Both orders fill at $100 → trade value $10k and $20k.
        # order1 (100 sh BUY): base = max(0.35, 100×0.0035=0.35) = $0.35
        #                       reg  = 100 × 0.000203 = $0.0203 → $0.3703
        # order2 (200 sh BUY): base = max(0.35, 200×0.0035=0.70) = $0.70
        #                       reg  = 200 × 0.000203 = $0.0406 → $0.7406
        # Total ≈ $1.1109 (was asserting $2.00 under the stale Fixed-rate formula).
        import pytest
        assert total == pytest.approx(1.1109, rel=1e-3)

    def test_get_today_trades(self):
        """Today trades counts today's fills."""
        registry = OrderRegistry()

        order = OrderRecord(
            order_id="1",
            symbol="INFY",
            side=OrderSide.BUY,
            qty=100,
            order_type=OrderType.MARKET,
        )
        registry.submit(order)
        registry.on_fill("1", 100, 100.0)

        trades = registry.get_today_trades("INFY")
        assert trades == 1
