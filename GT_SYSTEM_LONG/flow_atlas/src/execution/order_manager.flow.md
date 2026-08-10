━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 30 ·  src/execution/order_manager.py
  the order-shaping layer — signal → typed request → broker → filled record
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  339 lines · 3 classes (OrderEvent · OrderRequest · OrderManager) · 11 methods
  A clean seam between *deciding* an order and *executing* one.  It mints order
  IDs, validates the request shape, builds the OrderRecord, drives the broker,
  and books slippage — but it neither owns positions nor talks the wire itself.
  (A self-contained pre-bracket design; the live engine now favours the atomic
   bracket path in 流 broker — see 注.)

要 Require ┊ a Gateway (broker) with .place_order / .place_stop_limit /
          ┊   .cancel_order and a .symbol attribute
          ┊ optionally an audit sink exposing .log_order(**kw)
出 Provides┊ class OrderManager — request builders + place / cancel / query
          ┊ class OrderRequest — immutable, self-validating order intent
          ┊ enum OrderEvent — CREATED·SUBMITTED·PARTIAL_FILL·FILLED·
          ┊   CANCELLED·REJECTED·EXPIRED  (audit vocabulary)

─── 部  Modules used ─────────────────────────────────────────────────────
   dataclasses              ┊ @dataclass(slots=True) · field(default_factory)
   datetime                 ┊ timestamps — submitted_at · filled_at via _ts()
   typing                   ┊ Optional · TYPE_CHECKING (Gateway import guard)
   enum                     ┊ Enum base for OrderEvent
   config.models            ┊ OrderRecord · OrderSide · OrderType · OrderStatus
   execution.broker (Gateway)┊ type-only import; the live execution target

─── 算  Algorithm · signal → request → broker → record ────────────────────
 Require: a Gateway gw (connected, knows its .symbol), optional audit
 Ensure : every placed order is registered, audited at SUBMITTED, and — on a
          truthy fill price — stamped FILLED with avg price, qty, slippage.

  1: OrderManager(broker, audit=None)         ▷ _order_registry ← {} ; _ts ← datetime.now
     │                                           _order_counter ← 0 ; _symbol ← broker.symbol
  2: ── build a request ──  (caller picks one shape)
  3:   market_entry(side, qty, signal_price)  ▷ id "MKT_n_SYM" ; MARKET ; algo BREAKOUT
  4:   limit_entry(side, qty, limit, signal)  ▷ id "LMT_n_SYM" ; LIMIT  ; algo BREAKOUT
  5:   stop_limit_exit(side, qty, entry_price, ▷ id "SL_n_SYM" ; STOP_LIMIT ; algo STOP_LOSS
     │     stop_pct=0.001, trigger_offset=0.05)
     │       trigger ← round(entry × (1 − stop_pct), 2)
     │       limit   ← round(trigger − trigger_offset, 2)   ▷ slippage buffer below trigger
  6:   limit_reentry(side, qty, breakout, sig) ▷ id "RE_n_SYM" ; LIMIT at prior high ; algo REENTRY
  7:   each builder → _next_id(prefix)         ▷ ++_order_counter ; "{prefix}_{n}_{symbol}"
  8:   OrderRequest.__post_init__()            ▷ validate: LIMIT needs limit_price ;
     │                                           STOP needs stop_price ; STOP_LIMIT needs both
     │                                           — raises ValueError on a malformed shape
  9: ── await place_order(request) ──          ▷ the one execution path
 10:   build OrderRecord(status=SUBMITTED, submitted_at=_ts(), …)
 11:   _order_registry[order_id] ← record      ▷ registry is the in-memory book
 12:   if audit → log_order(event="SUBMITTED", state="ORDER_ENTRY", pos="FLAT")
 13:   if order_type is STOP_LIMIT:
     │       filled ← await broker.place_stop_limit(side, qty, stop, limit, order_id)
 14:   else:
     │       filled ← await broker.place_order(side, qty, order_type, limit_price)
 15:   if filled:                              ▷ truthy fill price ⇒ booked
     │       record.status ← FILLED ; filled_at ← _ts()
     │       avg_fill_price ← filled ; filled_qty ← qty
 16:       slippage ← filled − signal_price    ▷ sign-flipped for SELL (cost is positive)
     │       if audit → log_order(event="FILLED", fill_price, slippage,
     │                            commission=record.calculate_commission())
 17:   return record                           ▷ pending (async) fills stay SUBMITTED
 18: ── await cancel_all() ──                  ▷ sweep the book
     │       for each SUBMITTED record → broker.cancel_order(id) ; status ← CANCELLED
     │       audit CANCELLED ; return count cancelled
 19: ── query (sync) ──  get_order(id) · get_pending_orders() · get_filled_orders()
     │       .stats → {total, filled, pending, cancelled}   ▷ derived from registry scan

