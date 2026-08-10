"""FutureSpec tests + per-root multiplier regression baseline.

The multiplier math is the most safety-critical part of futures
trading: if the spec ships the wrong number, the risk gate is blind.
This file tests every supported root's notional explicitly so a typo
in FUTURE_ROOTS would surface in CI immediately.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.assets import (
    AssetClass, Currency, contracts, resolve, price,
)
from src.assets.future import (
    FUTURE_ROOTS,
    FuturesContractPolicy,
    FuturesRiskOverlay,
    FuturesRollLifecycle,
    FuturesTickPolicy,
    IBKRFuturesCommission,
    MultiplierSizing,
    _front_quarterly_month,
    _parse_futures_symbol,
    make_future_spec,
)
from src.assets.policies import FeedSnapshot, OrderIntent, PortfolioView, RoundDirection
from src.assets.policies.sizing import SizingMismatch
from src.assets.types import Money, Quantity, QuantityUnit, usd

UTC = timezone.utc


# ────────────────────────────────────────────────────────────────────
# Symbol parsing
# ────────────────────────────────────────────────────────────────────

class TestSymbolParsing:
    def test_root_only(self):
        root, month = _parse_futures_symbol("ES")
        assert root == "ES" and month is None

    def test_root_yyyymm(self):
        root, month = _parse_futures_symbol("ES202503")
        assert root == "ES" and month == "202503"

    def test_root_code_year(self):
        root, month = _parse_futures_symbol("ESH5")
        assert root == "ES"
        # Year encoding is decade-relative; just confirm shape
        assert month is not None and month.endswith("03")

    def test_rejects_unknown_root(self):
        with pytest.raises(ValueError):
            _parse_futures_symbol("ZZZ")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            _parse_futures_symbol("ES_BAD")

    def test_front_quarterly_month_format(self):
        m = _front_quarterly_month()
        assert len(m) == 6
        assert m[4:] in ("03", "06", "09", "12")


# ────────────────────────────────────────────────────────────────────
# Multiplier sizing — THE SAFETY-CRITICAL TEST
# ────────────────────────────────────────────────────────────────────

class TestMultiplierSizingPerRoot:
    """For every supported root, verify the notional math at $4500.

    A typo in FUTURE_ROOTS multiplier would silently mis-size every
    order. This test makes the per-root values explicit so a CI
    failure points at the exact root and value.
    """

    @pytest.mark.parametrize("root,price_str,expected_notional", [
        ("ES",  "4500", Decimal("225000")),  # 1 × 4500 × 50
        ("MES", "4500", Decimal("22500")),   # 1 × 4500 × 5  (10× difference!)
        ("NQ",  "4500", Decimal("90000")),   # 1 × 4500 × 20
        ("MNQ", "4500", Decimal("9000")),    # 1 × 4500 × 2  (also 10× microvs full)
        ("RTY", "2000", Decimal("100000")),  # 1 × 2000 × 50
        ("M2K", "2000", Decimal("10000")),
        ("GC",  "2400", Decimal("240000")),  # 1 × 2400 × 100
        ("MGC", "2400", Decimal("24000")),
        ("SI",  "30",   Decimal("150000")),  # 1 × 30 × 5000
        ("CL",  "80",   Decimal("80000")),   # 1 × 80 × 1000
        ("MCL", "80",   Decimal("8000")),
        ("NG",  "3",    Decimal("30000")),   # 1 × 3 × 10000
    ])
    def test_one_contract_notional(self, root, price_str, expected_notional):
        spec = make_future_spec(root)
        n = spec.sizing.notional(contracts(1), price(price_str))
        assert n.amount == expected_notional, (
            f"{root} multiplier mis-sized: 1 contract @ {price_str} "
            f"= {n.amount} but expected {expected_notional}"
        )
        assert n.currency is Currency.USD

    def test_es_vs_mes_10x_relationship(self):
        es = make_future_spec("ES").sizing
        mes = make_future_spec("MES").sizing
        # ES notional = 10× MES notional at same price/qty
        es_n = es.notional(contracts(1), price("4500"))
        mes_n = mes.notional(contracts(1), price("4500"))
        assert es_n.amount == mes_n.amount * 10

    def test_rejects_shares_qty(self):
        from src.assets import shares
        spec = make_future_spec("ES")
        with pytest.raises(SizingMismatch):
            spec.sizing.notional(shares(1), price("4500"))

    def test_rejects_fractional_contracts(self):
        from src.assets.types import Quantity
        spec = make_future_spec("ES")
        # Fractional contracts construction goes via contracts(1) which
        # rejects non-integer at the type level
        from src.assets import contracts as ctr
        with pytest.raises(ValueError):
            ctr("0.5")


# ────────────────────────────────────────────────────────────────────
# Tick policy per root
# ────────────────────────────────────────────────────────────────────

class TestTickPolicyPerRoot:
    @pytest.mark.parametrize("root,unrounded,expected", [
        ("ES",  "4500.30", "4500.25"),   # quarter-point
        ("NQ",  "15000.40", "15000.50"), # quarter-point
        ("GC",  "2400.13",  "2400.10"),  # dime
        ("CL",  "80.123",   "80.12"),    # cent
        ("NG",  "3.0007",   "3.001"),    # 0.001
        ("SI",  "30.0072",  "30.005"),   # half-cent
    ])
    def test_round_to_grid(self, root, unrounded, expected):
        spec = make_future_spec(root)
        assert spec.tick.round_to_tick(price(unrounded)) == price(expected)


# ────────────────────────────────────────────────────────────────────
# Commission
# ────────────────────────────────────────────────────────────────────

class TestFuturesCommission:
    def test_es_per_contract(self):
        spec = make_future_spec("ES")
        fee = spec.commission.estimate(contracts(1), price("4500"), "BUY")
        assert fee.amount == Decimal("2.27")
        assert fee.currency is Currency.USD

    def test_mes_cheaper_than_es(self):
        # Micros have lower fees
        es = make_future_spec("ES").commission.estimate(contracts(1), price("4500"), "BUY")
        mes = make_future_spec("MES").commission.estimate(contracts(1), price("4500"), "BUY")
        assert mes.amount < es.amount

    def test_scales_with_qty(self):
        spec = make_future_spec("ES")
        f1 = spec.commission.estimate(contracts(1), price("4500"), "BUY")
        f10 = spec.commission.estimate(contracts(10), price("4500"), "BUY")
        assert f10.amount == f1.amount * 10


# ────────────────────────────────────────────────────────────────────
# Lifecycle — roll detection
# ────────────────────────────────────────────────────────────────────

class TestFuturesRollLifecycle:
    def test_not_rolling_when_far_from_expiry(self):
        from ib_async import Future
        lc = FuturesRollLifecycle(roll_window_days=5)
        # Use a fixed contract with month 6 months out
        c = Future("ES", "203012", "CME", multiplier="50", currency="USD")
        now = datetime(2026, 6, 5, tzinfo=UTC)
        assert not lc.needs_roll(c, now)

    def test_rolls_within_window(self):
        from ib_async import Future
        lc = FuturesRollLifecycle(roll_window_days=10)
        # Contract expires June 2026 (~20th); test 5 days before
        c = Future("ES", "202606", "CME", multiplier="50", currency="USD")
        now = datetime(2026, 6, 15, tzinfo=UTC)
        assert lc.needs_roll(c, now)

    def test_expiry_returned(self):
        from ib_async import Future
        lc = FuturesRollLifecycle()
        c = Future("ES", "202603", "CME", multiplier="50", currency="USD")
        assert lc.expiry(c) == date(2026, 3, 20)

    def test_no_overnight_financing(self):
        # Distinct from CFDs: futures don't accrue daily financing
        assert not FuturesRollLifecycle().has_overnight_financing()


# ────────────────────────────────────────────────────────────────────
# Risk overlay
# ────────────────────────────────────────────────────────────────────

class TestFuturesRiskOverlay:
    def _portfolio(self, bp_usd: Decimal) -> PortfolioView:
        return PortfolioView(
            total_open_notional_base=usd(0),
            daily_pnl_base=usd(0),
            account_equity_base=usd(100_000),
            account_buying_power_base=usd(bp_usd),
        )

    def _intent(self, spec, side="BUY", qty=1):
        return OrderIntent(
            symbol="ES", side=side, qty=contracts(qty),
            intended_price=price("4500"), order_type="MARKET",
            spec=spec,
        )

    def test_sell_never_blocked_by_margin(self):
        spec = make_future_spec("ES")
        overlay = FuturesRiskOverlay(margin_buffer_pct=Decimal("8"))
        # BP only $1k, but it's a SELL — overlay passes
        portfolio = self._portfolio(Decimal("1000"))
        v = overlay.check(self._intent(spec, side="SELL"), portfolio)
        assert v.allow

    def test_buy_blocked_when_bp_insufficient(self):
        spec = make_future_spec("ES")
        overlay = FuturesRiskOverlay(margin_buffer_pct=Decimal("8"))
        # ES 1 contract @ 4500 = $225K notional; 8% = $18K required
        # BP = $10K → insufficient
        portfolio = self._portfolio(Decimal("10000"))
        v = overlay.check(self._intent(spec, side="BUY"), portfolio)
        assert not v.allow
        assert "margin" in v.reason.lower()

    def test_buy_ok_when_bp_sufficient(self):
        spec = make_future_spec("ES")
        overlay = FuturesRiskOverlay(margin_buffer_pct=Decimal("8"))
        # BP = $50K, more than enough
        portfolio = self._portfolio(Decimal("50000"))
        v = overlay.check(self._intent(spec, side="BUY"), portfolio)
        # Margin OK; roll-window may still flag if test runs near expiry
        # — accept either allow or roll-window block
        assert v.allow or "roll" in v.reason.lower()


# ────────────────────────────────────────────────────────────────────
# Spec composition + resolver
# ────────────────────────────────────────────────────────────────────

class TestSpecComposition:
    def test_resolve_es(self):
        spec = resolve("ES")
        assert spec.asset_class is AssetClass.FUTURE
        assert spec.quote_currency is Currency.USD
        assert spec.venue == "CME"

    def test_resolve_es_yyyymm(self):
        spec = resolve("ES202503")
        assert spec.asset_class is AssetClass.FUTURE

    def test_resolve_es_with_code(self):
        spec = resolve("ESH5")
        assert spec.asset_class is AssetClass.FUTURE

    def test_es_overrides_equity_pattern(self):
        # "ES" matches the equity pattern shape, but FuturesSpec is
        # higher-priority and claims it.
        spec = resolve("ES")
        assert spec.asset_class is AssetClass.FUTURE

    def test_aapl_still_goes_to_equity(self):
        # AAPL doesn't match a futures root — equity wins
        spec = resolve("AAPL")
        assert spec.asset_class is AssetClass.US_EQUITY

    def test_unknown_root_raises_via_make(self):
        with pytest.raises(ValueError):
            make_future_spec("ZZZ")
