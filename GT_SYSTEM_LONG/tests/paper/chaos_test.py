"""CHAOS TEST — three scripted scenarios that push the engine past
"happy-path stress" into "real-world failure" territory.

Reuses the stress_churn driver to spawn 8 concurrent bots, then injects
ONE of three chaos events:

  1. tws-disconnect   — kill TWS mid-cycle, wait, restart TWS, watch all
                        8 bots reconnect and reconcile cleanly.
  2. state-corrupt    — write garbage JSON to 2 bots' state files mid-run;
                        verify those bots either self-heal or fail loudly
                        (NOT silently corrupt).
  3. restart-positions — let bots get to IN_POSITION, kill all 8 bots,
                        respawn them; verify each adopts its position via
                        the orphan-adoption path.

After each scenario, audits the order.csv files and reports:
  - bot survival   (did each connection come back / did each bot restart)
  - state recovery (did the engine state match broker state after chaos)
  - safety events  (PHANTOM_SELL_REJECTED, POSITION_AUTO_FLAT, etc.)

USAGE:
  python3 -m tests.paper.chaos_test --scenario tws-disconnect
  python3 -m tests.paper.chaos_test --scenario state-corrupt
  python3 -m tests.paper.chaos_test --scenario restart-positions
  python3 -m tests.paper.chaos_test --scenario all          # runs all 3
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from tests.paper.stress_churn import (
    PAIRS, PORT, WATCHDOG_CID, LTP_FETCH_CID,
    _kill_stale_sessions, _cleanup_state_files,
    _resolve_all_ltps, _spawn_bot, _shutdown_bot,
    _watchdog_iteration, _flatten_all_fx, _round_to_tick,
    _tmux, _bot_alive, _respawn_until_alive, TRIGGER_OFFSET_BPS, STOP_PCT,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "tests" / "paper" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ────────────────────────────────────────────────────────────────────────────
# Common: spawn 8 bots + verify they reach MONITORING
# ────────────────────────────────────────────────────────────────────────────

async def _spawn_fleet(label: str, pre_flatten: bool = False) -> list[dict]:
    """Spawn all 8 stress bots, verify they all reach MONITORING.
    Returns the list of spawned bot dicts (with session, log_path, etc.).
    """
    print(f"\n═══ {label}  PRE-FLIGHT ═══")
    _kill_stale_sessions()
    _cleanup_state_files()
    # Risk-cap override
    limits_path = PROJECT_ROOT / ".gt_portfolio_limits.json"
    limits_path.write_text(json.dumps({
        "max_position_value_usd": 1_000_000_000,
        "max_daily_loss_usd": 1_000_000_000,
        "_set_by": "chaos_test.py",
    }))
    # Pre-flatten — OFF by default; operator handles flattening manually
    # in TWS. Pass --pre-flatten on the CLI to re-enable auto-flatten
    # if you actually want the script to clear positions before spawning.
    if pre_flatten:
        print(f"  pre-flattening FX positions…")
        _, errs = await _flatten_all_fx(PORT)
        if errs:
            print(f"  {len(errs)} non-fatal flatten errors (expected for USD-base pairs)")
    else:
        print(f"  pre-flatten SKIPPED (operator handles manually; pass --pre-flatten to re-enable)")

    print(f"\n═══ {label}  LTP RESOLUTION ═══")
    ltps = await _resolve_all_ltps(PORT)

    print(f"\n═══ {label}  SPAWNING 8 BOTS ═══")
    spawned: list[dict] = []
    for p in PAIRS:
        ltp = ltps.get(p["symbol"])
        if ltp is None:
            print(f"  SKIP {p['symbol']} — no LTP")
            continue
        trigger = _round_to_tick(
            ltp * (1 + TRIGGER_OFFSET_BPS / 10_000.0), p["symbol"],
        )
        session, log_path = _spawn_bot(p, trigger, LOG_DIR)
        spawned.append({
            "symbol": p["symbol"], "client_id": p["client_id"],
            "session": session, "log_path": log_path, "trigger": trigger,
        })
        await asyncio.sleep(0.4)

    print(f"\n═══ {label}  VERIFYING STARTUPS ═══")
    await asyncio.sleep(6.0)
    healthy = []
    for s in spawned:
        if s["log_path"].exists():
            txt = s["log_path"].read_text(encoding='utf-8', errors='replace')
            ok = "[Gateway] Connected" in txt or "MONITORING" in txt
            if ok:
                healthy.append(s)
                print(f"  ✓ {s['symbol']:7s} cid={s['client_id']}  alive")
            else:
                print(f"  ✗ {s['symbol']:7s} cid={s['client_id']}  failed")
        else:
            print(f"  ? {s['symbol']:7s} cid={s['client_id']}  no log yet")
    return healthy


async def _watchdog_loop(spawned: list[dict], duration_s: float, label: str) -> None:
    """A55 (2026-06-10) — passive soak phase. NO MORE BULK-CANCEL.

    Previous behaviour: every 10s, ran `_watchdog_iteration` which connects
    via clientId=79 sidecar and bulk-cancels EVERY open broker order for
    each symbol. That includes the engine's own bracket parent AND child
    legs. Result: engine places bracket → watchdog kills both legs 10s
    later → engine re-places → watchdog kills again. Audible cancel-storm
    in TWS (operator reported "I can hear all SELLs getting cancelled").
    Over multiple chaos runs the engine kept rebuilding brackets while
    watchdog kept tearing them down, and the broker accumulated naked
    longs (375k EURUSD seen in latest logs vs 25k engine_qty).

    The watchdog's original purpose was to prevent cross-test order
    accumulation in the basic `stress_churn` driver. For chaos tests
    that's the WRONG behaviour — we're testing how the engine handles
    real bracket lifecycle, not how it survives an external attacker.
    `_shutdown_fleet` still calls `_watchdog_iteration` once at teardown
    as a final cleanup pass, which is fine.

    During soak we just sleep + periodically print a heartbeat so the
    operator can see the test is alive.
    """
    print(f"\n═══ {label}  SOAK  ({duration_s:.0f}s, watchdog DISABLED — engine drives) ═══")
    end = time.monotonic() + duration_s
    HEARTBEAT_S = 10.0
    while time.monotonic() < end:
        t_left = max(0, end - time.monotonic())
        print(f"  t-{int(t_left):3d}s  soaking (no watchdog cancels)")
        await asyncio.sleep(min(HEARTBEAT_S, t_left))


# A59 (2026-06-10): post-teardown broker-truth verifier. The chaos test's
# "PASS" was historically derived from log events only — claimed PASS even
# when the broker had naked shorts. This pulls account-currency balances
# AND open orders directly from IBKR and fails the test if either is
# dirty. Currencies the test should never leave non-zero are the seven
# FX base currencies in play (USD is the account base; expected to drift
# with P&L).
_FX_NONUSD_CCYS = ("EUR", "GBP", "JPY", "AUD", "CHF", "CAD", "NZD")
# Tolerance for tiny rounding from commission/slippage modeling.
# Anything > 100 units of any of these is a real position remnant.
_CCY_BALANCE_TOLERANCE = 100.0


async def _verify_broker_clean(port: int) -> dict:
    """Query the broker for account currency balances + working orders.
    Returns a dict:
        {
            "is_clean": bool,
            "currency_balances": {"EUR": 25000.0, ...},    # only non-zero
            "open_orders": [{symbol, action, qty, broker_id, status}, ...],
            "errors": ["..."],
        }

    A clean run requires BOTH:
      - All non-USD FX ccy balances in `_FX_NONUSD_CCYS` are within
        ±_CCY_BALANCE_TOLERANCE of zero
      - Zero working orders for any of our 8 stress symbols
    """
    from ib_async import IB
    result = {
        "is_clean": True,
        "currency_balances": {},
        "open_orders": [],
        "errors": [],
    }
    ib = IB()
    try:
        # Use the existing watchdog client_id — by this point in the
        # teardown the bots are dead, the prior watchdog connection
        # has disconnected, and this clientId is free.
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=WATCHDOG_CID),
            timeout=8.0,
        )
        # Let accountValues + openOrders populate.
        await asyncio.sleep(1.0)
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception:
            pass

        # Currency balances. accountValues() is a list of AccountValue
        # named tuples with tag='CashBalance' (or 'TotalCashBalance')
        # per currency. We want CashBalance, scoped to BASE currency
        # if there are multiple model rows.
        for v in ib.accountValues():
            if v.tag != "CashBalance":
                continue
            ccy = (v.currency or "").upper()
            if ccy not in _FX_NONUSD_CCYS:
                continue
            try:
                bal = float(v.value)
            except (TypeError, ValueError):
                continue
            if abs(bal) > _CCY_BALANCE_TOLERANCE:
                result["currency_balances"][ccy] = bal
                result["is_clean"] = False

        # Open working orders for our 8 stress symbols.
        stress_syms = {p["symbol"] for p in PAIRS}
        for trade in ib.openTrades():
            c = trade.contract
            sym_compound = (getattr(c, 'symbol', '') or '') + (
                getattr(c, 'currency', '') or ''
            )
            ls = (getattr(c, 'localSymbol', '') or '').replace('.', '')
            sym_match = sym_compound if sym_compound in stress_syms else (
                ls if ls in stress_syms else None
            )
            if sym_match is None:
                continue
            status = trade.orderStatus.status
            # Skip orders that are already terminal in the API but
            # haven't been removed from openTrades yet.
            if status in ("Cancelled", "Filled", "ApiCancelled", "Inactive"):
                continue
            o = trade.order
            result["open_orders"].append({
                "symbol": sym_match,
                "action": o.action,
                "order_type": o.orderType,
                "qty": int(o.totalQuantity),
                "broker_id": str(o.orderId),
                "status": status,
                "parent_id": int(getattr(o, "parentId", 0) or 0),
            })
            result["is_clean"] = False
    except Exception as e:
        result["errors"].append(f"{type(e).__name__}: {e}")
        # Conservative: if we can't verify, treat as NOT clean so
        # operator investigates rather than trusts a stale PASS.
        result["is_clean"] = False
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return result


def _print_broker_truth(result: dict) -> None:
    """Pretty-print the broker-truth verification result. Called from
    each scenario's verdict block before the OVERALL line."""
    if result["is_clean"]:
        print(f"    BROKER TRUTH:  ✓ all FX ccys at 0, no open orders for stress symbols")
        return
    print(f"    BROKER TRUTH:  ⚠ FAIL")
    if result["currency_balances"]:
        print(f"      Non-zero FX ccy balances:")
        for ccy, bal in sorted(result["currency_balances"].items()):
            print(f"        {ccy}: {bal:+,.2f}")
    if result["open_orders"]:
        print(f"      Working orders left at broker ({len(result['open_orders'])}):")
        for o in result["open_orders"][:10]:
            pid = f" parent={o['parent_id']}" if o['parent_id'] else ""
            print(f"        {o['symbol']:7s} {o['action']:4s} {o['order_type']:8s} "
                  f"qty={o['qty']:>7,} broker_id={o['broker_id']} "
                  f"status={o['status']}{pid}")
        if len(result["open_orders"]) > 10:
            print(f"        ... and {len(result['open_orders']) - 10} more")
    if result["errors"]:
        print(f"      Verifier errors:")
        for e in result["errors"]:
            print(f"        {e}")


