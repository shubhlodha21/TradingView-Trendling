# Short-Selling Requirements — Implementation Notes (branch `shubh`)

Reference: `IBKR - short selling.pdf`

## Problem / gap

The system already handles the short **order mechanics** (SELL to open,
BUY to cover, inverted PnL — see `SHORT_CONVERSION_CHANGES.md`). What it
does **not** model is the short-selling **requirements and costs** the
PDF describes, and how they differ per asset class:

| PDF section | Factor | Currently modeled? |
|-------------|--------|--------------------|
| §1 | Initial margin (Reg-T equity = 150%: 100% proceeds + 50% equity) | ❌ No |
| §2 | Maintenance margin (variable, stock-specific: 30% → 300%+) | ❌ No |
| §3 | Short-sale proceeds are restricted (held as collateral) | ❌ No |
| §4 | Borrow fee + short-stock interest | ❌ No (only reactive reject backoff) |
| §5 | Portfolio Margin vs Reg-T | ❌ No |
| §6 | Order Preview / whatIf = authoritative requirement | ❌ No |

These do **not** apply uniformly:

- **Equity (Reg-T):** 150% collateral, restricted proceeds, must
  locate/borrow shares, borrow fee, SSR/uptick.
- **CFD:** synthetic — no share borrow, no restricted proceeds. Cost =
  overnight financing; requirement = margin % (leverage).
- **FX / Futures:** symmetric — "shorting" is just being long the other
  side. No borrow, no locate, no restricted proceeds. Requirement =
  leverage margin (FX) / SPAN (futures); cost = swap / carry.

## How each equity-short factor is handled in code

Core principle: **split every factor into (a) what is deterministic and
computable offline → lives in `ShortPolicy`, and (b) what is
IBKR-authoritative and changes daily → must be fetched from the broker
at runtime (whatIf / account values / market data), never hardcoded.**
The PDF's §6 (Order Preview / whatIf) exists precisely because points
2–4 are *not* knowable offline.

| Point | Source of truth | Where in code |
|-------|-----------------|---------------|
| 1. Reg-T initial 50% | Deterministic formula | `RegTEquityShort.initial_margin()` |
| 2. Maintenance (variable) | **IBKR whatIf** `maintMarginChange` | broker `whatif_order_margin()`; policy = conservative fallback |
| 3. Restricted proceeds | IBKR `AvailableFunds`/`ExcessLiquidity` | `proceeds_restricted` flag + trust broker buying power (never `cash += proceeds`) |
| 4. Borrow fee | **IBKR live rate** (`reqMktData`) | `daily_carry(annual_rate=live)` + accrual into PnL |

### 1. Reg-T initial margin (50%) — deterministic → `ShortPolicy`

Fixed formula: 150% collateral = 100% sale proceeds + 50% your equity.
`initial_margin()` returns the 50% own-equity slice (the part that
consumes excess liquidity); the proceeds slice is reported separately as
`proceeds_collateral`.

```python
initial_equity_rate = Decimal("0.50")
def initial_margin(self, notional): return notional * 0.50
```

Pre-trade estimate only — reconcile against whatIf `InitMarginChange`.

### 2. Maintenance margin (variable) — NOT offline → IBKR whatIf

Stock-specific (price/liquidity/volatility/mcap/IBKR risk model); can be
30% or 300%. Hardcoding 30% would silently under-reserve on a volatile
small-cap. So:

- `ShortPolicy` carries only a **conservative fallback** (`maintenance_rate`
  default 30%, `htb_maintenance_rate` 100% when hard-to-borrow).
- The **authoritative** number comes from a whatIf Order Preview on the
  broker: `ib.whatIfOrderAsync(contract, SELL order with whatIf=True)`
  returns `maintMarginChange` (and `initMarginChange`,
  `equityWithLoanAfter`). `whatIf=True` computes but **never places**.
- `ShortRequirement.authoritative` = `True` only when it came from the
  broker.

### 3. Short-sale proceeds are restricted — an accounting rule

The trap: treating the short proceeds as spendable cash. Handling:

- **Never credit proceeds to buying power** in sizing/risk. Source
  buying power from IBKR's `AvailableFunds`/`ExcessLiquidity`
  (`PortfolioView.account_buying_power_base`), which already excludes
  restricted proceeds.
- `ShortRequirement.proceeds_restricted=True` is the guard so no path
  double-counts proceeds as available capacity.
