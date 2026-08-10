#!/usr/bin/env python3
"""LV10 — VIRTUAL FX POSITIONS: the intuitive monitoring lens.

You think in PAIRS, not currency nodes. This shows each FX pair as ONE
virtual position (EUR.AUD = a single line), derived straight from the
durable fill ledger — so it's immune to the shared-currency cash mess and
proves, per pair, that the multi-change problem is handled.

    PAIR      VIRTUAL POS   SIDE   CLIENT   BASE LEG      QUOTE LEG
    EURGBP    +25,000       LONG   c88      EUR +25.0k    GBP -21.6k

A footer shows the NET per-currency exposure (the shared-currency
aggregation — watch USD = sum of every USD-pair leg resolve correctly) and
a one-line verdict (no shorts / N open).

File-based: no broker connection, no client-id slot, engine untouched.

    python3 scripts/fx_positions.py --universe fx            # one-shot
    python3 scripts/fx_positions.py --universe fx --watch    # live
    python3 scripts/fx_positions.py --universe fx --log data/fx_pos.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ledger.graph import build_ledger_graph    # noqa: E402

# ANSI
_G = "\033[32m"; _R = "\033[31m"; _DIM = "\033[90m"; _B = "\033[1m"; _0 = "\033[0m"
_Y = "\033[33m"


def _fmt(n) -> str:
    if n is None:
        return "–"
    a = abs(n)
    if a >= 1e6:
        return f"{n/1e6:+.2f}M"
    if a >= 1e3:
        return f"{n/1e3:+.1f}k"
    return f"{n:+.0f}"


def _resolve_universe(name: str):
    if not name:
        return None
    mod = {"fx": "tests.paper.stress_churn",
           "mixed": "tests.paper.stress_churn_mixed",
           "equity": "tests.paper.stress_churn_equity"}.get(name)
    if not mod:
        return None
    try:
        import importlib
        return {p["symbol"] for p in importlib.import_module(mod).PAIRS}
    except Exception:
        return None


def render(g: dict, universe_size: int = 0) -> str:
    pairs = g["pairs"]
    open_p = sorted(((k, v) for k, v in pairs.items() if abs(v["position"]) > 1e-9),
                    key=lambda kv: -abs(kv[1]["position"]))
    flat_n = len(pairs) - len(open_p)
    shorts = [k for k, v in open_p if v["position"] < 0]

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    out = []
    roster = universe_size or len(pairs)
    out.append(f"{_B}═══ VIRTUAL FX POSITIONS  ·  {ts}  ·  "
               f"{len(open_p)} OPEN / {roster} pairs ═══{_0}")
    out.append("  each pair = ONE virtual position (ledger truth, "
               "immune to shared-currency cash)")
    out.append("")
    hdr = (f"  {'PAIR':<8} {'VIRTUAL POS':>12}  {'SIDE':<5} {'CID':>4}   "
           f"{'BASE LEG':>14}   {'QUOTE LEG':>16}")
    out.append(f"{_DIM}{hdr}{_0}")
    out.append(f"  {_DIM}{'─'*72}{_0}")
    if not open_p:
        out.append(f"  {_DIM}— all {roster} pairs FLAT —{_0}")
    for k, v in open_p:
        pos = v["position"]
        side = "LONG" if pos > 0 else "SHORT"
        col = _G if pos > 0 else _R
        out.append(
            f"  {k:<8} {col}{_fmt(pos):>12}{_0}  {col}{side:<5}{_0} "
            f"c{v['client_id']:<3}  "
            f"{v['base']} {_fmt(v['base_leg']):>9}   "
            f"{v['quote']} {_fmt(v['quote_leg']):>11}")
    out.append(f"  {_DIM}{'─'*72}{_0}")
    if flat_n:
        out.append(f"  {_DIM}+ {flat_n} flat pair(s){_0}")

    # ── currency exposure footer: the shared-currency aggregation ──
    cur = g.get("currencies", {})
    nz = sorted(((c, v["net_cash"]) for c, v in cur.items() if abs(v["net_cash"]) > 0.5),
                key=lambda cv: -abs(cv[1]))
    if nz:
        out.append("")
        out.append(f"  {_DIM}currency exposure (Σ all pairs' legs — the shared "
                   f"pocket, resolved):{_0}")
        line = "   " + "  ".join(
            f"{c} {(_G if val>0 else _R)}{_fmt(val)}{_0}" for c, val in nz)
        out.append(line)
    bd = g.get("blind_dim", 0)
    if bd:
        out.append(f"  {_Y}↻ {bd} triangular cycle(s) open "
                   f"(positions net ~0 cash but are real — system is aware){_0}")

    out.append("")
    if shorts:
        out.append(f"  {_R}{_B}✗ {len(shorts)} SHORT: {', '.join(shorts)}{_0}")
    else:
        out.append(f"  {_G}{_B}✓ NO SHORTS — every open position is LONG{_0}")
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=".")
    p.add_argument("--universe", default="", choices=["", "fx", "mixed", "equity"])
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--log", default="", help="append a JSONL snapshot of open positions each render")
    args = p.parse_args()
    uni = _resolve_universe(args.universe)
    uni_n = len(uni) if uni else 0

    def snapshot():
        g = build_ledger_graph(args.data_dir, universe=uni, ts=time.time())
        if args.log:
            try:
                rec = {"ts": g["ts"],
                       "open": {k: v["position"] for k, v in g["pairs"].items()
                                if abs(v["position"]) > 1e-9}}
                with open(args.log, "a") as f:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            except Exception:
                pass
        return g

    if args.watch:
        try:
            while True:
                g = snapshot()
                sys.stdout.write("\033[2J\033[H")
                print(render(g, uni_n))
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
        return 0
    print(render(snapshot(), uni_n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
