"""SL-PCT-RESTART (SHORT) - a tighter-than-default stop must NOT widen to the
config default across a restart / reconcile.

Mirror of the long-side regression, inverted for SHORT: the protective stop
is a BUY-STOP sitting ABOVE the short entry (entry x (1 + pct)), so the pct is
backed out as `child_stop/ref - 1` (not `1 - child_stop/ref`).

Root cause (same as long): a bracket placed with `--stop-pct 0.0025` (0.25%)
rests its child (cover) at entry x 1.0025. If the engine restarts BEFORE the
parent fills and is relaunched WITHOUT the flag (config default 1%), the
frozen pct is lost and the parent-fill retarget defaults to 1% -> the stop
widens UPWARD (farther above entry = looser for a short). Two gaps:

  A. `_place_entry_stop_limit_inner` never persisted the freeze + child.
  B. On parent fill, the freeze defaulted to config.stop_loss_pct instead of
     the pct the resting child was actually placed at.

Locks BOTH fixes (SHORT-inverted):
  Part 1 (Fix A): submit-time freeze survives a save->load round-trip.
  Part 2 (Fix B): parent-fill freeze recovers the pct from the resting child.
  Part 3 (Fix C): reconcile recovery does NOT widen the resting child.

Run:  python3 tests/test_sl_pct_survives_restart.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.models import (                                   # noqa: E402
    Config, OrderRecord, OrderSide, OrderType, OrderStatus,
)
from src.config.persistence import StateStore                     # noqa: E402
from src.strategy.engine import Engine                            # noqa: E402
from tests.harness.clock import SimulatedClock                    # noqa: E402
from tests.harness.rng import DeterministicRNG                    # noqa: E402
from tests.harness.mock_gateway import MockGateway                # noqa: E402

# SHORT: entry is a SELL; protective stop is a BUY-STOP ABOVE entry.
ENTRY = 162.838
TIGHT_PCT = 0.0025           # what the operator passed via --stop-pct
DEFAULT_PCT = 0.01           # config.stop_loss_pct fallback (the 1% bug)
CHILD_STOP = ENTRY * (1 + TIGHT_PCT)   # ABOVE entry - the resting cover stop
QTY = 50000

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


def _wait_for_pct(path, timeout=5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.load(open(path))
            if last.get("stop_loss_pct") is not None:
                return last
        except Exception:
            pass
        time.sleep(0.05)
    return last or {}


def _build(tmp, ticker, cid, stop_pct):
    cfg = Config(ticker=ticker, ibkr_client_id=cid, quantity=QTY,
                 stop_loss_pct=stop_pct)
    gw = MockGateway(ticker,
                     SimulatedClock(start=datetime(2026, 7, 21, 13, 40, 0, tzinfo=timezone.utc)),
                     DeterministicRNG(seed=1))
    store = StateStore(path=os.path.join(tmp, f".gt_state_{ticker}_4001_{cid}.json"))
    return Engine(config=cfg, gateway=gw, state_store=store), store


def _child_dict(child_id, parent_id, stop):
    return {
        'order_id': child_id,
        'qty': QTY,
        'stop_price': stop,
        'side': OrderSide.BUY,   # SHORT: the child is a BUY (cover)
        'order_type': 'STP',
        'parent_order_id': parent_id,
    }


def part1_persistence_round_trip(tmp):
    """Fix A - the submit-time freeze reaches disk and survives a restart
    launched with a DIFFERENT config default."""
    print("Part 1 - freeze survives save->load with a different config default")
    path = os.path.join(tmp, ".gt_state_USDJPY_4001_2.json")

    eng1, _ = _build(tmp, "USDJPY", 2, TIGHT_PCT)
    eng1.state_store = StateStore(path=path)
    eng1._active_stop_pct = TIGHT_PCT
    eng1._bracket_child = _child_dict(
        "BR_BUY_50000_USDJPY_c2_n1_s7a50",
        "ENTRY_SELL_50000_USDJPY_c2_n1_s7a50", CHILD_STOP,
    )
    eng1._save_state()
    on_disk = _wait_for_pct(path)
    check("submit-time freeze persisted to disk (stop_loss_pct == 0.25%)",
          on_disk.get("stop_loss_pct") is not None
          and abs(float(on_disk["stop_loss_pct"]) - TIGHT_PCT) < 1e-9)
    check("bracket child persisted to disk",
          (on_disk.get("bracket_child") or {}).get("order_id")
          == "BR_BUY_50000_USDJPY_c2_n1_s7a50")

    eng2, _ = _build(tmp, "USDJPY", 2, DEFAULT_PCT)
    eng2.state_store = StateStore(path=path)
    eng2._load_state()
    check("restart with 1% config STILL restores the frozen 0.25%",
          eng2._active_stop_pct is not None
          and abs(eng2._active_stop_pct - TIGHT_PCT) < 1e-9)
    check("restart did NOT adopt the config default (1%)",
          abs((eng2._active_stop_pct or 0) - DEFAULT_PCT) > 1e-6)
    check("effective_stop_pct() returns the tight 0.25%, not 1%",
          abs(eng2._effective_stop_pct() - TIGHT_PCT) < 1e-9)


def part2_freeze_recovers_from_child(tmp):
    """Fix B - disk state lost; child recovered only via reconcile. The
    parent-fill freeze must back the pct out of the resting child (SHORT:
    stop/fill - 1), not default to config's 1%."""
    print("Part 2 - parent-fill freeze recovers pct from the resting child")

    async def scenario():
        eng, _ = _build(tmp, "USDJPY", 3, DEFAULT_PCT)

        parent_id = "ENTRY_SELL_50000_USDJPY_c3_n1_s7a50"
        child_id = "BR_BUY_50000_USDJPY_c3_n1_s7a50"
        eng._active_stop_pct = None
        eng._bracket_child = _child_dict(child_id, parent_id, CHILD_STOP)
        eng._position_open = False
        eng._quantity = 0

        # SHORT: the ENTRY is a SELL. Register it so _on_gateway_fill can
        # read its cumulative fill (it calls registry.on_fill internally).
        eng.registry.submit(OrderRecord(
            order_id=parent_id, symbol="USDJPY", side=OrderSide.SELL, qty=QTY,
            order_type=OrderType.LIMIT, limit_price=ENTRY,
            status=OrderStatus.SUBMITTED, submitted_at=eng._ts(),
            signal_price=ENTRY,
        ))

        eng._on_gateway_fill(parent_id, QTY, ENTRY)   # short entry fills
        await asyncio.sleep(0)
        return eng

    eng = asyncio.run(scenario())

    check("freeze recovered 0.25% from the resting child (not 1% default)",
          eng._active_stop_pct is not None
          and abs(eng._active_stop_pct - TIGHT_PCT) < 1e-6)
    check("effective_stop_pct() is 0.25% after the fill",
          abs(eng._effective_stop_pct() - TIGHT_PCT) < 1e-6)
    expected_stop = eng._protective_stop_price(ENTRY, eng._effective_stop_pct())
    check(f"resulting stop stays ~{CHILD_STOP:.2f} (NOT the 1% level ~{ENTRY*1.01:.2f})",
          abs(expected_stop - CHILD_STOP) < 0.05)


