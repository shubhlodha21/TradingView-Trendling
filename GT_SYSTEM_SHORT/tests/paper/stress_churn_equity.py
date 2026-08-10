"""STRESS_CHURN — drive the strategy engine across 8 FX pairs concurrently.

Spawns N engine bots (one per pair) with very tight params so the engine
naturally cycles enter→fill→SL→re-entry. A watchdog every 10s force-cancels
all open orders + flattens any open positions via a sidecar IBKR connection
(simulates the "cancel everything no matter what" requirement).

This is a STRATEGY-ENGINE end-to-end test: tests the real code path through
the strategy engine, bracket placement, fill events, reconcile-on-cancel,
state persistence, and re-entry logic. Captures performance metrics from
the engine's audit logs (data/audit/<DATE>/<PAIR>/order.csv) and emits a
CSV + terminal summary for handoff to a senior reviewer.

USAGE:
    python3 -m tests.paper.stress_churn                          # 5min default
    python3 -m tests.paper.stress_churn --duration 600           # 10min
    python3 -m tests.paper.stress_churn --watchdog 5 --duration 120
    python3 -m tests.paper.stress_churn --pairs EURUSD,USDJPY    # subset
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import shlex
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ────────────────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────────────────

# ════════════════════════════════════════════════════════════════════════════
# EQUITY STRESS — S&P top names, client_ids 80..95 (16 bots).
#
# Forked from the FX stress_churn.py 2026-06-10 after the A52-A66 chaos-test
# stack proved the engine clean on 8 FX pairs (Test 3 PASS, zero phantoms,
# zero reconcile-cancels). This file targets US equities to validate the
# same defenses on a different asset class + push concurrency.
#
# Tickers chosen: top S&P names by market cap, all clean symbols (no dots).
# qty=100 shares each → ~$15k-30k notional per position. Stops/triggers
# tuned for equity tick grid ($0.01 minimum, vs FX 0.5 pip).
# ════════════════════════════════════════════════════════════════════════════

# A71 (2026-06-10) — scaled to 32 bots (clientIds 80-111). Combined with
# sidecars at 78-79, this requires 34 concurrent API connections.
#
# A73 ATTEMPT (2026-06-10) reverted: tried to push to 62 bots but TWS's
# API connection limit is a hard ceiling at 32 — cids 112+ silently fail
# to connect regardless of the "Maximum number of simultaneous API
# connections" setting in Global Configuration. 32 bots + 2 sidecars
# (clientIds 78, 79) saturates this universe's practical max.
#
# To go beyond, the operator would need to run multiple TWS sessions or
# switch to IB Gateway with a different connection model. Keeping the
# A74 contract-disambiguation and retry-duplicate fixes — they're real
# bugs caught at this scale that bite even at 32 bots.
PAIRS = [
    # Mega-cap tech (CIDs 80-87)
    {"symbol": "NVDA",  "client_id": 80, "qty": 100, "exchange": "SMART"},
    {"symbol": "AAPL",  "client_id": 81, "qty": 100, "exchange": "SMART"},
    {"symbol": "MSFT",  "client_id": 82, "qty": 100, "exchange": "SMART"},
    {"symbol": "GOOGL", "client_id": 83, "qty": 100, "exchange": "SMART"},
    {"symbol": "AMZN",  "client_id": 84, "qty": 100, "exchange": "SMART"},
    {"symbol": "META",  "client_id": 85, "qty": 100, "exchange": "SMART"},
    {"symbol": "AVGO",  "client_id": 86, "qty": 100, "exchange": "SMART"},
    {"symbol": "TSLA",  "client_id": 87, "qty": 100, "exchange": "SMART"},
    # Finance + retail (CIDs 88-95)
    {"symbol": "JPM",   "client_id": 88, "qty": 100, "exchange": "SMART"},
    {"symbol": "V",     "client_id": 89, "qty": 100, "exchange": "SMART"},
    {"symbol": "MA",    "client_id": 90, "qty": 100, "exchange": "SMART"},
    {"symbol": "WMT",   "client_id": 91, "qty": 100, "exchange": "SMART"},
    {"symbol": "COST",  "client_id": 92, "qty": 100, "exchange": "SMART"},
    {"symbol": "HD",    "client_id": 93, "qty": 100, "exchange": "SMART"},
    {"symbol": "LLY",   "client_id": 94, "qty": 100, "exchange": "SMART"},
    {"symbol": "NFLX",  "client_id": 95, "qty": 100, "exchange": "SMART"},
    # Tech (second wave) + finance (CIDs 96-103)
    {"symbol": "ORCL",  "client_id": 96, "qty": 100, "exchange": "SMART"},
    {"symbol": "AMD",   "client_id": 97, "qty": 100, "exchange": "SMART"},
    {"symbol": "ADBE",  "client_id": 98, "qty": 100, "exchange": "SMART"},
    {"symbol": "CRM",   "client_id": 99, "qty": 100, "exchange": "SMART"},
    {"symbol": "NOW",   "client_id": 100, "qty": 100, "exchange": "SMART"},
    {"symbol": "BAC",   "client_id": 101, "qty": 100, "exchange": "SMART"},
    {"symbol": "UNH",   "client_id": 102, "qty": 100, "exchange": "SMART"},
    {"symbol": "XOM",   "client_id": 103, "qty": 100, "exchange": "SMART"},
    # Pharma + consumer staples (CIDs 104-111)
    {"symbol": "ABBV",  "client_id": 104, "qty": 100, "exchange": "SMART"},
    {"symbol": "JNJ",   "client_id": 105, "qty": 100, "exchange": "SMART"},
    {"symbol": "MRK",   "client_id": 106, "qty": 100, "exchange": "SMART"},
    {"symbol": "PFE",   "client_id": 107, "qty": 100, "exchange": "SMART"},
    {"symbol": "TMO",   "client_id": 108, "qty": 100, "exchange": "SMART"},
    {"symbol": "PG",    "client_id": 109, "qty": 100, "exchange": "SMART"},
    {"symbol": "KO",    "client_id": 110, "qty": 100, "exchange": "SMART"},
    {"symbol": "PEP",   "client_id": 111, "qty": 100, "exchange": "SMART"},
]

# TRIGGER_OFFSET_BPS — same semantic as FX version (0 = trigger AT LTP).
# Equity prices range $100-700, so 0 bps = trigger at exactly LTP.
# 1 bp on a $200 stock = $0.02 above LTP (snaps to next tick).
TRIGGER_OFFSET_BPS = 0.0

# STOP_PCT: protective stop as a fraction of entry price.
#   0.0001 = 1 bp = $0.02 on $200 stock — sub-tick, will be too tight
#   0.0005 = 5 bp = $0.10 on $200 stock — fires within seconds on normal vol
#   0.001  = 10 bp = $0.20 on $200 stock — produces full cycles in ~10s
# Default 0.0005 for stress — guarantees SL fires within the 5-min soak.
STOP_PCT = 0.0005
OFFSET_FIXED = 0.05        # parent limit = trigger - $0.05 ($1 stock = 5% room; safe across price range)

# Sidecar client IDs (must not collide with bot client_ids OR live trading)
WATCHDOG_CID = 79          # sidecar that flattens / cancels
LTP_FETCH_CID = 78         # sidecar that fetches LTP

# A78 (2026-06-11): PORT overridable via GT_IBKR_PORT env var so the same
# code runs against TWS (default 7497) or against headless IB Gateway
# in Docker (typically 4002 for paper, 4001 for live). Default unchanged
# so existing TWS-based runs are not affected.
import os as _os
PORT = int(_os.environ.get("GT_IBKR_PORT", "7497"))


# ────────────────────────────────────────────────────────────────────────────
# Helpers — venue tick grid
# ────────────────────────────────────────────────────────────────────────────

_TICK = {
    # US equities: $0.01 minimum tick for prices ≥ $1.00 (NYSE/NASDAQ).
    # Sub-dollar stocks get $0.0001 but none of our names trade there.
    # Original 16 (CIDs 80-95)
    "NVDA": 0.01, "AAPL": 0.01, "MSFT": 0.01, "GOOGL": 0.01,
    "AMZN": 0.01, "META": 0.01, "AVGO": 0.01, "TSLA": 0.01,
    "JPM": 0.01, "V": 0.01, "MA": 0.01, "WMT": 0.01,
    "COST": 0.01, "HD": 0.01, "LLY": 0.01, "NFLX": 0.01,
    # A71 — second 16 (CIDs 96-111)
    "ORCL": 0.01, "AMD": 0.01, "ADBE": 0.01, "CRM": 0.01,
    "NOW": 0.01, "BAC": 0.01, "UNH": 0.01, "XOM": 0.01,
    "ABBV": 0.01, "JNJ": 0.01, "MRK": 0.01, "PFE": 0.01,
    "TMO": 0.01, "PG": 0.01, "KO": 0.01, "PEP": 0.01,
}


def _round_to_tick(px: float, symbol: str) -> float:
    t = _TICK.get(symbol, 0.01)
    return round(round(px / t) * t, 8)


# A74 (2026-06-10) — primaryExchange disambiguation map. Some tickers
# collide with non-US instruments on SMART (e.g. single-letter "V" / "C"
# / "F"; or "MMC" Marsh & McLennan vs other global MMC listings). Pass
# primaryExchange to force the correct listing. Empty string / missing
# entry = bare SMART works fine.
#
# A73 ATTEMPT revealed MMC needs this (Error 200 at LTP fetch). MMC is
# not in the current PAIRS universe (only single-letter "V" is), but we
# keep MMC + likely-ambiguous defensive entries so when the universe
# expands they're already covered.
_PRIMARY_EXCHANGE = {
    "MMC": "NYSE",   # Marsh & McLennan (Error 200 without this)
    "V":   "NYSE",   # Visa (single letter — in current PAIRS)
    # Defensive entries for future expansion (not in current PAIRS):
    "C":   "NYSE",   # Citigroup
    "F":   "NYSE",   # Ford
    "T":   "NYSE",   # AT&T
    "GE":  "NYSE",   # General Electric
    "GS":  "NYSE",   # Goldman Sachs
    "MS":  "NYSE",   # Morgan Stanley
    "BA":  "NYSE",   # Boeing
}


def _primary_exchange_for(symbol: str) -> str:
    """Return primaryExchange hint for SMART routing, '' if not needed."""
    return _PRIMARY_EXCHANGE.get(symbol, "")


def _is_jpy(sym: str) -> bool:
    return sym.endswith("JPY")


def _offset_fixed_for(sym: str) -> float:
    # Equity: $0.05 below trigger gives parent BUY a 5-cent fill window
    # (enough slop for spread+slippage at market open / lunchtime). Same
    # value for every name in our universe — tickers >$1 use $0.01 grid
    # so $0.05 = 5 ticks of slop.
    return OFFSET_FIXED


# ────────────────────────────────────────────────────────────────────────────
# IBKR sidecar — fetch LTP, cancel, flatten
# ────────────────────────────────────────────────────────────────────────────

async def _fetch_ltp(ib, symbol: str) -> Optional[float]:
    # Equity: Stock(symbol, exchange='SMART', currency='USD')
    # SMART routes through best venue (NASDAQ for tech, NYSE for big board)
    # A74 (2026-06-10): some symbols are ambiguous on SMART without
    # primaryExchange (Marsh & McLennan "MMC" collides with non-US
    # instruments; Error 200 "No security definition has been found").
    # Pass primaryExchange via _primary_exchange_for() — falls back to
    # bare SMART for symbols that aren't ambiguous.
    from ib_async import Stock
    pex = _primary_exchange_for(symbol)
    if pex:
        contract = Stock(symbol, exchange='SMART', currency='USD',
                         primaryExchange=pex)
    else:
        contract = Stock(symbol, exchange='SMART', currency='USD')
    try:
        q = await asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout=4.0)
    except (asyncio.TimeoutError, Exception):
        return None
    if not q:
        return None
    qc = q[0]
    ticker = ib.reqMktData(qc, "", False, False)
    try:
        for _ in range(20):  # up to 4s
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
    """Fetch LTP for all 8 pairs via one sidecar IB connection."""
    from ib_async import IB
    ib = IB()
    out: dict[str, Optional[float]] = {p["symbol"]: None for p in PAIRS}
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=LTP_FETCH_CID),
            timeout=10.0,
        )
        # Sequential (avoid pacing): each call ~0.5-2s. 8 pairs ~8s total.
        for p in PAIRS:
            ltp = await _fetch_ltp(ib, p["symbol"])
            out[p["symbol"]] = ltp
            print(f"  [ltp] {p['symbol']:7s}  LTP={ltp}")
    except Exception as e:
        print(f"  [ltp] connect/fetch error: {e}", file=sys.stderr)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return out


# ─────────────────────────────────────────────────────────────────────────
# Pre-flatten — close every open equity position before stress
# ─────────────────────────────────────────────────────────────────────────

# Symbols we may touch. We only flatten positions in this set (don't touch
# anything else the operator has in the account). For "flatten everything",
# extend or remove the filter.
_STRESS_SYMBOLS = {p["symbol"] for p in PAIRS}

# Kept for cross-compatibility with chaos_test_equity.py which still
# references `_FLEET_CCY` symbolically — equity has no per-currency
# fleet concept, so this is just an empty set.
_FLEET_CCY = set()


async def _flatten_all_fx(port: int) -> tuple[int, list[str]]:
    """EQUITY VERSION: close every open stock position in _STRESS_SYMBOLS.

    (Name kept as `_flatten_all_fx` for cross-compatibility with the
    chaos_test_equity.py import — the function does NOT touch FX here.)

    Strategy is simpler than the FX version: equity positions appear
    cleanly in `ib.positions()` as Stock objects (no cash-ledger quirk
    to work around). For each position whose symbol is in our stress
    universe, place an opposite-side MARKET order with tif='IOC' to
    close. Skip positions outside the universe so we don't touch the
    operator's other holdings.

    Returns (orders_placed, error_log).
    """
    from ib_async import IB, Stock, MarketOrder
    errors: list[str] = []
    placed = 0

    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=WATCHDOG_CID),
            timeout=10.0,
        )
        await asyncio.sleep(2.0)  # let positions stream populate

        # A61 (2026-06-10): global cancel before flatten. The per-bot
        # cancel sweep in `_watchdog_iteration` cannot cross-client
        # cancel without masterClientId in TWS (clientId=79 silently
        # ignored). reqGlobalCancel() bypasses ownership.
        try:
            ib.reqGlobalCancel()
            await asyncio.sleep(2.0)  # let cancels propagate
            print(f"  reqGlobalCancel() issued, 2s settle")
        except Exception as e:
            errors.append(f"reqGlobalCancel: {type(e).__name__}: {e}")

        # Walk positions() for stocks in our stress universe
        try:
            positions = ib.positions()
            stress_positions = []
            for pos in positions:
                c = pos.contract
                sec_type = getattr(c, 'secType', '') or ''
                if sec_type != 'STK':
                    continue
                sym = getattr(c, 'symbol', '') or ''
                if sym not in _STRESS_SYMBOLS:
                    continue
                qty_signed = float(pos.position or 0)
                if abs(qty_signed) < 1.0:
                    continue
                stress_positions.append((sym, c, qty_signed))

            if not stress_positions:
                print(f"  positions()-based flatten: no equity positions "
                      f"to close in stress universe")
            else:
                print(f"  positions()-based flatten: {len(stress_positions)} "
                      f"equity position(s) to close")
                for sym, contract, qty_signed in stress_positions:
                    action = "SELL" if qty_signed > 0 else "BUY"
                    qty = int(abs(qty_signed))
                    try:
                        # Re-qualify in case the position's contract is
                        # missing a routing detail (rare; positions usually
                        # come back fully populated).
                        qualified = await asyncio.wait_for(
                            ib.qualifyContractsAsync(contract), timeout=4.0,
                        )
                        if not qualified:
                            errors.append(f"flatten {sym}: qualify returned empty")
                            continue
                        # A69 (2026-06-10): tif='DAY' for equity flatten.
                        # IOC was inherited from the FX A61 fix (FX account
                        # preset forces GTC and MARKET+GTC is invalid). For
                        # equity, IOC partial-fills or fails entirely when
                        # SMART can't route immediately (Error 202 with no
                        # reason). Live regression 2026-06-10: 4 of 7 IOC
                        # flatten orders left positions open (AAPL/HD/MA/LLY).
                        # DAY market orders are the standard for equity
                        # flatten — IBKR's SMART router will hold them as
                        # needed during RTH until filled.
                        order = MarketOrder(action, qty, tif='DAY')
                        ib.placeOrder(qualified[0], order)
                        placed += 1
                        print(f"    → {action} {qty:>6,} {sym:<6s} "
                              f"(flatten position {qty_signed:+,.0f} shares)")
                    except Exception as e:
                        errors.append(f"flatten {sym} {action} {qty}: "
                                      f"{type(e).__name__}: {e}")
        except Exception as e:
            errors.append(f"positions() walk: {type(e).__name__}: {e}")

        # Wait for fills, re-walk positions to verify
        if placed > 0:
            print(f"  waiting 6s for {placed} flatten order(s) to settle…")
            await asyncio.sleep(6.0)
            # A69 single-shot retry: any position still open after the
            # first DAY market sweep gets a SECOND DAY market order. This
            # catches the edge case where SMART couldn't fill during the
            # 6-second window (e.g. tight bid-ask, LULD halt, momentary
            # routing failure). One retry is enough; if THAT fails the
            # operator must intervene manually in TWS.
            retry_targets = []
            for pos in ib.positions():
                c = pos.contract
                if getattr(c, 'secType', '') != 'STK':
                    continue
                sym = getattr(c, 'symbol', '') or ''
                if sym not in _STRESS_SYMBOLS:
                    continue
                q = float(pos.position or 0)
                if abs(q) >= 1.0:
                    retry_targets.append((sym, c, q))
            if retry_targets:
                # A74 (2026-06-10): before placing retry, check if there's
                # already a working SELL/BUY order for that symbol at the
                # broker. The first flatten round's order may still be
                # Submitted/PendingSubmit — placing a second order would
                # over-flatten (ABBV regression 2026-06-10: 2 SELL MKT
                # orders both filled, leaving ABBV -100). Skip symbols with
                # in-flight opposite-side orders and just wait longer.
                already_working_syms = set()
                try:
                    for trade in ib.openTrades():
                        c = trade.contract
                        if getattr(c, 'secType', '') != 'STK':
                            continue
                        s = getattr(c, 'symbol', '') or ''
                        st = (trade.orderStatus.status or '') if trade.orderStatus else ''
                        if st in ('Submitted', 'PreSubmitted', 'PendingSubmit'):
                            already_working_syms.add(s)
                except Exception:
                    pass
                actionable = [t for t in retry_targets
                              if t[0] not in already_working_syms]
                skipped = [t for t in retry_targets
                           if t[0] in already_working_syms]
                if skipped:
                    print(f"  retry pass: {len(skipped)} symbol(s) "
                          f"({', '.join(s for s,_,_ in skipped)}) have working "
                          f"orders already — extending wait, NOT duplicating")
                if actionable:
                    print(f"  retry pass: {len(actionable)} position(s) still "
                          f"open after first DAY market sweep, placing second round")
                else:
                    print(f"  retry pass: 0 new orders to place — waiting 8s "
                          f"for in-flight orders to settle")
                retry_placed = 0
                for sym, contract, qty_signed in actionable:
                    action = "SELL" if qty_signed > 0 else "BUY"
                    qty = int(abs(qty_signed))
                    try:
                        order = MarketOrder(action, qty, tif='DAY')
                        ib.placeOrder(contract, order)
                        retry_placed += 1
                        placed += 1
                        print(f"    ↻ {action} {qty:>6,} {sym:<6s} "
                              f"(retry flatten {qty_signed:+,.0f} shares)")
                    except Exception as e:
                        errors.append(f"retry flatten {sym} {action} {qty}: "
                                      f"{type(e).__name__}: {e}")
                # Wait long enough for both new orders AND any still-pending
                # first-round orders to fill. Bumped 4s → 8s.
                if retry_placed > 0 or skipped:
                    print(f"  waiting 8s for orders to settle…")
                    await asyncio.sleep(8.0)
            # Final position check
            still_open = []
            for pos in ib.positions():
                c = pos.contract
                if getattr(c, 'secType', '') != 'STK':
                    continue
                sym = getattr(c, 'symbol', '') or ''
                if sym not in _STRESS_SYMBOLS:
                    continue
                q = float(pos.position or 0)
                if abs(q) >= 1.0:
                    still_open.append((sym, q))
            print("  post-flatten equity positions:")
            if not still_open:
                print(f"    all stress symbols flat ✓")
            else:
                for sym, q in still_open:
                    print(f"    {sym:<6s} {q:+8.0f}  ⚠ STILL OPEN")
    except Exception as e:
        errors.append(f"flatten connect: {type(e).__name__}: {e}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return placed, errors


async def _cancel_open_orders_for(ib, symbol: str, log: list) -> int:
    """Cancel every open order at the broker matching this FX pair.
    Returns count cancelled. Does NOT touch positions — that's the
    engine's protective-SL job, not ours.

    Why cancel-only (live regression 2026-06-09):
        The previous flatten-by-cash-delta approach was fundamentally
        broken because multiple FX pairs share base currencies
        (USDJPY/USDCHF/USDCAD all USD-quoted), so cash deltas can't be
        uniquely attributed to one pair. Trying produced spurious 10M+
        unit SELLs that IBKR rejected. Cancel-only is correct: the
        engine's bracket child SELL STP is a real broker order, which
        protects the position even if we don't manually flatten."""
    cancelled = 0
    try:
        for trade in ib.openTrades():
            c = trade.contract
            # Equity match: secType STK + bare symbol equality.
            # (FX version used compound + localSymbol with dot stripped
            # because IDEALPRO uses "EUR.USD"; equity has clean symbols.)
            sec_type = getattr(c, 'secType', '') or ''
            sym = getattr(c, 'symbol', '') or ''
            if sec_type == 'STK' and sym == symbol:
                try:
                    ib.cancelOrder(trade.order)
                    cancelled += 1
                except Exception as e:
                    log.append(f"cancel({symbol}/{trade.order.orderId}): {e}")
    except Exception as e:
        log.append(f"openTrades({symbol}): {e}")
    return cancelled


