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

# 32 IDEALPRO FX pairs (A76 — scaled 8 → 32 for 24×7×5 EC2 chaos loop).
# clientIds 80-111. Combined with sidecars (78, 79) this exactly saturates
# TWS's 32-connection API budget (verified hard ceiling, see A73 attempt).
#
# Selection criteria:
#   - All IDEALPRO-routable
#   - Liquid through at least one of Asia / Europe / NY sessions
#     (no pegged/illiquid exotics like HKD that would never trigger SL)
#   - Covers EUR/GBP/AUD/NZD/CAD/CHF/JPY/SEK/NOK family — gives the
#     state-↔-broker drift defenses (A42/A43) realistic cross-currency
#     concurrency to stress
#
# Original 8-pair fallback: comment out the cross/Scandi entries below
# to revert to the 8-major config that the FX chaos work was perfected on.
PAIRS = [
    # ── Tier 1: original 8 majors (FX "perfection" baseline) ──
    {"symbol": "EURUSD", "client_id": 80, "qty": 25000},
    {"symbol": "GBPUSD", "client_id": 81, "qty": 25000},
    {"symbol": "USDJPY", "client_id": 82, "qty": 25000},
    {"symbol": "AUDUSD", "client_id": 83, "qty": 25000},
    {"symbol": "USDCHF", "client_id": 84, "qty": 25000},
    {"symbol": "USDCAD", "client_id": 85, "qty": 25000},
    {"symbol": "NZDUSD", "client_id": 86, "qty": 25000},
    {"symbol": "EURJPY", "client_id": 87, "qty": 25000},
    # ── Tier 2: EUR + GBP crosses (10) ──
    {"symbol": "EURGBP", "client_id": 88, "qty": 25000},
    {"symbol": "EURCHF", "client_id": 89, "qty": 25000},
    {"symbol": "EURAUD", "client_id": 90, "qty": 25000},
    {"symbol": "EURCAD", "client_id": 91, "qty": 25000},
    {"symbol": "EURNZD", "client_id": 92, "qty": 25000},
    {"symbol": "GBPJPY", "client_id": 93, "qty": 25000},
    {"symbol": "GBPCHF", "client_id": 94, "qty": 25000},
    {"symbol": "GBPAUD", "client_id": 95, "qty": 25000},
    {"symbol": "GBPCAD", "client_id": 96, "qty": 25000},
    {"symbol": "GBPNZD", "client_id": 97, "qty": 25000},
    # ── Tier 3: AUD / NZD / CAD / CHF crosses (10) ──
    {"symbol": "AUDJPY", "client_id":  98, "qty": 25000},
    {"symbol": "AUDCHF", "client_id":  99, "qty": 25000},
    {"symbol": "AUDCAD", "client_id": 100, "qty": 25000},
    {"symbol": "AUDNZD", "client_id": 101, "qty": 25000},
    {"symbol": "CADJPY", "client_id": 102, "qty": 25000},
    {"symbol": "CADCHF", "client_id": 103, "qty": 25000},
    {"symbol": "CHFJPY", "client_id": 104, "qty": 25000},
    {"symbol": "NZDJPY", "client_id": 105, "qty": 25000},
    {"symbol": "NZDCHF", "client_id": 106, "qty": 25000},
    {"symbol": "NZDCAD", "client_id": 107, "qty": 25000},
    # NOTE: trimmed to 28 pairs (cids 80-107). Leaves headroom under the 32
    # TWS API-connection cap for: 1 manually-running ticker + the three-truths
    # monitor (cid 177) + the ledger-graph server / ad-hoc one-shots.
    # (Scandi crosses USDSEK 108 / USDNOK 109 / EURSEK 110 / EURNOK 111 removed.)
]

# Tight params so engine cycles fast.
# TRIGGER_OFFSET_BPS controls how far above LTP the STP-LMT BUY trigger is set:
#   0    → trigger AT LTP (fires on first ASK tick — most aggressive)
#   0.1  → 0.01% above LTP (~1 pip on EURUSD; SLOW for less-volatile pairs)
#   1    → 1 bp above LTP (~1 pip above for most pairs)
# Default 0 because previous 0.1 bps left 6 of 8 pairs sitting in MONITORING
# for the entire stress run waiting for triggers to cross. AT-LTP fires now.
TRIGGER_OFFSET_BPS = 0.0

