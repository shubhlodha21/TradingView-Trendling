━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 34 ·  src/feed/handler.py
  the central dispatcher — raw IBKR ticks → normalized Tick → subscribers
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  823 lines · 5 classes · 2 free funcs · ~30 methods · the mouth of the feed
  community. One FeedHandler owns the IBKR subscriptions and fans every tick
  out to registered TickHandlers (Observer / pub-sub). Asset-aware at the
  edge: it asks AssetSpec what contract to build and which tick-by-tick mode
  to request, then speaks one uniform Tick to everyone downstream.

要 Require ┊ a live ib_async.IB socket · a ConnectionManager (heartbeat)
          ┊ src.assets.resolve(symbol) → AssetSpec (contract + asset_class)
          ┊ at least one subscribed TickHandler to receive anything
出 Provides┊ class FeedHandler (subscribe · subscribe_symbol · start · stop)
          ┊ @dataclass Tick — the normalized, LTP-first market datum
          ┊ MessageType enum · TickFilter · TickHandler ABC · ErrorHandler

─── 部  Modules used ─────────────────────────────────────────────────────
   asyncio                       ┊ get_running_loop · create_task · sleep · Task
   math                          ┊ isnan / isinf guards in _safe_int/_safe_float
   abc                           ┊ ABC · abstractmethod  (TickHandler contract)
   dataclasses                   ┊ @dataclass(slots=True) · field  (Tick · TickFilter)
   datetime                      ┊ datetime.now → each Tick.timestamp (UTC)
   enum                          ┊ Enum  (MessageType)
   typing / collections          ┊ Callable · Optional · Any · defaultdict
   ib_async  (lazy, in-func)     ┊ Stock — legacy contract fallback
   src.assets  (lazy, in-func)   ┊ resolve → spec.contract.make · AssetClass FX gate
   os  (lazy, in-func)           ┊ GT_DISABLE_TICKBYTICK operator lever

─── 算  Algorithm · raw socket → uniform tick ────────────────────────────
 Require: a connected IB, an AssetSpec per symbol, ≥1 TickHandler
 Ensure : every consumer sees the SAME normalized Tick exactly ONCE, with
          prev_* carried forward so crossing-detection downstream is correct.

  1: FeedHandler(ib, connection_manager)        ▷ slots; empty handler list;
     │                                             _next_req_id ← 1; counters ← 0
  2: subscribe(handler)                          ▷ append TickHandler (dedup); pub-sub roster
  3: await subscribe_symbol(symbol)              ▷ one reqId per symbol
  4:   resolve(symbol) → spec.contract.make()    ▷ D2-PM asset-aware contract
     │      except → legacy Stock(NSE/INR for INFY, else SMART/USD)
  5:   await ib.qualifyContractsAsync(contract)  ▷ async-only; nest_asyncio removed (3.14)
     │      if not qualified → print + return -1  ▷ symbol silently absent from feed
  6:   ticker ← ib.reqMktData(...)               ▷ stream 1: BBO + cumulative volume
  7:   tbt_mode ← 'AllLast'                       ▷ equity/futures: every trade prints
     │      if spec.asset_class ∈ {FX_CASH,FX_CFD} → 'BidAsk'  ▷ FX has NO last-trade
  8:   if GT_DISABLE_TICKBYTICK and mode=='AllLast' → SKIP stream 2
     │      ▷ escapes IBKR Err 10190 at fleet scale (>~30 bots); BBO `last`
     │        still drives the breakout engine. NEVER skipped for FX.
  9:   else tbto ← ib.reqTickByTickData(contract, tbt_mode, 0, True)  ▷ stream 2
     │      except → tbto ← None, fall back to BBO only
 10:   record _symbol_to_reqid / _reqid_to_symbol / _subscriptions / _tbto_tickers
 11: await start()                               ▷ event-driven, no queue
 12:   for each BBO ticker → _setup_bbo_callback(reqId, ticker, symbol)
 13:   for each tbto ticker → _setup_tbto_callback(reqId, tbto, symbol)
 14:   while _running: await sleep(1.0)          ▷ keep-alive; callbacks do the work
 15: ── BBO event ──  ib_async fires ticker.updateEvent
 16:   on_bbo_update(ticker)                      ▷ _ticks_received += 1
     │      loop.create_task(_dispatch_bbo_tick(...))  ▷ sync→async hand-off
 17:   _dispatch_bbo_tick(reqId, ticker, symbol)
     │      seed prev_last/prev_bid/prev_ask from _last_ticks[symbol]
     │      build Tick(... _safe_float/_safe_int ...) tick_type=TICK
     │      _last_ticks[symbol] ← tick            ▷ next delta seed
     │      for handler in _handlers if enabled & filter.matches:
     │          handler.on_tick(tick); _ticks_dispatched += 1
     │          on exception → _errors += 1; handler.on_error(e)
 18: ── tick-by-tick event ──  tbto.updateEvent with tickByTicks populated
 19:   on_tbto_update(tbto)                        ▷ loop.create_task(_dispatch_trade_ticks)
 20:   _dispatch_trade_ticks(reqId, tbto, symbol)
     │      for tbto in tbto_ticker.tickByTicks:
     │        is_bidask = has bidPrice & no price
     │        BidAsk (FX): bid/ask per tick; last ← carried prev_last (no LTP);
     │                     tick_type=QUOTE; advance prev_bid/ask seeds
     │        AllLast (eq): price/size/exchange/conditions; tick_type=TRADE;
     │                      prev_last ← tick.last
     │        _last_ticks[symbol] ← tick
     │        SINGLE dispatch pass  ▷ a duplicate loop once double-fired every
     │                                trade (corrupted prev_ltp, doubled audit)
     │        suppress "event loop" errors (not actionable) before on_error
 21: await stop()                                 ▷ _running ← False; cancel _dispatch_task
 22: unsubscribe_symbol / unsubscribe             ▷ cancelMktData + cancelTickByTickData,
     │                                               purge all four maps + _last_ticks

