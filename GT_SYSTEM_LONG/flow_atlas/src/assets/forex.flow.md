━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 07 ·  src/assets/forex.py
  the spot-Forex AssetSpec — bid/ask is truth · last is a liar · 24/5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  568 lines · 7 policy classes · 4 free functions · 1 registry hook.
  The asset class most UNLIKE equity: IDEALPRO never prints a usable `last`,
  quantities are BASE_UNITS not shares, and there is no daily close — one
  continuous Sun 22:00 → Fri 22:00 UTC week. ForexSpec composes seven
  policies into the one AssetSpec the engine reads through.

要 Require ┊ a 6-char ISO pair string ("EURUSD") whose halves are known Currency
          ┊ ib_async.Forex (lazy import) · a live FeedSnapshot carrying bid/ask
          ┊ the shared policy protocols + types from assets.policies / assets.types
出 Provides┊ make_forex_spec(pair) → AssetSpec(asset_class=FX_CASH, …)
          ┊ 7 reusable policy classes (CFD + Future borrow Session & Pricing)
          ┊ IDEALPROForexContract.identify — reverse base+quote → pair string
          ┊ a SpecRegistry resolver auto-registered at import (priority 50)

─── 部  Modules used ──────────────────────────────────────────────────────
   re · datetime · decimal · zoneinfo  ┊ pair regex · UTC/NY tz · Decimal ticks
   .enum                 (AssetClass)  ┊ FX_CASH tag
   .policies                           ┊ FeedSnapshot · RoundDirection · SessionWindow
                                       ┊ OrderIntent · PortfolioView · RiskVerdict
   .policies.commission  (Side)        ┊ BUY/SELL enum for fee estimate
   .policies.contract    (ContractNotFound)
   .policies.price       (NoUsablePrice)┊ raised when bid/ask absent (never last)
   .policies.sizing      (SizingMismatch)
   .policies.tick        (round_to_grid)┊ shared grid-rounder
   .resolver             (SpecRegistry) ┊ register the FX resolver at module load
   .spec                 (AssetSpec)    ┊ the composite the factory returns
   .types                              ┊ Currency · Money · Price · Quantity ·
                                       ┊ QuantityUnit · base_units
   .us_stock             (NoLifecycle)  ┊ reused — FX cash also doesn't roll (T+2)
   ib_async  (TYPE_CHECKING / lazy)     ┊ Contract · IB · Forex — imported inside .make

─── 算  Algorithm · pair string → composed AssetSpec ───────────────────────
 Require: a candidate symbol string; optional AssetClass hint
 Ensure : FX pairs resolve to a fully-composed spec; non-FX symbols return
          None so the next registry resolver gets a chance.

  1: import-time:  SpecRegistry.register(_forex_resolver, priority=50)
     │                                        ▷ runs once when assets/__init__ pulls forex in
  2: _forex_resolver(symbol, hint)            ▷ the gate
     │      if hint is not None and not FX_CASH → return None
     │      if not _PAIR_PATTERN.match (6 upper) → return None
  3:     try _split_pair(p)                    ▷ both halves must be known Currency
     │        except ValueError → return None  ▷ soft fail; let equity resolver try
  4:     return make_forex_spec(p)             ▷ matched — build the spec
  5: make_forex_spec(pair)                     ▷ the factory
     │      pair ← pair.upper()
     │      _, quote ← _split_pair(pair)        ▷ validates + extracts quote ccy
  6:     return AssetSpec(                      ▷ compose seven policies:
     │        contract  = IDEALPROForexContract()
     │        price     = BidAskComparePricing()
     │        tick      = PipTickPolicy.for_pair(pair)        ▷ JPY→0.005 else 0.00005
     │        sizing    = FXBaseCurrencySizing.for_pair(pair) ▷ notional in quote ccy
     │        commission= IBKRFXCommission()                  ▷ 0.20 bps, min $2 USD
     │        session   = ForexContinuousSession()            ▷ Sun 22:00 → Fri 22:00
     │        lifecycle = NoLifecycle(settlement_days_=2)      ▷ T+2 cash, no roll
     │        risk_overlay = FXRiskOverlay()                  ▷ weekend-gap guard
     │      )
  7: ── at runtime the engine reads through the spec ──
  8:   price.reference(feed)  = (bid+ask)/2     ▷ mid for tracking; raises if either None
     │   price.buy_compare(feed)  = ask          ▷ what a BUY actually pays
     │   price.sell_compare(feed) = bid          ▷ what a SELL actually receives
  9:   price.is_actionable(feed)               ▷ both sides present · not crossed ·
     │        spread ≤ 5 pips (pip=0.01 if mid>50 else 0.0001) · feed age < 5s
 10:   contract.make(symbol)                   ▷ lazy import ib_async.Forex(pair);
     │        _split_pair validates shape at OUR boundary first
 11:   contract.qualify(ib, contract)          ▷ await qualifyContractsAsync; empty → ContractNotFound
 12:   sizing.notional(qty, price) = qty.value × price → Money(quote_ccy)
     │        raises SizingMismatch unless qty.unit is BASE_UNITS
 13:   commission.estimate(qty, price, side)   ▷ max(notional×0.00002, $2) → Money(USD)
 14:   session.is_open_at(now) / next_close / is_within_n_minutes_of_close
 15:   risk_overlay.check(intent, portfolio)   ▷ SELL → always ok; BUY within 30 min of
     │        Friday close → BLOCK (Sunday-open gap risk); else ok
 16: ── reconcile path (why .identify exists) ──
 17:   Gateway.get_positions sees pos.symbol="EUR", pos.currency="USD"
     │        IDEALPROForexContract.identify(pos) → "EURUSD"  ▷ rebuilds the pair string
     │        without it the naive symbol match fails → phantom-FLAT → killed stop (A18)

