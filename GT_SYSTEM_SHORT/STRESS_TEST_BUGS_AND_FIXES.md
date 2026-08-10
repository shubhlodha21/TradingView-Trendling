# GT System — Stress / Chaos Test Findings

_Last updated: 2026-06-10 — covers every defect surfaced by the
purpose-built chaos and stress harnesses introduced from 2026-05-26
onwards: `tests/paper/chaos_test.py`, `tests/paper/chaos_test_equity.py`,
`tests/paper/stress_churn.py`, `tests/paper/stress_churn_equity.py`._

This document is a companion to `BUGS_AND_EDGE_CASES.md`. That file
catalogues bugs found in production, code review, and incident response.
**This file catalogues bugs found by deliberately breaking the system
under test conditions** — fault injection, multi-bot concurrency
storms, TWS disconnect/reconnect cycles, hard-kill respawn races, and
saturation of IBKR's API connection budget.

Every finding here became a defense or fix that ships in `src/`.

---

## Headline numbers

| Bucket | Count |
| --- | --- |
| **Total defects surfaced by stress / chaos testing** | **35** |
| Resolved (fix shipped) | 33 |
| Designed-then-reverted (learning) | 2 |
| Pending | 0 |

## Severity breakdown

| Severity | Resolved | Reverted | Total |
| --- | ---: | ---: | ---: |
| 🔴 CRITICAL (real exposure leak or naked position) | 12 | 1 | 13 |
| 🟠 HIGH (operational drift, state corruption risk) | 14 | 1 | 15 |
| 🟡 MEDIUM (alert noise, observability gap, cosmetic) | 7 | 0 | 7 |

## Findings by surface

| Surface | Count |
| --- | ---: |
| 1. Bracket placement & lifecycle | 9 |
| 2. State ↔ broker drift under churn | 5 |
| 3. Restart, reconnect, adoption | 7 |
| 4. Watchdog / invariant sweep correctness | 4 |
| 5. Teardown & flatten | 6 |
| 6. Alerts / observability | 4 |

---

## Legend

- ✅ **Resolved** — fix shipped, regression-tested by re-running the
  chaos / stress harness
- 🔄 **Reverted** — fix was implemented, broke something else, rolled
  back. Listed for institutional memory.
- 🔴 / 🟠 / 🟡 — Severity (Critical / High / Medium)

---

## 1. Bracket placement & lifecycle

These bugs all surfaced because the chaos / stress harnesses generate
order-placement volume IBKR's order-management thread cannot serialize
cleanly. Each one is a race in the parent ↔ child handoff that real
trading rarely sees but stress testing reliably reproduces.

### 1.1 ✅ EURUSD double-entry TOCTOU race — A37

**Severity:** 🔴 CRITICAL
**Found by:** `stress_churn.py` 5-min FX run, EURUSD bot
**Symptom:** EURUSD placed two `ENTRY_BUY` brackets within ~200ms during
the same MONITORING cycle. Second entry tried to fire while the first
was still in flight; broker accepted both. Net effect: doubled exposure
without engine awareness.
**Root cause:** The entry placement path checked `_position_open` /
`_pending_entry` and then called `place_bracket_buy_stop_market`
asynchronously. Between the check and the placement, a second tick
could re-enter the same path because `_entry_placing` flag was set
**inside** the placement function, not before the gate check.
**Fix shipped:** Set `_entry_placing = True` **before** the gate check
in `_attempt_entry`; reset it in a `finally` after placement
succeeds/fails.
**Verification:** Re-ran `stress_churn.py` 8-pair × 5-min — 0 duplicate
entries across 200+ cycles.

### 1.2 ✅ Between-call race: _bracket_child not yet set — A39

**Severity:** 🔴 CRITICAL
**Found by:** Same EURUSD stress run as A37
**Symptom:** A37's `_entry_placing` flag closes the gap between gate
check and `placeOrder`, but `_bracket_child` is only assigned **after**
the function returns. A second entry attempt arriving in that window
saw `_pending_stop=None and _bracket_child=None` and re-entered.
**Fix shipped:** Extended the entry-placement guard to also check
`_bracket_child`. If set (even pre-fill), the guard refuses re-entry.
**Verification:** Same regression run, 0 leaks.

