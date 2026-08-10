━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 01 ·  src/strategy/engine.py
  the trading state-machine — one bot · one symbol · cradle → grave
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  8210 lines · 1 class (Engine) · ~39 methods · the busiest node in the graph
  (community: feed-order / unit-order spine). Everything else exists to feed
  this file ticks and to carry out the orders it decides.

要 Require ┊ a connected Gateway, a Config, a StateStore, an AlertManager
          ┊ a FillLedger (durable per-(symbol,client) fill record)
出 Provides┊ class Engine · TradeState lifecycle · .gt_state persistence
          ┊ the single writer of orders for its (symbol, client_id)

─── 部  Modules used ─────────────────────────────────────────────────────
   ib_async                      ┊ broker socket types (indirect, via Gateway)
   config.models                 ┊ Config · TradeState · ENTRY_CUTOFF_BUFFER_MIN
                                 ┊ session_is_open · entries_allowed · ET_ZONE
   execution.broker  (Gateway)   ┊ place_bracket_buy_stop_market · cancel_order
                                 ┊ get_our_position_via_executions · fetch_open_orders
   execution.fill_ledger         ┊ FillLedger — net(), merge_broker_fills(), count()
   feed.handler / feed.production┊ tick stream (last>0 ticks drive the engine)
   infra.alerts  (AlertManager)  ┊ raise_alert(code, severity, …)  → CSV + Slack/Teams
   config.persistence (StateStore)┊ async-flushed .gt_state JSON snapshots
   assets.*  (AssetSpec)         ┊ session / sizing / price / tick policies per asset
   strategy.risk (RiskGate)      ┊ pre-trade O(1) gate (size, daily loss, consec, …)

─── 算  Algorithm · the lifecycle spine ──────────────────────────────────
 Require: Config cfg, Gateway gw, StateStore store, AlertManager alerts
 Ensure : the bot is FLAT — or LONG-with-a-resting-protective-SELL — at
          every instant; never a naked position, never a phantom.

  1: __init__(cfg, gw, store, …)              ▷ slots; _state ← IDLE; FillLedger
     │                                           opened; _started_with_position ← False
  2: await start()                            ▷ the boot sequence
  3:   gateway.connect()                      ▷ socket + market-data subscribe (→ 流 handler)
  4:   _load_state()                          ▷ restore _state, _previous_breakout_level,
     │                                           _bracket_child, _pending_stop, stop_loss_pct
  5:   _started_with_position ← _position_open ▷ FL9: chooses the execution floor below
  6:   _promote_resumable_state()             ▷ RS1: STOPPED/IDLE → WAITING_REENTRY (flat
     │                                           + breakout) else MONITORING; active states
     │                                           left untouched (mid-session restart safe)
  7:   await _reconcile_missed_fills()        ▷ FL3 replay — see 流 fill_ledger
     │      floor_ts ← last_saved_ts or _engine_started_at
     │      merge( ib.fills() 3-day backfill ∪ ledger ) floored at floor_ts
     │      ▷ catches fills that landed while the engine was DOWN
  8:   await _reconcile_position_state()      ▷ three-truths: engine vs broker vs market
     │      if broker FLAT but engine LONG → auto-correct to FLAT
     │      if ledger SHORT (naked) → CUSTOM_LEDGER_SHORT alert (refuse, never mutate)
  9:   if WAITING_REENTRY or MONITORING and entries_allowed():
     │      await _place_entry_stop_limit(trigger)   ▷ arm the bracket (→ step 12)
 10: ── tick loop ──  on every last>0 tick from the feed:
 11:   _on_tick(ltp)                          ▷ updates _prev_ltp, peak/low tracking
     │      if MONITORING and ltp crosses trigger → _place_entry_stop_limit()
     │      if IN_POSITION → track highest/lowest; SL rides at broker (no manual exit)
 12: _place_entry_stop_limit(trigger)         ▷ A37/A39 atomic guard:
     │      if _pending_stop or _entry_placing or _bracket_child → refuse (no double)
     │      else → _place_entry_stop_limit_inner()
 13:     gateway.place_bracket_buy_stop_market(…)  ▷ parent BUY STP-LMT + child SELL STP,
     │                                                transmit-chained, atomic at IBKR
     │      _bracket_child ← {child engine_id, stop_price, …}   ▷ protection ARMED
 14: ── broker callback ──  _on_order_status_change(trade)
 15:   if parent BUY FILLED:                  ▷ promote the bracket
     │      _pending_stop ← child ; _bracket_child kept ; _state ← IN_POSITION
     │      _modify_bracket_child(stop = fill × (1 − SL%))   ▷ retarget SL to real fill
 16:   if child SELL FILLED (stop hit):       ▷ the exit
     │      record P&L = (exit − entry) × qty   (→ 流 risk.record_fill, FX→USD scaled)
     │      _previous_breakout_level ← _highest_price   ▷ next cycle's re-entry trigger
     │      _state ← WAITING_REENTRY ; re-arm a fresh bracket at the peak
 17:   if SELL FILLED while engine FLAT → PHANTOM_SELL_REJECTED  ▷ A17 guard
 18:   if parent CANCELLED → A45 cancel orphan child ; if child CANCELLED → A57 cancel parent
 19: ── health-check loop ──  every ~30 s (and session controller):
 20:   _reconcile_position_state()            ▷ defense-in-depth (step 8 again)
 21:   if WAITING_REENTRY and no BUY resting → re-arm
     │      clear a STALE _bracket_child (flat + not at broker) so A39 can't lock forever
 22:   session controller:                    ▷ _entries_allowed() flips near close
     │      GT_ENTRY_CUTOFF set → cancel resting BUY in last 5 min ; default → keep it
 23: await stop()                             ▷ graceful: _state ← STOPPED, persist, leave
     │                                           positions + resting orders at broker
 24: return — the bot rests; restart resumes at step 2.

