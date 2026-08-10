"""FXP1 — realized P&L is normalized to USD for non-USD-quoted FX, while
equities and USD-quoted FX are returned UNCHANGED (provable no-equity-impact).
Run: python3 tests/test_fx_pnl_usd.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.strategy.risk import fx_quote_pnl_to_usd as f
fails = 0
def chk(name, got, want, tol=1e-6):
    global fails
    ok = abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  got={got:.4f} want={want:.4f}")
    if not ok: fails += 1

# Equity: 6-alpha-upper guard fails (AAPL is 4 chars) → unchanged.
chk("equity AAPL unchanged", f("AAPL", 123.45, 230.0, 5), 123.45)
chk("equity MMM unchanged",  f("MMM", -50.0, 99.0, 10), -50.0)
# USD-quoted FX: quote==USD → unchanged.
chk("EURUSD unchanged", f("EURUSD", 250.0, 1.16, 25000), 250.0)
chk("GBPUSD unchanged", f("GBPUSD", -80.0, 1.34, 25000), -80.0)
# USDJPY: P&L in JPY → /USDJPY rate. (153.30-153.20)*25000=2500 JPY /153.30 ≈ 16.31 USD
chk("USDJPY JPY→USD", f("USDJPY", (153.30-153.20)*25000, 153.30, 25000), 2500/153.30)
# Cross EURGBP: P&L in GBP → /ref_price (1/0.85 factor, consistent w/ notional helper)
chk("EURGBP GBP→USD (approx)", f("EURGBP", 100.0, 0.85, 25000), 100.0/0.85)
# Robustness: ref_price=0 → unchanged (no ÷0); junk ticker → unchanged.
chk("ref_price 0 guard", f("USDJPY", 2500.0, 0.0, 25000), 2500.0)
chk("lowercase/odd ticker unchanged", f("eurusd", 99.0, 1.16, 25000), 99.0)
chk("non-fx 7-char unchanged", f("BRK_B", 42.0, 400.0, 5), 42.0)
print()
if fails: print(f"{fails} CHECK(S) FAILED"); sys.exit(1)
print("ALL FXP1 P&L-USD TESTS PASSED")
