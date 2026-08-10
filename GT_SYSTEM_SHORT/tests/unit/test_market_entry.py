"""Unit tests for --market / GT_ENTRY_MARKET (MARKET entry instead of STP-LMT).

SHORT INVERSION of the long-side test. The entry parent normally rests at IBKR
as a SELL STP-LMT and the broker fires it when LTP *falls* to the trigger, with
the limit BELOW the stop (IBKR requires lmtPrice <= stopPrice for a SELL
stop-limit). `--market` is for the case where something upstream has already
detected the breakdown and launched this bot in response: there is no trigger
left to wait for, so the parent goes in as a plain MARKET.

Everything else about the bracket must be identical — same child, same
parentId/transmit handshake — or the protective leg stops being atomic.
"""
import asyncio
import os

import pytest

from src.config.models import Config, OrderSide
from src.execution.broker import Gateway


# --------------------------------------------------------------------------- #
# config plumbing
# --------------------------------------------------------------------------- #

class TestEntryMarketConfig:
    """--market and GT_ENTRY_MARKET reach Config.entry_market."""

    def test_defaults_off(self):
        """Absent any flag, the historical STP-LMT entry is unchanged."""
        assert Config().entry_market is False

    def test_env_var_sets_it(self, monkeypatch):
        monkeypatch.setenv("GT_ENTRY_MARKET", "1")
        assert Config.from_env().entry_market is True

    @pytest.mark.parametrize("value", ["true", "True"])
    def test_env_var_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv("GT_ENTRY_MARKET", value)
        assert Config.from_env().entry_market is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_env_var_falsey_spellings(self, monkeypatch, value):
        """Anything but an explicit yes leaves the safe default in place."""
        monkeypatch.setenv("GT_ENTRY_MARKET", value)
        assert Config.from_env().entry_market is False

    def test_absent_env_var(self, monkeypatch):
        monkeypatch.delenv("GT_ENTRY_MARKET", raising=False)
        assert Config.from_env().entry_market is False

    def test_cli_flag_parses(self):
        from run_live import parse_args
        import sys

        argv = sys.argv
        try:
            sys.argv = ["run_live.py", "AAPL", "--trigger", "128.86", "--market"]
            assert parse_args().market is True
            sys.argv = ["run_live.py", "AAPL", "--trigger", "128.86"]
            assert parse_args().market is False
        finally:
            sys.argv = argv


# --------------------------------------------------------------------------- #
# bracket construction
# --------------------------------------------------------------------------- #

class _Event(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


class _Status:
    status = "PreSubmitted"
    filled = 0
    remaining = 0
    avgFillPrice = 0.0


class _Trade:
    """Stand-in for ib_async.Trade: hands out an event for any *Event lookup,
    so the broker's watcher wiring attaches without being enumerated here."""

    def __init__(self, order):
        self.order = order
        self.orderStatus = _Status()
        self.log = []
        self.fills = []
        self._events = {}

    def __getattr__(self, name):
        if name.endswith("Event"):
            return self._events.setdefault(name, _Event())
        raise AttributeError(name)


class _Client:
    def __init__(self):
        self._next = 500

    def getReqId(self):
        self._next += 1
        return self._next


class _FakeIB:
    """Captures the orders the bracket hands to IBKR."""

    def __init__(self):
        self.client = _Client()
        self.placed = []

    def placeOrder(self, contract, order):
        self.placed.append(order)
        return _Trade(order)


@pytest.fixture
def gateway(monkeypatch):
    # Gateway defines __slots__, so the contract lookup is stubbed on the class
    # rather than the instance.
    async def _contract(self):
        return object()

    monkeypatch.setattr(Gateway, "_get_contract", _contract, raising=True)
    gw = Gateway(port=4002, paper=False, symbol="AAPL")
    gw._ib = _FakeIB()
    return gw


def _place(gw, **overrides):
    kwargs = dict(
        qty=100,
        parent_stop_price=128.86,
        # SHORT: the limit sits BELOW the stop (mirror of the long geometry).
        parent_limit_price=128.73,
        # And the protective cover sits ABOVE the entry.
        child_stop_price=129.18,
        parent_order_id="ENTRY_SELL_n1",
        child_order_id="BR_BUY_n1",
    )
    kwargs.update(overrides)
    asyncio.run(gw.place_bracket_sell_stop_market(**kwargs))
    return gw._ib.placed


class TestBracketParentOrderType:
    """The parent leg swaps type; nothing else about the bracket moves."""

    def test_default_parent_is_stop_limit(self, gateway):
        """Regression guard: the historical path must not change."""
        parent, _child = _place(gateway)
        assert parent.orderType == "STP LMT"
        assert parent.auxPrice == 128.86      # trigger
        assert parent.lmtPrice == 128.73      # floor, below the trigger
        assert parent.lmtPrice <= parent.auxPrice   # IBKR's SELL constraint

    def test_market_parent_is_a_plain_market_order(self, gateway):
        parent, _child = _place(gateway, parent_market=True)
        assert parent.orderType == "MKT"
        assert parent.action == OrderSide.SELL.value
        assert parent.totalQuantity == 100

    def test_market_parent_carries_no_trigger_or_ceiling(self, gateway):
        """A leftover stop/limit on a MKT order is how you get a rejection.

        ib_async spells "not set" as UNSET_DOUBLE, not 0.0 — a literal 0.0
        lmtPrice would be a real (and disastrous) instruction.
        """
        from ib_async.order import UNSET_DOUBLE

        parent, _child = _place(gateway, parent_market=True)
        assert parent.auxPrice == UNSET_DOUBLE
        assert parent.lmtPrice == UNSET_DOUBLE

    def test_market_parent_still_works_outside_rth(self, gateway):
        """Without this IBKR queues an ETH entry until 09:30 (Warning 399)."""
        parent, _child = _place(gateway, parent_market=True)
        assert parent.outsideRth is True

    @pytest.mark.parametrize("market", [False, True])
    def test_bracket_handshake_is_unchanged(self, gateway, market):
        """Parent buffered, child transmits both, child references parent."""
        parent, child = _place(gateway, parent_market=market)
        assert parent.transmit is False
        assert child.transmit is True
        assert child.parentId == parent.orderId

    @pytest.mark.parametrize("market", [False, True])
    def test_protective_child_is_unchanged(self, gateway, market):
        """The protective leg must not care how the entry was placed.

        SHORT INVERSION: the cover is a BUY STP sitting above the entry.
        """
        _parent, child = _place(gateway, parent_market=market)
        assert child.orderType == "STP"
        assert child.action == OrderSide.BUY.value
        assert child.auxPrice == 129.18
        assert child.totalQuantity == 100
        assert child.outsideRth is True

    @pytest.mark.parametrize("market", [False, True])
    def test_engine_ids_still_map(self, gateway, market):
        parent, child = _place(gateway, parent_market=market)
        assert gateway._order_id_map[str(parent.orderId)] == "ENTRY_SELL_n1"
        assert gateway._order_id_map[str(child.orderId)] == "BR_BUY_n1"

    def test_paper_mode_still_takes_the_legacy_path(self):
        """Paper has no parent-id semantics; None tells the engine to fall back."""
        gw = Gateway(port=4002, paper=True, symbol="AAPL")
        result = asyncio.run(gw.place_bracket_sell_stop_market(
            qty=100, parent_stop_price=128.86, parent_limit_price=128.73,
            child_stop_price=129.18, parent_order_id="E", child_order_id="C",
            parent_market=True,
        ))
        assert result is None