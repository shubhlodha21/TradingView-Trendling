━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 25 ·  src/config/models.py
  the shared vocabulary — enums · dataclasses · the session clock · the
  commission arithmetic that every other file speaks in
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  807 lines · 7 enums/dataclasses · 1 service class (OrderRegistry) · 7 module
  functions · pure types + arithmetic, almost no I/O (only os.environ + a lazy
  import into assets). The leaf everyone imports — engine, broker, risk,
  order_manager, audit, dashboard, run_live all read their nouns from here.

要 Require ┊ os.environ (config knobs) · stdlib datetime/zoneinfo/enum/dataclass
          ┊ (lazy, inside calculate_commission) src.assets.resolve — AssetSpec
出 Provides┊ Config · Order · Position · TradeContext · OrderRecord · OrderRegistry
          ┊ enums ConnectionStatus / OrderType / OrderSide / OrderStatus / TradeState
          ┊ session clock — session_is_open · entries_allowed · seconds_until_*
          ┊ commission — calc_ibkr_commission · _base · _regulatory ; ET_ZONE

─── 部  Modules used ─────────────────────────────────────────────────────
   datetime · zoneinfo       ┊ ET wall-clock math; ZoneInfo("America/New_York")
                             ┊ → ET_ZONE (DST-aware) · fallback fixed UTC-5
   enum (str, Enum)          ┊ the five string-valued state/type enums
   dataclasses               ┊ slots=True dataclasses (Config, Order, …)
   os                        ┊ every default is GT_*-overridable via env
   threading  (lazy)         ┊ OrderRegistry._lock — guards mutating ops
   src.assets.*  (lazy)      ┊ resolve(symbol) → AssetSpec.commission.estimate
                             ┊ — only entered for non-equity in calculate_commission

─── 算  Algorithm · two engines live here: the session clock + the till ─────
 Require: a tz-aware UTC `now` (or None → now); for fees a qty, price, side.
 Ensure : session predicates agree on one ET window; commission never
          over-bills tiny trades nor mis-prices non-equity assets.

  1: module load                              ▷ ET_ZONE ← ZoneInfo or UTC-5 fallback
     │                                          _ET alias kept for session helpers
  2:   read SESSION_*_ET, ENTRY_CUTOFF_BUFFER_MIN from env  ▷ RTH 09:30–16:00, buffer 0
  3:   read IBKR_TIERED_RATE / _MIN / _MAX_FRACTION + regulatory rate consts
  4: ── session clock ──  session_is_open(now)
     │      to ET ; weekday ≥ 5 → False ; start ≤ _et_minutes_of_day < end
  5:   entries_allowed(now)                   ▷ stricter gate
     │      if not session_is_open → False
     │      else minutes-of-day < (end − ENTRY_CUTOFF_BUFFER_MIN)
     │      ▷ engine consults this twice: refuse new BUY · cancel resting BUY at cutoff
  6:   seconds_until_session_open(now)         ▷ sleep target when market shut
     │      0 if open ; else next 09:30 ET, skip Sat/Sun → seconds
  7:   seconds_until_entry_cutoff(now)         ▷ wake at 15:55 ET to flip the gate
     │      0 if outside RTH or already past cutoff
  8: ── the till ──  calc_ibkr_commission(qty, price, side)
  9:   calc_ibkr_base_commission(qty, price)   ▷ raw = qty×0.0035
     │      floor = max($0.35, raw)  ;  final = min(floor, trade×1%)
     │      ▷ cap wins over floor — fixes the ~10× over-bill on penny/tiny trades
 10:   calc_ibkr_regulatory_fees(qty, price, side)
     │      CAT + NSCC/DTC clearing (both sides) ; +SEC +FINRA TAF (SELL only)
 11:   sum → one side's all-in commission
 12: ── per-order accounting ──  OrderRecord.calculate_commission()
     │      if broker_commission > 0 → trust IBKR's penny-exact number (live)
     │      else (paper/replay) → try assets.resolve(symbol):
     │         non-equity → spec.commission.estimate(qty,price,side)  ▷ FX/futures/CFD
     │         equity / unknown / failure → calc_ibkr_commission (legacy byte-identical)
 13: ── the registry ──  OrderRegistry.on_fill(id, qty, price, exec_id, …)
     │      under _lock: if exec_id already seen → no-op (IBKR replay dedup)
     │      filled_qty += qty ; recompute running avg_fill_price
     │      accumulate broker_commission across partials
     │      if filled_qty ≥ qty → status FILLED ; filled_at (broker ts, tz-stripped)
     │         ; commission ← calculate_commission()  (→ step 12)
 14: return — pure values; no order is ever placed from this file.