# STOP_PCT: protective stop as a fraction of entry price.
#   0.0002 = 2 bp = ~2 pips on EURUSD  → too wide; rarely hits in 10s,
#                                         leaves naked positions after cancel
#   0.0001 = 1 bp = ~1 pip on EURUSD   → still wide on JPY pairs
#   0.00005 = 0.5 bp = ~0.5 pip        → triggers on bid-side spread alone
#                                         → guaranteed fire within seconds
# Default 0.00005 (0.5 bp) for stress mode so EVERY entry's SL fires within
# the active window, producing full BUY→SL→re-entry cycles. The engine's
# tick-grid rounder will snap to venue minimum if 0.5 bp falls between ticks.
STOP_PCT = 0.00005
OFFSET_FIXED = 0.0005      # parent limit = trigger - 0.0005 (5 pips room for fill)

# Sidecar client IDs (must not collide with bot client_ids OR live trading)
WATCHDOG_CID = 79          # sidecar that flattens / cancels
LTP_FETCH_CID = 78         # sidecar that fetches LTP

PORT = 7497


# ────────────────────────────────────────────────────────────────────────────
# Helpers — venue tick grid
# ────────────────────────────────────────────────────────────────────────────

_TICK = {
    # Tier 1 — original 8 majors
    "EURUSD": 1e-5, "GBPUSD": 1e-5, "AUDUSD": 1e-5, "NZDUSD": 1e-5,
    "USDCAD": 1e-5, "USDCHF": 1e-5,
    "USDJPY": 1e-3, "EURJPY": 1e-3,
    # A76 — Tier 2/3/4 crosses (32-pair config).
    # All *JPY pairs use 1e-3 (quote ~150); everything else 1e-5 (quote ~1).
    # Scandi USD/EUR pairs use 1e-5 (quote ~9-12).
    "EURGBP": 1e-5, "EURCHF": 1e-5, "EURAUD": 1e-5,
    "EURCAD": 1e-5, "EURNZD": 1e-5,
    "GBPJPY": 1e-3, "GBPCHF": 1e-5, "GBPAUD": 1e-5,
    "GBPCAD": 1e-5, "GBPNZD": 1e-5,
    "AUDJPY": 1e-3, "AUDCHF": 1e-5, "AUDCAD": 1e-5, "AUDNZD": 1e-5,
    "CADJPY": 1e-3, "CADCHF": 1e-5,
    "CHFJPY": 1e-3, "NZDJPY": 1e-3,
    "NZDCHF": 1e-5, "NZDCAD": 1e-5,
    "USDSEK": 1e-5, "USDNOK": 1e-5, "EURSEK": 1e-5, "EURNOK": 1e-5,
}


def _round_to_tick(px: float, symbol: str) -> float:
    t = _TICK.get(symbol, 1e-5)
    return round(round(px / t) * t, 8)


def _is_jpy(sym: str) -> bool:
    return sym.endswith("JPY")


def _offset_fixed_for(sym: str) -> float:
    # JPY pairs quote ~150, so 0.05 = 5 pips. Non-JPY ~1, so 0.0005 = 5 pips.
    return 0.05 if _is_jpy(sym) else OFFSET_FIXED


# ────────────────────────────────────────────────────────────────────────────
# IBKR sidecar — fetch LTP, cancel, flatten
# ────────────────────────────────────────────────────────────────────────────

async def _fetch_ltp(ib, symbol: str) -> Optional[float]:
    from ib_async import Forex
    contract = Forex(symbol)
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
# Pre-flatten — wipe ALL FX cash balances to zero before stress
# ─────────────────────────────────────────────────────────────────────────

# All currencies that the stress fleet touches (base + quote of each).
# Used to scan accountValues() for non-zero balances that need flattening.
# A76: bumped 7 → 9 currencies to include SEK + NOK from Scandi crosses.
_FLEET_CCY = {"EUR", "GBP", "JPY", "AUD", "CHF", "CAD", "NZD", "SEK", "NOK"}


