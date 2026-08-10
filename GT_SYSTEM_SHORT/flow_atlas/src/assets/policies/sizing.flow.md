━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 20 ·  src/assets/policies/sizing.py
  quantity semantics + notional math — one typed answer per asset class
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  106 lines · 1 Protocol (SizingPolicy) · 1 exception (SizingMismatch)
  the route for "qty × price → Money" that used to be hardcoded, unit-blind,
  scattered across risk.py and engine.py. Makes the math TYPED so the PLTR
  -30 "qty + qty" class of bug cannot silently mis-size an order.

要 Require ┊ a Quantity (value + unit) and a Price · an AssetSpec that carries
          ┊ the right concrete SizingPolicy for the symbol being traded
出 Provides┊ Protocol SizingPolicy · exception SizingMismatch
          ┊ notional() in the asset's QUOTE currency (never base-converted here)
          ┊ min_qty · qty_increment · is_valid_qty validation grid

─── 部  Modules used ──────────────────────────────────────────────────────
   __future__                     ┊ annotations — string-lazy type hints
   decimal.Decimal                ┊ exact money math (no float drift)
   typing.Protocol/runtime_checkable┊ structural contract, isinstance-checkable
   assets.types                   ┊ Currency · Money · Price · Quantity · QuantityUnit
                                  ┊ the unit/currency vocabulary all policies speak

─── 算  Algorithm · the sizing contract ───────────────────────────────────
 Require: a Quantity whose .unit matches the policy's expected_unit
 Ensure : a Money in the policy's quote_currency — or a crash, never a
          silent miscompute. Base-currency totals are the CALLER's job.

  1: AssetSpec holds spec.sizing : SizingPolicy   ▷ concrete impl per asset
     │                                               (equity / forex / future / cfd)
  2: caller asks  sizing.notional(qty, price)     ▷ the one hot path
  3:   guard  qty.unit is expected_unit ?         ▷ each impl checks first
     │      no  → raise SizingMismatch(expected, received)
     │              ▷ "engine's AssetSpec ≠ the symbol it's trading"
  4:   yes → compute notional, asset-specific:
     │      equity    qty(SHARES)      × price                 → Money[USD]
     │      ES future qty(CONTRACTS)   × price × 50            → Money[USD]
     │      MES       qty(CONTRACTS)   × price × 5             → Money[USD]
     │      GC        qty(CONTRACTS)   × price × 100           → Money[USD]
     │      EURUSD    qty(BASE_UNITS)  × price                 → Money[USD]
     │      USDJPY    qty(BASE_UNITS)  × 1                     → Money[USD]
     │                  ▷ qty IS already the quote-ccy notional; /price → JPY side
     │      index CFD qty × price × cfd_multiplier             → Money[quote ccy]
  5:   return Money(value, quote_currency)        ▷ QUOTE ccy only — no FX here
     │      ▷ callers needing base totals: Money.to(base, fx) explicitly
  6: ── validation path (risk gate · order-ticket UI) ──
  7: sizing.min_qty()                             ▷ refuse below: 1 share /
     │                                               25k base units / 1 contract
  8: sizing.qty_increment()                       ▷ the grid step (≈1, FX may vary)
  9: sizing.is_valid_qty(qty)                     ▷ default impl combines 3/7/8:
     │      unit is expected_unit ?  no  → False
     │      qty < min_qty()       ?  yes → False
     │      increment.is_zero     ?  yes → True   (free-grained)
     │      (qty − min_qty) % increment == 0 ?    → on-grid verdict

─── 関  Functions / classes defined ───────────────────────────────────────
   class SizingMismatch(TypeError)
     __init__(expected, received)  ▷ stores .expected/.received; human message
                                      points at the AssetSpec↔symbol mismatch
   class SizingPolicy(Protocol)    @runtime_checkable — structural contract
     notional(qty, price) → Money  ▷ capital outlay in quote ccy; raises on unit
     min_qty() → Quantity          ▷ smallest valid order for the asset
     qty_increment() → Quantity    ▷ valid-quantity grid step from min_qty
     is_valid_qty(qty) → bool      ▷ DEFAULT impl (only concrete body in file):
                                      unit + min + on-grid check

─── 変  Variables / state created ─────────────────────────────────────────
   expected_unit   QuantityUnit  protocol attr — the unit this asset accepts
   quote_currency  Currency      protocol attr — the ccy notional() returns
   SizingMismatch.expected  QuantityUnit  what the policy wanted
   SizingMismatch.received  QuantityUnit  what the caller passed
   (stateless otherwise — pure functions; no module-level mutable state)

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   assets.types.Quantity   ┊ .unit · .value · comparison (< min_qty)
   assets.types.QuantityUnit.name      (in the mismatch message)
   Money · Price · Currency            (type contract only; constructed by impls)

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   policies.__init__       ▷ re-exports SizingPolicy (the policy quartet barrel)
   assets.spec             ▷ AssetSpec.sizing : SizingPolicy  (the carried slot)
   assets.us_stock         ▷ USEquity sizing impl  + imports SizingMismatch
   assets.forex            ▷ Forex sizing impl     + imports SizingMismatch
   assets.future           ▷ Future sizing impl    + imports SizingMismatch
   assets.cfds             ▷ CFD sizing impls      + imports SizingMismatch
   assets.policies.risk_overlay ▷ references sizing in the risk-sizing overlay
   strategy.risk           ▷ pre-trade gate uses notional / min_qty for sizing
   ▷ graph node absent for this file — edges recovered from imports + grep

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Quote-currency only.  notional() NEVER does FX. USDJPY returns USD because
     qty(BASE_UNITS) is already the USD quote-side; the JPY side is qty / price.
   • Crash over miscompute.  Wrong unit → SizingMismatch, not a silent product.
     This is the typed answer to the PLTR -30 unit-blind "qty + qty" bug.
   • Protocol, not base class.  @runtime_checkable — impls live in the asset
     modules; only is_valid_qty carries a default body here.
   • Multipliers are asset facts.  ES ×50 · MES ×5 · GC ×100 · share CFD ×1 —
     they live in the concrete impls, not in the engine's hot path.
   • Links:  units → [[types]] · per-asset impls → [[us_stock]] · [[forex]] ·
     [[future]] · [[cfds]] · carried by → [[spec]] · barrel → [[__init__]] ·
     consumed by → [[risk]] · [[risk_overlay]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
