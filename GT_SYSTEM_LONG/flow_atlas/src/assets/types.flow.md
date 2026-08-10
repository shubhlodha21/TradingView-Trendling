━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 11 ·  src/assets/types.py
  the unit-of-account vocabulary — every number in the system wears a tag
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  513 lines · 7 classes · 8 free functions · zero runtime deps beyond the
  stdlib (dataclasses · decimal · enum).  Born from the 2026-06-02 PLTR
  shorting bug, where `30 shares`, `30 base-units`, and `30 contracts` were
  all just `30`.  Here a number is never naked: it carries a unit or a
  currency, and arithmetic across a mismatch RAISES rather than lies.

要 Require ┊ nothing — leaf of the import graph; only Decimal / Enum / dataclass
          ┊ (CurrencyService referenced under TYPE_CHECKING only, to dodge a cycle)
出 Provides┊ Currency · QuantityUnit · Quantity · Price · Money
          ┊ QuantityUnitMismatch · CurrencyMismatch (typed failures)
          ┊ constructors: shares · contracts · base_units · cfd_units
          ┊                usd · money · price  (the ergonomic front door)

─── 部  Modules used ──────────────────────────────────────────────────────
   decimal.Decimal               ┊ the one true numeric — no IEEE-754 drift
   enum.Enum                     ┊ Currency · QuantityUnit closed value sets
   dataclasses.dataclass         ┊ frozen + slots → immutable, compact, typed
   typing.Final / TYPE_CHECKING  ┊ DEFAULT_BASE_CURRENCY const · cycle-safe hint
   .currency_service.CurrencyService ┊ TYPE_CHECKING only — Money.to() rate source

─── 算  Algorithm · how a tagged value lives ──────────────────────────────
 Require: a raw number (int | str | float | Decimal) and an intended unit
 Ensure : the unit/currency stays welded to the value through every op;
          any cross-tag arithmetic dies loudly at the call site, not 6 frames deep.

  1: caller wants a quantity → shares(30) / contracts(1) / base_units("25000.5")
     │                                          ▷ ergonomic front door (関 below)
  2:   _to_decimal(n)                           ▷ int/str/float → Decimal via str()
     │      ▷ float routed through str() so Decimal(0.1) noise never enters
  3:   Quantity(value, unit)                    ▷ frozen dataclass constructed
  4:     __post_init__                          ▷ guard: value MUST already be Decimal
     │      ▷ else TypeError — names the bug at construction, not downstream
  5:   contracts() extra check                  ▷ fractional futures → ValueError
  6: ── arithmetic ──  q1 + q2
  7:   __add__ / __sub__                         ▷ if unit mismatch → QuantityUnitMismatch
     │      ▷ same guard on __lt__ __le__ __gt__ __ge__ (ordering crosses units too)
  8:   __mul__(scalar)                           ▷ Quantity × unitless OK; Quantity×Quantity raises
     │      ▷ units don't compose — no Quantity² type exists
  9:   __neg__ · __abs__ · is_zero/positive/negative  ▷ unit preserved throughout
 10: ── boundary exit ──  to_int() / to_float()
     │      ▷ to_int raises if fractional (FX base-units mistaken for shares = caught)
     │      ▷ to_float for FX where 25_000.50 is legal — the only float egress
 11: ── money parallel track ──  usd(4545) / money(amt, cur) / Money(Decimal, Currency)
 12:   same guards: __post_init__ Decimal check; +/−/×/compare gated by CurrencyMismatch
 13:   Money.to(target, fx)                      ▷ the ONLY cross-currency bridge
     │      if target is self.currency → return self (no service call)
     │      else rate ← fx.rate(self.currency, target) ; Money(amount × rate, target)
     │      ▷ no implicit conversion anywhere — the caller always sees which rate, when
 14: ── price track ──  price(p) → Price(Decimal subclass)
     │      ▷ Price * Decimal → Decimal "just works"; tag is documentation, not a wall
     │      ▷ float input coerced via str() to dodge binary-float baggage
 15: return — the value flows on, unit/currency riding with it forever.

