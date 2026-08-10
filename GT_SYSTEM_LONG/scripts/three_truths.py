#!/usr/bin/env python3
"""Three-Truths Reconciliation — engine vs broker vs market.

A81 (2026-06-12): the deepest safety invariant. A position is only
"perfect" when ALL THREE sources of truth agree:

  1. ENGINE  — what the engine believes it holds  (.gt_state_<SYM>_<CID>.json)
  2. BROKER  — what the IBKR account actually holds (ib.positions())
  3. MARKET  — protection is truly LIVE at the venue (a SELL stop with
               status Submitted/PreSubmitted) AND market data is flowing
               (a current LTP is fetchable, so the engine isn't blind)

Coherence rule, per symbol:
  - engine_qty == broker_qty                       (engine ⇄ broker)
  - if holding (qty != 0):
        a live protective SELL exists at the venue  (broker ⇄ market: real)
        AND a current LTP is available              (not trading blind)
  - if flat (qty == 0):
        no live working orders remain               (no orphans)

Exit code: 0 if every symbol is COHERENT, 1 if any drift — so it can gate
a chaos run or a cron health-check.

Usage:
    python3 scripts/three_truths.py --port 7497
    python3 scripts/three_truths.py --port 4002 --symbols GBPUSD,TSLA,USDJPY
    python3 scripts/three_truths.py --port 7497 --no-ltp     # skip price check
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Live working statuses = order is truly active at the venue.
_LIVE_STATUSES = {"Submitted", "PreSubmitted", "PendingSubmit"}

# Cash-baseline file — the "playing field at 0". Snapshot the per-currency
# cash when the account is FLAT, then every later render shows Δ-from-baseline
# so the account's standing funding reads ~0 and only TRADING activity moves a
# currency off zero. A non-zero Δ in a non-base currency = real open FX
# exposure — the bug, upfront, no reliance on positions().
_CASH_BASELINE_FILE = PROJECT_ROOT / ".gt_cash_baseline.json"


def _load_cash_baseline() -> dict:
    try:
        with open(_CASH_BASELINE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cash_baseline(cash: dict) -> None:
    try:
        with open(_CASH_BASELINE_FILE, "w") as f:
            json.dump(cash, f)
    except Exception:
        pass


# ── ENGINE truth ─────────────────────────────────────────────────────────

def _scan_engine_state(symbols: Optional[set]) -> dict:
    """Read every .gt_state_<SYM>_<CID>.json in the project root.

    Returns {symbol: {"qty": signed_float, "open": bool, "cid": int,
                      "state": str, "files": [paths]}}.
    If two engines (different CIDs) claim the same symbol, their quantities
    are summed and a `dup` flag is set — itself a single-writer red flag.
    """
    out: dict = {}
    pat = re.compile(r"\.gt_state_(?P<sym>[A-Z0-9]+)_(?P<cid>\d+)\.json$")
    for fp in glob.glob(str(PROJECT_ROOT / ".gt_state_*.json")):
        m = pat.search(os.path.basename(fp))
        if not m:
            continue
        sym = m.group("sym")
        cid = int(m.group("cid"))
        if symbols and sym not in symbols:
            continue
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        qty = float(d.get("quantity") or 0)
        is_open = bool(d.get("position_open"))
        signed = qty if is_open else 0.0
        rec = out.setdefault(sym, {"qty": 0.0, "open": False, "cids": [],
                                   "state": "", "files": [], "dup": False})
        if rec["cids"]:
            rec["dup"] = True  # second engine on same symbol
        rec["qty"] += signed
        rec["open"] = rec["open"] or is_open
        rec["cids"].append(cid)
        rec["state"] = d.get("state", rec["state"])
        rec["files"].append(os.path.basename(fp))
    return out


# ── LEDGER truth (FL5) — reliable FX position, NO broker connection ──────

def _scan_fill_ledgers(symbols: Optional[set]) -> dict:
    """Read every .gt_fills_<SYM>_<PORT>_<CID>.jsonl — the durable per-bot
    execution journal (FL1–FL4). Returns {symbol: signed_net}.

    This is the RELIABLE position truth, especially for FX: positions() is
    structurally blind to the currency graph's cycle space (dim E−V+C), but
    the ledger is the integral of pair-tagged fills — exact per pair. Reads
    files only → needs NO broker connection, so a ledger-only monitor
    consumes ZERO client-id slots (freeing all 32 for bots).
    """
    out: dict = {}
    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from src.execution.fill_ledger import FillLedger
    except Exception:
        return out
    pat = re.compile(r"\.gt_fills_(?P<sym>[A-Z0-9]+)_(?P<port>\d+)_(?P<cid>\d+)\.jsonl$")
    for fp in glob.glob(str(PROJECT_ROOT / ".gt_fills_*.jsonl")):
        m = pat.search(os.path.basename(fp))
        if not m:
            continue
        sym = m.group("sym")
        if symbols and sym not in symbols:
            continue
        try:
            led = FillLedger(fp)
            n = sum(led.net().values())   # single-symbol file → its net
        except Exception:
            continue
        out[sym] = out.get(sym, 0.0) + n
    return out


def _ledger_expected_cash(symbols: Optional[set]) -> dict:
    """From the durable fill ledgers, compute the EXPECTED per-currency cash
    delta (this is A·x̂ — the cash the broker SHOULD show given our fills).

    For an FX pair BASE/QUOTE, a fill of N base units at price P moves
    +N BASE and −(N·P) QUOTE (sign by side: BUY +, SELL −). For an equity,
    shares aren't a currency — a fill of N shares at P just spends/returns
    N·P of the base currency (USD).

    Compared against IBKR's ACTUAL per-currency cash, this RECONCILES our
    books to the broker's settlement currency-by-currency. A near-zero
    residual = our ledger matches what IBKR actually settled. A large
    residual = a missed fill or external/cross-client activity — caught
    even when the raw per-currency aggregate nets it away (triangular-loop
    blind spot). Reads files only; no broker connection.
    """
    exp: dict = {}
    pat = re.compile(r"\.gt_fills_(?P<sym>[A-Z0-9]+)_(?P<port>\d+)_(?P<cid>\d+)\.jsonl$")
    for fp in glob.glob(str(PROJECT_ROOT / ".gt_fills_*.jsonl")):
        m = pat.search(os.path.basename(fp))
        if not m:
            continue
        sym = m.group("sym")
        if symbols and sym not in symbols:
            continue
        is_fx = (len(sym) == 6 and sym.isalpha()
                 and sym[:3] in _CCY and sym[3:] in _CCY)
        base = sym[:3] if is_fx else None
        quote = sym[3:] if is_fx else "USD"
        try:
            with open(fp) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    sd = str(r.get("side", "")).upper()
                    sign = 1 if sd in ("BOT", "BUY", "B") else \
                           (-1 if sd in ("SLD", "SELL", "S") else 0)
                    if sign == 0:
                        continue
                    try:
                        n = float(r.get("shares", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    p = r.get("price")
                    try:
                        p = float(p) if p is not None else None
                    except (TypeError, ValueError):
                        p = None
                    if is_fx:
                        exp[base] = exp.get(base, 0.0) + sign * n
                        if p is not None:
                            exp[quote] = exp.get(quote, 0.0) - sign * n * p
                    elif p is not None:
                        exp["USD"] = exp.get("USD", 0.0) - sign * n * p
        except Exception:
            continue
    return exp


def _ledger_pair_legs(symbols: Optional[set]) -> dict:
    """Per-PAIR cash legs from the ledger, so the pair→two-currency mapping
    is explicit. For each FX pair BASE/QUOTE: the BASE leg (signed base units
    currently held) and the QUOTE leg (signed quote cash). E.g. long USD.JPY
    → USD +25,000 and JPY −(25,000·price). Buying a pair lifts the base
    currency and spends the quote currency; selling reverses it.

    Returns {pair: [base_ccy, base_amt, quote_ccy, quote_amt]}.
    """
    legs: dict = {}
    pat = re.compile(r"\.gt_fills_(?P<sym>[A-Z0-9]+)_(?P<port>\d+)_(?P<cid>\d+)\.jsonl$")
    for fp in glob.glob(str(PROJECT_ROOT / ".gt_fills_*.jsonl")):
        m = pat.search(os.path.basename(fp))
        if not m:
            continue
        sym = m.group("sym")
        if symbols and sym not in symbols:
            continue
        is_fx = (len(sym) == 6 and sym.isalpha()
                 and sym[:3] in _CCY and sym[3:] in _CCY)
        if not is_fx:
            continue  # only FX has a two-currency leg structure
        base, quote = sym[:3], sym[3:]
        rec = legs.setdefault(sym, [base, 0.0, quote, 0.0])
        try:
            with open(fp) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    sd = str(r.get("side", "")).upper()
                    sign = 1 if sd in ("BOT", "BUY", "B") else \
                           (-1 if sd in ("SLD", "SELL", "S") else 0)
                    if sign == 0:
                        continue
                    try:
                        n = float(r.get("shares", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    p = r.get("price")
                    try:
                        p = float(p) if p is not None else None
                    except (TypeError, ValueError):
                        p = None
                    rec[1] += sign * n                      # BASE leg (+buy / -sell)
                    if p is not None:
                        rec[3] += -sign * n * p             # QUOTE leg (spend on buy)
        except Exception:
            continue
    return legs


# ── BROKER + MARKET truth ────────────────────────────────────────────────

def _canon(c) -> str:
    sec = getattr(c, "secType", "") or ""
    if sec == "STK":
        return getattr(c, "symbol", "") or ""
    if sec == "CASH":
        return (getattr(c, "localSymbol", "") or "").replace(".", "") \
            or (getattr(c, "symbol", "") or "")
    return getattr(c, "symbol", "") or ""


async def _broker_market_truth(port: int, symbols: Optional[set],
                               check_ltp: bool) -> tuple[dict, bool, str, dict]:
    """Returns (rows, connected, error, cash).

    `connected` is False if we could not reach the broker — in which case
    the caller MUST suppress drift verdicts (a failed query is NOT proof
    that the broker is flat). Returning bogus broker_qty=0 and crying
    'DRIFT' would be a silent-failure false alarm — the exact bug class
    this whole system guards against.

    `cash` is {currency: balance} from accountValues CashBalance — the
    HONEST FX truth (forex settles into the per-currency cash ledger). When
    the account is flat, every non-base currency is 0; any non-zero value
    is a real open FX exposure in that currency. This sidesteps positions()
    entirely for FX.
    """
    from ib_async import IB
    out: dict = {}
    cash: dict = {}
    ib = IB()
    connected = False
    err = ""
    try:
        # UNIQUE clientId per process (200 + pid%30 → 200..229, safely above
        # the bot/sidecar range). `watch -n 1` relaunches this script every
        # second; each run takes ~2s, so consecutive runs OVERLAP. With a fixed
        # clientId they collided ("clientId 177 already in use" → false
        # BROKER UNREACHABLE). A per-process id means overlapping runs use
        # different ids and never fight. Range 200-229 avoids bot cids (80-147)
        # and the ledger_server cash poller (178).
        _mon_cid = 200 + (os.getpid() % 30)
        await asyncio.wait_for(
            ib.connectAsync("127.0.0.1", port, clientId=_mon_cid), timeout=10.0)
        connected = True
        await asyncio.sleep(1.5)
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception:
            pass

        # NOTE: the per-currency cash subscribe (reqAccountUpdates) was removed
        # from the watch path — this Gateway's CashBalance feed is dead
        # (accountValues() returns empty), so subscribing only added a wasted
        # ~1.5s to every run, which made watch -n 1 runs overlap MORE. The cash
        # panel stays blank here; use `ledger_server --with-cash` for cash when
        # a Gateway with a live account feed is available. accountValues() is
        # still read below in case the feed ever populates (cheap, non-blocking).
        # Per-currency cash ledger (the reliable FX truth).
        try:
            for v in ib.accountValues():
                if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
                    try:
                        cash[v.currency] = float(v.value)
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass

        for pos in ib.positions():
            sym = _canon(pos.contract)
            if symbols and sym not in symbols:
                continue
            q = float(pos.position or 0)
            if abs(q) < 1e-9:
                continue
            out.setdefault(sym, {"broker_qty": 0.0, "live_sells": 0,
                                 "live_orders": 0, "ltp": None})["broker_qty"] += q

        for t in ib.openTrades():
            sym = _canon(t.contract)
            if symbols and sym not in symbols:
                continue
            st = t.orderStatus.status if t.orderStatus else ""
            if st not in _LIVE_STATUSES:
                continue
            rec = out.setdefault(sym, {"broker_qty": 0.0, "live_sells": 0,
                                       "live_orders": 0, "ltp": None})
            rec["live_orders"] += 1
            if (t.order.action or "").upper() == "SELL":
                rec["live_sells"] += 1
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if not connected:
            print(f"[!] broker UNREACHABLE on port {port}: {err}", file=sys.stderr)
    finally:
        # Only fetch LTPs if we actually connected.
        if connected and check_ltp:
            try:
                await _fill_ltps(ib, out)
            except Exception as e:
                print(f"[!] ltp error: {e}", file=sys.stderr)
        try:
            ib.disconnect()
        except Exception:
            pass
    return out, connected, err, cash


async def _fill_ltps(ib, rows: dict) -> None:
    """Fetch a current LTP for each symbol in `rows` (asset-routed)."""
    from ib_async import Stock, Forex
    for sym, rec in rows.items():
        # FX pairs are 6 uppercase letters of known currency codes; else STK.
        is_fx = (len(sym) == 6 and sym.isalpha()
                 and sym[:3] in _CCY and sym[3:] in _CCY)
        contract = Forex(sym) if is_fx else Stock(sym, "SMART", "USD")
        try:
            q = await asyncio.wait_for(
                ib.qualifyContractsAsync(contract), timeout=4.0)
            if not q:
                continue
            tk = ib.reqMktData(q[0], "", False, False)
            for _ in range(15):
                await asyncio.sleep(0.2)
                px = (tk.last if tk.last and tk.last > 0 else
                      tk.close if tk.close and tk.close > 0 else
                      tk.bid if tk.bid and tk.bid > 0 else
                      tk.ask if tk.ask and tk.ask > 0 else None)
                if px:
                    rec["ltp"] = float(px)
                    break
            try:
                ib.cancelMktData(q[0])
            except Exception:
                pass
        except Exception:
            pass


_CCY = {"EUR", "USD", "JPY", "GBP", "AUD", "CHF", "CAD", "NZD", "SEK", "NOK"}


# ── Reconciliation ─────────────────────────────────────────────────────────

def reconcile(engine: dict, bm: dict, check_ltp: bool,
              broker_ok: bool = True, seed: Optional[set] = None,
              ledger: Optional[dict] = None, orders_known: bool = True) -> dict:
    """Merge the three truths per symbol and assign a verdict.

    verdict ∈ {COHERENT, DRIFT, NO-BROKER}. When the broker query failed
    (broker_ok=False) every verdict is NO-BROKER and drift is forced to 0
    — a failed query must never be reported as a naked position.
    """
    if not broker_ok:
        rows = []
        for sym in sorted(seed if seed else engine):
            e = engine.get(sym, {})
            rows.append({
                "symbol": sym, "engine_qty": round(e.get("qty", 0.0), 6),
                "broker_qty": None, "live_sells": None, "live_orders": None,
                "ltp": None, "verdict": "NO-BROKER",
                "reason": "broker unreachable — verdict suppressed",
                "cids": e.get("cids", []),
            })
        return {"rows": rows, "total": len(rows), "coherent": 0,
                "drift": 0, "broker_ok": False}
    # When a fleet `seed` universe is given, show EXACTLY those symbols —
    # every fleet member appears every render (even flat with no resting
    # order), and stale state files from prior runs (outside the seed) are
    # excluded. Without a seed, fall back to "whatever has data right now".
    if seed:
        syms = sorted(seed)
    else:
        syms = sorted(set(engine) | set(bm))
    rows = []
    coherent = 0
    for sym in syms:
        e = engine.get(sym, {})
        b = bm.get(sym, {})
        eq = round(e.get("qty", 0.0), 6)
        bq = round(b.get("broker_qty", 0.0), 6)
        live_sells = b.get("live_sells", 0)
        live_orders = b.get("live_orders", 0)
        ltp = b.get("ltp")
        dup = e.get("dup", False)

        # FX symbols: ib.positions() is UNRELIABLE for forex (cash-ledger,
        # not ContractPosition — the A42/A43 saga). The engine uses
        # execution-filter truth; the monitor can only read positions().
        # So an FX engine≠broker mismatch is almost always the monitor's
        # blind spot, NOT real drift — we soften it to a WATCH note.
        is_fx = (len(sym) == 6 and sym.isalpha()
                 and sym[:3] in _CCY and sym[3:] in _CCY)

        # FL5 — THREE independent truths kept SIDE-BY-SIDE (we do NOT drop
        # the broker source; we add the ledger as the reliable arbiter):
        #   ENGINE  = eq  — what the engine believes (state file)
        #   LEDGER  = ln  — the receipt-integral of pair-tagged fills (the
        #                   reliable position, incl. FX where positions()
        #                   is blind to the currency cycle space)
        #   BROKER  = bq  — ib.positions() (authoritative for equity;
        #                   unreliable for FX — kept for cross-check)
        ln = (round(ledger.get(sym), 6)
              if (ledger is not None and ledger.get(sym) is not None) else None)

        reasons = []
        warns = []

        # (1) ENGINE vs LEDGER — both are fill-derived, so they MUST agree.
        #     A mismatch = the engine's in-memory book drifted from the
        #     actual executions (the HD/MSFT/JPM bug class). Hard DRIFT.
        if ln is not None and abs(eq - ln) > 1e-6:
            reasons.append(f"engine={eq:g} ≠ ledger={ln:g}")

        # True position for the downstream NAKED/price checks: the ledger
        # when we have it (reliable for FX + equity), else broker positions().
        tp = ln if ln is not None else bq

        # (2) LEDGER vs BROKER positions().
        if ln is not None and orders_known and abs(ln - bq) > 1e-6:
            if is_fx:
                # positions() is blind to the FX cycle space — expected. Soft.
                warns.append(f"ledger={ln:g} vs positions()={bq:g} "
                             f"(FX positions() unreliable — ledger authoritative)")
            else:
                # equity positions() IS reliable → a genuine divergence.
                reasons.append(f"ledger={ln:g} ≠ broker positions()={bq:g}")
        elif ln is None and orders_known and abs(eq - bq) > 1e-6:
            # No ledger file for this symbol → legacy engine-vs-positions().
            if is_fx and abs(bq) < 1e-6:
                warns.append(f"engine={eq:g}, positions()={bq:g} "
                             f"(FX positions() unreliable — confirm via ledger/executions)")
            else:
                reasons.append(f"engine={eq:g} ≠ broker={bq:g}")

        # (3) NAKED: a real position (ledger truth) with no live protective
        #     SELL — the one that actually loses money. Needs broker ORDER
        #     data; in ledger-only mode (no connection) orders are unknown.
        if orders_known and abs(tp) > 1e-6 and live_sells == 0:
            reasons.append("NAKED: position with no live SELL")
        # (4) price liveness (only when checked + order/price data known)
        if orders_known and abs(tp) > 1e-6 and check_ltp and not ltp:
            reasons.append("no live market price (blind)")
        # DUPLICATE engines on one symbol — always a hard fault (A79 class)
        if dup:
            reasons.append(f"DUPLICATE engines (cids={e.get('cids')})")

        # NOTE: a FLAT engine with resting working orders is NORMAL — it's a
        # MONITORING entry bracket (BUY parent + SELL child) armed and
        # waiting for breakout. We do NOT flag "orders while flat" here;
        # the teardown broker-truth check (which expects zero) handles the
        # quiescent case. This monitor runs during live churn.

        if reasons:
            verdict = "DRIFT"
        elif warns:
            verdict = "WATCH"
            coherent += 1   # WATCH is not a failure — counts as coherent
        else:
            verdict = "COHERENT"
            coherent += 1
        rows.append({
            "symbol": sym, "engine_qty": eq, "ledger_qty": ln,
            "broker_qty": (bq if orders_known else None),
            "live_sells": live_sells, "live_orders": live_orders,
            "ltp": ltp, "verdict": verdict,
            "reason": "; ".join(reasons + warns),
            "cids": e.get("cids", []),
        })
    return {"rows": rows, "total": len(rows), "coherent": coherent,
            "drift": len(rows) - coherent}


# ── Pretty print ─────────────────────────────────────────────────────────

def _print(report: dict, check_ltp: bool) -> None:
    rows = report["rows"]
    print()
    print("═══ THREE-TRUTHS RECONCILIATION  (engine · ledger · broker) ═══")
    if report.get("broker_ok") is False:
        print("  ⚠ BROKER UNREACHABLE — could not connect to TWS/Gateway.")
        print("    Verdicts SUPPRESSED (a failed query is NOT a naked position).")
        print("    Likely cause: TWS API connection cap is full (32 bots already")
        print("    hold all slots) — bump 'Max simultaneous API connections' to 64.")
        print("    Engine state shown below for reference only:")
        for r in rows:
            if abs(r["engine_qty"]) > 1e-6:
                print(f"      {r['symbol']:<8} engine={r['engine_qty']:g} "
                      f"(cids={r['cids']})")
        print()
        print("  ⚠ NO-BROKER — reconciliation unavailable this cycle")
        print()
        return
    if not rows:
        print("  no positions or state files found — nothing to reconcile")
    hdr = (f"  {'SYMBOL':<8} {'ENGINE':>8} {'LEDGER':>8} {'BROKER':>8} "
           f"{'SELLS':>6} {'ORDERS':>7}")
    if check_ltp:
        hdr += f" {'LTP':>10}"
    hdr += "  VERDICT"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for r in rows:
        _lq = r.get("ledger_qty")
        _lq_s = f"{_lq:>8g}" if _lq is not None else f"{'—':>8}"
        _bq = r.get("broker_qty")
        _bq_s = f"{_bq:>8g}" if _bq is not None else f"{'—':>8}"
        line = (f"  {r['symbol']:<8} {r['engine_qty']:>8g} {_lq_s} "
                f"{_bq_s} {r['live_sells']:>6} {r['live_orders']:>7}")
        if check_ltp:
            line += f" {('%.5f' % r['ltp']) if r['ltp'] else '—':>10}"
        mark = {"COHERENT": "✓", "WATCH": "≈"}.get(r["verdict"], "✗")
        line += f"  {mark} {r['verdict']}"
        print(line)
        if r["reason"]:
            print(f"           ↳ {r['reason']}")
    print()
    v = "✓ ALL THREE TRUTHS AGREE" if report["drift"] == 0 else \
        f"✗ {report['drift']} symbol(s) in DRIFT"
    print(f"  {v}   ({report['coherent']}/{report['total']} coherent)")
    print()

    # ── PER-PAIR CASH LEGS — make the pair→two-currency mapping explicit ──
    # E.g. long USD.JPY shows USD +25,000 and JPY −(25,000·price): buying a
    # pair lifts the BASE currency and spends the QUOTE currency. Only pairs
    # with an open base leg are shown (a fully-cycled pair nets its base to 0;
    # any quote-leg remainder there is realised P&L).
    legs = report.get("pair_legs") or {}
    if legs:
        open_legs = [(p, v) for p, v in sorted(legs.items()) if abs(v[1]) > 0.5]
        if open_legs:
            print("  ─── PER-PAIR CASH LEGS  (ledger; open FX positions) ───")
            for pair, (bc, ba, qc, qa) in open_legs:
                print(f"     {pair:8s}  {bc} {ba:>+14,.0f}      "
                      f"{qc} {qa:>+16,.0f}")
            print(f"     → {len(open_legs)} open FX position"
                  f"{'' if len(open_legs) == 1 else 's'} "
                  f"(base leg = units held; quote leg = cash spent/received)")
            print()

    # ── CURRENCY CASH — the honest FX truth (forex settles into cash) ──
    # When the account is FLAT, every non-base currency is 0. Any non-zero
    # Δ-from-baseline in a non-base currency = a REAL open FX exposure — the
    # bug, upfront, with zero reliance on positions(). The base currency
    # (USD) Δ is just financing / realised P&L.
    cash = report.get("cash") or {}
    base = report.get("cash_baseline") or {}
    exp = report.get("cash_expected") or {}
    if cash or exp:
        BASE_CCY = "USD"
        ccys = sorted(set(cash) | set(base) | set(exp))
        rows_c = []
        for c in ccys:
            now = float(cash.get(c, 0.0))
            b0 = float(base.get(c, 0.0))
            d_actual = now - b0                 # IBKR's real Δ (b)
            d_exp = float(exp.get(c, 0.0))      # what our fills say (A·x̂)
            resid = d_actual - d_exp            # disagreement (our books vs IBKR)
            if abs(d_actual) > 0.5 or abs(d_exp) > 0.5:
                rows_c.append((c, d_actual, d_exp, resid))
        print("  ─── CURRENCY CASH  ·  IBKR actual Δ  vs  ledger-expected (A·x̂)  →  residual ───")
        if not rows_c:
            print("     all currencies flat (Δ 0) ✓  — clean playing field")
        else:
            drift_ccys = 0
            for c, d_actual, d_exp, resid in rows_c:
                # Reconciled if residual is tiny vs the magnitude (financing /
                # avg-price rounding give small residuals; a big residual =
                # missed fill or external/cross-client activity).
                scale = max(abs(d_actual), abs(d_exp), 1.0)
                if abs(resid) <= max(2.0, 0.005 * scale):
                    mark = "✓ reconciled"
                elif c == BASE_CCY:
                    mark = "(base: financing/PnL)"
                else:
                    mark = "⚠ UNRECONCILED — missed fill / external activity"
                    drift_ccys += 1
                print(f"     {c:4s}  IBKRΔ {d_actual:>+15,.0f}   "
                      f"ledger {d_exp:>+15,.0f}   resid {resid:>+13,.0f}   {mark}")
            if drift_ccys:
                print(f"     → {drift_ccys} currenc"
                      f"{'y' if drift_ccys == 1 else 'ies'} where IBKR cash ≠ our "
                      f"ledger — investigate (our books missed something, or a "
                      f"different client moved it)")
            else:
                print("     → ledger fully reconciles to IBKR cash ✓")
        print()


# ── Reusable entry point (importable by chaos tests) ───────────────────────

def _load_universe(name: str) -> Optional[set]:
    """Load the fleet symbol roster from a stress driver, so the monitor
    shows EXACTLY those symbols every render (and excludes stale files)."""
    if not name:
        return None
    mod = {
        "mixed": "tests.paper.stress_churn_mixed",
        "fx": "tests.paper.stress_churn",
        "equity": "tests.paper.stress_churn_equity",
    }.get(name)
    if not mod:
        return None
    try:
        proj = Path(__file__).resolve().parents[1]
        if str(proj) not in sys.path:
            sys.path.insert(0, str(proj))
        import importlib
        PAIRS = importlib.import_module(mod).PAIRS
        return {p["symbol"] for p in PAIRS}
    except Exception as e:
        print(f"[!] could not load universe '{name}': {e}", file=sys.stderr)
        return None


async def run(port: int, symbols: Optional[set] = None,
              check_ltp: bool = True, seed: Optional[set] = None,
              ledger_only: bool = False, rebaseline: bool = False) -> dict:
    # If a seed universe is given, scope the broker/engine scan to it too
    # (so stale non-fleet state files don't leak in) unless an explicit
    # --symbols filter was already supplied.
    scan_filter = symbols if symbols else seed
    engine = _scan_engine_state(scan_filter)
    # FL5 — durable ledger truth (no connection required).
    ledger = _scan_fill_ledgers(scan_filter)
    if ledger_only:
        # Connection-free mode: compare engine-book vs ledger-net only.
        # Consumes NO client-id slot → all 32 free for bots. We don't know
        # live orders/LTP here, so NAKED/price checks are suppressed.
        rep = reconcile(engine, {}, check_ltp=False, broker_ok=True,
                        seed=seed, ledger=ledger, orders_known=False)
        rep["cash"] = {}
        rep["cash_baseline"] = _load_cash_baseline()
        rep["cash_expected"] = _ledger_expected_cash(scan_filter)
        rep["pair_legs"] = _ledger_pair_legs(scan_filter)
        return rep
    bm, broker_ok, _err, cash = await _broker_market_truth(port, scan_filter, check_ltp)
    # Cash baseline (the "playing field at 0"). --rebaseline snapshots NOW.
    if rebaseline and broker_ok and cash:
        _save_cash_baseline(cash)
    rep = reconcile(engine, bm, check_ltp, broker_ok=broker_ok, seed=seed,
                    ledger=ledger, orders_known=True)
    rep["cash"] = cash
    rep["cash_baseline"] = _load_cash_baseline()
    # A·x̂ — the cash our fills SAY we should have, for the reconciliation line.
    rep["cash_expected"] = _ledger_expected_cash(scan_filter)
    return rep


async def _watch_loop(port: int, symbols, check_ltp: bool, interval: float,
                      seed: Optional[set] = None, ledger_only: bool = False,
                      rebaseline: bool = False) -> None:
    """Live monitor: clear screen + re-render the three-truths table every
    `interval` seconds until Ctrl+C. Shows nothing but the reconciliation."""
    import time as _time
    _first = True
    while True:
        try:
            report = await run(port, symbols, check_ltp=check_ltp, seed=seed,
                               ledger_only=ledger_only,
                               rebaseline=(rebaseline and _first))
            _first = False
        except Exception as e:
            report = None
            err = f"{type(e).__name__}: {e}"
        # Clear screen + home cursor (ANSI), then render.
        sys.stdout.write("\033[2J\033[H")
        ts = _time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"THREE-TRUTHS LIVE MONITOR   ·   port {port}   ·   {ts}   ·   "
              f"refresh {interval:g}s   ·   Ctrl+C to stop")
        if report is None:
            print(f"\n  [!] query error: {err}\n")
        else:
            _print(report, check_ltp=check_ltp)
        sys.stdout.flush()
        await asyncio.sleep(interval)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=int(os.environ.get("GT_IBKR_PORT", "7497")))
    p.add_argument("--symbols", default="", help="comma-separated filter (default: all)")
    p.add_argument("--no-ltp", action="store_true", help="skip market-price liveness check (faster)")
    p.add_argument("--watch", action="store_true", help="live monitor: refresh continuously")
    p.add_argument("--interval", type=float, default=10.0, help="watch refresh seconds (default 10)")
    p.add_argument("--universe", default="", choices=["", "mixed", "fx", "equity"],
                   help="show EXACTLY this fleet's roster every render "
                        "(all members appear even when flat; stale non-fleet "
                        "state files excluded)")
    p.add_argument("--ledger-only", action="store_true",
                   help="FL5: compare engine-book vs durable fill-ledger ONLY "
                        "(no broker connection → consumes ZERO client-id slots, "
                        "freeing all 32 for bots). Reliable FX truth; omits "
                        "live order/price columns.")
    p.add_argument("--rebaseline", action="store_true",
                   help="snapshot the CURRENT per-currency cash as the baseline "
                        "(the 'playing field at 0'). Run this when the account "
                        "is genuinely FLAT so later renders show Δ from zero.")
    args = p.parse_args()
    syms = {s.strip().upper() for s in args.symbols.split(",") if s.strip()} or None
    seed = _load_universe(args.universe)
    check_ltp = not args.no_ltp
    if args.watch:
        try:
            asyncio.run(_watch_loop(args.port, syms, check_ltp, args.interval,
                                    seed=seed, ledger_only=args.ledger_only,
                                    rebaseline=args.rebaseline))
        except KeyboardInterrupt:
            print("\nwatch stopped.")
        return 0
    report = asyncio.run(run(args.port, syms, check_ltp=check_ltp, seed=seed,
                             ledger_only=args.ledger_only, rebaseline=args.rebaseline))
    _print(report, check_ltp=check_ltp)
    return 0 if report["drift"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
