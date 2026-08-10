"""Nested ledger-graph aggregator — a COMPUTED view over the flat, durable
per-bot fill ledgers (`.gt_fills_<SYM>_<PORT>_<CID>.jsonl`).

We deliberately do NOT complicate the source of truth: storage stays the
append-only, exactly-once JSONL the engine writes (FL1–FL8). This module
reads those files and projects them into the structure the trader actually
reasons about — the **currency graph**:

    currencies  → NODES   (net cash per currency = the A·x̂ projection)
    pairs       → EDGES   (each pair connects its base↔quote currency)
    clients     → owners  (which client_id/strategy holds which pairs)

That graph is the natural object to visualize: a fill on AUD.JPY lights up
the AUD↔JPY edge and ripples into the AUD and JPY nodes. Buying a pair lifts
the BASE currency and spends the QUOTE currency (sign by side).

Pure + file-based: no broker connection, no client-id slot, engine untouched.
"""

from __future__ import annotations

import glob
import json
import os
import re

_FILE_RE = re.compile(
    r"\.gt_fills_(?P<sym>[A-Z0-9]+)_(?P<port>\d+)_(?P<cid>\d+)\.jsonl$")

# Currencies we recognise as FX legs (mirror of the monitor's set).
_CCY = {"EUR", "USD", "JPY", "GBP", "AUD", "CHF", "CAD", "NZD", "SEK", "NOK"}


def _is_fx(sym: str) -> bool:
    return (len(sym) == 6 and sym.isalpha() and sym.isupper()
            and sym[:3] in _CCY and sym[3:] in _CCY)


def _sign(side) -> int:
    s = str(side or "").strip().upper()
    if s in ("BOT", "BUY", "B"):
        return +1
    if s in ("SLD", "SELL", "S"):
        return -1
    return 0


def _cycle_basis(open_edges: list) -> list:
    """Fundamental cycle basis of the OPEN-position FX subgraph.

    `open_edges` = list of (base, quote, pair). Currencies are vertices, open
    pairs are edges. A spanning forest (union-find) classifies each edge as
    tree or non-tree; every non-tree edge closes exactly one fundamental
    cycle (its endpoints' path through the forest + itself). The number of
    such cycles == E − V + C — the dimensions of the position space that
    IBKR's per-currency cash (and positions()) is structurally BLIND to.

    Returns a list of cycles, each a list of pair symbols.
    """
    parent = {}
    adj = {}        # vertex -> list of (neighbor, pair)
    tree_edges = []
    nontree = []

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for b, q, pair in open_edges:
        find(b); find(q)
        ra, rb = find(b), find(q)
        if ra != rb:
            parent[ra] = rb
            tree_edges.append((b, q, pair))
            adj.setdefault(b, []).append((q, pair))
            adj.setdefault(q, []).append((b, pair))
        else:
            nontree.append((b, q, pair))

    def tree_path_pairs(s, t):
        # BFS in the spanning forest; return the pair symbols on s→t path.
        import collections
        prev = {s: (None, None)}
        dq = collections.deque([s])
        while dq:
            u = dq.popleft()
            if u == t:
                break
            for (w, pr) in adj.get(u, []):
                if w not in prev:
                    prev[w] = (u, pr)
                    dq.append(w)
        out = []
        u = t
        while u in prev and prev[u][0] is not None:
            pu, pr = prev[u]
            out.append(pr)
            u = pu
        return out

    cycles = []
    for b, q, pair in nontree:
        loop = [pair] + tree_path_pairs(b, q)
        cycles.append(loop)
    return cycles