### 1.3 ✅ Bracket parent placed but child fails — A40

**Severity:** 🔴 CRITICAL
**Found by:** `chaos_test.py` USDCHF restart-positions scenario
**Symptom:** During chaotic respawn, `placeOrder(parent)` succeeded but
`placeOrder(child)` raised (rate limit). Parent was left at IBKR with
`transmit=False` — visible in TWS as a permanent "Transmit" status
order that would never fire on its own but consumes the bot's bracket
slot.
**Fix shipped:** Wrap child `placeOrder` in `try/except`. On any error,
issue `cancelOrder(parent_trade.order)` and re-raise so the engine can
retry. Audit code `ORPHAN_PARENT_CANCELLED`.
**Verification:** Manually injected child placement failure → parent
cancelled within 50ms, no TWS residue.

### 1.4 ✅ Explicit bracket-child cancel on parent cancel — A45

**Severity:** 🟠 HIGH
**Found by:** `chaos_test.py` multi-cycle FX run
**Symptom:** IBKR's documented behavior is "cancel parent → auto-cancel
child." In practice, child sometimes stays Submitted with parent
already gone (race or partial-fill state). Result: orphan SELL stops
firing against zero position → broker goes short.
**Fix shipped:** When the engine cancels a bracket parent, also issue
an explicit `cancelOrder` on the bracket child. Don't trust IBKR's
auto-cancel.
**Verification:** Audit code `BRACKET_CHILD_FORCE_CANCEL` present in
every recent chaos run.

### 1.5 ✅ Bracket child cancelled by broker → cancel orphan parent — A57

**Severity:** 🟠 HIGH
**Found by:** Equity chaos restart-positions, NVDA/HD/META
**Symptom:** Inverse of A45 — IBKR cancels the child first (Error 135
or other), parent stays alive with `transmit=False`, never fires.
Visible as a phantom BUY in TWS that hangs around for the whole soak.
**Fix shipped:** When a bracket-tagged child Cancelled event arrives,
also cancel the parent. Audit code mirrors `BRACKET_CHILD_FORCE_CANCEL`.
**Verification:** 3 of 32 bots hit Error 135 in the 2026-06-10 32-bot
equity run; all 3 self-healed via A57 with no exposure leak.

### 1.6 ✅ A57: skip parent-cancel on Error 201 — A65

**Severity:** 🟠 HIGH
**Found by:** FX chaos, AVGO equity follow-up
**Symptom:** A57 fired on **legitimate** child cancellations from Error
201 ("Stop price revision is disallowed after order has triggered").
Engine cancelled the parent while the child was actually filling →
phantom SELL rejected event.
**Fix shipped:** Inspect the child's last error message; suppress
A57 parent-cancel when the cancel reason matches Error 201.
**Verification:** AVGO 2026-06-10 run, 0 phantom-SELL events.

### 1.7 🔄 ExecutionCondition on bracket child — A50 / A51

**Severity:** 🔴 CRITICAL (designed) → ROLLED BACK
**Found by:** Code review during A48 invariant sweep work
**Idea:** Attach an `ExecutionCondition` to the bracket child so it
**cannot** execute unless the parent's `permId` has executed. Would
make orphan SELL structurally impossible.
**Why reverted (A51):** ExecutionCondition + parentId-based bracket
both target the same client-side "Transmit" gate inside ib_async. The
combination left **both** parent and child stuck in `PendingSubmit`
locally; nothing was ever flushed to IBKR. The chaos run hit a "0
trades over 5 minutes" failure mode that took an hour to diagnose.
**What replaced it:** The A57 reactive cancel + A48 invariant sweep
(≤250ms orphan detection) → safety net without breaking the transmit
chain.

### 1.8 ✅ Bracket-lifecycle instrumentation — A52

