"""CFD spec tests — Index CFD, Share CFD, FX CFD."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.assets import (
    AssetClass, Currency, base_units, cfd_units, resolve, price,
)
from src.assets.cfds import (
    CFDLifecycle,
    CFDMarkPricing,
    CFDRiskOverlay,
    CFDSizing,
    FixedGrainTickPolicy,
    IBKRCFDCommission,
    INDEX_CFD_METADATA,
    make_fx_cfd_spec,
    make_index_cfd_spec,
    make_share_cfd_spec,
)
from src.assets.policies import FeedSnapshot, RoundDirection

UTC = timezone.utc


# ────────────────────────────────────────────────────────────────────
# Index CFD
# ────────────────────────────────────────────────────────────────────

class TestIndexCFD:
    def test_ibus500_spec(self):
        spec = make_index_cfd_spec("IBUS500")
        assert spec.asset_class is AssetClass.INDEX_CFD
        assert spec.quote_currency is Currency.USD
        assert spec.venue == "SMART"

    def test_ibde40_uses_eur(self):
        spec = make_index_cfd_spec("IBDE40")
        assert spec.quote_currency is Currency.EUR

    def test_ibus500_tick_is_quarter_point(self):
        spec = make_index_cfd_spec("IBUS500")
        # S&P 500 CFD ticks at 0.25 (matches ES futures)
        assert spec.tick.tick_size(price("4500.25")) == Decimal("0.25")
        # Rounding: 4500.30 → 4500.25 (nearest)
        assert spec.tick.round_to_tick(price("4500.30")) == price("4500.25")

    def test_unknown_symbol_raises(self):
        with pytest.raises(ValueError):
            make_index_cfd_spec("NEVERHEARDOF")

    def test_resolver_finds_ibus500(self):
        spec = resolve("IBUS500")
        assert spec.asset_class is AssetClass.INDEX_CFD


# ────────────────────────────────────────────────────────────────────
# Share CFD
# ────────────────────────────────────────────────────────────────────

class TestShareCFD:
    def test_aapl_share_cfd_via_hint(self):
        # Only fires with explicit hint; default for "AAPL" is US_EQUITY
        spec = resolve("AAPL", hint=AssetClass.SHARE_CFD)
        assert spec.asset_class is AssetClass.SHARE_CFD
        assert spec.quote_currency is Currency.USD

    def test_aapl_without_hint_is_equity(self):
        spec = resolve("AAPL")  # no hint
        assert spec.asset_class is AssetClass.US_EQUITY

    def test_share_cfd_uses_penny_ticks(self):
        spec = make_share_cfd_spec("AAPL")
        assert spec.tick.tick_size(price("180")) == Decimal("0.01")


# ────────────────────────────────────────────────────────────────────
# FX CFD
# ────────────────────────────────────────────────────────────────────

class TestFXCFD:
    def test_eurusd_via_hint(self):
        spec = resolve("EURUSD", hint=AssetClass.FX_CFD)
        assert spec.asset_class is AssetClass.FX_CFD
        assert spec.quote_currency is Currency.USD

    def test_eurusd_without_hint_is_spot_fx(self):
        spec = resolve("EURUSD")
        assert spec.asset_class is AssetClass.FX_CASH

    def test_fx_cfd_has_zero_commission(self):
        # FX CFDs at IBKR have commission baked into spread
        spec = make_fx_cfd_spec("EURUSD")
        fee = spec.commission.estimate(cfd_units(25000), price("1.16175"), "BUY")
        assert fee.amount == Decimal("0")


# ────────────────────────────────────────────────────────────────────
# Tick — FixedGrainTickPolicy
# ────────────────────────────────────────────────────────────────────

class TestFixedGrainTick:
    def test_quarter_point_grid(self):
        tick = FixedGrainTickPolicy.for_grain(Decimal("0.25"))
        assert tick.round_to_tick(price("4500.30")) == price("4500.25")
        assert tick.round_to_tick(price("4500.50")) == price("4500.50")
        assert tick.round_to_tick(price("4500.62")) == price("4500.50")  # banker's rounds half to even
        assert tick.round_to_tick(price("4500.63")) == price("4500.75")

    def test_one_point_grid(self):
        tick = FixedGrainTickPolicy.for_grain(Decimal("1"))
        assert tick.round_to_tick(price("39524.5")) == price("39524")
        assert tick.round_to_tick(price("39524.51")) == price("39525")

    def test_round_up(self):
        tick = FixedGrainTickPolicy.for_grain(Decimal("0.25"))
        assert tick.round_to_tick(price("4500.30"), RoundDirection.UP) == price("4500.50")


# ────────────────────────────────────────────────────────────────────
# Sizing — CFD math is straightforward
# ────────────────────────────────────────────────────────────────────

class TestCFDSizing:
    def test_index_cfd_notional(self):
        # 10 units of IBUS500 at 4500.25 = $45,002.50
        sizing = CFDSizing(quote_currency=Currency.USD)
        n = sizing.notional(cfd_units(10), price("4500.25"))
        assert n.amount == Decimal("45002.50")
        assert n.currency is Currency.USD

    def test_share_cfd_notional(self):
        # 30 units of AAPL CFD at 180.50 = $5,415
        sizing = CFDSizing(quote_currency=Currency.USD)
        n = sizing.notional(cfd_units(30), price("180.50"))
        assert n.amount == Decimal("5415.00")

    def test_rejects_shares_unit(self):
        from src.assets import shares
        from src.assets.policies.sizing import SizingMismatch
        sizing = CFDSizing(quote_currency=Currency.USD)
        with pytest.raises(SizingMismatch):
            sizing.notional(shares(30), price("100"))


# ────────────────────────────────────────────────────────────────────
# Commission
# ────────────────────────────────────────────────────────────────────

class TestCFDCommission:
    def test_5bps_on_large_order(self):
        # 10 units IBUS500 @ 4500.25 = $45,002.50 notional
        # 0.05% = $22.50
        comm = IBKRCFDCommission(bps=Decimal("5"), min_fee=Decimal("1"))
        fee = comm.estimate(cfd_units(10), price("4500.25"), "BUY")
        assert fee.amount == Decimal("22.501250")

    def test_min_fee_on_tiny_order(self):
        comm = IBKRCFDCommission(bps=Decimal("5"), min_fee=Decimal("1"))
        # 1 share AAPL @ $5 = $5 notional, 5 bps = $0.0025 → min $1
        fee = comm.estimate(cfd_units(1), price("5"), "BUY")
        assert fee.amount == Decimal("1")


# ────────────────────────────────────────────────────────────────────
# Lifecycle — overnight financing flag
# ────────────────────────────────────────────────────────────────────

class TestCFDLifecycle:
    def test_has_overnight_financing(self):
        lc = CFDLifecycle()
        assert lc.has_overnight_financing()

    def test_no_roll_no_expiry(self):
        lc = CFDLifecycle()
        assert not lc.needs_roll(None, datetime.now(tz=UTC))
        assert lc.expiry(None) is None

    def test_zero_settlement(self):
        # CFDs are continuous mark-to-market — no T+N settlement
        assert CFDLifecycle().settlement_days() == 0


# ────────────────────────────────────────────────────────────────────
# Pricing
# ────────────────────────────────────────────────────────────────────

class TestCFDMarkPricing:
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
        pol = CFDMarkPricing()
        snap = self._snap(bid=price("4500.00"), ask=price("4500.50"))
        assert pol.reference(snap) == price("4500.25")

    def test_buy_compare_is_ask(self):
        pol = CFDMarkPricing()
        snap = self._snap(bid=price("4500.00"), ask=price("4500.50"))
        assert pol.buy_compare(snap) == price("4500.50")

    def test_falls_back_to_last(self):
        pol = CFDMarkPricing()
        snap = self._snap(last=price("4500.25"))  # no bid/ask
        assert pol.reference(snap) == price("4500.25")
