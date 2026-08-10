# Execution Engine — Testing Roadmap to 99.99% Surety

> **Thesis:** A test is a question made executable. The quality of a test suite is the quality of the questions someone thought to ask. The bugs that blow up accounts are always the failure mode nobody imagined. This roadmap enumerates the questions a production-grade execution engine must answer, mapped to the harness and primitives that already exist.

**System:** `kinshasa_multi_asset` — IBKR multi-asset (FX + US equity) bracket-order engine
**Author:** Engineering · Systematic Trading
**Status legend:** ✅ Done · ⚠️ Partial · ❌ Not started
**Effort legend:** S = small (hours) · M = medium (1–2 days) · L = large (needs new harness component)

---

## How to read this document

Each test is framed as **the question it answers**, because that is the transferable skill. For every question we record:

- **Proves** — what guarantee passing the test buys us
- **Harness** — *Live* (real paper Gateway, tmux fleet) or *Sim* (deterministic MockGateway)
- **Reuses** — the existing primitive it builds on (proves implementability)
- **Effort** — S / M / L
- **Priority** — P0 (account-killer) · P1 (high) · P2 (hardening)

### The two harnesses

| Harness | What it is | Best for |
| --- | --- | --- |
| **Live chaos** (`tests/paper/chaos_test.py`) | Real paper Gateway, 32-bot tmux fleet, sidecar connection | process / connection / state / position faults |
| **MockGateway sim** (`tests/harness/`) | Deterministic in-memory broker, no live connection | market conditions, fill-event races, generative fuzzing |

Both already exist. Market-condition tests are physically impossible on a live paper account (you cannot make a stock halt on command) — which is precisely why the simulation harness was built. The two are complementary, not redundant.

---

## Coverage summary

| Tier | Theme | Tests | Done | Partial | Missing |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | Connectivity & session | 8 | 1 | 1 | 6 |
| 2 | Order lifecycle | 10 | 4 | 2 | 4 |
| 3 | Market-condition faults | 8 | 0 | 1 | 7 |
| 4 | State & persistence | 7 | 1 | 0 | 6 |
| 5 | Position & reconciliation | 5 | 3 | 2 | 0 |
| 6 | Risk & capital | 5 | 2 | 0 | 3 |
| 7 | The 99.99% tier | 7 | 0 | 3 | 4 |
| **Total** | | **50** | **11** | **9** | **30** |

---

## Tier 1 — Connectivity & Session Faults

| # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | TWS dies mid-cycle and returns? | Reconnect + reconcile | Live | `pkill` + respawn | — | P0 | ✅ |
| 2 | Socket half-opens — TCP stalls but never closes? | Heartbeat/timeout detection, not just disconnect-event | Live | `docker pause` freezes container | S | P0 | ❌ |
| 3 | Market-data farm down but order farm up? | Engine doesn't act blind, doesn't false-flatten | Sim | model two farm states in MockGateway | M | P1 | ❌ |
| 4 | 32 bots reconnect in the same 50 ms window? | Rate-limit (Error 326) survival | Live | reconnect stagger (A41) | — | P1 | ⚠️ |
| 5 | Broker forces its daily 17:00-ET restart under us? | Scheduled-downtime recovery | Live | `pkill`+respawn on schedule | S | P1 | ❌ |
| 6 | Network latency spikes to 5 s but doesn't drop? | Slow-path correctness, timeout tuning | Live | `pfctl` / `tc netem` | S | P1 | ❌ |
| 7 | Client clock drifts from broker clock? | Timestamp logic survives skew (fill floors, A18) | Sim | `libfaketime` + tick_clock | M | P2 | ❌ |
| 8 | Reconnect succeeds with a stale nextValidId? | Order-id collision avoidance | Sim | MockGateway id allocator | M | P2 | ❌ |

---

## Tier 2 — Order Lifecycle

| # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 9 | Partial fills in many tiny tranches? | Child-stop retarget per partial | Live | bracket lifecycle | — | P0 | ✅ |
| 10 | Fill arrives during the disconnect window? | Execution replay (A19) | Live | `reqExecutionsAsync` replay | — | P0 | ✅ |
| 11 | Fill arrives after cancel was sent (cancel/fill race)? | No phantom state, correct winner | Sim | `force_cancel` + `_execute_fill` timing | M | P0 | ❌ |
| 12 | Broker sends the same fill event twice? | Idempotent fill handling (dedup by execId) | Sim | call `_execute_fill` twice | S | P0 | ❌ |
| 13 | Fill events arrive out of order? | Sequence-independent accounting | Sim | reorder `_execute_fill` calls | S | P1 | ❌ |
| 14 | Order rejected after submit (margin/halt/bad price)? | Reject path, no stuck state | Live | sidecar bad-price order + `inject_next_rejection` | S | P1 | ⚠️ |
| 15 | Modify rejected by Error 201 (after-trigger)? | Suppression (A65/A68) | Live | — | — | P0 | ✅ |
| 16 | Order stuck in PendingSubmit forever? | Stuck-order timeout + escalation | Sim | hold the ack in MockGateway | M | P1 | ❌ |
| 17 | Order acknowledged but never fills (dead order)? | Staleness detection | Live | far-off limit + timeout | M | P2 | ❌ |
| 18 | Parent fills but child placement fails? | Orphan-parent cleanup (A40) | Live | — | — | P0 | ✅ |

---

## Tier 3 — Market-Condition Faults (largest gap)

> All of these run in the **Sim** harness via the universal `feed_tick(bid, ask, last)` primitive. They are impossible to summon on a live paper account.

| # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 19 | LTP freezes — does the engine act on a dead price? | Stale-quote guard | Sim | stop calling `feed_tick` | S | P0 | ❌ |
| 20 | Price gaps past the stop in one tick? | Slippage handling, MARKET fallback | Sim | `feed_tick` with jump | S | P0 | ⚠️ |
| 21 | Crossed / locked market (bid > ask)? | Bad-book rejection | Sim | `feed_tick(bid>ask)` | S | P1 | ❌ |
| 22 | A bad tick arrives (0, negative, 10× spike)? | Tick sanity filter | Sim | `feed_tick` + `_validate_price` | S | P0 | ❌ |
| 23 | Stock halted / LULD while we hold a position? | Halt survival, no blind re-entry | Sim | pause ticks + reject orders | M | P0 | ❌ |
| 24 | RTH open/close boundary crosses mid-cycle? | Session-aware order routing | Sim | `tick_clock` across boundary | M | P1 | ❌ |
| 25 | Spread blows out 50×? | Spread-aware entry suppression | Sim | `feed_tick` wide | S | P1 | ❌ |
| 26 | Tick storm — 10k updates/sec? | Engine keeps up, no OOM | Sim | `feed_tick` tight loop | S | P2 | ❌ |

---

## Tier 4 — State & Persistence

| # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 27 | State file is garbage JSON? | Loud fail, no silent corruption | Live | — | — | P0 | ✅ |
| 28 | Power loss mid-write → torn / half-written file? | Atomic write (temp + rename) | Live | extends `state-corrupt` | S | P0 | ❌ |
| 29 | State file from an older schema version? | Migration / version guard | Live | write old-format file | S | P1 | ❌ |
| 30 | Disk full during state save? | Save-failure handling, doesn't trade blind | Live | quota / mock write raise | M | P1 | ❌ |
| 31 | State day-rollover (date boundary)? | Audit-path correctness | Live | `faketime` / date set | S | P2 | ❌ |
| 32 | State says LONG, broker says FLAT (and inverse)? | Broker-truth wins (A19) | Live | — | — | P0 | ✅ |
| 33 | Two processes write the same state file? | Single-writer guarantee / lock | Live | spawn 2 bots same clientId | S | P1 | ❌ |

---

## Tier 5 — Position & Reconciliation

| # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 34 | Orphan position at broker engine never ordered? | Adopt-or-refuse (A53/A54) | Live | — | — | P0 | ✅ |
| 35 | A manual trade moves the position under us? | Cross-source drift detection | Live | sidecar places order mid-run | S | P1 | ⚠️ |
| 36 | Engine qty ≠ broker qty by a few shares? | Quantity reconciliation | Live | sidecar partial close | S | P1 | ⚠️ |
| 37 | FX position truth via cash ledger not positions()? | A42 / A43 | Live | — | — | P0 | ✅ |
| 38 | Position goes negative (accidental short)? | Short-guard / invariant kill | Live | — | — | P0 | ✅ |

