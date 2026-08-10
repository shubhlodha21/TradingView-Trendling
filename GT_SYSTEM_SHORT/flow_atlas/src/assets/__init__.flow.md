━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 03 ·  src/assets/__init__.py
  the multi-asset façade — one small door the engine walks through
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  56 lines · 0 classes · 0 functions · a pure re-export + side-effect shim.
  Its whole job: keep one tiny public surface so engine.py never says
  `if asset_class == X`.  Importing this package wires up every concrete
  spec by side-effect, then hands back resolve() and the unit constructors.

要 Require ┊ the sibling modules exist & import cleanly (enum, spec,
          ┊ resolver, currency_service, types) and that each concrete
          ┊ spec module self-registers on import
出 Provides┊ a flat namespace: resolve · AssetSpec · AssetClass ·
          ┊ SpecRegistry · CurrencyService · the unit constructors
          ┊ (shares, contracts, base_units, cfd_units, usd, money, price)
          ┊ and the error types — all via __all__

─── 部  Modules used ─────────────────────────────────────────────────────
   .enum                 ┊ AssetClass enum (the taxonomy root)
   .spec                 ┊ AssetSpec — the per-asset policy bundle
   .resolver             ┊ SpecRegistry · resolve · UnknownSymbol ·
                         ┊ SpecMismatchError  (symbol → spec lookup)
   .currency_service     ┊ CurrencyService · StaleRate · NoRateAvailable
   .types                ┊ Currency · Money · Price · Quantity & units +
                         ┊ the constructor functions + mismatch errors
   .us_stock             ┊ side-effect: registers US_EQUITY resolver
   .forex                ┊ side-effect: registers FX_CASH resolver
   .cfds                 ┊ side-effect: INDEX_CFD / SHARE_CFD / FX_CFD
   .future               ┊ side-effect: registers FUTURE resolver

─── 算  Algorithm · what `import src.assets` actually does ─────────────────
 Require: the package directory with all sibling modules present
 Ensure : after import, resolve() knows every asset class & the public
          names in __all__ are bound — engine stays branch-free.

  1: from .enum import AssetClass            ▷ bind the taxonomy enum
  2: from .spec import AssetSpec             ▷ bind the policy-bundle type
  3: from .resolver import SpecRegistry, resolve, …
     │                                       ▷ bind lookup + its errors
  4: from .currency_service import CurrencyService, …
     │                                       ▷ bind FX rate service + errors
  5: from .types import Currency, Quantity, Money, Price, units…
     │                                       ▷ bind value types + constructors
  6: ── side-effect imports (order is load-bearing) ──
  7: from . import us_stock                  ▷ module body runs → SpecRegistry
     │                                          gains the US_EQUITY resolver
  8: from . import forex                     ▷ registers FX_CASH resolver
  9: from . import cfds                      ▷ registers the 3 CFD resolvers
 10: from . import future                    ▷ registers FUTURE resolver
     │   ▷ first-registered wins ambiguous symbols (e.g. "ES" equity-vs-
     │     futures); when FuturesSpec lands in D3 it must import BEFORE
     │     us_stock to claim "ES"
 11: __all__ = [ … ]                         ▷ freeze the public surface
 12: ── done ──  resolve("EURUSD"/"AAPL"/…) now returns the right AssetSpec

─── 関  Functions / classes defined ──────────────────────────────────────
   (none)  ┊ this file defines no functions or classes of its own —
           ┊ it only re-exports names from sibling modules and triggers
           ┊ their import-time registration.

─── 変  Variables / state created ────────────────────────────────────────
   __all__              list[str]   the 21-name public export contract
   (no module-level mutable state — the only "state" created is the
    SpecRegistry population that happens inside the spec modules during
    steps 7–10, not here)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   import-time only — no runtime calls.  Pulls names from:
   .enum · .spec · .resolver · .currency_service · .types
   triggers side-effect import of:
   .us_stock · .forex · .cfds · .future  (each → SpecRegistry.register)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   (graph had no node for this __init__; edges below from grep of src/)
   config.models     ▷ from src.assets import resolve            (:626)
   execution.broker  ▷ from src.assets import resolve            (:27,579)
                     ▷ from src.assets import SpecRegistry, SpecMismatchError (:622)
   feed.handler      ▷ from src.assets import resolve            (:419,457)
   strategy.risk     ▷ from src.assets import resolve            (:239)
   strategy.engine   ▷ from src.assets import resolve            (:340)
   run_live.py       ▷ (reaches submodules src.assets.types / .policies)
   ▷ note: many callers import deeper submodules (.types, .policies.tick,
     .enum, .forex, .us_stock, .cfds, .future) directly — those bypass
     this façade; only `resolve` / `SpecRegistry` come through here.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Small door by design.  Engine should need only `resolve` + `AssetSpec`
     + the type constructors; everything else is plumbing kept off-stage.
   • Side-effect registration.  The four `from . import …` lines exist
     purely so each spec self-registers; the `# noqa: F401` marks them as
     intentional "unused" imports.  Remove one → that asset class silently
     becomes UnknownSymbol.
   • Registration order matters.  Modules listed first register first and
     win ambiguous symbols.  Reorder steps 7–10 only with care.
   • Pure import-time.  No runtime control flow lives here; the real work
     is in the modules it names.
   • Links:  lookup → [[resolver]] · policy bundle → [[spec]] · value
     types → [[types]] · taxonomy → [[enum]] · FX rates → [[currency_service]]
     consumers → [[engine]] · [[broker]] · [[risk]] · [[handler]] · [[models]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
