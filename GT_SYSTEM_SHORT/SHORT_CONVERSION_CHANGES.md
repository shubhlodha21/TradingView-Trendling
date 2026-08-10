# SHORT Strategy Conversion — Change Log

Converts the long-only breakout engine into a **short breakdown** engine
(per `Strategy_Breakout_Short_Flow_Chart.pdf`).

- **Branch:** `short-strategy` (created off `multi-asset`)
- **Date:** 2026-06-23
- **Scope:** `src/strategy/engine.py`, `src/execution/broker.py`, `run_live.py`
- **Status:** First complete pass. **Syntax validated (`py_compile` OK).**
  **NOT yet runtime-tested. MUST be paper-tested before `GT_PAPER=false`.**

## ⚠️ READ FIRST

This inverts safety-critical, real-money order logic. Do **not** run live until you
have run a full paper cycle (entry → SL hit → re-entry) and confirmed the orders
in TWS look right (SELL stop-limit entry below market, BUY stop cover above entry).

Launch command is unchanged in shape, e.g.:

```
GT_PAPER=true python run_live.py EURUSD --trigger 1.14231 --port 7496 \
  --client-id 1 --offset-fixed 0.00010 --stop 0.0020 --uvloop --qty 43750
```

`--trigger` is now the **breakdown** level (enter short when price FALLS to it).

---

## The model (long → short)

| Aspect            | Long (before)                        | Short (after)                          |
|-------------------|--------------------------------------|----------------------------------------|
| Entry order       | BUY stop-limit @ trigger, lim=trig+off | **SELL** stop-limit @ trigger, lim=trig−off |
| Entry fires when  | price RISES to trigger               | price **FALLS** to trigger             |
| Protective leg    | SELL stop @ entry×(1−pct) (below)    | **BUY** stop @ entry×(1+pct) (above)   |
| Opening fill      | BUY fill                             | **SELL** fill                          |
| Closing fill      | SELL fill                            | **BUY** (cover) fill                   |
| Tracks extreme    | highest price (peak)                 | **lowest** price (trough)              |
| Re-entry level    | prior peak (re-enter on move up)     | prior **trough** (re-enter on move down) |
| PnL               | (exit − entry)×qty                   | (entry − cover)×qty                     |
| Healthy broker qty| positive                             | **negative**                           |

---

## Conventions used in this conversion

1. **All changes are tagged** in code with a comment containing `SHORT INVERSION`
   plus the point id (`P1`..`P10`). Grep for `SHORT INVERSION` to see every edit.
2. **Internal field/method names kept** to minimise blast radius across the
   state-file schema, dashboard, and reconcile:
   - `_highest_price`, `_feed_high_at_entry`, `_track_high`,
     `_previous_breakout_level` now hold the **trough / breakdown** value.
     Read "high/peak/breakout" as "tracked extreme / breakdown".
   - `_quantity` stays a **positive magnitude**; direction is implied (short).
   - `saved_buy_intent` (naked-guard) now holds the **SELL** entry intent.
   - `_preflight_sell_allowed` now guards the **BUY cover** (arg name kept).
   - A few audit/alert **code strings** kept (e.g. `PHANTOM_SELL_REJECTED`,
     `DUPLICATE_SELL_GUARD_ADOPTED`, `ORPHAN_SELL_CANCELLED`,
     `SHORTING_PREVENTED`, `CUSTOM_LEDGER_SHORT`) so any downstream
     greps/alert routing keep working; the human messages + `side` fields were
     flipped.

---

## Changes by point

### P1 — Entry order: BUY stop-limit → SELL stop-limit
`src/strategy/engine.py`
- `_place_entry_stop_limit_inner`: `limit = stop − offset` (was `+`); engine_id
  `ENTRY_SELL`; `OrderRecord`/history/audit `side=SELL`; bracket child id
  `BR_BUY`; risk-reject + circuit-break audit `side=SELL`; paper fallback
  `place_stop_limit(side=SELL)`; `_pending_stop.side=SELL`,
  `_bracket_child.side=BUY`; child registry/history `side=BUY`;
  `BRACKET_SUBMITTED side=BUY`.
