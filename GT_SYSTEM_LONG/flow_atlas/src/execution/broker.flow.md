━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 28 ·  src/execution/broker.py
  the broker socket — the engine's only hand on IBKR · paper or live
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  3061 lines · 2 classes (MarketData · Gateway) · ~40 methods · two free helpers
  (_paper_slippage_for_symbol · _logical_symbol_from_contract). Every order the
  engine decides leaves through here; every fill, position, and account number
  comes back through here. One Gateway per (symbol, client_id). Paper and live
  are the same surface — the `paper` flag forks each method into a local sim or
  an ib_async call.

要 Require ┊ ib_async (IB · Stock · MarketOrder · LimitOrder · StopOrder ·
          ┊ StopLimitOrder · ExecutionFilter) — imported lazily, per method
          ┊ a reachable IB Gateway / TWS socket (live) — or nothing (paper)
          ┊ config.models  ConnectionStatus · OrderType · OrderSide · Position
          ┊ assets.*  resolve · SpecRegistry · contract policies (lazy, optional)
出 Provides┊ class Gateway — connect · order placement · reconcile truth sources
          ┊ class MarketData — a single (bid/ask/last/vol) snapshot dataclass
          ┊ the bracket primitive  place_bracket_buy_stop_market
          ┊ broker-truth readers   get_our_position_via_executions · get_positions
          ┊                        fetch_open_orders · get_all_fills

─── 部  Modules used ──────────────────────────────────────────────────────
   asyncio                       ┊ wait_for(connect,8s) · Queue · create_task · sleep
   subprocess · threading        ┊ start_polling spawns a child IB + reader thread
   ib_async  (lazy, in-method)   ┊ IB · order types · ExecutionFilter — imported
                                 ┊ inside methods to dodge a top-level import cycle
   config.models                 ┊ ConnectionStatus · OrderType · OrderSide ·
                                 ┊ OrderStatus · Position
   assets  (resolve)             ┊ AssetSpec — owns contract.make() per asset class
   assets.types · .policies.tick ┊ price() · round_to_tick — paper slippage snap
   assets.forex / us_stock /     ┊ contract policies' identify() — reverse-map an
     cfds / future               ┊ ib_async Contract back to the LOGICAL ticker
   assets  SpecRegistry          ┊ cross_validate — ES-vs-MES tripwire + runtime tick