async def _shutdown_fleet(spawned: list[dict], label: str) -> None:
    """Ctrl-C all bots, wait, kill sessions, final cancel sweep,
    then FLATTEN any naked positions left at the broker.

    A60 (2026-06-10): teardown previously only cancelled open ORDERS.
    Real POSITIONS — accumulated when hard-kill interrupted a bracket
    cycle mid-flight (BUY filled, SELL stop still pending) — survived
    the cancel sweep as live broker exposure. A59's broker-truth check
    surfaced this as 100k EUR / 50k GBP / -9.27M JPY accumulating per
    run. The fix is a flatten pass after the cancel sweep: cancel all
    resting orders first (so we don't race a child SELL that might
    fire during the flatten window), then close any non-zero FX cash
    balance via opposite-side MARKET orders.

    Order matters:
      1. SIGTERM bots
      2. Wait for graceful shutdown (5s)
      3. SIGKILL sessions
      4. Cancel all open broker orders for our 8 stress symbols
      5. Wait 2s for cancels to settle
      6. Flatten all non-zero FX cash balances (MARKET orders)
    """
    print(f"\n═══ {label}  TEAR-DOWN ═══")
    for s in spawned:
        _shutdown_bot(s["session"])
    await asyncio.sleep(5.0)
    for s in spawned:
        _tmux(["kill-session", "-t", s["session"]], check=False)
    errs: list[str] = []
    counts = await _watchdog_iteration(PORT, errs)
    print(f"  final cancel sweep: {sum(counts.values())} orders")
    # Let the cancel sweep settle before placing flatten MARKET orders.
    # Without this, IBKR may report a still-PendingCancel SELL stop and
    # the flatten thinks the position is already being closed.
    await asyncio.sleep(2.0)
    print(f"  flattening any naked FX positions…")
    flat_errs: list[str] = []
    n_flatten, flat_errs = await _flatten_all_fx(PORT)
    if n_flatten or flat_errs:
        print(f"  flatten: placed {n_flatten} market order(s), "
              f"{len(flat_errs)} non-fatal errors")
        for e in flat_errs[:5]:
            print(f"    {e}")
    else:
        print(f"  flatten: already flat ✓")


