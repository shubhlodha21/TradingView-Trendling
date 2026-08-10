━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 05 ·  src/assets/currency_service.py
  the base-currency convergence point — FX cross-rate cache + conversion
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  172 lines · 1 service class (CurrencyService) · 2 errors · 1 record · 5 methods
  Day-1 STUB: the interface stands so sizing + risk can wire to it without
  churn; live rate sourcing (ib_async crosses, snapshot fallback) lands D2-PM.

要 Require ┊ a Currency enum + DEFAULT_BASE_CURRENCY (from assets.types)
          ┊ rates pushed in via update() — D1 has no live source
出 Provides┊ class CurrencyService · .rate() lookup · .update() seed
          ┊ StaleRate · NoRateAvailable · CachedRate
          ┊ the rate engine behind every Money.to(target, fx)

─── 部  Modules used ─────────────────────────────────────────────────────
   dataclasses                   ┊ @dataclass(frozen, slots) for CachedRate
   datetime  (datetime/timedelta/timezone)
                                 ┊ UTC tz-aware fetched_at + age_seconds()
   decimal.Decimal               ┊ rates are exact — never float arithmetic
   typing.Optional               ┊ now: Optional[datetime] hooks
   assets.types                  ┊ Currency · DEFAULT_BASE_CURRENCY (no cycle)

─── 算  Algorithm · one rate lookup, three resolution paths ───────────────
 Require: a Currency pair (from_ccy, to_ccy) + a seeded or live cache
 Ensure : returns an exact Decimal rate OR raises loudly — never silently
          hands back a stale or invented number.

  1: __init__(base=USD, staleness_tolerance_seconds=60.0)
     │                                  ▷ slots: _base · _cache · _staleness_tolerance
     │                                  ▷ _cache: dict[(Currency,Currency) → CachedRate]
  2: update(from_ccy, to_ccy, rate)     ▷ the write path (D1: tests · D2: ticker handler)
  3:   if from_ccy is to_ccy → return   ▷ no-op; identity never cached
  4:   _cache[(from,to)] ← CachedRate(rate, fetched_at = now(UTC))
     │                                  ▷ only the direct direction is stored
  5: rate(from_ccy, to_ccy) → Decimal   ▷ the read path — the heart
  6:   if from_ccy is to_ccy → Decimal("1")
     │                                  ▷ same-currency conversion is free, no cache touch
  7:   ── path A · direct ──  cached ← _cache.get((from,to))
  8:     if cached:  age ← cached.age_seconds(now)
     │       if age > tolerance → raise StaleRate(from,to,age)   ▷ refuse, don't guess
     │       else → return cached.rate
  9:   ── path B · inverse ──  inv ← _cache.get((to,from))
     │                                  ▷ seeded EURUSD answers a USDEUR ask
 10:     if inv:  age ← inv.age_seconds(now)
     │       if age > tolerance → raise StaleRate(from,to,age)
     │       else → return Decimal("1") / inv.rate   ▷ reciprocal
 11:   ── path C · miss ──  raise NoRateAvailable(from,to)
     │                                  ▷ D2 will add cross-via-base: EUR→USD × USD→JPY
 12: CachedRate.age_seconds(now=None)   ▷ now or datetime.now(UTC); seconds since fetched_at
 13: base (property) → _base            ▷ the convergence currency for risk + P&L

─── 関  Functions / classes defined ──────────────────────────────────────
   class StaleRate(RuntimeError)        cached rate older than tolerance; carries
                                        from_ccy · to_ccy · age_seconds
     __init__(from_ccy, to_ccy, age_seconds)   builds the "refresh or refuse" message
   class NoRateAvailable(RuntimeError)  pair never seen — warm() not called / feed down
     __init__(from_ccy, to_ccy)         builds the "call warm() / check feed" message
   class CachedRate (frozen, slots)     one cache entry: rate + fetched_at (UTC)
     age_seconds(now=None) → float      cheap staleness measure
   class CurrencyService                FX rate cache + conversion (slots)
     __init__(base, staleness_tolerance_seconds)   sets base · empty cache · TTL
     base (property) → Currency         the system's base / convergence currency
     rate(from_ccy, to_ccy) → Decimal   direct → inverse → raise (the 3 paths above)
     update(from_ccy, to_ccy, rate)     seed/refresh one direction; identity no-op

─── 変  Variables / state created ────────────────────────────────────────
   DEFAULT_BASE_CURRENCY  Currency   imported default for the base arg (= USD)
   _base                  Currency   convergence currency; immutable per instance
   _cache    dict[(Currency,Currency) → CachedRate]   the live rate store
   _staleness_tolerance   float      TTL seconds; > this → StaleRate (default 60)
   CachedRate.rate        Decimal    exact rate, one direction
   CachedRate.fetched_at  datetime   UTC tz-aware; basis for age_seconds()

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   datetime.now(tz=timezone.utc)       ▷ in update() write + age_seconds() read
   CachedRate.age_seconds              ▷ rate() consults it on both direct + inverse
   StaleRate / NoRateAvailable         ▷ raised, never swallowed
   Decimal("1") / inv.rate             ▷ reciprocal for the inverse path
   (assets.types only — no broker, no IBKR, no I/O in D1)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   assets.types.Money.to(target, fx)   ▷ THE hot caller — fx.rate(self.currency, target)
                                          then Money(amount × rate, target)   [grep]
   assets.__init__                      ▷ re-exports CurrencyService · StaleRate ·
                                          NoRateAvailable to the package surface  [grep]
   assets.forex                         ▷ doc: "CurrencyService converts to base later"  [grep]
   tests/assets/test_types.py           ▷ seeds rates via update(), asserts .to()  [grep]
   run_live.py                          ▷ references the service at wiring time  [grep]
   ▷ graph had NO node for this file (too new to index) — edges are grep-sourced

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Exact money.  Rates are Decimal — never float; arithmetic stays exact through
     Money.to(). float only at the IBKR/display boundary (to_float).
   • Fail loud, not stale.  A rate past TTL raises StaleRate; a missing pair raises
     NoRateAvailable. The caller chooses last-known-good OR refuse — the service
     never silently returns a stale or guessed number.
   • Identity is free.  from_ccy is to_ccy → Decimal("1"), no cache touch, no I/O.
   • One-direction store.  update() caches only (from,to); rate() derives the
     reciprocal on demand. D2 adds cross-via-base (EUR→USD × USD→JPY).
   • Stub honesty.  D1 supports same-currency + direct/inverse cached pairs only;
     class shape (warm/rate) is annotated for the D2-PM live-ticker wiring.
   • Links:  Money / Currency → [[types]] · per-asset FX shape → [[forex]]
     package surface → [[__init__]] · P&L convergence to base → [[risk]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
