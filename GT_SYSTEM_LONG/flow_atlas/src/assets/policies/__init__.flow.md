━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 13 ·  src/assets/policies/__init__.py
  the policy façade — eight protocols, one import surface for every asset class
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  47 lines · 0 classes · 0 functions · pure re-export. A namespace seam: it
  gathers eight sibling policy modules into one name so concrete asset specs
  write `from .policies import (…)` once. Each protocol owns exactly ONE
  asset-class-dependent behavior; AssetSpec bundles a chosen implementation
  of each. (community: asset-spec / policy-composition spine.)

要 Require ┊ the eight sibling policy modules existing and importable:
          ┊ contract · price · tick · sizing · commission · session
          ┊ lifecycle · risk_overlay  (each defines its Protocol + dataclasses)
出 Provides┊ one flat import surface — 14 names re-exported via __all__
          ┊ 8 Protocols (the policy slots) + 6 supporting data types
          ┊ no logic of its own; never executes, only routes names

─── 部  Modules used ──────────────────────────────────────────────────────
   .contract                     ┊ ContractPolicy                "what IBKR contract am I?"
   .price                        ┊ PricePolicy · FeedSnapshot    "bid? ask? last? mid?"
   .tick                         ┊ TickPolicy · RoundDirection   "smallest price increment?"
   .sizing                       ┊ SizingPolicy                  "(qty,price) → notional?"
   .commission                   ┊ CommissionPolicy              "what fee per fill?"
   .session                      ┊ SessionPolicy · SessionWindow "tradable now? next open/close?"
   .lifecycle                    ┊ LifecyclePolicy               "roll? expire? settle T+N?"
   .risk_overlay                 ┊ RiskOverlay · RiskVerdict      "asset-class risk gates?"
                                 ┊ · OrderIntent · PortfolioView

─── 算  Algorithm · what import-time does ─────────────────────────────────
 Require: the eight sibling modules import cleanly (no cycles, all present)
 Ensure : `from .policies import X` resolves any of 14 names to its real
          definition in the owning sibling module — one seam, no logic.

  1: import .contract                         ▷ bind ContractPolicy
  2: import .price                            ▷ bind PricePolicy, FeedSnapshot
  3: import .tick                             ▷ bind TickPolicy, RoundDirection
  4: import .sizing                           ▷ bind SizingPolicy
  5: import .commission                       ▷ bind CommissionPolicy
  6: import .session                          ▷ bind SessionPolicy, SessionWindow
  7: import .lifecycle                        ▷ bind LifecyclePolicy
  8: import .risk_overlay                     ▷ bind RiskOverlay, RiskVerdict,
     │                                           OrderIntent, PortfolioView
  9: __all__ = […]                            ▷ publish exactly the 14 names;
     │                                           composition order = doc order:
     │                                           contract → price → tick → sizing →
     │                                           commission → session → lifecycle → risk
 10: ── done ──                               ▷ no runtime branches; importers now
     │                                           reach every policy through one name
     │                                           ▷ specs still deep-import sub-symbols
     │                                              (Side, round_to_grid, ContractNotFound,
     │                                              NoUsablePrice, SizingMismatch) directly
     │                                              from the sibling modules — those are
     │                                              NOT re-exported here, by design

─── 関  Functions / classes defined ──────────────────────────────────────
   (none)                          ▷ a re-export module; defines no class, no function

─── 変  Variables / state created ────────────────────────────────────────
   __all__              list[str]   the 14 published names — the export contract
                                    8 Protocols + 6 data types; nothing else leaks

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   (no calls — import statements only)
   re-exports ←  .contract · .price · .tick · .sizing · .commission
                 .session · .lifecycle · .risk_overlay   (8 sibling modules)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   assets.spec        ▷ AssetSpec fields: the 8 Protocols (the bundle's slots)
   assets.us_stock    ▷ all 8 Protocols + FeedSnapshot · RoundDirection ·
                        SessionWindow · OrderIntent · PortfolioView · RiskVerdict
   assets.forex       ▷ FeedSnapshot · RoundDirection · SessionWindow + risk trio
   assets.future      ▷ FeedSnapshot · RoundDirection + OrderIntent/Portfolio/Verdict
   assets.cfds        ▷ FeedSnapshot · RoundDirection + risk trio
   strategy.engine    ▷ deep: FeedSnapshot (line 6205) ; .policies.tick.RoundDirection (978)
   execution.broker   ▷ deep: .policies.tick.RoundDirection (line 29)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pure seam.  No logic, no state but __all__. Editing it cannot change
     behavior — only which names are reachable through the short path.
   • Protocols, not ABCs (PEP 544).  Structural typing — any object with the
     right method signatures satisfies a policy; tests pass tiny in-line
     stand-ins (e.g. NoOpRisk) with no inheritance ceremony.
   • Two import depths coexist.  Specs pull the Protocols + data types from
     here, but reach helpers (Side, round_to_grid) and exceptions
     (ContractNotFound, NoUsablePrice, SizingMismatch) straight from the
     sibling modules. Adding such a symbol to __all__ would be the only way
     to shorten those paths — currently deliberate that they stay deep.
   • Order is the contract.  The 8 listed/exported order is also the order an
     AssetSpec composes its policies (1 contract … 8 risk). Keep them aligned.
   • Graph note.  The knowledge-graph has NO node for this __init__.py (it
     indexes definitions, not re-exports); 被 edges above were recovered by
     grep over real `from .policies import` / `.policies.*` call sites.
   • Links:  bundle → [[spec]] · concretes → [[us_stock]] [[forex]] [[future]]
     [[cfds]] · consumers → [[engine]] [[broker]] · resolver → [[resolver]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
