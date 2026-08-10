#!/usr/bin/env python3
"""Fetch account activity directly from IBKR for cross-checking the local audit.

Pulls three things for a given trading day:
  1. Executions/fills (reqExecutionsAsync)         — historical, ~7 day window
  2. Open positions (ib.positions())               — CURRENT snapshot only
  3. Account summary (ib.accountSummary())         — CURRENT snapshot only

Writes them to data/audit/ibkr_<kind>_<YYYYMMDD>.{csv,json} and prints a
diff against data/Logs/order_*_<YYYYMMDD>.csv when local logs exist.

Usage:
  python scripts/ibkr_account_audit.py                 # today
  python scripts/ibkr_account_audit.py --date 20260520
  python scripts/ibkr_account_audit.py --date 20260520 --port 4002 --client-id 99

Caveats:
  * IBKR's execution history is limited (~7 days). Older dates return empty.
  * Positions/account summary are LIVE NOW. They do not reconstruct EOD state
    of a past day — for that you need IBKR Flex Web Service (separate setup).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path so we can import nothing — this script is
# standalone and only depends on ib_async, which the project already pins.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

AUDIT_DIR = PROJECT_ROOT / "data" / "audit"
LOGS_DIR = PROJECT_ROOT / "data" / "Logs"


def parse_date(s: str) -> datetime:
    """Parse YYYYMMDD or YYYY-MM-DD into a naive date at 00:00."""
    s = s.strip().replace("-", "")
    return datetime.strptime(s, "%Y%m%d")


async def fetch_executions(ib, day: datetime) -> list[dict]:
    """Pull executions on `day` via ExecutionFilter.

    IBKR's filter `time` field means "executions at or after this time" in
    UTC. We request from start-of-day and then filter the response to that
    same calendar day, so we don't accidentally pick up the next day's fills.
    """
    from ib_async import ExecutionFilter

    # IBKR expects "YYYYMMDD HH:MM:SS" in UTC (or with timezone suffix).
    # Asking for the whole day from 00:00 UTC is wide enough to capture
    # any US session — RTH 13:30-20:00 UTC, ETH 08:00-01:00 UTC next day.
    start = day.replace(hour=0, minute=0, second=0)
    filt = ExecutionFilter(time=start.strftime("%Y%m%d 00:00:00"))

    fills = await ib.reqExecutionsAsync(filt)

    day_str = day.strftime("%Y%m%d")
    out: list[dict] = []
    for f in fills:
        ex = f.execution
        co = f.contract
        cr = f.commissionReport
        # `time` is tz-aware UTC. Convert to date string for filtering.
        t = getattr(ex, "time", None)
        if t is None:
            continue
        if isinstance(t, datetime):
            t_utc = t.astimezone(timezone.utc) if t.tzinfo else t.replace(tzinfo=timezone.utc)
        else:
            continue
        if t_utc.strftime("%Y%m%d") != day_str:
            continue

        out.append({
            "exec_id": ex.execId,
            "order_id": ex.orderId,
            "perm_id": ex.permId,
            "account": ex.acctNumber,
            "symbol": co.symbol,
            "sec_type": co.secType,
            "exchange": ex.exchange,
            "side": ex.side,  # 'BOT' | 'SLD'
            "shares": float(ex.shares),
            "price": float(ex.price),
            "avg_price": float(getattr(ex, "avgPrice", 0.0) or 0.0),
            "cum_qty": float(getattr(ex, "cumQty", 0.0) or 0.0),
            "time_utc": t_utc.isoformat(),
            "commission": float(getattr(cr, "commission", 0.0) or 0.0),
            "realized_pnl": float(getattr(cr, "realizedPNL", 0.0) or 0.0),
            "currency": getattr(cr, "currency", "") or "",
        })
    out.sort(key=lambda r: r["time_utc"])
    return out


def fetch_positions(ib) -> list[dict]:
    out = []
    for p in ib.positions():
        if p.position == 0:
            continue
        out.append({
            "account": p.account,
            "symbol": p.contract.symbol,
            "sec_type": p.contract.secType,
            "currency": p.contract.currency,
            "position": float(p.position),
            "avg_cost": float(p.avgCost),
            "notional": float(p.position) * float(p.avgCost),
        })
    return out


def fetch_account_summary(ib) -> dict:
    """Tag → value (as string, IBKR's native format).

    Numeric tags are common (NetLiquidation, BuyingPower, GrossPositionValue,
    UnrealizedPnL, RealizedPnL, AvailableFunds, etc.); a few are strings
    (AccountType, Currency). We keep value-as-string and let the consumer
    cast — preserves currency suffix and avoids silent precision loss.
    """
    out: dict[str, dict] = {}
    for v in ib.accountSummary():
        out[v.tag] = {
            "value": v.value,
            "currency": v.currency,
            "account": v.account,
        }
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")  # touch so downstream tools see "ran but empty"
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


def load_local_orders(day_str: str) -> list[dict]:
    """Read all order_*_<YYYYMMDD>.csv files into a single list."""
    rows: list[dict] = []
    for csv_path in sorted(LOGS_DIR.glob(f"order_*_{day_str}.csv")):
        symbol = csv_path.stem.split("_")[1]  # order_TSLA_20260519 → TSLA
        with csv_path.open() as f:
            for r in csv.DictReader(f):
                r["_symbol"] = symbol
                r["_src"] = csv_path.name
                rows.append(r)
    return rows


def diff_summary(ibkr_fills: list[dict], local_orders: list[dict]) -> str:
    """Best-effort reconciliation. Local orders log every state transition
    (SUBMITTED, FILLED, ...); IBKR fills are only actual executions, so we
    compare against the FILLED rows only.
    """
    local_fills = [r for r in local_orders if r.get("event") == "FILLED"]

    by_sym_ibkr: dict[str, int] = {}
    by_sym_local: dict[str, int] = {}
    for r in ibkr_fills:
        by_sym_ibkr[r["symbol"]] = by_sym_ibkr.get(r["symbol"], 0) + 1
    for r in local_fills:
        by_sym_local[r["_symbol"]] = by_sym_local.get(r["_symbol"], 0) + 1

    symbols = sorted(set(by_sym_ibkr) | set(by_sym_local))
    lines = ["", "=== Fill-count reconciliation (per symbol) ===",
             f"{'symbol':<8} {'ibkr':>6} {'local':>6} {'delta':>6}"]
    for s in symbols:
        i = by_sym_ibkr.get(s, 0)
        l = by_sym_local.get(s, 0)
        flag = "" if i == l else "  <-- MISMATCH"
        lines.append(f"{s:<8} {i:>6} {l:>6} {i - l:>+6}{flag}")
    if not symbols:
        lines.append("(no fills on either side)")
    return "\n".join(lines)


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"),
                    help="Day to audit, YYYYMMDD or YYYY-MM-DD (default: today)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4001,
                    help="IBKR Gateway/TWS port (default 4001 = paper gateway)")
    ap.add_argument("--client-id", type=int, default=77,
                    help="Distinct client_id to avoid clashing with the live bot")
    ap.add_argument("--out-dir", type=Path, default=AUDIT_DIR)
    ap.add_argument("--no-diff", action="store_true",
                    help="Skip reconciliation against local data/Logs CSVs")
    args = ap.parse_args()

    try:
        day = parse_date(args.date)
    except ValueError as e:
        print(f"Bad --date {args.date!r}: {e}", file=sys.stderr)
        return 2

    age_days = (datetime.now() - day).days
    if age_days > 7:
        print(f"WARNING: --date is {age_days}d old; IBKR usually only keeps "
              f"~7 days of executions. Expect empty fills.", file=sys.stderr)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    day_str = day.strftime("%Y%m%d")

    from ib_async import IB
    ib = IB()
    print(f"[audit] Connecting to {args.host}:{args.port} (clientId={args.client_id})...")
    try:
        await ib.connectAsync(args.host, args.port, clientId=args.client_id)
    except Exception as e:
        print(f"[audit] Connect failed: {e}", file=sys.stderr)
        return 1
    print(f"[audit] Connected. Fetching activity for {day_str}...")

    try:
        execs = await fetch_executions(ib, day)
        # accountSummary needs a brief beat to populate after connect
        await asyncio.sleep(1.0)
        positions = fetch_positions(ib)
        summary = fetch_account_summary(ib)
    finally:
        ib.disconnect()

    exec_path = args.out_dir / f"ibkr_executions_{day_str}.csv"
    pos_path = args.out_dir / f"ibkr_positions_{day_str}.json"
    acct_path = args.out_dir / f"ibkr_account_{day_str}.json"

    write_csv(exec_path, execs)
    write_json(pos_path, {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "for_date": day_str,
        "snapshot_note": "Positions are CURRENT, not end-of-day for the requested date.",
        "positions": positions,
    })
    write_json(acct_path, {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "for_date": day_str,
        "snapshot_note": "Account summary is CURRENT, not end-of-day for the requested date.",
        "summary": summary,
    })

    print(f"[audit] Executions: {len(execs):>4}  -> {exec_path.relative_to(PROJECT_ROOT)}")
    print(f"[audit] Positions : {len(positions):>4}  -> {pos_path.relative_to(PROJECT_ROOT)}")
    print(f"[audit] Acct tags : {len(summary):>4}  -> {acct_path.relative_to(PROJECT_ROOT)}")

    # Quick highlight: equity & exposure right now
    netliq = summary.get("NetLiquidation", {}).get("value", "?")
    gross  = summary.get("GrossPositionValue", {}).get("value", "?")
    bp     = summary.get("BuyingPower", {}).get("value", "?")
    rpnl   = summary.get("RealizedPnL", {}).get("value", "?")
    upnl   = summary.get("UnrealizedPnL", {}).get("value", "?")
    print(f"[audit] NetLiq={netliq}  GrossPos={gross}  BP={bp}  RealPnL={rpnl}  UnrealPnL={upnl}")

    if not args.no_diff:
        local = load_local_orders(day_str)
        if local:
            print(diff_summary(execs, local))
        else:
            print(f"[audit] No local order_*_{day_str}.csv files found; skipping diff.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