async def _watchdog_iteration(port: int, log: list) -> dict[str, int]:
    """One sweep across all pairs: cancel resting orders via sidecar.
    Returns dict[symbol -> cancel count]."""
    from ib_async import IB
    counts: dict[str, int] = {p["symbol"]: 0 for p in PAIRS}
    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=WATCHDOG_CID),
            timeout=8.0,
        )
        await asyncio.sleep(0.5)  # let openTrades populate
        # Refresh open orders cache so cross-client cancels work
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception:
            pass

        for p in PAIRS:
            counts[p["symbol"]] = await _cancel_open_orders_for(ib, p["symbol"], log)
    except Exception as e:
        log.append(f"watchdog connect: {type(e).__name__}: {e}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return counts


# ────────────────────────────────────────────────────────────────────────────
# Bot lifecycle (tmux)
# ────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_ROOT = PROJECT_ROOT / "tests" / "paper" / "logs"


def _tmux(args: list[str], check: bool = True) -> None:
    try:
        subprocess.run(["tmux", *args], check=check, capture_output=True)
    except subprocess.CalledProcessError:
        if check:
            raise


def _kill_stale_sessions() -> None:
    try:
        out = subprocess.run(
            ["tmux", "ls"], capture_output=True, text=True, check=False
        ).stdout
        for line in out.splitlines():
            name = line.split(":", 1)[0]
            if name.startswith("gt_churn_"):
                _tmux(["kill-session", "-t", name], check=False)
    except FileNotFoundError:
        pass


def _cleanup_state_files() -> None:
    for p in PAIRS:
        for fname in (
            f".gt_state_{p['symbol']}_{p['client_id']}.json",
            f".gt_live_{p['symbol']}_{p['client_id']}.json",
        ):
            fp = PROJECT_ROOT / fname
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass


def _python_bin() -> str:
    """Resolve the right python interpreter for the spawned bot.
    tmux's new shell doesn't always inherit the parent's venv-activated
    PATH, so plain `python3` can land on the system interpreter which
    lacks ib_async. Prefer $VIRTUAL_ENV/bin/python3 when set.
    """
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        for cand in (f"{venv}/bin/python3", f"{venv}/bin/python"):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    # Final fallback: sys.executable (may follow symlink to system python,
    # but better than guessing wrong).
    return sys.executable or "python3"


def _spawn_bot(p: dict, trigger, log_dir: Path) -> tuple[str, Path]:
    """Spawn one engine bot in a named tmux session. Returns (session, log_path).

    `trigger` can be a float (passes `--trigger N` on the CLI for a fresh
    cycle) or None (omit `--trigger` — run_live.py recovers it from the
    saved state file). Pass None on RESPAWN scenarios where reconcile
    is supposed to adopt the existing broker bracket — passing a trigger
    that differs from the saved one trips the trigger-mismatch guard and
    refuses to start; even when they match, having a `--trigger` on the
    CLI suppresses the state-file recovery path entirely.
    """
    session = f"gt_churn_{p['symbol']}_{p['client_id']}"
    log_path = log_dir / f"{p['symbol']}_{p['client_id']}_{int(time.time())}.log"
    offset_fixed = _offset_fixed_for(p["symbol"])
    py = _python_bin()
    # Three env vars set for stress mode bots:
    #   GT_SKIP_NAKED_GUARD=1   — ignore pre-existing FX cash balances
    #   GT_DISABLE_RISK_GATE=1  — bypass ALL risk gate checks (exposure,
    #                              daily loss, consec losses, max trades,
    #                              price staleness). Stress-only.
    trigger_flag = f"--trigger {trigger} " if trigger is not None else ""
    # Propagate GT_DISABLE_TICKBYTICK to each bot so the operator can skip the
    # scarce tick-by-tick subscription on big EQUITY fleets (escapes IBKR
    # Error 10190). Engine-side it only affects 'AllLast'/equity; FX keeps it.
    _tbt_env = (f"GT_DISABLE_TICKBYTICK={_os.environ.get('GT_DISABLE_TICKBYTICK', '').strip()} "
                if _os.environ.get('GT_DISABLE_TICKBYTICK', '').strip() else "")
    # Propagate GT_ENTRY_CUTOFF. DEFAULT (unset) → engine KEEPS resting BUY
    # entry brackets through the equity close, so the overnight reconcile flow
    # finds them still resting. Set GT_ENTRY_CUTOFF=1 to restore the old
    # cancel-5-min-before-close behaviour. SELL stops + pause untouched either way.
    _cutoff_env = (f"GT_ENTRY_CUTOFF={_os.environ.get('GT_ENTRY_CUTOFF', '').strip()} "
                   if _os.environ.get('GT_ENTRY_CUTOFF', '').strip() else "")
    cli = (
        f"GT_PAPER=false "
        f"GT_SKIP_NAKED_GUARD=1 "
        f"GT_DISABLE_RISK_GATE=1 "
        f"{_tbt_env}"
        f"{_cutoff_env}"
        f"{shlex.quote(py)} run_live.py "
        f"{p['symbol']} "
        f"{trigger_flag}--stop {STOP_PCT} "
        f"--offset-fixed {offset_fixed} --qty {p['qty']} "
        f"--port {PORT} --client-id {p['client_id']} --uvloop"
    )
    _tmux(["new-session", "-d", "-s", session, "-c", str(PROJECT_ROOT)])
    _tmux([
        "pipe-pane", "-o", "-t", session,
        f"cat >> {shlex.quote(str(log_path))}",
    ])
    _tmux(["send-keys", "-t", session, cli, "Enter"])
    return session, log_path


def _shutdown_bot(session: str) -> None:
    """Ctrl-C the bot, wait for graceful shutdown, kill the session."""
    _tmux(["send-keys", "-t", session, "C-c"], check=False)


# ────────────────────────────────────────────────────────────────────────────
# Audit-log analysis (per pair)
# ────────────────────────────────────────────────────────────────────────────

def _today_audit_dir(symbol: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return PROJECT_ROOT / "data" / "audit" / today / symbol


def _load_order_csv(symbol: str, since_ts: datetime) -> list[dict]:
    p = _today_audit_dir(symbol) / "order.csv"
    if not p.exists():
        return []
    rows = []
    try:
        with open(p) as f:
            for row in csv.DictReader(f):
                ts_s = row.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_s)
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    # Naive — assume UTC (engine writes UTC)
                    pass
                if ts >= since_ts:
                    row["_ts"] = ts
                    rows.append(row)
    except Exception as e:
        print(f"  [audit/{symbol}] read error: {e}", file=sys.stderr)
    return rows


def _aggregate_pair(symbol: str, since_ts: datetime) -> dict:
    rows = _load_order_csv(symbol, since_ts)
    # Latency: SUBMITTED → FILLED for BUYs
    submitted_by_id: dict[str, datetime] = {}
    fill_latencies_ms: list[float] = []
    cancel_latencies_ms: list[float] = []
    counts = {
        "brackets_placed": 0,
        "filled": 0,
        "sl_hit": 0,
        "cancelled": 0,
        "rejected": 0,
        "commission_reports": 0,
        "phantom_sells": 0,
        "auto_flats": 0,
        "errors": 0,
    }
    for row in rows:
        ev = row.get("event", "")
        oid = row.get("order_id", "")
        ts: datetime = row["_ts"]
        if ev == "SUBMITTED":
            submitted_by_id[oid] = ts
        elif ev == "BRACKET_SUBMITTED":
            counts["brackets_placed"] += 1
            submitted_by_id[oid] = ts
        elif ev == "FILLED":
            side = row.get("side", "")
            if side == "BUY":
                counts["filled"] += 1
                if oid in submitted_by_id:
                    delta_ms = (ts - submitted_by_id[oid]).total_seconds() * 1000.0
                    fill_latencies_ms.append(delta_ms)
            elif side == "SELL":
                # SL hit OR force-flat — distinguish by reason
                reason = (row.get("reason") or "").upper()
                if "STOP_LOSS" in reason:
                    counts["sl_hit"] += 1
        elif ev == "CANCELLED":
            counts["cancelled"] += 1
            if oid in submitted_by_id:
                delta_ms = (ts - submitted_by_id[oid]).total_seconds() * 1000.0
                cancel_latencies_ms.append(delta_ms)
        elif ev == "REJECTED":
            counts["rejected"] += 1
            counts["errors"] += 1
        elif ev == "COMMISSION_REPORT":
            counts["commission_reports"] += 1
        elif ev == "PHANTOM_SELL_REJECTED":
            counts["phantom_sells"] += 1
        elif ev == "POSITION_AUTO_FLAT":
            counts["auto_flats"] += 1
            counts["errors"] += 1

    def _pct(samples: list[float], p: float) -> float:
        if not samples:
            return 0.0
        s = sorted(samples)
        k = max(0, min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1)))))
        return s[k]

    return {
        "symbol": symbol,
        **counts,
        "fill_latency_p50_ms": _pct(fill_latencies_ms, 50),
        "fill_latency_p95_ms": _pct(fill_latencies_ms, 95),
        "fill_latency_p99_ms": _pct(fill_latencies_ms, 99),
        "fill_latency_max_ms": max(fill_latencies_ms) if fill_latencies_ms else 0.0,
        "cancel_latency_p50_ms": _pct(cancel_latencies_ms, 50),
        "cancel_latency_p95_ms": _pct(cancel_latencies_ms, 95),
        "cancel_latency_p99_ms": _pct(cancel_latencies_ms, 99),
        "cancel_latency_max_ms": max(cancel_latencies_ms) if cancel_latencies_ms else 0.0,
        "sample_size_fill": len(fill_latencies_ms),
        "sample_size_cancel": len(cancel_latencies_ms),
    }


