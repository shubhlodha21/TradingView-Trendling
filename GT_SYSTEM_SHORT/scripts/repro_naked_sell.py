#!/usr/bin/env python3
"""Reproduce the PLTR naked-SELL scenario in your paper account, end-to-end.

This script is a stress test for the qty-mismatch guard:

  1. Connects to PAPER IBKR (port 7497).
  2. Verifies you are flat on the test ticker.
  3. Places a "stale orphan" SELL STP order for qty=30 (simulating leftover
     from a previous run with --quantity 30).
  4. Verifies the order is resting at IBKR.
  5. Tells YOU to launch the engine with --quantity 100 (mismatched).
  6. Polls IBKR open orders for up to 60 seconds.
  7. PASS if the orphan SELL is cancelled by reconcile (guard worked).
  8. FAIL if it's still resting after 60s (guard missing or broken).

After PASS, the order is gone — broker is clean, you can stop the engine.
After FAIL, the script will MANUALLY cancel the stale SELL so you don't
trip into the same bug in paper.

USAGE — TWO STEPS, TWO TERMINALS:

  Terminal 1:
    export GT_TEAMS_WEBHOOK_URL='...'
    python3 scripts/repro_naked_sell.py --ticker PLTR --stale-qty 30 \
        --setup-only

  → script places the orphan, prints a single line "ORPHAN_PLACED order_id=...",
    then exits. Verify in TWS that the SELL is showing.

  Terminal 2:
    python3 run_live.py --ticker PLTR --quantity 100 --paper
    # watch the reconcile log — you should see STALE_SELL_REJECTED

  Terminal 1, again:
    python3 scripts/repro_naked_sell.py --ticker PLTR --stale-qty 30 \
        --verify-only

  → polls broker, reports PASS/FAIL.

(Two-step design is intentional: a single script can't both place an order
AS one client AND run the engine AS another — the engine needs its own
client_id. Splitting it keeps both behaviors honest.)

EXIT CODES:
  0 = scenario verified (guard works as designed)
  1 = scenario failed (guard didn't fire — DO NOT GO LIVE)
  2 = setup error (couldn't connect, ticker symbol wrong, etc.)
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

# Match the engine's choice of IBKR library (see src/execution/broker.py).
# ib_async is the maintained fork of ib_insync and works on Python 3.12+.
try:
    from ib_async import IB, Stock, StopOrder, util
except ImportError:
    print("ERROR: ib_async not installed. pip install ib_async", file=sys.stderr)
    sys.exit(2)


# ─────────────────────────────────────────────────────────────────

PAPER_PORT = 7497
DEFAULT_TEST_CLIENT_ID = 11  # MUST match the --client-id you'll launch the
                              # engine with — otherwise the engine's
                              # startup-conflict guard refuses to start and
                              # the qty-mismatch guard never gets tested.


@dataclass
class TestConfig:
    ticker: str
    stale_qty: int
    stop_price: float | None = None  # auto-derive from current LTP if None
    host: str = "127.0.0.1"
    port: int = PAPER_PORT
    poll_timeout: int = 60
    client_id: int = DEFAULT_TEST_CLIENT_ID


def connect(cfg: TestConfig) -> IB:
    ib = IB()
    print(f"[setup] Connecting to PAPER IBKR at {cfg.host}:{cfg.port} "
          f"(clientId={cfg.client_id})…")
    try:
        ib.connect(cfg.host, cfg.port, clientId=cfg.client_id, timeout=10)
    except Exception as e:
        print(f"[setup] FAILED to connect: {e}", file=sys.stderr)
        sys.exit(2)
    if not ib.isConnected():
        print("[setup] Connected but isConnected() == False — abort.", file=sys.stderr)
        sys.exit(2)
    print(f"[setup] Connected. server_version={ib.client.serverVersion()}")
    return ib


def assert_flat(ib: IB, ticker: str) -> None:
    """Refuse to run if you already hold the ticker — too dangerous."""
    positions = [p for p in ib.positions() if p.contract.symbol == ticker]
    nonzero = [p for p in positions if p.position != 0]
    if nonzero:
        print(f"[setup] REFUSING TO RUN — you hold {ticker}: {nonzero}",
              file=sys.stderr)
        print(f"[setup] Flatten manually in TWS before running this test.",
              file=sys.stderr)
        sys.exit(2)


def get_current_ltp(ib: IB, ticker: str) -> float:
    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)
    [ticker_data] = ib.reqTickers(contract)
    ltp = ticker_data.marketPrice()
    if not ltp or ltp <= 0:
        # Fall back to last/close
        ltp = ticker_data.last or ticker_data.close
    if not ltp or ltp <= 0:
        print(f"[setup] Could not get LTP for {ticker}", file=sys.stderr)
        sys.exit(2)
    print(f"[setup] {ticker} LTP ≈ ${ltp:.2f}")
    return float(ltp)


def place_stale_orphan(ib: IB, cfg: TestConfig) -> int:
    """Place a SELL STP at a stop price comfortably below LTP so it WON'T
    trigger during the test (we want it to sit there)."""
    ltp = get_current_ltp(ib, cfg.ticker)
    # Stop 2% below LTP — far enough not to trigger naturally
    stop = cfg.stop_price or round(ltp * 0.98, 2)
    contract = Stock(cfg.ticker, "SMART", "USD")
    ib.qualifyContracts(contract)

    order = StopOrder("SELL", cfg.stale_qty, stopPrice=stop)
    order.tif = "GTC"
    order.outsideRth = False
    # Tag it so a human can recognize it in TWS
    order.orderRef = f"TEST_STALE_ORPHAN_{cfg.stale_qty}_{cfg.ticker}"

    trade = ib.placeOrder(contract, order)
    # Wait briefly for ack
    for _ in range(30):
        ib.sleep(0.1)
        if trade.orderStatus.status in ("Submitted", "PreSubmitted"):
            break
    if trade.orderStatus.status not in ("Submitted", "PreSubmitted"):
        print(f"[setup] Order not accepted: status={trade.orderStatus.status}",
              file=sys.stderr)
        sys.exit(2)

    print(f"[setup] ORPHAN_PLACED  order_id={trade.order.orderId}  "
          f"qty={cfg.stale_qty}  stop=${stop}  status={trade.orderStatus.status}")
    return trade.order.orderId


def find_orphan(ib: IB, ticker: str, qty: int) -> int | None:
    """Return orderId of the test orphan if it's still resting, else None."""
    ib.sleep(0.5)  # let openOrders refresh
    for trade in ib.openTrades():
        c = trade.contract
        o = trade.order
        if (c.symbol == ticker
            and o.action == "SELL"
            and o.orderType in ("STP", "STP LMT")
            and int(o.totalQuantity) == qty
            and (o.orderRef or "").startswith("TEST_STALE_ORPHAN_")):
            return o.orderId
    return None


