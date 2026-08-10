━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 14 ·  src/assets/policies/commission.py
  the fee-estimate contract — what IBKR will charge for one fill, guessed early
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  58 lines · 1 Protocol (CommissionPolicy) · 1 method (estimate) · 1 type alias.
  A pure interface — no logic, no state, no class instances of its own. Each
  asset class supplies a concrete policy; this file only names the shape they
  must satisfy. Estimate runs at order-PLACEMENT time, before any fill exists.

要 Require ┊ types.Money · types.Price · types.Quantity   (the value vocabulary)
          ┊ a concrete implementor per asset (equity / fx / future / cfd / option)
出 Provides┊ class CommissionPolicy (Protocol, runtime_checkable)
          ┊ type Side = Literal["BUY","SELL"]
          ┊ the .estimate(qty, price, side, venue) → Money contract

─── 部  Modules used ─────────────────────────────────────────────────────
   __future__.annotations        ┊ defer annotation eval (forward refs, PEP 563)
   typing                        ┊ Literal · Protocol · runtime_checkable
   ..types                       ┊ Money · Price · Quantity  (→ 流 types)

─── 算  Algorithm · the estimate contract ────────────────────────────────
 Require: a fill description — qty, price, side, and optional venue.
 Ensure : a Money fee in the asset's QUOTE currency, ~5% of truth, never relied
          on as authoritative; the real fee returns later on the fill.

  1: caller resolves AssetSpec for the symbol        ▷ spec.commission : CommissionPolicy
  2: spec.commission.estimate(qty, price, side, venue="SMART")
     │                                                ▷ structural dispatch — Protocol,
     │                                                  not inheritance; any object with
     │                                                  a matching estimate() qualifies
  3:   concrete policy branches on asset rules        ▷ (lives in sibling files, not here)
     │      equity  → tiered: rate·shares, clamp[min, max·notional]
     │      forex   → 0.20 bps of notional, min $2
     │      future  → flat per-contract + exch + clearing
     │      cfd     → venue-dependent; may fold into spread
     │      option  → per-contract, min $1
  4:   policy may branch on venue                      ▷ PEARL vs ARCA · IDEALPRO vs ARCAFX
     │                                                   fee schedule differs slightly
  5:   return Money (quote ccy)                        ▷ may be Money.zero (promos, crosses);
     │                                                   callers must not assume non-zero
  6: estimate flows to two consumers:
     │      risk gate          ▷ size the trade knowing the expected fee up front
     │      order-ticket UI    ▷ show fee BEFORE the fill lands
  7: later (NOT here) IBKR's commissionReport returns the authoritative fee
     │      ▷ Order.calculate_commission() prefers broker_commission; this estimate
     │        is only the modeled fallback while the report is in flight (→ 流 models)

─── 関  Functions / classes defined ──────────────────────────────────────
   Side = Literal["BUY","SELL"]                        order-side alias
   class CommissionPolicy  (Protocol, runtime_checkable)  the fee-estimate interface
     estimate(qty, price, side, venue="SMART") -> Money   sole method; body is `...`

─── 変  Variables / state created ────────────────────────────────────────
   Side                 type alias   module-level; "BUY" | "SELL"
   (none at runtime)    —            a Protocol holds no state; concrete policies
                                     carry whatever rate/min/max constants they need

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   (none)  ▷ this file declares a contract; it invokes nothing. It only NAMES
            Money / Price / Quantity / Side as the types its method speaks in.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   config.models.Order.calculate_commission()  ▷ spec.commission.estimate(qty, price, side)
                                                  — modeled fallback when IBKR's
                                                    broker_commission hasn't landed yet
   assets.* AssetSpec  ▷ each spec exposes a `.commission : CommissionPolicy` slot
   (graph: no node yet for this Day-1 file — edges above from import + grep call sites)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pure interface.  No implementation here — `estimate` body is `...`. The
     concrete policies live beside this file, one per asset class.
   • Structural typing.  @runtime_checkable Protocol — implementors need not
     subclass; a matching signature is enough (isinstance works at runtime).
   • Quote-currency fees.  estimate returns Money in the asset's QUOTE ccy; FX
     P&L scaling to USD happens downstream, not here  (→ 流 risk).
   • Estimate, not truth.  Within ~5% of reality, used only for pre-fill sizing
     and display. The authoritative number is IBKR's commissionReport, applied
     in Order.calculate_commission()  (→ 流 models).
   • Zero is legal.  Money.zero may come back (promos, internal crosses) — never
     assume the fee is non-zero, and never assume it's non-trivial either.
   • Single-fill scope.  Does NOT track cumulative per-cycle commission; that is
     the engine's `_total_commission` field  (→ 流 engine).
   • Links:  value types → [[types]] · authoritative fee → [[models]] ·
     P&L currency → [[risk]] · cumulative rollup → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