async def _flatten_all_fx(port: int) -> tuple[int, list[str]]:
    """Pre-flight: bring every fleet-touched FX position to zero.

    For each non-zero balance in `_FLEET_CCY`:
      * positive balance (long) → MARKET SELL of {ccy}/USD for the qty
      * negative balance (short) → MARKET BUY of {ccy}/USD for abs(qty)
      * tiny balance (|qty| < 1) → ignore (sub-unit dust)

    Waits ~5 seconds after placement for fills to settle, then returns.
    Pair routing: for JPY/CHF/CAD which IBKR quotes USDXXX, we use the
    USDxxx pair and reverse the side (because qty is in XXX-base terms).

    Returns (orders_placed, error_log).
    """
    from ib_async import IB, Forex, MarketOrder
    errors: list[str] = []
    placed = 0

    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=WATCHDOG_CID),
            timeout=10.0,
        )
        await asyncio.sleep(2.0)  # let accountValues stream populate

        # 1. Read all fleet-currency balances
        balances: dict[str, float] = {}
        for v in ib.accountValues():
            if v.tag == "CashBalance" and v.currency in _FLEET_CCY:
                try:
                    balances[v.currency] = float(v.value)
                except (TypeError, ValueError):
                    pass

        print("  pre-flatten balances:")
        for ccy in sorted(_FLEET_CCY):
            bal = balances.get(ccy, 0.0)
            tag = " ← will flatten" if abs(bal) >= 1.0 else ""
            print(f"    {ccy}  {bal:+15.2f}{tag}")

        # A61 (2026-06-10): global cancel before flatten. Belt-and-
        # suspenders — the per-bot cancel sweep in `_watchdog_iteration`
        # cannot cross-client cancel without masterClientId set in TWS
        # (clientId=79 silently ignored when cancelling orders placed
        # by clientIds 80-87). `reqGlobalCancel()` is a privileged
        # account-wide cancel that bypasses ownership entirely. Always
        # safe to call here — bots are already dead by this point.
        try:
            ib.reqGlobalCancel()
            await asyncio.sleep(2.0)  # let cancels propagate
            print(f"  reqGlobalCancel() issued, 2s settle")
        except Exception as e:
            errors.append(f"reqGlobalCancel: {type(e).__name__}: {e}")

        # A66 (2026-06-10): position-based flatten. The cash-balance
        # approach below mis-attributes balances when a currency comes
        # from MULTIPLE pairs (e.g. EUR balance = sum of EUR holdings
        # from EURUSD AND EURJPY). Live run 2026-06-10: EUR was +50k
        # (25k EURUSD + 25k EURJPY), flatten sold 50k EURUSD → EUR=0
        # but EURJPY position remained open → -4.6M JPY imbalance.
        # The fix: ask the broker for actual FX positions and close
        # each one specifically. ib.positions() returns Position
        # objects with `.contract` (Forex) and `.position` (signed
        # qty in BASE currency).
        try:
            positions = ib.positions()
            fx_positions = []
            for pos in positions:
                c = pos.contract
                # FX contracts have secType='CASH' and a 6-char pair as
                # symbol+currency or localSymbol like "EUR.USD".
                sec_type = getattr(c, 'secType', '') or ''
                if sec_type != 'CASH':
                    continue
                ls = (getattr(c, 'localSymbol', '') or '').replace('.', '')
                pair = ls or (
                    (getattr(c, 'symbol', '') or '')
                    + (getattr(c, 'currency', '') or '')
                )
                qty_signed = float(pos.position or 0)
                if abs(qty_signed) < 1.0:
                    continue
                fx_positions.append((pair, c, qty_signed))

            if fx_positions:
                print(f"  positions()-based flatten: {len(fx_positions)} "
                      f"FX position(s) to close")
                for pair, contract, qty_signed in fx_positions:
                    action = "SELL" if qty_signed > 0 else "BUY"
                    qty = int(abs(qty_signed))
                    try:
                        qualified = await asyncio.wait_for(
                            ib.qualifyContractsAsync(contract), timeout=4.0,
                        )
                        if not qualified:
                            errors.append(f"flatten {pair}: qualify returned empty")
                            continue
                        order = MarketOrder(action, qty, tif='IOC')
                        ib.placeOrder(qualified[0], order)
                        placed += 1
                        print(f"    → {action} {qty:>10,} {pair} "
                              f"(flatten position {qty_signed:+,.0f})")
                    except Exception as e:
                        errors.append(f"flatten {pair} {action} {qty}: "
                                      f"{type(e).__name__}: {e}")
            else:
                print(f"  positions()-based flatten: no FX positions to close")
        except Exception as e:
            errors.append(f"positions() walk: {type(e).__name__}: {e}")

        # ── Fallback: cash-balance approach (kept for the IDEALPRO
        # cash-ledger quirk where positions() returns empty for FX
        # after restart). Only runs if positions()-based flatten
        # placed nothing — otherwise it would double-flatten what we
        # already closed above.
        positions_flatten_placed = placed  # snapshot the count
        # 2a. NON-USD-BASE pairs (EUR/GBP/AUD/NZD) — flatten by ccy
        # balance. Pair is XXXUSD; positive XXX balance = LONG XXXUSD
        # → SELL to flatten. Negative = BUY back.
        non_usd_base = {"EUR", "GBP", "AUD", "NZD"}
        for ccy, bal in balances.items():
            # A66 fallback gate: skip cash-balance flatten if A66 positions()
            # walk already placed orders. The positions() data is authoritative
            # — running both layers would double-flatten.
            if positions_flatten_placed > 0:
                break
            if ccy not in non_usd_base:
                continue
            if abs(bal) < 1.0:
                continue
            qty = int(abs(bal))
            pair = f"{ccy}USD"
            action = "SELL" if bal > 0 else "BUY"
            try:
                contract = Forex(pair)
                qualified = await asyncio.wait_for(
                    ib.qualifyContractsAsync(contract), timeout=4.0,
                )
                if not qualified:
                    errors.append(f"flatten {pair}: qualify returned empty")
                    continue
                # A61 (2026-06-10): tif='IOC' overrides the account
                # preset that was auto-forcing GTC on every order.
                # MARKET orders are inherently single-tick — IOC ensures
                # immediate-or-cancel semantics and avoids IBKR Error
                # 10349 "Order TIF was set to GTC based on order preset".
                order = MarketOrder(action, qty, tif='IOC')
                ib.placeOrder(qualified[0], order)
                placed += 1
                print(f"    → {action} {qty:>10,} {pair} (flatten {bal:+.0f} {ccy})")
            except Exception as e:
                errors.append(f"flatten {pair} {action} {qty}: "
                              f"{type(e).__name__}: {e}")

        # 2b. USD-BASE pairs (USDJPY/USDCHF/USDCAD) — legacy fallback.
        # A66 (2026-06-10): superseded by the top-level positions() walk
        # which covers ALL FX pairs. Skip when A66 already placed orders.
        if positions_flatten_placed == 0:
            try:
                usd_base_pairs = {"USDJPY", "USDCHF", "USDCAD"}
                for pos in ib.positions():
                    c = pos.contract
                    ls = (getattr(c, 'localSymbol', '') or '').replace('.', '')
                    sym = ls if ls else (
                        (getattr(c, 'symbol', '') or '')
                        + (getattr(c, 'currency', '') or '')
                    )
                    if sym not in usd_base_pairs:
                        continue
                    qty_signed = float(pos.position or 0)
                    if abs(qty_signed) < 1.0:
                        continue
                    qty = int(abs(qty_signed))
                    action = "SELL" if qty_signed > 0 else "BUY"
                    try:
                        qualified = await asyncio.wait_for(
                            ib.qualifyContractsAsync(c), timeout=4.0,
                        )
                        if not qualified:
                            errors.append(f"flatten {sym}: qualify returned empty")
                            continue
                        order = MarketOrder(action, qty, tif='IOC')
                        ib.placeOrder(qualified[0], order)
                        placed += 1
                        print(f"    → {action} {qty:>10,} {sym} (flatten {qty_signed:+.0f} USD-base)")
                    except Exception as e:
                        errors.append(f"flatten {sym} {action} {qty}: "
                                      f"{type(e).__name__}: {e}")
            except Exception as e:
                errors.append(f"positions(): {type(e).__name__}: {e}")

        # 3. Wait for fills to settle, then re-read balances to verify
        if placed > 0:
            print(f"  waiting 6s for {placed} flatten order(s) to settle…")
            await asyncio.sleep(6.0)
            new_balances: dict[str, float] = {}
            for v in ib.accountValues():
                if v.tag == "CashBalance" and v.currency in _FLEET_CCY:
                    try:
                        new_balances[v.currency] = float(v.value)
                    except (TypeError, ValueError):
                        pass
            print("  post-flatten balances:")
            for ccy in sorted(_FLEET_CCY):
                bal = new_balances.get(ccy, 0.0)
                flag = " ✓" if abs(bal) < 1.0 else " ⚠ STILL NON-ZERO"
                print(f"    {ccy}  {bal:+15.2f}{flag}")
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
            sym_compound = (getattr(c, 'symbol', '') or '') + (getattr(c, 'currency', '') or '')
            ls = (getattr(c, 'localSymbol', '') or '').replace('.', '')
            if sym_compound == symbol or ls == symbol:
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


