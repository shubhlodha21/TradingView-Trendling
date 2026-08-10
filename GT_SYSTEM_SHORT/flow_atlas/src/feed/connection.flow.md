━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 33 ·  src/feed/connection.py
  the connection heartbeat — watch the socket · on loss, repair it · backoff
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  408 lines · 3 classes (ConnectionState · ConnectionConfig · ConnectionObserver)
  + ConnectionManager · ~24 methods · a small asyncio liveness watchdog.
  Owns no trading logic — its single job is to KNOW whether the IBKR socket is
  alive and, when it dies, to bring it back (itself or via the caller).

要 Require ┊ an asyncio event loop · an ib_async.IB instance (owned here OR
          ┊ owned externally by Gateway) · a ConnectionConfig
          ┊ optional ConnectionObserver listeners for lifecycle events
出 Provides┊ ConnectionManager — connect / disconnect / reconnect / supervise
          ┊ ConnectionState enum · ConnectionConfig dataclass
          ┊ ConnectionObserver ABC (no-op default hooks to subclass)
          ┊ a never-stale heartbeat: .is_connected · .heartbeat_age_seconds

─── 部  Modules used ─────────────────────────────────────────────────────
   asyncio                       ┊ sleep · create_task · Task · CancelledError
   dataclasses                   ┊ @dataclass(slots=True) for ConnectionConfig
   datetime                      ┊ timestamps — connected_at · last_heartbeat
   enum                          ┊ ConnectionState lifecycle values
   typing                        ┊ Optional hints
   abc                           ┊ ABC · abstractmethod (observer interface)
   ib_async  (lazy, in connect)  ┊ IB() — only imported inside connect(), owner mode
   sys       (lazy, in _notify)  ┊ stderr for observer-error logging

─── 算  Algorithm · the liveness loop ─────────────────────────────────────
 Require: a ConnectionConfig (host, port, client_id, intervals, backoff caps)
 Ensure : while _running, the manager either holds a live socket OR is actively
          backing-off to re-establish one; observers always hear the truth.

  1: __init__(config)                          ▷ slots; _state ← DISCONNECTED;
     │                                           _ib ← None; _observers ← []; _ts ← now
     │                                           supervise callbacks ← None (owner mode)
  2: add_observer(obs) / remove_observer(obs)  ▷ dedup list of ConnectionObserver
  3: ── two ways to go live ──────────────────────────────────────────────
  4: OWNER MODE  await connect()               ▷ this manager owns the IB()
     │      if already CONNECTED → return True
     │      _state ← CONNECTING ; _setup_async_compat() (no-op)
  5:     from ib_async import IB ; _ib ← IB()  ▷ lazy import, set RequestTimeout
  6:     _ib.connect(host, port, clientId, readonly)   ▷ blocking ib_async call
  7:     on success → _state ← CONNECTED ; stamp _connected_at = _last_heartbeat
     │      _reconnect_attempts ← 0 ; _notify("connect", at)
  8:     _running ← True ; spawn _heartbeat_loop() as a Task   ▷ → step 12
  9:     on Exception → _state ← FAILED ; _notify("error", e) ; return False
 10: SUPERVISOR MODE  await supervise(ib, on_connect, on_reconnect?)
     │      _ib ← externally-owned IB (Gateway keeps ownership)
     │      store _supervise_connect / _supervise_on_reconnect callbacks
     │      _state ← CONNECTED ; stamp times ; _notify("connect")
 11:     spawn _heartbeat_loop() if not already running   ▷ → step 12
 12: ── _heartbeat_loop() ── every config.heartbeat_interval seconds:
     │      if not _running → break ; if _state != CONNECTED → break
 13:     ib = _ib ; if ib and ib.isConnected():
     │          _last_heartbeat ← _ts() ; _notify("heartbeat", ts)   ▷ healthy beat
 14:     else  (socket lost):
     │          _state ← RECONNECTING ; _notify("disconnect", "Lost connection")
     │          if _supervise_connect is not None:
     │              spawn _supervise_reconnect_loop()   ▷ supervisor repair → step 17
     │          else:
     │              spawn reconnect()                   ▷ owner repair → step 15
     │          break   ▷ this loop ends; the repair task restarts it on success
 15: ── reconnect() ── (owner mode) exponential backoff:
     │      if already RECONNECTING → return False ; else _state ← RECONNECTING
     │      while attempts < max (or max<=0 = infinite):
     │          attempts++ ; _notify("reconnecting", n, max)
 16:         backoff = min(initial·2^(n-1), max_backoff) ; await sleep(backoff)
     │          if await connect() → return True       ▷ re-enters step 4, re-arms loop
     │      exhausted → _state ← FAILED ; return False
 17: ── _supervise_reconnect_loop() ── (supervisor mode) same backoff shape:
     │      while attempts < max:
     │          attempts++ ; _notify("reconnecting", n, max) ; sleep(backoff)
     │          ok = await _supervise_connect()        ▷ caller re-establishes (gateway.connect)
     │          on Exception → _notify("error", e) ; ok ← False
 18:         if ok: _state ← CONNECTED ; restamp times ; _notify("connect")
     │              if _supervise_on_reconnect → await it  ▷ run_live re-subscribes / reconciles
     │              spawn _heartbeat_loop() ; return True
     │      exhausted → _state ← FAILED ; return False   ▷ caller treats as fatal
 19:     set_ib(ib)                               ▷ caller re-points _ib after each reconnect
 20: ── await disconnect(reason?) ── graceful teardown:
     │      _running ← False
 21:     if _heartbeat_task alive → cancel() AND await it   ▷ no orphan pending Task
 22:     owner mode only: if _ib.isConnected() → _ib.disconnect() ; _ib ← None
     │      (supervisor mode leaves _ib alone — Gateway owns it)
 23:     _state ← DISCONNECTED ; _notify("disconnect", reason)
 24: ── _notify(event, *args) ── for each observer: call on_<event>; swallow &
     │      log observer exceptions to stderr so one bad listener can't crash the chain

