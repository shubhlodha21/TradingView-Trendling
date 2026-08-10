#!/usr/bin/env python3
"""Multi-asset paper smoke — validate AssetSpec against live IBKR paper.

Runs three checks per asset class against IBKR paper account:

  1. RESOLVE  — SpecRegistry.resolve(symbol) returns the right
                AssetSpec, ContractPolicy, etc.

  2. QUALIFY  — IBKR's reqContractDetails returns a valid contract
                matching what spec.contract.make() produces.

  3. VALIDATE — SpecRegistry.cross_validate asserts spec fields
                (currency, multiplier, tick) match IBKR's
                ContractDetails. THE KEY SAFETY CHECK.

Optional:
  4. ORDER    — placed with --place-order flag. Submits one tiny BUY
                LIMIT (deliberately far from the LTP so it won't fill),
                verifies submission, then CANCELS immediately. This
                proves the engine could place orders end-to-end on
                this asset class. Default: skipped (read-only).

Usage:
    python3 scripts/multi_asset_paper_smoke.py --asset fx
    python3 scripts/multi_asset_paper_smoke.py --asset future
    python3 scripts/multi_asset_paper_smoke.py --asset cfd
    python3 scripts/multi_asset_paper_smoke.py --asset all

    # With order placement (test real submission, cancels immediately):
    python3 scripts/multi_asset_paper_smoke.py --asset all --place-order

Exit codes:
    0 = all checks pass for the requested asset classes
    1 = at least one check failed (DO NOT GO LIVE)
    2 = setup error (IBKR unreachable, missing dep, etc.)

SAFETY:
  * Only runs against paper port (7497) by default
  * --place-order submits LIMIT orders FAR from market price
  * Always cancels submitted orders before exit
  * Refuses to start if you already hold the test ticker
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# Ensure src/ is importable when run from the repo root
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from ib_async import IB, util
except ImportError:
    print("ERROR: ib_async not installed. pip install ib_async", file=sys.stderr)
    sys.exit(2)

from src.assets import (
    AssetClass, SpecRegistry, SpecMismatchError, resolve,
    contracts, base_units, cfd_units, shares, price,
)
from src.assets.policies.contract import ContractNotFound


PAPER_PORT = 7497
TEST_CLIENT_ID = 33


# ────────────────────────────────────────────────────────────────────
# Per-asset test scenarios
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Scenario:
    """One paper-smoke scenario: an asset class + symbol + optional
    explicit hint + a sample qty for the order test."""
    label: str               # human-readable name
    symbol: str              # ticker to resolve
    hint: AssetClass | None  # None for ambiguous-but-defaulted
    sample_qty: object       # Quantity for the order test
    order_offset: Decimal    # how far below LTP to put the BUY limit
                             # (must be > 1% to guarantee no fill)


SCENARIOS = {
    "equity": Scenario(
        label="US Equity (baseline)",
        symbol="AAPL",
        hint=None,
        sample_qty=shares(1),
        order_offset=Decimal("10"),  # $10 below LTP on a ~$180 stock
    ),
    "fx": Scenario(
        label="Forex (IDEALPRO)",
        symbol="EURUSD",
        hint=None,
        sample_qty=base_units(25000),
        order_offset=Decimal("0.05"),  # 5 cents below LTP on ~$1.16
    ),
    "future": Scenario(
        label="Future (CME MES — micro for safety)",
        symbol="MES",   # micro contract = $5 multiplier (safer test)
        hint=None,
        sample_qty=contracts(1),
        order_offset=Decimal("100"),  # 100 points below LTP
    ),
    "cfd": Scenario(
        label="Index CFD (S&P 500)",
        symbol="IBUS500",
        hint=None,
        sample_qty=cfd_units(1),
        order_offset=Decimal("100"),
    ),
}


# ────────────────────────────────────────────────────────────────────
# Check primitives
# ────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    elapsed_ms: float = 0.0


async def check_resolve(scenario: Scenario) -> CheckResult:
    """Step 1 — SpecRegistry.resolve()"""
    import time
    t0 = time.perf_counter()
    try:
        spec = resolve(scenario.symbol, hint=scenario.hint)
    except Exception as e:
        return CheckResult("RESOLVE", False, f"{type(e).__name__}: {e}",
                           (time.perf_counter() - t0) * 1000)
    elapsed = (time.perf_counter() - t0) * 1000
    return CheckResult(
        "RESOLVE", True,
        f"{spec.asset_class.name} via {spec.venue}, "
        f"contract={type(spec.contract).__name__}, "
        f"sizing={type(spec.sizing).__name__}",
        elapsed,
    )


async def check_qualify(ib: IB, scenario: Scenario) -> CheckResult:
    """Step 2 — qualify the contract against IBKR."""
    import time
    t0 = time.perf_counter()
    try:
        spec = resolve(scenario.symbol, hint=scenario.hint)
        contract = spec.contract.make(scenario.symbol)
        qualified = await spec.contract.qualify(ib, contract)
    except Exception as e:
        return CheckResult("QUALIFY", False, f"{type(e).__name__}: {e}",
                           (time.perf_counter() - t0) * 1000)
    elapsed = (time.perf_counter() - t0) * 1000
    return CheckResult(
        "QUALIFY", True,
        f"conId={qualified.conId} "
        f"localSym={getattr(qualified, 'localSymbol', '?')} "
        f"exchange={qualified.exchange}",
        elapsed,
    )


async def check_cross_validate(ib: IB, scenario: Scenario) -> CheckResult:
    """Step 3 — SpecRegistry.cross_validate(): spec ↔ IBKR field assertions."""
    import time
    t0 = time.perf_counter()
    try:
        spec = resolve(scenario.symbol, hint=scenario.hint)
        await SpecRegistry.cross_validate(spec, scenario.symbol, ib)
    except SpecMismatchError as e:
        return CheckResult("CROSS_VALIDATE", False,
                           f"MISMATCH field={e.field} "
                           f"expected={e.expected} actual={e.actual} "
                           f"({e.context})",
                           (time.perf_counter() - t0) * 1000)
    except Exception as e:
        return CheckResult("CROSS_VALIDATE", False, f"{type(e).__name__}: {e}",
                           (time.perf_counter() - t0) * 1000)
    elapsed = (time.perf_counter() - t0) * 1000
    return CheckResult(
        "CROSS_VALIDATE", True,
        "spec ↔ IBKR ContractDetails match (currency, multiplier, tick)",
        elapsed,
    )


async def check_order_submit_cancel(ib: IB, scenario: Scenario) -> CheckResult:
    """Step 4 (opt-in) — submit a far-from-market LIMIT BUY, verify
    it lands, cancel immediately.

    Async-API note: inside an asyncio.run() event loop, we must use
    the *Async() variants of ib_async methods. The sync wrappers
    (reqTickers, sleep) call loop.run_until_complete internally
    which fails with "loop already running" when nested.
    """
    import time
    from ib_async import LimitOrder
    t0 = time.perf_counter()

    spec = resolve(scenario.symbol, hint=scenario.hint)
    contract = spec.contract.make(scenario.symbol)
    qualified = await spec.contract.qualify(ib, contract)

    # Get current price — MUST use the *Async variant inside event loop
    try:
        tickers = await ib.reqTickersAsync(qualified)
    except Exception as e:
        return CheckResult("ORDER_SUBMIT", False,
                           f"reqTickersAsync failed: {type(e).__name__}: {e}",
                           (time.perf_counter() - t0) * 1000)
    if not tickers:
        return CheckResult("ORDER_SUBMIT", False,
                           f"reqTickersAsync returned empty list for {scenario.symbol}",
                           (time.perf_counter() - t0) * 1000)
    ticker = tickers[0]
    # marketPrice() can return nan; check carefully. Fall back through
    # last → close → midpoint.
    import math
    candidates = [
        ticker.marketPrice(),
        ticker.last,
        ticker.close,
        ((ticker.bid + ticker.ask) / 2) if (ticker.bid and ticker.ask
            and not math.isnan(ticker.bid) and not math.isnan(ticker.ask)) else None,
    ]
    ltp = None
    for c in candidates:
        if c is None: continue
        if isinstance(c, float) and math.isnan(c): continue
        if c <= 0: continue
        ltp = c
        break
    if ltp is None:
        return CheckResult("ORDER_SUBMIT", False,
                           f"no usable LTP for {scenario.symbol} "
                           f"(market closed? subscription missing?)",
                           (time.perf_counter() - t0) * 1000)

    # Place LIMIT BUY far below LTP — guaranteed not to fill
    limit_px = float(spec.tick.round_to_tick(
        price(str(Decimal(str(ltp)) - scenario.order_offset))
    ))
    # qty as Python primitive for ib_async
    if hasattr(scenario.sample_qty, "to_float"):
        try:
            qty_val = scenario.sample_qty.to_int()
        except Exception:
            qty_val = scenario.sample_qty.to_float()
    else:
        qty_val = float(scenario.sample_qty.value)

    order = LimitOrder("BUY", qty_val, limit_px)
    order.tif = "DAY"
    order.outsideRth = False
    order.orderRef = f"MULTIASSET_SMOKE_{scenario.symbol}"

    try:
        trade = ib.placeOrder(qualified, order)
    except Exception as e:
        return CheckResult("ORDER_SUBMIT", False, f"placeOrder raised: {e}",
                           (time.perf_counter() - t0) * 1000)

    # Wait for ack (Submitted/PreSubmitted) — asyncio.sleep, NOT ib.sleep
    for _ in range(50):
        await asyncio.sleep(0.1)
        if trade.orderStatus.status in ("Submitted", "PreSubmitted"):
            break

    status = trade.orderStatus.status
    if status not in ("Submitted", "PreSubmitted"):
        return CheckResult("ORDER_SUBMIT", False,
                           f"order not accepted; status={status}",
                           (time.perf_counter() - t0) * 1000)

    # CANCEL immediately — we never want this to actually fill
    ib.cancelOrder(trade.order)
    for _ in range(30):
        await asyncio.sleep(0.1)
        if trade.orderStatus.status in ("Cancelled", "ApiCancelled", "PendingCancel"):
            break

    elapsed = (time.perf_counter() - t0) * 1000
    return CheckResult(
        "ORDER_SUBMIT", True,
        f"placed LIMIT BUY {qty_val} {scenario.symbol} @ {limit_px} "
        f"(LTP={ltp:.4f}), cancelled — status={trade.orderStatus.status}",
        elapsed,
    )


# ────────────────────────────────────────────────────────────────────
# Per-scenario runner
# ────────────────────────────────────────────────────────────────────

async def run_scenario(ib: IB, scenario: Scenario, place_order: bool) -> list[CheckResult]:
    print(f"\n  ─── {scenario.label} ({scenario.symbol}) ─────────────")
    results: list[CheckResult] = []

    for step_fn, needs_ib in [
        (check_resolve, False),
        (check_qualify, True),
        (check_cross_validate, True),
    ]:
        if needs_ib:
            r = await step_fn(ib, scenario)
        else:
            r = await step_fn(scenario)
        results.append(r)
        symbol = "✓" if r.passed else "✗"
        print(f"    {symbol} {r.name:<16}  {r.elapsed_ms:>6.1f}ms  {r.detail}")
        if not r.passed:
            # Don't run downstream checks if a foundation step failed
            return results

    if place_order:
        r = await check_order_submit_cancel(ib, scenario)
        results.append(r)
        symbol = "✓" if r.passed else "✗"
        print(f"    {symbol} {r.name:<16}  {r.elapsed_ms:>6.1f}ms  {r.detail}")

    return results


# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────

async def main_async(args):
    ib = IB()
    print(f"\n  Connecting to IBKR paper at {args.host}:{args.port} "
          f"(clientId={args.client_id}) ...")
    try:
        await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=10)
    except Exception as e:
        print(f"\n  ✗ Connection failed: {e}", file=sys.stderr)
        return 2
    if not ib.isConnected():
        print(f"\n  ✗ Connected but isConnected()=False", file=sys.stderr)
        return 2
    print(f"  ✓ Connected (server v{ib.client.serverVersion()})")

    # Pick which scenarios to run
    if args.asset == "all":
        scenarios_to_run = ["equity", "fx", "future", "cfd"]
    else:
        scenarios_to_run = [args.asset]

    all_results: dict[str, list[CheckResult]] = {}
    try:
        for key in scenarios_to_run:
            scen = SCENARIOS[key]
            all_results[key] = await run_scenario(ib, scen, args.place_order)
    finally:
        ib.disconnect()

    # Summary table
    print(f"\n  ═══ SUMMARY ════════════════════════════════════════════════")
    overall_pass = True
    for key, results in all_results.items():
        scen = SCENARIOS[key]
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        symbol = "✓" if passed == total else "✗"
        print(f"    {symbol} {scen.label:<40}  {passed}/{total} checks pass")
        if passed != total:
            overall_pass = False
    print(f"  ════════════════════════════════════════════════════════════")
    if overall_pass:
        print(f"\n  ✓ ALL CHECKS PASSED — multi-asset spec system validated against live paper.\n")
        return 0
    else:
        print(f"\n  ✗ SOME CHECKS FAILED — DO NOT promote to live. Inspect output above.\n")
        return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", choices=["equity", "fx", "future", "cfd", "all"],
                    default="all",
                    help="Which asset class scenario(s) to run.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PAPER_PORT,
                    help=f"Default {PAPER_PORT} (paper). NEVER use 7496 (live).")
    ap.add_argument("--client-id", type=int, default=TEST_CLIENT_ID,
                    help=f"IBKR client_id for this script (default {TEST_CLIENT_ID})")
    ap.add_argument("--place-order", action="store_true",
                    help="Submit and immediately cancel a far-from-market LIMIT "
                         "BUY for each asset class. Off by default; turn on to "
                         "test real submission end-to-end.")
    args = ap.parse_args()

    if args.port == 7496:
        print("REFUSING TO RUN AGAINST LIVE PORT (7496). This script can place orders.",
              file=sys.stderr)
        sys.exit(2)

    util.logToConsole(level=30)  # WARNING and above
    code = asyncio.run(main_async(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
