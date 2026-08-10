"""Standalone tests for FillLedger — the persistent exactly-once fill journal.

Run:  python3 -m tests.test_fill_ledger      (from project root)
   or: python3 tests/test_fill_ledger.py
No pytest required; prints PASS/FAIL and exits non-zero on any failure.
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.execution.fill_ledger import FillLedger, _norm_side  # noqa: E402

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


def _fake_fill(exec_id, sym, side, shares, client_id=88, price=1.0):
    return SimpleNamespace(
        execution=SimpleNamespace(execId=exec_id, side=side, shares=shares,
                                  price=price, time="t", orderId=1, clientId=client_id),
        contract=SimpleNamespace(symbol=sym, localSymbol=sym, currency="USD"),
    )


def main():
    with tempfile.TemporaryDirectory() as d:
        # ── 1. basic net + dedup ────────────────────────────────────────
        p = FillLedger.path_for(d, "EURAUD", 7497, 96)
        L = FillLedger(p)
        L.record(exec_id="e1", symbol="EURAUD", side="BOT", shares=25000)
        L.record(exec_id="e2", symbol="EURAUD", side="BOT", shares=25000)
        L.record(exec_id="e3", symbol="EURAUD", side="SLD", shares=25000)
        check("net after 2 buys 1 sell = +25000", L.net("EURAUD") == 25000)
        # duplicate execId must be ignored
        dup = L.record(exec_id="e1", symbol="EURAUD", side="BOT", shares=25000)
        check("duplicate execId returns False", dup is False)
        check("net unchanged after dup", L.net("EURAUD") == 25000)
        check("count = 3 (dup not counted)", L.count() == 3)

        # ── 2. durability / gap-free across restart ─────────────────────
        L2 = FillLedger(p)  # fresh instance, same file
        check("reloaded net survives restart = +25000", L2.net("EURAUD") == 25000)
        check("reloaded count = 3", L2.count() == 3)
        # a fill recorded by the reloaded instance dedups against history
        check("reloaded instance still dedups e2", L2.record(
            exec_id="e2", symbol="EURAUD", side="BOT", shares=25000) is False)

        # ── 3. side normalization (BOT/SLD and BUY/SELL) ────────────────
        check("_norm_side BOT=+1", _norm_side("BOT") == 1)
        check("_norm_side SLD=-1", _norm_side("SLD") == -1)
        check("_norm_side BUY=+1", _norm_side("buy") == 1)
        check("_norm_side SELL=-1", _norm_side("Sell") == -1)
        check("_norm_side junk=0", _norm_side("FOO") == 0)

        # ── 4. merge_broker_fills exactly-once across live + replay ──────
        p2 = FillLedger.path_for(d, "USDCHF", 7497, 100)
        M = FillLedger(p2)
        # live path recorded e10 already
        M.record(exec_id="e10", symbol="USDCHF", side="BOT", shares=25000)
        fills = [
            _fake_fill("e10", "USDCHF", "BOT", 25000),   # dup of live → ignored
            _fake_fill("e11", "USDCHF", "BOT", 25000),   # new
            _fake_fill("e12", "USDCHF", "SLD", 25000),   # new
            _fake_fill("e13", "OTHER", "BOT", 999, client_id=5),  # other client → filtered
        ]
        new = M.merge_broker_fills(fills, symbol_of=lambda c: c.symbol, our_client_id=88)
        check("merge counted only 2 new (e11,e12)", new == 2)
        check("merge dedup'd live e10", M.count() == 3)
        check("USDCHF net after merge = +25000", M.net("USDCHF") == 25000)
        check("other-client fill filtered out", M.net("OTHER") == 0)
        # idempotent: merging again adds nothing
        check("re-merge adds 0 (idempotent)", M.merge_broker_fills(
            fills, symbol_of=lambda c: c.symbol, our_client_id=88) == 0)

        # ── 5. corruption tolerance ─────────────────────────────────────
        p3 = FillLedger.path_for(d, "GBPUSD", 7497, 97)
        with open(p3, "w") as f:
            f.write('{"exec_id":"g1","symbol":"GBPUSD","side":"BOT","shares":25000}\n')
            f.write('THIS IS NOT JSON{{{\n')                      # garbage line
            f.write('{"exec_id":"g2","symbol":"GBPUSD","side":"SLD","shares":10000}\n')
        Lc = FillLedger(p3)
        check("corrupt line skipped, net = +15000", Lc.net("GBPUSD") == 15000)
        check("corrupt-tolerant count = 2", Lc.count() == 2)

        # ── 6. path scales 1..32 client ids, no collision ───────────────
        paths = {FillLedger.path_for(d, "EURUSD", 7497, cid) for cid in range(1, 33)}
        check("32 distinct ledger paths for 32 client ids", len(paths) == 32)

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL FILLLEDGER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