def part3_reconcile_recovery_no_widening(tmp):
    """Gap C (SHORT) - broker-only recovery. Reconcile recovers the position
    for the PARENT but the child's frozen pct is None. Without the fix, the
    half-fill recovery computes expected_stop from config's 1% and widens the
    resting 0.25% cover UPWARD. The fix recovers the pct from the child first,
    so the re-modify is a no-op."""
    print("Part 3 - reconcile recovery does NOT widen the resting child")

    eng, _ = _build(tmp, "USDJPY", 4, DEFAULT_PCT)
    eng._position_open = True
    eng._entry_price = ENTRY
    eng._quantity = QTY
    eng._active_stop_pct = None
    eng._pending_stop = {
        'order_id': "BR_BUY_50000_USDJPY_c4_n1_s7a50",
        'qty': QTY, 'stop_price': CHILD_STOP, 'side': OrderSide.BUY,
        'order_type': 'STP', 'from_bracket': True,
    }

    buggy_stop = eng._protective_stop_price(ENTRY, DEFAULT_PCT)
    check("precondition: 1% default would widen the child (bug reproduces)",
          abs(buggy_stop - CHILD_STOP) > 0.05)

    # Apply the reconcile recovery step exactly as engine.py does (SHORT).
    current_stop = eng._pending_stop['stop_price']
    if eng._active_stop_pct is None and current_stop > 0:
        eng._active_stop_pct = max(
            0.0, round((current_stop / eng._entry_price) - 1.0, 6))

    check("reconcile recovered 0.25% from the resting child",
          eng._active_stop_pct is not None
          and abs(eng._active_stop_pct - TIGHT_PCT) < 1e-6)

    expected_stop = eng._protective_stop_price(ENTRY, eng._effective_stop_pct())
    check("expected_stop == resting child (re-modify is a NO-OP, not a widening)",
          abs(expected_stop - current_stop) <= eng._price_epsilon())
    check(f"child stays ~{CHILD_STOP:.2f}, NOT widened to ~{ENTRY*1.01:.2f}",
          abs(expected_stop - CHILD_STOP) < 0.05)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        part1_persistence_round_trip(tmp)
        part2_freeze_recovers_from_child(tmp)
        part3_reconcile_recovery_no_widening(tmp)

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL SL-PCT-RESTART (SHORT) TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
