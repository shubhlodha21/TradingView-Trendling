# GT System — Bug & Edge Case Inventory

_Last updated: 2026-06-03 — covers all issues discovered, resolved, or
designed through the 2026-05 / 2026-06 hardening sessions._

---

## Headline numbers

| Bucket | Count |
| --- | --- |
| **Total bugs / edge cases tracked** | **42** |
| Resolved (fix shipped) | 27 |
| Designed but not implemented | 1 |
| Pending — not yet addressed | 12 |
| Won't-fix / out of current scope | 2 |

## Severity breakdown

| Severity | Resolved | Pending | Designed | Won't-fix | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| 🔴 CRITICAL (can short the account / lose real money) | 10 | 4 | 1 | 0 | 15 |
| 🟠 HIGH (operational drift, slippage, missed protection) | 11 | 5 | 0 | 0 | 16 |
| 🟡 MEDIUM (silent inconsistency, observability gap) | 5 | 3 | 0 | 1 | 9 |
| 🟢 LOW (cosmetic / docs / convenience) | 1 | 0 | 0 | 1 | 2 |

## Resolved-vs-pending by category

| Category | Resolved | Pending | Total |
| --- | ---: | ---: | ---: |
| 1. State ↔ Broker Drift (shorting class) | 6 | 3 | 9 |
| 2. Bracket / Order Lifecycle | 9 | 2 | 11 |
| 3. Restart & Recovery | 4 | 2 | 6 |
| 4. Risk & Portfolio Exposure | 3 | 2 | 5 |
| 5. Multi-bot / Multi-client | 2 | 1 | 3 |
| 6. Observability / Alerts / Operations | 2 | 3 | 5 |
| 7. Asset class / Feed | 0 | 1 | 1 |
| 8. Tooling / Deployment | 1 | 1 | 2 |

---

## Legend

- ✅ **Resolved** — fix is in source, tested in paper (or has audit evidence of firing in live)
- ⚙️ **Designed** — implementation plan written, not yet coded
- ⏳ **Pending** — known gap, not yet planned in detail
- 🚫 **Won't-fix** — explicitly deferred or out of scope

---

## 1. State ↔ Broker Drift (shorting class)

### 1.1 ✅ ZM double-shorting incident — 2026-06-01

**Severity:** 🔴 CRITICAL
**Symptom:** Engine restarted between bracket submission and parent fill;
forgot `_bracket_child` in memory; placed a duplicate legacy SL via the
fallback path; both stops eventually fired, broker went SHORT 100 ZM.
**Root cause:** Two sources — (a) no periodic state save meant restart
amnesia for any in-memory mutation between events, (b) no pre-flight
broker-side SELL check before placing the legacy SL.
**Fixes shipped:**
- `_periodic_state_save_loop` at 1s cadence (was event-driven only)
- `_place_protective_stop_inner` pre-flight `fetch_open_orders` check —
  if a SELL already exists at broker, adopt it instead of placing duplicate
  (`DUPLICATE_SELL_GUARD_ADOPTED` audit code)
- `_bracket_child` field persisted in state file + restored on load
**Verification:** AAPL test on 2026-06-02 confirmed the guard fires
(`DUPLICATE_SELL_GUARD_ADOPTED` correlation_id `8da9c3d9`).

### 1.2 ✅ PLTR -30 shorting incident — 2026-06-02

**Severity:** 🔴 CRITICAL
**Symptom:** Engine restarted with `--qty 100` while broker still held a
resting `BR_SELL_30` from an earlier `--qty 30` session. Reconcile silently
adopted it. When fired, broker SOLD 30 against the 30-share position →
broker FLAT but the SELL kept firing on the second cycle → broker went
SHORT 30 PLTR.
**Root cause:** `_reconcile_open_orders` adopted any same-side, same-symbol
SELL without validating qty against current engine config.
**Fix shipped (new this session):** Qty-mismatch guard in
`_reconcile_open_orders` — for any SELL STP/STP-LMT, compares broker qty
to engine expected qty (current `_quantity` if open, else `config.quantity`).
On mismatch, cancels at broker, audits `STALE_SELL_REJECTED`, alerts HIGH,
skips adoption.
**Verification:** `scripts/repro_naked_sell.py` reproduces the exact
scenario in paper and confirms guard cancels the orphan within ~5s.

