# ADR-001 — Multi-Asset Adaptability for GT System

| | |
|---|---|
| **Status** | PROPOSED — awaiting sign-off |
| **Date opened** | 2026-06-04 |
| **Last edited** | 2026-06-04 |
| **Authors** | Engineering (GT System) |
| **Required sign-offs** | Operator (Nabi), MD |
| **Supersedes** | — |
| **Branch** | `multi-asset` in `kinshasa_multi_asset/` |
| **Estimated effort** | 4-6 weeks engineering + 3-4 weeks paper validation |

---

## 1. Context

GT System today is a **US-equity** trading engine. Every line of the
12,329 lines across the six core files (`engine.py`, `broker.py`,
`handler.py`, `risk.py`, `run_live.py`, `dashboard_agg.py`) assumes:

- Symbols resolve to `ib_async.Stock(symbol, "SMART", "USD")`
- Prices round to 2 decimals (`round(price, 2)`)
- Quantities are integer **shares**, never fractional, never lots
- Notional = qty × price in USD (no multiplier, no base/quote currency)
- A real `last`-trade price is always present in the feed
- Trading session is US RTH (09:30–16:00 ET), with US holidays
- Commission is per-share (tiered or fixed), USD-denominated
- Daily P&L, daily-loss limit, exposure cap are all in USD
- Account margin is US Reg-T

The Engineering & Trading lead (MD) has requested adaptability to
three additional asset classes:

1. **Currencies** (Forex pairs, e.g. EURUSD, GBPUSD, USDJPY)
2. **CFDs** (index CFDs, share CFDs, FX CFDs — exact subset TBD)
3. **Global equity** (non-US stocks on LSE, TSE, HKEX, ASX, etc.)

The engineering reality is that **equity working does not mean these
are easy** — every one of the assumptions above must be unwound or
parameterised. A naive "just add a contract type" approach would
re-introduce the kind of safety regressions that caused the 2026-06-02
PLTR -30 shorting bug, because every safety guard added in the last
two weeks (27 production-critical guards) was validated against
equity semantics and only equity semantics.

### Hard data informing this decision

Counted from the actual codebase on 2026-06-04:

| Metric | Count |
|---|---|
| Total lines in core trading files | 12,329 |
| Hardcoded `Stock(...)` construction sites | 6 |
| `round(price, 2)` sites | 22 |
| `"USD"` / `"SMART"` hardcoded literals | 17 |
| Per-share commission / sizing math sites | 132 (≈30 are real money-math, rest are display/log) |
| Trading-session / RTH references | 156 |
| `feed.last` price-source reads in strategy | 17 |
| State fields with USD-stock semantics | 22 |
| Dashboard `$` / USD labels | 48 |
| Production-critical guards needing per-class re-validation | 27 |

---

## 2. Decision

### 2.1 Architectural pattern — **additive AssetSpec layer**

We add a new `src/assets/` module containing an `AssetSpec` abstraction
that owns every asset-class-dependent piece of behaviour. Every site in
the engine that currently encodes a US-equity assumption is changed to
delegate to its `AssetSpec`.

**This is additive, not greenfield.** The existing equity code path is
preserved as the implementation of `USStockSpec`. An equity-only run
of the engine after this work must behave byte-identically to today.

```
src/assets/
    __init__.py
    spec.py              # AssetSpec base class (Protocol)
    class_enum.py        # AssetClass enum (US_EQUITY, FX, INDEX_CFD, GLOBAL_EQUITY_LSE, ...)
    resolver.py          # symbol -> AssetSpec
    us_stock.py          # USStockSpec — preserves current behaviour
    forex.py             # ForexSpec
    index_cfd.py         # IndexCFDSpec (subject to CFD eligibility)
    lse_equity.py        # LSEEquitySpec  (Phase 9+)
    tse_equity.py        # TSEEquitySpec  (deferred to follow-up)
    currency_service.py  # base-currency conversion
```

### 2.2 AssetSpec contract (what each implementation must provide)

