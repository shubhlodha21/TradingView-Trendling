━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 04 ·  src/assets/cfds.py
  CFD asset specs — settlement-by-difference · index · share · FX
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  506 lines · 7 dataclasses · 3 spec factories · 3 resolvers · 1 metadata table.
  Three CFD families (INDEX · SHARE · FX) share one contract shape and one
  overnight-financing flag, diverging only in price / tick / sizing / session.
  At import time the three resolvers register themselves on SpecRegistry.

要 Require ┊ a quote Currency + venue per family · a symbol the resolver claims
          ┊ INDEX_CFD: symbol in INDEX_CFD_METADATA · SHARE/FX: an explicit hint
出 Provides┊ make_index_cfd_spec · make_share_cfd_spec · make_fx_cfd_spec → AssetSpec
          ┊ CFDContract (ib_async CFD builder + reverse-identify) · 7 policy classes
          ┊ side-effect: registers 3 resolvers on SpecRegistry (priority 30/40/80)

─── 部  Modules used ──────────────────────────────────────────────────────
   ib_async (CFD, Contract, IB)  ┊ secType='CFD' contract · qualifyContractsAsync (TYPE_CHECKING + local import)
   assets.enum                   ┊ AssetClass.INDEX_CFD / SHARE_CFD / FX_CFD
   assets.forex                  ┊ BidAskComparePricing · ForexContinuousSession · PipTickPolicy
                                 ┊ _PAIR_PATTERN · _split_pair  (FX-family reuse)
   assets.us_stock               ┊ DecimalTickPolicy · LastPricePolicy · USEquitySession
                                 ┊ _US_EQUITY_PATTERN  (share-family reuse)
   assets.policies               ┊ FeedSnapshot · RoundDirection · OrderIntent · PortfolioView · RiskVerdict
   assets.policies.commission    ┊ Side
   assets.policies.contract      ┊ ContractNotFound (raised when IBKR returns no match)
   assets.policies.sizing        ┊ SizingMismatch (raised on wrong QuantityUnit)
   assets.policies.tick          ┊ round_to_grid
   assets.policies.price         ┊ NoUsablePrice  (local import — feed missing bid/ask/last)
   assets.resolver               ┊ SpecRegistry.register (module-load side-effect)
   assets.spec                   ┊ AssetSpec (the assembled product)
   assets.types                  ┊ Currency · Money · Price · Quantity · QuantityUnit
   decimal / datetime            ┊ Decimal ticks/fees · UTC age check on the feed

─── 算  Algorithm · build a spec, then claim symbols ──────────────────────
 Require: a symbol (and for share/FX, an explicit AssetClass hint)
 Ensure : the returned AssetSpec carries CFD lifecycle (financing=True) and
          a price/tick/sizing policy matched to the family.

  1: ── module load ──                         ▷ runs once on import (assets/__init__ → 部)
  2:   SpecRegistry.register(_index_cfd_resolver, priority=30)
  3:   SpecRegistry.register(_fx_cfd_resolver,    priority=40)
  4:   SpecRegistry.register(_share_cfd_resolver, priority=80)
     │      ▷ priorities sit below forex(50)/equity(100) so CFD never steals a default
  5: ── resolve(symbol, hint) ──               ▷ SpecRegistry walks resolvers by priority
  6:   _index_cfd_resolver(symbol, hint)       ▷ claims only INDEX_CFD_METADATA keys
     │      if hint set and not INDEX_CFD → None ; else metadata-membership decides
  7:     → make_index_cfd_spec(symbol)
     │        lookup meta ; unknown symbol → ValueError (lists known keys)
     │        CFDContract(currency, venue) · CFDMarkPricing() (mid/ask/bid + spread+age gate)
     │        FixedGrainTickPolicy.for_grain(tick) ▷ derive display decimals from grain exponent
     │        CFDSizing · IBKRCFDCommission(5 bps, $1 min) · ForexContinuousSession (23h stand-in)
     │        CFDLifecycle (financing=True) · CFDRiskOverlay
  8:   _fx_cfd_resolver(symbol, hint)          ▷ requires hint is FX_CFD ; else None
     │      _PAIR_PATTERN.match + _split_pair guard (bad pair → None)
  9:     → make_fx_cfd_spec(pair)
     │        quote ← _split_pair(pair)[1] ; BidAskComparePricing + PipTickPolicy.for_pair
     │        IBKRCFDCommission(bps=0, min=0)  ▷ FX CFD fee is baked into spread
 10:   _share_cfd_resolver(symbol, hint)       ▷ requires hint is SHARE_CFD ; else US_EQUITY wins
     │      _US_EQUITY_PATTERN.match guard
 11:     → make_share_cfd_spec(symbol)
     │        LastPricePolicy (real prints) · DecimalTickPolicy(2) · USEquitySession (RTH)
 12: ── at trade time, the engine reads the spec's policies ──
 13:   price.reference / buy_compare / sell_compare(feed)  ▷ mid for track, ask buy, bid sell
     │        CFDMarkPricing.is_actionable → False if crossed/wide(>20bps)/stale(>10s)
 14:   sizing.notional(qty, price) → Money     ▷ qty.unit must be CFD_UNITS else SizingMismatch
 15:   commission.estimate(qty, price, side)   ▷ notional×bps, floored at min_fee
 16:   lifecycle.has_overnight_financing() → True   ▷ the defining CFD trait
 17:   risk_overlay.check(intent, portfolio)   ▷ Day-1: always RiskVerdict.ok + financing note
 18: ── broker side ──  CFDContract.make(symbol) → ib_async CFD ; .qualify(ib, c) → qualified
     │        no match → ContractNotFound ; .identify(ib_contract) reverse-maps secType=='CFD'→symbol
 19: return — one AssetSpec per (symbol, family); spec is frozen, reused per tick.