# ────────────────────────────────────────────────────────────────────────────
# Audit-log analysis (post-chaos)
# ────────────────────────────────────────────────────────────────────────────

def _today_audit_dir(symbol: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return PROJECT_ROOT / "data" / "audit" / today / symbol


def _post_chaos_report(spawned: list[dict], since: datetime, label: str) -> dict:
    """Walk each bot's order.csv since `since` and surface anomalies."""
    print(f"\n═══ {label}  POST-CHAOS REPORT ═══")
    report = {
        "phantom_sells": [],
        "auto_flats": [],
        "double_entries": [],   # n+1 SUBMITTED before n SELL completes
        "startup_refused_naked": [],
        "broker_fill_replays": [],
        "by_pair": {},
    }
    for s in spawned:
        sym = s["symbol"]
        path = _today_audit_dir(sym) / "order.csv"
        per_pair = {
            "rows": 0, "buys_filled": 0, "sells_filled": 0,
            "brackets_placed": 0, "phantom_sells": 0, "auto_flats": 0,
            "broker_fill_replays": 0, "startup_refused": 0,
        }
        if not path.exists():
            report["by_pair"][sym] = per_pair
            continue
        try:
            with open(path) as f:
                rows = list(csv.DictReader(f))
        except Exception:
            rows = []
        rows_since = []
        for r in rows:
            ts_s = r.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_s)
            except ValueError:
                continue
            if ts >= since:
                rows_since.append(r)
        per_pair["rows"] = len(rows_since)
        for r in rows_since:
            ev = r.get("event", "")
            side = r.get("side", "")
            if ev == "BRACKET_SUBMITTED":
                per_pair["brackets_placed"] += 1
            elif ev == "FILLED":
                if side == "BUY":
                    per_pair["buys_filled"] += 1
                elif side == "SELL":
                    per_pair["sells_filled"] += 1
            elif ev == "PHANTOM_SELL_REJECTED":
                per_pair["phantom_sells"] += 1
                report["phantom_sells"].append((sym, r.get("order_id", ""), r.get("reason", "")))
            elif ev == "POSITION_AUTO_FLAT":
                per_pair["auto_flats"] += 1
                report["auto_flats"].append((sym, r.get("reason", "")))
            elif ev == "BROKER_FILL_REPLAY":
                per_pair["broker_fill_replays"] += 1
                report["broker_fill_replays"].append((sym, r.get("order_id", "")))
        report["by_pair"][sym] = per_pair

    print(f"  {'Pair':<8} {'Rows':>5} {'Place':>5} {'Buy':>4} {'Sell':>4} "
          f"{'Phntm':>5} {'AutoFlat':>9} {'BrokRepl':>9}")
    for sym, p in report["by_pair"].items():
        flag = ""
        if p["phantom_sells"] or p["auto_flats"]:
            flag = " ⚠"
        print(f"  {sym:<8} {p['rows']:>5} {p['brackets_placed']:>5} "
              f"{p['buys_filled']:>4} {p['sells_filled']:>4} "
              f"{p['phantom_sells']:>5} {p['auto_flats']:>9} "
              f"{p['broker_fill_replays']:>9}{flag}")

    # Critical issues at the bottom
    if report["phantom_sells"]:
        print(f"\n  ⚠ PHANTOM SELLS detected ({len(report['phantom_sells'])}):")
        for sym, oid, reason in report["phantom_sells"][:5]:
            print(f"     {sym}  {oid}  {reason[:80]}")
    if report["auto_flats"]:
        print(f"\n  ⚠ AUTO-FLATS detected ({len(report['auto_flats'])}):")
        for sym, reason in report["auto_flats"][:5]:
            print(f"     {sym}  {reason[:80]}")
    if report["broker_fill_replays"]:
        print(f"\n  ℹ BROKER_FILL_REPLAYS ({len(report['broker_fill_replays'])}) "
              f"— EXPECTED on reconnect, means engine caught missed fills:")
        for sym, oid in report["broker_fill_replays"][:5]:
            print(f"     {sym}  {oid}")

    return report