- `start()`: restore re-entry log "Placing SELL STOP_LIMIT".

### P2 — Protective stop geometry: entry×(1−pct) → entry×(1+pct)
`src/strategy/engine.py`
- `_protective_stop_price`: `raw = entry × (1 + pct)` (stop now ABOVE entry).
`run_live.py`
- Startup preview: `stop_est = trigger × (1 + pct)` (P10).

### P3 — Fill handler: SELL opens, BUY covers
`src/strategy/engine.py` `_on_gateway_fill`
- Top-level branch conditions swapped: `if side==SELL` = open short;
  `elif side==BUY` = cover/close.
- Open branch: extreme seed lowers (`avg_price < _highest_price`);
  `_feed_high_at_entry = feed.low`; `record_fill(...,"SELL")`; logs/audit/notify
  `side=SELL`; bracket-child promote `_pending_stop.side=BUY` and "(1+pct)".
- Cover branch: phantom-cover labels; partial guard `side=BUY`,
  `position_at_time=SHORT`; entry-remainder cancel looks up `ENTRY_SELL`;
  `record_fill(...,"BUY")`; FILLED audit `side=BUY/SHORT`; notify `side=BUY`.
`src/execution/broker.py`
- `place_bracket_buy_stop_market` **renamed** → `place_bracket_sell_stop_market`;
  parent `action=SELL`, child `action=BUY`. (Engine call site updated.)
- Manual force-exit + gap market fallback now place **MARKET BUY (cover)**.

### P4 / P5 — Track trough; re-enter at prior low
`src/strategy/engine.py`
- `_track_high`: compares `candidate < extreme` (sentinel `+inf`); uses
  `feed.low`.
- `_gap_fill_highest_price` & `_gap_fill_peak_window`: `min(b.low)` instead of
  `max(b.high)`; `<` comparisons.
- Re-entry placed at `_previous_breakout_level` (= trough) via the SELL entry path.

### P6 / P7 — Reactive stop + price selection
`src/strategy/engine.py`
- `_track_position` reactive backup: `ltp >= stop_loss` (was `<=`).
- `on_tick`: IN_POSITION tracks/checks against **buy_px** (ask, cover cost);
  entry trigger checks against **sell_px** (bid, short proceeds).
- `_check_stop_limit`: logic unchanged (already side-keyed); comment updated.
- `_place_protective_stop_inner` gap guard flipped to **gap-UP** (`ltp >= stop`).

### P8 — PnL sign
`src/strategy/engine.py`
- Realized: `gross_pnl = (entry − price) × qty`.
- Unrealized: `(entry − ltp) × qty` in `_log_state_and_pnl` and `get_summary`.

### P9 — Reconcile / naked-position / health-check sign (the dangerous core)
`src/strategy/engine.py`
- `_broker_qty_for_symbol`: documented as **signed** (negative = short).
- Naked-position guard: adopt when `broker_qty < 0`; match magnitude
  `_quantity == -broker_qty`; hydrate `_quantity = -broker_qty`; capture **SELL**
  entry intent; check resting **BUY** cover.
- `_reconcile_position_state`: `engine_qty = -_quantity` (signed compare).
- FL8 self-heal: adopt `broker_qty < 0` as short + re-arm BUY cover;
  `broker_qty > 0` flagged as unexpected long.
- `_reconcile_open_orders_inner` role inference: SELL STP-LMT = entry,
  BUY STP-LMT = legacy cover, BUY STP = bracket-child cover.
- A82 prior-session orphan guard: targets stray **BUY** cover (would fire into a long).
- A54 bracket-child re-adoption: parent `ENTRY_SELL`/SELL, child BUY STP.
- `_preflight_sell_allowed`: now guards the BUY cover — safe only if broker is
  short ≥ qty (`-broker_qty >= qty`); else refuses (would go long).
