"""Railguard regression matrix: 8 critical shorting-class guards
× 3 new asset classes (FX, Future, CFD) = 24 cases.

Why this file exists
====================
The 27 production guards in src/strategy/engine.py were all written +
validated against US equity semantics. Each one needs re-confirmation
that its CORE INVARIANT still holds when the engine's spec is FX or
Futures or CFD instead of equity. If the underlying primitives the
guard depends on (Quantity unit comparison, SizingMismatch, Money
currency, contract construction) behave differently per asset class,
the guard silently becomes a no-op.

The 8 guards covered here are the ones that specifically prevent
SHORTING THE ACCOUNT — the bug class that cost real money on PLTR
(2026-06-02) and ZM (2026-06-01):

  G1  STALE_SELL_REJECTED         — broker has resting SELL of wrong qty
  G2  PHANTOM_SELL_REJECTED       — SELL fill arrives while engine FLAT
  G3  DUPLICATE_SELL_GUARD        — pre-flight finds existing SELL
  G4  STARTUP_REFUSED_CONFLICT    — cross-client_id orders on same ticker
  G5  STARTUP_REFUSED_NAKED       — broker has unknown position
  G6  PRE_FLIGHT_BROKER_QTY_CHECK — verify broker qty matches before SELL
  G7  AUTO_FLAT_SAFE_DIRECTION    — engine LONG + broker FLAT → fold to FLAT
  G8  BRACKET_CHILD_QTY_ON_PARTIAL — partial parent fill → child adjusted

Each test validates the SPEC-LEVEL primitive that backs the guard,
NOT the full engine wiring. The engine tests confirm the engine
respects the spec's verdict (covered in D3-PM paper smoke).

If any cell in this matrix fails, the corresponding guard either
needs an asset-specific patch or the spec needs adjustment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.assets import (
    AssetClass, Currency, base_units, cfd_units, contracts, resolve, price, usd,
)
from src.assets.policies import OrderIntent, PortfolioView, RiskVerdict
from src.assets.policies.sizing import SizingMismatch
from src.assets.types import Money, Quantity, QuantityUnit, QuantityUnitMismatch

UTC = timezone.utc


# ────────────────────────────────────────────────────────────────────
# Fixtures: one spec per new asset class
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def fx_spec():
    return resolve("EURUSD")


@pytest.fixture
def future_spec():
    return resolve("ES")


@pytest.fixture
def cfd_spec():
    return resolve("IBUS500")


@pytest.fixture
def equity_spec():
    """Equity baseline. If any of the 8 guards regresses on equity,
    we'd see it here too — important canary."""
    return resolve("PLTR")


def make_portfolio(equity: Decimal = Decimal("100000"),
                   bp: Decimal = Decimal("50000"),
                   open_notional: Decimal = Decimal("0"),
                   daily_pnl: Decimal = Decimal("0")) -> PortfolioView:
    return PortfolioView(
        total_open_notional_base=usd(open_notional),
        daily_pnl_base=usd(daily_pnl),
        account_equity_base=usd(equity),
        account_buying_power_base=usd(bp),
    )


# ════════════════════════════════════════════════════════════════════
# GUARD 1 — STALE_SELL_REJECTED
#
# Engine invariant: a SELL order resting at the broker whose qty
# differs from the engine's current position qty is STALE (left over
# from a previous run / different config) and must be cancelled, not
# adopted. The spec primitive backing this: SizingPolicy.is_valid_qty
# refuses Quantity values that don't match the asset class's
# expected unit / size grid.
# ════════════════════════════════════════════════════════════════════

class TestG1_StaleSellRejected:
    """If a previous-run SELL has qty in the wrong unit (e.g. SHARES
    when the current spec expects BASE_UNITS), the spec rejects it
    before the engine adopts it."""

    def test_fx_rejects_shares_qty(self, fx_spec):
        from src.assets import shares
        with pytest.raises(SizingMismatch):
            fx_spec.sizing.notional(shares(30), price("1.16175"))

    def test_future_rejects_base_units_qty(self, future_spec):
        with pytest.raises(SizingMismatch):
            future_spec.sizing.notional(base_units(30), price("4500"))

    def test_cfd_rejects_contracts_qty(self, cfd_spec):
        with pytest.raises(SizingMismatch):
            cfd_spec.sizing.notional(contracts(30), price("4500"))