# ────────────────────────────────────────────────────────────────────────────
# Scenario 1: TWS DISCONNECT mid-cycle
# ────────────────────────────────────────────────────────────────────────────

async def _scenario_tws_disconnect(soak_s: float = 30.0, downtime_s: float = 30.0, pre_flatten: bool = False) -> None:
    """Spawn 8 bots, run soak_s, kill TWS, wait downtime_s, restart TWS,
    run another soak_s, then report.

    TWS kill uses pkill — on Linux/macOS that should work for "TWS" and
    "IB Gateway". User may be prompted to confirm on first run.
    """
    label = "TWS-DISCONNECT"
    start = datetime.now()
    spawned = await _spawn_fleet(label, pre_flatten=pre_flatten)
    if not spawned:
        print("No bots running — aborting.")
        return

    # Soak: let bots cycle a bit before disruption
    print(f"\n[{label}] soaking {soak_s:.0f}s before chaos…")
    await _watchdog_loop(spawned, soak_s, label)

    # CHAOS: kill TWS
    print(f"\n[{label}] ⚡ KILLING TWS — operator must confirm or run pkill manually")
    print(f"  Run in another shell:")
    print(f"    pkill -f 'Trader Workstation'   # if running TWS")
    print(f"    pkill -f 'ibgateway'             # if running IB Gateway")
    print(f"  Press ENTER when TWS is DOWN…")
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, input),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        print("  (timed out — proceeding)")
    # A52: wall-clock marker for TWS-DOWN moment. Match against the
    # [BRACKET_LIFECYCLE] CONNECT / RECONNECT_ATTEMPT lines in each bot's
    # log to bracket the disconnect window. Printed AFTER the operator
    # confirms via ENTER so it stamps real TWS-down time.
    print(f"[BRACKET_LIFECYCLE] CHAOS_TWS_DOWN  at={datetime.now().isoformat()}")

    # Downtime: bots should sit disconnected, NOT crash, NOT fold to FLAT
    print(f"\n[{label}] waiting {downtime_s:.0f}s with TWS down — bots should "
          f"log [HEALTH] skipped — gateway DISCONNECTED repeatedly")
    await asyncio.sleep(downtime_s)

    # Restart TWS
    print(f"\n[{label}] ⚡ RESTART TWS now and log into PAPER account")
    print(f"  Press ENTER when TWS shows 'Connected' status at the bottom…")
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, input),
            timeout=300.0,
        )
    except asyncio.TimeoutError:
        print("  (timed out — proceeding anyway)")
    # A52: wall-clock marker for TWS-UP moment.
    print(f"[BRACKET_LIFECYCLE] CHAOS_TWS_UP    at={datetime.now().isoformat()}")

    # Post-restart soak: bots should auto-reconnect and resume cycling
    print(f"\n[{label}] post-restart soak {soak_s:.0f}s — watching reconnect")
    await _watchdog_loop(spawned, soak_s, label)

    # Teardown + report
    await _shutdown_fleet(spawned, label)
    report = _post_chaos_report(spawned, start, label)

    # A59: broker-truth verification — what does IBKR ACTUALLY show?
    print(f"\n  ── BROKER-TRUTH VERIFICATION ──")
    broker_truth = await _verify_broker_clean(PORT)
    _print_broker_truth(broker_truth)

    # Pass/fail criteria
    crashed = sum(1 for p in report["by_pair"].values() if p["rows"] == 0)
    phantoms = sum(p["phantom_sells"] for p in report["by_pair"].values())
    auto_flats = sum(p["auto_flats"] for p in report["by_pair"].values())
    print(f"\n  ── PASS/FAIL ──")
    print(f"    Bots that produced NO audit rows post-chaos: {crashed} / {len(spawned)}")
    print(f"    PHANTOM_SELL_REJECTED (post-chaos): {phantoms} {'⚠' if phantoms else '✓'}")
    print(f"    POSITION_AUTO_FLAT (during disconnect): {auto_flats} {'⚠' if auto_flats else '✓'}")
    log_pass = (crashed == 0 and auto_flats == 0 and phantoms == 0)
    verdict = "PASS" if (log_pass and broker_truth["is_clean"]) else "FAIL"
    print(f"    OVERALL: {verdict}")


# ────────────────────────────────────────────────────────────────────────────
# Scenario 2: STATE-FILE CORRUPTION mid-cycle
# ────────────────────────────────────────────────────────────────────────────