### 1.3 ✅ Phantom-SELL fill rejection (engine-level)

**Severity:** 🟠 HIGH
**Symptom:** A SELL fill arrives while engine is FLAT — would otherwise
cause `_quantity` to go negative in books.
**Fix shipped:** `PHANTOM_SELL_REJECTED` — engine refuses to update its
own state when fill side is SELL but `_position_open=False`.
**Caveat:** This guard only fixes the engine's books. The broker-side
trade has ALREADY happened by the time we get the fill event. The
**real** prevention is the 1.2 qty-mismatch guard, which kills the
orphan at the broker BEFORE it can trigger. Phantom-SELL is the
belt to that suspenders.

### 1.4 ✅ Auto-FLAT when engine LONG but broker FLAT

**Severity:** 🔴 CRITICAL (2026-05-27 META postmortem)
**Symptom:** Engine restored IN_POSITION from saved state; broker had 0
shares (child SELL filled during offline gap; reconcile's `floor_ts` bug
filtered it out). Health-check then submitted SL_SELL_100 @ $612.36 on
a phantom position — would have OPENED a short.
**Fix shipped:**
- `_reconcile_position_state` auto-corrects engine to FLAT only in the
  `broker_qty=0, engine_qty>0` direction (the safe direction).
- `_preflight_sell_allowed` reused by every SELL placement site.
- `_fold_engine_to_flat` shared helper.

### 1.5 ⏳ POSITION_MISMATCH does not auto-flatten on persistent breach

**Severity:** 🔴 CRITICAL
**Symptom:** Today's PLTR — `POSITION_MISMATCH IBKR=-70 engine=30` fired
every 30s for 13+ minutes while the user bled. Engine logged the alert
and kept going.
**Why not fixed yet:** Auto-flatten in the broker-SHORT direction
requires deliberate policy — do we issue a covering BUY market order?
Wait for operator? Today's design proposal was: ≥5 consecutive ticks
of dangerous mismatch → auto-issue MARKET cover + CRITICAL alert.
**Next step:** Implement; same shape as 1.4 but for the reverse direction.

### 1.6 ⏳ Live qty change without restart bypasses qty guard

**Severity:** 🟠 HIGH
**Symptom:** `_reconcile_open_orders` runs only at startup. If
`PortfolioLimitsReader` or another path mutates `config.quantity` while
the engine is running, the existing qty guard never re-evaluates resting
SELL legs against the new config.
**Why not fixed yet:** Decision needed — either lock qty to restart-only
or re-validate on every health-check tick.

### 1.7 ⏳ Bracket child qty must equal parent FILLED qty (partial fill)

**Severity:** 🔴 CRITICAL
**Symptom:** ENTRY_BUY_100 partial-fills at 70 shares; bracket child
SELL_100 is still armed at broker. If child fires before parent completes,
broker SELLs 100 against a 70-share position → SHORT 30.
**Why not fixed yet:** Requires modifying child qty on every partial
fill of parent, OR refusing to arm child until parent fully fills.
Today's PLTR `PARTIAL_FILL` events (30→35→100) suggest this hasn't
fired yet in practice but the structural risk exists.

### 1.8 ✅ Defensive `_position_open` check in `_track_position`

**Severity:** 🟡 MEDIUM
**Fix shipped:** Task #25 — engine no longer mutates position fields
if `_position_open=False` despite incoming fill.

### 1.9 ✅ STP-MARKET not STP-LMT for protective stops

**Severity:** 🟠 HIGH
**Symptom:** STP-LMT protective stops can refuse to fill in a gap-down
scenario (price gaps below the limit, order rests unfilled, position
keeps bleeding).
**Fix shipped:** Task #26 — `_place_protective_stop_inner` now places
plain STP (market on trigger), not STP-LMT.

---

## 2. Bracket / Order Lifecycle

### 2.1 ✅ Atomic bracket submission (BUY STP-LMT + child SELL STP)

