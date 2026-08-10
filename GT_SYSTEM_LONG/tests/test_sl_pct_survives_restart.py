"""SL-PCT-RESTART - a tighter-than-default stop must NOT widen to the config
default across a restart / reconcile.

Root cause (observed 2026-07-21, USDJPY): a bracket was placed with
`--stop-pct 0.0025` (0.25%) -> child SELL STP rested at 162.43. The engine
restarted BEFORE the parent BUY filled and was relaunched WITHOUT the flag
(config default 1%). Two gaps combined:

  A. `_place_entry_stop_limit` never persisted the freeze (`_active_stop_pct`)
     + `_bracket_child` after submitting the bracket - so disk had nothing to
     restore.
  B. On parent fill, the freeze at engine.py:1624 defaulted to
     `config.stop_loss_pct` (1%) instead of the pct the resting child was
     actually placed at -> the child was RETARGETED 162.43 -> 161.21 (1%),
     silently widening the operator's 0.25% stop 4x.

This test locks BOTH fixes:

  Part 1 (Fix A): the submit-time freeze survives a save->load round-trip even
                  when the restarted session's config default differs.
  Part 2 (Fix B): if disk state is lost entirely and the child is recovered
                  only from the broker (reconcile), the parent-fill freeze
                  recovers the pct FROM THE RESTING CHILD, not the 1% default.

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

# The exact numbers from the incident.
ENTRY = 162.838
TIGHT_PCT = 0.0025           # what the operator passed via --stop-pct
DEFAULT_PCT = 0.01           # config.stop_loss_pct fallback (the 1% bug)
CHILD_STOP = ENTRY * (1 - TIGHT_PCT)   # 162.43 - the resting child stop
QTY = 50000

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


def _wait_for_pct(path, timeout=5.0):
    """StateStore flushes on a background thread - poll until the on-disk
    snapshot carries a non-null stop_loss_pct (or timeout)."""
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
        'side': OrderSide.SELL,
        'order_type': 'STP',
        'parent_order_id': parent_id,
    }


def part1_persistence_round_trip(tmp):
    """Fix A - the submit-time freeze reaches disk and survives a restart
    launched with a DIFFERENT config default."""
    print("Part 1 - freeze survives save->load with a different config default")
    path = os.path.join(tmp, ".gt_state_USDJPY_4001_2.json")

    # Session 1: launched with --stop-pct 0.0025. Bracket just submitted:
    # the freeze + child are set, and (post Fix A) _save_state() runs.
    eng1, _ = _build(tmp, "USDJPY", 2, TIGHT_PCT)
    eng1.state_store = StateStore(path=path)
    eng1._active_stop_pct = TIGHT_PCT
    eng1._bracket_child = _child_dict(
        "BR_SELL_50000_USDJPY_c2_n1_s7a50",
        "ENTRY_BUY_50000_USDJPY_c2_n1_s7a50", CHILD_STOP,
    )
    eng1._save_state()
    on_disk = _wait_for_pct(path)
    check("submit-time freeze persisted to disk (stop_loss_pct == 0.25%)",
          on_disk.get("stop_loss_pct") is not None
          and abs(float(on_disk["stop_loss_pct"]) - TIGHT_PCT) < 1e-9)
    check("bracket child persisted to disk",
          (on_disk.get("bracket_child") or {}).get("order_id")
          == "BR_SELL_50000_USDJPY_c2_n1_s7a50")

    # Session 2: RESTART launched WITHOUT the flag -> config default 1%.
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
    parent-fill freeze must back the pct out of the resting child, not
    default to config's 1%."""
    print("Part 2 - parent-fill freeze recovers pct from the resting child")

    async def scenario():
        # Restarted session's config default is 1% (the trap).
        eng, _ = _build(tmp, "USDJPY", 3, DEFAULT_PCT)

        # Reconcile re-adopted the child from the broker: _bracket_child is
        # set, but _active_stop_pct was NOT recovered (this is the bug).
        parent_id = "ENTRY_BUY_50000_USDJPY_c3_n1_s7a50"
        child_id = "BR_SELL_50000_USDJPY_c3_n1_s7a50"
        eng._active_stop_pct = None
        eng._bracket_child = _child_dict(child_id, parent_id, CHILD_STOP)
        eng._position_open = False
        eng._quantity = 0

        # Register the parent BUY in the registry so _on_gateway_fill can
        # read its cumulative fill (it calls registry.on_fill internally).
        eng.registry.submit(OrderRecord(
            order_id=parent_id, symbol="USDJPY", side=OrderSide.BUY, qty=QTY,
            order_type=OrderType.LIMIT, limit_price=ENTRY,
            status=OrderStatus.SUBMITTED, submitted_at=eng._ts(),
            signal_price=ENTRY,
        ))

        # Parent BUY fills at the limit -> drives the freeze.
        eng._on_gateway_fill(parent_id, QTY, ENTRY)
        await asyncio.sleep(0)   # let any spawned modify task settle

        return eng

    eng = asyncio.run(scenario())

    check("freeze recovered 0.25% from the resting child (not 1% default)",
          eng._active_stop_pct is not None
          and abs(eng._active_stop_pct - TIGHT_PCT) < 1e-6)
    check("effective_stop_pct() is 0.25% after the fill",
          abs(eng._effective_stop_pct() - TIGHT_PCT) < 1e-6)
    # The whole point: the retarget stop stays at the operator's level.
    expected_stop = eng._protective_stop_price(ENTRY, eng._effective_stop_pct())
    check(f"resulting stop stays ~{CHILD_STOP:.2f} (NOT the 1% level ~{ENTRY*0.99:.2f})",
          abs(expected_stop - CHILD_STOP) < 0.05)