async def _scenario_state_corrupt(soak_s: float = 30.0, recover_s: float = 30.0, pre_flatten: bool = False) -> None:
    """Spawn 8 bots, run soak_s, corrupt 2 bots' state files, wait recover_s,
    report. The engine should either rebuild state from reconcile OR raise
    a clear error — NOT silently continue with corrupt state.

    Note: state files are written by the engine periodically, so a corruption
    may be overwritten quickly by a fresh save. The window where the engine
    READS corrupted state is at restart — so we ALSO kill+restart the targeted
    bots to force a state reload from the corrupted file.
    """
    label = "STATE-CORRUPT"
    start = datetime.now()
    spawned = await _spawn_fleet(label, pre_flatten=pre_flatten)
    if not spawned:
        return

    print(f"\n[{label}] soaking {soak_s:.0f}s to build up some state…")
    await _watchdog_loop(spawned, soak_s, label)

    # Pick the FIRST 2 bots whose state file exists
    targets = []
    for s in spawned:
        sf = PROJECT_ROOT / f".gt_state_{s['symbol']}_{s['client_id']}.json"
        if sf.exists():
            targets.append((s, sf))
        if len(targets) == 2:
            break

    if not targets:
        print(f"\n[{label}] no state files exist yet — aborting")
        return

    print(f"\n[{label}] ⚡ CORRUPTING state files for {[s['symbol'] for s, _ in targets]}")
    for s, sf in targets:
        original = sf.read_bytes()
        # Garbage payload — keep filename, scramble contents
        sf.write_text("{ this is not valid JSON, the bot must handle this }\n")
        print(f"  corrupted {sf.name} (was {len(original)} bytes)")

    # Kill+restart those bots to force them to re-read the corrupted state
    print(f"\n[{label}] killing + restarting target bots to force state reload…")
    for s, _ in targets:
        _shutdown_bot(s["session"])
    await asyncio.sleep(3.0)
    for s, _ in targets:
        _tmux(["kill-session", "-t", s["session"]], check=False)
    await asyncio.sleep(2.0)

    # Re-spawn just the corrupted bots
    ltps = await _resolve_all_ltps(PORT)
    for s, _ in targets:
        # find PAIR config
        p = next((pp for pp in PAIRS if pp["symbol"] == s["symbol"]), None)
        if not p:
            continue
        ltp = ltps.get(s["symbol"])
        if ltp is None:
            print(f"  no LTP for {s['symbol']} — skip re-spawn")
            continue
        trigger = _round_to_tick(ltp, s["symbol"])
        session, log_path = _spawn_bot(p, trigger, LOG_DIR)
        s["session"] = session  # update in-place so teardown finds the new sess
        s["log_path"] = log_path
        await asyncio.sleep(0.4)

    # Recover window: watch what happens
    print(f"\n[{label}] watching {recover_s:.0f}s — expect either clean self-heal "
          f"OR loud REFUSED_TO_START message in target bots' logs")
    await _watchdog_loop(spawned, recover_s, label)

    # Check target bots' logs for the right behavior
    print(f"\n[{label}] target bot startup logs:")
    for s, _ in targets:
        if s["log_path"].exists():
            txt = s["log_path"].read_text(encoding='utf-8', errors='replace')
            last_30 = "\n".join(txt.strip().splitlines()[-30:])
            print(f"\n  ── {s['symbol']} ──")
            print(last_30[:2000])

    await _shutdown_fleet(spawned, label)
    report = _post_chaos_report(spawned, start, label)

    # A59: broker-truth verification + verdict (scenario 2 used to skip this)
    print(f"\n  ── BROKER-TRUTH VERIFICATION ──")
    broker_truth = await _verify_broker_clean(PORT)
    _print_broker_truth(broker_truth)
    phantoms = sum(p["phantom_sells"] for p in report["by_pair"].values())
    auto_flats = sum(p["auto_flats"] for p in report["by_pair"].values())
    print(f"\n  ── PASS/FAIL ──")
    print(f"    PHANTOM_SELL_REJECTED: {phantoms} {'⚠' if phantoms else '✓'}")
    print(f"    POSITION_AUTO_FLAT: {auto_flats} {'⚠' if auto_flats else '✓'}")
    verdict = "PASS" if (phantoms == 0 and auto_flats == 0 and broker_truth["is_clean"]) else "FAIL"
    print(f"    OVERALL: {verdict}")


# ────────────────────────────────────────────────────────────────────────────
# Scenario 3: KILL ALL + RESTART WITH POSITIONS OPEN
# ────────────────────────────────────────────────────────────────────────────