```python
class AssetSpec(Protocol):
    asset_class:    AssetClass
    currency:       str   # quote currency, e.g. "USD", "EUR", "JPY"
    exchange:       str   # "SMART", "IDEALPRO", "LSE", "TSE", ...
    multiplier:     float # 1 for stocks/FX-cash, 5 for MES, 50 for ES, etc.
    min_qty:        Number  # 1 share / 25_000 EUR base / 1 contract

    def make_contract(self, symbol: str) -> ib_async.Contract:
        """Return the IBKR contract for this symbol."""

    def tick_size(self, price: float) -> float:
        """Min tick increment (NOT decimals). 0.01 for stocks, 0.00005 for EURUSD,
        0.25 for ES, variable per LSE share."""

    def round_to_tick(self, price: float) -> float:
        """Round price to the nearest valid tick."""

    def reference_price(self, feed_snapshot) -> float:
        """The 'current price' for high-water tracking + display.
        Stocks: feed.last. FX: (bid+ask)/2."""

    def buy_compare_price(self, feed_snapshot) -> float:
        """Price to compare against trigger on BUY entries.
        Stocks: feed.last. FX: feed.ask."""

    def sell_compare_price(self, feed_snapshot) -> float:
        """Price to compare against stop on SELL exits.
        Stocks: feed.last. FX: feed.bid."""

    def notional(self, qty: Number, price: float) -> float:
        """Capital outlay in QUOTE currency.
        Stocks: qty × price. Futures: qty × price × multiplier.
        FX: qty × price (base × rate = notional in quote ccy)."""

    def commission(self, qty: Number, price: float, side: str) -> float:
        """Expected commission for this fill, in QUOTE currency.
        Stocks: tiered/fixed per share. FX: per-notional. Futures: per-contract."""

    def session_window(self, date: date) -> list[tuple[datetime, datetime]]:
        """Trading session intervals (UTC) for this date.
        Stocks: [(14:30, 21:00)] M-F. FX: [(00:00, 24:00)] Sun 22:00 -> Fri 22:00.
        LSE: [(08:00, 16:30)] M-F GMT. TSE: [(00:00, 02:30), (03:30, 06:00)] M-F UTC.
        Returns [] on holidays / weekends."""

    def supports_short(self) -> bool:
        """Whether short selling is supported on this asset for this account.
        CFDs: True. Many global equity venues: True with locate. ETC."""
```

### 2.3 The invariant — equity behaviour byte-identical

Every change in every phase must preserve this invariant:

> When the engine runs with an equity symbol (e.g. PLTR, AAPL, GOOG),
> the order traffic, audit log, state file, alert codes, and dashboard
> output must be **byte-identical** to the pre-change behaviour on the
> `nabi` branch.

This is verified by:

1. **Diff testing** — run paper engine on PLTR with the same trigger
   for 1 hour pre- and post-change. Compare `audit/order.csv`,
   `audit/state.csv`, `audit/pnl.csv`. Allow only timestamp delta.
2. **Guard regression** — all 27 production guards still fire on the
   exact same synthetic scenarios with the same audit codes.
3. **State-file shape** — `.gt_state_*.json` field set unchanged for
   equity. New fields may be added but old ones cannot disappear.

Any phase that cannot demonstrate the invariant is **blocked at the
phase gate** until fixed.

---

## 3. Scope

### 3.1 In scope for this ADR

| Asset class | Examples | Priority | Phase introduced |
|---|---|---|---|
| US_EQUITY | PLTR, AAPL, SPY | Existing (preserved) | — |
| FX (IDEALPRO) | EURUSD, GBPUSD, USDJPY | **1st** | Phase 1-6 |
| INDEX_CFD | IBUS500 (S&P CFD), IBDE40 (DAX CFD) | 2nd, **gated on CFD eligibility check** | Phase 5+ |
| GLOBAL_EQUITY_LSE | AZN, BP, HSBA on LSE | 3rd | Phase 6+ |

### 3.2 Explicitly deferred (out of scope for this ADR — separate ADR if needed)

