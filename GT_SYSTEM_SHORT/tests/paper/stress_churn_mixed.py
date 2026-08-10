"""MIXED stress-churn driver — 16 equities + 16 FX pairs in one fleet.

A80 (2026-06-12): the FX (`stress_churn.py`) and equity
(`stress_churn_equity.py`) drivers are asset-pure. This module runs BOTH
asset classes in a single 32-bot fleet, routing every asset-specific
operation (LTP fetch, tick grid, entry offset, flatten) on a per-symbol
`asset` tag. `run_live.py` itself is already multi-asset (SpecRegistry),
so the bot launch command is identical for both — only the driver-side
broker helpers differ.

The two asset-pure drivers are left UNTOUCHED — this is a new module so
the "perfection achieved in currency" is unaffected.

Client IDs 80-111 (32 bots). Every symbol is unique, so the A79
single-writer lock never collides within the fleet.
"""

from __future__ import annotations

import asyncio
import os as _os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ────────────────────────────────────────────────────────────────────────────
# Fleet — 16 equities (CIDs 80-95) + 16 FX pairs (CIDs 96-111)
# Each entry carries an `asset` tag: 'STK' or 'FX'. Everything asset-specific
# routes on this tag.
# ────────────────────────────────────────────────────────────────────────────

PAIRS = [
    # ── 16 EQUITIES (CIDs 80-95) ──
    {"symbol": "NVDA",  "client_id": 80, "qty": 100,   "asset": "STK"},
    {"symbol": "AAPL",  "client_id": 81, "qty": 100,   "asset": "STK"},
    {"symbol": "MSFT",  "client_id": 82, "qty": 100,   "asset": "STK"},
    {"symbol": "GOOGL", "client_id": 83, "qty": 100,   "asset": "STK"},
    {"symbol": "AMZN",  "client_id": 84, "qty": 100,   "asset": "STK"},
    {"symbol": "META",  "client_id": 85, "qty": 100,   "asset": "STK"},
    {"symbol": "AVGO",  "client_id": 86, "qty": 100,   "asset": "STK"},
    {"symbol": "TSLA",  "client_id": 87, "qty": 100,   "asset": "STK"},
    {"symbol": "JPM",   "client_id": 88, "qty": 100,   "asset": "STK"},
    {"symbol": "V",     "client_id": 89, "qty": 100,   "asset": "STK"},
    {"symbol": "MA",    "client_id": 90, "qty": 100,   "asset": "STK"},
    {"symbol": "WMT",   "client_id": 91, "qty": 100,   "asset": "STK"},
    {"symbol": "COST",  "client_id": 92, "qty": 100,   "asset": "STK"},
    {"symbol": "HD",    "client_id": 93, "qty": 100,   "asset": "STK"},
    {"symbol": "LLY",   "client_id": 94, "qty": 100,   "asset": "STK"},
    # A82: dropped NFLX (was cid 95) — fleet trimmed 16→15 STK so the
    # total stays ≤ 30 bots, leaving API connection slots free for the
    # three-truths monitor (+ a transient sidecar) under TWS's 32 cap.
    # ── 15 FX (CIDs 96-110) ──
    {"symbol": "EURUSD", "client_id": 96,  "qty": 25000, "asset": "FX"},
    {"symbol": "GBPUSD", "client_id": 97,  "qty": 25000, "asset": "FX"},
    {"symbol": "USDJPY", "client_id": 98,  "qty": 25000, "asset": "FX"},
    {"symbol": "AUDUSD", "client_id": 99,  "qty": 25000, "asset": "FX"},
    {"symbol": "USDCHF", "client_id": 100, "qty": 25000, "asset": "FX"},
    {"symbol": "USDCAD", "client_id": 101, "qty": 25000, "asset": "FX"},
    {"symbol": "NZDUSD", "client_id": 102, "qty": 25000, "asset": "FX"},
    {"symbol": "EURJPY", "client_id": 103, "qty": 25000, "asset": "FX"},
    {"symbol": "EURGBP", "client_id": 104, "qty": 25000, "asset": "FX"},
    {"symbol": "EURCHF", "client_id": 105, "qty": 25000, "asset": "FX"},
    {"symbol": "GBPJPY", "client_id": 106, "qty": 25000, "asset": "FX"},
    {"symbol": "AUDJPY", "client_id": 107, "qty": 25000, "asset": "FX"},
    {"symbol": "EURAUD", "client_id": 108, "qty": 25000, "asset": "FX"},
    {"symbol": "GBPCHF", "client_id": 109, "qty": 25000, "asset": "FX"},
    {"symbol": "CADJPY", "client_id": 110, "qty": 25000, "asset": "FX"},
    # A82: dropped CHFJPY (was cid 111) — keeps the 15 STK + 15 FX balance
    # at 30 bots total. CIDs used: 80-94 (STK) + 96-110 (FX). Sidecars at
    # 78/79, monitor at 177 — all comfortably under the 32 connection cap.
]

