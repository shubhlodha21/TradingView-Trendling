━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 17 ·  src/assets/policies/price.py
  the price-reading contract — which feed field IS "the price" · per asset
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  149 lines · 1 Protocol (PricePolicy) · 1 dataclass (FeedSnapshot) · 1 error
  (NoUsablePrice). Pure interface — no behavior lives here; concrete policies
  live in us_stock (LastPricePolicy), forex (BidAskComparePricing), cfds
  (PrefMark). The 17 scattered `feed.last` reads in engine.py route through
  this one shape, isolating the bid/ask/last asymmetry to one file per asset.

要 Require ┊ a per-tick feed snapshot adapted to FeedSnapshot at the boundary
          ┊ (handler.py / production.py build it from the native Ticker)
          ┊ assets.types.Price  (Decimal price wrapper)
出 Provides┊ Protocol PricePolicy · dataclass FeedSnapshot · NoUsablePrice
          ┊ re-exported by policies/__init__ → composed into AssetSpec.price

─── 部  Modules used ──────────────────────────────────────────────────────
   __future__ annotations        ┊ deferred type-hint evaluation
   dataclasses (dataclass)       ┊ frozen+slots FeedSnapshot
   datetime (datetime)           ┊ FeedSnapshot.ts — staleness clock
   typing                        ┊ Optional · Protocol · runtime_checkable
   ..types (Price)               ┊ the Decimal price type returned by all 3 reads

─── 算  Algorithm · the per-tick price contract ──────────────────────────
 Require: a FeedSnapshot (bid? ask? last? — any field may be None / nan)
 Ensure : every read returns a usable Price OR raises NoUsablePrice;
          the engine SKIPS the tick on raise, never crashes, never guesses.

  1: feed boundary builds FeedSnapshot(bid, ask, last, …, ts)   ▷ frozen, slots
     │   ▷ narrow on purpose — policy never sees the full Ticker
  2: engine reads, per tick, on a usable snapshot  (engine.py:6258)
  3:   price.reference(feed)   → ref_px                ▷ the "current value"
     │      quote-driven (FX):  mid = (bid+ask)/2      ▷ symmetric, unbiased
     │      trade-driven (eq):  last, mid fallback     ▷ feeds highest_price,
     │                                                    trailing stop, P&L, UI
     │      no usable value → raise NoUsablePrice
  4:   price.buy_compare(feed) → buy_px                ▷ what you'd PAY now
     │      FX:  ask   (comparing to bid arms too early)
     │      eq:  last  (tight spread → last ≈ ask)
     │      engine fires entry when  buy_px >= trigger
  5:   price.sell_compare(feed)→ sell_px               ▷ what you'd RECEIVE now
     │      FX:  bid   (comparing to ask fires too early)
     │      eq:  last
     │      engine fires exit when  sell_px <= stop
  6:   price.is_actionable(feed) → bool                ▷ the freshness gate
     │      False on: stale ts · crossed/locked (bid>=ask) · absurd spread
     │                · missing required field per asset
     │      engine: False → skip tick silently; a window of False → STALE_FEED
  7: on NoUsablePrice at 3/4/5  →  engine catches, skips the tick
     │   ▷ a fresh tick is usually right behind  (engine.py:6262)
  8: return — three semantically distinct prices from one snapshot, the
     │        asymmetry confined to one policy class per asset class.

─── 関  Functions / classes defined ──────────────────────────────────────
   dataclass FeedSnapshot          frozen · slots — minimal feed subset
       bid ask last                Optional[Price]   quote + last trade
       bid_size ask_size last_size Optional[int]     depth at top of book
       volume                      Optional[int]     session volume
       high low vwap               Optional[Price]   session stats
       ts                          Optional[datetime] UTC snapshot time (staleness)
   class NoUsablePrice(ValueError) raised when no field is usable; caller skips
   Protocol PricePolicy            runtime_checkable structural contract
       reference(feed)   → Price   the stable "current value" (mid / last)
       buy_compare(feed) → Price   price you'd PAY  (ask / last) — entry trigger
       sell_compare(feed)→ Price   price you'd GET  (bid / last) — exit stop
       is_actionable(feed)→ bool   fresh + well-formed enough to trade on

─── 変  Variables / state created ────────────────────────────────────────
   (none — stateless protocol)     no module-level mutable state
   FeedSnapshot is immutable       frozen=True, slots=True — one per tick, GC'd
   all price fields Optional       None tolerated; policy decides fallback/raise

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   assets.types.Price              ┊ the only import edge (type only)
   (no runtime calls — pure interface; concrete impls do the work)

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   policies/__init__              ▷ re-exports PricePolicy · FeedSnapshot
   assets.spec (AssetSpec)        ▷ field  price: PricePolicy
   assets.us_stock                ▷ LastPricePolicy implements it; imports NoUsablePrice
   assets.forex                   ▷ BidAskComparePricing implements it (ask/bid)
   assets.future                  ▷ reuses us_stock.LastPricePolicy
   assets.cfds                    ▷ PrefMark policy; raises NoUsablePrice on gaps
   strategy.engine               ▷ tick path 6258-6262: reference / buy_compare /
                                     sell_compare + catches NoUsablePrice
   feed.handler                  ▷ builds the snapshot consumers read via spec.price.*

─── 注  Notes · invariants ───────────────────────────────────────────────
   • One file per asymmetry.  The bid/ask/last "which is the price" question
     is answered ONCE per asset class — not re-litigated at 17 call sites.
   • Conservative compare.  buy uses ASK, sell uses BID (FX) — you never arm
     on a price you couldn't actually transact at. Equity collapses all three
     to `last` only because its spread is tight.
   • Raise, don't guess.  NoUsablePrice is the contract for "no value" — callers
     MUST catch and skip the tick. Silent fallbacks would re-introduce the
     stale-FX-`last` bug this module exists to prevent.
   • Structural typing.  runtime_checkable Protocol — any object with the four
     methods satisfies it; tiny in-line test policies need no inheritance.
   • Decimal all the way.  returns Price (Decimal), never float — float only at
     the IBKR boundary.  [[types]]
   • Links:  spec bundle → [[spec]] · equity impl → [[us_stock]] · FX impl →
     [[forex]] · CFD mark → [[cfds]] · ticks → [[handler]] · consumer → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