| Asset class | Reason for deferral |
|---|---|
| Futures (ES, MES, GC, CL) | Multiplier semantics need their own ADR; rollover handling is non-trivial |
| Options | Greeks, expiry, strike chain — entirely different state machine |
| Crypto (BTC/ETH via Paxos) | Settlement model and 24/7 hours need bespoke design |
| Bonds | Fixed income calculus is a different engine |
| TSE / HKEX / ASX equity | Add post-LSE once the global-equity pattern is proven |
| NSE / BSE equity (India) | Stub exists in `feed/handler.py:413` but full support deferred |

### 3.3 Conditional scope — CFDs

**IBKR does not offer CFDs to US-domiciled accounts (SEC rule).** Before
Phase 5 we must confirm with the IBKR rep whether this account is
eligible. If not, CFDs drop from scope entirely and we save ~1 week.

Tracked as task #39.

---

## 4. Currency-conversion strategy

### 4.1 Base currency

- Account base currency = **USD** (verified by reading IBKR account
  summary `BaseCurrency` field on startup; abort if mismatch).
- All P&L, daily-loss, exposure cap, dashboard pills converted to base
  before comparison or display.

### 4.2 Conversion source

Two-tier:

1. **Live rate (preferred)** — for active conversions during a trade,
   read the IBKR live FX cross from a small set of always-subscribed
   reference pairs (EURUSD, GBPUSD, USDJPY, etc.). One subscription
   per non-base currency we trade.
2. **Daily reference rate (fallback)** — for historical state-file
   reconstruction and offline calculations, use a daily snapshot of
   IBKR closing rates stored in `.gt_fx_reference_<date>.json`. Cron
   updates daily at 22:00 UTC (Forex close).

### 4.3 P&L attribution

Each cycle's audit row records P&L in **both** native quote currency
**and** account base currency, plus the rate used. Operator can audit
the conversion after the fact.

### 4.4 What we do NOT do (anti-scope)

- We do **not** hedge non-base-currency exposure automatically.
- We do **not** convert mid-cycle (the rate at cycle open and cycle
  close may differ; that's just FX P&L and is accounted for in the
  realised P&L line).
- We do **not** add a separate "FX P&L" decomposition. Mid-cycle FX
  drift is rolled into the trade's realised P&L.

---

## 5. Testing strategy

### 5.1 Three test layers

| Layer | What | Run cadence |
|---|---|---|
| **Unit** | Each `AssetSpec` method, the resolver, `CurrencyService` | On every commit |
| **Integration (mocked gateway)** | All 27 guards × all 4 asset classes = 108 test cases. Synthetic ticks driven through the engine with a mocked IBKR; assert audit + alert + state. | On every commit |
| **Paper-broker** | Live IBKR paper account, 1 week per asset class. | Before each Phase 10 promotion |

### 5.2 The 108-case guard regression matrix