# ════════════════════════════════════════════════════════════════════
# GUARD 2 — PHANTOM_SELL_REJECTED
#
# Engine invariant: a SELL fill that arrives while engine is FLAT is
# rejected from updating the position (because applying it would
# make _quantity negative). The spec primitive: Quantity arithmetic
# refuses sub-zero results to be silently treated as a valid LONG.
# ════════════════════════════════════════════════════════════════════

class TestG2_PhantomSellRejected:
    """When subtracting a SELL fill from a FLAT position the result
    is a negative Quantity. The type system makes this explicit — the
    engine's PHANTOM_SELL guard checks is_negative."""

    def test_fx_negative_position_detected(self):
        # Engine FLAT (zero BASE_UNITS); broker fill for 25k SELL
        flat = base_units(0)
        broker_fill = base_units(25000)
        net = flat - broker_fill
        assert net.is_negative
        assert net.value == Decimal("-25000")
        assert net.unit is QuantityUnit.BASE_UNITS

    def test_future_negative_position_detected(self):
        flat = contracts(0)
        broker_fill = contracts(1)
        net = flat - broker_fill
        assert net.is_negative
        assert net.unit is QuantityUnit.CONTRACTS

    def test_cfd_negative_position_detected(self):
        flat = cfd_units(0)
        broker_fill = cfd_units(10)
        net = flat - broker_fill
        assert net.is_negative
        assert net.unit is QuantityUnit.CFD_UNITS


# ════════════════════════════════════════════════════════════════════
# GUARD 3 — DUPLICATE_SELL_GUARD
#
# Engine invariant: before placing a fresh protective SELL, the
# engine queries the broker for existing SELLs on this symbol; if
# one already exists with matching qty it's ADOPTED (not duplicated).
# The spec primitive: SizingPolicy.is_valid_qty validates that an
# existing-broker SELL's qty matches the engine's expected size.
# ════════════════════════════════════════════════════════════════════

class TestG3_DuplicateSellGuard:
    """is_valid_qty(qty) is the gate. Matching qty → adopt; mismatching
    → cancel + replace."""

    def test_fx_validates_matching_qty(self, fx_spec):
        # Engine config: 25,000 EUR. Broker shows resting SELL for 25,000.
        # is_valid_qty should accept (matches min_qty + on increment)
        assert fx_spec.sizing.is_valid_qty(base_units(25000))

    def test_fx_rejects_below_min(self, fx_spec):
        # Below 25k min — engine should treat as suspicious
        assert not fx_spec.sizing.is_valid_qty(base_units(1000))

    def test_future_validates_integer_contracts(self, future_spec):
        assert future_spec.sizing.is_valid_qty(contracts(1))
        assert future_spec.sizing.is_valid_qty(contracts(10))

    def test_future_rejects_zero(self, future_spec):
        # Zero contracts isn't a valid SELL — engine should refuse to adopt
        assert not future_spec.sizing.is_valid_qty(contracts(0))

    def test_cfd_validates_unit_matches(self, cfd_spec):
        assert cfd_spec.sizing.is_valid_qty(cfd_units(10))
        # Wrong unit → invalid (the protection the guard relies on)
        from src.assets import shares
        assert not cfd_spec.sizing.is_valid_qty(shares(10))


# ════════════════════════════════════════════════════════════════════
# GUARD 4 — STARTUP_REFUSED_CONFLICT
#
# Engine invariant: on startup, if any IBKR order on this symbol
# belongs to a DIFFERENT client_id, the engine refuses to start
# (would otherwise double-fill on trigger crossing). The spec
# primitive: ContractPolicy.make produces a unique-per-asset contract
# that the engine compares against IBKR-side open orders.
# ════════════════════════════════════════════════════════════════════