# Per-symbol asset lookup — the heart of the routing.
_ASSET = {p["symbol"]: p["asset"] for p in PAIRS}
_STRESS_SYMBOLS = {p["symbol"] for p in PAIRS}
_STRESS_STK = {p["symbol"] for p in PAIRS if p["asset"] == "STK"}
_STRESS_FX = {p["symbol"] for p in PAIRS if p["asset"] == "FX"}

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

STOP_PCT = 0.00005          # 0.5 bp — fires within seconds in normal vol
TRIGGER_OFFSET_BPS = 0      # enter AT the LTP so every bot fires first tick
OFFSET_FIXED_STK = 0.05     # equity parent limit = trigger - $0.05
OFFSET_FIXED_FX = 0.0005    # FX parent limit = trigger - 5 pips
OFFSET_FIXED_FX_JPY = 0.05  # JPY pairs quote ~150, so 0.05 = ~5 pips

WATCHDOG_CID = 79
LTP_FETCH_CID = 78
PORT = int(_os.environ.get("GT_IBKR_PORT", "7497"))

# Tick grid — equities $0.01, FX 1e-5 (non-JPY) / 1e-3 (JPY).
_TICK = {
    # equities
    "NVDA": 0.01, "AAPL": 0.01, "MSFT": 0.01, "GOOGL": 0.01, "AMZN": 0.01,
    "META": 0.01, "AVGO": 0.01, "TSLA": 0.01, "JPM": 0.01, "V": 0.01,
    "MA": 0.01, "WMT": 0.01, "COST": 0.01, "HD": 0.01, "LLY": 0.01, "NFLX": 0.01,
    # FX non-JPY
    "EURUSD": 1e-5, "GBPUSD": 1e-5, "AUDUSD": 1e-5, "USDCHF": 1e-5,
    "USDCAD": 1e-5, "NZDUSD": 1e-5, "EURGBP": 1e-5, "EURCHF": 1e-5,
    "GBPCHF": 1e-5, "EURAUD": 1e-5,
    # FX JPY
    "USDJPY": 1e-3, "EURJPY": 1e-3, "GBPJPY": 1e-3, "AUDJPY": 1e-3,
    "CADJPY": 1e-3, "CHFJPY": 1e-3,
}

# Equity primaryExchange disambiguation (A74). Single-letter / collision-prone.
_PRIMARY_EXCHANGE = {"V": "NYSE", "MA": "NYSE"}


def _is_jpy(sym: str) -> bool:
    return sym.endswith("JPY")


def _round_to_tick(px: float, symbol: str) -> float:
    t = _TICK.get(symbol, 0.01 if _ASSET.get(symbol) == "STK" else 1e-5)
    return round(round(px / t) * t, 8)


def _primary_exchange_for(symbol: str) -> str:
    return _PRIMARY_EXCHANGE.get(symbol, "")


def _offset_fixed_for(sym: str) -> float:
    if _ASSET.get(sym) == "STK":
        return OFFSET_FIXED_STK
    return OFFSET_FIXED_FX_JPY if _is_jpy(sym) else OFFSET_FIXED_FX


# ────────────────────────────────────────────────────────────────────────────
# LTP fetch — asset-routed contract construction
# ────────────────────────────────────────────────────────────────────────────

async def _fetch_ltp(ib, symbol: str) -> Optional[float]:
    if _ASSET.get(symbol) == "FX":
        from ib_async import Forex
        contract = Forex(symbol)
    else:
        from ib_async import Stock
        pex = _primary_exchange_for(symbol)
        if pex:
            contract = Stock(symbol, exchange="SMART", currency="USD",
                             primaryExchange=pex)
        else:
            contract = Stock(symbol, exchange="SMART", currency="USD")
    try:
        q = await asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout=4.0)
    except (asyncio.TimeoutError, Exception):
        return None
    if not q:
        return None
    qc = q[0]
    ticker = ib.reqMktData(qc, "", False, False)
    try:
        for _ in range(20):
            await asyncio.sleep(0.2)
            px = (
                ticker.last if ticker.last and ticker.last > 0 else
                ticker.close if ticker.close and ticker.close > 0 else
                ticker.bid if ticker.bid and ticker.bid > 0 else
                ticker.ask if ticker.ask and ticker.ask > 0 else
                None
            )
            if px:
                return float(px)
        return None
    finally:
        try:
            ib.cancelMktData(qc)
        except Exception:
            pass