─── 算  Algorithm · the broker's day ──────────────────────────────────────
 Require: host · port · client_id · symbol · paper
 Ensure : never report [] / 0 / True on a disconnected socket — raise
          ConnectionError so the caller can't mistake "unknown" for "flat".

  1: __init__(host,port,client_id,symbol,paper)  ▷ __slots__; _status←DISCONNECTED
     │                                              _paper_positions={}; caches empty
  2: await connect()                            ▷ the boot handshake
  3:   IB(); wait_for(connectAsync, 8s)         ▷ 8s cap so a hung TWS can't wedge
     │                                              the fleet; reqMarketDataType(1)=live
  4:   reqAllOpenOrdersAsync()                  ▷ populate resting orders BEFORE the
     │                                              engine reconciles (else false-FLAT fold)
  5:   reqExecutionsAsync(ExecFilter 3-day)     ▷ A19/A52 backfill — catch fills that
     │      └ fall back to today-only on reject    landed while the engine was DOWN
     │      ▷ logs BACKFILL_NEW / BACKFILL_KNOWN per fill
  6:   _subscribe_account_summary()            ▷ ONE reqAccountSummary; event stream
     │                                              keeps _account_cache fresh (no err 322)
  7:   _get_contract()                          ▷ eager-qualify so the first order skips
     │                                              the 50–200ms ContractDetails RPC
  8:   _on_connect()                            ▷ hand control back to the engine
  9: ── place an entry bracket ── place_bracket_buy_stop_market(qty, …)  [live only]
 10:   parent = StopLimitOrder(BUY, transmit=False)  ▷ buffered client-side, held
     │      parent.orderId ← client.getReqId()       reserve id so child can ref it
 11:   await asyncio.sleep(0.05)                ▷ A75 — 50ms ingest gap kills Error 135
 12:   child  = StopOrder(SELL, parentId, transmit=True)  ▷ flushes BOTH atomically
     │      ▷ on child placeOrder failure → cancelOrder(parent) so no stuck "Transmit"
 13:   wire each leg: fillEvent · statusEvent · bracket_lifecycle · commission watcher
     │      return (parent_broker_id, child_broker_id)   ▷ paper → None (legacy 2-step)
 14: ── single orders ──  place_order → _paper_order | _live_order
     │      LIMIT→GTC+outsideRth · MARKET→DAY+outsideRth (warning 399 avoidance)
 15:   place_stop_limit → _paper/_live_stop_limit   ▷ StopLimitOrder GTC outsideRth
 16:   place_stop_market → _paper/_live_stop_market ▷ StopOrder — guarantees exit fill
 17:   each live placement stashes order.orderRef ← engine_id  ▷ survives the round-trip
     │      and maps _order_id_map[broker_id] ← engine_id      so fills report engine ids
 18: ── a fill arrives ──  trade.fillEvent → on_fill closure
 19:   read fill.execution (shares/price/execId/time), NOT orderStatus  ▷ avoids 0-price
     │      race + partial double-count; commissionReport usually None here
 20:   _on_fill(engine_id, shares, price, execId, time, ib_commission)  → engine
 21:   later: commissionReportEvent → _attach_commission_watcher fires TRUE commission
     │      ▷ dedup on execId; corrects the modeled estimate ($92→$2 on EURUSD)
 22: ── retarget the child stop ── modify_stop_trigger(order_id, new_stop, new_qty)
     │      mutate trade.order.auxPrice in place; re-placeOrder = atomic IBKR modify
     │      ▷ disconnected → raise (caller must NOT cancel+replace → orphan)
 23: ── reconcile truth sources (engine asks "what do I really hold?") ──
 24:   get_our_position_via_executions(sym, since)  ▷ A43 — sum OUR clientId fills,
     │      prefer durable _fill_ledger.net() when populated (FL4, survives 24h evict);
     │      else live-sum ib.fills() floored at `since` (FL9 phantom-short guard)
 25:   get_positions() · fetch_open_orders() · get_all_fills()  ▷ ALL raise on
     │      disconnect — []-on-disconnect was the 2026-06-06 false-fold bug class
 26:   get_fx_position_via_account_values(sym)  ▷ A42 — base-ccy cash ledger truth
 27: ── status callbacks ── _attach_status_watcher → terminal non-fill (Rejected/
     │      Cancelled/Inactive) → _on_order_status; dedup via _terminal_statuses_seen
 28:   _attach_bracket_lifecycle_logger → A57: CHILD rejected & filled==0 → cancel
     │      orphan PARENT; A65 skips this when "Cancelled" is an Error-201 modify race
 29: ── market data ── start_streaming(reqMktData) · start_polling(child-proc IB)
     │      update_price(px) sets _has_price + drains _check_pending_limits (paper)
 30: ── teardown / reset ── cancel_open_orders_for_symbol · verify_symbol_flat_at_broker
     │      flatten_position (MARKET, outsideRth) · cancel_order · cancel_all
 31: await disconnect()                         ▷ cancelAccountSummary first (free the
     │                                              slot, else err 322 on reconnect)
 32: return — socket closed; the engine restarts at step 2 and re-qualifies.

─── 関  Functions / classes defined ──────────────────────────────────────
   module helpers
     _paper_slippage_for_symbol(symbol)   spec-aware (max_slip, snap) for paper fills
     _logical_symbol_from_contract(c)     ib_async Contract → LOGICAL ticker (FX fix)
   class MarketData (dataclass, slots)    ticker · bid · ask · last · volume · timestamp
   class Gateway (slots)
     lifecycle      __init__ · connect · disconnect · _apply_async_patches (no-op)
                    set_callbacks · connected (prop) · status (prop)
     contract       _get_contract (qualify+cache+fallback sweep+cross_validate)
                    get_runtime_min_tick
     truth reads    get_price · get_positions · get_our_position_via_executions
                    get_fx_position_via_account_values · get_all_fills
                    fetch_open_orders · fetch_all_open_orders_for_symbol
     account        _subscribe_account_summary · get_account_value · get_equity
                    get_buying_power · get_cash
     place          place_order · _paper_order · _live_order
                    place_stop_limit · _paper_stop_limit · _live_stop_limit
                    place_stop_market · _paper_stop_market · _live_stop_market
                    place_bracket_buy_stop_market
     modify/cancel  modify_stop_trigger · modify_order · cancel_order · cancel_all
                    cancel_open_orders_for_symbol · verify_symbol_flat_at_broker
                    flatten_position
     fill plumbing  _execute_fill · register_existing_order
                    _attach_status_watcher · _attach_commission_watcher
                    _attach_bracket_lifecycle_logger
     market data    update_price · _check_pending_limits · start_polling
                    _process_queue · stop_polling · start_streaming · stop_streaming
     (nested closures: on_fill · on_status · on_commission_report · read_loop ·
      on_tick · _on_acct_update · _make_on_fill — wire ib_async events to callbacks)

