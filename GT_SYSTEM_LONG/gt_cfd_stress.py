#!/usr/bin/env python3
"""gt_cfd_stress.py — STANDALONE CFD bracket placer (self-contained stress fleet).

This does NOT use run_live / the strategy engine. Everything lives in this one
file. It places real CFD bracket orders (BUY stop-limit entry + protective SELL
stop) directly via ib_async, keeps them running for days, and re-arms a symbol
when it goes flat — so order flow is continuous.

WHY a standalone placer: on this paper account the CFD market-data line is dark
for US SHARE CFDs (CFD("AAPL") streams NaN) while the underlying Stock streams
fine. INDEX / metal / energy CFDs DO stream their price straight from the CFD
line. So the PRICE SOURCE is per-type:
    * share CFD   -> price from the underlying Stock(symbol)   (CFD line dark)
    * index/metal -> price STRAIGHT from the CFD contract itself
ORDERS always route to the CFD contract.

    venv/bin/python3 gt_cfd_stress.py --dry-run    # qualify + price + plan, place NOTHING
    venv/bin/python3 gt_cfd_stress.py              # preview + confirm, then place (PAPER 7497)
    venv/bin/python3 gt_cfd_stress.py --yes         # no prompt (unattended / days)

Watch:  (this is one process) tail its tmux/log; or run under tmux:
    tmux new -d -s gt_cfd -c ~/GT_TESTING_CFD 'venv/bin/python3 gt_cfd_stress.py --yes 2>&1 | tee tests/paper/logs/gt_cfd_stress.log'
Stop :  Ctrl-C (resting orders persist at IBKR) or tmux kill-session -t gt_cfd
"""
import argparse
import asyncio
import math
import sys
from pathlib import Path

from ib_async import IB, CFD, Stock, Order

PROJECT_ROOT = Path(__file__).resolve().parent

# ─────────────────────────── CONFIG ───────────────────────────
PAPER_PORT  = 7497
LIVE_PORT   = 7496
CLIENT_ID   = 300          # single connection for the whole fleet (clear of 200-231 / 49-53)
STOP_PCT    = 0.0005       # protective stop = entry * (1 - 0.05%)
REARM_SECS  = 20           # re-check + re-arm flat symbols every N seconds
PRICE_WAIT  = 12.0         # seconds to wait for first price per symbol

# straight=True  -> price comes STRAIGHT from the CFD line (index/metal/energy)
# straight=False -> price comes from the underlying Stock(symbol) (share CFDs)
CFD_UNIVERSE = [
    # ── Index / metal / energy CFDs — price straight from the CFD ──
    {"symbol": "IBUS500",  "ccy": "USD", "qty": 1, "tick": 0.25, "offset": 1.0, "straight": True},
    {"symbol": "IBUS30",   "ccy": "USD", "qty": 1, "tick": 1.0,  "offset": 2.0, "straight": True},
    {"symbol": "IBUST100", "ccy": "USD", "qty": 1, "tick": 0.25, "offset": 1.0, "straight": True},
    {"symbol": "IBDE40",   "ccy": "EUR", "qty": 1, "tick": 0.5,  "offset": 1.0, "straight": True},
    {"symbol": "IBGB100",  "ccy": "GBP", "qty": 1, "tick": 0.5,  "offset": 1.0, "straight": True},
    {"symbol": "IBJP225",  "ccy": "JPY", "qty": 1, "tick": 5.0,  "offset": 10.0,"straight": True},
    {"symbol": "IBEU50",   "ccy": "EUR", "qty": 1, "tick": 1.0,  "offset": 2.0, "straight": True},
    {"symbol": "XAUUSD",   "ccy": "USD", "qty": 1, "tick": 0.01, "offset": 0.5, "straight": True},
    {"symbol": "XAGUSD",   "ccy": "USD", "qty": 1, "tick": 0.001,"offset": 0.05,"straight": True},
    {"symbol": "IBUKOIL",  "ccy": "USD", "qty": 1, "tick": 0.01, "offset": 0.1, "straight": True},
    # ── US share CFDs — price from the underlying Stock (CFD line is dark) ──
    {"symbol": "AAPL",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "MSFT",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "TSLA",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "NVDA",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "AMZN",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "GOOGL", "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "META",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "NFLX",  "ccy": "USD", "qty": 5,  "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "AMD",   "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "INTC",  "ccy": "USD", "qty": 20, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "JPM",   "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "BAC",   "ccy": "USD", "qty": 20, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "DIS",   "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "KO",    "ccy": "USD", "qty": 15, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "PEP",   "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "ORCL",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "CSCO",  "ccy": "USD", "qty": 20, "tick": 0.01, "offset": 0.05, "straight": False},
    {"symbol": "QCOM",  "ccy": "USD", "qty": 10, "tick": 0.01, "offset": 0.05, "straight": False},
]
# ───────────────────────────────────────────────────────────────


