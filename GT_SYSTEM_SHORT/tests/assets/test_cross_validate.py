"""Unit tests for SpecRegistry.cross_validate_against_broker.

Approach: mock ib_async's IB to return fabricated ContractDetails so
we can drive every branch (currency mismatch, multiplier mismatch,
tick mismatch, contract not found) without IBKR access.

This is the FOUNDATION of the cross-validation safety net. If these
tests aren't tight, real spec/broker drift will sneak past at engine
startup and cause exactly the 10× multiplier mis-sizing we built
this layer to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.assets import (
    AssetClass, SpecRegistry, SpecMismatchError, resolve,
)
from src.assets.policies.contract import ContractNotFound


# ────────────────────────────────────────────────────────────────────
# Tiny fake IB that returns whatever ContractDetails we feed it
# ────────────────────────────────────────────────────────────────────

@dataclass
class FakeContractDetails:
    """Mimics the slice of ib_async.ContractDetails we read in
    cross_validate. SimpleNamespace would work but dataclass is
    easier to read in tests."""
    contract: object
    minTick: float = 0.01


class FakeIB:
    """Minimal IB stub. cross_validate calls reqContractDetailsAsync;
    we return a pre-seeded list."""

    def __init__(self, contract_details_list):
        self._cds = contract_details_list

    async def reqContractDetailsAsync(self, _contract):
        return self._cds


def _make_broker_contract(currency="USD", multiplier="", min_tick_extra=None):
    """Build a stand-in for ib_async Contract with the attributes
    cross_validate inspects."""
    return SimpleNamespace(currency=currency, multiplier=multiplier)


# ════════════════════════════════════════════════════════════════════
# Happy paths — spec agrees with broker
# ════════════════════════════════════════════════════════════════════

class TestHappyPaths:
    @pytest.mark.asyncio
    async def test_equity_passes_cross_validate(self):
        spec = resolve("PLTR")
        broker = _make_broker_contract(currency="USD")
        cd = FakeContractDetails(contract=broker, minTick=0.01)
        ib = FakeIB([cd])
        # Should complete with no exception
        await SpecRegistry.cross_validate(spec, "PLTR", ib)

    @pytest.mark.asyncio
    async def test_forex_passes_cross_validate(self):
        spec = resolve("EURUSD")
        broker = _make_broker_contract(currency="USD")
        cd = FakeContractDetails(contract=broker, minTick=0.00005)
        ib = FakeIB([cd])
        await SpecRegistry.cross_validate(spec, "EURUSD", ib)

    @pytest.mark.asyncio
    async def test_es_future_passes(self):
        spec = resolve("ES")
        broker = _make_broker_contract(currency="USD", multiplier="50")
        cd = FakeContractDetails(contract=broker, minTick=0.25)
        ib = FakeIB([cd])
        await SpecRegistry.cross_validate(spec, "ES", ib)

    @pytest.mark.asyncio
    async def test_mes_future_passes(self):
        spec = resolve("MES")
        broker = _make_broker_contract(currency="USD", multiplier="5")
        cd = FakeContractDetails(contract=broker, minTick=0.25)
        ib = FakeIB([cd])
        await SpecRegistry.cross_validate(spec, "MES", ib)


# ════════════════════════════════════════════════════════════════════
# Currency mismatches
# ════════════════════════════════════════════════════════════════════

class TestCurrencyMismatch:
    @pytest.mark.asyncio
    async def test_eur_spec_usd_broker(self):
        """USDJPY-style: spec says quote=JPY but broker reports USD."""
        spec = resolve("USDJPY")
        # spec.quote_currency = JPY; broker says USD = mismatch
        broker = _make_broker_contract(currency="USD")
        cd = FakeContractDetails(contract=broker, minTick=0.005)
        ib = FakeIB([cd])
        with pytest.raises(SpecMismatchError) as exc:
            await SpecRegistry.cross_validate(spec, "USDJPY", ib)
        assert exc.value.field == "currency"
        assert exc.value.expected == "JPY"
        assert exc.value.actual == "USD"


# ════════════════════════════════════════════════════════════════════
# Multiplier mismatches — THE BUG CLASS WE'RE PROTECTING AGAINST
# ════════════════════════════════════════════════════════════════════

class TestMultiplierMismatch:
    @pytest.mark.asyncio
    async def test_es_spec_mes_multiplier(self):
        """The classic catastrophe: spec configured for ES ($50
        multiplier) but the broker contract is actually MES ($5).
        Without this check, every order would be 10× under-sized
        and the risk gate blind to half a million dollars of notional.
        """
        spec = resolve("ES")  # spec.sizing.multiplier = 50
        broker = _make_broker_contract(currency="USD", multiplier="5")  # MES
        cd = FakeContractDetails(contract=broker, minTick=0.25)
        ib = FakeIB([cd])
        with pytest.raises(SpecMismatchError) as exc:
            await SpecRegistry.cross_validate(spec, "ES", ib)
        assert exc.value.field == "multiplier"
        assert exc.value.expected == "50"
        assert exc.value.actual == "5"
        assert "MULTIPLIER MISMATCH" in str(exc.value)

    @pytest.mark.asyncio
    async def test_mes_spec_es_multiplier(self):
        """Reverse direction: spec MES ($5) but broker shows ES ($50)
        — orders would be 10× OVER-sized."""
        spec = resolve("MES")
        broker = _make_broker_contract(currency="USD", multiplier="50")
        cd = FakeContractDetails(contract=broker, minTick=0.25)
        ib = FakeIB([cd])
        with pytest.raises(SpecMismatchError) as exc:
            await SpecRegistry.cross_validate(spec, "MES", ib)
        assert exc.value.expected == "5"
        assert exc.value.actual == "50"

    @pytest.mark.asyncio
    async def test_equity_no_multiplier_skipped(self):
        """Equity's sizing has no multiplier attribute — assertion
        helper skips it cleanly."""
        spec = resolve("PLTR")
        broker = _make_broker_contract(currency="USD", multiplier="")
        cd = FakeContractDetails(contract=broker, minTick=0.01)
        ib = FakeIB([cd])
        # No exception — multiplier check skipped for equity
        await SpecRegistry.cross_validate(spec, "PLTR", ib)

    @pytest.mark.asyncio
    async def test_future_with_empty_broker_multiplier_raises(self):
        """If a futures spec gets back a contract WITHOUT a multiplier,
        that's a serious config issue — refuse."""
        spec = resolve("ES")
        broker = _make_broker_contract(currency="USD", multiplier="")
        cd = FakeContractDetails(contract=broker, minTick=0.25)
        ib = FakeIB([cd])
        with pytest.raises(SpecMismatchError) as exc:
            await SpecRegistry.cross_validate(spec, "ES", ib)
        assert exc.value.field == "multiplier"