─── 変  Variables / state created ─────────────────────────────────────────
   _status            ConnectionStatus  DISCONNECTED→CONNECTING→CONNECTED→ERROR
   _ib                IB | None         the ib_async socket; None when paper/down
   _contract          Contract | None   qualified once, cached; cleared on connect()
   _paper_positions   dict[sym,{qty,avg_cost}]   local sim ledger (paper only)
   _pending_limits    dict[oid,{side,limit,qty}] paper LIMIT orders awaiting fill
   _order_id_map      dict[broker_id,engine_id]  so fills report the engine's id
   _terminal_statuses_seen  set         dedup repeated terminal status events
   _commission_exec_ids_seen set        dedup commission reports across replays
   _account_cache     dict[tag,float]   event-fed; get_*() are pure dict reads
   _runtime_min_tick  float | None      venue's reported tick (beats spec default)
   _fill_ledger       FillLedger | None  read-only borrow from engine (FL4)
   _last_price·_has_price  float·bool   _has_price = "real tick seen?" (no $100 sentinel)
   callbacks          _on_connect · _on_disconnect · _on_fill · _on_error
                      _on_order_status · _on_commission   (wired by the engine)
   _SYMBOL_OVERRIDES · _FALLBACK_VENUES  class-level venue/currency tables

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   ib_async.IB         connectAsync · reqAllOpenOrdersAsync · reqExecutionsAsync
                       qualifyContractsAsync · placeOrder · cancelOrder · trades
                       fills · positions · openTrades · accountValues · reqMktData
                       reqHistoricalDataAsync · reqAccountSummary(Async)
   assets.resolve / SpecRegistry.cross_validate    ▷ contract.make · tick policy
   contract policies   IDEALPROForex/SMARTStock/CFD/Futures .identify()
   FillLedger.net · .count    (via the borrowed _fill_ledger)
   → fires engine callbacks: _on_fill · _on_order_status · _on_commission · _on_connect

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   strategy.engine (Engine)   ▷ the sole order writer — every place_*/modify/cancel,
                                every reconcile truth read, lands here
   run_live.py                ▷ constructs the Gateway, set_callbacks, connect/disconnect
   --reset CLI flow           ▷ cancel_open_orders_for_symbol · verify_symbol_flat ·
                                flatten_position
   dashboard / monitors       ▷ get_equity · get_buying_power · get_cash · get_positions
   (graph note: the indexed graph held a stale 1094-line sibling copy under
    .../kinshasa/ — class+method inventory matched, but its line spans/edges did
    NOT correspond to this 3061-line file, so callers above are read from the
    imports + the known engine→Gateway call surface, not invented from the graph.)

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Disconnect = unknown, never "flat".  get_positions / fetch_open_orders /
     get_all_fills / modify_stop_trigger / cancel_order / verify_symbol_flat /
     flatten_position all RAISE ConnectionError when the socket is down. Returning
     []/0/True was the 2026-06-06 false-fold + naked-cascade bug class.
   • Logical ticker, always.  _logical_symbol_from_contract is the ONLY way to match
     a contract — Forex("EURUSD") stores symbol="EUR"; a passthrough silently drops
     every FX bracket child (live 2026-06-05).
   • orderRef carries the per-cycle engine_id.  Survives ib.openTrades()/ib.fills()
     so reconcile recovers the EXACT id, not a guess — the 2026-05-27 TSLA $44k
     phantom-PnL root cause.
   • Bracket atomicity is parentId, NOT OCA.  A20→A22 reverted OCA (broke the modify
     path, Error 10326); parentId alone gives auto-cancel-on-parent-cancel. A57/A65
     and the engine's invariant sweep cover the orphan-child case.
   • Child stop is STP-MARKET, not STP-LMT.  Senior-prescribed after 2026-05-27 NVDA:
     in a gap the limit gets skipped and the position bleeds; MARKET guarantees exit.
   • Commission truth arrives late.  fillEvent's commissionReport is usually None;
     commissionReportEvent (~100–1000ms later) carries the real number → engine
     overwrites the modeled estimate.
   • Account reads are free.  ONE reqAccountSummary at connect; every get_*() is an
     O(1) cache read. 0.0 means "unknown" — the risk gate refuses, never assumes $1M.
   • Links:  orders ← [[engine]] · ledger truth → [[fill_ledger]] · ticks → [[handler]]
     P&L → [[risk]] · contracts/specs → [[assets]] · types → [[models]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