─── 関  Functions / classes defined ───────────────────────────────────────
   class Currency(Enum)              ISO-4217 set we trade (USD…CNH) · __repr__
   DEFAULT_BASE_CURRENCY  Final      module const = Currency.USD (greenfield default)
   class QuantityUnit(Enum)          SHARES·CONTRACTS·BASE_UNITS·OPTION_CONTRACTS·CFD_UNITS
   class QuantityUnitMismatch(TypeError)  carries left·right·op for an actionable trace
   class Quantity  (frozen, slots)   Decimal value + QuantityUnit tag
       __post_init__   Decimal-only guard at construction
       __add__ __sub__ unit-checked add/sub → QuantityUnitMismatch
       __mul__ __rmul__ scale by unitless scalar; Quantity×Quantity raises
       __neg__ __abs__ unit-preserving sign ops
       __lt__ __le__ __gt__ __ge__  unit-checked ordering
       is_zero is_positive is_negative   sign predicates (properties)
       to_int          int egress; raises if fractional
       to_float        float egress (FX base-units)
       __repr__        "Quantity(30 SHARES)"
   class Price(Decimal, slots=())    per-unit price; Decimal subclass · __repr__
   class CurrencyMismatch(TypeError) carries left·right·op; names .to() as the fix
   class Money  (frozen, slots)      Decimal amount + Currency tag
       __post_init__   Decimal-only guard
       __add__ __sub__ currency-checked → CurrencyMismatch
       __mul__ __rmul__ scale by scalar; Money×Money raises
       __neg__ __abs__ sign ops
       __lt__ __le__ __gt__ __ge__  currency-checked ordering
       is_zero is_positive is_negative   sign predicates
       to(target, fx)  the sole cross-currency conversion (explicit rate)
       to_float        lossy egress for API/display
       __repr__        "Money(4545.00 USD)"
   shares contracts base_units cfd_units   Quantity constructors (unit-fixed)
   usd money                          Money constructors
   price                              Price constructor (float→str coerce)
   _to_decimal                        private — int/str/float → Decimal, no drift
   __all__                            explicit public surface (16 names)

─── 変  Variables / state created ─────────────────────────────────────────
   DEFAULT_BASE_CURRENCY  Final[Currency]   USD — every default base-currency
   Quantity.value         Decimal           the magnitude (frozen)
   Quantity.unit          QuantityUnit      the denomination tag (frozen)
   Money.amount           Decimal           the magnitude (frozen)
   Money.currency         Currency          the denomination tag (frozen)
   QuantityUnitMismatch.left/right/op       the two clashing units + operator
   CurrencyMismatch.left/right/op           the two clashing currencies + operator
   ▷ instances are frozen+slots → no __dict__, no mutation, hashable

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   CurrencyService.rate(from, to)    ▷ only inside Money.to() — late-bound, cycle-safe
   Decimal(str(...))                 ▷ the coercion spine of _to_decimal / price
   (otherwise pure — no broker, no feed, no IO; a leaf node)

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   (graph had no node for this file; edges below grepped from src — not invented)
   config.models             ▷ lazy `import Quantity as _Q, price as _P` (sizing math)
   execution.broker          ▷ `import price as _to_price` at the IBKR boundary
   strategy.risk             ▷ lazy import of Money/Quantity for P&L attribution
   strategy.engine           ▷ uses tagged values through the tick / sizing path
   assets.spec               ▷ `from .types import Currency`
   assets.forex / future / us_stock / cfds / resolver  ▷ per-asset specs import the tags
   assets.currency_service   ▷ Currency · DEFAULT_BASE_CURRENCY (and is Money.to()'s peer)
   assets.__init__           ▷ re-exports the vocabulary upward

─── 注  Notes · invariants ────────────────────────────────────────────────
   • A number is never naked.  Every magnitude carries a unit or a currency;
     mismatched arithmetic RAISES (QuantityUnitMismatch / CurrencyMismatch) —
     the PLTR-shorting class of bug becomes a loud TypeError, not a silent loss.
   • Decimal everywhere.  float is forbidden except at the IBKR boundary; all
     ingress (_to_decimal, price) routes float through str() to avoid 0.1-drift.
   • No implicit FX.  Cross-currency math has exactly one door — Money.to(t, fx) —
     so every conversion shows which rate was used and when.  We refuse to guess.
   • Frozen + slots.  Values are immutable and compact; safe to share, hash, cache.
   • Leaf of the graph.  Zero runtime project deps → import it from anywhere with
     no cycle (CurrencyService kept under TYPE_CHECKING for exactly this reason).
   • Units that don't compose stay un-composed: no Quantity×Quantity, no Money².
   • Links:  conversion → [[currency_service]] · sizing → [[models]] · P&L → [[risk]]
     API egress → [[broker]] · per-asset tags → [[spec]] · [[forex]] · namespace → [[__init__]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