─── 関  Functions / classes defined ──────────────────────────────────────
   class Engine
     lifecycle      __init__ · start · stop · state · set_log_callback
     state restore  _load_state · _promote_resumable_state · _save_state
     reconcile      _reconcile_missed_fills · _reconcile_position_state
                    _try_active_reconnect · _try_recover_connection_status
     entry          _place_entry_stop_limit · _place_entry_stop_limit_inner
     protect        _place_protective_stop · _place_protective_stop_inner
                    _modify_bracket_child
     callbacks      _on_tick · _on_order_status_change · _track_position
     session        _session_is_open · _entries_allowed · _seconds_until_session_open
     helpers        _log · _ts · _emit_state · (… ~39 methods total — full per-method
                    sub-flows expand beneath this page as the atlas grows)

─── 変  Variables / state created ────────────────────────────────────────
   _state               TradeState   IDLE→MONITORING→IN_POSITION→WAITING_REENTRY→STOPPED
   _position_open       bool         engine's belief; reconciled against broker truth
   _pending_stop        dict|None    the resting protective SELL (promoted child)
   _bracket_child       dict|None    the bracket's SELL leg engine_id (A39 guard key)
   _previous_breakout_level float    the peak → next cycle's re-entry trigger
   _highest_price       float        running peak while IN_POSITION
   _entry_placing       bool         A37 atomic-claim flag (one bracket at a time)
   _started_with_position bool       FL9 — selects the execution-replay floor
   _engine_started_at   datetime     fresh-start floor for reconcile
   _fill_ledger         FillLedger   durable per-(symbol,client) net position truth

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   Gateway.place_bracket_buy_stop_market · .cancel_order · .modify_stop_trigger
   Gateway.get_our_position_via_executions · .fetch_open_orders · .connect
   FillLedger.net · .merge_broker_fills · .count
   RiskGate.check / record_fill        AlertManager.raise_alert
   StateStore.save                     config.models.entries_allowed / session_is_open

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   run_live.py            ▷ constructs one Engine per (symbol, client_id), runs start()
   tests/paper/*          ▷ stress_churn_equity, gt_eq_test (spawn / reconcile)
   feed.handler callback  ▷ delivers ticks into _on_tick

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Single writer.  Exactly one Engine per (symbol, port) — A79 lock refuses dupes.
   • Atomic protection.  Entry is always a BRACKET: the SELL child rests the instant
     the parent exists, so a fill can never be momentarily naked.
   • Truth order (equity).  broker positions() ⟶ trusted over the executions ledger
     when they disagree (catches splits / corporate actions / out-of-window fills).
     FX keeps the ledger as truth (positions() reports 0 after restart).  [planned]
   • Never auto-flatten.  A53 — on engine⇄broker mismatch the engine ALERTS and
     refuses; it never silently mutates a real position.
   • Links:  reconcile → [[fill_ledger]] · orders → [[broker]] · ticks → [[handler]]
     P&L → [[risk]] · alerts → [[alerts]] · persistence → [[persistence]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