| Guard | US_EQUITY | FX | INDEX_CFD | LSE_EQUITY |
|---|---|---|---|---|
| STALE_SELL_REJECTED (qty mismatch) | ✓ pre-existing | re-test | re-test | re-test |
| PHANTOM_SELL_REJECTED | ✓ pre-existing | re-test | re-test | re-test |
| DUPLICATE_SELL_GUARD_ADOPTED | ✓ pre-existing | re-test | re-test | re-test |
| STARTUP_REFUSED_CONFLICT | ✓ pre-existing | re-test | re-test | re-test |
| STARTUP_REFUSED_NAKED | ✓ pre-existing | re-test | re-test | re-test |
| CUSTOM_ORPHAN_ADOPTED | ✓ pre-existing | re-test | re-test | re-test |
| TRIPWIRE_LOST_PENDING | ✓ pre-existing | re-test | re-test | re-test |
| POSITION_MISMATCH (auto-FLAT direction) | ✓ pre-existing | re-test | re-test | re-test |
| Bracket parent fill → child rearm | ✓ pre-existing | re-test | re-test | re-test |
| Bracket child stop modify | ✓ pre-existing | re-test | re-test | re-test |
| Half-filled bracket recovery | ✓ pre-existing | re-test | re-test | re-test |
| --reset cancels bracket legs | ✓ pre-existing | re-test | re-test | re-test |
| Pending Submit child visibility | ✓ pre-existing | re-test | re-test | re-test |
| Engine_id per client_id | ✓ pre-existing | re-test | re-test | re-test |
| Inter-fill gap-fill highest_price | ✓ pre-existing | re-test | re-test | re-test |
| Per-cycle _n{seq} disambiguation | ✓ pre-existing | re-test | re-test | re-test |
| Cancel orphan child before SL fallback | ✓ pre-existing | re-test | re-test | re-test |
| CHILD_STOP_MODIFY audit | ✓ pre-existing | re-test | re-test | re-test |
| Phantom SELL rejection | ✓ pre-existing | re-test | re-test | re-test |
| floor_ts uses saved_ts | ✓ pre-existing | re-test | re-test | re-test |
| Auto-FLAT engine-LONG/broker-FLAT | ✓ pre-existing | re-test | re-test | re-test |
| Preserve stop_loss_pct across restart | ✓ pre-existing | re-test | re-test | re-test |
| Pre-flight broker-qty check | ✓ pre-existing | re-test | re-test | re-test |
| Reconcile on every health-check | ✓ pre-existing | re-test | re-test | re-test |
| Orphan-cancel verify + MARKET escalate | ✓ pre-existing | re-test | re-test | re-test |
| Defensive _position_open in _track | ✓ pre-existing | re-test | re-test | re-test |
| STP-MARKET (not STP-LMT) protective | ✓ pre-existing | re-test | re-test | re-test |
| _modify_bracket_child failure preserves SL | ✓ pre-existing | re-test | re-test | re-test |

Each cell must result in a documented PASS / FAIL. FAIL = block phase
promotion until fixed.

### 5.3 Per-asset paper validation period

- FX: 1 week minimum
- Index CFD: 1 week minimum (if eligible)
- LSE equity: 2 weeks minimum (overnight risk, currency conversion)

---

## 6. Rollback plan

Multi-asset work happens entirely on the `multi-asset` git branch in
`kinshasa_multi_asset/`. The `nabi` branch in `kinshasa/` is sacred
and untouched.

**Rollback at any phase:**

1. Stop any new-asset bots on EC2 (regular `Ctrl+C`).
2. Revert to `nabi` branch: `cd ~/GT_SYSTEM_Paper && git checkout nabi && git pull`.
3. Restart equity bots — unchanged behaviour, zero risk.

There are **no schema migrations**, **no state-file format changes** for
equity, and **no external resource changes** (Teams webhook URL,
audit log format, dashboard CSV format) that would block a rollback.

The only inputs we add to state files for equity bots are new optional
fields that older code reads as missing-and-default. Forward and
backward compatibility both work.

---

## 7. Phase-by-phase success criteria

| Phase | Deliverable | Acceptance gate |
|---|---|---|
| 0 | This ADR | Operator + MD sign-off |
| 1 | `src/assets/` foundation + USStockSpec preserving equity | Equity audit log on PLTR 1h paper run is byte-identical to pre-change |
| 2 | All 6 contract construction sites delegate to AssetSpec | Same as Phase 1; FX symbols now build correct Forex contracts |
| 3 | All 22 round-to-tick sites delegate to AssetSpec | EURUSD price 1.161715 rounds to 1.16172, PLTR 151.5051 rounds to 151.51 |
| 4 | Price source per asset (last/bid/ask/mid) | Synthetic FX tick stream drives engine; entry / stop fire on correct side |
| 5 | Notional + commission per asset | FX BUY 25_000 EUR at 1.16 reports notional $29k, commission ~$0.20 |
| 6 | Trading session per asset | FX engine runs Sunday evening into Monday; LSE engine respects 08:00 GMT open |
| 7 | Multi-currency P&L | Mixed PLTR + EURUSD portfolio shows correct USD-base total |
| 8 | 108-case guard regression matrix | All 108 cells GREEN |
| 9 | Paper validation per asset | Operator sign-off after 1-2 weeks per asset |
| 10 | Production rollout | Operator daily review for first 2 weeks per asset |

