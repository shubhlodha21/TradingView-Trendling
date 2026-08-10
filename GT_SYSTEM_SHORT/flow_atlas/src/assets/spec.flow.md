━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 10 ·  src/assets/spec.py
  the asset-class identity card — one frozen bundle of policies per bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  117 lines · 1 class (AssetSpec) · frozen+slots dataclass · the seam that
  un-hardcoded "US equity" from 12,000 lines. Every Engine carries exactly
  one, resolved once at __init__. Where the engine once assumed equity it now
  asks  self._spec.<policy>.<method>(…)  instead.

要 Require ┊ an AssetClass · a quote Currency · a venue string
          ┊ eight already-built policy objects (composition, not inheritance)
          ┊ sizing.quote_currency MUST equal the spec's quote_currency
出 Provides┊ class AssetSpec — immutable, hashable, diff-able across restarts
          ┊ .describe() one-line summary · .to_audit_dict() order-time snapshot
          ┊ the single place asset-class behaviour is composed & validated

─── 部  Modules used ──────────────────────────────────────────────────────
   dataclasses                   ┊ @dataclass(frozen=True, slots=True)
   __future__.annotations        ┊ string-deferred type hints
   .enum            (AssetClass) ┊ EQUITY / FOREX / FUTURE / CFD discriminator
   .types           (Currency)   ┊ quote currency value-type (USD / EUR / JPY…)
   .policies                     ┊ the 8 policy Protocols this bundle composes:
                                 ┊ Contract · Price · Tick · Sizing · Commission
                                 ┊ Session · Lifecycle · RiskOverlay

─── 算  Algorithm · compose once, then answer forever ─────────────────────
 Require: a concrete factory (make_us_equity_spec / make_forex_spec / …)
 Ensure : a frozen, internally-consistent spec — or a loud ValueError at
          construction; never a half-built spec that lies at order time.

  1: factory builds the 8 policies              ▷ in us_stock / forex / cfds /
     │                                             future  (→ 流 resolver)
  2: AssetSpec(asset_class, quote_currency,     ▷ every field REQUIRED — no
     │          venue, contract, price, tick,      defaults; omission is how the
     │          sizing, commission, session,       equity assumption leaked once,
     │          lifecycle, risk_overlay)           so the factory must choose all
  3:   __post_init__()  fires                    ▷ cross-policy consistency gate
  4:     if sizing.quote_currency is not          ▷ identity check (is, not ==);
     │        self.quote_currency:                   catches "sizing returns EUR
     │           raise ValueError(…)                 but spec says USD" mis-wiring
  5:   dataclass freezes the instance            ▷ slots + frozen ⇒ immutable,
     │                                             hashable, no hot-swap mid-life
  6: ── at runtime the engine only READS ──
  7:   spec.tick.round_to_tick(price)            ▷ price discipline per asset
  8:   spec.sizing.notional(qty, price)          ▷ Money in the right currency
  9:   spec.session.is_open_at(now)              ▷ RTH / 24×5 / GLOBEX hours
 10:   spec.risk_overlay.check(intent, port)     ▷ per-asset pre-trade verdict
 11: ── at order time the engine SNAPSHOTS ──
 12:   spec.to_audit_dict()                      ▷ asset_class.value, ccy.value,
     │                                             venue, + each policy class name
     │                                             → frozen into the audit row so a
     │                                             post-mortem can ask "what config
     │                                             was live when this order fired?"
 13:   spec.describe()                           ▷ same content, one log-line form
 14: return — the spec never mutates; it is data, the Gateway holds connection.

─── 関  Functions / classes defined ───────────────────────────────────────
   class AssetSpec  (frozen=True, slots=True)
     fields(identity)   asset_class · quote_currency · venue
     fields(policies)   contract · price · tick · sizing · commission
                        session · lifecycle · risk_overlay
     __post_init__      cross-policy consistency check (sizing ccy == spec ccy)
     describe()         → str   human-readable one-line spec summary
     to_audit_dict()    → dict  serializable snapshot for the audit log

─── 変  Variables / state created ─────────────────────────────────────────
   asset_class      AssetClass   the discriminator (EQUITY/FOREX/FUTURE/CFD)
   quote_currency   Currency     currency the asset is priced/settled in
   venue            str          "SMART"/"IDEALPRO"/"GLOBEX"/… routing tag
   contract         ContractPolicy   builds the ib_async Contract
   price            PricePolicy      price parse / format discipline
   tick             TickPolicy       tick size + round_to_tick
   sizing           SizingPolicy     notional() → Money in quote_currency
   commission       CommissionPolicy fee model per asset
   session          SessionPolicy    is_open_at() trading-hours calendar
   lifecycle        LifecyclePolicy  entry/exit/re-entry order shapes
   risk_overlay     RiskOverlay      per-asset pre-trade check()
   (no module-level state — every value lives on the frozen instance)

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   AssetClass.name / .value            Currency.name / .value
   type(<policy>).__name__             ▷ for describe() / to_audit_dict()
   <policy>.quote_currency             ▷ the __post_init__ consistency probe
   (graph node absent for this file — edges read from source + grep)

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   assets.us_stock        ▷ make_us_equity_spec(symbol) → AssetSpec(…)
   assets.forex           ▷ make_forex_spec(pair)       → AssetSpec(…)
   assets.cfds            ▷ 3 CFD factories             → AssetSpec(…)
   assets.future          ▷ make_future_spec(…)         → AssetSpec(…)
   assets.resolver        ▷ symbol → factory → AssetSpec  (the dispatcher)
   assets.__init__        ▷ re-exports AssetSpec at package surface
   ─ indirect consumers (hold the spec, never import this file) ─
   strategy.engine        ▷ self._spec.<policy>.<method>(…) on the hot path
   execution.broker       ▷ spec.contract / spec.venue when building orders
   feed.handler           ▷ spec.price / spec.tick on the tick path
   strategy.risk          ▷ spec.risk_overlay + quote_currency for P&L scaling

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Frozen forever.  One spec per bot, resolved at __init__ from the symbol;
     no asset-class hot-swap during a bot's life.  Immutability buys free
     hashability + clean cross-restart diffing + safe audit capture.
   • Every field required.  No defaults — a factory must make a deliberate
     choice on all 8 dimensions; "noop" must be picked explicitly, never
     fall out of omission (the original equity-leak failure mode).
   • Composition over inheritance.  Behaviour is 8 swappable policy objects,
     not a class tree; new asset classes = new factory, not new subclass.
   • Consistency gate.  __post_init__ uses `is` (identity) on the Currency
     singleton — sizing currency and spec currency must be the SAME object,
     not merely equal; a mismatch raises at construction, never at order time.
   • Not here.  Symbol lives on engine.config.ticker; cycle state (entry_price,
     qty) lives on the engine; sockets / IB handles live on the Gateway.
     AssetSpec is data — the Gateway is connection.
   • Links:  built-by → [[resolver]] · policies → [[policies]] · types → [[types]]
     class tag → [[enum]] · consumed-on-hot-path → [[engine]] · orders → [[broker]]
     P&L scaling → [[risk]] · ticks → [[handler]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