---

## Tier 6 — Risk & Capital

| # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 39 | Margin call / insufficient buying power mid-fleet? | Halt-all, no doubling down | Sim | set `get_equity` low | M | P0 | ❌ |
| 40 | Daily loss limit breached? | Square-off + stop | Sim | inject losses | M | P0 | ❌ |
| 41 | 32 bots place entries simultaneously exceeding capital? | Atomic portfolio-level exposure cap | Live | spawn-all-at-once | S | P1 | ❌ |
| 42 | JPY / cross-currency notional miscount? | A24 | Live | — | — | P1 | ✅ |
| 43 | Exposure tracker drifts after exits? | A25 | Live | — | — | P1 | ✅ |

---

## Tier 7 — The 99.99% Tier (compound · generative · adversarial)

> This tier buys the last 0.4%. Hand-written tests plateau around 99.5%; the failure modes here are invisible in a single run and require composition, randomization, or thousands of iterations.

| # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 44 | Two faults at once — disconnect *during* a partial fill *during* a state save? | Compound-failure survival (where real incidents live) | Both | compose existing primitives | M | P0 | ❌ |
| 45 | 100k randomized scenarios from a state machine (Hypothesis)? | Finds races no human would enumerate | Sim | scenario + invariant framework (A5/A6) | L | P0 | ❌ |
| 46 | Invariant `engine == broker` asserted after *every* event in *every* test? | Turns silent drift into instant loud failure | Both | `tests/harness/invariant.py` DSL | S | P0 | ⚠️ |
| 47 | Replay of historical incident days (the NVDA $300 day)? | Regression against real past failures | Sim | feed recorded ticks via `feed_tick` | M | P1 | ❌ |
| 48 | Chaos monkey — random fault at random time, thousands of runs? | Statistical confidence, not anecdotal | Live | randomize injection timing | M | P1 | ⚠️ |
| 49 | 72 h continuous soak — watch RSS for leaks? | No slow degradation over days | Live | rolling-kill scenario | — | P1 | ⚠️ |
| 50 | Deliberately break a guard — does a test *catch* it? | Meta-test: the tests actually test | Both | mutation of guards (A10) | M | P2 | ⚠️ |

---

## Prioritization — recommended order of work

Ranked by `P(failure) × cost(failure)`, not by ease of implementation.

| Order | Tests | Rationale |
| --- | --- | --- |
| **1** | #19 stale-data guard, #22 bad-tick filter | Acting on a frozen or garbage price is the #1 way live systems lose money. Pure account-killers, both effort-S in sim. |
| **2** | #46 invariant-everywhere | Assert `engine == broker` after every event. Converts silent drift into loud failure across the whole suite. Cheap, enormous payoff. |
| **3** | #11–13 fill-event races, #44 compound faults | The races that single-run tests can never find. Foundation for the generative campaign. |
| **4** | #45 generative fuzzing (Hypothesis) | The single highest-leverage item for the last 0.4%. Requires building the stateful fuzzer on top of the existing scenario framework. |
| **5** | #23 halt/LULD, #5 daily-restart, #39/#40 margin & daily-loss | Real, scheduled, will-definitely-happen events plus the financial circuit breakers. |

---

## The three meta-questions to ask of this suite

