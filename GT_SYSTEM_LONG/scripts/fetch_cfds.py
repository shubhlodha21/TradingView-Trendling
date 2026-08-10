#!/usr/bin/env python3
"""Fetch real CFD contract details from IBKR and emit them in the exact
`INDEX_CFD_METADATA` format used by src/assets/cfds.py.

This is an OFFLINE GENERATOR. It does NOT touch the live trading runtime:
nothing imports it, and the live boot path never calls it. You run it by
hand, review the output, and paste the dict entries into
src/assets/cfds.py yourself. That keeps the metadata static/reviewable and
means a flaky IBKR connection can never break the bots at startup.

What it does
------------
For a curated master list of CFD symbols (index, metals, FX), it asks IBKR
`reqContractDetailsAsync` with secType='CFD' and reads back the REAL:
  * currency   (contract.currency)
  * tick       (details.minTick)
  * venue      (contract.exchange, normalised to "SMART")
  * underlying (details.longName, for the human-readable comment)

So tick sizes are never guessed — they come straight from the broker, the
same source SpecRegistry.cross_validate adopts at qualify time.

Output
------
  1. Paste-ready Python dict entries printed to stdout, grouped by family.
     Index + metals go into INDEX_CFD_METADATA. FX CFDs are emitted in a
     SEPARATE block because in cfds.py they resolve via the FX_CFD path
     (make_fx_cfd_spec / _fx_cfd_resolver), not the index metadata dict.
  2. data/cfds/cfd_specs_<YYYYMMDD>.json — the raw fetched details, for
     reference / diffing against a later run.

Usage
-----
  python scripts/fetch_cfds.py
  python scripts/fetch_cfds.py --port 4002 --client-id 78
  python scripts/fetch_cfds.py --only index,metals
  python scripts/fetch_cfds.py --symbols IBUS500,XAUUSD,EURUSD

Caveats
-------
  * "Fetch ALL CFDs" is not a real IBKR endpoint — there is no call that
    enumerates every CFD. This script fetches full details for the symbols
    in the curated MASTER list below. To add more, extend that list.
  * Some CFDs need region-specific entitlements; symbols your account
    can't see come back with zero matches and are reported as SKIPPED.
  * Currencies IBKR returns that are NOT in src/assets/types.Currency
    (e.g. SEK) are flagged — you'd need to add the enum member first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUT_DIR = PROJECT_ROOT / "data" / "cfds"

# Currencies that exist in src/assets/types.Currency. Kept here as a plain
# set so this script stays standalone (no runtime imports). Keep in sync if
# the enum grows.
KNOWN_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "HKD", "SGD", "INR", "CNH",
}

# ────────────────────────────────────────────────────────────────────
# Master symbol list. Logical name -> how to ask IBKR for it.
#
# Each entry yields one or more candidate contract specs; the first that
# returns matches wins (lets the script self-discover IBKR's symbology
# without us hard-coding currency/exchange up front).
# ────────────────────────────────────────────────────────────────────

# Index CFDs (IBKR "IB"-prefixed synthetic index CFDs). Currency is left
# for IBKR to fill in.
INDEX_SYMBOLS = [
    "IBUS500",    # S&P 500
    "IBUS30",     # Dow Jones
    "IBUST100",   # Nasdaq 100
    "IBUSM2000",  # Russell 2000
    "IBDE40",     # DAX 40
    "IBGB100",    # FTSE 100
    "IBEU50",     # Euro Stoxx 50
    "IBFR40",     # CAC 40
    "IBES35",     # IBEX 35
    "IBNL25",     # AEX 25
    "IBCH20",     # SMI 20
    "IBJP225",    # Nikkei 225
    "IBAU200",    # ASX 200
    "IBHK50",     # Hang Seng
    "IBSE30",     # OMX Stockholm 30 (likely SEK -> flagged)
    "IBUSOIL",    # WTI Crude Oil (energy CFD; IB-prefixed, resolves via index path)
]

# Spot-metal CFDs ("Loco London-OTC"). Symbol carries the pair directly.
METAL_SYMBOLS = [
    "XAUUSD",  # Gold
    "XAGUSD",  # Silver
    "XPTUSD",  # Platinum
    "XPDUSD",  # Palladium
]

# FX CFD pairs. IBKR FX CFDs are quoted base/quote; the contract symbol is
# the BASE currency and `currency` is the QUOTE. We try that form first,
# then fall back to the joined 6-letter symbol.
FX_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "AUDJPY", "EURAUD", "USDHKD",
    "USDSGD",
]


def _index_candidates(sym: str):
    from ib_async import Contract
    # secType='CFD', let IBKR resolve currency + exchange.
    yield Contract(secType="CFD", symbol=sym, exchange="SMART")
    yield Contract(secType="CFD", symbol=sym)


def _metal_candidates(sym: str):
    from ib_async import Contract
    yield Contract(secType="CFD", symbol=sym, exchange="SMART", currency="USD")
    yield Contract(secType="CFD", symbol=sym, exchange="SMART")
    yield Contract(secType="CFD", symbol=sym)


def _fx_candidates(pair: str):
    from ib_async import Contract
    base, quote = pair[:3], pair[3:]
    # IBKR FX CFD: symbol=base, currency=quote.
    yield Contract(secType="CFD", symbol=base, currency=quote, exchange="SMART")
    yield Contract(secType="CFD", symbol=pair, exchange="SMART")
    yield Contract(secType="CFD", symbol=pair)


FAMILIES = {
    "index": (INDEX_SYMBOLS, _index_candidates),
    "metals": (METAL_SYMBOLS, _metal_candidates),
    "fx": (FX_PAIRS, _fx_candidates),
}


def _fmt_tick(min_tick: float) -> str:
    """Render a broker minTick as a Decimal literal, trimming float noise."""
    from decimal import Decimal
    # Round-trip through repr to kill 0.0010000001-style float artifacts.
    d = Decimal(str(min_tick)).normalize()
    s = format(d, "f")
    return s


async def fetch_one(ib, logical: str, candidates) -> dict | None:
    """Try each candidate spec until one qualifies. Returns a record dict
    or None if IBKR has no match for any form."""
    for cand in candidates(logical):
        try:
            details = await ib.reqContractDetailsAsync(cand)
        except Exception as e:  # noqa: BLE001 - report and keep going
            print(f"  [warn] {logical}: reqContractDetails error: {e}",
                  file=sys.stderr)
            details = None
        if details:
            d = details[0]
            c = d.contract
            return {
                "logical": logical,
                "symbol": (c.symbol or "").upper(),
                "currency": (c.currency or "").upper(),
                "exchange": c.exchange or "SMART",
                "min_tick": float(d.minTick) if d.minTick else None,
                "long_name": getattr(d, "longName", "") or logical,
                "sec_type": c.secType,
                "con_id": c.conId,
            }
    return None


def emit_metadata_block(records: list[dict], *, title: str) -> str:
    """Render records as INDEX_CFD_METADATA-style dict entries."""
    lines = [f"    # -- {title} " + "-" * max(0, 50 - len(title))]
    for r in records:
        cur = r["currency"]
        known = cur in KNOWN_CURRENCIES
        tick = _fmt_tick(r["min_tick"]) if r["min_tick"] else "0.01"
        venue = "SMART"  # all CFDs route SMART; keep consistent with file
        key = r["symbol"]
        und = r["long_name"]
        entry = (
            f'    "{key}":'.ljust(14)
            + f' {{"currency": Currency.{cur}, "tick": Decimal("{tick}"),'
            + f' "venue": "{venue}", "underlying": "{und}"}},'
        )
        if known:
            lines.append(entry)
        else:
            # Currency.<cur> doesn't exist yet — comment the whole entry so
            # pasting can't break the dict. Add the enum member, then
            # uncomment.
            lines.append(f"    # !! {cur} not in Currency enum - add it, "
                         f"then uncomment:")
            lines.append("    # " + entry.lstrip())
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4001,
                    help="IBKR Gateway/TWS port (default 4001 = paper gateway)")
    ap.add_argument("--client-id", type=int, default=78,
                    help="Distinct clientId so it won't clash with a live bot")
    ap.add_argument("--only", default="index,metals,fx",
                    help="Comma list of families to fetch: index,metals,fx")
    ap.add_argument("--symbols", default="",
                    help="Override: comma list of specific symbols/pairs to "
                         "fetch (bypasses --only and the master list)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    # Build the work list.
    work: list[tuple[str, object]] = []  # (logical, candidate_fn)
    if args.symbols.strip():
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        for s in wanted:
            # Heuristic: 6-letter all-alpha non-IB symbol -> FX; X??USD -> metal.
            if s.startswith("IB"):
                work.append((s, _index_candidates))
            elif s in METAL_SYMBOLS or (len(s) == 6 and s[:1] == "X"):
                work.append((s, _metal_candidates))
            else:
                work.append((s, _fx_candidates))
    else:
        families = [f.strip() for f in args.only.split(",") if f.strip()]
        for fam in families:
            if fam not in FAMILIES:
                print(f"Unknown family {fam!r}; valid: {', '.join(FAMILIES)}",
                      file=sys.stderr)
                return 2
            syms, fn = FAMILIES[fam]
            for s in syms:
                work.append((s, fn))

    from ib_async import IB
    ib = IB()
    print(f"[cfds] Connecting to {args.host}:{args.port} "
          f"(clientId={args.client_id})...", file=sys.stderr)
    try:
        await ib.connectAsync(args.host, args.port, clientId=args.client_id)
    except Exception as e:  # noqa: BLE001
        print(f"[cfds] Connect failed: {e}", file=sys.stderr)
        return 1
    print("[cfds] Connected. Resolving CFD contracts...", file=sys.stderr)

    found: list[dict] = []
    skipped: list[str] = []
    try:
        for logical, cand in work:
            rec = await fetch_one(ib, logical, cand)
            if rec:
                found.append(rec)
                flag = "" if rec["currency"] in KNOWN_CURRENCIES else "  (!! ccy not in enum)"
                print(f"  ok   {logical:<10} -> {rec['symbol']:<8} "
                      f"{rec['currency']}  tick={rec['min_tick']}{flag}",
                      file=sys.stderr)
            else:
                skipped.append(logical)
                print(f"  SKIP {logical:<10} (no IBKR match / not entitled)",
                      file=sys.stderr)
    finally:
        ib.disconnect()

    # Persist raw JSON for reference.
    args.out_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = args.out_dir / f"cfd_specs_{day}.json"
    json_path.write_text(json.dumps({
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": args.host, "port": args.port,
        "found": found, "skipped": skipped,
    }, indent=2), encoding="utf-8")

    # Split for emission: IB-prefixed + metals go to the index dict; FX
    # pairs are emitted separately (different resolver path in cfds.py).
    index_recs = [r for r in found if r["logical"].startswith("IB")]
    metal_recs = [r for r in found
                  if r["logical"] in METAL_SYMBOLS or
                  (len(r["logical"]) == 6 and r["logical"][:1] == "X")]
    fx_recs = [r for r in found
               if r not in index_recs and r not in metal_recs]

    print("\n" + "=" * 70)
    print("PASTE-READY: INDEX_CFD_METADATA entries (index CFDs + metals)")
    print("=" * 70)
    if index_recs:
        print(emit_metadata_block(index_recs, title="Index CFDs"))
    if metal_recs:
        print(emit_metadata_block(metal_recs, title="Spot-metal CFDs"))

    if fx_recs:
        print("\n" + "=" * 70)
        print("FX CFDs — NOTE: these resolve via make_fx_cfd_spec / "
              "_fx_cfd_resolver")
        print("(hint=FX_CFD), NOT via INDEX_CFD_METADATA. Listed here for "
              "reference;")
        print("the FX_CFD path already derives currency/tick from the pair "
              "itself.")
        print("=" * 70)
        print(emit_metadata_block(fx_recs, title="FX CFDs (reference only)"))

    print(f"\n[cfds] {len(found)} resolved, {len(skipped)} skipped. "
          f"Raw JSON -> {json_path.relative_to(PROJECT_ROOT)}", file=sys.stderr)
    if skipped:
        print(f"[cfds] Skipped: {', '.join(skipped)}", file=sys.stderr)
    bad_ccy = sorted({r["currency"] for r in found
                      if r["currency"] not in KNOWN_CURRENCIES})
    if bad_ccy:
        print(f"[cfds] Currencies NOT in Currency enum (add before using): "
              f"{', '.join(bad_ccy)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))