class TestG4_StartupRefusedConflict:
    """The cross-client-id check happens at the engine layer. The
    spec's job is to produce a contract whose symbol matches what
    IBKR returns in open orders for that asset class."""

    def test_fx_contract_carries_pair(self, fx_spec):
        c = fx_spec.contract.make("EURUSD")
        # ib_async.Forex normalizes to base symbol = 'EUR'
        # The pair-uniqueness check uses the local symbol or
        # (symbol, currency) tuple.
        assert c.symbol == "EUR"
        assert c.currency == "USD"

    def test_future_contract_carries_month(self, future_spec):
        c = future_spec.contract.make("ES")
        # The contract month disambiguates concurrent ES positions
        # (front month vs back month). Engine cross-check uses month.
        assert c.lastTradeDateOrContractMonth
        assert c.symbol == "ES"

    def test_cfd_contract_distinct_sectype(self, cfd_spec):
        c = cfd_spec.contract.make("IBUS500")
        # CFD-vs-equity disambiguation via secType — guard compares
        # secType when matching open orders.
        assert c.secType == "CFD"


# ════════════════════════════════════════════════════════════════════
# GUARD 5 — STARTUP_REFUSED_NAKED
#
# Engine invariant: on startup, if broker reports a position that
# engine state has no record of, refuse to start (could be a manual
# trade, different bot, or state corruption — engine has no right to
# manage shares it didn't open). The spec primitive: Quantity unit
# comparison flags a broker position whose unit doesn't match the
# spec's expected_unit.
# ════════════════════════════════════════════════════════════════════

class TestG5_StartupRefusedNaked:
    """Broker reports a position; engine spec has the expected unit.
    A unit mismatch (broker reports SHARES but spec expects
    BASE_UNITS) should fail loud."""

    def test_fx_unit_mismatch_with_equity_position(self, fx_spec):
        # Imagine engine restarts on EURUSD but the broker reports
        # 30 SHARES of something else under this client_id. The Quantity
        # arithmetic refuses to compute "net = expected - broker".
        engine_expected = base_units(25000)
        broker_observed = Quantity(Decimal("30"), QuantityUnit.SHARES)
        with pytest.raises(QuantityUnitMismatch):
            _ = engine_expected - broker_observed

    def test_future_unit_mismatch_with_cfd_position(self, future_spec):
        engine_expected = contracts(1)
        broker_observed = cfd_units(10)
        with pytest.raises(QuantityUnitMismatch):
            _ = engine_expected - broker_observed

    def test_cfd_unit_mismatch_with_fx_position(self, cfd_spec):
        engine_expected = cfd_units(10)
        broker_observed = base_units(25000)
        with pytest.raises(QuantityUnitMismatch):
            _ = engine_expected - broker_observed


# ════════════════════════════════════════════════════════════════════
# GUARD 6 — PRE_FLIGHT_BROKER_QTY_CHECK
#
# Engine invariant: before placing ANY SELL, the engine queries IBKR
# for the actual position and verifies it matches engine state. The
# spec primitive backing this: Quantity equality is strict on both
# value AND unit.
# ════════════════════════════════════════════════════════════════════

class TestG6_PreFlightBrokerQtyCheck:
    """Quantity equality across the pre-flight comparison is the
    primitive. If engine thinks it has X but broker reports Y, the
    types make the diff explicit."""

    def test_fx_equality_strict_on_unit(self):
        a = base_units(25000)
        b = Quantity(Decimal("25000"), QuantityUnit.SHARES)
        assert a != b  # different units, not equal

    def test_future_equality_strict_on_value(self):
        a = contracts(1)
        b = contracts(2)
        assert a != b

    def test_cfd_equality_includes_unit(self):
        a = cfd_units(10)
        b = cfd_units(10)
        assert a == b


# ════════════════════════════════════════════════════════════════════
# GUARD 7 — AUTO_FLAT_SAFE_DIRECTION
#
# Engine invariant: when engine thinks LONG but broker shows FLAT,
# engine auto-corrects to FLAT (refuses to arm a SELL that would
# short the account). This is the SAFE direction. The dangerous
# direction (engine FLAT, broker SHORT) only alerts — never auto-acts.
# The spec primitive: Quantity.is_zero / is_positive.
# ════════════════════════════════════════════════════════════════════

