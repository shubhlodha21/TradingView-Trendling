━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 09 ·  src/assets/resolver.py
  symbol → AssetSpec — first-match resolution + IBKR cross-validation gate
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  366 lines · 1 class (SpecRegistry) · 2 errors · 1 module fn · the front door
  to the multi-asset layer. Concrete spec modules register here on import; the
  engine asks only "what is PLTR?" and never names an asset class itself.

要 Require ┊ resolvers registered (us_stock / forex / cfds / future do this
          ┊ at import-time via SpecRegistry.register)
          ┊ for cross_validate: a connected ib_async.IB + an AssetSpec
出 Provides┊ SpecRegistry · resolve(symbol, hint) → AssetSpec
          ┊ cross_validate(spec, symbol, ib) → broker-truth dict | raise
          ┊ UnknownSymbol · SpecMismatchError

─── 部  Modules used ─────────────────────────────────────────────────────
   .enum                         ┊ AssetClass — the hint / disambiguation key
   .spec                         ┊ AssetSpec — the value every resolver yields
   ib_async.IB                   ┊ TYPE_CHECKING only; reqContractDetailsAsync
   .policies.contract            ┊ ContractNotFound (lazy import, no-CD case)
   .types.price                  ┊ price() — representative ref for tick check
   decimal.Decimal               ┊ lazy — exact multiplier / tick comparison

─── 算  Algorithm · resolve, then prove it against the venue ──────────────
 Require: at least one resolver registered ; (for validation) a live ib
 Ensure : a spec is returned only if SOME resolver claims the symbol; the
          engine starts on a spec only if IBKR agrees field-for-field.

  1: register(resolver, priority=100)          ▷ class-level list grows
     │      _resolvers.append((priority, resolver)) ; sort by priority
     │      ▷ lower priority = checked first; future=20 < forex=50 < equity=100
  2: resolve(symbol, hint=None)                ▷ the engine's single question
  3:   for (_priority, resolver) in _resolvers ▷ O(N), priority order, <10 ever
  4:     spec ← resolver(symbol, hint)         ▷ each says "mine" (spec) or None
  5:     if spec is None → continue            ▷ not this resolver's symbol
  6:     if hint and spec.asset_class is not hint → continue  ▷ honor hint strictly
  7:     return spec                           ▷ FIRST non-None wins
  8:   raise UnknownSymbol(symbol, hint)       ▷ nobody claimed it → refuse
  9: ── startup gate ──  await cross_validate(spec, symbol, ib)
 10:   contract ← spec.contract.make(symbol)   ▷ build the IBKR contract
 11:   cds ← await ib.reqContractDetailsAsync(contract)   ▷ one cheap RPC
 12:   if not cds → raise ContractNotFound     ▷ doesn't qualify ≠ disagrees
     │      ▷ symbol typed wrong / wrong hint / no account permission
 13:   cd ← cds[0] ; broker_contract ← cd.contract
 14:   _assert_currency_matches(spec, broker_contract)    ▷ quote ccy must equal
 15:   _assert_multiplier_matches(spec, broker_contract)  ▷ futures only; ES50≠MES5
 16:   _assert_tick_matches(spec, cd)          ▷ spec grid ⊆ broker minTick grid
     │      ▷ any disagreement above → raise SpecMismatchError (engine refuses)
 17:   build broker_truth dict {min_tick, currency, multiplier}   ▷ venue reality
     │      ▷ D5: runtime rounding follows the BROKER's grid, not spec defaults
 18:   return broker_truth                     ▷ caller stores on Gateway
 19: resolve(symbol, hint)  (module fn)        ▷ thin shim → SpecRegistry.resolve

─── 関  Functions / classes defined ──────────────────────────────────────
   Resolver (type alias)   Callable[[str, Optional[AssetClass]], Optional[AssetSpec]]
   class UnknownSymbol(LookupError)        no resolver claimed the symbol
   class SpecMismatchError(RuntimeError)   spec disagrees with IBKR ContractDetails
   class SpecRegistry      __slots__=() — class-method-only, never instantiated
     register(resolver, priority=100)      append + sort; lower = first
     reset()                               clear all resolvers (tests)
     resolve(symbol, hint=None)            first-match scan → AssetSpec | raise
     async cross_validate(spec, symbol, ib) round-trip vs IBKR → broker-truth dict
     _assert_currency_matches(spec, bc)    quote currency equality
     _assert_multiplier_matches(spec, bc)  futures multiplier (Decimal-exact)
     _assert_tick_matches(spec, cd)        spec tick must align to broker minTick
   resolve(symbol, hint=None)  (module)    public shortcut for the engine

─── 変  Variables / state created ────────────────────────────────────────
   _resolvers      list[tuple[int, Resolver]]   class-level; the ONLY mutable state
                                                 (priority, resolver) sorted ascending
   __slots__       ()                            no-instance-state contract, explicit
   broker_truth    dict   per-call; {min_tick:float, currency:str, multiplier:float}
   UnknownSymbol.symbol / .hint                  carried for the operator's log
   SpecMismatchError.field / .expected / .actual / .context   structured diff

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   resolver(symbol, hint)               ▷ each registered factory (us_stock / forex /
                                           cfds / future _*_resolver functions)
   spec.contract.make · spec.tick.tick_size · spec.quote_currency · spec.sizing
   ib.reqContractDetailsAsync           ▷ the single broker RPC
   ContractNotFound · SpecMismatchError ▷ raised on no-CD / disagreement
   types.price · decimal.Decimal

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   src/assets/__init__.py        ▷ re-exports SpecRegistry · resolve · UnknownSymbol
                                    · SpecMismatchError (the public surface)
   us_stock.py · forex.py · cfds.py · future.py
                                 ▷ SpecRegistry.register(_*_resolver, priority=…)
                                    eq=100 · forex=50 · cfd=30/40/80 · future=20
   execution/broker.py           ▷ resolve() in qualify_contract; SpecRegistry
                                    .cross_validate at startup; catches SpecMismatchError
   config/models.py · feed/handler.py · strategy/risk.py · strategy/engine.py
                                 ▷ `from src.assets import resolve as _resolve_spec`
                                    (spec drives sizing · tick · session · P&L scaling)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • First match wins.  Priority orders the scan; a strict hint can still veto a
     match (step 6) so "EURUSD" defaults FX_CASH but FX_CFD is forcible.
   • Open-closed.  New asset class = a new resolver + register(); no central switch.
     Registration is an import side-effect — order in __init__ decides "ES" ties.
   • Never naked of truth.  cross_validate refuses to start when spec ≠ venue:
     ES(×50) vs MES(×5), pip granularity, currency. A 10× multiplier slip would
     under-size risk 10× — refuse, don't trade.
   • Qualify ≠ agree.  No ContractDetails → ContractNotFound (symbol/permission),
     NOT SpecMismatchError (that's for qualified contracts that disagree).
   • Tick rule.  spec tick may be EQUAL or an integer multiple of broker minTick
     (coarser-and-aligned = safe subset); finer-than-broker → IBKR rejects → block.
   • Broker grid is authoritative at runtime.  spec ticks are the safe offline
     default; cross_validate returns the venue's minTick for live rounding.
   • Class-only.  __slots__=() ; tests monkey-patch _resolvers / reset() directly.
   • Links:  values → [[spec]] · classes → [[enum]] · units → [[types]]
     resolvers → [[us_stock]] · [[forex]] · [[cfds]] · [[future]]
     consumers → [[broker]] · [[engine]] · [[handler]] · [[risk]] · [[models]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
