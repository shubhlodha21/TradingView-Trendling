"""RA1 — rolling-kill verify-and-respawn self-heal.

Tests the pure orchestration loop (_respawn_until_alive) with mocked
spawn/alive, so no tmux/IBKR needed. The loop must: recover bots that don't
come up on the first spawn, report honestly when one is unrecoverable, and
no-op when the fleet is already healthy.

Run:  python3 tests/test_respawn_selfheal.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.paper.stress_churn import _respawn_until_alive   # noqa: E402

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


async def _noop(_):
    return


def main():
    # ── 1. recovers bots that lose the clientId race ──
    alive = {"sA": True, "sB": False, "sC": False}
    respawns = {}
    fleet = [{"symbol": "A", "session": "sA"},
             {"symbol": "B", "session": "sB"},
             {"symbol": "C", "session": "sC"}]
    need = {"B": 1, "C": 2}     # B alive after 1 respawn, C after 2

    def spawn(b):
        s = b["symbol"]
        respawns[s] = respawns.get(s, 0) + 1
        ns = f"s{s}_{respawns[s]}"
        alive[ns] = respawns[s] >= need.get(s, 1)
        return ns, "log"

    n, rounds = asyncio.run(_respawn_until_alive(
        fleet, spawn, lambda s: alive.get(s, False),
        retries=4, settle_s=0, stagger_s=0, post_s=0, sleep_fn=_noop, log_fn=lambda *a: None))
    check("recovers all dead bots (3/3 alive)", n == 3)
    check("used 2 rounds (C needed 2)", rounds == 2)
    check("fleet sessions updated in place", fleet[2]["session"].startswith("sC_"))

    # ── 2. honest when a bot is unrecoverable ──
    alive2 = {"sA": True, "sZ": False, "sZ_x": False}
    n2, r2 = asyncio.run(_respawn_until_alive(
        [{"symbol": "A", "session": "sA"}, {"symbol": "Z", "session": "sZ"}],
        lambda b: ("sZ_x", "log"), lambda s: alive2.get(s, False),
        retries=3, settle_s=0, stagger_s=0, post_s=0, sleep_fn=_noop, log_fn=lambda *a: None))
    check("unrecoverable bot reported (1/2 alive, 3 rounds tried)", n2 == 1 and r2 == 3)

    # ── 3. no-op when already healthy ──
    n3, r3 = asyncio.run(_respawn_until_alive(
        [{"symbol": "A", "session": "sA"}], lambda b: ("x", "l"), lambda s: True,
        retries=3, settle_s=0, stagger_s=0, post_s=0, sleep_fn=_noop, log_fn=lambda *a: None))
    check("healthy fleet → 0 respawn rounds", n3 == 1 and r3 == 0)

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL RA1 RESPAWN-SELF-HEAL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
