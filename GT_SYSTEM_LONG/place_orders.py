#!/usr/bin/env python3
"""place_orders.py — launch the REAL strategy (run_live.py) for a few symbols.

Places orders EXACTLY the way the fleet does — a BUY stop-limit entry + protective
stop bracket, via run_live.py — just for a SMALL set (e.g. 2 equity + 2 FX) instead
of the full 32. Does NOT modify run_live.py / gt_eq_test.py / the engine; it only
launches run_live, the same as gt_eq_test's `_spawn_bot`.

    venv/bin/python3 place_orders.py              # preview + confirm  (PAPER 7497)
    venv/bin/python3 place_orders.py --yes         # PAPER, no prompt
    venv/bin/python3 place_orders.py --live --yes  # LIVE / real money (7496)

Edit ORDERS / STOP_PCT below to your exact picks before running.
"""
import argparse
import asyncio
import shlex
import subprocess
import sys
from pathlib import Path

from ib_async import IB, Stock, Forex

PROJECT_ROOT = Path(__file__).resolve().parent
PY = str(PROJECT_ROOT / "venv" / "bin" / "python3")

# ─────────────────────────── CONFIG — edit these ───────────────────────────
# qty: equity = shares; fx = base-currency units (EURUSD 20000 = 20,000 EUR).
# client_id: kept clear of the bot fleet (200-231), CLEAN_CID 77, monitors.
ORDERS = [
    {"asset": "equity", "symbol": "AAPL",   "qty": 10,    "client_id": 50},
    {"asset": "equity", "symbol": "MSFT",   "qty": 10,    "client_id": 51},
    {"asset": "fx",     "symbol": "EURUSD", "qty": 20000, "client_id": 52},
    {"asset": "fx",     "symbol": "GBPUSD", "qty": 20000, "client_id": 53},
]
STOP_PCT      = 0.0005     # protective-stop %, same as the fleet (0.05%)
PAPER_PORT    = 7497
LIVE_PORT     = 7496
LTP_CID       = 49         # sidecar connection for LTP fetch (must be free)
TICK          = {"equity": 0.01,  "fx": 0.00005}   # for trigger rounding
OFFSET_FIXED  = {"equity": 0.05,  "fx": 0.0002}    # stop-limit buffer (price units)
# ────────────────────────────────────────────────────────────────────────────


def _contract(o: dict):
    return Forex(o["symbol"]) if o["asset"] == "fx" else Stock(o["symbol"], "SMART", "USD")


def _round_tick(px: float, asset: str) -> float:
    t = TICK[asset]
    return round(round(px / t) * t, 8)


async def _fetch_ltp(ib: IB, o: dict):
    qc = await ib.qualifyContractsAsync(_contract(o))
    if not qc:
        return None
    c = qc[0]
    tk = ib.reqMktData(c, "", False, False)
    try:
        for _ in range(20):                       # ~6s
            await asyncio.sleep(0.3)
            px = tk.last or tk.close or (
                (tk.bid + tk.ask) / 2 if tk.bid and tk.ask else None)
            if px and px == px:                   # not NaN
                return float(px)
    finally:
        ib.cancelMktData(c)
    return None


def _launch(o: dict, trigger: float, port: int) -> str:
    """Spawn run_live.py in a tmux session — identical pattern to gt_eq_test._spawn_bot."""
    sess = f"gt_manual_{o['symbol']}_{o['client_id']}"
    log = PROJECT_ROOT / "tests" / "paper" / "logs" / f"{o['symbol']}_{o['client_id']}_manual.log"
    cli = (
        f"GT_PAPER=false {shlex.quote(PY)} run_live.py {o['symbol']} "
        f"--trigger {trigger} --stop {STOP_PCT} --qty {o['qty']} "
        f"--offset-fixed {OFFSET_FIXED[o['asset']]} "
        f"--port {port} --client-id {o['client_id']} --uvloop"
    )
    subprocess.run(["tmux", "new-session", "-d", "-s", sess, "-c", str(PROJECT_ROOT)], check=True)
    subprocess.run(["tmux", "pipe-pane", "-o", "-t", sess, f"cat >> {shlex.quote(str(log))}"], check=False)
    subprocess.run(["tmux", "send-keys", "-t", sess, cli, "Enter"], check=True)
    return sess


async def main(live: bool, assume_yes: bool) -> int:
    port = LIVE_PORT if live else PAPER_PORT
    mode = "LIVE  ‼ REAL MONEY ‼" if live else "PAPER"

    ib = IB()
    print(f"[ltp] connecting {mode} 127.0.0.1:{port} clientId={LTP_CID}")
    try:
        await ib.connectAsync("127.0.0.1", port, clientId=LTP_CID, timeout=15)
    except Exception as e:
        print(f"[ltp] connect FAILED: {e!r}"); return 1

    plan = []
    for o in ORDERS:
        ltp = await _fetch_ltp(ib, o)
        if not ltp:
            print(f"  !! no LTP for {o['symbol']} — skipping"); continue
        trig = _round_tick(ltp, o["asset"])       # TRIGGER = LTP → fires on next tick (like fleet)
        plan.append((o, ltp, trig))
    ib.disconnect()

    if not plan:
        print("nothing to place."); return 1

    print(f"\n=== {len(plan)} STRATEGY BRACKET ENTRIES  [{mode}] ===")
    print(f"    (each = BUY stop-limit @ trigger + protective stop {STOP_PCT*100:.3f}%, via run_live.py)")
    for o, ltp, trig in plan:
        print(f"  {o['symbol']:8} {o['asset']:6} qty={o['qty']:>8}  LTP={ltp}  trigger={trig}  cid={o['client_id']}")

    if not assume_yes:
        ans = input(f"\nLaunch these {len(plan)} {mode} strategy bots? type 'yes': ").strip().lower()
        if ans != "yes":
            print("aborted — nothing launched."); return 0

    for o, _ltp, trig in plan:
        s = _launch(o, trig, port)
        print(f"  launched {s}")
    print(f"\nWatch with:  tmux ls | grep gt_manual   /   tail -f tests/paper/logs/<SYM>_<cid>_manual.log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Launch the strategy (run_live) for a few symbols.")
    ap.add_argument("--live", action="store_true", help=f"REAL MONEY (port {LIVE_PORT}). Requires --yes.")
    ap.add_argument("--yes", action="store_true", help="skip the confirm prompt")
    a = ap.parse_args()
    if a.live and not a.yes:
        print("Refusing --live without --yes (double-confirm for real money).")
        sys.exit(1)
    sys.exit(asyncio.run(main(a.live, a.yes)))