def _bot_alive(session: str) -> bool:
    """True iff the bot's tmux session is running a LIVE python process.

    A killed/crashed bot often leaves its tmux session behind with only the
    log-pipe shell (`cat`/`sh`) as the pane process — the engine is dead. We
    therefore check the pane's CURRENT command, not just session existence:
    a live bot's pane runs `python`/`python3`; a dead one runs `cat`/`sh`/`bash`.
    Arg-format-agnostic and robust (this is exactly how we spotted the dead
    JPM bot — its pane was `sh -c cat >> …log`).
    """
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return False                      # session gone entirely
        return any("python" in ln.lower() for ln in r.stdout.splitlines())
    except Exception:
        return False


async def _respawn_until_alive(fleet, spawn_fn, alive_fn, *,
                               retries: int = 3, settle_s: float = 15.0,
                               stagger_s: float = 0.5, post_s: float = 8.0,
                               sleep_fn=None, log_fn=print):
    """Self-healing respawn: after an initial spawn, VERIFY every bot is alive
    and RE-SPAWN any that aren't — up to `retries` rounds, with a `settle_s`
    pause between rounds so a just-killed clientId's socket frees at TWS
    (the root cause of respawn attrition: reusing a clientId before IBKR
    released it → Error 326 → bot exits, and nothing re-spawned it).

    Pure orchestration — `spawn_fn(bot)->(session,log_path)` re-launches a
    bot, `alive_fn(session)->bool` checks liveness — both injectable, so this
    is unit-testable without tmux/IBKR. Mutates each bot's session/log_path
    in place on respawn. Returns (alive_count, rounds_used).
    """
    import asyncio as _aio
    sleep_fn = sleep_fn or _aio.sleep
    await sleep_fn(post_s)
    rounds = 0
    for attempt in range(max(0, retries)):
        dead = [b for b in fleet if not alive_fn(b["session"])]
        if not dead:
            break
        rounds = attempt + 1
        log_fn(f"  [SELF-HEAL] respawn round {rounds}: {len(dead)} not alive → "
               f"{[b.get('symbol') for b in dead]}")
        await sleep_fn(settle_s)            # let killed clientId sockets free
        for b in dead:
            try:
                session, log_path = spawn_fn(b)
                b["session"] = session
                b["log_path"] = log_path
            except Exception as e:
                log_fn(f"  [SELF-HEAL] respawn {b.get('symbol')} failed: {e}")
            await sleep_fn(stagger_s)
        await sleep_fn(post_s)
    alive = sum(1 for b in fleet if alive_fn(b["session"]))
    log_fn(f"  [SELF-HEAL] {alive}/{len(fleet)} bots alive after {rounds} retry round(s)")
    return alive, rounds


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