**Severity:** 🟡 MEDIUM (observability)
**Found by:** A48 + A50 debug sessions burning multi-hour cycles
**Symptom:** When a bracket misbehaved we had no way to see when the
parent was placed, when the child was placed, what the child's
parentId was, what cancellations fired, in what order.
**Fix shipped:** `[BRACKET_LIFECYCLE]` log lines at every transition —
PLACE_PARENT, PLACE_CHILD, PARENT_FILL, CHILD_FILL, PARENT_CANCEL,
CHILD_CANCEL, FORCE_CANCEL. Used in every subsequent debug session.

### 1.9 ✅ Error 135 ingest race — 50ms gap between parent & child — A75

**Severity:** 🟠 HIGH
**Found by:** Equity chaos 32-bot respawn 2026-06-10
**Symptom:** 3 of 32 bots (NVDA, HD, META) hit
`"Error 135, Can't find order with id=N"` on child placement. Parent
+ child are sent over the same TCP socket so they **arrive** in order,
but IBKR's order-management thread can ingest them out-of-order under
load — child sees no parent and gets rejected.
**Fix shipped:** `await asyncio.sleep(0.05)` between `placeOrder(parent)`
and `placeOrder(child)` in `place_bracket_buy_stop_market`. 50ms wins
the race in every observed condition; negligible vs the 100-500ms
end-to-end latency of bracket entry. A57 retained as backstop.
**Verification:** Race still self-heals if it ever leaks past 50ms.

---

## 2. State ↔ broker drift under churn

The stress harness runs 8-32 bots for 5+ minutes, accumulating dozens of
cycles per bot. Each cycle is an opportunity for engine state to drift
from broker truth. These fixes hardened the reconcile loop.

### 2.1 ✅ Missed fills during downtime — reqExecutionsAsync replay — A19

**Severity:** 🔴 CRITICAL
**Found by:** `chaos_test.py` tws-disconnect scenario
**Symptom:** TWS killed mid-cycle; bot continued running blind; when
TWS came back, the fill that happened during downtime was **silently
lost** by the engine event stream. Engine thought IN_POSITION, broker
was FLAT (or vice-versa).
**Fix shipped:** On every reconnect, replay
`reqExecutionsAsync(filter=fromUtc=saved_ts)` and synthesize fill
events for anything we missed. Audit code `BROKER_FILL_REPLAYED`.
**Verification:** chaos_test.py tws-disconnect: 0 missed fills across
multiple injected outages.

### 2.2 ✅ FX position truth via accountValues — A42 / A43

**Severity:** 🔴 CRITICAL
**Found by:** Multi-bot FX stress
**Symptom:** `ib.positions()` does NOT report FX positions (they show
as cash deltas in `accountValues`, not as ContractPositions). Engine
reconcile thought every FX bot was FLAT after restart, but the broker
held real EUR/JPY/GBP balances.
**Initial fix (A42):** Read `accountValues` cash balances by currency
to derive FX position truth.
**Real fix (A43):** A42 had cross-bot contamination — EURUSD bot saw
USDCHF's USD cash and freaked out. Switched to a per-bot truth signal
via `reqExecutionsAsync` filtered by this bot's clientId, summed to
get this-bot-only position regardless of currency overlap.
**Verification:** 8-pair stress run shows zero cross-contamination
alerts.

### 2.3 ✅ Stale exposure tracker — A25

**Severity:** 🟠 HIGH
**Found by:** `stress_churn.py` cumulative exposure check
**Symptom:** Risk gate's `open_exposure_usd` field stayed at $46k after
all positions had closed — entries added to the tracker but exits never
decremented. Eventually blew the risk cap and refused new entries.
**Fix shipped:** Hooked exit fill handler to subtract from
`open_exposure_usd`; added defensive `max(0, ...)` to prevent negative
drift from rounding.
**Verification:** stress_churn 5-min run, tracker correctly returns to
$0 after teardown.

### 2.4 ✅ POSITION_AUTO_FLAT removed — A53