---

## 8. Risks + mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Equity behaviour drifts despite invariant claim | Medium | 🔴 CRITICAL | Diff-test on every phase. Auto-block phase promotion if drift detected. |
| R2 | A safety guard misfires on FX because of price semantics | Medium | 🔴 CRITICAL | 108-case regression matrix at Phase 8. No live trade until all cells pass. |
| R3 | CFD eligibility blocks Phase 5 | High | 🟠 HIGH | Task #39 — confirm with IBKR rep in week 1. If blocked, drop CFD scope. |
| R4 | Currency conversion bug breaks risk-gate math | Medium | 🔴 CRITICAL | CurrencyService gets dedicated unit-test coverage; risk gate gets integration test with mixed-currency scenarios. |
| R5 | Multi-currency P&L wrong in dashboard | High | 🟡 MEDIUM | Show both native + base in dashboard. Operator can spot mismatches. |
| R6 | LSE / TSE session bug stalls re-entry across sessions | Medium | 🟠 HIGH | Session calendar logic gets dedicated test coverage with holiday matrix. |
| R7 | Pending unfilled BUY in non-base currency miscounts exposure | Medium | 🟠 HIGH | Convert at pending-write time using cached rate; risk gate uses pre-converted value. |
| R8 | Effort estimate slips (>50% over) | Medium | 🟡 MEDIUM | Re-estimate at end of each phase; surface to MD if any phase >150% of estimate. |
| R9 | Account-wide leverage misjudged across asset classes (Reg-T stocks + portfolio margin futures) | Medium | 🔴 CRITICAL | Out-of-scope for this ADR — flag for separate ADR before going live mixed. Single-asset-class live trading is the only initial approval. |
| R10 | Buggy multi-asset code accidentally pushed to `nabi` | Low | 🔴 CRITICAL | All work on `multi-asset` branch in `kinshasa_multi_asset/`. Explicit operator gate before any merge to nabi. |

---

## 9. Open questions for MD sign-off

These MUST be answered before Phase 1 starts. Defaults shown but
operator/MD must explicitly confirm.

| # | Question | Default if no answer | Owner |
|---|---|---|---|
| Q1 | Confirm account base currency is USD | USD assumed | Nabi / IBKR account |
| Q2 | CFD eligibility — US-domiciled or not? | Assume blocked; drop CFD scope | IBKR rep |
| Q3 | Asset priority order (which first if we have to drop one) | FX > LSE > CFD | MD |
| Q4 | Acceptable per-phase delivery latency (weekly milestone vs end-of-phase only) | Weekly check-in | MD |
| Q5 | First FX pair to validate against (EURUSD recommended for tight spread + 24/5) | EURUSD | Operator |
| Q6 | First LSE name to validate against (large-cap, tight spread recommended) | AZN or BP | Operator |
| Q7 | Tolerated equity-side test downtime during Phase 1-2 wiring | None — paper continues on `nabi` throughout | MD |
| Q8 | Approval gate for going live on a new asset class — operator or MD? | MD signs off per asset | MD |

---

## 10. Glossary

| Term | Definition |
|---|---|
| **AssetSpec** | The abstraction layer encapsulating per-asset-class behaviour |
| **Asset class** | Stock / Forex / CFD / Future / Option category, distinct from individual symbol |
| **Base currency** | The account's reporting currency (USD here); all P&L converts to this for risk/display |
| **Quote currency** | The currency a price is denominated in (USD for PLTR, USD for EURUSD's price, JPY for USDJPY's price) |
| **Notional** | Capital outlay in quote currency: stocks=qty×price, FX=qty(base)×price(rate), futures=qty×price×multiplier |
| **IDEALPRO** | IBKR's FX exchange routing (quote-driven, no last-trade feed) |
| **Tick size** | Smallest valid price increment for the instrument (0.01 stocks, 0.00005 EURUSD, 0.25 ES) |
| **Multi-asset** | Capability to trade two or more asset classes from the same engine architecture |
| **Greenfield** | Rewrite from scratch (rejected approach) |
| **Additive** | Adding a new layer that doesn't disturb existing code paths (chosen approach) |