- Pre-trade gate: check whatIf `equityWithLoanAfter` / excess liquidity
  stays positive, rather than assuming proceeds freed capital.

### 4. Borrow fees — dynamic per-stock rate → fetch live, then accrue

Ranges ~0 → triple-digit and changes daily, so it's fetched not stored:

- `ShortPolicy` provides the accrual math:
  `daily_carry = notional × annual_rate / 360`; pass the **live** rate.
- Live rate + shortability come from IBKR market data (`reqMktData`
  shortable / fee-rate). Default in policy is a placeholder for a liquid
  name only.
- Accrue over the holding period and **fold into exit PnL + the
  expected-edge check** (a crowded short can eat the whole breakout edge).

## What is being implemented in this pass

1. `src/assets/policies/short.py` — offline `ShortPolicy` (points 1, 3, 4
   fully; point 2 conservative fallback + flags).
2. Wiring into `AssetSpec` + all 6 factories.
3. `src/execution/broker.py` — `whatif_order_margin()`: the authoritative
   whatIf capability (read-only; `whatIf=True` never places an order).
4. `src/strategy/engine.py` — `get_status()` surfaces a `short_requirement`
   snapshot (offline estimate) + an authoritative whatIf snapshot captured
   best-effort at startup (`preview_short_margin()`), guarded so it can
   never affect order placement.
5. `dashboard.py` — a SHORT REQ panel showing initial/maintenance margin,
   proceeds-restricted, borrow/day, and whether the number is authoritative.
6. `tests/assets/test_short.py`.

**Order placement path is unchanged** — no order is blocked/altered in
this pass. Blocking a short entry when excess liquidity would go negative
is the next step (see Follow-ups).

## Design

Add a **9th AssetSpec policy: `ShortPolicy`**, following the existing
policy-per-asset pattern (each asset factory already composes 8
policies: contract, price, tick, sizing, commission, session,
lifecycle, risk_overlay). This keeps the equity assumption from leaking
a second time.

### New file: `src/assets/policies/short.py`

- `ShortRequirement` — structured pre-trade bundle mirroring IBKR's
  Order Preview (initial margin impact, maintenance margin impact,
  proceeds collateral, daily carry, flags, `to_audit_dict()`).
- `ShortPolicy` — Protocol (pure, stateless, no I/O — same contract as
  other policies).
- `RegTEquityShort` — 50% initial equity (+100% proceeds = 150%),
  30% maintenance default (1.00 if hard-to-borrow), restricted proceeds,
  locate required, borrow fee via annual rate.
- `CFDShort` — margin % (default 20% share / configurable), overnight
  financing carry, no locate, no restricted proceeds.
- `SymmetricShort` — FX & futures: no borrow/locate/restricted proceeds,
  leverage/SPAN margin, swap carry. `for_fx()` / `for_futures()`.

### Wiring

- `src/assets/policies/__init__.py` — export the new symbols.
- `src/assets/spec.py` — add `short: ShortPolicy` field to `AssetSpec`;
  extend `describe()` and `to_audit_dict()`.
- 6 factory sites get a `short=` argument:
  - `us_stock.py` → `RegTEquityShort()`
  - `forex.py` → `SymmetricShort.for_fx()`
  - `future.py` → `SymmetricShort.for_futures()`
  - `cfds.py` (index / share / fx CFD) → `CFDShort(...)`

### Tests

- `tests/assets/test_short.py` — margin math per asset class, the
  150% collateral multiplier, restricted-proceeds flags, carry with
  default vs live rate, `to_audit_dict()` shape.

## Guardrails / scope

- **Additive only.** No edits to the engine order-placement path,
  broker, or run_live in this first pass — real-money-safe.
- These are **conservative offline estimates**. `authoritative=False`
  always: the real requirement is IBKR's whatIf Order Preview (§6),
  which is a planned follow-up (broker `whatIf` call + margin preflight
  before short entry).
- Existing spec tests only assert policy presence (`>=`), so adding a
  9th policy + audit key does not break them.

## Follow-ups (not in this pass)

1. Broker `whatIf` Order Preview call → authoritative InitMargin /
   MaintMargin / ExcessLiquidity before placing a short entry.
2. Risk-gate preflight: block short entry if excess liquidity would go
   negative; surface borrow rate / HTB to the operator.
3. Dashboard: show margin impact + daily carry per open short.
