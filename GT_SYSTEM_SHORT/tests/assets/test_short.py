"""Tests for ShortPolicy — asset-class short-selling requirements.

Covers the four IBKR short-selling factors (PDF §1–4) as modeled
offline, per asset class:

  1. Reg-T initial margin (equity 50% own equity → 150% collateral)
  2. Maintenance margin (conservative fallback; HTB bump)
  3. Restricted proceeds (equity only)
  4. Borrow / financing / swap carry

Plus the per-asset flags (locate, symmetric) and the ShortRequirement
audit bundle.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.assets import resolve
from src.assets.enum import AssetClass
from src.assets.types import Currency, Money
from src.assets.policies.short import (
    RegTEquityShort, CFDShort, SymmetricShort, ShortRequirement,
)


def _usd(x) -> Money:
    return Money(Decimal(str(x)), Currency.USD)


# ────────────────────────────────────────────────────────────────────
# Reg-T equity short (PDF §1–4)
# ────────────────────────────────────────────────────────────────────

class TestRegTEquityShort:
    def test_initial_margin_is_50_pct(self):
        # PDF §1: 50% own equity on top of 100% proceeds.
        pol = RegTEquityShort()
        assert pol.initial_margin(_usd(10_000)).amount == Decimal("5000.00")

    def test_collateral_multiplier_is_150_pct(self):
        # PDF §1: 100% proceeds + 50% equity = 150% total collateral.
        req = RegTEquityShort().requirement(_usd(10_000))
        assert req.proceeds_collateral.amount == Decimal("10000")
        assert req.initial_margin.amount == Decimal("5000.00")
        assert req.collateral_multiplier == Decimal("1.5")

    def test_maintenance_default_30_pct(self):
        # PDF §2: default large-cap-ish estimate.
        pol = RegTEquityShort()
        assert pol.maintenance_margin(_usd(10_000)).amount == Decimal("3000.00")

    def test_maintenance_hard_to_borrow_bumps_to_100_pct(self):
        pol = RegTEquityShort()
        htb = pol.maintenance_margin(_usd(10_000), hard_to_borrow=True)
        assert htb.amount == Decimal("10000.00")
        # And the requirement carries the flag through.
        req = pol.requirement(_usd(10_000), hard_to_borrow=True)
        assert req.hard_to_borrow is True
        assert req.maintenance_margin.amount == Decimal("10000.00")

    def test_proceeds_are_restricted(self):
        # PDF §3.
        req = RegTEquityShort().requirement(_usd(10_000))
        assert req.proceeds_restricted is True
        assert req.requires_locate is True
        assert req.is_symmetric is False

    def test_borrow_fee_uses_360_day_year(self):
        # PDF §4: daily borrow = notional × annual_rate / 360.
        pol = RegTEquityShort()
        # 36% annual on 10k → 100/day.
        carry = pol.daily_carry(_usd(10_000), annual_rate=Decimal("0.36"))
        assert carry.amount == Decimal("10.00")

    def test_default_borrow_rate_applied_when_no_live_rate(self):
        pol = RegTEquityShort()
        req = pol.requirement(_usd(10_000))
        # 25 bps/yr default → 10000 * 0.0025 / 360.
        assert req.carry_rate_annual == Decimal("0.0025")
        assert req.daily_carry.amount == Decimal("10000") * Decimal("0.0025") / Decimal("360")

    def test_not_authoritative(self):
        # Offline policy is never authoritative — only IBKR whatIf is.
        assert RegTEquityShort().requirement(_usd(10_000)).authoritative is False

    def test_currency_preserved(self):
        eur = Money(Decimal("10000"), Currency.EUR)
        req = RegTEquityShort().requirement(eur)
        assert req.initial_margin.currency is Currency.EUR
        assert req.proceeds_collateral.currency is Currency.EUR


# ────────────────────────────────────────────────────────────────────
# CFD short — synthetic
# ────────────────────────────────────────────────────────────────────

class TestCFDShort:
    def test_margin_is_leverage_pct(self):
        pol = CFDShort(margin_rate=Decimal("0.20"))
        assert pol.initial_margin(_usd(10_000)).amount == Decimal("2000.00")
        # Same to open and maintain.
        assert pol.maintenance_margin(_usd(10_000)).amount == Decimal("2000.00")

    def test_no_proceeds_no_locate(self):
        req = CFDShort().requirement(_usd(10_000))
        assert req.proceeds_restricted is False
        assert req.requires_locate is False
        assert req.proceeds_collateral.amount == Decimal("0")

    def test_financing_carry(self):
        pol = CFDShort(default_financing_rate_annual=Decimal("0.036"))
        assert pol.daily_carry(_usd(10_000)).amount == Decimal("1.00")


# ────────────────────────────────────────────────────────────────────
# Symmetric short — FX / futures
# ────────────────────────────────────────────────────────────────────

class TestSymmetricShort:
    def test_fx_symmetric_no_borrow(self):
        req = SymmetricShort.for_fx().requirement(_usd(10_000))
        assert req.is_symmetric is True
        assert req.requires_locate is False
        assert req.proceeds_restricted is False
        assert req.proceeds_collateral.amount == Decimal("0")

    def test_fx_margin_default_3_pct(self):
        assert SymmetricShort.for_fx().initial_margin(_usd(10_000)).amount == Decimal("300.00")

    def test_futures_margin_source_is_span(self):
        pol = SymmetricShort.for_futures()
        assert pol.margin_source == "SPAN"
        assert pol.is_symmetric is True

    def test_carry_magnitude_only(self):
        # Swap can be a credit; policy reports magnitude (abs rate).
        pol = SymmetricShort.for_fx()
        carry = pol.daily_carry(_usd(10_000), annual_rate=Decimal("-0.036"))
        assert carry.amount == Decimal("1.00")


# ────────────────────────────────────────────────────────────────────
# ShortRequirement bundle
# ────────────────────────────────────────────────────────────────────

class TestShortRequirement:
    def test_to_audit_dict_shape(self):
        d = RegTEquityShort().requirement(_usd(10_000)).to_audit_dict()
        for key in (
            "notional", "currency", "initial_margin", "maintenance_margin",
            "proceeds_collateral", "daily_carry", "collateral_multiplier",
            "proceeds_restricted", "requires_locate", "hard_to_borrow",
            "is_symmetric", "authoritative", "carry_rate_annual", "notes",
        ):
            assert key in d
        assert d["currency"] == "USD"
        assert Decimal(d["collateral_multiplier"]) == Decimal("1.5")

    def test_zero_notional_multiplier_safe(self):
        # No divide-by-zero on an empty notional.
        req = RegTEquityShort().requirement(_usd(0))
        assert req.collateral_multiplier == Decimal("0")


# ────────────────────────────────────────────────────────────────────
# Wiring: every asset spec ships a ShortPolicy
# ────────────────────────────────────────────────────────────────────

class TestSpecWiring:
    def test_equity_spec_uses_regt(self):
        spec = resolve("PLTR")
        assert isinstance(spec.short, RegTEquityShort)
        assert spec.to_audit_dict()["policies"]["short"] == "RegTEquityShort"
        assert "short=RegTEquityShort" in spec.describe()

    def test_fx_spec_uses_symmetric(self):
        spec = resolve("EURUSD")
        assert isinstance(spec.short, SymmetricShort)
        assert spec.short.is_symmetric is True

    def test_futures_spec_uses_symmetric_span(self):
        spec = resolve("ES")
        assert isinstance(spec.short, SymmetricShort)
        assert spec.short.margin_source == "SPAN"

    def test_index_cfd_uses_cfd_short(self):
        spec = resolve("IBUS500")
        assert isinstance(spec.short, CFDShort)

    def test_share_cfd_uses_cfd_short(self):
        spec = resolve("AAPL", AssetClass.SHARE_CFD)
        assert isinstance(spec.short, CFDShort)

    def test_notional_flows_into_requirement(self):
        # End-to-end: sizing.notional → short.requirement, equity 100@150.
        from src.assets.types import shares, price as _price
        spec = resolve("PLTR")
        notional = spec.sizing.notional(shares(100), _price("150"))
        req = spec.short.requirement(notional)
        assert req.notional.amount == Decimal("15000")
        assert req.initial_margin.amount == Decimal("7500.00")
        assert req.collateral_multiplier == Decimal("1.5")