# ════════════════════════════════════════════════════════════════════
# Tick-size mismatches
# ════════════════════════════════════════════════════════════════════

class TestTickMismatch:
    @pytest.mark.asyncio
    async def test_spec_tick_finer_than_broker_raises(self):
        """Spec says 0.01 grid; broker reports 0.05 minimum. Orders
        rounded to spec grid would be rejected by IBKR."""
        spec = resolve("PLTR")  # spec tick = 0.01
        broker = _make_broker_contract(currency="USD")
        cd = FakeContractDetails(contract=broker, minTick=0.05)  # broker tighter
        ib = FakeIB([cd])
        with pytest.raises(SpecMismatchError) as exc:
            await SpecRegistry.cross_validate(spec, "PLTR", ib)
        assert exc.value.field == "tick_size"
        assert "FINER" in str(exc.value)

    @pytest.mark.asyncio
    async def test_spec_tick_aligned_with_finer_broker_ok(self):
        """If spec is 0.25 and broker is 0.05, 0.25 is a clean multiple
        of 0.05 → spec grid is a strict subset of broker grid → SAFE.
        Should NOT raise."""
        spec = resolve("ES")  # spec tick = 0.25
        broker = _make_broker_contract(currency="USD", multiplier="50")
        cd = FakeContractDetails(contract=broker, minTick=0.05)
        ib = FakeIB([cd])
        # 0.25 / 0.05 = 5, integer → safe
        await SpecRegistry.cross_validate(spec, "ES", ib)

    @pytest.mark.asyncio
    async def test_spec_tick_off_grid_raises(self):
        """Spec 0.10 (gold) vs broker 0.07 → not an integer multiple,
        off-grid orders possible. Raise."""
        spec = resolve("GC")  # spec tick = 0.10
        broker = _make_broker_contract(currency="USD", multiplier="100")
        cd = FakeContractDetails(contract=broker, minTick=0.07)  # weird
        ib = FakeIB([cd])
        with pytest.raises(SpecMismatchError) as exc:
            await SpecRegistry.cross_validate(spec, "GC", ib)
        assert exc.value.field == "tick_size"

    @pytest.mark.asyncio
    async def test_zero_min_tick_skipped(self):
        """If IBKR returns minTick=0 (rare; usually means 'not
        specified'), skip the check rather than false-alarm."""
        spec = resolve("PLTR")
        broker = _make_broker_contract(currency="USD")
        cd = FakeContractDetails(contract=broker, minTick=0)
        ib = FakeIB([cd])
        # No exception — skip silently
        await SpecRegistry.cross_validate(spec, "PLTR", ib)


# ════════════════════════════════════════════════════════════════════
# Contract not found
# ════════════════════════════════════════════════════════════════════

class TestContractNotFound:
    @pytest.mark.asyncio
    async def test_empty_cd_list_raises_contract_not_found(self):
        """IBKR returns no ContractDetails → ContractNotFound, NOT
        SpecMismatchError (different semantics)."""
        spec = resolve("PLTR")
        ib = FakeIB([])  # zero results
        with pytest.raises(ContractNotFound):
            await SpecRegistry.cross_validate(spec, "PLTR", ib)