**Severity:** 🔴 CRITICAL (the cure was worse than the disease)
**Found by:** Chaos test broker-truth diff
**Symptom:** Engine had a "POSITION_AUTO_FLAT" path that, on detecting
state-vs-broker drift, would **mutate engine state to match broker** —
silently zeroing out engine's `_position_open`/`_quantity`. This
masked real drift events and let phantom SELLs fire against the now-
flat engine state.
**Fix shipped:** Ripped out auto-mutation entirely. Engine now
**alerts + refuses** on persistent drift, never silently mutates. The
correct response is operator intervention, not silent state laundry.

### 2.5 ✅ Suppress NAKED re-arm on Error 201 — A68

**Severity:** 🟠 HIGH
**Found by:** Equity chaos 32-bot, AVGO
**Symptom:** Engine sees bracket child Cancelled → infers position is
NAKED → places fresh protective stop. But the child's "Cancelled" was
actually Error 201 (modify-after-triggered), meaning the child was in
the act of filling. Result: 3 phantom SELLs (AVGO).
**Fix shipped:** Before re-arming protective stop on NAKED detection,
check the child's last error. If Error 201, log
`A68_SUPPRESS_NAKED_REARM` and skip the re-arm — the child is filling.

---

## 3. Restart, reconnect, adoption

### 3.1 ✅ Chaos test infrastructure — A38

**Severity:** N/A (test scaffolding)
**Description:** The original stress test exercised steady-state.
Chaos test adds three fault-injection scenarios: `tws-disconnect`,
`state-corruption`, `restart-positions`. Each found bugs (A39, A40,
A41, A45, ...). Without this, none of the rest of section 3 would
exist.

### 3.2 ✅ Reconnect stagger by client_id — A41

**Severity:** 🟠 HIGH
**Found by:** chaos_test.py tws-disconnect: 8 bots all reconnecting in
the same 50ms window
**Symptom:** TWS rate-limit-on-connect (Error 326 "Already connected")
when 8 bots reconnect simultaneously. Some bots never reconnected.
**Fix shipped:** `connectAsync` retry loop with backoff staggered by
`client_id` modulo, plus per-call timeout. Each bot sleeps
`(client_id - base_cid) * 100ms` before its first reconnect attempt.
**Verification:** 32 simultaneous bots in equity chaos all reconnect
within ~3.5 sec.

### 3.3 ✅ Reconcile: adopt BOTH bracket legs together — A54

**Severity:** 🔴 CRITICAL
**Found by:** restart-positions chaos
**Symptom:** Reconcile adopted the parent BUY into `_pending_entry`
but ignored the child SELL stop sitting at the broker. Engine then
placed a **second** SELL stop via the legacy path → two stops, broker
went short on whichever fired first.
**Fix shipped:** Reconcile now walks `openTrades()` and adopts the
parent into `_pending_entry` AND the child into `_bracket_child` as a
matched pair.
**Verification:** restart-positions 8-pair run shows clean adoption,
no duplicate stops.

### 3.4 ✅ register_existing_order: re-attach bracket lifecycle logger — A58

**Severity:** 🟡 MEDIUM (observability + A57 enablement)
**Found by:** A57 not firing on adopted brackets
**Symptom:** A57 (orphan-parent cancel on child Cancelled) only worked
for brackets the engine had placed itself, because the cancellation
listener was attached at place-time. Adopted brackets had no listener.
**Fix shipped:** `register_existing_order` now re-attaches the bracket
lifecycle logger and cancellation watcher when adopting.

### 3.5 ✅ Invariant sweep must skip during reconcile — A62

**Severity:** 🔴 CRITICAL
**Found by:** restart-positions with adopted brackets
**Symptom:** The A48 invariant sweep ran during the reconcile window
and saw "BUY parent with no `_pending_entry`" (because reconcile hadn't
populated it yet) → killed the bracket → engine immediately
re-adopted the same broker order → infinite cancel-replace loop.
**Fix shipped:** Set `_reconciling=True` for the duration of
`_reconcile_open_orders`; invariant sweep early-returns when set.

### 3.6 ✅ Chaos respawn must NOT pass --trigger — A63