def _order_contract(o: dict):
    """Orders ALWAYS go to the CFD contract."""
    return CFD(o["symbol"].upper(), "SMART", o["ccy"])


def _data_contract(o: dict):
    """Price source: CFD itself for index/metal (straight), underlying Stock for shares."""
    if o["straight"]:
        return CFD(o["symbol"].upper(), "SMART", o["ccy"])
    return Stock(o["symbol"].upper(), "SMART", "USD")


def _ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and not math.isnan(x) and x > 0


def _round_tick(px: float, tick: float) -> float:
    return round(round(px / tick) * tick, 8)


def _price_from_ticker(tk) -> float | None:
    """NaN-safe, bid/ask-MID first (CFDs/quote-driven have no `last`)."""
    if _ok(getattr(tk, "bid", None)) and _ok(getattr(tk, "ask", None)):
        return (float(tk.bid) + float(tk.ask)) / 2
    for f in ("last", "close", "bid", "ask"):
        v = getattr(tk, f, None)
        if _ok(v):
            return float(v)
    return None


def _bracket_orders(o: dict, trigger: float):
    """BUY stop-limit entry @ trigger + protective SELL stop @ trigger*(1-STOP_PCT).
    Native IBKR bracket via parentId; child arms when the parent fills."""
    qty = o["qty"]
    lmt = _round_tick(trigger + o["offset"], o["tick"])      # buy limit just above trigger
    stop_px = _round_tick(trigger * (1 - STOP_PCT), o["tick"])
    parent = Order(action="BUY", orderType="STP LMT", totalQuantity=qty,
                   auxPrice=trigger, lmtPrice=lmt, tif="GTC",
                   transmit=False, outsideRth=True)
    child = Order(action="SELL", orderType="STP", totalQuantity=qty,
                  auxPrice=stop_px, tif="GTC", transmit=True, outsideRth=True)
    return parent, child, lmt, stop_px


async def _subscribe(ib: IB, o: dict):
    """Qualify the data contract + order contract, start the data stream.
    Returns (ticker, order_contract) or (None, None) if not tradable."""
    try:
        oc = await ib.qualifyContractsAsync(_order_contract(o))
        if not oc:
            print(f"  !! {o['symbol']:8} CFD not tradable on this account — skip")
            return None, None
        dc = await ib.qualifyContractsAsync(_data_contract(o))
        if not dc:
            print(f"  !! {o['symbol']:8} data contract no-qualify — skip")
            return None, None
    except Exception as e:
        print(f"  !! {o['symbol']:8} qualify FAILED: {e!r}")
        return None, None
    tk = ib.reqMktData(dc[0], "", False, False)
    return tk, oc[0]


def _open_for(ib: IB, sym: str):
    """(has_open_cfd_position, has_active_cfd_order) for a symbol."""
    pos = any(
        getattr(p.contract, "secType", "") == "CFD"
        and (p.contract.symbol or "").upper() == sym
        and abs(p.position) > 0
        for p in ib.positions()
    )
    active = any(
        getattr(t.contract, "secType", "") == "CFD"
        and (t.contract.symbol or "").upper() == sym
        and t.orderStatus.status in ("PendingSubmit", "PreSubmitted", "Submitted", "ApiPending")
        for t in ib.openTrades()
    )
    return pos, active


def _place_bracket(ib: IB, o: dict, contract, trigger: float):
    parent, child, lmt, stop_px = _bracket_orders(o, trigger)
    pt = ib.placeOrder(contract, parent)
    child.parentId = pt.order.orderId
    ct = ib.placeOrder(contract, child)
    return pt, ct, lmt, stop_px


