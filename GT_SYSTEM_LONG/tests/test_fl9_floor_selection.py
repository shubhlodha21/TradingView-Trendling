"""FL9 floor SELECTION — the engine must pass the SAME floor to A43 that
_reconcile_missed_fills (FL3) uses, so the two never disagree:

  • started WITH position        → since = None  (full history; recover entry)
  • started FLAT, state present   → since = last_saved_ts  (KEEP dead-window
                                     fills after last save — equity overnight)
  • started FLAT, no state        → since = engine_started_at  (FX cid-reuse
                                     phantom: exclude stale pre-restart execs)

Regression guard for the bug where FL9 floored at engine_started_at on a
resume, dropping genuine overnight dead-window fills (the 2026-05-27 META
postmortem / task #18, re-introduced in the A43 path).

Run:  python3 tests/test_fl9_floor_selection.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.models import Config, TradeState                 # noqa: E402
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


T1 = datetime(2026, 6, 15, 18, 38, 0, tzinfo=timezone.utc)   # last_saved_ts (shutdown)
T2 = datetime(2026, 6, 16, 13, 30, 0, tzinfo=timezone.utc)   # engine_started_at (restart)


class _FakeStore:
    """Minimal StateStore stand-in returning a chosen updated_at."""
    def __init__(self, updated_at_iso):
        self._u = updated_at_iso
    def load(self):
        return {"updated_at": self._u} if self._u else {}
    def save(self, *a, **k):
        return True


def _capturing_gateway(captured):
    def _exec(t, since=None):
        captured["since"] = since
        return 0                      # flat at broker → no adoption, just capture
    async def get_positions():
        return []
    return NS(
        paper=False, connected=True, client_id=2,
        get_positions=get_positions,
        get_our_position_via_executions=_exec,
        get_fx_position_via_account_values=lambda t: None,
    )


def _engine(ticker="NFLX", cid=2):
    cfg = Config(ticker=ticker, ibkr_client_id=cid, quantity=100)
    gw = MockGateway(ticker, SimulatedClock(start=T2), DeterministicRNG(seed=1))
    return Engine(config=cfg, gateway=gw, state_store=_FakeStore(""))


def _run_reconcile(started_with_position, saved_iso):
    eng = _engine()
    eng.state_store = _FakeStore(saved_iso)
    eng._engine_started_at = T2
    eng._started_with_position = started_with_position
    eng._position_open = False
    eng._quantity = 0
    eng._state = TradeState.WAITING_REENTRY
    captured = {}
    eng.gateway = _capturing_gateway(captured)
    asyncio.run(eng._reconcile_position_state())
    return captured.get("since", "UNSET")


def main():
    # ── flat + state present → last_saved_ts (the equity overnight fix) ──
    since = _run_reconcile(started_with_position=False, saved_iso=T1.isoformat())
    check("flat+state: floor == last_saved_ts (KEEPS dead-window fills)", since == T1)
    check("flat+state: floor is NOT engine_started_at (the #18 bug)", since != T2)

    # ── flat + NO state (FX phantom / fresh) → engine_started_at ────────
    since = _run_reconcile(started_with_position=False, saved_iso="")
    check("flat+no-state: floor == engine_started_at (FX phantom excluded)", since == T2)

    # ── started WITH position → no floor (recover own entry) ────────────
    since = _run_reconcile(started_with_position=True, saved_iso=T1.isoformat())
    check("started-with-position: floor is None (full history)", since is None)

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL FL9 FLOOR-SELECTION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