─── 関  Functions / classes defined ──────────────────────────────────────
   module fn   _et_minutes_of_day(et_dt) → int          ET hour×60+minute
   module fn   session_is_open(now_utc=None) → bool      inside RTH window?
   module fn   entries_allowed(now_utc=None) → bool      RTH minus cutoff buffer
   module fn   seconds_until_session_open(now_utc=None)  sleep-until-open
   module fn   seconds_until_entry_cutoff(now_utc=None)  sleep-until-cutoff
   module fn   calc_ibkr_base_commission(qty, price)     tiered base only
   module fn   calc_ibkr_regulatory_fees(qty, price, side) SEC/TAF/CAT/clearing
   module fn   calc_ibkr_commission(qty, price, side=BUY) base + regulatory
   enum        ConnectionStatus    DISCONNECTED…CONNECTED…RECONNECTING…ERROR
   enum        OrderType           MARKET · LIMIT · STOP · STOP_LIMIT  (defined twice;
                                   second decl "senior quant grade" wins)
   enum        OrderSide           BUY · SELL
   enum        OrderStatus         PENDING…SUBMITTED…FILLED…PARTIAL…CANCELLED…REJECTED
   enum        TradeState          IDLE…MONITORING…IN_POSITION…WAITING_REENTRY…STOPPED
   dataclass   Config              all trading knobs ; .from_env() classmethod
   dataclass   Order               lean order (the order_manager path)
   dataclass   Position            symbol/qty/avg_cost ; .unrealized_pnl(price)
   dataclass   TradeContext        persisted cycle context ; .to_dict / .from_dict
   dataclass   OrderRecord         full lifecycle record ; .calculate_commission
                                   .is_complete
   class       OrderRegistry       thread-safe, idempotent fill book
     methods   __init__ · submit · get · on_fill · on_cancel · on_reject
               get_filled_orders · total_commission · get_today_trades

─── 変  Variables / state created ────────────────────────────────────────
   ET_ZONE / _ET            tzinfo     America/New_York (DST) or fixed UTC-5
   SESSION_START/END_*_ET   int        09:30 / 16:00 ET window edges (env-tuned)
   ENTRY_CUTOFF_BUFFER_MIN  int        0 default — no cutoff (bracket protects)
   IBKR_TIERED_RATE         float      $0.0035/share base (GT_IBKR_TIERED_RATE)
   IBKR_MIN_PER_ORDER       float      $0.35 floor  ;  IBKR_MAX_FRACTION 1% cap
   _SEC/_FINRA_TAF/_CAT/_CLEARING_RATE  regulatory pass-through consts
   COMMISSION_PER_SHARE     float      0.35 legacy shim (do not use; was the bug)
   MIN_COMMISSION · ROUND_TRIP_COMMISSION   back-compat constants (deprecated)
   Config.trigger_price=0.0 · quantity=0   SENTINELS — run_live refuses to start
   OrderRegistry._orders / _seen_execs / _lock   the dedup'd, locked fill book
   OrderRecord.broker_commission  Optional   IBKR-authoritative; None → formula

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   os.environ.get                       ▷ every default knob + Config.from_env
   datetime.now / astimezone / replace  ▷ all session-clock math
   src.assets.resolve → AssetSpec.commission.estimate   ▷ lazy, non-equity fees
   calc_ibkr_commission                 ▷ OrderRecord's equity fallback
   threading.Lock                       ▷ OrderRegistry mutation guard

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   strategy/engine.py     ▷ Config · TradeState · OrderRegistry · OrderRecord ;
                            session_is_open / entries_allowed / seconds_until_*
   execution/broker.py    ▷ Order/OrderRecord types · enums for placement
   execution/order_manager.py ▷ OrderRecord · OrderSide/Type/Status
   strategy/risk.py       ▷ Config + commission for P&L gates
   config/audit.py        ▷ rebuilds OrderRecord rows from CSV
   config/loader.py · config/__init__.py ▷ re-export Config
   dashboard.py           ▷ OrderStatus · calc_ibkr_commission · session_is_open
   run_live.py            ▷ Config · TradeState (startup + state gating)
   tests/unit/*           ▷ test_order_registry · test_engine · test_risk · test_broker

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pure leaf.  No file is mutated, no order placed here — only types, the
     session clock, and arithmetic. Safe to import from anywhere.
   • Cap-over-floor.  calc_ibkr_base_commission does min(max($0.35,raw), 1%×trade) —
     the 1% ceiling beats the $0.35 minimum on tiny trades. Order matters.
   • Two OrderType decls.  Defined at line 308 and again at 332; the second
     ("senior quant grade") shadows the first. Identical members — harmless, but
     dead. [[engine]] imports the live one.
   • Sentinels are load-bearing.  trigger_price=0.0 and quantity=0 mean
     "not configured"; run_live refuses to start rather than place real orders
     at a stale default. partial_fill_chase_offset=0.0 → "reuse entry offset".
   • from_env ↔ dataclass drift.  max_position_value_usd default lives in BOTH
     the constructor (50000) and from_env; daily_loss_limit_pct differs (-0.90
     dataclass vs -0.02 env). Mismatch silently caps at the lower — keep in sync.
   • Idempotent fills.  on_fill dedups on exec_id (IBKR replays on reconnect);
     without it filled_qty would double-count and corrupt P&L.  [[fill_ledger]]
   • Multi-asset commission.  calculate_commission routes non-equity through
     [[assets]] specs (FX 0.20bps, futures $0.85/contract); equity keeps the
     byte-identical legacy formula so audit history doesn't shift.
   • Links:  session gate → [[engine]] · fees + P&L → [[risk]] · order types →
     [[broker]] · [[order_manager]] · record rebuild → [[audit]] · specs → [[assets]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