**Severity:** 🟠 HIGH
**Fixes shipped:** Tasks #1-#11 of the prior hardening cycle.
Bracket parent and child are submitted as a single transmit, child SL
is modified on parent fill, half-filled recovery re-modifies stale child,
`--reset` cancels both legs.

### 2.2 ✅ Engine_id disambiguation by client_id

**Severity:** 🟡 MEDIUM
**Symptom:** Multiple bots could collide on engine_ids when sharing a
single client_id. (We avoid this operationally, but the engine should
be robust.)
**Fix shipped:** Task #12 — engine_id includes client_id suffix.

### 2.3 ✅ Per-cycle `_n{seq}` engine_id suffix

**Severity:** 🟡 MEDIUM
**Symptom:** Same-cycle re-entries could reuse engine_ids across
attempts, colliding with legacy adopted orders.
**Fix shipped:** Task #14 — `ENTRY_BUY_30_PLTR_c8_n1`,
`BR_SELL_30_PLTR_c8_n2`, etc.

### 2.4 ✅ Cancel orphan bracket child before SL fallback

**Severity:** 🟠 HIGH
**Fix shipped:** Task #15 — when bracket flow fails and engine falls
back to legacy SL, any orphan bracket child at broker is cancelled
first.

### 2.5 ✅ `CHILD_STOP_MODIFY` audited unconditionally

**Severity:** 🟡 MEDIUM (observability)
**Fix shipped:** Task #16 — every modify outcome (success/failure)
audits a row, eliminating silent failures.

### 2.6 ✅ Inter-fill gap-fill for `_highest_price`

**Severity:** 🟠 HIGH
**Symptom:** Engine offline during BUY-then-SELL window; replaying both
fills sequentially set `_previous_breakout_level ≈ entry`, leading to
premature re-entry.
**Fix shipped:** Task #13 — between BUY and SELL replays, downloads
historical bars across the gap and sets high to the true window max.

### 2.7 ✅ Reject phantom SELL fills when no position open

**Severity:** 🔴 CRITICAL (engine books)
**Fix shipped:** Task #17 — see also 1.3.

### 2.8 ✅ `_modify_bracket_child` failure preserves original SL

**Severity:** 🟠 HIGH
**Symptom:** When modifying the bracket child stop failed (broker
rejected the modify), engine used to cancel the child — leaving the
position unprotected.
**Fix shipped:** Task #27 — on modify failure, keep original SL,
don't cancel.

### 2.9 ✅ Pre-flight broker-qty check before every SELL placement

**Severity:** 🔴 CRITICAL
**Fix shipped:** Task #22 — every SELL placement now queries broker
position first and refuses if qty mismatch.

### 2.10 ⏳ Cancel-vs-fill race on bracket child

**Severity:** 🟠 HIGH
**Symptom:** Engine sends `cancel(BR_SELL)`. Cancel ack hasn't arrived.
SELL triggers and fills concurrently. Engine state says cancelled but
broker says filled.
**Why not fixed yet:** Requires verifying both `fetch_open_orders` AND
`fetch_positions` after any cancel before treating it as final.

### 2.11 ⏳ Bracket parent OK, child submit fails

**Severity:** 🔴 CRITICAL
**Symptom:** `placeOrder(parent)` succeeded but `placeOrder(child)`
errored (rate-limited, timeout). Parent fills → position naked.
**Why not fixed yet:** Today's design — if child submission fails,
immediately cancel the parent before it can fill.

---

## 3. Restart & Recovery

### 3.1 ✅ Periodic state save loop (1s cadence)

**Severity:** 🔴 CRITICAL
**Symptom:** Any in-memory mutation between event-driven saves was lost
on crash. The ZM double-short bug had exactly this profile.
**Fix shipped:** `_periodic_state_save_loop` runs every 1s, atomic
write (write-temp + rename). Bound on drift = 1s.

### 3.2 ✅ `_active_stop_pct` preserved across restart

**Severity:** 🟡 MEDIUM
**Symptom:** Restarting with a different `--stop-pct` would silently
move the stop for an existing position.
**Fix shipped:** Task #20 — pct used at SL placement time is persisted
and restored.

