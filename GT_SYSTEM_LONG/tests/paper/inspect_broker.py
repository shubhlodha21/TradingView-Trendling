"""Read-only diagnostic — show what IBKR actually reports.

Connects to TWS (clientId=97 sidecar so it doesn't clash with your live
bot or test scripts), then prints:

  1. ib.positions()          ← what get_positions() sees
  2. ib.accountValues()      ← cash balances per currency (FX truth)
  3. ib.openOrders()         ← resting orders (your SELL STPs)

Use this BEFORE and AFTER a TWS restart to prove the FX cash quirk:
- BEFORE restart: positions() may show EUR.USD 25000
- AFTER  restart: positions() probably shows NOTHING for EUR.USD,
                  but accountValues() still shows Currency=EUR with
                  Value≈25000

USAGE:
    python3 -m tests.paper.inspect_broker
    python3 -m tests.paper.inspect_broker --port 7497
    python3 -m tests.paper.inspect_broker --symbol EURUSD
"""
from __future__ import annotations

import argparse
import asyncio
import sys


async def inspect(port: int, symbol_filter: str | None) -> int:
    try:
        from ib_async import IB
    except ImportError:
        print("ERROR: ib_async not installed. Activate your venv first.",
              file=sys.stderr)
        return 1

    ib = IB()
    try:
        print(f"\n[inspect] connecting to 127.0.0.1:{port} clientId=97...")
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=97),
            timeout=8.0,
        )
        print("[inspect] connected.\n")

        # Give IBKR a moment to push initial account state
        await asyncio.sleep(2.0)

        # ── 1. positions() ────────────────────────────────────────────
        print("=" * 78)
        print("1. ib.positions()  ← what the engine's get_positions() uses")
        print("=" * 78)
        positions = ib.positions()
        if not positions:
            print("  (empty — NO positions reported)")
        else:
            for p in positions:
                c = p.contract
                marker = "  ◀ MATCH" if symbol_filter and (
                    symbol_filter in (c.symbol, c.localSymbol or "")
                ) else ""
                print(f"  symbol={c.symbol!r:<10} "
                      f"localSymbol={(c.localSymbol or '')!r:<10} "
                      f"secType={c.secType:<6} "
                      f"exchange={c.exchange:<10} "
                      f"qty={p.position:>+12.2f}  "
                      f"avgCost={p.avgCost:>10.5f}{marker}")
        print()

        # ── 2. accountValues() filtered for currency balances ────────
        print("=" * 78)
        print("2. ib.accountValues()  ← cash balances per currency (FX TRUTH)")
        print("=" * 78)
        all_vals = ib.accountValues()
        # Filter to the rows that matter for FX: CashBalance per currency
        cash = [v for v in all_vals if v.tag == "CashBalance"]
        if not cash:
            print("  (no CashBalance entries — wait a moment and re-run?)")
        else:
            for v in cash:
                amount = float(v.value)
                if abs(amount) < 0.005:
                    continue  # hide ~0 currencies
                print(f"  account={v.account:<12} "
                      f"currency={v.currency:<5} "
                      f"cashBalance={amount:>+15.2f}")
        # Also show ExchangeRate so you can verify the position economics
        rates = [v for v in all_vals if v.tag == "ExchangeRate"]
        if rates:
            print()
            print("  (exchange rates — for reference)")
            for v in rates:
                print(f"  currency={v.currency:<5}  rate={float(v.value):.5f}")
        print()

        # ── 3. openOrders() ──────────────────────────────────────────
        print("=" * 78)
        print("3. ib.openOrders()  ← resting orders (your SELL STPs)")
        print("=" * 78)
        # Refresh open-orders explicitly so all-clients shows up
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception as e:
            print(f"  (reqAllOpenOrdersAsync failed: {e})")
        opens = ib.openTrades()
        if not opens:
            print("  (empty — NO resting orders)")
        else:
            for t in opens:
                o = t.order
                c = t.contract
                marker = "  ◀ MATCH" if symbol_filter and (
                    symbol_filter in (c.symbol, c.localSymbol or "")
                ) else ""
                print(f"  id={o.orderId:<8} "
                      f"sym={(c.localSymbol or c.symbol)!r:<10} "
                      f"{o.action:<4} "
                      f"qty={o.totalQuantity:>10.0f} "
                      f"type={o.orderType:<8} "
                      f"aux={o.auxPrice or 0:>10.5f} "
                      f"lmt={o.lmtPrice or 0:>10.5f} "
                      f"tif={o.tif:<3} "
                      f"status={t.orderStatus.status}{marker}")
        print()

        # ── 4. Verdict ───────────────────────────────────────────────
        print("=" * 78)
        print("4. VERDICT — does the FX cash quirk apply to your setup?")
        print("=" * 78)
        if symbol_filter:
            in_positions = any(
                symbol_filter in (p.contract.symbol, p.contract.localSymbol or "")
                for p in positions
            )
            # For FX EURUSD: check if EUR cash balance is non-zero
            base = symbol_filter[:3] if len(symbol_filter) >= 6 else None
            non_base_cash = 0.0
            if base:
                for v in cash:
                    if v.currency == base:
                        non_base_cash = float(v.value)
                        break
            has_sell_stop = any(
                t.order.action == "SELL"
                and t.order.orderType in ("STP", "STP LMT")
                and symbol_filter in (t.contract.symbol, t.contract.localSymbol or "")
                for t in opens
            )
            print(f"  filter:                          {symbol_filter}")
            print(f"  appears in positions():          {in_positions}")
            print(f"  cash balance in {base or '???'} currency:    "
                  f"{non_base_cash:+.2f}")
            print(f"  has resting SELL STP:            {has_sell_stop}")
            print()
            if not in_positions and abs(non_base_cash) > 100:
                print("  ⚠️  FX CASH QUIRK CONFIRMED:")
                print("      positions() says NOTHING, but you have a real")
                print(f"      cash balance of {non_base_cash:+.0f} {base}.")
                print("      Engine's _reconcile_position_state would WRONGLY")
                print("      fold to FLAT here.")
            elif in_positions:
                print("  ✓ FX position IS in positions() — quirk not active.")
            else:
                print("  → No FX position found in either source.")
        else:
            print("  (run with --symbol EURUSD for a per-pair verdict)")

        return 0

    except (asyncio.TimeoutError, Exception) as e:
        print(f"\n[inspect] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--symbol", default=None,
                   help="Filter & verdict for one symbol (e.g., EURUSD)")
    args = p.parse_args()
    return asyncio.run(inspect(args.port, args.symbol))


if __name__ == "__main__":
    sys.exit(main())
