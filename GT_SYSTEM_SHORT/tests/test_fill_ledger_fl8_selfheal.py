"""FL8 — engine self-heals by adopting the ledger-backed position truth in
_reconcile_position_state (the MSFT-class fix: engine missed a dead-window
fill, ledger caught it, engine must adopt instead of staying blind).

Run:  python3 tests/test_fill_ledger_fl8_selfheal.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.models import Config, TradeState                 # noqa: E402
from src.config.persistence import StateStore                    # noqa: E402
from src.strategy.engine import Engine                           # noqa: E402
from tests.harness.clock import SimulatedClock                   # noqa: E402
from tests.harness.rng import DeterministicRNG                   # noqa: E402
from tests.harness.mock_gateway import MockGateway               # noqa: E402

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


def _build(tmp, ticker, cid):
    cfg = Config(ticker=ticker, ibkr_client_id=cid, quantity=100)
    gw = MockGateway(ticker,
                     SimulatedClock(start=datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)),
                     DeterministicRNG(seed=1))
    store = StateStore(path=os.path.join(tmp, f".gt_state_{ticker}_4001_{cid}.json"))
    return Engine(config=cfg, gateway=gw, state_store=store)


def _stub_gateway(exec_qty, positions=None):
    """Minimal gateway exposing exactly what _reconcile_position_state uses."""
    async def get_positions():
        return positions or []
    return NS(
        paper=False, connected=True, client_id=82,
        get_positions=get_positions,
        # FL9: signature now takes an optional `since` floor. The real
        # ledger-backed (FL4) path ignores it (ledger is floored at merge),
        # so the stub does too — returning the durable net regardless.
        get_our_position_via_executions=lambda t, since=None: exec_qty,
        get_fx_position_via_account_values=lambda t: None,
        # place path is exercised via engine._place_protective_stop; give it
        # nothing so that call throws and is swallowed (we test STATE only).
    )


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # ── 1. LEDGER LONG, engine FLAT → adopt (the MSFT bug) ──────────
        eng = _build(tmp, "MSFT", 82)
        eng._prev_ltp = 388.0
        eng._engine_started_at = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        eng._position_open = False
        eng._quantity = 0
        eng._state = TradeState.WAITING_REENTRY
        eng.gateway = _stub_gateway(exec_qty=70)
        asyncio.run(eng._reconcile_position_state())
        check("FL8 adopts ledger LONG: engine _quantity == 70",
              eng._quantity == 70)
        check("FL8 sets _position_open True", eng._position_open is True)
        check("FL8 sets state IN_POSITION", eng._state == TradeState.IN_POSITION)
        check("FL8 set an entry price (~388)", bool(eng._entry_price) and eng._entry_price > 0)

        # ── 2. LEDGER FLAT, engine LONG → fold to FLAT (missed close) ───
        eng2 = _build(tmp, "AAPL", 83)
        eng2._prev_ltp = 230.0
        eng2._engine_started_at = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        eng2._position_open = True
        eng2._quantity = 100
        eng2._entry_price = 230.0
        eng2._state = TradeState.IN_POSITION
        eng2.gateway = _stub_gateway(exec_qty=0)
        asyncio.run(eng2._reconcile_position_state())
        check("FL8 folds to FLAT when ledger says 0: _position_open False",
              eng2._position_open is False)
        check("FL8 fold: _quantity == 0", eng2._quantity == 0)
        check("FL8 fold: state WAITING_REENTRY", eng2._state == TradeState.WAITING_REENTRY)

        # ── 3. agreement → no change (normal operation untouched) ───────
        eng3 = _build(tmp, "NVDA", 84)
        eng3._engine_started_at = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        eng3._position_open = True
        eng3._quantity = 100
        eng3._entry_price = 200.0
        eng3._state = TradeState.IN_POSITION
        eng3.gateway = _stub_gateway(exec_qty=100)
        asyncio.run(eng3._reconcile_position_state())
        check("FL8 no-op when ledger == engine (100): still IN_POSITION 100",
              eng3._position_open and eng3._quantity == 100
              and eng3._state == TradeState.IN_POSITION)

        # ── 4. LEDGER SHORT → alert, no crash (long-only) ───────────────
        eng4 = _build(tmp, "EURGBP", 88)
        eng4._engine_started_at = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        eng4._position_open = False
        eng4._quantity = 0
        eng4._state = TradeState.WAITING_REENTRY
        eng4.gateway = _stub_gateway(exec_qty=-25000)
        try:
            asyncio.run(eng4._reconcile_position_state())
            check("FL8 ledger SHORT path does not crash", True)
        except Exception as e:
            check(f"FL8 ledger SHORT path does not crash ({e})", False)

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL FL8 SELF-HEAL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