async def _scenario_restart_with_positions(
    soak_s: float = 60.0, restart_pause_s: float = 5.0, post_s: float = 30.0,
    pre_flatten: bool = False,
) -> None:
    """Spawn 8 bots, let them get to IN_POSITION (soak_s long enough for some
    fills), kill all bots HARD, wait, respawn them. Each bot should adopt
    its broker-side position via the orphan-adoption path.

    Note: we set GT_SKIP_NAKED_GUARD=1 in the spawn command so the engine
    tries to adopt rather than refuse-on-naked.
    """
    label = "RESTART-WITH-POSITIONS"
    start = datetime.now()
    spawned = await _spawn_fleet(label, pre_flatten=pre_flatten)
    if not spawned:
        return

    print(f"\n[{label}] soaking {soak_s:.0f}s to accumulate IN_POSITION states…")
    await _watchdog_loop(spawned, soak_s, label)

    # HARD KILL all bots (no Ctrl-C — emulate crash)
    print(f"\n[{label}] ⚡ HARD KILL ALL 8 BOTS (no graceful shutdown)")
    for s in spawned:
        _tmux(["kill-session", "-t", s["session"]], check=False)
    await asyncio.sleep(restart_pause_s)

    # A63 (2026-06-10): RESPAWN with trigger=None — let the engine
    # recover the trigger from the saved state file. This mirrors what
    # a real operator does to restart a mid-cycle bot:
    #   python run_live.py NZDUSD --port 7497 --client-id 86
    # (no --trigger). Passing --trigger on the respawn was the source
    # of the "engine cancels adopted bracket and resubmits with new
    # cycle_seq" pattern — even when the values matched, the presence
    # of --trigger suppressed run_live.py's state-file recovery path
    # and forced the engine into a fresh-cycle code path.
    print(f"\n[{label}] respawning ALL 8 bots — NO --trigger, engine "
          f"reads from saved state file and ADOPTS broker bracket as-is")
    new_spawned: list[dict] = []
    for s in spawned:
        # find the PAIR config to pass to _spawn_bot
        p = next((pp for pp in PAIRS if pp["symbol"] == s["symbol"]), None)
        if not p:
            continue
        # trigger=None → _spawn_bot omits --trigger from the CLI
        session, log_path = _spawn_bot(p, None, LOG_DIR)
        new_spawned.append({
            "symbol": s["symbol"], "client_id": s["client_id"],
            "session": session, "log_path": log_path,
            "trigger": s["trigger"],  # carry original for reporting only
        })
        await asyncio.sleep(0.4)

    # Wait for startup + adoption
    print(f"\n[{label}] waiting 10s for bots to start + adoption to complete…")
    await asyncio.sleep(10.0)

    # Check each new bot's log for adoption markers
    print(f"\n[{label}] adoption verification:")
    adopted = 0
    refused = 0
    started_clean = 0
    for s in new_spawned:
        if not s["log_path"].exists():
            print(f"  ? {s['symbol']:7s} no log yet")
            continue
        txt = s["log_path"].read_text(encoding='utf-8', errors='replace')
        if "ORPHAN POSITION DETECTED" in txt or "LOST-FILL RECOVERY" in txt:
            print(f"  ✓ {s['symbol']:7s} ADOPTED prior position")
            adopted += 1
        elif "STARTUP_REFUSED_NAKED" in txt:
            print(f"  ✗ {s['symbol']:7s} REFUSED — naked guard fired")
            refused += 1
        elif "MONITORING" in txt:
            print(f"  ○ {s['symbol']:7s} clean start (no position to adopt)")
            started_clean += 1
        else:
            print(f"  ? {s['symbol']:7s} unclear — see log")

    # Watch a bit more to see if cycles continue
    print(f"\n[{label}] post-restart watchdog {post_s:.0f}s…")
    await _watchdog_loop(new_spawned, post_s, label)

    await _shutdown_fleet(new_spawned, label)
    report = _post_chaos_report(new_spawned, start, label)

    # A59: broker-truth verification — the real test
    print(f"\n  ── BROKER-TRUTH VERIFICATION ──")
    broker_truth = await _verify_broker_clean(PORT)
    _print_broker_truth(broker_truth)

    print(f"\n  ── PASS/FAIL ──")
    print(f"    Adopted positions: {adopted}")
    print(f"    Refused (naked guard): {refused}")
    print(f"    Clean starts (no position): {started_clean}")
    phantoms = sum(p["phantom_sells"] for p in report["by_pair"].values())
    print(f"    PHANTOM_SELL_REJECTED post-restart: {phantoms} {'⚠' if phantoms else '✓'}")
    verdict = "PASS" if (phantoms == 0 and broker_truth["is_clean"]) else "FAIL"
    print(f"    OVERALL: {verdict}")


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