### 3.3 ✅ `floor_ts` uses saved_ts not max(saved, started)

**Severity:** 🔴 CRITICAL (2026-05-27 META postmortem)
**Symptom:** After a RESTART (not reconnect), `engine_started_at >
saved_ts`, so `max()` filtered out every fill that happened during the
offline gap.
**Fix shipped:** Task #18 — prefer `saved_ts` unconditionally;
`engine_started_at` is fallback for fresh starts only.

### 3.4 ✅ `STARTUP_REFUSED_NAKED` on unknown broker position

**Severity:** 🔴 CRITICAL
**Symptom:** Engine could start while broker held shares this client_id
never opened (manual TWS, different bot, state corruption) — and then
try to manage them.
**Fix shipped:** Hard-abort on startup. No feed subscribed, no
dashboard, no orders. State file untouched so operator can inspect.

### 3.5 ⏳ State file from yesterday — daily counter carryover

**Severity:** 🟠 HIGH
**Symptom:** If `.gt_state_*.json` from yesterday is present and engine
restarts after midnight, `trades_today / wins / losses / daily_loss`
carry over. Daily-loss circuit breaker reads the wrong baseline.
**Why not fixed yet:** Needs a `session_date` stamp on state file, with
load-time reset of daily counters when date != today.

### 3.6 ⏳ Cycle counter restart_epoch

**Severity:** 🟡 MEDIUM
**Symptom:** After `--reset`, cycle counter goes back to 1, which can
collide with stale orders at IBKR named `_c1_n1`.
**Why not fixed yet:** Mostly mitigated by the qty guard (1.2) but
would be cleaner with explicit `restart_epoch` in engine_ids.

---

## 4. Risk & Portfolio Exposure

### 4.1 ✅ Exposure cap $25k → $50k — ghost default in env fallback

