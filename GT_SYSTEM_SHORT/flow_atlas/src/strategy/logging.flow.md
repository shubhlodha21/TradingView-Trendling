━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 49 ·  src/strategy/logging.py
  the structured-log mouth — every event becomes one line of JSON
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  241 lines · 2 classes (LogLevel · QuantLogger) · ~24 methods · a leaf node.
  Two writing paths share one record shape: a SYNC path that prints critical
  events immediately, and an ASYNC path that buffers high-frequency ticks and
  flushes them in batches so logging never stalls the engine's tick loop.

要 Require ┊ an asyncio event loop (for the async path + flush task)
          ┊ a writable TextIO sink (default sys.stdout) — ELK-friendly JSON
出 Provides┊ class QuantLogger · class LogLevel
          ┊ the engine's named event vocabulary (order_*, trade_*, strategy_*,
          ┊ connection events) + raw log/debug/warn/error + .dropped counter

─── 部  Modules used ─────────────────────────────────────────────────────
   asyncio                        ┊ Task · Lock · sleep · create_task (flush loop)
   json                           ┊ dumps — one record → one JSON line
   sys                            ┊ stdout default sink · stderr overflow warning
   collections.deque              ┊ bounded buffer (maxlen = buffer_size × 2)
   datetime.datetime              ┊ now() — cached as self._ts for fast timestamps
   typing.Optional / TextIO       ┊ sink + nullable-task hints
   enum.Enum                      ┊ LogLevel string enum

─── 算  Algorithm · the two writing paths ─────────────────────────────────
 Require: a TextIO sink, a trade_cycle_id tag, buffer_size, flush_interval_ms
 Ensure : critical events are written the instant they happen; tick-rate events
          are batched and never block; overflow is counted, never silent.

  1: __init__(output, trade_cycle_id, buffer_size=50, flush_ms=100)
     │                                   ▷ __slots__ — no per-instance __dict__
     │      flush_interval ← flush_ms / 1000.0
     │      _ts ← datetime.now            ▷ bound once, called per record
     │      _order_latencies ← {}         ▷ order_id → submit-timestamp
     │      _buffer ← deque(maxlen = buffer_size × 2)
     │      _lock ← asyncio.Lock ; _flush_task ← None ; _running ← False
     │      _dropped ← 0 ; _last_drop_warn ← 0.0   ▷ overflow diagnostics
  2: start()                              ▷ idempotent — returns if _running
     │      _running ← True ; _flush_task ← create_task(_flush_loop())
  3: ── async path ──  log_async / debug_async  (high-frequency ticks)
  4:   await _emit(level, event, **kwargs)
     │      record ← {ts, level, event, cycle_id, **kwargs} ; line ← json.dumps
     │      async with _lock:
     │        if len(_buffer) ≥ _buffer.maxlen:   ▷ deque about to evict oldest
     │          _dropped += 1
     │          if now − _last_drop_warn > 5.0:   ▷ rate-limited to once / 5 s
     │            stderr ← "buffer overflow — dropped=N … flush may be stuck"
     │        _buffer.append(line)        ▷ append regardless (deque drops oldest)
  5: ── flush loop ──  _flush_loop()  (background task)
  6:   while _running:  sleep(flush_interval) ; await _flush()
  7:     _flush():  if empty → return
     │        async with _lock:  lines ← list(_buffer) ; _buffer.clear()
     │        output.write('\n'.join(lines) + '\n') ; output.flush()
  8: ── sync path ──  log / debug / warn / error  (critical, immediate)
  9:   _emit_sync(level, event, **kwargs)
     │      record ← {ts, level, event, cycle_id, **kwargs}
     │      print(json.dumps(record), file=output, flush=True)  ▷ no buffer
 10: ── named events ──  thin wrappers over the two paths:
     │      order_submitted → stamps _order_latencies[order_id] = now ; log(…)
     │      order_filled    → latency_ms = (now − submit)×1000 ; log(…)
     │      order_cancelled · order_rejected(warn) · strategy_* · trade_*
     │      risk_rejected(warn) · connected · disconnected · connection_error
 11: set_cycle_id(cycle_id)               ▷ retag subsequent records mid-run
 12: await stop()                         ▷ shutdown: _running ← False
     │      _flush_task.cancel() ; await it (swallow CancelledError)
     │      await _flush()                ▷ final drain — no record left behind

─── 関  Functions / classes defined ──────────────────────────────────────
   class LogLevel(str, Enum)      DEBUG · INFO · WARN · ERROR
   class QuantLogger
     lifecycle      __init__ · start · stop
     flush          _flush_loop · _flush
     emit core      _emit (async, buffered) · _emit_sync (immediate)
     diagnostics    dropped (property) — overflow eviction count
     raw sync       log · debug · warn · error
     raw async      log_async · debug_async
     order events   order_submitted · order_filled · order_cancelled · order_rejected
     strategy events strategy_started · strategy_stopped · strategy_state_change
     trade events   trade_entry · trade_exit · risk_rejected
     connection     connected · disconnected · connection_error
     tagging        set_cycle_id

─── 変  Variables / state created ────────────────────────────────────────
   output             TextIO         the JSON sink (default sys.stdout)
   trade_cycle_id     str            cycle_id stamped into every record
   buffer_size        int            target batch size (50)
   flush_interval     float          seconds between flushes (0.1)
   _ts                callable       datetime.now, bound once
   _order_latencies   dict[str,float] order_id → submit ts (→ fill latency_ms)
   _buffer            deque[str]     bounded async line buffer (maxlen 2×size)
   _lock              asyncio.Lock   guards buffer append ⇄ drain
   _flush_task        Task|None      the background flush coroutine
   _running           bool           flush-loop alive flag (start/stop gate)
   _dropped           int            lines evicted by overflow (surfaced on stop)
   _last_drop_warn    float          throttle clock for the stderr warning

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   asyncio.create_task · asyncio.Lock · asyncio.sleep
   json.dumps            datetime.now            sys.stdout / sys.stderr write
   deque.append / clear  print(file=output, flush=True)
   ▷ pure leaf — depends on stdlib only; no project modules imported.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   strategy.engine        ▷ the Engine holds a QuantLogger; calls order_*/trade_*/
                          ┊ strategy_* on every state transition (→ 流 engine)
   infra.__init__         ▷ re-exports QuantLogger into the infra namespace
   run_live.py            ▷ constructs + start()/stop()s the logger per bot
   tests/unit/test_logging ▷ buffer/flush/overflow + latency coverage
   live_trading.py · test_live.py · test_paper_simulation.py   ▷ legacy harnesses

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Two paths, one shape.  _emit and _emit_sync build the IDENTICAL record
     dict {ts, level, event, cycle_id, **kwargs}; only the sink discipline
     differs (batch vs flush-now).  ELK can parse either line the same way.
   • Critical never buffers.  order/trade/strategy/connection events all go
     through the SYNC path — they survive a crash before the next flush tick.
   • Overflow is loud, not silent.  deque(maxlen) drops the OLDEST line; the
     engine wanted that visible, so _dropped counts it and stderr warns once
     per 5 s.  Read .dropped on shutdown to know if the flush task ever stalled.
   • Flush is single-owner.  Only _flush_loop drains the buffer; _emit only
     appends.  The Lock makes append⇄drain atomic across the await boundary.
   • stop() is total.  Cancels the task, swallows CancelledError, then does a
     final _flush() so no buffered line is lost at shutdown.
   • Links:  events sourced from → [[engine]] · re-exported via [[infra]] ·
     P&L numbers it logs → [[risk]] · orders it names → [[broker]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