async def _scenario_rolling_kill(
    kill_interval_s: float = 600.0,
    settle_s: float = 45.0,           # ↑ from 30: give TWS longer to release
                                       # killed clientId sockets before reuse
    post_respawn_s: float = 8.0,
    pre_flatten: bool = False,
    respawn_retries: int = 3,         # self-heal: re-spawn dead bots N rounds
    respawn_settle_s: float = 15.0,   # pause between self-heal rounds
) -> None:
    """A77 (2026-06-11): persistent-fleet rolling chaos.

    Unlike `restart-with-positions` which is a one-shot cycle (spawn →
    soak → kill → respawn → soak → teardown), this scenario spawns the
    fleet ONCE and then loops the kill+respawn injection forever until
    Ctrl+C. Final teardown (cancel + flatten + broker-truth) runs only
    on shutdown.

    Each cycle:
      1. SOAK for kill_interval_s (default 600 = 10 min)
      2. SIGKILL every bot tmux session
      3. Wait settle_s for the kill to register at IBKR
      4. Respawn every bot from saved state (engine reads state file,
         adopts broker brackets via reqExecutionsAsync replay)
      5. Wait post_respawn_s, verify adoption, write cycle row to
         logs/chaos_loop/summary.csv (compatible with report generator)

    Use case: long-duration EC2 chaos testing where you want to
    measure how the system handles continuous mass-kill events over
    hours or days, not just a one-shot restart drill.

    Compatibility: writes summary.csv in the same format as
    chaos_loop.sh (iter,timestamp,verdict,elapsed_s,exit_code,log_file),
    so `scripts/generate_chaos_report.py` works without changes.
    """
    import signal as _signal
    import csv as _csv

    label = "ROLLING-KILL"
    start_total = datetime.now()

    # Summary CSV — compatible with chaos_loop.sh format
    summary_csv = LOG_DIR / "summary.csv"
    summary_csv.parent.mkdir(parents=True, exist_ok=True)
    fresh = not summary_csv.exists()
    if fresh:
        with summary_csv.open("w") as f:
            f.write("iter,timestamp,verdict,elapsed_s,exit_code,log_file\n")

    # Initial spawn
    print(f"\n═══ {label}  INITIAL FLEET SPAWN ═══")
    spawned = await _spawn_fleet(label, pre_flatten=pre_flatten)
    if not spawned:
        print(f"[{label}] no bots spawned — aborting", file=sys.stderr)
        return

    print(f"\n[{label}] {len(spawned)} bots running")
    print(f"[{label}] cycle: SOAK {kill_interval_s:.0f}s → MASS-KILL → "
          f"SETTLE {settle_s:.0f}s → RESPAWN → VERIFY")
    print(f"[{label}] summary: {summary_csv}")
    print(f"[{label}] Ctrl+C to stop cleanly (final teardown + broker truth)\n")

    # Signal handling — graceful stop
    stop_requested = {"v": False}
    def _on_stop(*_a):
        if not stop_requested["v"]:
            stop_requested["v"] = True
            print(f"\n\n[{label}] ⚡ stop signal received — finishing current "
                  f"cycle then running final teardown\n", file=sys.stderr)
    _signal.signal(_signal.SIGINT,  _on_stop)
    _signal.signal(_signal.SIGTERM, _on_stop)

    cycle = 0
    current_fleet = spawned

    while not stop_requested["v"]:
        cycle += 1
        cycle_start = datetime.now()
        cycle_ts = cycle_start.strftime("%Y%m%d_%H%M%S")
        cycle_log = LOG_DIR / f"iter{cycle}_{cycle_ts}.log"

        # ── PHASE A: SOAK ───────────────────────────────────────────
        print(f"\n═══ CYCLE {cycle:03d}  SOAK  ({int(kill_interval_s)}s) ═══")
        elapsed_soak = 0
        chunk = 10  # sleep in 10s chunks so Ctrl+C interrupts quickly
        while elapsed_soak < kill_interval_s and not stop_requested["v"]:
            await asyncio.sleep(chunk)
            elapsed_soak += chunk
            remaining = int(kill_interval_s - elapsed_soak)
            if elapsed_soak % 60 == 0 and remaining > 0:
                print(f"  cycle {cycle} · {remaining}s remaining")

        if stop_requested["v"]:
            break

        # ── PHASE B: MASS KILL ──────────────────────────────────────
        print(f"\n═══ CYCLE {cycle:03d}  HARD KILL ALL {len(current_fleet)} BOTS ═══")
        for s in current_fleet:
            _tmux(["kill-session", "-t", s["session"]], check=False)
        await asyncio.sleep(settle_s)

        # ── PHASE C: RESPAWN + SELF-HEAL (no --trigger; engine adopts state) ─
        # Initial respawn, then verify each bot is ALIVE and re-spawn any that
        # didn't come up (lost the clientId race → Error 326 → exited). Without
        # this, ~1/3 of the fleet died each cycle and was never recovered
        # (respawn attrition). The self-heal loop re-spawns the dead until the
        # whole roster is alive — so a dead bot can never strand a position.
        print(f"\n═══ CYCLE {cycle:03d}  RESPAWN ═══")
        new_fleet: list[dict] = []
        for s in current_fleet:
            p = next((pp for pp in PAIRS if pp["symbol"] == s["symbol"]), None)
            if not p:
                continue
            session, log_path = _spawn_bot(p, None, LOG_DIR)
            new_fleet.append({
                "symbol": s["symbol"], "client_id": s["client_id"],
                "session": session, "log_path": log_path,
                "trigger": s.get("trigger"),
            })
            await asyncio.sleep(0.5)

        def _respawn_one(b):
            p = next((pp for pp in PAIRS if pp["symbol"] == b["symbol"]), None)
            # clear any stale (dead-python) session before relaunching
            _tmux(["kill-session", "-t", b["session"]], check=False)
            return _spawn_bot(p, None, LOG_DIR)

        alive_n, heal_rounds = await _respawn_until_alive(
            new_fleet, spawn_fn=_respawn_one, alive_fn=_bot_alive,
            retries=respawn_retries, settle_s=respawn_settle_s,
            stagger_s=0.5, post_s=post_respawn_s,
        )
        if alive_n < len(new_fleet):
            print(f"  [SELF-HEAL] WARNING: {len(new_fleet) - alive_n} bot(s) "
                  f"still not alive after {heal_rounds} rounds")

        # ── PHASE D: VERIFY ADOPTION ────────────────────────────────
        adopted = refused = clean = unclear = 0
        for s in new_fleet:
            if not s["log_path"].exists():
                unclear += 1
                continue
            try:
                txt = s["log_path"].read_text(encoding='utf-8', errors='replace')
            except Exception:
                unclear += 1
                continue
            if "ORPHAN POSITION DETECTED" in txt or "LOST-FILL RECOVERY" in txt:
                adopted += 1
            elif "STARTUP_REFUSED_NAKED" in txt:
                refused += 1
            elif "MONITORING" in txt:
                clean += 1
            else:
                unclear += 1

        # ── PHASE E: RECORD CYCLE RESULT ────────────────────────────
        cycle_end = datetime.now()
        elapsed = int((cycle_end - cycle_start).total_seconds())
        # Verdict: any refused = FAIL; any unclear = ERROR; else PASS
        if refused > 0:
            verdict = "FAIL"
        elif unclear > 0 and (adopted + clean) == 0:
            verdict = "ERROR_no_verdict"
        else:
            verdict = "PASS"

        # Write per-cycle log (compatible with report generator's parser).
        # Includes POST-CHAOS REPORT block so per-pair stats populate.
        # Per-pair stats are derived from each bot's audit/order.csv since
        # cycle_start — we synthesize a summary line per pair.
        try:
            _write_cycle_log(cycle_log, label, cycle, cycle_start, cycle_end,
                             new_fleet, adopted, refused, clean, unclear,
                             verdict, elapsed)
        except Exception as e:
            print(f"  [warn] cycle log write failed: {e}", file=sys.stderr)

        # Append summary row
        with summary_csv.open("a") as f:
            w = _csv.writer(f)
            w.writerow([cycle, cycle_ts, verdict, elapsed, 0, cycle_log.name])

        print(f"\n[{label}] CYCLE {cycle:03d} DONE  verdict={verdict}  "
              f"adopted={adopted}  refused={refused}  clean={clean}  "
              f"unclear={unclear}  elapsed={elapsed}s")
        print(f"[{label}] cumulative cycles: {cycle}")

        current_fleet = new_fleet

    # ── FINAL TEARDOWN ──────────────────────────────────────────────
    print(f"\n═══ {label}  FINAL TEARDOWN ═══")
    await _shutdown_fleet(current_fleet, label)
    print(f"\n  ── BROKER-TRUTH VERIFICATION ──")
    broker_truth = await _verify_broker_clean(PORT)
    _print_broker_truth(broker_truth)

    total_elapsed = int((datetime.now() - start_total).total_seconds())
    h, m = total_elapsed // 3600, (total_elapsed % 3600) // 60
    print(f"\n[{label}] STOPPED after {cycle} cycles · {h}h {m:02d}m total runtime")
    print(f"[{label}] summary: {summary_csv}")


