"""ForexSpec regression + behavior tests.

These tests use the EURUSD diagnostic dump from 2026-06-03 as ground
truth for what an IDEALPRO feed actually looks like (bid populated,
ask populated, last STALE/None, spread = 1 pip = 0.00001).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from src.assets import (
    AssetClass, Currency, base_units, resolve, price, shares,
)
from src.assets.forex import (
    BidAskComparePricing,
    FXBaseCurrencySizing,
    FXRiskOverlay,
    ForexContinuousSession,
    IBKRFXCommission,
    IDEALPROForexContract,
    PipTickPolicy,
    make_forex_spec,
    _split_pair,
)
from src.assets.policies import FeedSnapshot, OrderIntent, RoundDirection
from src.assets.policies.sizing import SizingMismatch
from src.assets.policies.price import NoUsablePrice

UTC = timezone.utc


# ────────────────────────────────────────────────────────────────────
# Pair parsing
# ────────────────────────────────────────────────────────────────────

class TestPairParsing:
    def test_split_eurusd(self):
        base, quote = _split_pair("EURUSD")
        assert base is Currency.EUR
        assert quote is Currency.USD

    def test_split_usdjpy(self):
        base, quote = _split_pair("USDJPY")
        assert base is Currency.USD
        assert quote is Currency.JPY

    def test_rejects_4_letter(self):
        with pytest.raises(ValueError):
            _split_pair("PLTR")

    def test_rejects_7_letter(self):
        with pytest.raises(ValueError):
            _split_pair("EURUSDT")

    def test_rejects_unknown_currency(self):
        with pytest.raises(ValueError):
            _split_pair("EURXYZ")


# ────────────────────────────────────────────────────────────────────
# Spec composition
# ────────────────────────────────────────────────────────────────────

class TestSpecComposition:
    def test_eurusd_spec(self):
        spec = make_forex_spec("EURUSD")
        assert spec.asset_class is AssetClass.FX_CASH
        assert spec.quote_currency is Currency.USD
        assert spec.venue == "IDEALPRO"

    def test_usdjpy_spec(self):
        spec = make_forex_spec("USDJPY")
        assert spec.asset_class is AssetClass.FX_CASH
        assert spec.quote_currency is Currency.JPY

    def test_jpy_pair_uses_3dp_tick(self):
        spec = make_forex_spec("USDJPY")
        assert spec.tick.tick_size(price("153.25")) == Decimal("0.005")

    def test_non_jpy_pair_uses_5dp_tick(self):
        spec = make_forex_spec("EURUSD")
        assert spec.tick.tick_size(price("1.16175")) == Decimal("0.00005")


# ────────────────────────────────────────────────────────────────────
# Resolver
# ────────────────────────────────────────────────────────────────────

class TestResolver:
    def test_resolves_eurusd(self):
        spec = resolve("EURUSD")
        assert spec.asset_class is AssetClass.FX_CASH
        assert spec.quote_currency is Currency.USD

    def test_resolves_usdjpy(self):
        spec = resolve("USDJPY")
        assert spec.quote_currency is Currency.JPY

    def test_pltr_does_not_match_forex(self):
        spec = resolve("PLTR")
        assert spec.asset_class is AssetClass.US_EQUITY


# ────────────────────────────────────────────────────────────────────
# Tick policy — the key per-pair difference
# ────────────────────────────────────────────────────────────────────

class TestTickPolicy:
    def test_eurusd_rounds_to_5dp_pip_grid(self):
        tick = PipTickPolicy.for_pair("EURUSD")
        # Diagnostic-dump example: bid 1.1617, ask 1.16171
        # Round 1.161725 to nearest 0.00005 → 1.16170 (banker's rounding)
        assert tick.round_to_tick(price("1.161725"), RoundDirection.NEAREST) == price("1.16170")
        # 1.16173 → nearest 0.00005 → 1.16175
        assert tick.round_to_tick(price("1.16173"), RoundDirection.NEAREST) == price("1.16175")

    def test_usdjpy_rounds_to_3dp_half_pip(self):
        tick = PipTickPolicy.for_pair("USDJPY")
        # 153.252 → nearest 0.005 → 153.250
        assert tick.round_to_tick(price("153.252")) == price("153.250")
        # 153.253 → nearest 0.005 → 153.255
        assert tick.round_to_tick(price("153.253")) == price("153.255")

    def test_round_up_for_buy_limit(self):
        # When placing a BUY limit, round DOWN so we don't bid through
        tick = PipTickPolicy.for_pair("EURUSD")
        assert tick.round_to_tick(price("1.16173"), RoundDirection.DOWN) == price("1.16170")

    def test_decimals_for_display(self):
        assert PipTickPolicy.for_pair("EURUSD").decimals_for_display(price("1.16175")) == 5
        assert PipTickPolicy.for_pair("USDJPY").decimals_for_display(price("153.25")) == 3


# ────────────────────────────────────────────────────────────────────
# Price policy — uses bid/ask, NEVER last
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

    def test_reference_is_mid(self):
        pol = BidAskComparePricing()
        snap = self._snap(bid=price("1.1617"), ask=price("1.16171"))
        # Mid of 1.1617 and 1.16171 = 1.161705
        assert pol.reference(snap) == price("1.161705")

    def test_buy_compare_is_ask(self):
        pol = BidAskComparePricing()
        snap = self._snap(bid=price("1.1617"), ask=price("1.16171"))
        assert pol.buy_compare(snap) == price("1.16171")

    def test_sell_compare_is_bid(self):
        pol = BidAskComparePricing()
        snap = self._snap(bid=price("1.1617"), ask=price("1.16171"))
        assert pol.sell_compare(snap) == price("1.1617")

    def test_raises_on_missing_bid_ask(self):
        # Even if last is present, we DON'T fall back — IDEALPRO last
        # is documented stale.
        pol = BidAskComparePricing()
        snap = self._snap(last=price("1.16175"))  # bid/ask both None
        with pytest.raises(NoUsablePrice):
            pol.reference(snap)
        with pytest.raises(NoUsablePrice):
            pol.buy_compare(snap)
        with pytest.raises(NoUsablePrice):
            pol.sell_compare(snap)

    def test_actionable_tight_spread(self):
        pol = BidAskComparePricing()
        # 1-pip spread on EURUSD = 0.0001
        snap = self._snap(bid=price("1.1617"), ask=price("1.1618"))
        assert pol.is_actionable(snap)

    def test_not_actionable_when_crossed(self):
        pol = BidAskComparePricing()
        snap = self._snap(bid=price("1.16180"), ask=price("1.1617"))  # bid > ask
        assert not pol.is_actionable(snap)

    def test_not_actionable_when_spread_too_wide(self):
        pol = BidAskComparePricing(max_actionable_spread_pips=Decimal("5"))
        # 10-pip spread on EURUSD = 0.0010
        snap = self._snap(bid=price("1.1617"), ask=price("1.1627"))
        assert not pol.is_actionable(snap)

    def test_actionable_handles_jpy_pair_scale(self):
        # USDJPY: prices ~150, spread ~0.01 (1 pip) should be fine
        pol = BidAskComparePricing()
        snap = self._snap(bid=price("153.25"), ask=price("153.26"))
        assert pol.is_actionable(snap)


# ────────────────────────────────────────────────────────────────────
# Sizing policy
# ────────────────────────────────────────────────────────────────────

class TestSizingPolicy:
    def test_eurusd_notional(self):
        sizing = FXBaseCurrencySizing.for_pair("EURUSD")
        # 25,000 EUR @ 1.16175 = 29,043.75 USD
        n = sizing.notional(base_units(25000), price("1.16175"))
        assert n.amount == Decimal("29043.75000")
        assert n.currency is Currency.USD

    def test_usdjpy_notional_in_jpy(self):
        sizing = FXBaseCurrencySizing.for_pair("USDJPY")
        # 10,000 USD @ 153.25 = 1,532,500 JPY
        n = sizing.notional(base_units(10000), price("153.25"))
        assert n.amount == Decimal("1532500.00")
        assert n.currency is Currency.JPY

    def test_rejects_shares_qty(self):
        sizing = FXBaseCurrencySizing.for_pair("EURUSD")
        with pytest.raises(SizingMismatch):
            sizing.notional(shares(25000), price("1.16175"))

    def test_min_qty_is_25k(self):
        sizing = FXBaseCurrencySizing.for_pair("EURUSD")
        assert sizing.min_qty().value == Decimal("25000")

    def test_validates_above_min(self):
        sizing = FXBaseCurrencySizing.for_pair("EURUSD")
        assert sizing.is_valid_qty(base_units(25000))
        assert sizing.is_valid_qty(base_units(100000))
        assert not sizing.is_valid_qty(base_units(1000))


# ────────────────────────────────────────────────────────────────────
# Commission
# ────────────────────────────────────────────────────────────────────

class TestCommission:
    def test_small_notional_hits_min(self):
        # 25,000 EUR @ 1.16175 = 29,043.75 USD-equiv
        # 0.20 bps = 0.00002, fee = 29043.75 × 0.00002 = $0.58
        # → min $2 applies
        comm = IBKRFXCommission()
        fee = comm.estimate(base_units(25000), price("1.16175"), "BUY")
        assert fee.amount == Decimal("2.00")
        assert fee.currency is Currency.USD

    def test_large_notional_uses_bps(self):
        # 10,000,000 EUR @ 1.16175 = 11,617,500 USD-equiv
        # fee = 11,617,500 × 0.00002 = $232.35
        comm = IBKRFXCommission()
        fee = comm.estimate(base_units(10_000_000), price("1.16175"), "BUY")
        assert fee.amount == Decimal("232.35000")

    def test_rejects_shares(self):
        comm = IBKRFXCommission()
        with pytest.raises(SizingMismatch):
            comm.estimate(shares(25000), price("1.16175"), "BUY")


# ────────────────────────────────────────────────────────────────────
# Session policy — 24/5 weekly window
# ────────────────────────────────────────────────────────────────────

class TestSessionPolicy:
    def test_open_during_weekday(self):
        # Wednesday 12:00 UTC — middle of the week
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 16, 12, 0, tzinfo=UTC)  # Wed
        assert session.is_open_at(ts)

    def test_open_sunday_evening_after_22utc(self):
        # Sunday 22:30 UTC — just after weekly open
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 13, 22, 30, tzinfo=UTC)  # Sun
        assert session.is_open_at(ts)

    def test_closed_sunday_before_22utc(self):
        # Sunday 12:00 UTC — before weekly open at 22:00
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 13, 12, 0, tzinfo=UTC)
        assert not session.is_open_at(ts)

    def test_closed_friday_after_22utc(self):
        # Friday 22:30 UTC — after weekly close
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 11, 22, 30, tzinfo=UTC)  # Fri
        assert not session.is_open_at(ts)

    def test_closed_saturday(self):
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 12, 12, 0, tzinfo=UTC)  # Sat
        assert not session.is_open_at(ts)

    def test_next_open_from_saturday(self):
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 12, 12, 0, tzinfo=UTC)  # Sat
        # Next open = Sunday April 13, 22:00 UTC
        expected = datetime(2025, 4, 13, 22, 0, tzinfo=UTC)
        assert session.next_open(ts) == expected

    def test_next_close_during_week(self):
        # Wed 12:00 UTC → next close = this Friday 22:00 UTC
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 16, 12, 0, tzinfo=UTC)
        expected = datetime(2025, 4, 18, 22, 0, tzinfo=UTC)
        assert session.next_close(ts) == expected

    def test_within_30min_of_close(self):
        # Friday 21:35 UTC — 25 min before weekly close
        session = ForexContinuousSession()
        ts = datetime(2025, 4, 18, 21, 35, tzinfo=UTC)
        assert session.is_within_n_minutes_of_close(ts, minutes=30)
        assert not session.is_within_n_minutes_of_close(ts, minutes=15)


# ────────────────────────────────────────────────────────────────────
# Risk overlay — weekend gap
# ────────────────────────────────────────────────────────────────────

class TestFXRiskOverlay:
    def _intent(self, side="BUY", spec=None):
        return OrderIntent(
            symbol="EURUSD", side=side, qty=base_units(25000),
            intended_price=price("1.16175"), order_type="MARKET",
            spec=spec,
        )

    def test_sell_never_blocked(self):
        overlay = FXRiskOverlay()
        v = overlay.check(self._intent(side="SELL"), None)
        assert v.allow

    def test_buy_ok_outside_close_window(self):
        # Mid-week, far from close → ok
        spec = make_forex_spec("EURUSD")
        overlay = FXRiskOverlay()
        # The overlay reads datetime.now() internally; we can't easily
        # mock it without freezegun. Run it now and just assert that
        # if we're NOT within 30 min of Friday 22:00 UTC, we pass.
        now = datetime.now(tz=UTC)
        if spec.session.is_within_n_minutes_of_close(now, 30):
            # We happen to be in the gap window right now — skip
            pytest.skip("Test runtime fell inside the FX close window")
        v = overlay.check(self._intent(side="BUY", spec=spec), None)
        assert v.allow


# ────────────────────────────────────────────────────────────────────
# End-to-end via the spec — covers what the engine will actually do
# ────────────────────────────────────────────────────────────────────

class TestEndToEnd:
    def test_eurusd_via_resolve(self):
        spec = resolve("EURUSD")
        # Use the diagnostic-dump snapshot as input
        snap = FeedSnapshot(
            bid=price("1.1617"), ask=price("1.16171"), last=None,
            bid_size=1_000_000, ask_size=1_500_000, last_size=0,
            volume=0, high=price("1.16335"), low=price("1.1605"),
            vwap=None, ts=datetime.now(tz=UTC),
        )
        # Verify each policy gives sensible output
        assert spec.price.reference(snap) == price("1.161705")
        assert spec.price.buy_compare(snap) == price("1.16171")
        assert spec.price.sell_compare(snap) == price("1.1617")
        assert spec.price.is_actionable(snap)
        # Notional for 25k EUR
        n = spec.sizing.notional(base_units(25000), price("1.16175"))
        assert n.amount == Decimal("29043.75000")
        # Tick rounding
        assert spec.tick.round_to_tick(price("1.161725")) == price("1.16170")