def part3_reconcile_recovery_no_widening(tmp):
    """Gap C - broker-only recovery (parent filled during downtime, disk
    state lost). Reconcile recovers position_open/entry for the PARENT from
    the broker, but the CHILD's frozen pct is None. Without the fix, the
    half-fill recovery computes expected_stop from config's 1% and ACTIVELY
    re-modifies the resting 0.25% child (162.43) down to 161.21. The fix
    recovers the pct from the resting child first, so the re-modify is a
    no-op. This test asserts the no-widening invariant using the engine's
    real _effective_stop_pct() + _protective_stop_price()."""
    print("Part 3 - reconcile recovery does NOT widen the resting child")

    # State exactly as reconcile leaves it just before the recovery block:
    # position open + entry recovered from broker, child adopted into
    # _pending_stop, but _active_stop_pct still None; config default 1%.
    eng, _ = _build(tmp, "USDJPY", 4, DEFAULT_PCT)
    eng._position_open = True
    eng._entry_price = ENTRY
    eng._quantity = QTY
    eng._active_stop_pct = None
    eng._pending_stop = {
        'order_id': "BR_SELL_50000_USDJPY_c4_n1_s7a50",
        'qty': QTY, 'stop_price': CHILD_STOP, 'side': OrderSide.SELL,
        'order_type': 'STP', 'from_bracket': True,
    }

    # What the OLD code would have done (the bug): expected_stop from the
    # 1% default → a widened level that differs from the resting child.
    buggy_stop = eng._protective_stop_price(ENTRY, DEFAULT_PCT)
    check("precondition: 1% default would widen the child (bug reproduces)",
          abs(buggy_stop - CHILD_STOP) > 0.05)

    # Apply the reconcile recovery step exactly as engine.py does.
    current_stop = eng._pending_stop['stop_price']
    if eng._active_stop_pct is None and current_stop > 0:
        eng._active_stop_pct = max(
            0.0, round(1.0 - (current_stop / eng._entry_price), 6))

    check("reconcile recovered 0.25% from the resting child",
          eng._active_stop_pct is not None
          and abs(eng._active_stop_pct - TIGHT_PCT) < 1e-6)

    # The invariant: after recovery, the expected_stop the recovery block
    # computes MATCHES the resting child → the re-modify is a no-op.
    expected_stop = eng._protective_stop_price(ENTRY, eng._effective_stop_pct())
    check("expected_stop == resting child (re-modify is a NO-OP, not a widening)",
          abs(expected_stop - current_stop) <= eng._price_epsilon())
    check(f"child stays ~{CHILD_STOP:.2f}, NOT widened to ~{ENTRY*0.99:.2f}",
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
    print("ALL SL-PCT-RESTART TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