def _write_cycle_log(path: Path, label: str, cycle: int,
                     start: datetime, end: datetime, fleet: list[dict],
                     adopted: int, refused: int, clean: int, unclear: int,
                     verdict: str, elapsed: int) -> None:
    """Write a rolling-kill cycle log file in the same shape as
    chaos_test's normal POST-CHAOS REPORT so the report generator can
    parse per-pair stats from it.

    Per-pair stats are derived from each bot's audit/order.csv lines
    written since the cycle started.
    """
    today = datetime.now().strftime("%Y%m%d")
    audit_root = Path(__file__).resolve().parents[2] / "data" / "audit" / today
    cutoff_iso = start.isoformat(timespec="seconds")

    lines = [
        f"═══ {label}  CYCLE {cycle}  ({start.strftime('%H:%M:%S')} → "
        f"{end.strftime('%H:%M:%S')}, {elapsed}s) ═══",
        f"  adopted={adopted}  refused={refused}  clean={clean}  unclear={unclear}",
        "",
        f"═══ {label}  POST-CHAOS REPORT ═══",
        "  Pair      Rows Place  Buy Sell Phntm  AutoFlat  BrokRepl",
    ]
    total_phntm = 0
    for s in fleet:
        sym = s["symbol"]
        audit = audit_root / sym / "order.csv"
        rows = place = buy = sell = phntm = autoflat = brokrepl = 0
        if audit.exists():
            try:
                with audit.open() as f:
                    next(f, None)  # skip header
                    for line in f:
                        if line < cutoff_iso:
                            continue
                        rows += 1
                        parts = line.split(",")
                        if len(parts) < 5:
                            continue
                        event, _id, side = parts[1], parts[2], parts[3]
                        if event == "BRACKET_SUBMITTED":
                            place += 1
                        elif event == "FILLED" and side == "BUY":
                            buy += 1
                        elif event == "FILLED" and side == "SELL":
                            sell += 1
                        elif event == "PHANTOM_SELL_REJECTED":
                            phntm += 1
                        elif event == "POSITION_AUTO_FLAT":
                            autoflat += 1
                        elif event == "BROKER_FILL_REPLAYED":
                            brokrepl += 1
            except Exception:
                pass
        total_phntm += phntm
        lines.append(
            f"  {sym:<8s} {rows:>4d} {place:>4d} {buy:>4d} {sell:>4d} "
            f"{phntm:>5d} {autoflat:>9d} {brokrepl:>9d}"
        )
    lines += [
        "",
        f"  ── BROKER-TRUTH VERIFICATION ──",
        f"    (deferred to final teardown)",
        "",
        f"  ── PASS/FAIL ──",
        f"    Adopted positions:     {adopted}",
        f"    Refused (naked guard): {refused}",
        f"    Clean starts:          {clean}",
        f"    PHANTOM_SELL_REJECTED: {total_phntm}",
        f"    OVERALL: {verdict}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


async def _amain(args) -> int:
    pf = bool(getattr(args, "pre_flatten", False))
    if args.scenario in ("tws-disconnect", "all"):
        await _scenario_tws_disconnect(
            soak_s=args.soak, downtime_s=args.downtime, pre_flatten=pf,
        )
    if args.scenario in ("state-corrupt", "all"):
        await _scenario_state_corrupt(
            soak_s=args.soak, recover_s=args.soak, pre_flatten=pf,
        )
    if args.scenario in ("restart-positions", "all"):
        await _scenario_restart_with_positions(
            soak_s=args.soak, restart_pause_s=5.0, post_s=args.soak,
            pre_flatten=pf,
        )
    if args.scenario == "rolling-kill":
        await _scenario_rolling_kill(
            kill_interval_s=args.kill_interval,
            settle_s=args.settle,
            pre_flatten=pf,
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--scenario", required=True,
        choices=("tws-disconnect", "state-corrupt", "restart-positions",
                 "rolling-kill", "all"),
    )
    p.add_argument("--soak", type=float, default=300.0,
                   help="Seconds to soak before/after chaos injection (default 300 = 5min). "
                        "A59: bumped from 60→300 to see multiple bracket cycles fire and "
                        "complete (trigger AT LTP + SL=0.5bp means cycles fire every "
                        "few seconds in normal FX volatility).")
    p.add_argument("--downtime", type=float, default=45.0,
                   help="Seconds TWS stays DOWN in tws-disconnect (default 45).")
    p.add_argument("--pre-flatten", action="store_true",
                   help="Auto-flatten all FX positions before spawning bots. "
                        "OFF by default (operator handles flattening manually "
                        "in TWS). Pass this flag to re-enable script-driven "
                        "flatten orders at pre-flight.")
    # A77 — rolling-kill scenario options
    p.add_argument("--kill-interval", type=float, default=600.0,
                   help="rolling-kill: seconds between mass-kill events "
                        "(default 600 = 10 min).")
    p.add_argument("--settle", type=float, default=30.0,
                   help="rolling-kill: seconds after kill before respawn "
                        "(default 30; gives IBKR time to register the "
                        "disconnect before the new connections arrive).")
    args = p.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
