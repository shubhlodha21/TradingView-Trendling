"""RS1 — a bot gracefully stopped while FLAT must RESUME (not stay STOPPED)
on the next launch.

Root cause (observed 2026-06-16, equity overnight): stop() sets
_state=STOPPED and persists it; on restart _load_state() restores STOPPED and
start() must promote it back to MONITORING, else the engine sits frozen all
day — never evaluating entries, never re-arming, never running the
dead-window fill recovery. AAPL/META escaped only via orphan-adoption (they
held a real position); flat bots (NFLX/AMD/SPCX) stayed dead.

Run:  python3 tests/test_rs1_resume_from_stopped.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone


def _wait_for_state(path, want_state, timeout=5.0):
    """StateStore flushes on a background thread — poll until the on-disk
    snapshot shows the state we expect (or timeout)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.load(open(path))
            if last.get("state") == want_state:
                return last
        except Exception:
            pass
        time.sleep(0.05)
    return last or {}

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
                     SimulatedClock(start=datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)),
                     DeterministicRNG(seed=1))
    store = StateStore(path=os.path.join(tmp, f".gt_state_{ticker}_4001_{cid}.json"))
    return Engine(config=cfg, gateway=gw, state_store=store)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, ".gt_state_NFLX_4001_2.json")

        # ── 1. graceful stop() while FLAT persists STOPPED to disk ──────
        eng = _build(tmp, "NFLX", 2)
        eng.state_store = StateStore(path=path)
        eng._state = TradeState.WAITING_REENTRY      # the real live state
        eng._previous_breakout_level = 81.68         # re-entry level (the NFLX case)
        eng._position_open = False
        asyncio.run(eng.stop())                      # graceful shutdown
        on_disk = _wait_for_state(path, "STOPPED")   # StateStore flushes async
        check("graceful stop() persists state=STOPPED", on_disk.get("state") == "STOPPED")
        check("breakout level survived the save", abs(float(on_disk.get("previous_breakout_level") or 0) - 81.68) < 1e-9)

        # ── 2. fresh engine reloads STOPPED (the bug precondition) ──────
        eng2 = _build(tmp, "NFLX", 2)
        eng2.state_store = StateStore(path=path)
        eng2._load_state()
        check("restart restores STOPPED from disk", eng2._state == TradeState.STOPPED)
        check("restart restores breakout level", abs((eng2._previous_breakout_level or 0) - 81.68) < 1e-9)

        # ── 3. RS1b faithful: FLAT + breakout → WAITING_REENTRY ─────────
        eng2._position_open = False
        eng2._promote_resumable_state()
        check("RS1b: STOPPED+flat+breakout → WAITING_REENTRY (faithful label)",
              eng2._state == TradeState.WAITING_REENTRY)
        check("RS1: breakout level preserved through promotion (re-arm intact)",
              abs((eng2._previous_breakout_level or 0) - 81.68) < 1e-9)

        # ── 3b. FLAT + NO breakout → MONITORING ─────────────────────────
        eng2._state = TradeState.STOPPED
        eng2._previous_breakout_level = None
        eng2._position_open = False
        eng2._promote_resumable_state()
        check("RS1b: STOPPED+flat+no-breakout → MONITORING",
              eng2._state == TradeState.MONITORING)

        # ── 3c. STOPPED + OPEN position → MONITORING (NOT WAITING_REENTRY);
        #        the reconcile/orphan-adoption path then sets IN_POSITION ──
        eng2._state = TradeState.STOPPED
        eng2._previous_breakout_level = 81.68
        eng2._position_open = True
        eng2._promote_resumable_state()
        check("RS1b: STOPPED+position_open → MONITORING (adoption sets IN_POSITION, not WAITING_REENTRY)",
              eng2._state == TradeState.MONITORING)
        eng2._position_open = False  # reset

        # ── 4. IDLE (no breakout) → MONITORING; ACTIVE states untouched ─
        eng2._state = TradeState.IDLE
        eng2._previous_breakout_level = None
        eng2._promote_resumable_state()
        check("RS1: IDLE (no breakout) → MONITORING", eng2._state == TradeState.MONITORING)

        eng2._state = TradeState.IN_POSITION
        eng2._promote_resumable_state()
        check("RS1: IN_POSITION is NOT clobbered (mid-session restart safe)",
              eng2._state == TradeState.IN_POSITION)

        eng2._state = TradeState.WAITING_REENTRY
        eng2._promote_resumable_state()
        check("RS1: WAITING_REENTRY is NOT clobbered",
              eng2._state == TradeState.WAITING_REENTRY)

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL RS1 RESUME-FROM-STOPPED TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