─── 関  Functions / classes defined ──────────────────────────────────────
   enum  OrderEvent                       audit lifecycle vocabulary (7 members)
   class OrderRequest  (dataclass, slots) immutable order intent
     __post_init__                        shape validation by order_type → ValueError
   class OrderManager
     lifecycle      __init__              wires broker · audit · registry · counter · symbol
     ids            _next_id(prefix)      monotonic "{prefix}_{n}_{symbol}"
     builders       market_entry          MARKET  · BREAKOUT
                    limit_entry           LIMIT   · BREAKOUT
                    stop_limit_exit       STOP_LIMIT · STOP_LOSS (trigger / limit math)
                    limit_reentry         LIMIT at prior high · REENTRY
     execute        place_order (async)   the request→broker→record spine + audit
                    cancel_all  (async)   cancel every SUBMITTED, audit, return count
     query          get_order             registry lookup by id → OrderRecord|None
                    get_pending_orders    SUBMITTED ∪ PARTIAL
                    get_filled_orders     FILLED
                    stats (property)      counts by status

─── 変  Variables / state created ────────────────────────────────────────
   broker            Gateway       the execution target (may be None → _symbol "")
   audit             obj|None      optional .log_order sink
   _order_registry   dict[str,     the in-memory order book — every minted order
                       OrderRecord]   keyed by order_id, mutated in place on fill/cancel
   _ts               callable      datetime.now bound once at construction
   _order_counter    int           monotonic id sequence (per OrderManager instance)
   _symbol           str           broker.symbol, baked into every order_id
   OrderRequest      dataclass     order_id·symbol·side·qty·order_type·limit_price·
                                   stop_price·signal_price·algo·timestamp

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   broker.place_order · broker.place_stop_limit · broker.cancel_order   (→ 流 broker)
   broker.symbol
   OrderRecord(...) · record.calculate_commission()                     (→ 流 models)
   audit.log_order(event, order_id, side, qty, …)
   datetime.now (via _ts)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   strategy.engine  Engine._force_market_exit  ▷ calls place_order for a forced exit
                                                  (→ 流 engine, broker.py:1358 site)
   test_order_cycle.test_order_cycle            ▷ full mint→place→fill cycle coverage
   ── note: no module imports order_manager at file scope (importers_of = 0);
      reach is through the Engine call site above, not a top-level import.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pure shaping layer.  It mints, validates, books and audits — it does NOT
     own positions (Engine does) nor execute the wire (Gateway does).
   • Registry is the book.  _order_registry is in-memory and per-instance; it is
     not persisted here — durability lives in [[fill_ledger]] / [[persistence]].
   • Truthy-fill contract.  A FILLED stamp requires a truthy fill price back from
     the broker; async / resting orders return still-SUBMITTED records.
   • Slippage sign.  Computed against signal_price and negated for SELL so that
     "worse fill" always reads as a positive cost.
   • Validation up front.  OrderRequest.__post_init__ rejects a malformed shape
     (missing limit/stop for its type) before it can ever reach the broker.
   • Design lineage.  This is the original pre-bracket order layer.  The live
     engine now arms an atomic parent+child BRACKET via [[broker]]
     (place_bracket_buy_stop_market) rather than a free-standing stop_limit_exit
     — so stop_limit_exit / limit_reentry here are the legacy single-order form.
   • Links:  order types / records → [[models]] · execution wire → [[broker]] ·
     position owner → [[engine]] · durable fills → [[fill_ledger]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