def cancel_by_id(ib: IB, order_id: int) -> bool:
    for trade in ib.openTrades():
        if trade.order.orderId == order_id:
            ib.cancelOrder(trade.order)
            for _ in range(20):
                ib.sleep(0.25)
                if trade.orderStatus.status in ("Cancelled", "ApiCancelled"):
                    return True
            return False
    return True  # not in open orders → already gone


# ─────────────────────────────────────────────────────────────────

def cmd_setup(cfg: TestConfig) -> None:
    ib = connect(cfg)
    try:
        assert_flat(ib, cfg.ticker)
        # Refuse if a test orphan is already resting
        existing = find_orphan(ib, cfg.ticker, cfg.stale_qty)
        if existing is not None:
            print(f"[setup] A TEST_STALE_ORPHAN_* order is already resting "
                  f"(id={existing}). Cancelling first.")
            cancel_by_id(ib, existing)
        order_id = place_stale_orphan(ib, cfg)
        print(f"\n  ─── ORPHAN IS LIVE AT IBKR (placed as client_id={cfg.client_id}) ───")
        print(f"  Next step (in a different terminal):")
        mismatched_qty = cfg.stale_qty * 3 if cfg.stale_qty < 50 else 30
        print(f"    python3 run_live.py {cfg.ticker} \\")
        print(f"        --qty {mismatched_qty} --trigger <high_price> \\")
        print(f"        --port {cfg.port} --client-id {cfg.client_id}")
        print(f"  (The engine MUST use --client-id {cfg.client_id} so reconcile sees "
              f"this orphan as one of its own.)")
        print(f"  Then in this terminal:")
        print(f"    python3 scripts/repro_naked_sell.py --ticker {cfg.ticker} \\")
        print(f"        --stale-qty {cfg.stale_qty} --port {cfg.port} "
              f"--client-id {cfg.client_id} --verify-only\n")
    finally:
        ib.disconnect()


