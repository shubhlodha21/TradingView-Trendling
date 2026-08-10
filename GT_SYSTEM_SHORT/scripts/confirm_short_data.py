#!/usr/bin/env python3
"""Confirm short-selling data is fetched DIRECTLY from IBKR on a PAPER account.

Connects to your paper TWS/Gateway and calls the exact same gateway methods
the engine/dashboard use, printing the RAW IBKR responses so you can verify
the four values are live (not the offline placeholders):

    1. Short Sale Availability   — get_shortable_info() -> shortable_shares / available
    2. Borrow Fee (Stock Loan)   — get_shortable_info() -> fee_rate_annual
    3. Initial Margin            — whatif_order_margin() -> init_margin
    4. Maintenance Margin        — whatif_order_margin() -> maint_margin

Usage (from the repo root):
    python scripts/confirm_short_data.py AAPL --port 7497 --client-id 99 --qty 100

Ports: paper TWS = 7497, paper IB Gateway = 4002 (yours may differ).
This places NOTHING — whatIf is compute-only and the shortable feed is read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make `src` importable when run as `python scripts/confirm_short_data.py`
# (Python only puts scripts/ on the path, not the project root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.broker import Gateway


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", help="Equity ticker to test, e.g. AAPL")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497,
                    help="Paper TWS=7497, paper Gateway=4002")
    ap.add_argument("--client-id", type=int, default=99)
    ap.add_argument("--qty", type=int, default=100,
                    help="Share qty for the margin preview")
    args = ap.parse_args()

    gw = Gateway(host=args.host, port=args.port, client_id=args.client_id,
                 symbol=args.symbol.upper(), paper=True)

    print(f"Connecting to IBKR PAPER at {args.host}:{args.port} "
          f"(clientId={args.client_id}) ...")
    ok = await gw.connect()
    if not ok or not gw.connected:
        print("!! Could not connect. Is your paper TWS/Gateway running on that "
              "port with API enabled?")
        return 1
    print(f"Connected. gateway.paper = {gw.paper}  (still a REAL IBKR link)\n")

    # Qualify the equity contract explicitly so the feeds have a concrete conId.
    from ib_async import Stock
    contract = Stock(args.symbol.upper(), "SMART", "USD")
    try:
        q = await gw._ib.qualifyContractsAsync(contract)
        if q:
            contract = q[0]
    except Exception as e:
        print(f"(contract qualify warning: {type(e).__name__}: {e})")

    print("=== 1+2. Short-sale availability + borrow fee (tick 236 + FEE_RATE) ===")
    info = await gw.get_shortable_info(contract=contract)
    if info:
        print(f"  available        : {info.get('available')}")
        print(f"  shortable_shares : {info.get('shortable_shares')}")
        print(f"  hard_to_borrow   : {info.get('hard_to_borrow')}")
        fr = info.get('fee_rate_annual')
        print(f"  fee_rate_annual  : {fr}"
              + (f"  (~{fr * 10000:.0f} bps/yr)" if isinstance(fr, (int, float)) else ""))
    else:
        print("  -> None. Feed not served to this paper account (needs the linked "
              "live account's short/SLB market-data entitlement). Dashboard will "
              "show the (est) placeholder for these two.")

    print("\n=== 3+4. Initial + maintenance margin (whatIf Order Preview) ===")
    wi = await gw.whatif_order_margin("SELL", args.qty, "MKT", contract=contract)
    if wi:
        print(f"  init_margin  (used)   : {wi.get('init_margin')}")
        print(f"  maint_margin (used)   : {wi.get('maint_margin')}")
        print(f"  init_margin_after     : {wi.get('init_margin_after')}")
        print(f"  maint_margin_after    : {wi.get('maint_margin_after')}")
        print(f"  equity_with_loan_after: {wi.get('equity_with_loan_after')}")
        if wi.get('init_margin') is None and wi.get('maint_margin') is None:
            print("  NOTE: IBKR returned no margin (both Change and After empty) — "
                  "this account/contract doesn't compute whatIf margin.")
    else:
        print("  -> None. whatIf preview unavailable (check the qty and that the "
              "symbol qualified).")

    print("\n=== 5. Account daily P&L — waiting for reqPnL to SETTLE ===")
    print("  (IBKR sends 0/placeholder first, then the real values ~1-4s later)")
    try:
        last = object()
        for i in range(100):  # up to ~10s, showing each change
            await asyncio.sleep(0.1)
            dp = gw.get_daily_pnl()
            if dp is not None and dp != last:
                last = dp
                print(f"    t={i * 0.1:4.1f}s  dailyPnL={dp}  "
                      f"realized={gw.get_realized_pnl()}  "
                      f"unrealized={gw.get_unrealized_pnl()}")
        print("  ------------------------------------------------------------")
        print(f"  FINAL dailyPnL      : {gw.get_daily_pnl()}")
        print(f"  FINAL realizedPnL   : {gw.get_realized_pnl()}")
        print(f"  FINAL unrealizedPnL : {gw.get_unrealized_pnl()}")
        print("  Compare FINAL dailyPnL to your TWS 'DAILY' P&L. They should match.")
    except Exception as e:
        print(f"  read PnL failed: {type(e).__name__}: {e}")

    print("\nDone. Any non-None value above is fetched LIVE from IBKR on paper.")
    await gw.disconnect()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
