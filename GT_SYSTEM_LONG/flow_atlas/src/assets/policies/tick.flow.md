━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 21 ·  src/assets/policies/tick.py
  the tick-grid — every order price snapped to a broker-legal increment
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  125 lines · 1 Enum · 1 Protocol · 1 free function · zero state.
  The smallest load-bearing file in the asset layer: it replaces the 22
  scattered `round(price, 2)` sites — a US-equity assumption that silently
  rejects FX / futures orders — with one grid-snapping primitive every
  per-asset TickPolicy reuses.

要 Require ┊ a Price (Decimal-backed) and a positive Decimal tick_size
          ┊ nothing else — pure math, no broker, no I/O, no clock
出 Provides┊ RoundDirection (NEAREST · DOWN · UP)
          ┊ TickPolicy (Protocol: tick_size · round_to_tick · decimals_for_display)
          ┊ round_to_grid(price, tick_size, direction) — the shared rounder

─── 部  Modules used ──────────────────────────────────────────────────────
   decimal                       ┊ Decimal · ROUND_DOWN · ROUND_UP · ROUND_HALF_EVEN
   enum                          ┊ Enum — the three rounding directions
   typing                        ┊ Protocol · runtime_checkable — structural contract
   ..types                       ┊ Price — the Decimal-backed money type (→ 流 types)

─── 算  Algorithm · snap a raw price onto the grid ────────────────────────
 Require: Price price, Decimal tick_size > 0, RoundDirection direction
 Ensure : the returned Price is an EXACT integer multiple of tick_size —
          never off-grid, so the broker accepts at order time, not fill time.

  1: a policy computes a raw price          ▷ e.g. entry × (1 − SL%) = 151.713
     │                                         — float-valid, grid-INVALID
  2: policy.round_to_tick(price, dir)       ▷ each asset's tiny method:
     │      DecimalTickPolicy  → round_to_grid(price, tick_size(price), dir)
     │      PipTickPolicy      → round_to_grid(price, self.tick,  dir)   ▷ FX
     │      FuturesTickPolicy  → round_to_grid(price, self.grain, dir)   ▷ CME
     │      FixedGrainTickPolicy→ round_to_grid(price, self.grain, dir)  ▷ CFD
  3: round_to_grid(price, tick_size, dir)   ▷ THE shared math — one body for all
  4:   if tick_size <= 0: raise ValueError  ▷ guard — a zero grid has no lines
  5:   n_ticks ← price / tick_size          ▷ how many ticks from zero (Decimal)
  6:   branch on direction:                 ▷ quantize n_ticks to a whole count
     │      NEAREST → quantize("1", ROUND_HALF_EVEN)  ▷ banker's tie-break (numpy-like)
     │      DOWN    → quantize("1", ROUND_DOWN)        ▷ floor — BUY limit, don't cross offer
     │      UP      → quantize("1", ROUND_UP)          ▷ ceil  — SELL limit, don't cross bid
     │      else    → raise ValueError                 ▷ unknown direction
  7:   return Price(rounded × tick_size)    ▷ whole tick-count × grain = on-grid Price
     │                                         151.713 →6.25  ticks → 6 → 151.71 (eq)
     │                                         4500.30 →18001 ×0.25 → 4500.25 (ES, NEAREST)
     │                                         4500.30 → ↑ UP        → 4500.50 (ES, UP)
     │                                         1.161725 →0.00005 grid → 1.16170 (EURUSD)
  8: caller (broker / policy) places the order at an exactly legal price.

─── 関  Functions / classes defined ───────────────────────────────────────
   class RoundDirection(Enum)             ▷ NEAREST · DOWN · UP — tie / bias policy
   class TickPolicy(Protocol)             ▷ @runtime_checkable structural contract
     tick_size(price) → Decimal           ▷ min increment AT this price (tiered-aware)
     round_to_tick(price, direction)→Price▷ snap to grid; the order-safety invariant
     decimals_for_display(price) → int    ▷ UI/audit format only; NOT order math
   def round_to_grid(price, tick_size, direction) → Price
                                          ▷ the one rounding body all policies share

─── 変  Variables / state created ─────────────────────────────────────────
   RoundDirection.NEAREST  "NEAREST"  display · mid prices (banker's rounding)
   RoundDirection.DOWN     "DOWN"     BUY limits — floor, never bid through offer
   RoundDirection.UP       "UP"       SELL limits — ceil, never ask below bid
   (module holds NO mutable state — TickPolicy impls live in sibling asset files)

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   Decimal.__truediv__ · Decimal.quantize          ▷ the grid arithmetic
   Price(...)            (← 流 types)                ▷ wrap result back to money type
   (no broker, no ledger, no alerts — leaf node of the call graph)

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   assets.us_stock      DecimalTickPolicy.round_to_tick      → round_to_grid  ▷ eq 0.01
   assets.forex         PipTickPolicy.round_to_tick          → round_to_grid  ▷ FX 5e-5/JPY
   assets.future        FuturesTickPolicy.round_to_tick      → round_to_grid  ▷ ES/GC grain
   assets.cfds          FixedGrainTickPolicy.round_to_tick   → round_to_grid  ▷ CFD grain
   assets.spec          AssetSpec.tick : TickPolicy          ▷ holds the policy slot
   assets.policies.__init__  re-exports TickPolicy · RoundDirection
   execution.broker     imports RoundDirection · spec.tick.round_to_tick / .tick_size
   assets.resolver      spec.tick.tick_size vs contractDetails.minTick  ▷ cross-validate
   (graph note: this file is NOT yet in the code-review-graph; edges above are
    from grep of real import / call sites, not invented.)

─── 注  Notes · invariants ────────────────────────────────────────────────
   • On-grid or rejected.  round_to_tick MUST return an exact multiple of
     tick_size. An off-grid price (ES 4500.30) is valid float math but an
     INVALID order — IBKR rejects it at FILL time, not order time, which is
     the confusing failure this file exists to prevent.
   • "decimals" is the wrong question.  The right one is "is price an integer
     multiple of tick_size?" — so the grain, not the dp count, is the truth.
   • Direction is sided.  DOWN for BUY limits, UP for SELL limits, AWAY_FROM_REF
     for protective stops (extra breathing room). NEAREST only for display/mids.
   • Decimal end-to-end.  tick_size returns Decimal so it composes with Price;
     no float drift creeps into the grid count.
   • Tiered ticks.  tick_size(price) takes the price so LSE pence grains /
     options chains that change increment by level work without a new method.
   • Pure leaf.  No state, no I/O — trivially testable; every asset policy is
     thin because the math lives here once.
   • Links:  the money type → [[types]] · the policy slot → [[spec]]
     equity grid → [[us_stock]] · FX pips → [[forex]] · CME → [[future]]
     CFD grains → [[cfds]] · minTick cross-check → [[resolver]] · order use → [[broker]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
