# Live Ledger Graph + Client-Level Reconciliation (LV1–LV9)

A computed, real-time view over the durable per-bot fill ledgers. Storage is
unchanged (append-only `.gt_fills_<SYM>_<PORT>_<CID>.jsonl`); everything here
is read-only projection — no broker connection for the graph itself, engine
untouched.

## The structure (the "nested ledger")

We don't build a complex nested *storage*. We project the flat ledgers into
the **currency graph** the trader actually reasons about:

```
currencies → NODES   (net cash per currency = A·x̂, the projection)
pairs      → EDGES   (each pair connects base ↔ quote)
clients    → owners  (client_id holds pairs; per-client currency footprint)
```

`src/ledger/graph.py::build_ledger_graph()` returns: `currencies`, `pairs`,
`edges`, `clients`, **`client_ccy`** (per-client per-currency footprint),
`cycles` + `blind_dim` (LV5), `recent_fills` (LV7), `totals`.

## The reconciliation problem — and the answer

IBKR holds **one cash balance per currency**, moved by every client (and
manual trades). Cash alone can't be attributed to a client. But **every fill
is `clientId`-tagged**, so each client's currency footprint (A·x̂ per client)
is exact. Therefore:

```
expected[ccy]   = Σ over OUR clients of their footprint in ccy
ibkr_delta[ccy] = actual IBKR cash − baseline           (account-global)
external[ccy]   = ibkr_delta − expected
                  → the part NO client of ours explains
                    = a DIFFERENT client / manual TWS / a missed fill
```

`src/ledger/graph.py::reconcile_cash()` produces, per currency:
`our_expected`, `ibkr_delta`, **`external_residual`**, `reconciled`, and
**`by_client`** (which client moved that currency, and by how much). It also
detects external activity (`external_detected`) — the "USD moved somewhere
else, be aware" signal.

### Two regimes (same code)

1. **Single account (today).** `cash_by_account = {"ALL": {...}}`, all clients
   on the one account. Reconciliation is **account-global per currency** with
   **per-client attribution** + **external detection**. This is the best
   achievable while strategies share one cash pocket: the *ledger* is exact
   per client; the *cash residual* catches anything our clients don't explain.

2. **Sub-account segregation (the true industry form, ready in code).** Give
   each strategy its **own FA sub-account**. Provide
   `.gt_client_accounts.json = {client_id: account}`; the poller already reads
   **per-account** `CashBalance` (`v.account`). Then `reconcile_cash` ties out
   **per (account, currency) EXACTLY** — no commingling, no blind spot. This
   is how Jane Street / HRT / 2Sigma-class desks do it: segregated books +
   event-sourced fills + drop-copy reconciliation.

## The fix that made the cash side real

The earlier panel showed `IBKRΔ +0` for every currency because the monitor
never subscribed to account updates. **LV8 fix:** the server's cash poller
calls `ib.reqAccountUpdates(account)` (one connection) and reads
`CashBalance` per `(account, currency)`, refreshed ~2s. Baseline (the
"playing field at 0") is auto-captured per account exactly when the book is
flat (`open_pairs == 0`), so `ibkr_delta` aligns with the ledger window.

## The live view (10 FPS, `scripts/ledger_view.html` via `ledger_server.py`)

- force-directed currency graph: nodes sized by net cash (green long / red
  short), edges = pairs (thickness ∝ position, animated flow)
- **LV5** triangular **cycle glow** + `blind dims N` — the dimensions cash /
  positions() is structurally blind to
- **LV6** press **C** → color edges by client_id (segregation lens)
- **LV7** fills ticker + per-pair activity heatmap
- **LV8/LV9** per-currency cash ring (reconciled green / drift red),
  per-client attribution (hover), **`⚠ EXTERNAL ACTIVITY`** banner,
  `sub-acct recon` badge when FA sub-accounts are configured

## Run

```bash
# server (FX-28 roster; with account-level cash reconciliation)
python3 scripts/ledger_server.py --port 8888 --universe fx --with-cash
# from your Mac:
ssh -L 8888:localhost:8888 <ec2>   # then open http://localhost:8888
```

## Path to "perfect"

Single-account + per-client attribution + external detection = **as exact as
possible while sharing one cash pocket**. The remaining step to *perfect* is
operational, not code: **one FA sub-account per strategy** + populate
`.gt_client_accounts.json`. The code already reconciles per-account exactly
the moment that exists.

## Tests

- `tests/test_ledger_graph.py` — graph build, LV5 cycles, LV7 fills,
  **LV9 client attribution + external detection + sub-account exactness**.