- Health check: Probe 1 re-arms when `not has_buy_sl`; Probe 1b divergence checks
  the BUY cover; Probe 2 expects a resting **SELL** entry (`not has_sell_sl`);
  "SELL entry still working" gate.
- Invariant sweep: already side-agnostic (tracks both sides) — no change needed.

### P10 — CLI display
`run_live.py`
- Stop preview uses `(1 + pct)`; added `Direction: SHORT` line; stop labelled
  "Stop (BUY cover) … above entry".
- `--reset` flatten already sign-aware (SELL longs / BUY shorts) — unchanged.

---

## Verified consistent

- **Paper mode** (`GT_PAPER=true`): `_paper_stop_limit` triggers are side-keyed
  (SELL fires on price falling, BUY on rising); paper positions record a
  **negative** qty on SELL → correct short. Reconcile is skipped in paper.
- `py_compile` passes for all three files.

## NOT changed (intentionally)

- State-file JSON schema / keys (field names reused).
- Dashboard internals beyond labels (it reads `get_status()` which now reports
  `position: SHORT` and inverted unrealized PnL).
- Audit/alert **code** strings (only `side`/messages flipped).

## Known residual COSMETIC items (no functional impact — clean up later)

These are audit/log **labels** still reading "LONG"/"SELL"/"BUY" that don't affect
order placement or risk logic:
- `_on_order_status` / `register_existing_order` audit rows still use
  `position_at_time="LONG"` in a few places (engine.py ~L1122, ~1165, ~1268).
- A couple of reconcile cancel audit rows (`side="SELL"`) around engine.py
  ~L3221 / ~L3469 — the actual cancels are direction-correct.
- Comment-only references to the old `place_bracket_buy_stop_market` name in
  `models.py`, `broker.py`, `engine.py` (the live call site is updated).
- The "place ONE SELL for the total quantity" senior-quant **comment** in the
  open branch (code uses the inverted bracket/cover).

## Flowchart cross-check (2026-06-23)

Reviewed the code against `Strategy_Breakout_Short_Flow_Chart.pdf`. All boxes
are implemented. Notes:

- **Risk guardrails** ($50k margin / $2k loss) already match config defaults.
- **"Exit only on SL, no profit booking"** — no take-profit logic exists; satisfied.
- **Parent "SELL LIMIT" wording** — implemented as a SELL **STOP-LIMIT** (rests and
  triggers on the breakdown). A plain SELL LIMIT would fill immediately above
  the trigger, so this is a deliberate, mechanically-correct deviation.
- **Re-entry offset sign — DECISION (confirmed with user 2026-06-23):**
  the flowchart's WAITING_REENTRY box says `Limit = trigger + offset`, which
  contradicts the initial-entry box (`trigger − offset`) and is mechanically
  wrong for a short. **Decision: use `trigger − offset` for re-entry too**
  (consistent + correct). The code already does this (`_place_entry_stop_limit`
  → `limit = stop − offset`). No change made. The `+offset` in the flowchart is
  treated as a long-template leftover.
- **Equity short-locate / SSR** — not shown in the flowchart and not a code
  change: equity shorts can be rejected by IBKR for borrow/uptick reasons
  (handled gracefully as BRACKET_PLACE_FAILED). Irrelevant for FX (e.g. EURUSD).

## TEST CHECKLIST before going live

1. `GT_PAPER=true` run; confirm:
   - On price FALLING to `--trigger`: a SELL fills → state IN_POSITION (SHORT).
   - A BUY stop cover rests ABOVE entry at entry×(1+stop).
   - On price RISING to the cover: BUY fills → P&L = (entry − cover)×qty.
   - WAITING_REENTRY arms a new SELL entry at the prior **low**.
2. Live with **tiny qty** first; eyeball the two legs in TWS.
3. Restart mid-position; confirm reconcile adopts the short (no fold, cover kept).