# ────────────────────────────────────────────────────────────────────────────
# Reporting
# ────────────────────────────────────────────────────────────────────────────

def _print_summary(results: list[dict], duration_s: float, watchdog_errors: list[str]) -> None:
    print()
    print("═" * 78)
    print(f"STRESS_CHURN  {duration_s:.0f}s  |  {len(PAIRS)} pairs concurrent  |  results")
    print("═" * 78)
    hdr = (f"{'Pair':<8} {'Place':>6} {'Fill':>5} {'SL':>4} {'Cncl':>5} "
           f"{'Rej':>4} {'Phntm':>5} {'AutoFlat':>9} {'Err':>4}")
    print(hdr)
    print("─" * 78)
    tot = {k: 0 for k in (
        "brackets_placed", "filled", "sl_hit", "cancelled",
        "rejected", "phantom_sells", "auto_flats", "errors",
    )}
    for r in results:
        print(f"{r['symbol']:<8} {r['brackets_placed']:>6} {r['filled']:>5} "
              f"{r['sl_hit']:>4} {r['cancelled']:>5} {r['rejected']:>4} "
              f"{r['phantom_sells']:>5} {r['auto_flats']:>9} {r['errors']:>4}")
        for k in tot:
            tot[k] += r[k]
    print("─" * 78)
    print(f"{'TOTAL':<8} {tot['brackets_placed']:>6} {tot['filled']:>5} "
          f"{tot['sl_hit']:>4} {tot['cancelled']:>5} {tot['rejected']:>4} "
          f"{tot['phantom_sells']:>5} {tot['auto_flats']:>9} {tot['errors']:>4}")
    print()
    # Aggregate latency across pairs
    all_fill_p50 = [r["fill_latency_p50_ms"] for r in results if r["sample_size_fill"]]
    all_fill_p95 = [r["fill_latency_p95_ms"] for r in results if r["sample_size_fill"]]
    all_fill_p99 = [r["fill_latency_p99_ms"] for r in results if r["sample_size_fill"]]
    all_fill_max = [r["fill_latency_max_ms"] for r in results if r["sample_size_fill"]]
    all_cnc_p50 = [r["cancel_latency_p50_ms"] for r in results if r["sample_size_cancel"]]
    all_cnc_p95 = [r["cancel_latency_p95_ms"] for r in results if r["sample_size_cancel"]]
    all_cnc_p99 = [r["cancel_latency_p99_ms"] for r in results if r["sample_size_cancel"]]
    all_cnc_max = [r["cancel_latency_max_ms"] for r in results if r["sample_size_cancel"]]
    if all_fill_p50:
        print(f"FILL LATENCY (SUBMITTED → FILLED for BUY, across pairs)")
        print(f"  p50 median-of-medians={statistics.median(all_fill_p50):.1f}ms  "
              f"p95={max(all_fill_p95):.1f}ms  p99={max(all_fill_p99):.1f}ms  "
              f"max={max(all_fill_max):.1f}ms")
    if all_cnc_p50:
        print(f"CANCEL LATENCY (SUBMITTED → CANCELLED)")
        print(f"  p50={statistics.median(all_cnc_p50):.1f}ms  "
              f"p95={max(all_cnc_p95):.1f}ms  p99={max(all_cnc_p99):.1f}ms  "
              f"max={max(all_cnc_max):.1f}ms")
    total_orders = tot["brackets_placed"] * 2  # parent + child per bracket
    print(f"\nTHROUGHPUT  {total_orders/duration_s:.2f} order events/sec  "
          f"({total_orders} orders / {duration_s:.0f}s)")
    print(f"FILL RATE   {100*tot['filled']/max(tot['brackets_placed'],1):.1f}%  "
          f"({tot['filled']} of {tot['brackets_placed']} BUYs filled)")
    if watchdog_errors:
        print(f"\nWATCHDOG ERRORS ({len(watchdog_errors)} total, showing first 10):")
        for e in watchdog_errors[:10]:
            print(f"  ! {e}")
    print("═" * 78)


