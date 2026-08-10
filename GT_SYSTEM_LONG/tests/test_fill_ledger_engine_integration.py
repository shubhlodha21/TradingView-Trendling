"""FL2 integration: the PRODUCTION Engine records every fill into its
FillLedger, additively and without disturbing existing behavior.

Constructs the real src.strategy.engine.Engine against the deterministic
MockGateway, fires BUY then SELL fills through the real _on_gateway_fill
path, and asserts the ledger net matches. Also checks the
GT_DISABLE_FILL_LEDGER kill-switch and that paper (no execId) no-ops.

Run:  python3 tests/test_fill_ledger_engine_integration.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.models import (Config, OrderRecord, OrderSide, OrderType,  # noqa: E402
                                OrderStatus)
from src.config.persistence import StateStore                              # noqa: E402
from src.strategy.engine import Engine                                     # noqa: E402
from tests.harness.clock import SimulatedClock                             # noqa: E402
from tests.harness.rng import DeterministicRNG                             # noqa: E402
from tests.harness.mock_gateway import MockGateway                         # noqa: E402

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


def _build_engine(tmp, ticker="EURAUD", cid=96, disable=False):
    if disable:
        os.environ["GT_DISABLE_FILL_LEDGER"] = "1"
    else:
        os.environ.pop("GT_DISABLE_FILL_LEDGER", None)
    cfg = Config(ticker=ticker, ibkr_client_id=cid, quantity=25000)
    gw = MockGateway(ticker,
                     SimulatedClock(start=datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)),
                     DeterministicRNG(seed=42))
    store = StateStore(path=os.path.join(tmp, f".gt_state_{ticker}_4001_{cid}.json"))
    return Engine(config=cfg, gateway=gw, state_store=store)


def _submit(engine, order_id, side, qty):
    engine.registry.submit(OrderRecord(
        order_id=order_id, symbol=engine.config.ticker, side=side, qty=qty,
        order_type=OrderType.STOP_LIMIT if side == OrderSide.BUY else OrderType.STOP,
        status=OrderStatus.SUBMITTED, submitted_at=engine._ts(),
    ))


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # ── 1. ledger constructed + wired ───────────────────────────────
        eng = _build_engine(tmp)
        check("engine built a FillLedger", eng._fill_ledger is not None)

        # ── 2. BUY fill lands in ledger via real _on_gateway_fill ───────
        _submit(eng, "ENTRY_BUY_25000_EURAUD_c96_n1_sTEST", OrderSide.BUY, 25000)
        eng._on_gateway_fill("ENTRY_BUY_25000_EURAUD_c96_n1_sTEST", 25000, 1.64205,
                             exec_id="exec-buy-1", fill_time=datetime(2026, 6, 12, 12, 0, 1))
        check("ledger net = +25000 after BUY fill", eng._fill_ledger.net("EURAUD") == 25000)

        # ── 3. duplicate exec_id (reconnect replay) does NOT double-count ─
        eng._on_gateway_fill("ENTRY_BUY_25000_EURAUD_c96_n1_sTEST", 25000, 1.64205,
                             exec_id="exec-buy-1", fill_time=datetime(2026, 6, 12, 12, 0, 1))
        check("ledger still +25000 after duplicate exec_id", eng._fill_ledger.net("EURAUD") == 25000)

        # ── 4. SELL fill returns net to flat ────────────────────────────
        _submit(eng, "BR_SELL_25000_EURAUD_c96_n1_sTEST", OrderSide.SELL, 25000)
        eng._on_gateway_fill("BR_SELL_25000_EURAUD_c96_n1_sTEST", 25000, 1.64000,
                             exec_id="exec-sell-1", fill_time=datetime(2026, 6, 12, 12, 0, 2))
        check("ledger net = 0 after SELL fill", eng._fill_ledger.net("EURAUD") == 0)

        # ── 5. paper-style fill (exec_id=None) no-ops, never crashes ────
        _submit(eng, "ENTRY_BUY_25000_EURAUD_c96_n2_sTEST", OrderSide.BUY, 25000)
        eng._on_gateway_fill("ENTRY_BUY_25000_EURAUD_c96_n2_sTEST", 25000, 1.64210,
                             exec_id=None)
        check("no-execId fill does not change ledger net (still 0)",
              eng._fill_ledger.net("EURAUD") == 0)

        # ── 6. durability: ledger survives 'restart' (new engine, same dir) ─
        eng2 = _build_engine(tmp)  # same ticker/cid/dir → same ledger file
        check("reloaded engine ledger net = 0 (BUY+SELL persisted, balanced)",
              eng2._fill_ledger.net("EURAUD") == 0)
        check("reloaded ledger counted 2 durable fills", eng2._fill_ledger.count() == 2)

        # ── 6b. FL3: startup replay merges broker fills (dead-bot gap) ──
        # Simulate fills that happened while the bot was DEAD: they exist
        # only in the broker's get_all_fills() history, never went through
        # _on_gateway_fill. _reconcile_missed_fills must absorb them into
        # the durable ledger (top-of-method merge), deduping the one already
        # recorded live. We put the fills BEFORE the replay floor so the
        # state/P&L replay loop skips them (no gateway side-effects) — we're
        # isolating the ledger-merge path, which runs regardless of floor.
        import asyncio as _aio
        from types import SimpleNamespace as _NS
        eng3 = _build_engine(tmp, ticker="USDCHF", cid=100)
        eng3._engine_started_at = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
        _during = datetime(2026, 6, 12, 12, 0, 30, tzinfo=timezone.utc)  # AFTER floor (dead-window)
        _stale  = datetime(2026, 6, 12, 11, 0, 0, tzinfo=timezone.utc)   # BEFORE floor (pre-restart)

        def _fill(eid, side, shares, t):
            return _NS(execution=_NS(execId=eid, side=side, shares=shares,
                                     price=0.7966, time=t, orderId=1, clientId=100),
                       contract=_NS(symbol="USD", localSymbol="USD.CHF",
                                    currency="CHF", secType="CASH"))
        # one already recorded live, two during-downtime (after floor), and a
        # STALE pre-restart buy (before floor) that FL7 must drop — else it
        # leaves a phantom long (the GOOGL/EURUSD bug).
        eng3._fill_ledger.record(exec_id="live-1", symbol="USDCHF", side="BOT", shares=25000)
        broker_fills = [_fill("live-1", "BOT", 25000, _during),  # dup of live → ignored
                        _fill("dead-1", "BOT", 25000, _during),  # during downtime → kept
                        _fill("dead-2", "SLD", 25000, _during),  # during downtime → kept
                        _fill("stale-1","BOT", 25000, _stale)]   # pre-restart → DROPPED by floor
        # Minimal stub gateway (MockGateway uses __slots__, can't patch). The
        # reconcile only needs paper/client_id/get_all_fills + state_store.
        eng3.gateway = _NS(paper=False, client_id=100, get_all_fills=lambda: broker_fills)
        _aio.run(eng3._reconcile_missed_fills())
        _agg = lambda L: sum(L.net().values())  # translator-agnostic total
        # kept: +25000 (live) +25000 (dead-1) -25000 (dead-2) = +25000; stale dropped
        check("FL7 floor drops stale pre-restart fill (net = +25000, not +50000)",
              _agg(eng3._fill_ledger) == 25000)
        check("FL7 counted 3 unique fills (stale-1 floored out)",
              eng3._fill_ledger.count() == 3)
        # idempotent: re-running reconcile adds nothing
        _aio.run(eng3._reconcile_missed_fills())
        check("FL3 re-reconcile is idempotent (still 3, net +25000)",
              eng3._fill_ledger.count() == 3 and _agg(eng3._fill_ledger) == 25000)

        # ── 6c. FL4: A43 truth source prefers durable ledger ───────────
        # Use the REAL Gateway (not MockGateway) so we exercise the actual
        # edited get_our_position_via_executions. Constructed, NOT connected.
        from src.execution.broker import Gateway as RealGateway   # noqa: E402
        from src.config.models import Config as Cfg               # noqa: E402
        cfg4 = Cfg(ticker="EURUSD", ibkr_client_id=80, quantity=25000)
        gw4 = RealGateway(host="127.0.0.1", port=4001, client_id=80,
                          symbol="EURUSD", paper=False)
        store4 = StateStore(path=os.path.join(tmp, ".gt_state_EURUSD_4001_80.json"))
        eng4 = Engine(config=cfg4, gateway=gw4, state_store=store4)
        # engine lent its ledger to the (real) gateway in __init__
        check("FL4 gateway received ledger borrow",
              gw4._fill_ledger is eng4._fill_ledger)
        # empty ledger → does NOT shortcut to 0; not connected + _ib None → None
        check("FL4 empty ledger falls through (None, not false 0)",
              gw4.get_our_position_via_executions("EURUSD") is None
              and eng4._fill_ledger.count() == 0)
        # populate the ledger → method returns ledger net WITHOUT a broker
        # call (gateway is not connected; _ib is None — would crash if touched)
        eng4._fill_ledger.record(exec_id="f1", symbol="EURUSD", side="BOT", shares=25000)
        eng4._fill_ledger.record(exec_id="f2", symbol="EURUSD", side="BOT", shares=25000)
        check("FL4 populated ledger returns +50000 without touching broker",
              gw4.get_our_position_via_executions("EURUSD") == 50000)

        # ── 7. kill-switch ──────────────────────────────────────────────
        eng_off = _build_engine(tmp, ticker="GBPUSD", cid=97, disable=True)
        check("GT_DISABLE_FILL_LEDGER=1 → no ledger", eng_off._fill_ledger is None)
        # and a fill with the ledger disabled must not crash
        _submit(eng_off, "ENTRY_BUY_25000_GBPUSD_c97_n1_sTEST", OrderSide.BUY, 25000)
        try:
            eng_off._on_gateway_fill("ENTRY_BUY_25000_GBPUSD_c97_n1_sTEST", 25000, 1.34,
                                     exec_id="exec-x")
            check("disabled-ledger fill path does not crash", True)
        except Exception as e:
            check(f"disabled-ledger fill path does not crash ({e})", False)
        os.environ.pop("GT_DISABLE_FILL_LEDGER", None)

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL FL2 ENGINE-INTEGRATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
