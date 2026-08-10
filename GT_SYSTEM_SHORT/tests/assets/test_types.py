"""Unit tests for src/assets/types.py — the strong-types layer.

These tests are the SAFETY NET for the whole AssetSpec system. If
Quantity unit mismatch doesn't raise, or Money currency mismatch
silently coerces, every higher-level guard is built on sand.

Coverage focus:
  * Arithmetic across mismatched units/currencies RAISES (not silent)
  * Decimal precision preserved through chained operations
  * Float→Decimal coercion goes via str() (no binary-float drift)
  * Convenience constructors produce correct units
  * to() FX conversion uses the service, returns Money in target ccy
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.assets.currency_service import CurrencyService
from src.assets.types import (
    Currency, DEFAULT_BASE_CURRENCY,
    Quantity, QuantityUnit, QuantityUnitMismatch,
    Money, CurrencyMismatch,
    Price,
    shares, contracts, base_units, usd, money, price,
)


# ────────────────────────────────────────────────────────────────────
# Quantity
# ────────────────────────────────────────────────────────────────────

class TestQuantityArithmetic:
    def test_same_unit_addition(self):
        q = shares(30) + shares(70)
        assert q == Quantity(Decimal("100"), QuantityUnit.SHARES)

    def test_same_unit_subtraction(self):
        q = shares(100) - shares(30)
        assert q == Quantity(Decimal("70"), QuantityUnit.SHARES)

    def test_cross_unit_addition_raises(self):
        with pytest.raises(QuantityUnitMismatch) as exc:
            _ = shares(30) + contracts(1)
        assert exc.value.left is QuantityUnit.SHARES
        assert exc.value.right is QuantityUnit.CONTRACTS
        assert exc.value.op == "+"

    def test_cross_unit_subtraction_raises(self):
        with pytest.raises(QuantityUnitMismatch):
            _ = shares(100) - base_units(25000)

    def test_scalar_multiplication(self):
        q = shares(30) * Decimal("2")
        assert q == shares(60)

    def test_int_scalar_multiplication(self):
        q = shares(30) * 2
        assert q == shares(60)

    def test_quantity_times_quantity_raises(self):
        with pytest.raises(QuantityUnitMismatch):
            _ = shares(30) * shares(2)

    def test_negation(self):
        assert -shares(30) == Quantity(Decimal("-30"), QuantityUnit.SHARES)

    def test_abs(self):
        assert abs(Quantity(Decimal("-30"), QuantityUnit.SHARES)) == shares(30)

    def test_comparison_same_unit(self):
        assert shares(30) < shares(50)
        assert shares(50) > shares(30)
        assert shares(30) <= shares(30)
        assert shares(30) >= shares(30)

    def test_comparison_cross_unit_raises(self):
        with pytest.raises(QuantityUnitMismatch):
            _ = shares(30) < contracts(1)


class TestQuantityConstruction:
    def test_float_input_rejected(self):
        # We refuse float at the constructor — caller should be
        # explicit about Decimal to avoid binary-float surprises.
        with pytest.raises(TypeError):
            Quantity(30.5, QuantityUnit.SHARES)

    def test_convenience_shares_int(self):
        q = shares(30)
        assert q.value == Decimal("30")
        assert q.unit is QuantityUnit.SHARES

    def test_convenience_shares_string(self):
        q = shares("30.5")
        assert q.value == Decimal("30.5")

    def test_contracts_must_be_integer(self):
        # Futures contracts can't be fractional
        with pytest.raises(ValueError):
            contracts("1.5")

    def test_base_units_can_be_fractional(self):
        # FX position sizes may be fractional (rare but legal)
        q = base_units("25000.50")
        assert q.value == Decimal("25000.50")
        assert q.unit is QuantityUnit.BASE_UNITS


class TestQuantityConversions:
    def test_to_int_when_integer(self):
        assert shares(30).to_int() == 30

    def test_to_int_raises_when_fractional(self):
        with pytest.raises(ValueError):
            shares("30.5").to_int()

    def test_to_float_works(self):
        assert base_units("25000.5").to_float() == 25000.5

    def test_predicates(self):
        assert shares(30).is_positive
        assert shares(0).is_zero
        assert (-shares(30)).is_negative


# ────────────────────────────────────────────────────────────────────
# Money
# ────────────────────────────────────────────────────────────────────

class TestMoneyArithmetic:
    def test_same_currency_addition(self):
        m = usd(100) + usd(50)
        assert m == Money(Decimal("150"), Currency.USD)

    def test_same_currency_subtraction(self):
        m = usd(100) - usd(30)
        assert m == Money(Decimal("70"), Currency.USD)

    def test_cross_currency_addition_raises(self):
        with pytest.raises(CurrencyMismatch) as exc:
            _ = usd(100) + money(50, Currency.EUR)
        assert exc.value.left is Currency.USD
        assert exc.value.right is Currency.EUR

    def test_cross_currency_subtraction_raises(self):
        with pytest.raises(CurrencyMismatch):
            _ = usd(100) - money(50, Currency.JPY)

    def test_scalar_multiplication(self):
        m = usd(100) * Decimal("1.5")
        assert m == usd("150.0")

    def test_money_times_money_raises(self):
        with pytest.raises(CurrencyMismatch):
            _ = usd(100) * usd(2)

    def test_negation(self):
        assert -usd(100) == Money(Decimal("-100"), Currency.USD)

    def test_abs(self):
        assert abs(Money(Decimal("-100"), Currency.USD)) == usd(100)

    def test_comparison_same_currency(self):
        assert usd(100) < usd(200)
        assert usd(200) > usd(100)
        assert usd(100) <= usd(100)

    def test_comparison_cross_currency_raises(self):
        with pytest.raises(CurrencyMismatch):
            _ = usd(100) < money(50, Currency.EUR)


class TestMoneyConstruction:
    def test_float_input_via_str(self):
        # The convenience constructor coerces float→Decimal via str()
        m = usd(151.71)
        assert m.amount == Decimal("151.71")
        # crucially NOT Decimal('151.71000000000000085...')

    def test_string_input_exact(self):
        m = usd("151.71")
        assert m.amount == Decimal("151.71")

    def test_decimal_input_passthrough(self):
        m = usd(Decimal("151.71"))
        assert m.amount == Decimal("151.71")

    def test_raw_money_constructor_rejects_float(self):
        with pytest.raises(TypeError):
            Money(151.71, Currency.USD)


class TestMoneyConversion:
    def test_same_currency_noop(self):
        m = usd(100)
        fx = CurrencyService(base=Currency.USD)
        # No service call needed for same-currency
        assert m.to(Currency.USD, fx) == m

    def test_cross_currency_uses_service(self):
        fx = CurrencyService(base=Currency.USD)
        fx.update(Currency.EUR, Currency.USD, Decimal("1.16175"))
        m_eur = money(100, Currency.EUR)
        m_usd = m_eur.to(Currency.USD, fx)
        assert m_usd == Money(Decimal("116.175"), Currency.USD)

    def test_cross_currency_inverse_lookup(self):
        # If we have EUR→USD cached, USD→EUR should compute as 1/rate
        fx = CurrencyService(base=Currency.USD)
        fx.update(Currency.EUR, Currency.USD, Decimal("1.16175"))
        m_usd = usd(116.175)
        m_eur = m_usd.to(Currency.EUR, fx)
        # 116.175 / 1.16175 = 100.0
        assert m_eur.currency is Currency.EUR
        assert m_eur.amount == (Decimal("116.175") / Decimal("1.16175"))


# ────────────────────────────────────────────────────────────────────
# Price
# ────────────────────────────────────────────────────────────────────

class TestPrice:
    def test_string_construction_exact(self):
        p = price("151.71")
        assert p == Decimal("151.71")

    def test_float_construction_via_str(self):
        # Goes through str() to avoid float drift
        p = price(151.71)
        assert p == Decimal("151.71")  # not Decimal('151.7099999...')

    def test_int_construction(self):
        p = price(151)
        assert p == Decimal("151")

    def test_arithmetic_compatible_with_decimal(self):
        # Price is a Decimal subclass — arithmetic should Just Work
        p = price("151.71")
        result = p * Decimal("30")
        assert result == Decimal("4551.30")


# ────────────────────────────────────────────────────────────────────
# Sanity checks
# ────────────────────────────────────────────────────────────────────

class TestSanity:
    def test_default_base_currency_is_usd(self):
        assert DEFAULT_BASE_CURRENCY is Currency.USD

    def test_quantity_repr_includes_unit(self):
        assert "SHARES" in repr(shares(30))

    def test_money_repr_includes_currency(self):
        assert "USD" in repr(usd(100))

    def test_currency_repr_explicit(self):
        assert repr(Currency.USD) == "Currency.USD"

    def test_pltr_minus_30_scenario(self):
        """The PLTR -30 shorting bug in narrative form.

        Engine thinks it has 30 PLTR LONG. Broker fills a stale SELL
        for 100 shares. Difference = -70. The type system should
        make it impossible to silently treat this as a valid LONG.
        """
        engine_long = shares(30)
        broker_sell = shares(100)
        net = engine_long - broker_sell
        # The math gives -70, which is a SHORT. The negative sign is
        # explicit; downstream code must check is_negative and refuse
        # to treat it as a fresh long.
        assert net.is_negative
        assert net.value == Decimal("-70")

    def test_eurusd_plus_pltr_currency_safety(self):
        """Mixed-currency portfolio: 25000 EUR EURUSD notional +
        $4551.30 PLTR notional must NOT silently sum."""
        eurusd_notional = money(25000, Currency.EUR)
        pltr_notional = usd(4551.30)
        with pytest.raises(CurrencyMismatch):
            _ = eurusd_notional + pltr_notional
        # The correct usage: convert one side via the FX service.
        fx = CurrencyService(base=Currency.USD)
        fx.update(Currency.EUR, Currency.USD, Decimal("1.16175"))
        combined = eurusd_notional.to(Currency.USD, fx) + pltr_notional
        assert combined.currency is Currency.USD
        # 25000 * 1.16175 + 4551.30 = 29043.75 + 4551.30 = 33595.05
        assert combined.amount == Decimal("33595.05")