**Severity:** 🔴 CRITICAL (silent — caused legitimate re-entries to be
rejected; today's `Combined exposure $35097 > $25000 cap` event)
**Symptom:** `models.py` dataclass default was bumped to 50000 but
`from_env()` env-fallback remained `"25000"`. Without
`GT_MAX_POSITION_VALUE_USD` exported, engine used 25000.
**Fix shipped (this session):**
- `models.py:476` env fallback `"25000"` → `"50000"`
- Aligned `run_live.py` help text + `risk.py` docstring examples
- Left `webapp/backend/ibkr_proxy.py:41` (`MAX_MANUAL_NOTIONAL`) at
  25000 deliberately — separate per-order cap concept.

### 4.2 ✅ Exposure books at fill, not at submission

**Severity:** 🔴 CRITICAL
**Symptom:** A working unfilled BUY for $19k contributed $0 to portfolio
exposure. A second bot could pass its risk check and place another BUY;
both filling would breach the cap.
**Fix shipped (this session):**
- New `pending_notional = qty × limit_price` computed from
  `engine._pending_stop` when side=BUY
- Written to `.gt_live_*.json` snapshot dict
- `PortfolioReader._maybe_refresh` reads `pending_notional` and adds it
  to `notional` so portfolio combined view sees pending immediately

### 4.3 ⚙️ Daily loss limit triggers square-off

**Severity:** 🔴 CRITICAL
**Symptom:** Hitting `max_daily_loss_usd` currently only pauses new
entries — existing positions keep bleeding. MD directive: flatten ALL
positions managed by running bots.
**Status:** Design complete; user paused implementation pending
confirmation. Design summary:
- Probe in `_health_check_loop` (30s cadence)
- Reuses existing `force_exit(qty, reason="DAILY_LOSS_SQUARE_OFF")`
- Idempotent via persistent `_daily_loss_squared_off` flag
- Each bot flattens its own position only — unmanaged IBKR positions
  untouched (smart scoping = "what state files are live")
- No auto-reset across days; `--reset` for next session.

### 4.4 ⏳ Daily loss circuit breaker resets on restart

**Severity:** 🔴 CRITICAL
**Symptom:** If `daily_loss = -$500` and you hit `-$2000` limit, engine
pauses. Restart without `--reset` — does it reload `daily_loss` and
remain paused, or boot with 0?
**Why not fixed yet:** Needs explicit verification that `_load_state`
restores `_paused` flag and daily counters correctly.

### 4.5 ⏳ Atomic portfolio exposure check

**Severity:** 🟠 HIGH
**Symptom:** Multiple bots simultaneously running risk gates can race
on `PortfolioReader._maybe_refresh` — each sees the other's outdated
exposure, both pass, combined exceeds cap.
**Why not fixed yet:** Requires a shared lock file or atomic
read-modify-write on `.gt_portfolio_limits.json`.

---

## 5. Multi-bot / Multi-client coordination

### 5.1 ✅ STARTUP_REFUSED_CONFLICT — cross-client_id detection

**Severity:** 🔴 CRITICAL
**Symptom:** Two engines on the same ticker (different client_ids)
both armed at the same trigger would double-fill on crossing.
**Fix shipped:** Engine queries IBKR open orders at startup; if ANY
order on this symbol belongs to a different client_id, refuses to
start. Verified in today's test session — fired on PLTR with cid=11
because cid=8 had resting orders.

### 5.2 ✅ Same client_id, same ticker — operational rule

**Severity:** N/A (operational; not a code bug)
**Status:** User explicitly enforces "one client_id per ticker."

### 5.3 ⏳ Margin call / IBKR auto-liquidation detection

**Severity:** 🔴 CRITICAL
**Symptom:** IBKR's margin engine could force-liquidate a position
WITHOUT telling the engine. Engine sees position drop to zero, goes
WAITING_REENTRY, re-enters, gets liquidated again → loop.
**Why not fixed yet:** Requires intercepting specific IBKR error
codes from `wrapper.error` and halting ALL engines on the account.

---

## 6. Observability / Alerts / Operations

### 6.1 ✅ Slack → Microsoft Teams migration

**Severity:** 🟡 MEDIUM
**Fix shipped (this session):** New `TeamsChannel` (~280 lines)
using Adaptive Cards via Power Automate webhook. Severity-to-style
mapping. Slack preserved as legacy fallback. Smoke tested 5/5
messages.
**Caveat:** Power Automate webhook can fall silent — observed once
during today's session.

### 6.2 ⏳ Teams webhook outage = operator blind

**Severity:** 🟠 HIGH
**Symptom:** If Power Automate workflow is paused or rate-limited,
alerts pile up then drop. Operator misses the next PLTR-class event.
**Why not fixed yet:** Mitigation = always echo CRITICAL/HIGH to a
local `alerts_critical.log` alongside Teams.

### 6.3 ⏳ Audit log disk fill → state file write fails silently

**Severity:** 🟠 HIGH
**Symptom:** Unbounded audit log growth on EC2 could fill the disk;
state writes start failing silently.
**Why not fixed yet:** Log rotation + IOError-alerting on state save
failure.

### 6.4 ⏳ Manual TWS action contradicts engine

**Severity:** 🟠 HIGH
**Symptom:** Operator cancels a SELL in TWS to "fix" something.
Engine doesn't know. Position becomes naked.
**Why not fixed yet:** Engine should subscribe to order-status updates
for its own orders and alert when one is cancelled without engine
initiating.

### 6.5 ✅ Comprehensive audit logging

**Severity:** 🟡 MEDIUM
**Fix shipped (earlier):** `order.csv`, `state.csv`, `pnl.csv`,
`feed.csv` per-ticker audit feeds. Confirmed instrumental in today's
PLTR / ZM postmortems.

---

## 7. Asset class / Feed

### 7.1 🚫 Forex (EURUSD) support

**Severity:** 🟢 LOW (feature, not bug)
**Symptom:** Engine treats EURUSD as `Stock("EURUSD", "SMART", "USD")`,
IBKR rejects → engine crashes later at `reqMktData` with NoneType.
**Status:** **Won't-fix per user decision today.** Equity engine stays
exactly as is. The diagnostic helper `debug_raw_ticker.py` was
extended to support Forex (read-only, no engine impact).

### 7.2 🚫 Contract qualification caching bug

**Severity:** 🟡 MEDIUM
**Symptom:** Gateway logs "Contract qualified and cached" even when
`reqContractDetails` returned empty → later `reqMktData` crashes on
`None.secType`.
**Status:** Identified but **not fixed** — would affect only
non-equity contracts, and user does not want any changes to the
equity engine right now.

---

## 8. Tooling / Deployment

### 8.1 ✅ Pre-flight check + naked-SELL repro test

**Severity:** 🟡 MEDIUM (operational risk reduction)
**Fix shipped (this session):**
- `scripts/preflight_check.py` — pre-launch verification: code version
  on disk (`STALE_SELL_REJECTED` present), env vars, IBKR reachable,
  state files parse, disk space, git clean+synced
- `scripts/repro_naked_sell.py` — paper-broker reproduction of the
  PLTR -30 scenario, two-phase setup/verify

### 8.2 ✅ Code-deployment gap on EC2 (the "STALE_SELL_REJECTED=0" event)

**Severity:** 🔴 CRITICAL (root cause of today's PLTR)
**Symptom:** Qty-mismatch guard was committed and pushed to `origin/nabi`,
but EC2 had not pulled. PLTR engine ran on old code without the guard,
which is why the -30 short happened.
**Mitigation shipped:** `preflight_check.py` catches this exact scenario
by `grep`-ing the source on disk for the expected guard string before
allowing engine launch.
**Why this is a "resolved" rather than "pending":** No code change can
prevent the operator from skipping the preflight — but the tool exists
and is documented as a pre-launch gate.

### 8.3 ⏳ No formal CI / unit-test harness for guard regressions

**Severity:** 🟠 HIGH
**Symptom:** Every guard added today is verified by manual paper test
or by waiting for the failure mode to occur in live. There's no
automated `pytest` suite mocking the gateway to drive each scenario.
**Why not fixed yet:** Designed in yesterday's report — 15 scenarios
matrix-tested with mocked IBKR. Estimated ~600 lines, runs in <2 sec.
Pending decision to actually build.

---

## Recommended next-step priorities

Ranked by "expected money saved × probability of happening":

| # | Item | Category | Severity |
| --- | --- | --- | --- |
| 1 | Auto-flatten on persistent POSITION_MISMATCH (1.5) | State drift | 🔴 |
| 2 | Daily loss square-off implementation (4.3) | Risk | 🔴 |
| 3 | Bracket child qty = parent FILLED qty (1.7) | Lifecycle | 🔴 |
| 4 | Bracket parent OK + child fail → cancel parent (2.11) | Lifecycle | 🔴 |
| 5 | Margin-call detection + halt-all (5.3) | Multi-bot | 🔴 |
| 6 | State file day-rollover handling (3.5) | Recovery | 🟠 |
| 7 | Atomic portfolio exposure (4.5) | Risk | 🟠 |
| 8 | Unit-test harness for guard regressions (8.3) | Tooling | 🟠 |
| 9 | Audit log rotation + IOError alert (6.3) | Ops | 🟠 |
| 10 | Manual-TWS-action detection (6.4) | Ops | 🟠 |

---

## Verification status

| Resolution | Verified by | Count |
| --- | --- | --- |
| Live audit evidence (alert fired correctly in production) | order.csv / alerts.jsonl | 7 |
| Paper reproduction test | `scripts/repro_naked_sell.py` | 1 |
| Code review + matched against original failure mode | git diff + postmortem | 19 |
| Not yet verified — relies on next live or paper event | — | 0 |

---

## Open questions for senior / MD review

1. **Daily-loss square-off** — confirm scope is only "running bots"
   (current design) versus "all account positions including unmanaged."
2. **POSITION_MISMATCH auto-cover** — okay to auto-issue MARKET BUY to
   cover a broker SHORT? Or require manual?
3. **Day-rollover policy** — auto-reset `daily_loss` / `trades_today`
   at session boundary, or require `--reset`?
4. **Multi-asset support roadmap** — Forex/crypto/futures all touch the
   same code paths. Decision: keep stocks-only indefinitely, or invest
   in a Contract abstraction layer when time permits?

---

_End of inventory._