async def main(live: bool, assume_yes: bool, dry_run: bool, limit: int) -> int:
    port = LIVE_PORT if live else PAPER_PORT
    mode = "LIVE  ‼ REAL MONEY ‼" if live else "PAPER"
    universe = CFD_UNIVERSE[:limit] if limit else CFD_UNIVERSE

    ib = IB()
    errs: list = []
    ib.errorEvent += lambda reqId, code, msg, *a: errs.append((code, str(msg)[:70]))
    print(f"[connect] {mode} 127.0.0.1:{port} clientId={CLIENT_ID}")
    try:
        await ib.connectAsync("127.0.0.1", port, clientId=CLIENT_ID, timeout=15)
    except Exception as e:
        print(f"[connect] FAILED: {e!r}")
        return 1
    ib.reqMarketDataType(1)

    # Subscribe everything, then wait once for prices to populate (concurrent).
    print(f"\n[resolve] qualifying {len(universe)} CFDs + starting data streams …")
    subs = []
    for o in universe:
        tk, oc = await _subscribe(ib, o)
        if tk is not None:
            subs.append((o, tk, oc))
    if not subs:
        print("nothing qualified — abort."); ib.disconnect(); return 1

    deadline = PRICE_WAIT
    while deadline > 0:
        await asyncio.sleep(0.5); deadline -= 0.5
        if all(_price_from_ticker(tk) for _o, tk, _oc in subs):
            break

    plan = []
    for o, tk, oc in subs:
        px = _price_from_ticker(tk)
        if not px:
            src = "CFD" if o["straight"] else "Stock"
            print(f"  !! {o['symbol']:8} no price from {src} in {PRICE_WAIT:.0f}s — skip")
            continue
        plan.append((o, oc, px, _round_tick(px, o["tick"])))

    if not plan:
        print("\nno prices — nothing to place."); ib.disconnect(); return 1

    print(f"\n=== {len(plan)} CFD BRACKETS  [{mode}] ===")
    print(f"    (BUY stop-limit @ trigger + protective SELL stop {STOP_PCT*100:.3f}% below; orders→CFD)")
    for o, _oc, px, trig in plan:
        src = "CFD(straight)" if o["straight"] else "Stock(underlying)"
        _p, _c, lmt, stop_px = _bracket_orders(o, trig)
        print(f"  {o['symbol']:8} {o['ccy']} qty={o['qty']:>3} price={px:<11.4f} "
              f"trig={trig:<11.4f} lmt={lmt:<11.4f} stop={stop_px:<11.4f} src={src}")

    if dry_run:
        print(f"\n[dry-run] {len(plan)}/{len(universe)} priced. Placed NOTHING.")
        if errs:
            print("IBKR messages:", errs[-8:])
        ib.disconnect()
        return 0 if len(plan) >= 24 else 2

    if not assume_yes:
        ans = input(f"\nPlace these {len(plan)} {mode} CFD brackets (runs for days, re-arms)? type 'yes': ").strip().lower()
        if ans != "yes":
            print("aborted — nothing placed."); ib.disconnect(); return 0

    placed = 0
    for o, oc, _px, trig in plan:
        try:
            pt, ct, lmt, stop_px = _place_bracket(ib, o, oc, trig)
            placed += 1
            print(f"  placed {o['symbol']:8} entry#{pt.order.orderId} stop#{ct.order.orderId} "
                  f"trig={trig} stop={stop_px}")
            await asyncio.sleep(0.05)   # 50ms ingest gap (avoids Error 135 race)
        except Exception as e:
            print(f"  !! {o['symbol']:8} placeOrder FAILED: {e!r}")
    print(f"\n{placed} CFD brackets placed. Monitoring + re-arming every {REARM_SECS}s. Ctrl-C to stop.")

    # ── Keep running for days; re-arm any symbol that goes flat ──
    syms = [(o, oc) for o, oc, _px, _t in plan]
    try:
        while True:
            await asyncio.sleep(REARM_SECS)
            for o, oc in syms:
                sym = o["symbol"].upper()
                has_pos, has_order = _open_for(ib, sym)
                if has_pos or has_order:
                    continue
                # flat + nothing resting → re-arm at the current price
                tk = next((t for oo, t, _c in subs if oo["symbol"] == o["symbol"]), None)
                px = _price_from_ticker(tk) if tk is not None else None
                if not px:
                    continue
                trig = _round_tick(px, o["tick"])
                try:
                    pt, ct, lmt, stop_px = _place_bracket(ib, o, oc, trig)
                    print(f"  [re-arm] {sym:8} entry#{pt.order.orderId} trig={trig} stop={stop_px}")
                    await asyncio.sleep(0.05)
                except Exception as e:
                    print(f"  !! [re-arm] {sym} FAILED: {e!r}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[stop] disconnecting — resting orders persist at IBKR.")
    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Standalone CFD bracket placer (index straight + share via underlying).")
    ap.add_argument("--live", action="store_true", help=f"REAL MONEY (port {LIVE_PORT}). Requires --yes.")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt (unattended)")
    ap.add_argument("--dry-run", action="store_true", help="qualify + price + plan only; place NOTHING")
    ap.add_argument("--limit", type=int, default=0, help="cap symbols (0 = all)")
    a = ap.parse_args()
    if a.live and not a.yes:
        print("Refusing --live without --yes (double-confirm for real money).")
        sys.exit(1)
    sys.exit(asyncio.run(main(a.live, a.yes, a.dry_run, a.limit)))