---

## 11. Alternatives considered

### Alt-A — Greenfield rewrite

A clean re-architecture with explicit asset-class types from the
ground up. Rejected because:
- Equity is in production live trading; cannot be paused for a rewrite
- The 27 production guards represent two weeks of careful debugging;
  re-deriving them on a new codebase risks losing institutional knowledge
- 2-3 months of effort vs 4-6 weeks for additive

### Alt-B — Fork-per-asset-class

A separate forked codebase per asset class (gt_system_fx, gt_system_lse,
etc.). Rejected because:
- Bug fixes have to be ported manually to each fork → guaranteed drift
- Operator runs N separate dashboards / N separate alert chains
- Risk gate cannot see cross-asset exposure

### Alt-C — Centralised position manager

One process owns all positions across all asset classes; strategy bots
emit signals only. The textbook prop-shop architecture. Rejected as
the *first step* because:
- Touches every part of the system at once — too risky to do in one go
- Worth revisiting after Phase 10 (Q4 2026 timeframe)

### Alt-D — Wait for "Asset" abstraction in ib_async

Some libraries are working on contract-type-agnostic abstractions. Rejected because:
- Timeline unknown (open source)
- We need it now
- Our AssetSpec is broader than just contract construction (it owns
  commission, sizing, session, price-source semantics too)

---

## 12. Sign-off

| Role | Name | Date | Signature / Comment |
|---|---|---|---|
| Operator | Nabi | __________ | __________ |
| MD | __________ | __________ | __________ |
| Engineering | (Claude session) | 2026-06-04 | Initial draft |

**Until both sign-offs are recorded above, this ADR is PROPOSED and no
Phase 1 code is written.**

---

## 13. Change log

| Date | Author | Change |
|---|---|---|
| 2026-06-04 | Engineering | Initial draft |

---

## Appendix A — File-by-file estimated touch points

The "where the work actually happens" map. Each phase touches a subset.

| File | Lines | Touch points | Affected phases |
|---|---:|---:|---|
| `src/strategy/engine.py` | 5,843 | ~50 | 1, 2, 3, 4, 5, 6, 7, 8 |
| `src/execution/broker.py` | 1,855 | ~25 | 2, 3, 5, 8 |
| `src/feed/handler.py` | 731 | ~10 | 2, 4, 8 |
| `src/strategy/risk.py` | 648 | ~20 | 5, 7, 8 |
| `run_live.py` | 1,763 | ~15 | 1, 5, 7 |
| `dashboard_agg.py` | 1,489 | ~12 | 5, 7 |
| `src/config/models.py` | ~700 | ~5 | 1, 5, 7 |
| `src/assets/` (NEW) | ~0 → 1,500 | all new | 1 |
| `tests/test_assets/` (NEW) | ~0 → 800 | all new | 1, 8 |

**Estimated total LOC change**: ~3,500 new + ~1,500 modified =
~5,000 lines net delta.

---

## Appendix B — Pre-existing house-cleaning items surfaced

Items noticed during this scoping that are worth tracking but are
NOT part of this ADR:

1. **`.gt_live_*.json` / `.gt_state_*.json` are not in `.gitignore`** —
   when `kinshasa` was copied to `kinshasa_multi_asset`, these stale
   live-state files came with it. Should be added to `.gitignore` so
   git status stays clean across branches.
2. **CLAUDE.md still references `nabi` branch** — when working in
   `multi-asset` branch, the local memory file gives stale guidance.
3. **`scripts/preflight_check.py` checks `STALE_SELL_REJECTED` count
   on `engine.py`** — when this work modifies engine.py, the count
   may shift; preflight needs an update or a per-asset variant.

These are noted as backlog items; not blocking for Phase 0 sign-off.

---

_End of ADR-001._
