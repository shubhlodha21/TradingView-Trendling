"""Regression-safety tests for USEquitySpec.

THE INVARIANT THIS SUITE VERIFIES:

  Every method on USEquitySpec produces output that matches the
  hardcoded equity behavior in today's nabi engine. If a test here
  fails, it means USEquitySpec drifted from equity behavior; engine
  routing through the spec will silently behave differently than
  the equity engine does today. That's a regression.

The pattern: for each method, hardcode the result the equity engine
WOULD produce today (e.g. round(151.713, 2) → 151.71), then assert
the spec method matches.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.assets import (
    AssetClass, Currency, SpecRegistry, resolve, shares, price,
)
from src.assets.policies import RoundDirection, FeedSnapshot
from src.assets.us_stock import (
    DecimalTickPolicy,
    IBKRTieredEquityCommission,
    LastPricePolicy,
    SMARTStockContract,
    SimpleSizing,
    USEquityRiskOverlay,
    USEquitySession,
    make_us_equity_spec,
    NYSE_HOLIDAYS_2024_2026,
)

UTC = timezone.utc


# ────────────────────────────────────────────────────────────────────
# Spec composition
# ────────────────────────────────────────────────────────────────────

class TestSpecComposition:
    def test_make_us_equity_spec_returns_us_equity(self):
        spec = make_us_equity_spec("PLTR")
        assert spec.asset_class is AssetClass.US_EQUITY
        assert spec.quote_currency is Currency.USD
        assert spec.venue == "SMART"

    def test_spec_describe(self):
        spec = make_us_equity_spec("PLTR")
        s = spec.describe()
        assert "US_EQUITY" in s
        assert "SMARTStockContract" in s
        assert "LastPricePolicy" in s

    def test_spec_audit_dict_includes_all_policies(self):
        spec = make_us_equity_spec("PLTR")
        d = spec.to_audit_dict()
        assert d["asset_class"] == "US_EQUITY"
        assert d["quote_currency"] == "USD"
        assert d["venue"] == "SMART"
        assert d["policies"]["sizing"] == "SimpleSizing"


# ────────────────────────────────────────────────────────────────────
# Resolver
# ────────────────────────────────────────────────────────────────────

class TestResolver:
    def test_resolves_common_equity_symbols(self):
        for sym in ("PLTR", "AAPL", "MSFT", "GOOG", "TSLA", "NVDA"):
            spec = resolve(sym)
            assert spec.asset_class is AssetClass.US_EQUITY, f"failed on {sym}"

    def test_resolves_dotted_share_classes(self):
        # BRK.B, BF.B — dot-suffixed share classes
        spec = resolve("BRK.B")
        assert spec.asset_class is AssetClass.US_EQUITY

    def test_hint_can_force_different_class(self):
        # Without a hint, "AAPL" resolves to US_EQUITY. With hint
        # forcing SHARE_CFD, the US-equity resolver yields and the
        # SHARE_CFD resolver claims it. (D2-AM now ships the SHARE_CFD
        # resolver — before that this raised UnknownSymbol.)
        spec = resolve("AAPL", hint=AssetClass.SHARE_CFD)
        assert spec.asset_class is AssetClass.SHARE_CFD

    def test_lowercase_symbol_normalized(self):
        spec = resolve("pltr")  # operator typo case
        assert spec.asset_class is AssetClass.US_EQUITY


# ────────────────────────────────────────────────────────────────────
# Tick policy — THE PRIMARY REGRESSION BASELINE
# ────────────────────────────────────────────────────────────────────

class TestTickPolicyMatchesEquity:
    """USEquityTickPolicy must produce identical output to
    `round(price, 2)` which is the 22-site equity behavior."""

    @pytest.mark.parametrize("input_price", [
        "151.713", "151.715", "151.71", "151.716",
        "0.01", "0.99", "1.50",
        "999.999", "10000.001",
        "151.70", "151.72",
    ])
    def test_round_to_tick_matches_round_2(self, input_price):
        # The behavior we're preserving: round(p, 2) with banker's
        # rounding (Python's default).
        tick = DecimalTickPolicy(decimals=2)
        p = price(input_price)
        result = tick.round_to_tick(p, RoundDirection.NEAREST)
        # Compute the reference value using Decimal quantize (avoids
        # float's binary-rep noise that built-in round() suffers from)
        reference = Decimal(input_price).quantize(Decimal("0.01"))
        assert result == reference, f"{input_price}: spec={result} vs reference={reference}"

    def test_tick_size_is_one_cent(self):
        tick = DecimalTickPolicy(decimals=2)
        assert tick.tick_size(price("151.71")) == Decimal("0.01")

    def test_round_down(self):
        tick = DecimalTickPolicy(decimals=2)
        assert tick.round_to_tick(price("151.719"), RoundDirection.DOWN) == price("151.71")

    def test_round_up(self):
        tick = DecimalTickPolicy(decimals=2)
        assert tick.round_to_tick(price("151.711"), RoundDirection.UP) == price("151.72")

    def test_already_on_grid(self):
        tick = DecimalTickPolicy(decimals=2)
        assert tick.round_to_tick(price("151.71")) == price("151.71")


# ────────────────────────────────────────────────────────────────────
# Sizing policy
# ────────────────────────────────────────────────────────────────────

class TestSizingMatchesEquity:
    def test_notional_is_qty_times_price(self):
        # The hardcoded equity behavior: notional = qty * price, USD.
        sizing = SimpleSizing.for_us_equity()
        n = sizing.notional(shares(30), price("151.71"))
        assert n.amount == Decimal("4551.30")
        assert n.currency is Currency.USD

    def test_notional_pltr_30_at_market(self):
        # Concrete test from today's PLTR trade.
        sizing = SimpleSizing.for_us_equity()
        n = sizing.notional(shares(30), price("151.50"))
        assert n.amount == Decimal("4545.00")

    def test_rejects_wrong_unit(self):
        from src.assets import base_units
        from src.assets.policies.sizing import SizingMismatch
        sizing = SimpleSizing.for_us_equity()
        with pytest.raises(SizingMismatch):
            sizing.notional(base_units(25000), price("1.16175"))

    def test_min_qty_is_1_share(self):
        sizing = SimpleSizing.for_us_equity()
        assert sizing.min_qty() == shares(1)

    def test_qty_increment_is_1_share(self):
        sizing = SimpleSizing.for_us_equity()
        assert sizing.qty_increment() == shares(1)

    def test_validates_integer_qty(self):
        sizing = SimpleSizing.for_us_equity()
        assert sizing.is_valid_qty(shares(30))
        assert not sizing.is_valid_qty(shares(0))


# ────────────────────────────────────────────────────────────────────
# Commission policy
# ────────────────────────────────────────────────────────────────────

class TestCommissionEstimate:
    def test_tiered_per_share_dominant_for_large_orders(self):
        # 1000 shares × $0.0035 = $3.50 (above $0.35 min)
        comm = IBKRTieredEquityCommission()
        fee = comm.estimate(shares(1000), price("100"), "BUY")
        assert fee.amount == Decimal("3.5000")  # 1000 * 0.0035
        assert fee.currency is Currency.USD

    def test_minimum_applies_for_tiny_orders(self):
        # 10 shares × $0.0035 = $0.035, but min is $0.35
        comm = IBKRTieredEquityCommission()
        fee = comm.estimate(shares(10), price("100"), "BUY")
        assert fee.amount == Decimal("0.35")

    def test_max_pct_caps_extreme_orders(self):
        # 1 share at $10 → raw fee $0.0035, but trade value $10,
        # max = 1% of $10 = $0.10. Min wins ($0.35 > $0.10).
        # Need a case where pct cap < per-share fee.
        # 100,000 shares × $0.0035 = $350; trade value 100k × $0.01 = $1,000;
        # max = 1% of $1,000 = $10. Cap wins.
        comm = IBKRTieredEquityCommission()
        fee = comm.estimate(shares(100000), price("0.01"), "BUY")
        # Max(min=0.35, min(350, 10)) = max(0.35, 10) = 10
        assert fee.amount == Decimal("10.0000")

    def test_rejects_non_shares_qty(self):
        from src.assets import contracts
        from src.assets.policies.sizing import SizingMismatch
        comm = IBKRTieredEquityCommission()
        with pytest.raises(SizingMismatch):
            comm.estimate(contracts(1), price("100"), "BUY")


# ────────────────────────────────────────────────────────────────────
# Session policy
# ────────────────────────────────────────────────────────────────────

class TestSessionPolicy:
    def test_open_during_rth(self):
        # 2025-04-15 (Tuesday) 14:30 UTC = 10:30 ET — should be open
        session = USEquitySession()
        ts = datetime(2025, 4, 15, 14, 30, tzinfo=UTC)
        assert session.is_open_at(ts)

    def test_closed_after_hours(self):
        # 2025-04-15 (Tuesday) 22:00 UTC = 18:00 ET — after close
        session = USEquitySession()
        ts = datetime(2025, 4, 15, 22, 0, tzinfo=UTC)
        assert not session.is_open_at(ts)

    def test_closed_on_weekend(self):
        # 2025-04-12 (Saturday) 14:30 UTC
        session = USEquitySession()
        ts = datetime(2025, 4, 12, 14, 30, tzinfo=UTC)
        assert not session.is_open_at(ts)

    def test_closed_on_holiday(self):
        # 2025-07-04 (Friday, US Independence Day)
        session = USEquitySession()
        ts = datetime(2025, 7, 4, 14, 30, tzinfo=UTC)
        assert not session.is_open_at(ts)

    def test_within_5min_of_close(self):
        # 2025-04-15 19:56 UTC = 15:56 ET (4 min before 16:00 close)
        session = USEquitySession()
        ts = datetime(2025, 4, 15, 19, 56, tzinfo=UTC)
        assert session.is_within_n_minutes_of_close(ts, minutes=5)

    def test_not_within_5min_of_close_midday(self):
        session = USEquitySession()
        ts = datetime(2025, 4, 15, 16, 0, tzinfo=UTC)  # 12:00 ET
        assert not session.is_within_n_minutes_of_close(ts, minutes=5)

    def test_time_to_close(self):
        # 19:00 UTC = 15:00 ET = 60 min to close
        session = USEquitySession()
        ts = datetime(2025, 4, 15, 19, 0, tzinfo=UTC)
        assert session.time_to_close(ts) == 60 * 60

    def test_time_to_close_none_when_closed(self):
        session = USEquitySession()
        ts = datetime(2025, 4, 15, 22, 0, tzinfo=UTC)  # after close
        assert session.time_to_close(ts) is None

    def test_next_open_skips_weekend(self):
        # Friday 22:00 UTC → next open is Monday's open
        session = USEquitySession()
        ts = datetime(2025, 4, 11, 22, 0, tzinfo=UTC)  # Friday after close
        next_open = session.next_open(ts)
        # Monday April 14, 2025, 09:30 ET = 13:30 UTC (EDT)
        assert next_open == datetime(2025, 4, 14, 13, 30, tzinfo=UTC)

    def test_holidays_loaded(self):
        session = USEquitySession()
        # Sanity: at least Christmas should be in there
        assert any(d.month == 12 and d.day == 25 for d in session.holidays)


# ────────────────────────────────────────────────────────────────────
# Price policy
# ────────────────────────────────────────────────────────────────────

class TestPricePolicy:
    def _snap(self, **kw) -> FeedSnapshot:
        defaults = dict(
            bid=None, ask=None, last=None,
            bid_size=None, ask_size=None, last_size=None,
            volume=None, high=None, low=None, vwap=None,
            ts=datetime.now(tz=UTC),
        )
        defaults.update(kw)
        return FeedSnapshot(**defaults)

    def test_reference_uses_last(self):
        pol = LastPricePolicy()
        snap = self._snap(bid=price("151.70"), ask=price("151.72"), last=price("151.71"))
        assert pol.reference(snap) == price("151.71")

    def test_buy_compare_uses_last_for_equity(self):
        # Equity convention: tight spread, last is reliable
        pol = LastPricePolicy()
        snap = self._snap(bid=price("151.70"), ask=price("151.72"), last=price("151.71"))
        assert pol.buy_compare(snap) == price("151.71")

    def test_falls_back_to_mid_when_last_missing(self):
        pol = LastPricePolicy()
        snap = self._snap(bid=price("151.70"), ask=price("151.72"), last=None)
        # Mid = (151.70 + 151.72) / 2 = 151.71
        assert pol.reference(snap) == price("151.71")

    def test_raises_when_nothing_usable(self):
        from src.assets.policies.price import NoUsablePrice
        pol = LastPricePolicy()
        snap = self._snap()  # all None
        with pytest.raises(NoUsablePrice):
            pol.reference(snap)

    def test_is_actionable_tight_spread(self):
        pol = LastPricePolicy()
        snap = self._snap(bid=price("151.70"), ask=price("151.72"), last=price("151.71"))
        assert pol.is_actionable(snap)

    def test_not_actionable_when_crossed(self):
        pol = LastPricePolicy()
        # Crossed: bid > ask
        snap = self._snap(bid=price("151.72"), ask=price("151.70"), last=price("151.71"))
        assert not pol.is_actionable(snap)

    def test_not_actionable_when_spread_too_wide(self):
        pol = LastPricePolicy(max_actionable_spread_bps=Decimal("50"))
        # Spread = 100 bps = $1.50 on $150 mid
        snap = self._snap(bid=price("150.00"), ask=price("151.50"), last=price("150.75"))
        assert not pol.is_actionable(snap)

    def test_not_actionable_when_stale(self):
        from datetime import timedelta
        pol = LastPricePolicy(max_actionable_age_seconds=30)
        old = datetime.now(tz=UTC) - timedelta(seconds=60)
        snap = self._snap(bid=price("151.70"), ask=price("151.72"), last=price("151.71"), ts=old)
        assert not pol.is_actionable(snap)


# ────────────────────────────────────────────────────────────────────
# Risk overlay
# ────────────────────────────────────────────────────────────────────

class TestRiskOverlay:
    def test_us_equity_overlay_always_passes(self):
        overlay = USEquityRiskOverlay()
        # OrderIntent shape matters but the overlay ignores everything
        # — we can pass None for portfolio without crash since it's
        # never inspected.
        from src.assets.policies import OrderIntent
        intent = OrderIntent(
            symbol="PLTR", side="BUY", qty=shares(30),
            intended_price=price("151.71"), order_type="MARKET",
            spec=None,  # not used by this overlay
        )
        verdict = overlay.check(intent, None)  # portfolio not used
        assert verdict.allow
        assert "universal" in verdict.reason


# ────────────────────────────────────────────────────────────────────
# Lifecycle policy
# ────────────────────────────────────────────────────────────────────

class TestLifecyclePolicy:
    def test_equity_never_rolls(self):
        from src.assets.us_stock import NoLifecycle
        lc = NoLifecycle()
        assert not lc.needs_roll(None, datetime.now(tz=UTC))

    def test_equity_has_no_expiry(self):
        from src.assets.us_stock import NoLifecycle
        lc = NoLifecycle()
        assert lc.expiry(None) is None

    def test_t_plus_1_settlement(self):
        from src.assets.us_stock import NoLifecycle
        lc = NoLifecycle(settlement_days_=1)
        assert lc.settlement_days() == 1

    def test_no_overnight_financing(self):
        from src.assets.us_stock import NoLifecycle
        assert not NoLifecycle().has_overnight_financing()
