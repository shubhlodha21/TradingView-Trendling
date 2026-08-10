"""Fetch live LTP, compute trigger, launch run_live.py with that trigger.

One-shot manual order entry. Replaces the test harness — just point at a
symbol and a config, and you get a bot running in the foreground.

USAGE:
    python3 -m tests.paper.enter_now EURUSD
    python3 -m tests.paper.enter_now EURUSD --qty 25000 --offset-bps 0.5
    python3 -m tests.paper.enter_now AAPL  --qty 10
    python3 -m tests.paper.enter_now USDJPY --qty 25000 --stop-pct 0.0005

DEFAULTS (all overridable):
    --offset-bps 0.5    (trigger = LTP * (1 + 0.5 bps))
    --stop-pct   0.001
    --port       7497
    --client-id  80
    --offset-fixed (auto-picked per symbol — 5e-4 FX, 0.05 equity, 1.0 CFD)
    --qty        (auto-picked: 25000 FX, 10 equity, 1 CFD/Futures)

The bot runs IN THE FOREGROUND, prints engine output directly to your
terminal. Ctrl-C kills it cleanly via the existing signal handlers.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import sys
from pathlib import Path

# Same tick grid as the orchestrator
_TICK_GRID: dict[str, float] = {
    "EURUSD": 1e-5, "GBPUSD": 1e-5, "AUDUSD": 1e-5, "NZDUSD": 1e-5,
    "USDCAD": 1e-5, "USDCHF": 1e-5,
    "USDJPY": 1e-3, "EURJPY": 1e-3, "GBPJPY": 1e-3,
    "AAPL": 0.01, "MSFT": 0.01, "TSLA": 0.01, "NVDA": 0.01,
    "GOOGL": 0.01, "META": 0.01, "AMZN": 0.01,
    "IBUS500": 0.25, "IBDE40": 0.5, "IBUK100": 0.5,
    "ES": 0.25, "NQ": 0.25, "CL": 0.01,
}


def _round_to_tick(px: float, symbol: str) -> float:
    tick = _TICK_GRID.get(symbol, 0.01)
    return round(round(px / tick) * tick, 8)


def _default_qty(symbol: str) -> int:
    if len(symbol) == 6 and symbol.isalpha():
        return 25000   # FX
    if symbol.startswith("IB") and len(symbol) <= 8:
        return 1       # CFD
    if symbol in ("ES", "NQ", "CL", "GC", "SI"):
        return 1       # Futures
    return 10          # Equity default


def _default_offset_fixed(symbol: str) -> float:
    if symbol.endswith("JPY"):
        return 0.05
    if len(symbol) == 6 and symbol.isalpha():
        return 0.0005  # FX non-JPY
    if symbol.startswith("IB") and len(symbol) <= 8:
        return 1.0     # CFD
    if symbol in ("ES", "NQ"):
        return 0.50
    if symbol == "CL":
        return 0.05
    return 0.05        # Equity


async def fetch_ltp(symbol: str, port: int) -> float | None:
    """Connect to IBKR briefly (clientId=98), grab LTP, disconnect."""
    try:
        from ib_async import IB, Forex, Stock, Future, CFD
    except ImportError:
        print("ERROR: ib_async not installed. Activate your venv first.",
              file=sys.stderr)
        return None

    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=98),
            timeout=8.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        print(f"ERROR: connect to TWS failed: {e}", file=sys.stderr)
        return None

    try:
        # Contract by symbol shape
        if len(symbol) == 6 and symbol.isalpha():
            contract = Forex(symbol)
        elif symbol.startswith("IB") and len(symbol) <= 8:
            contract = CFD(symbol)
        elif symbol in ("ES", "NQ"):
            contract = Future(symbol, exchange="CME")
        elif symbol == "CL":
            contract = Future(symbol, exchange="NYMEX")
        else:
            contract = Stock(symbol, "SMART", "USD")

        qualified = await asyncio.wait_for(
            ib.qualifyContractsAsync(contract), timeout=8.0,
        )
        if not qualified:
            print(f"ERROR: {symbol}: contract qualification returned empty",
                  file=sys.stderr)
            return None
        qc = qualified[0]

        ticker = ib.reqMktData(qc, "", False, False)
        for _ in range(25):  # up to 5s
            await asyncio.sleep(0.2)
            px = (ticker.last if ticker.last and ticker.last > 0 else
                  ticker.close if ticker.close and ticker.close > 0 else
                  ticker.bid if ticker.bid and ticker.bid > 0 else
                  ticker.ask if ticker.ask and ticker.ask > 0 else
                  None)
            if px:
                ib.cancelMktData(qc)
                return float(px)
        ib.cancelMktData(qc)
        print(f"ERROR: {symbol}: no price tick within 5s", file=sys.stderr)
        return None
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Fetch LTP and launch the bot with a near-market trigger.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("symbol", help="EURUSD, AAPL, USDJPY, ES, IBUS500, ...")
    p.add_argument("--offset-bps", type=float, default=0.5,
                   help="Trigger offset above LTP in basis points "
                        "(default 0.5 bps = 0.005 pct). Tiny → fires immediately.")
    p.add_argument("--qty", type=int, default=None, help="Order qty.")
    p.add_argument("--stop-pct", type=float, default=0.001, help="Stop loss pct.")
    p.add_argument("--offset-fixed", type=float, default=None,
                   help="STP-LMT limit offset below trigger (price units).")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument("--client-id", type=int, default=80)
    p.add_argument("--paper", action="store_true",
                   help="GT_PAPER=true (simulated). Default is GT_PAPER=false (live IBKR).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the command, don't run it.")
    args = p.parse_args()

    symbol = args.symbol.upper()
    qty = args.qty if args.qty is not None else _default_qty(symbol)
    offset_fixed = (args.offset_fixed if args.offset_fixed is not None
                    else _default_offset_fixed(symbol))

    print(f"[enter_now] Fetching LTP for {symbol} on port {args.port}...")
    ltp = asyncio.run(fetch_ltp(symbol, args.port))
    if ltp is None:
        print("[enter_now] aborted — no LTP available.", file=sys.stderr)
        return 1

    trigger = _round_to_tick(ltp * (1 + args.offset_bps / 10_000.0), symbol)
    print(f"[enter_now] {symbol}: LTP={ltp}  trigger={trigger}  "
          f"(+{args.offset_bps} bps)  qty={qty}  stop={args.stop_pct}  "
          f"offset_fixed={offset_fixed}  client_id={args.client_id}")

    # Build the actual command
    project_root = Path(__file__).resolve().parents[2]
    cli = [
        "python3", str(project_root / "run_live.py"), symbol,
        "--trigger", str(trigger),
        "--stop", str(args.stop_pct),
        "--offset-fixed", str(offset_fixed),
        "--qty", str(qty),
        "--port", str(args.port),
        "--client-id", str(args.client_id),
        "--uvloop",
    ]
    env_prefix = "GT_PAPER=true " if args.paper else "GT_PAPER=false "
    full_cmd = env_prefix + " ".join(shlex.quote(c) for c in cli)
    print(f"\n[enter_now] launching:")
    print(f"  {full_cmd}\n")

    if args.dry_run:
        print("[enter_now] --dry-run: not launching.")
        return 0

    # Replace this process with the bot — clean Ctrl-C, no orchestrator
    # wrapper in the way. The shell-style env-var prefix needs os.environ.
    env = dict(os.environ)
    env["GT_PAPER"] = "true" if args.paper else "false"
    os.execvpe("python3", cli, env)


if __name__ == "__main__":
    sys.exit(main())