async def _resolve_all_ltps(port: int) -> dict[str, Optional[float]]:
    from ib_async import IB
    ib = IB()
    out: dict[str, Optional[float]] = {p["symbol"]: None for p in PAIRS}
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=LTP_FETCH_CID),
            timeout=10.0,
        )
        for p in PAIRS:
            ltp = await _fetch_ltp(ib, p["symbol"])
            out[p["symbol"]] = ltp
            print(f"  [ltp] {p['symbol']:7s} ({p['asset']:3s})  LTP={ltp}")
    except Exception as e:
        print(f"  [ltp] connect/fetch error: {e}", file=sys.stderr)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return out


# ────────────────────────────────────────────────────────────────────────────
# Flatten — asset-routed per position. (Name kept `_flatten_all_fx` for
# import-compat with the chaos_test scaffolding.)
# ────────────────────────────────────────────────────────────────────────────

_FLEET_CCY = {"EUR", "GBP", "JPY", "AUD", "CHF", "CAD", "NZD"}


async def _flatten_all_fx(port: int) -> tuple[int, list[str]]:
    """Close every open position in the mixed stress universe.

    Routes per position secType:
      - STK  → opposite-side MarketOrder(tif='DAY')   (A69)
      - CASH → opposite-side MarketOrder(tif='IOC') on the Forex pair (A66)
    A single retry pass catches anything unfilled after the first sweep.
    """
    from ib_async import IB, Stock, Forex, MarketOrder
    errors: list[str] = []
    placed = 0
    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=WATCHDOG_CID),
            timeout=10.0,
        )
        await asyncio.sleep(2.0)
        try:
            ib.reqGlobalCancel()
            await asyncio.sleep(2.0)
            print("  reqGlobalCancel() issued, 2s settle")
        except Exception as e:
            errors.append(f"reqGlobalCancel: {type(e).__name__}: {e}")

        async def _sweep(label: str) -> int:
            nonlocal errors
            n = 0
            for pos in ib.positions():
                c = pos.contract
                sec = getattr(c, "secType", "") or ""
                sym = getattr(c, "symbol", "") or ""
                # FX positions report as CASH with localSymbol like 'EUR.USD';
                # map to our 6-char pair symbol.
                pair = sym
                if sec == "CASH":
                    ls = (getattr(c, "localSymbol", "") or "").replace(".", "")
                    pair = ls or sym
                if pair not in _STRESS_SYMBOLS:
                    continue
                qty_signed = float(pos.position or 0)
                if abs(qty_signed) < 1.0:
                    continue
                action = "SELL" if qty_signed > 0 else "BUY"
                qty = int(abs(qty_signed))
                try:
                    if _ASSET.get(pair) == "FX" or sec == "CASH":
                        contract = Forex(pair)
                        tif = "IOC"
                    else:
                        contract = c  # already a qualified Stock
                        tif = "DAY"
                    qel = await asyncio.wait_for(
                        ib.qualifyContractsAsync(contract), timeout=4.0)
                    if not qel:
                        errors.append(f"flatten {pair}: qualify empty")
                        continue
                    ib.placeOrder(qel[0], MarketOrder(action, qty, tif=tif))
                    n += 1
                    print(f"    [{label}] → {action} {qty:>6} {pair:<7s} "
                          f"({_ASSET.get(pair,'?')}, tif={tif})")
                except Exception as e:
                    errors.append(f"flatten {pair} {action} {qty}: "
                                  f"{type(e).__name__}: {e}")
            return n

        placed += await _sweep("pass1")
        if placed:
            print(f"  waiting 6s for {placed} flatten order(s)…")
            await asyncio.sleep(6.0)
            retry = await _sweep("retry")
            if retry:
                placed += retry
                print(f"  retry placed {retry}; waiting 5s…")
                await asyncio.sleep(5.0)

        # Final truth
        still = []
        for pos in ib.positions():
            c = pos.contract
            sec = getattr(c, "secType", "") or ""
            sym = getattr(c, "symbol", "") or ""
            pair = sym
            if sec == "CASH":
                pair = (getattr(c, "localSymbol", "") or "").replace(".", "") or sym
            if pair in _STRESS_SYMBOLS and abs(float(pos.position or 0)) >= 1.0:
                still.append((pair, float(pos.position)))
        print("  post-flatten:", "ALL FLAT ✓" if not still else
              " ".join(f"{s}={q:+.0f}" for s, q in still))
    except Exception as e:
        errors.append(f"flatten connect: {type(e).__name__}: {e}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return placed, errors


async def _cancel_open_orders_for(ib, symbol: str, log: list) -> int:
    """Cancel every open order at the broker matching this symbol (STK or FX)."""
    cancelled = 0
    want_fx = _ASSET.get(symbol) == "FX"
    try:
        for trade in ib.openTrades():
            c = trade.contract
            sec = getattr(c, "secType", "") or ""
            sym = getattr(c, "symbol", "") or ""
            pair = sym
            if sec == "CASH":
                pair = (getattr(c, "localSymbol", "") or "").replace(".", "") or sym
            match = (pair == symbol) and (
                (want_fx and sec == "CASH") or (not want_fx and sec == "STK"))
            if match:
                try:
                    ib.cancelOrder(trade.order)
                    cancelled += 1
                except Exception as e:
                    log.append(f"cancel {symbol}: {type(e).__name__}: {e}")
    except Exception as e:
        log.append(f"openTrades walk: {type(e).__name__}: {e}")
    return cancelled


async def _watchdog_iteration(port: int, log: list) -> dict[str, int]:
    """Cancel-only sweep across the whole mixed universe."""
    from ib_async import IB
    counts = {"cancelled": 0}
    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=WATCHDOG_CID),
            timeout=10.0,
        )
        await asyncio.sleep(1.5)
        for sym in _STRESS_SYMBOLS:
            counts["cancelled"] += await _cancel_open_orders_for(ib, sym, log)
    except Exception as e:
        log.append(f"watchdog connect: {type(e).__name__}: {e}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return counts


# ────────────────────────────────────────────────────────────────────────────
# tmux / process helpers (generic)
# ────────────────────────────────────────────────────────────────────────────

def _tmux(args: list[str], check: bool = True) -> None:
    subprocess.run(["tmux", *args], check=check,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _kill_stale_sessions() -> None:
    try:
        out = subprocess.run(["tmux", "ls"], capture_output=True, text=True).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        name = line.split(":", 1)[0]
        if name.startswith("gt_churn_"):
            _tmux(["kill-session", "-t", name], check=False)
    cids = [str(p["client_id"]) for p in PAIRS]
    print(f"  killed stale gt_churn_* sessions for client_ids {cids[0]}-{cids[-1]}")


def _cleanup_state_files() -> None:
    n = 0
    for p in PAIRS:
        for tmpl in (f".gt_state_{p['symbol']}_{p['client_id']}.json",
                     f".gt_live_{p['symbol']}_{p['client_id']}.json"):
            fp = PROJECT_ROOT / tmpl
            if fp.exists():
                try:
                    fp.unlink(); n += 1
                except Exception:
                    pass
    print(f"  cleaned {n} state/live files")


def _python_bin() -> str:
    return sys.executable or "python3"


def _spawn_bot(p: dict, trigger, log_dir: Path) -> tuple[str, Path]:
    """Spawn one engine bot in a named tmux session. Identical command for
    FX and equity — run_live.py routes the instrument internally. Only the
    --offset-fixed value is asset-specific."""
    session = f"gt_churn_{p['symbol']}_{p['client_id']}"
    log_path = log_dir / f"{p['symbol']}_{p['client_id']}_{int(time.time())}.log"
    offset_fixed = _offset_fixed_for(p["symbol"])
    py = _python_bin()
    trigger_flag = f"--trigger {trigger} " if trigger is not None else ""
    cli = (
        f"GT_PAPER=false "
        f"GT_SKIP_NAKED_GUARD=1 "
        f"GT_DISABLE_RISK_GATE=1 "
        f"{shlex.quote(py)} run_live.py "
        f"{p['symbol']} "
        f"{trigger_flag}--stop {STOP_PCT} "
        f"--offset-fixed {offset_fixed} --qty {p['qty']} "
        f"--port {PORT} --client-id {p['client_id']} --uvloop"
    )
    _tmux(["new-session", "-d", "-s", session, "-c", str(PROJECT_ROOT)])
    _tmux(["pipe-pane", "-o", "-t", session,
           f"cat >> {shlex.quote(str(log_path))}"])
    _tmux(["send-keys", "-t", session, cli, "Enter"])
    return session, log_path


def _shutdown_bot(session: str) -> None:
    _tmux(["send-keys", "-t", session, "C-c"], check=False)


def _today_audit_dir(symbol: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return PROJECT_ROOT / "data" / "audit" / today / symbol