def build_ledger_graph(data_dir: str = ".", universe=None,
                       ts: float = 0.0, recent_n: int = 50) -> dict:
    """Read every fill ledger under `data_dir` and build the nested graph.

    `universe` (optional set of symbols) restricts which pairs are included.
    `ts` is stamped into the result verbatim (callers pass a wall clock; we
    never read the clock here so the function stays pure/deterministic).

    Returns:
      {
        "ts": ts,
        "currencies": {CCY: {"net_cash": float, "degree": int, "pairs": [..]}},
        "pairs": {SYM: {base, quote, position, base_leg, quote_leg,
                        client_id, fills, asset}},
        "edges": [{pair, base, quote, position, magnitude, side, client_id}],
        "clients": {cid: {"pairs": [..], "fills": int, "open": int}},
        "totals": {fills, pairs, open_pairs, currencies, gross_base_notional},
      }
    """
    currencies: dict = {}
    pairs: dict = {}
    clients: dict = {}
    client_ccy: dict = {}    # cid -> {ccy: net cash footprint}  (per-client attribution)
    all_fills: list = []     # (time_str, pair, side_sign, shares, price, base, quote)
    total_fills = 0

    for fp in sorted(glob.glob(os.path.join(str(data_dir), ".gt_fills_*.jsonl"))):
        m = _FILE_RE.search(os.path.basename(fp))
        if not m:
            continue
        sym = m.group("sym")
        if universe and sym not in universe:
            continue
        cid = int(m.group("cid"))
        fx = _is_fx(sym)
        base = sym[:3] if fx else None
        quote = sym[3:] if fx else "USD"

        position = 0.0       # net base units (FX) / shares (equity)
        base_leg = 0.0       # signed base-currency cash effect
        quote_leg = 0.0      # signed quote-currency cash effect
        n_fills = 0
        last_fill_ts = ""    # most recent fill's timestamp string (for heatmap)
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
                    sg = _sign(r.get("side"))
                    if sg == 0:
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
                    n_fills += 1
                    position += sg * n
                    if fx:
                        base_leg += sg * n
                        if p is not None:
                            quote_leg += -sg * n * p
                    elif p is not None:
                        quote_leg += -sg * n * p   # equity spends USD
                    ft = str(r.get("time") or "")
                    if ft:
                        last_fill_ts = ft
                    all_fills.append((ft, sym, sg, n, p, base or sym, quote))
        except Exception:
            continue

        total_fills += n_fills
        rec = {
            "base": base or sym, "quote": quote,
            "position": round(position, 2),
            "base_leg": round(base_leg, 2),
            "quote_leg": round(quote_leg, 2),
            "client_id": cid, "fills": n_fills,
            "asset": "FX" if fx else "EQUITY",
            "last_fill_ts": last_fill_ts,
        }
        # If two client_ids ever write the same symbol, keep them distinct by
        # suffixing the cid (single-writer lock A79 should prevent this, but
        # the graph must never silently merge two owners).
        key = sym if sym not in pairs else f"{sym}#{cid}"
        pairs[key] = rec

        # ── currency nodes (net cash per currency = A·x̂) ──
        if fx:
            cb = currencies.setdefault(base, {"net_cash": 0.0, "degree": 0, "pairs": []})
            cb["net_cash"] += base_leg
            cb["pairs"].append(key)
            cq = currencies.setdefault(quote, {"net_cash": 0.0, "degree": 0, "pairs": []})
            cq["net_cash"] += quote_leg
            cq["pairs"].append(key)
        else:
            cq = currencies.setdefault("USD", {"net_cash": 0.0, "degree": 0, "pairs": []})
            cq["net_cash"] += quote_leg
            cq["pairs"].append(key)

        # ── client grouping ──
        cl = clients.setdefault(cid, {"pairs": [], "fills": 0, "open": 0})
        cl["pairs"].append(key)
        cl["fills"] += n_fills
        if abs(position) > 1e-9:
            cl["open"] += 1

        # ── per-client currency footprint (attribution: which client moved
        #    which currency, and by how much) ──
        cc = client_ccy.setdefault(cid, {})
        if fx:
            cc[base] = cc.get(base, 0.0) + base_leg
            cc[quote] = cc.get(quote, 0.0) + quote_leg
        else:
            cc["USD"] = cc.get("USD", 0.0) + quote_leg

    # finalize currency degree + rounding
    for c, v in currencies.items():
        v["degree"] = len(v["pairs"])
        v["net_cash"] = round(v["net_cash"], 2)

    # ── edges for rendering (only FX pairs connect two currency nodes) ──
    edges = []
    open_pairs = 0
    gross = 0.0
    for key, r in pairs.items():
        if abs(r["position"]) > 1e-9:
            open_pairs += 1
            gross += abs(r["position"])
        if r["asset"] == "FX":
            edges.append({
                "pair": key, "base": r["base"], "quote": r["quote"],
                "position": r["position"],
                "magnitude": abs(r["position"]),
                "side": "long" if r["position"] > 0 else
                        ("short" if r["position"] < 0 else "flat"),
                "client_id": r["client_id"],
            })

    # ── LV5: triangular cycles of the OPEN-position subgraph (the blind
    # dimensions positions()/per-currency cash can never see) ──
    open_fx = [(e["base"], e["quote"], e["pair"]) for e in edges
               if abs(e["position"]) > 1e-9]
    cycles = _cycle_basis(open_fx)

    # ── LV7: most-recent fills across all ledgers (newest first) ──
    # Sort by timestamp string desc (ISO sorts correctly); blanks sink.
    all_fills.sort(key=lambda x: x[0] or "", reverse=True)
    recent = []
    for ft, sym, sg, n, p, base, quote in all_fills[:max(0, recent_n)]:
        recent.append({
            "time": ft, "pair": sym,
            "side": "BUY" if sg > 0 else "SELL",
            "shares": round(n, 2), "price": p,
            "base": base, "quote": quote,
        })

    return {
        "ts": ts,
        "currencies": currencies,
        "pairs": pairs,
        "edges": edges,
        "clients": {str(k): v for k, v in clients.items()},
        "client_ccy": {str(k): {c: round(v, 2) for c, v in d.items()}
                       for k, d in client_ccy.items()},
        "cycles": cycles,
        "blind_dim": len(cycles),
        "recent_fills": recent,
        "totals": {
            "fills": total_fills,
            "pairs": len(pairs),
            "open_pairs": open_pairs,
            "currencies": len(currencies),
            "gross_base_notional": round(gross, 2),
            "blind_dim": len(cycles),
        },
    }