─── 関  Functions / classes defined ──────────────────────────────────────
   _safe_int(val) → int                  module helper · NaN/inf/None → 0
   _safe_float(val) → float              module helper · NaN/inf/None → 0.0
   class MessageType(Enum)               TICK·TRADE·QUOTE·TICK_*·ORDER_UPDATE·ERROR·…
   class Tick  (@dataclass slots)        the normalized datum (LTP-first)
     spread()              ask − bid (0 if either side missing)
     mid_price()           midpoint of bid/ask, else last
     is_complete()         last>0 or full bid&ask present
     is_odd_lot()          'I' in last_conditions
     is_regular_trade()    'F' in last_conditions
     ltp_change()          last − prev_last (0 if no prior)
     __repr__()            compact debug line
   class TickFilter  (@dataclass slots)  subscription criteria
     matches(tick)         symbol · type · size · full-tick gates → bool
   class TickHandler(ABC)                consumer contract
     __init__(name, filter)  on_tick(abstract)  on_error(abstract)
     enable · disable · is_enabled(prop)
   class ErrorHandler(TickHandler)       default: ignore ticks, print errors
   class FeedHandler                     the dispatcher
     __init__ · is_running(prop) · stats(prop)
     subscribe · unsubscribe                       handler roster
     subscribe_symbol · unsubscribe_symbol · get_ticker   IBKR subscriptions
     start · stop                                  lifecycle
     _setup_bbo_callback (→ on_bbo_update)         BBO hook
     _setup_tbto_callback (→ on_tbto_update)       tick-by-tick hook
     _process_tick_queue                           legacy queue path (unused; start() is direct-dispatch)
     _dispatch_bbo_tick · _dispatch_trade_ticks    normalize + fan-out

─── 変  Variables / state created ────────────────────────────────────────
   _ib                    IB             ib_async socket (broker)
   _conn                  ConnMgr        heartbeat / connection manager
   _ts                    callable       datetime.now (cached timestamp fn)
   _handlers              list[TickHandler]   subscriber roster, dispatch order
   _symbol_to_reqid       dict[str,int]  symbol → IBKR reqId
   _reqid_to_symbol       dict[int,str]  reverse map (callbacks resolve symbol)
   _next_req_id           int            monotonic reqId allocator (from 1)
   _subscriptions         dict[int,Any]  reqId → BBO ticker
   _tbto_tickers          dict[int,Any]  reqId → tick-by-tick ticker (or None)
   _last_ticks            dict[str,Tick] per-symbol prev tick → prev_* seeds
   _ticks_received        int            stat: callbacks fired
   _ticks_dispatched      int            stat: handler.on_tick calls
   _errors                int            stat: dispatch failures
   _running               bool           start/stop gate for the keep-alive loop
   _tick_queue            Queue|None     legacy; created lazily, unused in direct dispatch
   _dispatch_task         Task|None      cancelled on stop()

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   ib.qualifyContractsAsync · reqMktData · reqTickByTickData
   ib.cancelMktData · cancelTickByTickData          (unsubscribe path)
   src.assets.resolve → spec.contract.make           (asset-aware contract)
   ib_async.Stock                                    (legacy fallback)
   asyncio.get_running_loop · create_task · sleep
   handler.filter.matches · handler.on_tick · handler.on_error  (each subscriber)
   _safe_int · _safe_float                           (field normalization)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed.production (ProductionFeed.subscribe)  ▷ wraps FeedHandler; calls subscribe_symbol
   feed.pipeline.*  (base · normalizer · validator · fast_validator ·
                     deduplicator · sequence)  ▷ all import Tick / MessageType
   feed.cache · feed.__init__                  ▷ re-export Tick / FeedHandler
   strategy.engine                             ▷ consumes ticks via a TickHandler
   run_live.py · dashboard.py                  ▷ construct + wire the live feed
   test_feed.py · test_live.py · test_nvda_debug.py  ▷ harness drivers

─── 注  Notes · invariants ───────────────────────────────────────────────
   • One Tick, one pass.  Each tick fans out to every matching handler EXACTLY
     once — the removed duplicate dispatch loop is a load-bearing fix (it had
     doubled tick counts, audit rows, and corrupted prev_ltp crossing detection).
   • prev_* is the contract.  _last_ticks seeds prev_last/prev_bid/prev_ask so
     downstream crossing detection works; FX QUOTE ticks carry last forward
     (spot FX has no last-trade price) — the engine still reads bid/ask.
   • Asset-aware at the edge.  Contract + tbt_mode come from AssetSpec, not
     hardcodes; FX → 'BidAsk' (10189 otherwise), equity/futures → 'AllLast'.
   • Fleet lever.  GT_DISABLE_TICKBYTICK drops the scarce tick-by-tick request
     for AllLast symbols to dodge Err 10190 at >~30 bots — never for FX.
   • Single-loop assumption.  All methods expect one asyncio loop; sync IBKR
     callbacks hand off via create_task and silently no-op if no loop is running.
   • Links:  pipeline stages → [[production]] · prev-tick cache → [[cache]]
     consumer → [[engine]] · package re-exports → [[__init__]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
