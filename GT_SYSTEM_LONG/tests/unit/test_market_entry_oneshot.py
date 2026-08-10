"""--market must apply to the LAUNCH entry only, never to a re-entry.

`_place_entry_stop_limit` serves three callers:

  1. engine start        — the entry the upstream signal launched us for
  2. re-entry after exit — `_on_gateway_fill` schedules it at the previous
                           breakout level
  3. session open        — the health/session loop re-arms a resting entry

Only (1) may go in at market. (2) and (3) target a level price has NOT reached
yet; firing them at market would buy instantly at the depressed post-exit ask —
exactly the regression the re-entry comment in `_on_gateway_fill` records
("Previously this was a plain LIMIT, which filled instantly at the depressed
post-exit ask"). The one-shot flag is the guard against re-introducing it.
"""
import asyncio

import pytest

from src.config.models import Config, ConnectionStatus, OrderType
from src.execution.broker import Gateway
from src.strategy.engine import Engine


class _Recorder(list):
    """Captures what the bracket was asked to place, and nothing else."""

    @property
    def parent_markets(self):
        return [call["parent_market"] for call in self]


def _build(monkeypatch, **config_overrides):
    """An Engine whose bracket call is recorded instead of sent.

    Gateway and Engine both define __slots__, so the stubs go on the classes.
    """
    recorder = _Recorder()

    async def _capture(self, **kwargs):
        recorder.append(kwargs)
        return ("parent-broker-id", "child-broker-id")

    monkeypatch.setattr(Gateway, "place_bracket_buy_stop_market", _capture,
                        raising=True)
    monkeypatch.setattr(Engine, "_save_state", lambda self, *a, **k: None,
                        raising=True)

    settings = dict(ticker="AAPL", trigger_price=128.86, quantity=100,
                    stop_loss_pct=0.0025, paper_trading=False)
    settings.update(config_overrides)
    gateway = Gateway(port=4002, paper=False, symbol="AAPL")
    # The engine refuses to place while the socket is down (correctly), so
    # present it as connected without standing up a real one.
    gateway._status = ConnectionStatus.CONNECTED
    eng = Engine(config=Config(**settings), gateway=gateway)
    eng._running = True
    return eng, recorder


@pytest.fixture
def engine(monkeypatch):
    eng, recorder = _build(monkeypatch, entry_market=True)
    return eng, recorder


def _enter(eng, trigger=128.86):
    """Place one entry, clearing the per-cycle claims the way a real cycle would."""
    eng._pending_stop = None
    eng._bracket_child = None
    eng._entry_placing = False
    return asyncio.run(eng._place_entry_stop_limit(trigger))


class TestMarketEntryIsOneShot:

    def test_first_entry_goes_in_at_market(self, engine):
        eng, recorder = engine
        _enter(eng)
        assert recorder.parent_markets == [True]

    def test_second_entry_rests_a_stop_limit(self, engine):
        """The re-entry after an exit must NOT be a market order."""
        eng, recorder = engine
        _enter(eng)
        _enter(eng, trigger=131.40)          # re-entry at the breakout level
        assert recorder.parent_markets == [True, False]

    def test_every_later_entry_stays_a_stop_limit(self, engine):
        eng, recorder = engine
        for trigger in (128.86, 131.40, 133.10, 135.00):
            _enter(eng, trigger)
        assert recorder.parent_markets == [True, False, False, False]

    def test_the_flag_starts_unused(self, engine):
        eng, _ = engine
        assert eng._market_entry_used is False

    def test_the_flag_is_consumed(self, engine):
        eng, _ = engine
        _enter(eng)
        assert eng._market_entry_used is True

    def test_order_record_type_matches(self, engine):
        """The registry must not claim STOP_LIMIT for a market entry."""
        eng, _ = engine
        order_id = _enter(eng)
        assert eng.registry.get(order_id).order_type is OrderType.MARKET


class TestSizingCarriesIntoTheReEntry:
    """Initial entry is MARKET; the re-entry is a LIMIT at the same size.

    The operator sets --qty and --offset-entry-pct once, on the launch command.
    Those must still govern the re-entry, which is a resting STP-LMT — if the
    offset were dropped from the launch command, the re-entry would silently
    fall back to GT's built-in default instead.
    """

    def test_quantity_is_identical_on_both_entries(self, engine):
        eng, recorder = engine
        _enter(eng)
        _enter(eng, trigger=131.40)
        assert [c["qty"] for c in recorder] == [100, 100]

    def test_the_re_entry_gets_a_limit_derived_from_the_offset(self, engine):
        eng, recorder = engine
        _enter(eng)                                  # market: limit unused
        _enter(eng, trigger=131.40)                  # stop-limit: limit matters

        re_entry = recorder[-1]
        expected_offset = eng._compute_limit_offset(131.40)
        assert re_entry["parent_limit_price"] == pytest.approx(
            eng._round_to_tick(eng._round_to_tick(131.40) + expected_offset)
        )
        # And it must sit above the trigger — IBKR's BUY stop-limit constraint.
        assert re_entry["parent_limit_price"] >= re_entry["parent_stop_price"]

    def test_the_offset_comes_from_config_not_a_default(self, monkeypatch):
        """A non-default offset must reach the re-entry's limit price."""
        eng, recorder = _build(monkeypatch, entry_market=True,
                               offset_entry_pct=0.001)
        _enter(eng)
        _enter(eng, trigger=200.00)
        # 0.001 x 200.00 = 0.20, vs GT's 0.0005 default which would give 0.10.
        assert recorder[-1]["parent_limit_price"] == pytest.approx(200.20)

    def test_stop_pct_is_unchanged_across_entries(self, engine):
        """--stop 0.0025 governs the protective child on every cycle."""
        eng, recorder = engine
        _enter(eng)
        _enter(eng, trigger=131.40)
        for call, trigger in zip(recorder, (128.86, 131.40)):
            assert call["child_stop_price"] == pytest.approx(
                eng._protective_stop_price(trigger, 0.0025)
            )


class TestWithoutTheFlag:
    """Default config: nothing anywhere goes in at market."""

    def test_entry_market_defaults_off(self, monkeypatch):
        eng, _ = _build(monkeypatch)
        assert eng.config.entry_market is False

    def test_no_entry_uses_market(self, monkeypatch):
        eng, recorder = _build(monkeypatch)
        for trigger in (128.86, 131.40):
            _enter(eng, trigger)
        assert recorder.parent_markets == [False, False]