─── 関  Functions / classes defined ──────────────────────────────────────
   class CFDContract              frozen · currency, exchange="SMART"
     make(symbol)                 build ib_async CFD (local import); upper-cases symbol
     identify(ib_contract)        static · reverse-map: secType=='CFD' → symbol, else None
     qualify(ib, contract)        async · qualifyContractsAsync; raises ContractNotFound on empty
   class CFDMarkPricing           frozen · index "mark" model · max spread 20bps / age 10s
     reference(feed)              mid (bid+ask)/2 → last fallback → NoUsablePrice
     buy_compare(feed)            ask → last fallback → NoUsablePrice
     sell_compare(feed)           bid → last fallback → NoUsablePrice
     is_actionable(feed)          False if no bid/ask, crossed, spread>cap, or stale
   class FixedGrainTickPolicy     frozen · grain, decimals
     for_grain(grain)             classmethod · decimals from grain.as_tuple().exponent
     tick_size / round_to_tick    fixed grain · round_to_grid by RoundDirection
     decimals_for_display         the precomputed decimals
   class CFDSizing                frozen · quote_currency, _min_qty=1, unit=CFD_UNITS
     notional(qty, price)         qty.value×price as Money; wrong unit → SizingMismatch
     min_qty / qty_increment      Quantity(1, CFD_UNITS)
     is_valid_qty(qty)            unit match + ≥min + integer increment
   class IBKRCFDCommission        frozen · bps=5, min_fee=1, quote_currency=USD
     estimate(qty, price, side)   notional×bps/10000 floored at min_fee → Money
   class CFDLifecycle             frozen · settlement_days_=0 (continuous mark)
     needs_roll / expiry          never roll · no expiry
     settlement_days              0
     has_overnight_financing      True  ▷ the key flag
   class CFDRiskOverlay           frozen · informational only (Day-1)
     check(intent, portfolio)     always RiskVerdict.ok(financing note); never blocks
   make_index_cfd_spec(symbol)    AssetSpec · requires INDEX_CFD_METADATA key else ValueError
   make_share_cfd_spec(symbol)    AssetSpec · USD · equity-like price/tick/session + CFD lifecycle
   make_fx_cfd_spec(pair)         AssetSpec · spot-FX price/tick + zero commission + CFD lifecycle
   _index_cfd_resolver(sym, hint) metadata-membership claim (priority 30)
   _share_cfd_resolver(sym, hint) hint-gated, US-equity pattern (priority 80)
   _fx_cfd_resolver(sym, hint)    hint-gated, pair pattern + _split_pair (priority 40)

─── 変  Variables / state created ────────────────────────────────────────
   UTC                 timezone     timezone.utc — used in is_actionable age check
   INDEX_CFD_METADATA  dict[str,dict] symbol → {currency, tick, venue, underlying}
                                    IBUS500 · IBUS30 · IBUST100 · IBDE40 · IBGB100 · IBJP225 · IBEU50
   (no module-level mutable state — all policy objects are frozen dataclasses)
   (registration side-effect at import mutates the shared SpecRegistry, not this module)

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   SpecRegistry.register            ▷ 3× at import (the only module-load side-effect)
   AssetSpec(...)                   the assembled product, one per factory
   ib_async.CFD · IB.qualifyContractsAsync
   _split_pair · PipTickPolicy.for_pair · BidAskComparePricing · ForexContinuousSession  (← forex)
   LastPricePolicy · DecimalTickPolicy · USEquitySession · _US_EQUITY_PATTERN  (← us_stock)
   round_to_grid · Money · Quantity · Price
   raises: ContractNotFound · SizingMismatch · NoUsablePrice · ValueError

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   src/assets/__init__.py         ▷ `from . import cfds` — triggers resolver registration
   src/execution/broker.py:72     ▷ imports CFDContract (build/identify CFD contracts at the socket)
   tests/assets/test_cfds.py      ▷ exercises factories + CFDMarkPricing + INDEX_CFD_METADATA
   tests/assets/test_symbology_lockdown.py  ▷ pins CFDContract.identify() secType/symbol contract
   (no graph node for this file — edges above are grep-verified, not invented)

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Financing is the point.  All three families set has_overnight_financing=True;
     CFDs are settlement-by-difference — never own the underlying, broker marks daily.
   • Opt-in by hint.  SHARE_CFD and FX_CFD claim a symbol ONLY with an explicit hint;
     bare AAPL → US_EQUITY, bare EURUSD → FX_CASH (IDEALPRO). INDEX_CFD claims by
     metadata membership alone (IBUS500-style symbols can't collide with alpha tickers).
   • Priority below the defaults.  30 / 40 / 80 all sit under forex(50)/equity(100) so a
     CFD resolver never steals a symbol the default path should own.
   • Frozen + reuse.  Every policy is a frozen, slotted dataclass; a spec is built once
     and read per tick — no mutation, safe to share across the engine's hot path.
   • Risk overlay is informational in Day-1 — surfaces a financing note, never blocks.
   • Links:  contract socket → [[broker]] · price/tick reuse → [[forex]] · [[us_stock]]
     registration → [[resolver]] · assembled shape → [[spec]] · units → [[types]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
