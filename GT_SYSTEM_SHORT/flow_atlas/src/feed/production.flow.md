━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 35 ·  src/feed/production.py
  the wiring loom — FeedHandler → validators → strategy · dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  169 lines · 2 classes (PipelineEntryHandler · ProductionFeed) · 11 methods
  the seam between raw broker ticks and the trading brain. Builds the three-
  stage validator chain once, then gates every tick through it before the
  engine or the dashboard is ever allowed to see it. The fast path lives here.

要 Require ┊ a FeedHandler (IBKR socket + symbol subscription)
          ┊ a strategy callback  on_tick(Tick)  (the Engine)
          ┊ an optional dashboard callback  on_tick(Tick)
出 Provides┊ class ProductionFeed · class PipelineEntryHandler (a TickHandler)
          ┊ validated trade ticks (last>0) to the engine · all ticks to display
          ┊ rolling p50/p95/p99/max pipeline-stage latency stats

─── 部  Modules used ─────────────────────────────────────────────────────
   asyncio                       ┊ start/stop/subscribe are async pass-throughs
   bisect                        ┊ imported (latency-window tooling); unused here
   time                          ┊ perf_counter — the per-tick latency clock
   collections.deque             ┊ bounded rolling latency window (maxlen 1024)
   typing                        ┊ Callable · Optional callback signatures
   feed.handler                  ┊ FeedHandler · Tick · TickHandler (base class)
   feed.pipeline.base            ┊ PipelineChain — the validator container
   feed.pipeline.sequence        ┊ SequenceMonitor (stage 1 · out-of-order drop)
   feed.pipeline.deduplicator    ┊ Deduplicator (stage 2 · repeat-tick drop)
   feed.pipeline.fast_validator  ┊ FastValidator (stage 3 · bid<=ask, ltp>0)
   sys                           ┊ stderr for the pipeline-error print (lazy import)

─── 算  Algorithm · the tick path ─────────────────────────────────────────
 Require: FeedHandler feed, strategy_cb, optional dashboard_cb
 Ensure : the strategy NEVER acts on an unvalidated tick — validation is
          inline and upstream of every dispatch; bad ticks die silently.

  1: ProductionFeed.__init__(feed, strategy_cb, dashboard_cb)
  2:   pipeline ← PipelineChain()                ▷ built once, owned for life
  3:     add_stage(SequenceMonitor())            ▷ stage 1 · int compare
  4:     add_stage(Deduplicator())               ▷ stage 2 · dict lookup
  5:     add_stage(FastValidator())              ▷ stage 3 · bid<=ask · ltp>0
  6:   entry_handler ← PipelineEntryHandler(pipeline, strategy_cb, dashboard_cb)
  7:   feed.subscribe(entry_handler)             ▷ the handler now receives ticks
  8: await subscribe(symbol)  →  feed.subscribe_symbol(symbol)   ▷ from run_live
  9: await start()           →  feed.start()     ▷ socket live, ticks begin
 10: ── tick loop ──  FeedHandler delivers each raw Tick to:
 11:   PipelineEntryHandler.on_tick(tick)        ▷ THE FAST PATH
 12:     t0 ← perf_counter()
 13:     validated ← pipeline.process(tick)      ▷ validate FIRST, then dispatch
     │      ▷ history: strategy once ran before validation; bad ticks (zero
     │        LTP, crossed book, out-of-sequence, dupes) reached the engine.
     │        validators are sub-µs, so running inline costs ~nothing and gates all.
 14:     except → print to stderr ; validated ← None    ▷ never crash the feed
 15:     if validated is None → return None      ▷ bad tick dropped before anyone sees it
 16:     if strategy_cb and validated.last > 0 → strategy_cb(validated)
     │                                           ▷ engine gets trade ticks only (→ 流 engine)
 17:     if dashboard_cb → dashboard_cb(validated) ▷ no throttle; gated by validation (→ 流 dashboard)
 18:     elapsed_ms ← (perf_counter() − t0)×1000 ; _samples.append ; bump _max_latency
 19: ── on demand ──  get_latency_stats()        ▷ called at dashboard cadence (~300ms)
 20:     snapshot ← sorted(_samples)             ▷ O(W log W), W bounded at 1024
 21:     return p50/p95/p99 from snapshot · max sticky · count · avg (compat)
 22: await stop()  →  feed.stop()                ▷ socket down, loop ends