1. **Which failures are silent?** Rank every mode by whether it throws or quietly misbehaves. Silent failures (stale price, missed fill-dedup) are the account-killers — test them first regardless of tier.
2. **Which need 10,000 runs, not one?** Races (#11, #13, #44) are invisible in a single execution. They *require* the generative campaign — hand-written tests will never surface them.
3. **What is the blast radius if this one fails live?** 32 bots × $1M notional on a stale-data bug is catastrophic; a cosmetic alert bug is annoying. Prioritize by expected cost.

---

## Appendix — existing coverage (A-series defenses already shipped)

The following production defenses are already validated by the four live chaos scenarios (`tws-disconnect`, `state-corrupt`, `restart-positions`, `rolling-kill`):

A19 missed-fill replay · A37/A39 entry-placement race · A40/A57 orphan bracket cleanup · A41 reconnect stagger · A42/A43 FX position truth · A45/A48 invariant sweep · A52 bracket-lifecycle instrumentation · A54/A58 dual-leg adoption · A65/A68 Error-201 suppression · A75 ingest-gap race fix.

---

## Master table — database-ready (all 50 tests)

> **How to use in Notion:** Create a new database, then copy the table below and paste it in — Notion maps each column to a property. Set these column types after paste: **Tier** → Select · **Harness** → Select · **Effort** → Select (S/M/L) · **Priority** → Select (P0/P1/P2) · **Status** → Select (Done/Partial/Missing). Then build views: *Backlog* (Status ≠ Done, sort Priority), *P0 Gaps* (Priority = P0 AND Status ≠ Done), *Quick Wins* (Effort = S AND Status = Missing).

| Tier | # | Question | Proves | Harness | Reuses | Effort | Priority | Status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Connectivity | 1 | TWS dies mid-cycle and returns? | Reconnect + reconcile | Live | pkill + respawn | — | P0 | Done |
| 1 Connectivity | 2 | Socket half-opens (TCP stalls, never closes)? | Heartbeat/timeout detection | Live | docker pause | S | P0 | Missing |
| 1 Connectivity | 3 | Market-data farm down, order farm up? | No blind action, no false-flatten | Sim | two-farm model | M | P1 | Missing |
| 1 Connectivity | 4 | 32 bots reconnect in same 50ms window? | Rate-limit 326 survival | Live | reconnect stagger A41 | — | P1 | Partial |
| 1 Connectivity | 5 | Broker forces daily 17:00-ET restart? | Scheduled-downtime recovery | Live | pkill+respawn schedule | S | P1 | Missing |
| 1 Connectivity | 6 | Latency spikes to 5s but no drop? | Slow-path correctness | Live | pfctl / tc netem | S | P1 | Missing |
| 1 Connectivity | 7 | Client clock drifts from broker clock? | Timestamp logic survives skew | Sim | libfaketime + tick_clock | M | P2 | Missing |
| 1 Connectivity | 8 | Reconnect with stale nextValidId? | Order-id collision avoidance | Sim | MockGateway id allocator | M | P2 | Missing |
| 2 Order lifecycle | 9 | Partial fills in many tiny tranches? | Child-stop retarget per partial | Live | bracket lifecycle | — | P0 | Done |
| 2 Order lifecycle | 10 | Fill arrives during disconnect window? | Execution replay A19 | Live | reqExecutionsAsync | — | P0 | Done |
| 2 Order lifecycle | 11 | Fill arrives after cancel sent (race)? | Correct winner, no phantom | Sim | force_cancel + _execute_fill | M | P0 | Missing |
| 2 Order lifecycle | 12 | Broker sends same fill twice? | Idempotent fill dedup by execId | Sim | call _execute_fill twice | S | P0 | Missing |
| 2 Order lifecycle | 13 | Fill events arrive out of order? | Sequence-independent accounting | Sim | reorder _execute_fill | S | P1 | Missing |
| 2 Order lifecycle | 14 | Order rejected after submit? | Reject path, no stuck state | Live | inject_next_rejection | S | P1 | Partial |
| 2 Order lifecycle | 15 | Modify rejected by Error 201? | Suppression A65/A68 | Live | — | — | P0 | Done |
| 2 Order lifecycle | 16 | Order stuck in PendingSubmit forever? | Stuck-order timeout + escalation | Sim | hold the ack | M | P1 | Missing |
| 2 Order lifecycle | 17 | Order acked but never fills (dead)? | Staleness detection | Live | far limit + timeout | M | P2 | Missing |
| 2 Order lifecycle | 18 | Parent fills but child placement fails? | Orphan-parent cleanup A40 | Live | — | — | P0 | Done |
| 3 Market conditions | 19 | LTP freezes — engine acts on dead price? | Stale-quote guard | Sim | stop feed_tick | S | P0 | Missing |
| 3 Market conditions | 20 | Price gaps past the stop in one tick? | Slippage handling, MARKET fallback | Sim | feed_tick jump | S | P0 | Partial |
| 3 Market conditions | 21 | Crossed/locked market (bid > ask)? | Bad-book rejection | Sim | feed_tick bid>ask | S | P1 | Missing |
| 3 Market conditions | 22 | Bad tick (0, negative, 10x spike)? | Tick sanity filter | Sim | feed_tick + _validate_price | S | P0 | Missing |
| 3 Market conditions | 23 | Stock halted/LULD while holding? | Halt survival, no blind re-entry | Sim | pause ticks + reject | M | P0 | Missing |
| 3 Market conditions | 24 | RTH open/close boundary mid-cycle? | Session-aware routing | Sim | tick_clock boundary | M | P1 | Missing |
| 3 Market conditions | 25 | Spread blows out 50x? | Spread-aware entry suppression | Sim | feed_tick wide | S | P1 | Missing |
| 3 Market conditions | 26 | Tick storm — 10k updates/sec? | Engine keeps up, no OOM | Sim | feed_tick tight loop | S | P2 | Missing |
| 4 State | 27 | State file is garbage JSON? | Loud fail, no silent corruption | Live | — | — | P0 | Done |
| 4 State | 28 | Power loss mid-write (torn file)? | Atomic write temp+rename | Live | extends state-corrupt | S | P0 | Missing |
| 4 State | 29 | State file from older schema version? | Migration / version guard | Live | write old-format | S | P1 | Missing |
| 4 State | 30 | Disk full during state save? | Save-failure handling | Live | quota / mock raise | M | P1 | Missing |
| 4 State | 31 | State day-rollover (date boundary)? | Audit-path correctness | Live | faketime / date set | S | P2 | Missing |
| 4 State | 32 | State LONG, broker FLAT (and inverse)? | Broker-truth wins A19 | Live | — | — | P0 | Done |
| 4 State | 33 | Two processes write same state file? | Single-writer guarantee | Live | 2 bots same clientId | S | P1 | Missing |
| 5 Position recon | 34 | Orphan position engine never ordered? | Adopt-or-refuse A53/A54 | Live | — | — | P0 | Done |
| 5 Position recon | 35 | Manual trade moves position under us? | Cross-source drift detection | Live | sidecar order mid-run | S | P1 | Partial |
| 5 Position recon | 36 | Engine qty != broker qty by a few? | Quantity reconciliation | Live | sidecar partial close | S | P1 | Partial |
| 5 Position recon | 37 | FX position truth via cash ledger? | A42/A43 | Live | — | — | P0 | Done |
| 5 Position recon | 38 | Position goes negative (short)? | Short-guard / invariant kill | Live | — | — | P0 | Done |
| 6 Risk | 39 | Margin call mid-fleet? | Halt-all, no doubling down | Sim | set get_equity low | M | P0 | Missing |
| 6 Risk | 40 | Daily loss limit breached? | Square-off + stop | Sim | inject losses | M | P0 | Missing |
| 6 Risk | 41 | 32 bots enter simultaneously over capital? | Atomic portfolio exposure cap | Live | spawn-all-at-once | S | P1 | Missing |
| 6 Risk | 42 | JPY/cross-currency notional miscount? | A24 | Live | — | — | P1 | Done |
| 6 Risk | 43 | Exposure tracker drifts after exits? | A25 | Live | — | — | P1 | Done |
| 7 99.99% tier | 44 | Two faults at once (disconnect+partial+save)? | Compound-failure survival | Both | compose primitives | M | P0 | Missing |
| 7 99.99% tier | 45 | 100k randomized scenarios (Hypothesis)? | Finds unimaginable races | Sim | scenario+invariant A5/A6 | L | P0 | Missing |
| 7 99.99% tier | 46 | Invariant engine==broker after every event? | Instant loud failure on drift | Both | invariant.py DSL | S | P0 | Partial |
| 7 99.99% tier | 47 | Replay historical incident days? | Regression vs past failures | Sim | feed recorded ticks | M | P1 | Missing |
| 7 99.99% tier | 48 | Chaos monkey — random fault, 1000s runs? | Statistical confidence | Live | randomize injection | M | P1 | Partial |
| 7 99.99% tier | 49 | 72h continuous soak, watch RSS? | No slow degradation | Live | rolling-kill | — | P1 | Partial |
| 7 99.99% tier | 50 | Break a guard — does a test catch it? | Meta-test: tests actually test | Both | guard mutation A10 | M | P2 | Partial |

---

*End of roadmap.*