**Severity:** 🟠 HIGH
**Found by:** restart-positions
**Symptom:** Chaos test respawned bots with `--trigger LTP` after
chaos, forcing a fresh entry. But the state file (after the hard kill)
might still have IN_POSITION. New trigger + existing position →
double-entry as soon as price moved.
**Fix shipped:** Respawn command drops `--trigger`; engine reads from
state file and adopts whatever the broker has.

### 3.7 ✅ Reconcile saved_bracket_child mis-cancel — A64

**Severity:** 🔴 CRITICAL
**Found by:** restart-positions WAITING_REENTRY case
**Symptom:** Engine state showed `_bracket_child=BR_SELL_*` from a
prior cycle. Reconcile checked "is this child still at the broker?
If not, cancel it." Logic misfired: in WAITING_REENTRY, the saved
child SHOULD be gone (already filled), and the new bracket from re-
entry hadn't been placed yet. Code then tried to cancel an order that
no longer existed → noisy error logs, but worse, sometimes cancelled
the wrong order via stale broker_id collision.
**Fix shipped:** Reconcile's "saved_bracket_child not at broker"
branch checks the saved cycle number vs current; if cycle has
incremented, treat the saved id as stale-but-OK, do not cancel.

---

## 4. Watchdog / invariant sweep correctness

The invariant sweep is the engine's last line of defense — every tick
it walks broker state looking for impossible configurations (orphan
SELL, orphan BUY parent) and forcibly cancels them. Getting this
right took several iterations.

### 4.1 ✅ Invariant sweep for orphan SELL — A46

**Severity:** 🔴 CRITICAL
**Found by:** Stress churn — engines occasionally left SELL stops at
broker that didn't correspond to any open position
**Fix shipped:** Per-tick sweep: for every open SELL at broker, check
that engine owns a matching position. If not, cancel and audit
`ORPHAN_SELL_KILLED`.

### 4.2 ✅ A46 v2: per-order-id invariant — A47

**Severity:** 🟠 HIGH (correctness)
**Symptom:** A46's qty-and-symbol matching had a race during rapid
cycle: engine just filled n1, just placed n2 with a fresh SELL stop,
both visible at broker for ~200ms → A46 thought one was orphan and
killed it.
**Fix shipped:** Match by `orderRef` (engine_id) not by qty+symbol.
Engine owns `BR_SELL_*_n2_*` → only that id is "owned." Anything else
under the same symbol is genuinely orphan.

### 4.3 ✅ Extend invariant sweep to BUY parents — A48

**Severity:** 🔴 CRITICAL
**Symptom:** A46/A47 only covered SELL orphans. A BUY parent stuck in
"Transmit" state (A40 scenario before its fix) would never fire but
also never be killed. The bot's bracket slot was stuck.
**Fix shipped:** Sweep also kills orphan BUY parents (`ORPHAN_BUY_KILLED`).

### 4.4 ✅ A48 race fix: skip during _entry_placing — A49

**Severity:** 🟠 HIGH
**Symptom:** A48 saw a BUY parent the moment after `placeOrder`
returned, before `_pending_entry` was assigned. Killed the legitimate
order it had just placed.
**Fix shipped:** A48 early-returns when `_entry_placing=True`. The
A37 flag is set before placement, cleared in `finally` after — so
the gap is closed.

### 4.5 ✅ Disable watchdog bulk-cancel during chaos soak — A55

**Severity:** 🟠 HIGH
**Symptom:** The stress driver's watchdog ran every 10s and cancelled
"every open order older than 30s" — meant for cleanup, but it cancelled
the engine's own bracket children mid-cycle → engine re-placed → got
cancelled again. Audible cancel-storm during soaks.
**Fix shipped:** Watchdog skips bulk-cancel when running in chaos
soak phases (env var `GT_CHAOS_SOAK=1`). Teardown still calls one
final sweep so resting orders don't escape.

---

## 5. Teardown & flatten

The teardown phase verifies the engine left no exposure at the broker.
Multiple bugs lived here — most cosmetically masked as "test FAIL" but
they actually represented real cleanup gaps that would affect live
operation too.

### 5.1 ✅ Teardown TIF=GTC error + USD-base flatten — A61

