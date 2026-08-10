"""FL5 — three_truths monitor uses the durable fill ledger as FX/equity
position truth, with NO broker connection (ledger-only mode).

Run:  python3 tests/test_three_truths_ledger.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scripts.three_truths as tt                          # noqa: E402
from src.execution.fill_ledger import FillLedger           # noqa: E402

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


def main():
    with tempfile.TemporaryDirectory() as d:
        # Point the monitor's PROJECT_ROOT at our temp dir so its glob finds
        # the ledger files we write here (no real fleet involved).
        tt.PROJECT_ROOT = __import__("pathlib").Path(d)

        # write two ledger files: a balanced EURUSD (net 0) and a real
        # USDCHF long that positions() would have mis-reported.
        L1 = FillLedger(FillLedger.path_for(d, "EURUSD", 7497, 96))
        L1.record(exec_id="a1", symbol="EURUSD", side="BOT", shares=25000)
        L1.record(exec_id="a2", symbol="EURUSD", side="SLD", shares=25000)
        L2 = FillLedger(FillLedger.path_for(d, "USDCHF", 7497, 100))
        L2.record(exec_id="b1", symbol="USDCHF", side="BOT", shares=25000)

        # ── 1. scanner reads files, no connection ───────────────────────
        led = tt._scan_fill_ledgers(None)
        check("scanner reads EURUSD net = 0", led.get("EURUSD") == 0)
        check("scanner reads USDCHF net = +25000", led.get("USDCHF") == 25000)

        # ── 2. reconcile: ledger replaces positions() for FX ────────────
        # engine thinks USDCHF flat (the tonight bug); positions() returned 0
        # too — old monitor would call this COHERENT and miss the real long.
        engine = {"USDCHF": {"qty": 0.0, "cids": [100], "state": "WAITING_REENTRY"}}
        bm = {}  # no broker order data
        rep = tt.reconcile(engine, bm, check_ltp=False, broker_ok=True,
                           ledger=led, orders_known=False,
                           seed={"USDCHF", "EURUSD"})
        row = {r["symbol"]: r for r in rep["rows"]}
        check("USDCHF flagged DRIFT (engine 0 ≠ ledger +25000)",
              row["USDCHF"]["verdict"] == "DRIFT")
        check("USDCHF reason cites ledger, not positions()",
              "ledger" in row["USDCHF"]["reason"])
        check("EURUSD COHERENT (engine flat = ledger flat)",
              row["EURUSD"]["verdict"] == "COHERENT")

        # ── 3. engine agrees with ledger → COHERENT (no false WATCH) ────
        engine2 = {"USDCHF": {"qty": 25000.0, "open": True, "cids": [100]}}
        rep2 = tt.reconcile(engine2, {}, check_ltp=False, broker_ok=True,
                            ledger=led, orders_known=False, seed={"USDCHF"})
        r2 = rep2["rows"][0]
        check("engine +25000 = ledger +25000 → COHERENT (no FX false WATCH)",
              r2["verdict"] == "COHERENT")

        # ── 4. no ledger for symbol → falls back to positions() WATCH ───
        # (legacy behavior preserved when a symbol has no ledger file)
        rep3 = tt.reconcile({"GBPUSD": {"qty": 25000.0, "open": True, "cids": [97]}},
                            {"GBPUSD": {"broker_qty": 0.0, "live_sells": 1, "live_orders": 1}},
                            check_ltp=False, broker_ok=True,
                            ledger={}, orders_known=True, seed={"GBPUSD"})
        check("no-ledger FX engine≠positions() → soft WATCH (legacy)",
              rep3["rows"][0]["verdict"] == "WATCH")

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL FL5 MONITOR-LEDGER TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