def cmd_verify(cfg: TestConfig) -> None:
    ib = connect(cfg)
    try:
        print(f"[verify] Polling IBKR open orders for up to {cfg.poll_timeout}s…")
        deadline = time.time() + cfg.poll_timeout
        last_state = "unknown"
        while time.time() < deadline:
            orphan_id = find_orphan(ib, cfg.ticker, cfg.stale_qty)
            if orphan_id is None:
                elapsed = cfg.poll_timeout - (deadline - time.time())
                print(f"\n  ✓ PASS — orphan SELL was cancelled by engine "
                      f"in {elapsed:.1f}s.")
                print(f"  The qty-mismatch guard fired correctly.")
                print(f"  Verify in audit/order.csv there's a STALE_SELL_REJECTED row.\n")
                sys.exit(0)
            if last_state != f"resting id={orphan_id}":
                print(f"  … orphan still resting (id={orphan_id})")
                last_state = f"resting id={orphan_id}"
            time.sleep(2)

        # Timed out — guard failed
        orphan_id = find_orphan(ib, cfg.ticker, cfg.stale_qty)
        print(f"\n  ✗ FAIL — orphan SELL (id={orphan_id}) is STILL resting "
              f"after {cfg.poll_timeout}s.")
        print(f"  The guard did NOT fire. Possible causes:")
        print(f"    • Engine isn't actually running (check ps -ef)")
        print(f"    • Engine is on old code (run scripts/preflight_check.py)")
        print(f"    • Engine is connected to a different account/clientId")
        print(f"    • Guard has a bug — check engine logs for reconcile output")
        print(f"\n  Cancelling the orphan now so you don't trip into the bug…")
        if cancel_by_id(ib, orphan_id):
            print(f"  ✓ Orphan cancelled.")
        else:
            print(f"  ✗ Could NOT cancel orphan — DO THIS MANUALLY IN TWS NOW.")
        sys.exit(1)
    finally:
        ib.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--stale-qty", type=int, default=30,
                    help="Qty of the orphan SELL to plant")
    ap.add_argument("--stop-price", type=float, default=None,
                    help="Stop price for orphan (default: 2%% below LTP)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PAPER_PORT,
                    help=f"Default {PAPER_PORT} (paper). NEVER use 7496 (live).")
    ap.add_argument("--poll-timeout", type=int, default=60)
    ap.add_argument("--client-id", type=int, default=DEFAULT_TEST_CLIENT_ID,
                    help=(f"client_id for placing/inspecting the orphan. MUST match "
                          f"the --client-id you'll pass to run_live.py, or the engine's "
                          f"startup-conflict guard will refuse to start "
                          f"(default {DEFAULT_TEST_CLIENT_ID})."))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--setup-only",  action="store_true",
                   help="Place the orphan and exit")
    g.add_argument("--verify-only", action="store_true",
                   help="Poll for orphan cancellation by engine")
    args = ap.parse_args()

    if args.port == 7496:
        print("REFUSING TO RUN AGAINST LIVE PORT (7496). This script places "
              "real orders.", file=sys.stderr)
        sys.exit(2)

    cfg = TestConfig(
        ticker=args.ticker.upper(),
        stale_qty=args.stale_qty,
        stop_price=args.stop_price,
        host=args.host,
        port=args.port,
        poll_timeout=args.poll_timeout,
        client_id=args.client_id,
    )
    util.logToConsole(level=30)  # WARNING

    if args.setup_only:
        cmd_setup(cfg)
    else:
        cmd_verify(cfg)


if __name__ == "__main__":
    main()
