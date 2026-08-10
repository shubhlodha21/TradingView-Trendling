━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 18 ·  src/assets/policies/risk_overlay.py
  the second gate — asset-class risk that the universal RiskGate can't see
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  180 lines · 4 dataclasses + 1 Protocol + 1 trivial impl · pure contract file.
  No I/O, no orders, no globals — only types and one always-allow default.
  Each AssetSpec ships its own overlay; RiskGate runs universal checks first,
  then defers here for what notional-only math can't catch (SPAN margin, swap,
  overnight financing, weekend gap, option delta).

要 Require ┊ ..types — Money · Price · Quantity (the units the contract speaks)
          ┊ an AssetSpec (passed by ref inside OrderIntent.spec; never imported)
出 Provides┊ RiskOverlay (Protocol) — the asset-risk interface
          ┊ OrderIntent · PortfolioView · RiskVerdict — the call/return shapes
          ┊ NoExtraRisk — always-pass default for classes the universal gate covers

─── 部  Modules used ──────────────────────────────────────────────────────
   __future__                    ┊ annotations — deferred evaluation, fwd refs
   dataclasses                   ┊ dataclass · field — frozen, slotted records
   typing                        ┊ Any · Literal · Optional · Protocol
                                 ┊ runtime_checkable — structural typing
   ..types                       ┊ Money · Price · Quantity  (→ 流 types)

─── 算  Algorithm · how one order is judged ───────────────────────────────
 Require: an order about to be submitted + a portfolio snapshot
 Ensure : a pure verdict — allow/block + reason — no side effects, no mutation

  1: caller builds OrderIntent(symbol, side, qty, intended_price,
     │                          order_type, spec)        ▷ frozen, slotted
  2:   intent.notional  (property, on demand)            ▷ lazy — not pre-flat
     │      → spec.sizing.notional(qty, intended_price)  ▷ delegates to the
     │                                                      asset's sizing policy
  3: caller builds PortfolioView(...)                    ▷ snapshot at check time
     │      total_open_notional_base · daily_pnl_base
     │      account_equity_base · account_buying_power_base
     │      by_currency = {}                             ▷ FX net-per-ccy, lazy-filled
  4: verdict ← spec.risk_overlay.check(intent, portfolio)▷ THE call (→ 流 spec)
     │      ▷ universal RiskGate has already passed by now (→ 流 risk)
  5:   inside check():                                   ▷ each overlay decides
     │      NoExtraRisk      → RiskVerdict.ok("no asset-specific overlay")
     │      USEquityRisk     → ok (universal gate suffices)      (→ 流 us_stock)
     │      FXRiskOverlay    → exits always ok; entries → weekend-gap block (→ 流 forex)
     │      FuturesRisk      → SPAN-margin headroom + roll window (→ 流 future)
     │      CFDRiskOverlay   → overnight-financing awareness      (→ 流 cfds)
  6:   verdict via constructors:                         ▷ never raw __init__
     │      RiskVerdict.ok(reason, **details)            → allow=True
     │      RiskVerdict.block(reason, circuit_break=?, **details) → allow=False
  7: caller reads verdict.allow:                         ▷ both gates must pass
     │      True  → order proceeds to broker
     │      False → refuse; log verdict.reason to audit; surface to operator
     │      circuit_break → additionally pause entries on this ticker till reset
  8: verdict.details carried into audit / dashboard      ▷ post-mortem metrics

─── 関  Functions / classes defined ───────────────────────────────────────
   class OrderIntent     frozen·slots — symbol·side·qty·intended_price·order_type·spec
     property notional      → spec.sizing.notional(qty, intended_price)
   class PortfolioView    frozen·slots — the at-check portfolio snapshot
   class RiskVerdict      frozen·slots — allow·reason·circuit_break·details
     classmethod ok          → allow=True  verdict (reason, **details)
     classmethod block       → allow=False verdict (reason, circuit_break, **details)
   Protocol RiskOverlay  @runtime_checkable — the asset-risk interface
     check                   (intent, portfolio) → RiskVerdict   [the one method]
   class NoExtraRisk      __slots__=() — always-pass default
     check                   → RiskVerdict.ok("no asset-specific overlay")

─── 変  Variables / state created ─────────────────────────────────────────
   OrderIntent.symbol/side/qty/intended_price/order_type   immutable order shape
   OrderIntent.spec          Any        fwd ref to AssetSpec — avoids circular import
   PortfolioView.by_currency dict        default_factory — per-ccy net (FX swap calc)
   RiskVerdict.allow         bool        proceed / refuse
   RiskVerdict.circuit_break bool        pause entries on ticker until manual reset
   RiskVerdict.details       dict        default_factory — free-form audit metrics
   (no module-level mutable state — this file is a pure contract)

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   spec.sizing.notional(qty, price)     ▷ via OrderIntent.notional property
   RiskVerdict.ok / .block              ▷ self-construction inside overlays
   (no broker, no I/O, no globals — by design; see 注 below)

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   assets.policies.__init__   ▷ re-exports RiskOverlay·RiskVerdict·OrderIntent·PortfolioView
   assets.spec                ▷ AssetSpec.risk_overlay: RiskOverlay; __repr__/to_dict report it
   assets.us_stock            ▷ USEquityRiskOverlay implements check() → ok (universal only)
   assets.forex               ▷ FXRiskOverlay — weekend-gap block on entry
   assets.future              ▷ FuturesRiskOverlay — SPAN margin + roll-window
   assets.cfds                ▷ CFDRiskOverlay — overnight-financing awareness
   strategy.risk (RiskGate)   ▷ runs universal gate, then defers to spec.risk_overlay.check()
   ▷ graph had NO node for this file — edges above are grep-verified, not invented

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Two gates, both must pass.  Universal RiskGate first (size·daily-loss·consec·
     max-trades), then this asset overlay.  Either block → order refused.
   • Overlays are PURE.  Protocol contract: MUST NOT mutate global state, write
     files, or place orders.  State flows in via PortfolioView; decision out via
     RiskVerdict.  Pure decision functions — re-callable, side-effect-free.
   • Structural, not nominal.  @runtime_checkable Protocol — NoExtraRisk and every
     asset overlay satisfy it by shape (a check method), no inheritance.
   • Default is permissive.  NoExtraRisk always allows — equity / FX-cash lean on
     the universal gate alone; richer classes add their own overlay.
   • Snapshot is minimal on purpose.  PortfolioView holds only base-ccy aggregates;
     add fields as new overlays demand — don't pre-compute expensive things.
   • spec is Any.  Forward ref to AssetSpec to dodge a circular import; the overlay
     asks the spec live (multiplier, expiry) instead of pre-flattening attributes.
   • Links:  units → [[types]] · the AssetSpec bundle → [[spec]] · universal gate →
     [[risk]] · per-class overlays → [[us_stock]] [[forex]] [[future]] [[cfds]] ·
     namespace re-export → [[__init__]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