─── 関  Functions / classes defined ──────────────────────────────────────
   enum  ConnectionState        DISCONNECTED · CONNECTING · CONNECTED ·
                                RECONNECTING · FAILED
   data  ConnectionConfig       host · port · client_id · readonly ·
                                request_timeout · heartbeat_interval ·
                                max_reconnect_attempts · initial/max_backoff
   ABC   ConnectionObserver     on_connect · on_disconnect · on_reconnecting ·
                                on_error · on_heartbeat  (all no-op defaults)
   class ConnectionManager
     lifecycle      __init__ · connect · disconnect · reconnect
     supervisor     supervise · set_ib · _supervise_reconnect_loop
     loop           _heartbeat_loop
     observers      add_observer · remove_observer · _notify
     properties     state · is_connected · ib · connected_at ·
                    last_heartbeat · heartbeat_age_seconds
     internal       _setup_async_compat (no-op; was nest_asyncio.apply)

─── 変  Variables / state created ────────────────────────────────────────
   _config              ConnectionConfig  host/port/backoff/interval policy
   _state               ConnectionState   DISCONNECTED→CONNECTING→CONNECTED→
                                          RECONNECTING→(CONNECTED|FAILED)
   _ib                  IB|None           the socket; OWNED (owner) or BORROWED
                                          (supervisor — Gateway owns it)
   _connected_at        datetime|None     when the current session went live
   _last_heartbeat      datetime|None     last proven-alive timestamp
   _reconnect_attempts  int               backoff counter; reset to 0 on connect
   _running             bool              the heartbeat loop's run flag
   _heartbeat_task      asyncio.Task|None the liveness loop handle
   _observers           list              registered ConnectionObserver listeners
   _ts                  callable          datetime.now (one source of time)
   _supervise_connect       callable|None caller's re-establish hook (mode select)
   _supervise_on_reconnect  callable|None caller's post-reconnect re-subscribe hook

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   ib_async.IB · IB.connect · IB.isConnected · IB.disconnect   (owner mode)
   asyncio.create_task · asyncio.sleep · Task.cancel
   _supervise_connect()        ▷ caller-supplied — typically Gateway.connect
   _supervise_on_reconnect()   ▷ caller-supplied — run_live re-subscribe / reconcile
   self._notify → observer.on_connect / on_disconnect / on_reconnecting /
                  on_error / on_heartbeat

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   run_live.py            ▷ constructs ConnectionManager; uses supervise() to
                            watch the Gateway-owned IB and re-attach feeds
   fast_ltp_feed.py       ▷ owner-mode connect() for the standalone LTP feed
   src/feed/__init__.py   ▷ re-exports ConnectionManager / State / Config
   test_feed.py           ▷ unit coverage of the lifecycle + backoff

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Two modes, one loop.  OWNER mode (connect) creates & repairs its own IB();
     SUPERVISOR mode (supervise) only watches a Gateway-owned IB and delegates
     repair to caller callbacks.  _supervise_connect being None is the switch.
   • Never an orphan Task.  disconnect() cancels AND awaits the heartbeat task —
     fix for the "Task was destroyed but it is pending" warnings.
   • Backoff is bounded.  min(initial·2^(n-1), max_backoff); default settles to a
     reconnect every 10s at steady state (1→2→4→8→10→10…).
   • Heartbeat truth.  heartbeat_age_seconds = inf until first beat; callers can
     watchdog on it without trusting _state alone.
   • Observers are insulated.  _notify swallows + logs listener exceptions to
     stderr; one broken observer never breaks the lifecycle chain.
   • _setup_async_compat is a deliberate no-op — nest_asyncio cancels the main
     task within ~1s on Python 3.14 (eager-tasks); ib_async supports 3.14 natively.
   • Links:  socket owner → [[broker]] · re-subscribe target → [[handler]]
     supervisor host → [[run_live]] · feed namespace → [[__init__]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
