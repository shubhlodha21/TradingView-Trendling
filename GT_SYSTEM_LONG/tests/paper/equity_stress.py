"""EQUITY_STRESS — fire N equity entry orders SIMULTANEOUSLY, one per
client ID, in a single burst.

Unlike stress_churn_equity (which churns enter→SL→re-entry over a long
window with a watchdog) and chaos_test_equity (which injects failures
mid-run), this script does ONE thing: spawn N engine bots at once so that
N BUY entries hit the broker at (as close as possible to) the same moment.

Each bot:
  - has its OWN client_id (taken from the EQUITY PAIRS table, CIDs 80+),
  - trades its OWN symbol (NVDA, AAPL, MSFT, …),
  - enters at trigger = LTP (TRIGGER_OFFSET_BPS=0) so the entry fires on
    the first tick instead of sitting and waiting — that's what makes the
    burst land "at a single time".

Default count is 29. The hard ceiling is TWS's 32-connection API limit;
with the LTP sidecar (cid 78) and the watchdog/flatten sidecar (cid 79)
reserved, ~30 bot connections remain, so 29 leaves one slot of headroom.

After the burst it verifies broker truth (what IBKR actually shows as open
orders / positions), holds for --hold seconds so an operator can watch in
TWS, then tears the fleet down (cancel sweep + flatten) unless told to
leave it running.

USAGE:
    python3 -m tests.paper.equity_stress                      # 29 orders, hold 30s, teardown
    python3 -m tests.paper.equity_stress --count 16          # first 16 symbols only
    python3 -m tests.paper.equity_stress --hold 120          # hold 2min before teardown
    python3 -m tests.paper.equity_stress --no-teardown       # leave bots + positions running
    python3 -m tests.paper.equity_stress --pre-flatten       # flatten existing positions first
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow running this file directly (e.g. the IDE ▶ button) as well as via
# `python -m tests.paper.equity_stress`. When launched directly the project
# root isn't on sys.path, so the `tests.paper` package import below fails.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Reuse the EQUITY stress driver — same PAIRS / sidecars / helpers the
# chaos_test_equity.py reference script imports, so this stays in lockstep
# with the rest of the equity test harness.
from tests.paper.stress_churn_equity import (
    PAIRS, PORT, WATCHDOG_CID, LTP_FETCH_CID,
    _kill_stale_sessions, _cleanup_state_files,
    _resolve_all_ltps, _spawn_bot, _shutdown_bot,
    _watchdog_iteration, _flatten_all_fx, _round_to_tick,
    _tmux, TRIGGER_OFFSET_BPS, STOP_PCT,
)

LOG_DIR = PROJECT_ROOT / "tests" / "paper" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# How many orders to fire in the burst. 29 = TWS 32-conn limit minus the
# two sidecars (78 LTP, 79 watchdog) minus one slot of headroom.
DEFAULT_COUNT = 29

# Stress universe = symbols we actually touched this run (filled in at spawn).
_STRESS_SYMS = {p["symbol"] for p in PAIRS}


# ────────────────────────────────────────────────────────────────────────────
# Burst: spawn N bots at once → N entries land together
# ────────────────────────────────────────────────────────────────────────────

async def _fire_burst(count: int, stagger_s: float, pre_flatten: bool) -> list[dict]:
    """Spawn the first `count` PAIRS as bots, each with its own client_id,
    triggering at LTP so every entry fires immediately. Returns the list of
    spawned bot dicts (symbol, client_id, session, log_path, trigger)."""
    label = "EQUITY-STRESS"
    fleet = PAIRS[:count]

    print(f"\n═══ {label}  PRE-FLIGHT ═══")
    _kill_stale_sessions()
    _cleanup_state_files()
    # Risk-cap override — 29 × 100-share positions blow past the default
    # $50k portfolio cap, so lift it for the burst (same as chaos_test).
    limits_path = PROJECT_ROOT / ".gt_portfolio_limits.json"
    limits_path.write_text(json.dumps({
        "max_position_value_usd": 1_000_000_000,
        "max_daily_loss_usd": 1_000_000_000,
        "_set_by": "equity_stress.py",
    }))
    print(f"  risk-cap lifted to $1B for the burst")

    if pre_flatten:
        print(f"  pre-flattening existing equity positions…")
        _, errs = await _flatten_all_fx(PORT)
        if errs:
            print(f"  {len(errs)} non-fatal flatten errors")
    else:
        print(f"  pre-flatten SKIPPED (pass --pre-flatten to clear positions first)")

    print(f"\n═══ {label}  LTP RESOLUTION ═══")
    ltps = await _resolve_all_ltps(PORT)

    print(f"\n═══ {label}  FIRING {len(fleet)} ORDERS  "
          f"(trigger=LTP+{TRIGGER_OFFSET_BPS:+.2f}bps, SL={STOP_PCT*10_000:.2f}bps) ═══")
    spawned: list[dict] = []
    for p in fleet:
        ltp = ltps.get(p["symbol"])
        if ltp is None:
            print(f"  SKIP {p['symbol']:7s} cid={p['client_id']} — no LTP")
            continue
        trigger = _round_to_tick(
            ltp * (1 + TRIGGER_OFFSET_BPS / 10_000.0), p["symbol"],
        )
        session, log_path = _spawn_bot(p, trigger, LOG_DIR)
        spawned.append({
            "symbol": p["symbol"], "client_id": p["client_id"],
            "session": session, "log_path": log_path, "trigger": trigger,
        })
        print(f"  {p['symbol']:7s}  cid={p['client_id']}  trigger={trigger}  qty={p['qty']}")
        # Minimal stagger so spawns land as close to simultaneous as TWS
        # will accept. 0 risks API-pacing rejections; default 0.1s is tight
        # enough to read as "a single time" while staying safe.
        if stagger_s > 0:
            await asyncio.sleep(stagger_s)

    print(f"\n═══ {label}  VERIFYING ENTRIES ═══")
    await asyncio.sleep(6.0)
    entered = 0
    for s in spawned:
        if not s["log_path"].exists():
            print(f"  ? {s['symbol']:7s} cid={s['client_id']}  no log yet")
            continue
        txt = s["log_path"].read_text(encoding='utf-8', errors='replace')
        if "BRACKET_SUBMITTED" in txt or "IN_POSITION" in txt:
            print(f"  ✓ {s['symbol']:7s} cid={s['client_id']}  entry placed")
            entered += 1
        elif "[Gateway] Connected" in txt or "MONITORING" in txt:
            print(f"  ○ {s['symbol']:7s} cid={s['client_id']}  connected, awaiting fill")
        else:
            print(f"  ✗ {s['symbol']:7s} cid={s['client_id']}  not running")
    print(f"\n  {entered}/{len(spawned)} bots placed an entry within 6s")
    return spawned


# ────────────────────────────────────────────────────────────────────────────
# Broker-truth: what does IBKR ACTUALLY show after the burst?
# (Same contract as chaos_test_equity._verify_broker_clean / _print_broker_truth.)
# ────────────────────────────────────────────────────────────────────────────

async def _verify_broker_clean(port: int) -> dict:
    """Query IBKR for open EQUITY positions + working orders in our stress
    universe. Returns {is_clean, open_positions, open_orders, errors}."""
    from ib_async import IB
    result = {"is_clean": True, "open_positions": {}, "open_orders": [], "errors": []}
    ib = IB()
    try:
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=WATCHDOG_CID),
            timeout=8.0,
        )
        await asyncio.sleep(1.0)
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception:
            pass

        TERMINAL_STATUSES = {"Cancelled", "Filled", "ApiCancelled", "Inactive"}
        for pos in ib.positions():
            c = pos.contract
            if (getattr(c, 'secType', '') or '') != 'STK':
                continue
            sym = getattr(c, 'symbol', '') or ''
            if sym not in _STRESS_SYMS:
                continue
            qty_signed = float(pos.position or 0)
            if abs(qty_signed) >= 1.0:
                result["open_positions"][sym] = qty_signed
                result["is_clean"] = False

        for trade in ib.openTrades():
            c = trade.contract
            if (getattr(c, 'secType', '') or '') != 'STK':
                continue
            sym = getattr(c, 'symbol', '') or ''
            if sym not in _STRESS_SYMS:
                continue
            status = trade.orderStatus.status
            if status in TERMINAL_STATUSES or status == "PendingCancel":
                continue
            o = trade.order
            result["open_orders"].append({
                "symbol": sym,
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
        result["is_clean"] = False
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return result


def _print_broker_truth(result: dict) -> None:
    if result["is_clean"]:
        print(f"    BROKER TRUTH:  ✓ all stress symbols flat, no open orders")
        return
    if result.get("open_positions"):
        print(f"      Open equity positions ({len(result['open_positions'])}):")
        for sym, qty in sorted(result["open_positions"].items()):
            print(f"        {sym:<6s} {qty:+8,.0f} shares")
    if result["open_orders"]:
        print(f"      Working orders at broker ({len(result['open_orders'])}):")
        for o in result["open_orders"][:32]:
            pid = f" parent={o['parent_id']}" if o['parent_id'] else ""
            print(f"        {o['symbol']:7s} {o['action']:4s} {o['order_type']:8s} "
                  f"qty={o['qty']:>7,} broker_id={o['broker_id']} "
                  f"status={o['status']}{pid}")
    if result["errors"]:
        print(f"      Verifier errors:")
        for e in result["errors"]:
            print(f"        {e}")


# ────────────────────────────────────────────────────────────────────────────
# Teardown: Ctrl-C bots → kill sessions → cancel sweep → flatten
# (Same sequence as chaos_test_equity._shutdown_fleet.)
# ────────────────────────────────────────────────────────────────────────────

async def _teardown(spawned: list[dict]) -> None:
    label = "EQUITY-STRESS"
    print(f"\n═══ {label}  TEAR-DOWN ═══")
    for s in spawned:
        _shutdown_bot(s["session"])
    await asyncio.sleep(5.0)
    for s in spawned:
        _tmux(["kill-session", "-t", s["session"]], check=False)
    errs: list[str] = []
    counts = await _watchdog_iteration(PORT, errs)
    print(f"  final cancel sweep: {sum(counts.values())} orders")
    await asyncio.sleep(2.0)
    print(f"  flattening any naked positions…")
    n_flatten, flat_errs = await _flatten_all_fx(PORT)
    if n_flatten or flat_errs:
        print(f"  flatten: placed {n_flatten} market order(s), "
              f"{len(flat_errs)} non-fatal errors")
        for e in flat_errs[:5]:
            print(f"    {e}")
    else:
        print(f"  flatten: already flat ✓")


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

async def _amain(args) -> int:
    count = max(1, min(args.count, len(PAIRS)))
    if count != args.count:
        print(f"[equity_stress] clamped count {args.count} → {count} "
              f"(have {len(PAIRS)} symbols)", file=sys.stderr)

    spawned = await _fire_burst(count, args.stagger, args.pre_flatten)
    if not spawned:
        print("No bots spawned — aborting.", file=sys.stderr)
        return 1

    print(f"\n═══ EQUITY-STRESS  HOLD  ({args.hold:.0f}s) ═══")
    print(f"  {len(spawned)} bots live — watch them in TWS now")
    await asyncio.sleep(args.hold)

    print(f"\n  ── BROKER-TRUTH VERIFICATION ──")
    broker_truth = await _verify_broker_clean(PORT)
    _print_broker_truth(broker_truth)

    if args.no_teardown:
        print(f"\n[equity_stress] --no-teardown: leaving {len(spawned)} bots "
              f"+ positions running. Ctrl-C each tmux session manually, or run "
              f"`python3 -m tests.paper.equity_stress --teardown-only` style cleanup.")
        return 0

    await _teardown(spawned)
    print(f"\n  ── POST-TEARDOWN BROKER TRUTH ──")
    _print_broker_truth(await _verify_broker_clean(PORT))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--count", type=int, default=DEFAULT_COUNT,
                   help=f"Number of orders to fire at once (default {DEFAULT_COUNT}; "
                        f"capped at {len(PAIRS)} symbols / TWS 32-conn limit).")
    p.add_argument("--stagger", type=float, default=0.1,
                   help="Seconds between spawns (default 0.1 — near-simultaneous). "
                        "Set 0 for zero stagger; raise if TWS rejects on API pacing.")
    p.add_argument("--hold", type=float, default=30.0,
                   help="Seconds to hold the burst before teardown (default 30).")
    p.add_argument("--pre-flatten", action="store_true",
                   help="Flatten existing equity positions before the burst.")
    p.add_argument("--no-teardown", action="store_true",
                   help="Leave bots + positions running instead of tearing down.")
    args = p.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
