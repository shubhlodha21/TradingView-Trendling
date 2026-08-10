# FillLedger — durable, exactly-once position truth (FL1–FL6)

## Why

For spot FX, IBKR cannot report a per-pair position. It stores only
per-currency cash balances, and pairs that share a currency land in one
shared bucket. Modeling pairs as edges and currencies as vertices, the
account's cash vector is `b = A·x` where `A` is the signed currency-incidence
matrix. With more pairs than currencies, `A` is rank-deficient:

```
rank(A)   = V − C        # what positions() can observe
dim ker A = E − V + C    # the cycle space positions() is BLIND to
```

For the 15-pair fleet (8 currencies, 1 component): **rank 7, null-space 8** —
`positions()` carries only 7 independent numbers; 8 dimensions (triangular
loops) are invisible. This is a theorem, not a tuning problem. The only
complete observable is the stream of pair-tagged executions, and the true
position is their integral. That integral is wrong only if a fill is missed
or double-counted — so the fix is a gap-free, exactly-once fill journal.

## What

`src/execution/fill_ledger.py` — a standalone, `__slots__`, dependency-free
class:

* **Append-only JSONL**, one record per fill, keyed by broker-unique `execId`.
* **Dedup on read and write** → exactly-once (kills double-count).
* **fsync per append** → survives a hard `tmux kill-session`.
* **One file per `(symbol, port, client_id)`** → no cross-writer contention;
  scales to the 32-client TWS cap; matches the A79 single-writer model.
* `net(symbol)` = `Σ BOT − Σ SLD` — the authoritative position.

## Wiring (all additive; existing entry/exit/stop logic untouched)

| Step | File | Change |
|------|------|--------|
| FL2 | `engine.py` | `__init__` builds the ledger keyed by `(ticker,port,cid)` beside the state file; `_on_gateway_fill` records every execution. Opt-out: `GT_DISABLE_FILL_LEDGER=1`. |
| FL3 | `engine.py` | `_reconcile_missed_fills` merges `get_all_fills()` (reqExecutions) into the ledger on startup/reconnect — absorbs fills that landed while the bot was **dead**, deduped by execId. |
| FL4 | `broker.py` | `get_our_position_via_executions` prefers the durable ledger net when populated (survives IBKR's ~24h cache eviction); falls back to the live `ib.fills()` sum otherwise. Engine lends its ledger to the gateway (read-only borrow). |
| FL5 | `scripts/three_truths.py` | Monitor shows **three columns side-by-side** — ENGINE (state) · LEDGER (fills) · BROKER (positions). The broker source is KEPT; the ledger is added as the reliable arbiter. Cross-checks: engine vs ledger must agree (mismatch = real DRIFT); ledger vs positions() on FX is a soft WATCH (the cycle-space blind spot), on equity a hard DRIFT. `--ledger-only` mode skips the broker connection (zero client-id slots) when desired. Also: **per-pair cash legs** (USD.JPY → USD/JPY) and a **currency-cash reconciliation** (IBKR actual Δ vs ledger-expected A·x̂ → residual; `--rebaseline` zeroes the playing field). |
| FL7 | `engine.py` / `fill_ledger.py` | Floor the FL3 merge at `floor_ts` so a wiped+flattened restart doesn't replay stale pre-restart executions (which a different client may have closed). |
| FL8 | `engine.py` | **Engine self-heals.** In `_reconcile_position_state`, when the truth source is the ledger-backed execution sum and it disagrees with the engine: adopt LONG (set IN_POSITION + sized stop), fold to FLAT when ledger says 0 (missed close), or alert CRITICAL on a ledger short. Gated strictly to the ledger source — positions()/accountValues keep A53. This turns "the monitor *detects* drift" into "the engine *fixes* it," closing the MSFT-class trap (missed dead-window fill → engine blind → arms brackets on top → naked short). |

## Guarantees

* **Exactly-once**: reconnect replays / double-callbacks dedup by execId.
* **Gap-free across death**: a fill during downtime is absorbed on next reconcile.
* **Durable beyond 24h**: persisted JSONL outlives IBKR's execution cache.
* **FX-correct**: position is the receipt-integral, immune to the cycle-space
  blindness of `positions()`.
* **Safe**: every read/write is guarded; a ledger fault never disturbs the
  fill path or aborts reconcile. `GT_DISABLE_FILL_LEDGER=1` disables cleanly.

## Tests

* `tests/test_fill_ledger.py` — 20 unit checks (dedup, durability, merge,
  corruption tolerance, 32-client path scaling).
* `tests/test_fill_ledger_engine_integration.py` — FL2/FL3/FL4 against the real
  `Engine` + `MockGateway` / real `Gateway`.
* `tests/test_three_truths_ledger.py` — FL5 monitor uses ledger truth; the
  tonight USDCHF false-COHERENT now correctly flags DRIFT.

## Operate

```bash
# connection-free monitor (frees a client-id slot; reliable FX truth)
python3 scripts/three_truths.py --watch --ledger-only --universe mixed

# connected monitor now also uses ledger-net for FX (kills false WATCH/DRIFT)
python3 scripts/three_truths.py --watch --port 7497
```