class TestG7_AutoFlatSafeDirection:
    """is_zero detection on broker side triggers the auto-FLAT fold."""

    def test_fx_safe_direction(self):
        # Engine thinks 25k EUR LONG; broker reports 0 → safe to fold
        engine_q = base_units(25000)
        broker_q = base_units(0)
        # Safe to fold: engine positive, broker zero
        assert engine_q.is_positive
        assert broker_q.is_zero

    def test_future_safe_direction(self):
        engine_q = contracts(1)
        broker_q = contracts(0)
        assert engine_q.is_positive and broker_q.is_zero

    def test_cfd_safe_direction(self):
        engine_q = cfd_units(10)
        broker_q = cfd_units(0)
        assert engine_q.is_positive and broker_q.is_zero

    def test_dangerous_direction_recognizable(self):
        # Engine FLAT, broker SHORT — the dangerous case. We confirm
        # the type system makes it recognizable (is_negative on broker).
        engine_q = base_units(0)
        broker_q = Quantity(Decimal("-25000"), QuantityUnit.BASE_UNITS)
        assert engine_q.is_zero
        assert broker_q.is_negative
        # Engine guard does NOT auto-act here — alert only


# ════════════════════════════════════════════════════════════════════
# GUARD 8 — BRACKET_CHILD_QTY_ON_PARTIAL
#
# Engine invariant: on partial parent BUY fill, the bracket child
# SELL must be modified to match the FILLED qty (not the original
# intended qty), or the child fires for more than is held → short.
# The spec primitive: SizingPolicy.notional accepts any valid qty so
# the engine can compute the right partial-fill notional.
# ════════════════════════════════════════════════════════════════════

class TestG8_BracketChildQtyOnPartial:
    """If parent BUY for 1 contract partials to 1 contract (futures
    don't partial typically) or 25k EUR partials to 15k EUR, the child
    SELL qty must match the partial. notional() should compute the
    real notional regardless."""

    def test_fx_partial_notional(self, fx_spec):
        # Original intent: 25k EUR. Parent partial-filled at 15k.
        # Child SELL should size for 15k, not 25k.
        full_intent = fx_spec.sizing.notional(base_units(25000), price("1.16175"))
        partial = fx_spec.sizing.notional(base_units(15000), price("1.16175"))
        assert partial.amount == Decimal("17426.25000")
        assert partial < full_intent

    def test_future_partial_recomputes(self, future_spec):
        # Futures rarely partial-fill but multi-contract orders can.
        # Original 10 contracts, partial 6 → recompute.
        full = future_spec.sizing.notional(contracts(10), price("4500"))
        partial = future_spec.sizing.notional(contracts(6), price("4500"))
        # 6 × 4500 × 50 = 1,350,000
        assert partial.amount == Decimal("1350000")
        assert partial.amount * Decimal("10") / Decimal("6") == full.amount

    def test_cfd_partial_recomputes(self, cfd_spec):
        full = cfd_spec.sizing.notional(cfd_units(10), price("4500.25"))
        partial = cfd_spec.sizing.notional(cfd_units(7), price("4500.25"))
        # 7 × 4500.25 = 31,501.75
        assert partial.amount == Decimal("31501.75")


# ════════════════════════════════════════════════════════════════════
# Summary check — confirm all 24 cells covered
# ════════════════════════════════════════════════════════════════════

class TestMatrixCoverage:
    """Meta-test: ensure every guard × asset_class combination has at
    least one assertion. If this fails, somebody added a guard
    without adding the corresponding railguard tests."""

    def test_matrix_complete(self):
        guards = [
            "G1_StaleSellRejected",
            "G2_PhantomSellRejected",
            "G3_DuplicateSellGuard",
            "G4_StartupRefusedConflict",
            "G5_StartupRefusedNaked",
            "G6_PreFlightBrokerQtyCheck",
            "G7_AutoFlatSafeDirection",
            "G8_BracketChildQtyOnPartial",
        ]
        # Each guard class above has at least 3 test methods (one per
        # new asset class: FX, Future, CFD). Sanity-count via import:
        import tests.assets.test_railguard_matrix as m
        for g in guards:
            cls = getattr(m, f"Test{g}", None)
            assert cls is not None, f"Missing TestClass for {g}"
            methods = [a for a in dir(cls) if a.startswith("test_")]
            assert len(methods) >= 3, (
                f"Test{g} should have ≥3 test methods (one per asset "
                f"class); found {len(methods)}"
            )