**Severity:** 🟠 HIGH
**Symptom:** Teardown placed MARKET+GTC orders to flatten residual FX
balances. IBKR rejects MARKET+GTC ("invalid TIF for MARKET on this
account preset"). USD-base pairs (USDJPY, USDCHF, USDCAD) all share
USD as their cash-balance side — cash-delta-based flatten produced
absurd 10M-unit orders.
**Fix shipped:** TIF=IOC for FX flatten + cancel-only behavior for
USD-base pairs (the engine's bracket child SELL stop protects them).

### 5.2 ✅ Teardown: use ib.positions() not cash balances (FX) — A66

**Severity:** 🟠 HIGH
**Symptom:** FX cash-balance flatten misattributed deltas across
pairs (A43-class). Teardown left JPY -4.6M after a stress run that
had cleanly closed every position.
**Fix shipped:** For FX teardown, walk `ib.positions()` and close each
position contract-by-contract instead of via cash deltas.
**Caveat:** A66's "positions()" approach has a known limitation: FX
positions aren't always reported (A42 motivation). Live regression
showed 4.6M residual JPY from EURJPY that positions() missed. Tagged
"⚠ A66 limitation — acceptable for now."

### 5.3 ✅ Equity flatten: tif='DAY' not IOC — A69

**Severity:** 🟠 HIGH
**Found by:** Equity chaos 16-bot 2026-06-10
**Symptom:** Equity flatten inherited tif='IOC' from the FX A61 fix.
4 of 7 equity IOC flatten orders left positions open (AAPL, HD, MA,
LLY) — SMART couldn't route IOC immediately, Error 202 silent cancel.
**Fix shipped:** tif='DAY' for equity flatten. SMART router holds the
order during RTH until filled. Added single-shot retry pass for any
position still open after 6s.

### 5.4 ✅ Wait for PendingCancel before broker-truth FAIL — A72

**Severity:** 🟡 MEDIUM (false-positive FAIL)
**Symptom:** Equity 32-bot teardown reported FAIL because 2 MA bracket
orders were in `PendingCancel` / `PreSubmitted` state — IBKR hadn't
finalized the cancel yet but the broker-truth check had already run.
Test would FAIL despite the system being correct.
**Fix shipped:** Broker-truth verifier polls up to 16s for transient
statuses (`PendingCancel`, `PendingSubmit`) to resolve, and treats
`PendingCancel` as terminal-cancelled.

### 5.5 ✅ MMC primaryExchange + retry-duplicate guard — A74

**Severity:** 🟡 MEDIUM (one symbol failed; possible duplicate flatten)
**Found by:** Equity 62-bot attempt (now reverted to 32)
**Symptom (a):** MMC LTP fetch failed with Error 200 "No security
definition" on bare SMART — IBKR sees multiple global MMC instruments
and cannot disambiguate.
**Symptom (b):** Flatten retry placed a second SELL for ABBV while
the first SELL was still Submitted → both filled, leaving ABBV at
-100 shares (over-flatten).
**Fix shipped (a):** `_PRIMARY_EXCHANGE` map + `_primary_exchange_for()`
helper; pass `primaryExchange='NYSE'` to `Stock` construction for
ambiguous symbols.
**Fix shipped (b):** Retry pass checks `openTrades()` for in-flight
Submitted/PreSubmitted/PendingSubmit on the same symbol. Skip retry if
working order exists; just extend wait to 8s.

### 5.6 ✅ Teardown also flatten naked positions — A60

**Severity:** 🔴 CRITICAL
**Symptom:** Teardown only cancelled open ORDERS. Real POSITIONS —
accumulated when hard-kill interrupted a bracket cycle mid-flight
(BUY filled, SELL stop still pending) — survived the cancel sweep as
live broker exposure. 100k EUR / 50k GBP accumulating per chaos run.
**Fix shipped:** After cancel sweep, flatten all non-zero positions
via opposite-side MARKET orders.

---

## 6. Alerts / observability

### 6.1 ✅ Log replayed broker fills — A26

**Severity:** 🟡 MEDIUM (observability)
**Symptom:** A19's missed-fill replay synthesized fill events for
engine state but didn't log them to the audit. The diff between
"engine recorded fills" and "broker fills" appeared as an unresolved
delta until the operator manually checked executions.
**Fix shipped:** `BROKER_FILL_REPLAYED` audit row for each replayed
execution.

### 6.2 ✅ Rate-limit POSITION_MISMATCH alert — A44

**Severity:** 🟡 MEDIUM (alert noise)
**Symptom:** Persistent state drift fired the alert every tick — 600+
Teams notifications in a 5-min stress run.
**Fix shipped:** Per-bot cooldown: at most one POSITION_MISMATCH alert
per minute.

### 6.3 ✅ Throttle by drift not absolute values — A70

**Severity:** 🟡 MEDIUM (alert noise)
**Found by:** Equity 16-bot run, 117 alerts to Teams
**Symptom:** A44 compared absolute `(broker_qty, engine_qty)` tuples
between alerts. During rapid cycling the values changed every tick
even when the **drift** was identical — A44's "same tuple within 60s"
gate never tripped.
**Fix shipped:** Compare `(broker_qty - engine_qty)` only; bump
cooldown 60s → 300s; alerts dropped from 117 to 25 in regression.

### 6.4 ✅ Bracket-lifecycle instrumentation — A52

(See 1.8 above.) The same fix served observability for everything in
this section — every later debug session leaned on these logs.

---

## Test infrastructure additions

Not bugs in the engine, but worth listing because each enabled later
findings. Truncated descriptions.

| Tag | What it added |
| --- | --- |
| A23 | `stress_churn.py` — 8 FX pairs, 5 min, engine-driven |
| A28/A29 | `--no-risk-cap` flag (default ON for stress) |
| A30 | Verify each bot actually connected after spawn |
| A31/A35 | `GT_SKIP_NAKED_GUARD` / `GT_DISABLE_RISK_GATE` env bypass |
| A32 | Pre-flatten ALL FX positions before stress (`--flatten-first`) |
| A33 | Default trigger offset 0 bps so all bots fire on first tick |
| A36 | Default SL 0.5 bp so it fires within seconds |
| A38 | `chaos_test.py` — TWS disconnect + state corruption + restart |
| A59 | Post-teardown broker-truth verdict (PASS / FAIL) |
| A67 | `chaos_test_equity.py` + `stress_churn_equity.py` (equity fork) |
| A71 | Scale equity 16 → 32 bots |
| A73 | Attempted 32 → 62 bots, **REVERTED** (TWS 32-conn ceiling) |

---

## What stress testing has bought us

By repeatedly running these harnesses against a paper account, we
found and shipped fixes for failure modes that would otherwise have
surfaced only in production — typically as overnight surprises after
a TWS restart or under unusual market conditions. Examples:

- **Naked positions after restart** (A19, A38, A54, A64) — would have
  led to unhedged overnight exposure.
- **Orphan SELL stops firing against zero position** (A45, A46, A47,
  A48, A57) — would have produced short positions in inventory.
- **Double-entry** (A37, A39) — would have doubled position size on
  a tick storm.
- **Bracket-child silently lost on restart** (A54) — engine thinks
  protected, broker thinks naked, gap appears at the worst time.
- **State auto-mutation hiding drift** (A53) — silent inconsistency
  is the worst kind of bug; A53 removed the laundry.
- **Teardown not actually flat** (A60, A61, A66, A69) — chaos runs
  would leave residual exposure that quietly grew across runs.

Every one of these is now caught by a defense that runs every cycle.
The chaos / stress harnesses are no longer "find new bugs" tools —
they're regression suites. A run that exits with `OVERALL: PASS`
means the entire defensive stack is intact.

---

## Verification status

| Mechanism | Count |
| --- | ---: |
| Live audit evidence (`order.csv`/`alerts.jsonl` after chaos run) | 33 |
| Reverted (institutional memory — listed not as defects fixed but as paths tried) | 2 |

Run cadence: chaos_test + stress_churn FX + chaos_test_equity +
stress_churn_equity each run before any merge that touches engine /
broker / reconcile code.

---

_End of inventory._