─── 関  Functions / classes defined ──────────────────────────────────────
   class PipelineEntryHandler(TickHandler)       ▷ the validated entry point
     __init__            bind pipeline + callbacks; open the latency window
     on_tick             validate → strategy → dashboard → record latency (HEART)
     _run_pipeline       async no-op; kept for backwards compat (pipeline now sync)
     on_error            no-op error sink
     get_latency_stats   p50/p95/p99/max/count/avg over the rolling window
       _pct              inner — index a percentile into the sorted snapshot
   class ProductionFeed                          ▷ the assembler / facade
     __init__            build PipelineChain (Seq→Dedup→Validate), wire handler
     subscribe           async → feed.subscribe_symbol(symbol)
     start               async → feed.start()
     stop                async → feed.stop()
     get_latency_stats   delegate to entry_handler.get_latency_stats()

─── 変  Variables / state created ────────────────────────────────────────
   _WINDOW             int 1024     class const — bounded rolling-window size
   pipeline            PipelineChain the three-stage validator, built once
   entry_handler       PipelineEntryHandler  subscribed to the FeedHandler
   feed                FeedHandler   the IBKR socket / symbol source
   _perf               callable      time.perf_counter — the latency clock
   _samples            deque(1024)   rolling pipeline-stage latencies (ms)
   _max_latency        float         sticky max-since-start (never windowed out)
   _strategy_cb        callable      the Engine's on_tick (trade ticks only)
   _dashboard_cb       callable|None the display's on_tick (all valid ticks)
   _last_display       float         throttle clock (reserved; not gating today)
   _pending_pipeline   None          reserved slot (legacy async path artifact)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   PipelineChain.process · .add_stage          ▷ the validator chain (→ 流 base)
   SequenceMonitor() · Deduplicator() · FastValidator()  ▷ stage construction
   FeedHandler.subscribe · .subscribe_symbol · .start · .stop   ▷ (→ 流 handler)
   _strategy_cb(validated) · _dashboard_cb(validated)   ▷ the two downstream sinks
   time.perf_counter · deque.append · sorted · print(stderr)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   run_live.py            ▷ LiveTrader.setup_feed constructs the ProductionFeed,
     │                       passes engine.on_tick + dashboard.on_tick, subscribes
   dashboard.py           ▷ imports ProductionFeed for the display-side feed wiring
   FeedHandler callback   ▷ drives PipelineEntryHandler.on_tick on every raw tick

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Validate-first.  on_tick runs the pipeline INLINE before any dispatch — the
     engine can never act on an unvalidated tick.  _run_pipeline is a dead
     async no-op left only so old callers don't break.
   • Strategy ⊂ Dashboard.  Both are gated by validation, but only last>0 (trade)
     ticks reach the strategy; the dashboard sees every valid tick (quotes too).
   • Fixed memory.  _samples is maxlen-bounded → percentile cost is O(1024 log 1024)
     per call, and that call happens at display cadence, never per tick.
   • Sticky max.  _max_latency survives window eviction — a tail outlier seen once
     stays visible; p50/p95/p99 reflect only the live 1024-sample window.
   • Never crash the feed.  A pipeline exception is printed to stderr and the tick
     is dropped (validated=None) — the socket loop keeps running.
   • Dormant state.  bisect, _last_display, _pending_pipeline are carried but not
     load-bearing today (legacy of the old throttled/async dispatch path).
   • Links:  validators → [[base]] · [[sequence]] · [[deduplicator]] · [[fast_validator]]
     ticks in → [[handler]] · strategy sink → [[engine]] · display sink → [[dashboard]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
