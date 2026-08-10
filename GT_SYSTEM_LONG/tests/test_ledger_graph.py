"""LV1 — nested ledger-graph aggregator tests.

Run:  python3 tests/test_ledger_graph.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.execution.fill_ledger import FillLedger      # noqa: E402
from src.ledger.graph import build_ledger_graph, reconcile_cash   # noqa: E402

_fail = 0
def check(name, cond):
    global _fail
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fail += 1


def main():
    with tempfile.TemporaryDirectory() as d:
        # USDJPY long 25k @ 96  → USD +25000, JPY -2,400,000  (cid 98)
        L = FillLedger(FillLedger.path_for(d, "USDJPY", 7497, 98))
        L.record(exec_id="j1", symbol="USDJPY", side="BOT", shares=25000, price=96.0,
                 time="2026-06-13T12:00:00")
        # EURUSD long 25k @ 1.1 → EUR +25000, USD -27,500  (cid 96)
        L2 = FillLedger(FillLedger.path_for(d, "EURUSD", 7497, 96))
        L2.record(exec_id="e1", symbol="EURUSD", side="BOT", shares=25000, price=1.10)
        # AAPL: BUY 100 @ 200 then SELL 100 @ 201 → flat, +realized USD (cid 80)
        L3 = FillLedger(FillLedger.path_for(d, "AAPL", 7497, 80))
        L3.record(exec_id="a1", symbol="AAPL", side="BOT", shares=100, price=200.0)
        L3.record(exec_id="a2", symbol="AAPL", side="SLD", shares=100, price=201.0)

        g = build_ledger_graph(d, ts=123.0)

        # ── structure ──
        check("ts stamped through", g["ts"] == 123.0)
        check("3 pairs present", g["totals"]["pairs"] == 3)
        check("USDJPY position +25000", g["pairs"]["USDJPY"]["position"] == 25000)
        check("AAPL flat (cycled)", g["pairs"]["AAPL"]["position"] == 0)

        # ── per-pair legs (buy pair → +base, -quote) ──
        check("USDJPY base_leg USD +25000", g["pairs"]["USDJPY"]["base_leg"] == 25000)
        check("USDJPY quote_leg JPY -2,400,000",
              g["pairs"]["USDJPY"]["quote_leg"] == -2400000)
        check("EURUSD base=EUR quote=USD",
              g["pairs"]["EURUSD"]["base"] == "EUR" and g["pairs"]["EURUSD"]["quote"] == "USD")

        # ── currency NODES (net cash = A·x̂ summed across pairs) ──
        cur = g["currencies"]
        check("EUR node net +25000", round(cur["EUR"]["net_cash"]) == 25000)
        check("JPY node net -2,400,000", round(cur["JPY"]["net_cash"]) == -2400000)
        # USD touched by USDJPY(+25000), EURUSD(-27500), AAPL(+100 realized)
        check("USD node aggregates 3 pairs' USD legs",
              round(cur["USD"]["net_cash"]) == round(25000 - 27500 + 100))
        check("USD degree = 3 (all three pairs touch USD)", cur["USD"]["degree"] == 3)

        # ── EDGES (FX only; equity has no two-currency edge) ──
        epairs = {e["pair"] for e in g["edges"]}
        check("edges has USDJPY + EURUSD (FX)", epairs == {"USDJPY", "EURUSD"})
        check("no equity edge for AAPL", "AAPL" not in epairs)
        usdjpy_edge = next(e for e in g["edges"] if e["pair"] == "USDJPY")
        check("USDJPY edge side=long", usdjpy_edge["side"] == "long")
        check("USDJPY edge base=USD quote=JPY",
              usdjpy_edge["base"] == "USD" and usdjpy_edge["quote"] == "JPY")

        # ── clients ──
        check("3 clients (80,96,98)", set(g["clients"].keys()) == {"80", "96", "98"})
        check("client 98 owns USDJPY, 1 open",
              g["clients"]["98"]["pairs"] == ["USDJPY"] and g["clients"]["98"]["open"] == 1)
        check("client 80 (AAPL) flat → 0 open", g["clients"]["80"]["open"] == 0)

        # ── totals ──
        check("totals open_pairs == 2 (USDJPY, EURUSD)", g["totals"]["open_pairs"] == 2)
        check("totals currencies == 3 (EUR,USD,JPY)", g["totals"]["currencies"] == 3)

        # ── universe filter ──
        gf = build_ledger_graph(d, universe={"USDJPY"})
        check("universe filter restricts to USDJPY only", set(gf["pairs"]) == {"USDJPY"})

        # ── LV7: recent fills (newest first) ──
        check("recent_fills present", len(g["recent_fills"]) == 4)
        check("recent_fills carries side/pair", all(
            f.get("side") in ("BUY", "SELL") and f.get("pair") for f in g["recent_fills"]))
        check("USDJPY has last_fill_ts", bool(g["pairs"]["USDJPY"]["last_fill_ts"]))

    # ── LV5: triangular cycle (blind space) — separate fixture ──
    with tempfile.TemporaryDirectory() as d2:
        # A triangular loop that nets ZERO cash but holds 3 real positions:
        #   long EURUSD, long USDJPY, short EURJPY  → EUR/USD/JPY all net ~0
        from src.execution.fill_ledger import FillLedger as FL
        FL(FL.path_for(d2, "EURUSD", 7497, 1)).record(
            exec_id="c1", symbol="EURUSD", side="BOT", shares=25000, price=1.10)
        FL(FL.path_for(d2, "USDJPY", 7497, 2)).record(
            exec_id="c2", symbol="USDJPY", side="BOT", shares=25000, price=150.0)
        FL(FL.path_for(d2, "EURJPY", 7497, 3)).record(
            exec_id="c3", symbol="EURJPY", side="SLD", shares=25000, price=165.0)
        gc = build_ledger_graph(d2)
        check("LV5 detects 1 triangular cycle (E-V+C = 3-3+1)", gc["blind_dim"] == 1)
        check("LV5 cycle spans the 3 pairs", gc["cycles"]
              and set(gc["cycles"][0]) == {"EURUSD", "USDJPY", "EURJPY"})
        # no open positions → no cycles
        FL(FL.path_for(d2, "EURUSD", 7497, 1)).record(
            exec_id="c4", symbol="EURUSD", side="SLD", shares=25000, price=1.10)
        gc2 = build_ledger_graph(d2)
        check("LV5 cycle disappears when an edge closes (EURUSD flat)",
              gc2["blind_dim"] == 0)

    # ── LV9: client-id-level reconciliation + external detection ──────
    with tempfile.TemporaryDirectory() as d3:
        from src.execution.fill_ledger import FillLedger as FL
        # client 96 long EURUSD 25k @1.10 → EUR +25000, USD -27500
        FL(FL.path_for(d3, "EURUSD", 7497, 96)).record(
            exec_id="x1", symbol="EURUSD", side="BOT", shares=25000, price=1.10)
        # client 104 long EURGBP 25k @0.85 → EUR +25000, GBP -21250
        FL(FL.path_for(d3, "EURGBP", 7497, 104)).record(
            exec_id="x2", symbol="EURGBP", side="BOT", shares=25000, price=0.85)
        g = build_ledger_graph(d3)
        # per-client footprint present
        check("LV9 client_ccy attributes EUR to BOTH clients",
              g["client_ccy"]["96"]["EUR"] == 25000 and g["client_ccy"]["104"]["EUR"] == 25000)

        # IBKR cash MATCHES our combined ledgers → reconciled, no external
        cash = {"ALL": {"EUR": 50000.0, "USD": -27500.0, "GBP": -21250.0}}
        rc = reconcile_cash(g, cash, baseline_by_account={"ALL": {}})
        check("LV9 EUR reconciled (50k = 25k+25k)", rc["by_currency"]["EUR"]["reconciled"])
        check("LV9 EUR by_client attribution (96 & 104 each +25000)",
              rc["by_currency"]["EUR"]["by_client"] == {"96": 25000.0, "104": 25000.0})
        check("LV9 no external activity when cash matches", rc["external_detected"] is False)

        # Now a DIFFERENT client / manual moves EUR by +30000 that none of our
        # ledgers explain → external residual must light up.
        cash2 = {"ALL": {"EUR": 80000.0, "USD": -27500.0, "GBP": -21250.0}}
        rc2 = reconcile_cash(g, cash2, baseline_by_account={"ALL": {}})
        check("LV9 EXTERNAL detected (EUR +30000 unexplained)",
              rc2["external_detected"] and not rc2["by_currency"]["EUR"]["reconciled"])
        check("LV9 external_residual = +30000",
              rc2["by_currency"]["EUR"]["external_residual"] == 30000)

        # SUB-ACCOUNT segregation: put each client on its own sub-account →
        # exact per-(account,ccy) reconciliation.
        cash3 = {"SUBA": {"EUR": 25000.0, "USD": -27500.0},
                 "SUBB": {"EUR": 25000.0, "GBP": -21250.0}}
        rc3 = reconcile_cash(g, cash3, baseline_by_account={"SUBA": {}, "SUBB": {}},
                             client_account_map={"96": "SUBA", "104": "SUBB"})
        check("LV9 sub-account mode flagged", rc3["multi_account"] is True)
        check("LV9 SUBA EUR exact (client 96 only)",
              rc3["by_account"]["SUBA"]["EUR"]["reconciled"]
              and rc3["by_account"]["SUBA"]["EUR"]["our_expected"] == 25000)
        check("LV9 SUBB GBP exact (client 104 only)",
              rc3["by_account"]["SUBB"]["GBP"]["reconciled"])

    print()
    if _fail:
        print(f"{_fail} CHECK(S) FAILED")
        return 1
    print("ALL LV1/LV5/LV7/LV9 LEDGER-GRAPH TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