─── 関  Functions / classes defined ────────────────────────────────────────
   _split_pair(pair)            → (Currency, Currency)  · regex + Currency() parse, raises
   _tick_for_pair(pair)         → Decimal               · JPY quote → 0.005 else 0.00005
   make_forex_spec(pair)        → AssetSpec              · the public factory
   _forex_resolver(symbol,hint) → Optional[AssetSpec]    · SpecRegistry hook (priority 50)

   class IDEALPROForexContract   contract policy (frozen, slots)
       make · identify(static) · qualify(async)
   class BidAskComparePricing    price policy — bid/ask only, never last
       reference · buy_compare · sell_compare · is_actionable
   class PipTickPolicy           tick policy (carries resolved tick + decimals)
       for_pair(classmethod) · tick_size · round_to_tick · decimals_for_display
   class FXBaseCurrencySizing    sizing in BASE_UNITS → Money(quote ccy)
       for_pair(classmethod) · notional · min_qty · qty_increment · is_valid_qty
   class IBKRFXCommission        0.20 bps of notional, min $2 USD
       estimate
   class ForexContinuousSession  24/5 single weekly window
       is_open_at · windows_for_date · next_open · next_close
       is_within_n_minutes_of_close · time_to_close
   class FXRiskOverlay           weekend-gap entry guard
       check

─── 変  Variables / state created ──────────────────────────────────────────
   UTC                  timezone     timezone.utc
   NY_TZ                ZoneInfo     "America/New_York" (kept for ET observers)
   JPY_PAIR_TICK        Decimal      0.005   — half-pip on JPY's coarser grid
   DEFAULT_PAIR_TICK    Decimal      0.00005 — 5dp half-pip for non-JPY majors
   FX_COMMISSION_BPS    Decimal      0.20 bps  = 0.00002 fraction
   FX_COMMISSION_MIN_USD Decimal     $2.00 floor per order
   FX_MIN_BASE_UNITS    Decimal      25,000 — IDEALPRO minimum order
   FX_QTY_INCREMENT     Decimal      1 base unit (no rounded lots)
   _PAIR_PATTERN        re.Pattern   ^[A-Z]{6}$ — 6 uppercase letters, no dot
   (policies are frozen+slots dataclasses; all state is immutable per-pair config)

─── 呼  Calls-out  → ───────────────────────────────────────────────────────
   ib_async.Forex                    ▷ IDEALPROForexContract.make (lazy)
   ib.qualifyContractsAsync          ▷ IDEALPROForexContract.qualify
   round_to_grid                     ▷ PipTickPolicy.round_to_tick
   Currency(…) / Money / Price / Quantity      ▷ types — construction + validation
   SessionWindow · RiskVerdict.ok/block        ▷ session windows · overlay verdicts
   SpecRegistry.register             ▷ import-time resolver registration
   raises: ValueError · ContractNotFound · NoUsablePrice · SizingMismatch

─── 被  Called-by  ← ───────────────────────────────────────────────────────
   assets/__init__.py        ▷ `from . import forex` — import for resolver side-effect
   execution/broker.py:70    ▷ imports IDEALPROForexContract; .identify on get_positions
   assets/future.py:57       ▷ reuses ForexContinuousSession (Globex 24/5 stand-in)
   assets/cfds.py:46         ▷ reuses BidAskComparePricing + ForexContinuousSession
   assets/us_stock.py:156    ▷ references IDEALPROForexContract.identify in a comment
   strategy/engine.py        ▷ resolves the spec via SpecRegistry; reads price/session
   (knowledge-graph had no node for this file — edges above are grep-confirmed)

─── 注  Notes · invariants ─────────────────────────────────────────────────
   • Never reads feed.last.  IDEALPRO `last` is stale/lying (diag 2026-06-03:
     lastSize=0, 7s old) — every price comes from bid/ask or raises NoUsablePrice.
   • Quote currency is the output.  notional comes out in the QUOTE ccy (USD for
     EURUSD, JPY for USDJPY); CurrencyService converts to base later for the gate.
   • JPY scale trap.  is_actionable picks pip=0.01 when mid>50; the JPY notional
     bug (risk gate treating JPY as USD) was a separate fix [[risk]] A24.
   • identify() is load-bearing.  ib_async drops the pair string — symbol=base,
     currency=quote.  Without reverse-translation the engine reads FLAT and kills
     the protective stop (the FX phantom-position class, A18 / [[broker]]).
   • No daily close · no holidays.  one continuous weekly window; 22:00 UTC anchor
     stays fixed across DST by venue design.
   • Frozen + slots.  every policy is immutable per-pair config — safe to share.
   • Links:  composed into → [[spec]] · resolved by → [[resolver]] · contract qualify
     + positions → [[broker]] · P&L scaling → [[risk]] · read by → [[engine]]
     · reused by → [[cfds]] · [[future]] · lifecycle from → [[us_stock]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