def _write_csv(results: list[dict], out_path: Path) -> None:
    if not results:
        return
    keys = list(results[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\nFull per-pair CSV: {out_path}")


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

async def _amain(args) -> int:
    # Filter pairs if requested
    global PAIRS
    if args.pairs:
        wanted = set(s.strip().upper() for s in args.pairs.split(","))
        PAIRS = [p for p in PAIRS if p["symbol"] in wanted]
        if not PAIRS:
            print("No matching pairs.", file=sys.stderr)
            return 2

    LOG_ROOT.mkdir(parents=True, exist_ok=True)

    # 1. Pre-flight cleanup
    print("\n═══ PRE-FLIGHT ═══")
    _kill_stale_sessions()
    _cleanup_state_files()
    print(f"  killed stale gt_churn_* sessions; cleaned state files for client_ids "
          f"{[p['client_id'] for p in PAIRS]}")

    # Risk-cap override (A28, default ON via A29). Writes a $1B portfolio
    # cap that the risk gate's PortfolioLimitsReader picks up within 1s.
    # Restored on exit. Default ON because 8-pair stress fleet exceeds the
    # default $50k cap after 2 entries — otherwise 6 bots get rejected.
    saved_limits_content: Optional[bytes] = None
    limits_path = PROJECT_ROOT / ".gt_portfolio_limits.json"
    if args.no_risk_cap:
        print("\n═══ RISK-CAP OVERRIDE  (writing $1B cap to .gt_portfolio_limits.json) ═══")
        print("  (use --keep-risk-cap to disable this override and stress-test")
        print("   the risk gate's interaction with the fleet instead)")
        if limits_path.exists():
            saved_limits_content = limits_path.read_bytes()
        import json as _json
        limits_path.write_text(_json.dumps({
            "max_position_value_usd": 1_000_000_000,
            "max_daily_loss_usd": 1_000_000_000,
            "_set_by": "stress_churn.py (default no-risk-cap)",
        }, indent=2))
        print(f"  wrote: {limits_path}")
    else:
        print("\n═══ RISK-CAP KEPT  (engine's portfolio risk gate is ACTIVE) ═══")
        print(f"  default cap=$50,000. Expect 6 of 8 bots to get REJECTED.")
        print(f"  (drop --keep-risk-cap to enable the $1B override)")

    # Initial cancel-sweep (in case prior runs left resting orders)
    print("\n═══ INITIAL CANCEL SWEEP ═══")
    init_errs: list[str] = []
    init_counts = await _watchdog_iteration(PORT, init_errs)
    total_init = sum(init_counts.values())
    print(f"  cancelled {total_init} pre-existing resting orders "
          f"({len(init_errs)} non-fatal errors)")
    await asyncio.sleep(1.5)

    # Pre-flatten all fleet-currency positions. Without this, the engine's
    # naked-position guard fires for any bot whose pair has a non-zero
    # broker_qty (e.g., EUR +245k → EURUSD bot crashes). The GT_SKIP_NAKED_GUARD
    # env var SHOULD bypass this, but observed regression: bot still shows
    # REFUSED_NAKED in log. Pre-flatten removes the source of the trigger.
    if not args.keep_positions:
        print("\n═══ PRE-FLATTEN ALL FX POSITIONS ═══")
        n_flatten, flatten_errs = await _flatten_all_fx(PORT)
        print(f"  placed {n_flatten} flatten order(s), "
              f"{len(flatten_errs)} non-fatal errors")
        for e in flatten_errs[:8]:
            print(f"    ! {e}")
        if flatten_errs and not n_flatten:
            print("  WARNING: pre-flatten failed; some bots may REFUSE_NAKED")
        await asyncio.sleep(1.0)
    else:
        print("\n═══ PRE-FLATTEN SKIPPED (--keep-positions) ═══")
        print("  WARNING: bots whose pair has a non-zero broker_qty will "
              "crash with STARTUP_REFUSED_NAKED unless GT_SKIP_NAKED_GUARD works.")

    # 2. Fetch LTPs for trigger computation
    print("\n═══ LTP RESOLUTION ═══")
    ltps = await _resolve_all_ltps(PORT)
    if not any(ltps.values()):
        print("All LTP lookups failed. Aborting.", file=sys.stderr)
        return 1

    # 3. Spawn 8 bots
    print(f"\n═══ SPAWNING BOTS  trigger=LTP+{TRIGGER_OFFSET_BPS:+.2f}bps  "
          f"SL={STOP_PCT*10_000:.2f}bps  "
          f"{'(SL fires on bid-spread) ' if STOP_PCT * 10_000 <= 1.0 else ''}═══")
    spawned: list[dict] = []
    start_wall = datetime.now()
    for p in PAIRS:
        ltp = ltps.get(p["symbol"])
        if ltp is None:
            print(f"  SKIP {p['symbol']} — no LTP")
            continue
        trigger = _round_to_tick(
            ltp * (1 + TRIGGER_OFFSET_BPS / 10_000.0), p["symbol"]
        )
        session, log_path = _spawn_bot(p, trigger, LOG_ROOT)
        spawned.append({
            "symbol": p["symbol"], "client_id": p["client_id"],
            "session": session, "log_path": log_path, "trigger": trigger,
        })
        print(f"  {p['symbol']:7s}  cid={p['client_id']}  trigger={trigger}  "
              f"tmux={session}")
        await asyncio.sleep(0.4)  # stagger spawns

    if not spawned:
        print("No bots spawned. Aborting.", file=sys.stderr)
        return 1

    # 3b. Verify each bot actually connected (live regression 2026-06-09:
    #     bots were silently failing on `python3` interpreter mismatch but
    #     driver reported them as 'spawned'. Now we wait ~10s for each
    #     log to show '[Gateway] Connected!' and report status explicitly).
    print(f"\n═══ VERIFYING BOT STARTUPS ({len(spawned)} bots, up to 15s each) ═══")
    await asyncio.sleep(6.0)  # let bots get past initial imports
    healthy: list = []
    failed: list = []
    for s in spawned:
        log_path = s["log_path"]
        symbol = s["symbol"]
        cid = s["client_id"]
        ok = False
        err_lines: list[str] = []
        state_marker = ""
        try:
            if log_path.exists():
                txt = log_path.read_text(encoding='utf-8', errors='replace')
                # Connection success markers
                if "[Gateway] Connected" in txt or "MONITORING" in txt or "IN_POSITION" in txt:
                    ok = True
                    # Surface meaningful state markers
                    if "STARTUP_REFUSED_NAKED" in txt:
                        state_marker = "REFUSED_NAKED"
                    elif "ORPHAN POSITION DETECTED" in txt:
                        state_marker = "ADOPTED_ORPHAN"
                    elif "IN_POSITION" in txt:
                        state_marker = "IN_POSITION"
                    elif "MONITORING" in txt:
                        state_marker = "MONITORING"
                    else:
                        state_marker = "CONNECTED"
                else:
                    # Grab last 3 non-blank lines as the failure signal
                    err_lines = [ln for ln in txt.strip().splitlines() if ln.strip()][-3:]
            else:
                err_lines = ["(log file not created yet)"]
        except Exception as e:
            err_lines = [f"(log read error: {e})"]

        if ok:
            healthy.append(s)
            print(f"  ✓ {symbol:7s} cid={cid}  {state_marker}")
        else:
            failed.append(s)
            print(f"  ✗ {symbol:7s} cid={cid}  NOT RUNNING — last log lines:")
            for line in err_lines:
                # Truncate noisy lines so the diagnostic stays scannable
                snippet = line[:140] + ("…" if len(line) > 140 else "")
                print(f"      | {snippet}")
            # Kill the dead session so it doesn't linger
            _tmux(["kill-session", "-t", s["session"]], check=False)

    if not healthy:
        print(f"\nALL {len(spawned)} BOTS FAILED TO START. Aborting.", file=sys.stderr)
        print("Common causes:")
        print("  - VIRTUAL_ENV not set / wrong venv (must have ib_async)")
        print("  - IBKR TWS port wrong or not accepting connections")
        print("  - clientId already in use by another process", file=sys.stderr)
        return 1
    if failed:
        print(f"\n[startup] {len(failed)} of {len(spawned)} bots dead — continuing "
              f"with {len(healthy)} healthy bot(s).")
    spawned = healthy

    # 4. Run watchdog loop for the duration
    print(f"\n═══ CHURN ACTIVE  ({args.duration}s  |  watchdog every {args.watchdog}s) ═══")
    start = time.monotonic()
    watchdog_errors: list[str] = []
    total_cancels = 0
    iteration = 0
    while time.monotonic() - start < args.duration:
        iteration += 1
        elapsed = time.monotonic() - start
        remaining = args.duration - elapsed
        wd_start = time.monotonic()
        wd_counts = await _watchdog_iteration(PORT, watchdog_errors)
        iter_cancels = sum(wd_counts.values())
        total_cancels += iter_cancels
        wd_dt = time.monotonic() - wd_start
        print(f"  watchdog #{iteration:3d}  t={elapsed:6.1f}s  rem={remaining:6.1f}s  "
              f"cancelled={iter_cancels}  ({wd_dt:.1f}s)")
        sleep_left = max(0, args.watchdog - wd_dt)
        await asyncio.sleep(sleep_left)

    duration_s = time.monotonic() - start

    # 5. Tear-down: graceful Ctrl-C, wait, kill, final cancel sweep
    print("\n═══ TEAR-DOWN ═══")
    for s in spawned:
        _shutdown_bot(s["session"])
    print("  sent Ctrl-C to all bots; waiting 5s for graceful shutdown...")
    await asyncio.sleep(5.0)
    for s in spawned:
        _tmux(["kill-session", "-t", s["session"]], check=False)
    print("  final cancel sweep...")
    final_errs: list[str] = []
    final_counts = await _watchdog_iteration(PORT, final_errs)
    total_final = sum(final_counts.values())
    print(f"  cancelled {total_final} lingering orders "
          f"({len(final_errs)} non-fatal errors)")
    print("  NOTE: any open FX positions left in account currency cash "
          "ledger must be flattened MANUALLY in TWS — watchdog no longer "
          "auto-flattens to avoid cross-pair attribution errors.")

    # 6. Aggregate audit logs + print report
    print("\n═══ AGGREGATING AUDIT LOGS ═══")
    results = []
    for s in spawned:
        r = _aggregate_pair(s["symbol"], start_wall)
        results.append(r)

    # Write CSV
    out_csv = LOG_ROOT / f"stress_churn_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    _write_csv(results, out_csv)

    _print_summary(results, duration_s, watchdog_errors)

    # Restore the original .gt_portfolio_limits.json on exit
    if args.no_risk_cap:
        if saved_limits_content is not None:
            limits_path.write_bytes(saved_limits_content)
            print(f"\n[risk-cap] restored original {limits_path}")
        else:
            try:
                limits_path.unlink()
                print(f"\n[risk-cap] removed temporary {limits_path}")
            except OSError:
                pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration", type=int, default=300, help="Total churn seconds (default 300 = 5min).")
    p.add_argument("--watchdog", type=float, default=10.0, help="Watchdog interval seconds (default 10).")
    p.add_argument("--pairs", default=None, help="Subset of pairs, comma-sep (default: all 8).")
    # Risk-cap override is ON BY DEFAULT for stress mode. The 8-pair fleet
    # together produces ~$200k notional which would otherwise hit the
    # default $50k portfolio cap after 2 entries and block the other 6.
    # Pass --keep-risk-cap to disable the override (re-enables the gate).
    p.add_argument("--keep-risk-cap", action="store_true",
                   help="KEEP the portfolio risk gate active during stress "
                        "(default: gate is OVERRIDDEN to $1B for the run). "
                        "Use this when intentionally measuring how the risk "
                        "gate interacts with stress load.")
    p.add_argument("--keep-positions", action="store_true",
                   help="KEEP existing FX cash positions on the account "
                        "(default: ALL fleet-currency positions are MARKET-"
                        "flattened to zero before the stress test starts). "
                        "Pre-flatten exists because the engine's naked-"
                        "position guard refuses to start any bot whose pair "
                        "has a non-zero broker_qty. WARNING: ALL EXISTING "
                        "FX EXPOSURE WILL BE CLOSED unless this flag is set.")
    p.add_argument("--trigger-bps", type=float, default=None,
                   help="Override trigger offset above LTP in basis points. "
                        "Default 0 = trigger AT LTP (fires immediately on "
                        "first ASK tick). Use 1 for ~1 pip above (slower "
                        "less-volatile pairs may sit waiting).")
    p.add_argument("--sl-bps", type=float, default=None,
                   help="Override stop-loss as bps below entry. Default 0.5 "
                        "(tight; SL fires within seconds via bid-ask spread). "
                        "Larger (e.g. 5) means positions held longer; smaller "
                        "(e.g. 0.1) may be rejected for being inside the spread.")
    args = p.parse_args()
    # Translate to internal flag (we use the inverse internally for clarity)
    args.no_risk_cap = not args.keep_risk_cap
    # Apply CLI overrides before _amain runs
    if args.trigger_bps is not None:
        global TRIGGER_OFFSET_BPS
        TRIGGER_OFFSET_BPS = args.trigger_bps
    if args.sl_bps is not None:
        global STOP_PCT
        STOP_PCT = args.sl_bps / 10_000.0
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