def reconcile_cash(graph: dict, cash_by_account: dict,
                   baseline_by_account=None, client_account_map=None,
                   tol_abs: float = 2.0, tol_rel: float = 0.01) -> dict:
    """Client-id-level cash reconciliation with EXTERNAL-activity detection.

    The shared-pocket problem: IBKR holds ONE cash balance per currency, moved
    by every client (and manual trades). Cash alone can't be attributed to a
    client — but each FILL is clientId-tagged, so each client's *currency
    footprint* (A·x̂ per client) is exact. We therefore:

      expected[ccy]  = Σ over OUR clients of their footprint in ccy
      ibkr_delta[ccy]= actual IBKR cash − baseline           (account-global)
      external[ccy]  = ibkr_delta − expected
                       → the part NO client of ours explains = a DIFFERENT
                         client / manual TWS / missed fill.  *This* is the
                         "USD moved somewhere else" awareness you asked for.

    Per-currency we also return `by_client` — exactly which client moved that
    currency and by how much (attribution).

    SUB-ACCOUNT READY: pass `cash_by_account = {account: {ccy: bal}}` and a
    `client_account_map = {cid: account}`. With FA sub-accounts (one strategy
    per sub-account) the reconciliation becomes EXACT per (account, ccy) —
    the true industry segregation form. With a single account it degrades
    gracefully to account-global + per-client attribution.

    Args:
      graph: output of build_ledger_graph (uses graph['client_ccy']).
      cash_by_account: {account: {ccy: balance}}. Use {'ALL': {...}} if single.
      baseline_by_account: same shape — the flat-start zero point.
      client_account_map: {cid(str): account}. None → all clients on the sole
        account.
    Returns:
      {by_currency:{ccy:{ibkr_delta,our_expected,external_residual,reconciled,
                         by_client:{cid:contrib}}},
       by_account:{acct:{ccy:{ibkr_delta,our_expected,residual,reconciled}}},
       multi_account: bool, external_detected: bool}
    """
    baseline_by_account = baseline_by_account or {}
    client_ccy = graph.get("client_ccy", {})
    accounts = set(cash_by_account.keys()) or {"ALL"}
    sole = next(iter(accounts)) if len(accounts) == 1 else None

    def acct_of(cid):
        if client_account_map and cid in client_account_map:
            return client_account_map[cid]
        return sole if sole is not None else "ALL"

    def is_reconciled(ccy, resid, exp, delta):
        if ccy == "USD":
            return True                       # base ccy: financing/PnL noise
        scale = max(abs(exp), abs(delta), 1.0)
        return abs(resid) <= max(tol_abs, tol_rel * scale)

    # expected per (account, ccy) + per-currency per-client attribution
    exp_acct: dict = {}
    by_client_ccy: dict = {}
    for cid, legs in client_ccy.items():
        a = acct_of(cid)
        ea = exp_acct.setdefault(a, {})
        for ccy, v in legs.items():
            ea[ccy] = ea.get(ccy, 0.0) + v
            d = by_client_ccy.setdefault(ccy, {})
            d[cid] = round(d.get(cid, 0.0) + v, 2)

    by_account: dict = {}
    agg_delta: dict = {}
    agg_exp: dict = {}
    for a in accounts:
        cash = cash_by_account.get(a, {})
        base = baseline_by_account.get(a, {})
        ea = exp_acct.get(a, {})
        block = {}
        for ccy in (set(cash) | set(base) | set(ea)):
            delta = float(cash.get(ccy, 0.0)) - float(base.get(ccy, 0.0))
            exp = float(ea.get(ccy, 0.0))
            resid = delta - exp
            block[ccy] = {"ibkr_delta": round(delta, 2), "our_expected": round(exp, 2),
                          "residual": round(resid, 2),
                          "reconciled": is_reconciled(ccy, resid, exp, delta)}
            agg_delta[ccy] = agg_delta.get(ccy, 0.0) + delta
            agg_exp[ccy] = agg_exp.get(ccy, 0.0) + exp
        by_account[a] = block

    by_currency: dict = {}
    external = False
    for ccy in (set(agg_delta) | set(agg_exp) | set(by_client_ccy)):
        delta = agg_delta.get(ccy, 0.0)
        exp = agg_exp.get(ccy, 0.0)
        resid = delta - exp
        rec = is_reconciled(ccy, resid, exp, delta)
        if not rec:
            external = True
        by_currency[ccy] = {
            "ibkr_delta": round(delta, 2), "our_expected": round(exp, 2),
            "external_residual": round(resid, 2), "reconciled": rec,
            "by_client": by_client_ccy.get(ccy, {}),
        }
    return {"by_currency": by_currency, "by_account": by_account,
            "multi_account": sole is None and len(accounts) > 1,
            "external_detected": external}
