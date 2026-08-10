#!/usr/bin/env python3
"""SAFE CLEAR — square the FX fleet to a clean baseline WITHOUT a positions()
flatten.

Why no flatten: positions() is unreliable for FX (it shows phantom per-pair
shorts that the broker's own execution log contradicts). A flatten that reads
positions() would "cover" a phantom -25k by BUYING 25k → a REAL long. So this
tool NEVER places offsetting orders. It only:

  1. Cancels resting orders for FX (secType == CASH) ONLY.
     STK / FUT / anything else (e.g. the manual SPCX ticker) is PROTECTED —
     never cancelled.
  2. Reports the broker's reqExecutions NET per symbol — the authoritative
     "are we actually flat?" check. NET == 0 ⇒ genuinely flat regardless of
     what positions() claims.

Run AFTER killing the bots/orchestrator (so nothing replaces the orders):
    venv/bin/python scripts/safe_clear.py --port 7497
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

_LIVE = {"Submitted", "PreSubmitted", "PendingSubmit"}


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=219)
    args = p.parse_args()

    from ib_async import IB, ExecutionFilter
    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", args.port, clientId=args.client_id),
            timeout=12.0)
    except Exception as e:
        print(f"CONNECT FAILED: {type(e).__name__}: {e!r}")
        return 1
    await asyncio.sleep(1.5)

    # 1) cancel resting FX (CASH) orders only — protect SPCX / any non-CASH
    await ib.reqAllOpenOrdersAsync()
    cancelled = protected = 0
    for t in ib.openTrades():
        st = t.orderStatus.status if t.orderStatus else ""
        if st not in _LIVE:
            continue
        sec = t.contract.secType
        sym = t.contract.localSymbol or t.contract.symbol or "?"
        if sec != "CASH":
            protected += 1
            print(f"  PROTECT  {sym:10s} [{sec}] {t.order.action} {st} — NOT cancelling")
            continue
        try:
            ib.cancelOrder(t.order)
            cancelled += 1
            print(f"  CANCEL   {sym:10s} {t.order.action} {st}")
        except Exception as e:
            print(f"  CANCEL FAIL {sym}: {e}")
    print(f"\n  cancelled {cancelled} FX order(s); protected {protected} non-FX order(s)")
    await asyncio.sleep(2.5)

    # 2) authoritative flat check — broker reqExecutions NET per symbol
    execs = await asyncio.wait_for(
        ib.reqExecutionsAsync(ExecutionFilter()), timeout=15.0)
    net: dict = defaultdict(float)
    for f in execs:
        c = f.contract
        sym = (c.localSymbol or c.symbol or "").replace(".", "")
        sgn = 1 if f.execution.side == "BOT" else -1
        net[sym] += sgn * float(f.execution.shares or 0)
    nz = {s: v for s, v in net.items() if abs(v) > 1e-9}
    print("\n=== broker reqExecutions NET (authoritative flat check) ===")
    if not nz:
        print("  ALL symbols net 0 across today's executions → genuinely FLAT ✓")
    else:
        for s, v in sorted(nz.items()):
            print(f"  {s:10s} net={v:>+12.0f}   ⚠ NON-ZERO — real position, investigate")

    # also show what positions() claims, for the contrast
    poss = [(((pp.contract.localSymbol or pp.contract.symbol or "").replace(".", "")),
             float(pp.position or 0)) for pp in ib.positions()
            if abs(float(pp.position or 0)) > 1e-9]
    if poss:
        print("\n  (positions() still claims — likely phantom if execNet==0):")
        for s, q in sorted(poss):
            print(f"    {s:10s} {q:>+12.0f}")
    ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
