#!/usr/bin/env python3
"""LV2 — real-time ledger-graph server (Server-Sent Events, 10 FPS).

Serves a live view of the durable fill-ledger as a currency graph. Reads the
`.gt_fills_*.jsonl` files only — NO broker connection, NO client-id slot, the
engine is never touched. Push cadence is 10 FPS (100ms).

    python3 scripts/ledger_server.py --port 8888 --universe fx

Then from your Mac:   ssh -L 8888:localhost:8888 ubuntu@<ec2>   and open
http://localhost:8888 in the browser. The page (LV3) renders the graph and
updates 10×/second via the /stream SSE endpoint.

Routes:
    GET /          → ledger_view.html (the LV3 front-end)
    GET /stream    → text/event-stream, one JSON ledger-graph frame / 100ms
    GET /once      → a single JSON snapshot (debugging / curl)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.ledger.graph import build_ledger_graph, reconcile_cash    # noqa: E402

_HTML_PATH = os.path.join(_HERE, "ledger_view.html")

# Set by main() before the server starts (read by the handler).
_DATA_DIR = "."
_UNIVERSE = None          # set[str] or None
_FPS = 10.0

# ── LV8: optional account-level cash reconciliation ──────────────────────
import threading as _threading
_WITH_CASH = False
_CASH_PORT = 7497
_CASH_CID = 178
_CASH: dict = {}          # {account: {ccy: live IBKR CashBalance}}  (per sub-account)
_CASH_OK = False          # poller has populated cash at least once
_CASH_LOCK = _threading.Lock()
_BASELINE: dict = {}      # {account: {ccy: baseline}} — the "playing field at 0"
# Optional {client_id(str): account} so FA sub-accounts reconcile per-client
# EXACTLY. Loaded from .gt_client_accounts.json if present; else single-account.
_CLIENT_ACCOUNT_MAP: dict = {}


def _baseline_path() -> str:
    return os.path.join(str(_DATA_DIR), ".gt_cash_baseline.json")


def _load_baseline():
    try:
        with open(_baseline_path()) as f:
            _BASELINE.update({k: float(v) for k, v in json.load(f).items()})
    except Exception:
        pass


def _save_baseline(d: dict):
    try:
        with open(_baseline_path(), "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def _cash_poller():
    """Background thread: own asyncio loop, holds ONE broker connection,
    calls reqAccountUpdates (the fix for empty accountValues), and refreshes
    the per-currency CashBalance every ~2s. Robust: any failure just leaves
    the last good cash; the viz works fine without it."""
    import asyncio
    from ib_async import IB
    global _CASH_OK
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ib = IB()

    async def run():
        global _CASH_OK
        while True:
            try:
                if not ib.isConnected():
                    await ib.connectAsync("127.0.0.1", _CASH_PORT,
                                          clientId=_CASH_CID, timeout=8)
                    acct = (ib.managedAccounts() or [""])[0]
                    # MUST use the ASYNC variant — the sync reqAccountUpdates
                    # wraps util.run() and nests inside our already-running
                    # loop → "event loop is already running". reqAccountUpdates
                    # only subscribes; values arrive via the event stream.
                    if hasattr(ib, "reqAccountUpdatesAsync"):
                        await ib.reqAccountUpdatesAsync(acct)
                    else:
                        # fallback: subscribe via the request directly
                        ib.client.reqAccountUpdates(True, acct)
                    await asyncio.sleep(2)        # let account updates arrive
                cb = {}      # {account: {ccy: balance}}
                for v in ib.accountValues():
                    if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
                        try:
                            cb.setdefault(v.account or "ALL", {})[v.currency] = float(v.value)
                        except (TypeError, ValueError):
                            pass
                if cb:
                    with _CASH_LOCK:
                        _CASH.clear()
                        _CASH.update(cb)
                    _CASH_OK = True
            except Exception as e:
                print(f"[ledger_server] cash poll error: {e}", file=sys.stderr)
                try:
                    ib.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(2)

    try:
        loop.run_until_complete(run())
    except Exception:
        pass


def _attach_cash(g: dict):
    """LV9 — client-id-level reconciliation with external-activity detection.

    Uses reconcile_cash(): per currency it sums OUR clients' footprints
    (expected), compares to IBKR's actual cash Δ, and the residual is the
    part NO client of ours explains → a DIFFERENT client / manual / missed
    fill. `by_client` attributes each currency move to its client_id. With FA
    sub-accounts + a client→account map it reconciles per-account EXACTLY.
    Auto-captures the baseline (per account) when the book is flat."""
    if not (_WITH_CASH and _CASH_OK):
        return
    with _CASH_LOCK:
        cash = {a: dict(c) for a, c in _CASH.items()}
    if not cash:
        return
    # auto-capture the zero-point (per account) exactly when we're flat
    if not _BASELINE and g["totals"]["open_pairs"] == 0:
        _BASELINE.update({a: dict(c) for a, c in cash.items()})
        _save_baseline(_BASELINE)
    rc = reconcile_cash(g, cash, baseline_by_account=_BASELINE,
                        client_account_map=(_CLIENT_ACCOUNT_MAP or None))
    # Front-end node overlay shape (per currency) + meta.
    g["cash"] = {
        c: {"ibkr_delta": v["ibkr_delta"], "ledger": v["our_expected"],
            "residual": v["external_residual"], "reconciled": v["reconciled"],
            "by_client": v["by_client"]}
        for c, v in rc["by_currency"].items()
    }
    g["cash_meta"] = {
        "external_detected": rc["external_detected"],
        "multi_account": rc["multi_account"],
        "by_account": rc["by_account"],
    }


def _resolve_universe(name: str):
    """Resolve --universe {fx,mixed,equity} → set of fleet symbols (so the
    graph shows exactly that roster). Empty/unknown → None (show everything)."""
    if not name:
        return None
    mod = {"mixed": "tests.paper.stress_churn_mixed",
           "fx": "tests.paper.stress_churn",
           "equity": "tests.paper.stress_churn_equity"}.get(name)
    if not mod:
        return None
    try:
        import importlib
        PAIRS = importlib.import_module(mod).PAIRS
        return {p["symbol"] for p in PAIRS}
    except Exception as e:
        print(f"[ledger_server] could not load universe '{name}': {e}",
              file=sys.stderr)
        return None


class _Handler(BaseHTTPRequestHandler):
    # Quieter logs (one line per connection is enough; SSE is long-lived).
    def log_message(self, fmt, *args):
        pass

    def _frame(self) -> bytes:
        g = build_ledger_graph(_DATA_DIR, universe=_UNIVERSE, ts=time.time())
        _attach_cash(g)
        return json.dumps(g, separators=(",", ":")).encode("utf-8")

    def do_GET(self):
        if self.path.startswith("/stream"):
            return self._serve_stream()
        if self.path.startswith("/once"):
            body = self._frame()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # default: serve the HTML page
        try:
            with open(_HTML_PATH, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            body = (b"<h1>ledger_view.html not found</h1>"
                    b"<p>Deploy scripts/ledger_view.html next to ledger_server.py.</p>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        interval = 1.0 / _FPS
        try:
            while True:
                payload = self._frame()
                # SSE frame: "data: <json>\n\n"
                self.wfile.write(b"data: ")
                self.wfile.write(payload)
                self.wfile.write(b"\n\n")
                self.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            return            # browser closed the tab — normal
        except Exception as e:
            print(f"[ledger_server] stream ended: {e}", file=sys.stderr)
            return


def main() -> int:
    global _DATA_DIR, _UNIVERSE, _FPS
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--data-dir", default=".",
                   help="dir holding .gt_fills_*.jsonl (default: cwd)")
    p.add_argument("--universe", default="", choices=["", "fx", "mixed", "equity"],
                   help="restrict graph to a fleet roster")
    p.add_argument("--fps", type=float, default=10.0, help="push rate (default 10)")
    p.add_argument("--with-cash", action="store_true",
                   help="LV8: open ONE broker connection (reqAccountUpdates) to "
                        "overlay account-level cash reconciliation (IBKR Δ vs "
                        "ledger A·x̂ residual) on each currency node")
    p.add_argument("--cash-port", type=int, default=7497)
    p.add_argument("--cash-client-id", type=int, default=178)
    args = p.parse_args()
    global _WITH_CASH, _CASH_PORT, _CASH_CID
    _DATA_DIR = args.data_dir
    _UNIVERSE = _resolve_universe(args.universe)
    _FPS = max(1.0, args.fps)
    _WITH_CASH = args.with_cash
    _CASH_PORT = args.cash_port
    _CASH_CID = args.cash_client_id
    if _WITH_CASH:
        _load_baseline()
        # optional FA sub-account map {client_id: account} for exact per-client recon
        try:
            with open(os.path.join(str(_DATA_DIR), ".gt_client_accounts.json")) as f:
                _CLIENT_ACCOUNT_MAP.update(json.load(f))
        except Exception:
            pass
        _threading.Thread(target=_cash_poller, daemon=True).start()
        print(f"[ledger_server] cash reconciliation ON "
              f"(broker cid={_CASH_CID}, baseline={_baseline_path()}, "
              f"sub-accounts={'yes' if _CLIENT_ACCOUNT_MAP else 'no (single account)'})")

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), _Handler)
    print(f"[ledger_server] live ledger graph on http://0.0.0.0:{args.port} "
          f"(data_dir={_DATA_DIR!r}, universe={args.universe or 'ALL'}, "
          f"{_FPS:g} FPS)")
    print(f"[ledger_server] from your Mac:  ssh -L {args.port}:localhost:{args.port} "
          f"<ec2>  then open http://localhost:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[ledger_server] stopped.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
