━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 37 ·  src/feed/candles/builder.py
  OHLCV aggregation — ticks in, completed candles out (multi-timeframe)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  586 lines · 4 classes · 27 functions · pure-stdlib leaf node.
  No project imports, no project callers — only feed/candles/__init__.py
  re-exports it. A self-contained candle factory: Tick → BarBuilder →
  Candle, fanned across timeframes. Quiet today; nothing in the live
  trading spine calls add_tick (the engine rides raw last>0 ticks, not bars).

要 Require ┊ a tick object with .last (price) · .volume · .timestamp (datetime)
          ┊ nothing else — no Gateway, no Config, no broker, no I/O
出 Provides┊ class Candle (OHLCV dataclass) · CandleBuilder (live tick→bar)
          ┊ AggregatedCandleBuilder (coarse-bar → fine-bar) · TIMEFRAMES registry
          ┊ align_to_timeframe / align_fn_for helpers

─── 部  Modules used ─────────────────────────────────────────────────────
   collections        ┊ deque  — rolling window of recent bars (maxlen)
   dataclasses        ┊ @dataclass(slots=True) for Candle
   datetime           ┊ datetime · timedelta — bar boundaries / alignment
   typing             ┊ Callable · Optional — callback + nullable OHLC
   threading          ┊ Lock — guards _builders / _bars under concurrent ticks

─── 算  Algorithm · tick → bar → fan-out ──────────────────────────────────
 Require: a stream of ticks, each carrying last · volume · timestamp
 Ensure : per timeframe, bars are time-aligned, gap-free in close→open,
          and emitted exactly once at completion via registered callbacks.

  1: builder = CandleBuilder(symbol, timeframes, bar_window)  ▷ default tf =
     │                                          ["1m","5m","15m","1h","1d"]
     │      per-tf deque(maxlen=bar_window) ; empty _builders ; _lock
  2: builder.on_bar(tf, cb)                    ▷ subscribe; cb appended to
     │                                            _callbacks[tf] (observer)
  3: ── per tick ──  builder.add_tick(tick)     ▷ _tick_count += 1
  4:   for tf in self.timeframes:               ▷ fan the one tick across all tf
  5:     _maybe_update_bar(tick, tf)            ▷ the core decision (under _lock)
  6:       duration ← TIMEFRAMES.get(tf)        ▷ unknown tf → return None (skip)
  7:       builder = _builders.get(tf)
  8:       if builder is None:                  ▷ first tick of this tf
     │          start ← align_to_timeframe(ts, duration)   (→ step 13)
     │          _builders[tf] ← BarBuilder(symbol, tf, start)
  9:       elif ts >= builder.end_time:         ▷ tick fell past the bar window
     │          candle ← builder.build()        ▷ seal the old bar
     │          _bars[tf].append(candle) ; _bar_counts[tf] += 1
     │          _emit_callbacks(tf, candle)      ▷ fire subscribers (→ step 12)
     │          start ← align_to_timeframe(ts, duration)
     │          _builders[tf] ← fresh BarBuilder ▷ roll into the next bar
 10:       completed ← builder.update(tick)      ▷ accumulate this tick (→ step 11)
     │          if completed: _bars[tf].append ; _bar_counts += 1 ; return it
 11: BarBuilder.update(tick)                     ▷ the OHLC accumulator
     │      tick_count += 1 ; price ← tick.last ; vol ← tick.volume or 0
     │      first_tick_time set once ; last_tick_time ← ts
     │      open is None → seed O=H=L=price ; else H=max,L=min ; close=price
     │      volume += vol
     │      if ts >= end_time → return self.build()  else None
     │      ▷ NOTE: a bar can also self-complete here, not only at step 9
 12: _emit_callbacks(tf, candle)                 ▷ for cb in callbacks: cb(candle)
     │      each wrapped in try/except → prints, never raises (one bad
     │      subscriber can't stall the feed)
 13: align_to_timeframe(ts, duration)            ▷ snap ts to bar start:
     │      >= 1d → midnight ; >= 1h → floor to hour-bucket ;
     │      else → floor to minute-bucket (minutes==0 coerced to 1)
 14: ── readout, any time ──
 15:   get_bars(tf, count, include_current)      ▷ snapshot list; optionally
     │                                              append builder.get_current()
 16:   get_current_bar(tf)                        ▷ the in-progress (incomplete) bar
 17:   get_stats()                                ▷ tick_count + per-tf bars_built
 18:   reset()                                    ▷ clear builders/bars/counts
 19: ── alternate path (offline) ── AggregatedCandleBuilder
 20:   add_source_bar(coarse)                     ▷ break a big bar into target tf:
     │      seed _current_target from source O/H/L ; fold H/L/C/V/n in ;
     │      while target_end <= source_end: seal target bar, append, open next
     │      at source.close  ▷ used for backtest fill-in, NOT the live feed
 21: return — completed candles bubble back to add_tick's caller as a list.

─── 関  Functions / classes defined ──────────────────────────────────────
   class Candle (dataclass, slots)  immutable OHLCV bar
     .range          property   high − low
     .body           property   |close − open|
     .direction      property   "bullish" / "bearish" / "doji"
     .hl_mid         property   (high + low) / 2
     .to_dict()                 compact JSON-ish dict (ts/sym/tf/o/h/l/c/v/n)
   class TimeframeConfig           per-tf alignment config
     __init__                   name · duration · align_fn  (⚠ see 注)
     .seconds        property   duration.total_seconds() as int
   align_to_timeframe(ts, dur)     module fn — snap a ts to its bar start
   align_fn_for(tf)                module fn — lambda binding tf's duration
   class BarBuilder                accumulates ONE bar from ticks
     __init__                   symbol · tf · start_time → end_time
     update(tick)               fold tick into OHLCV; return Candle if sealed
     build()                    finalize → complete=True Candle
     get_current()              snapshot → complete=False Candle
   class CandleBuilder             multi-timeframe live aggregator
     __init__                   timeframes · bar_window · deques · _lock
     on_bar(tf, cb)             register completion callback (observer)
     add_tick(tick)             fan tick across tf, return completed list
     _maybe_update_bar(tick,tf) per-tf create/roll/update under _lock
     _emit_callbacks(tf, c)     fire subscribers, swallow exceptions
     get_bars(tf, count, …)     recent bars, optionally + current
     get_current_bar(tf)        the forming bar
     get_stats()                tick + per-tf bar counts
     reset()                    clear all state
   class AggregatedCandleBuilder   coarse-bar → fine-bar (offline/backtest)
     __init__                   target_tf · source_tf · max_bars
     add_source_bar(src)        explode a source bar into target candles
     get_target_bars(count)     recent target bars

─── 変  Variables / state created ────────────────────────────────────────
   TIMEFRAMES        dict          module registry: "1s".."1w" → timedelta
   CandleBuilder._builders   dict  tf → live BarBuilder (the open bar)
   CandleBuilder._bars       dict  tf → deque[Candle] (maxlen=bar_window)
   CandleBuilder._callbacks  dict  tf → list[Callable] (subscribers)
   CandleBuilder._bar_counts dict  tf → int (lifetime bars sealed)
   CandleBuilder._tick_count int   lifetime ticks ingested
   CandleBuilder._lock       Lock  serializes _maybe_update_bar / readers
   CandleBuilder._ts         fn    cached datetime.now (unused in flow)
   BarBuilder.open/high/low/close  Optional[float] — None until first tick
   BarBuilder.start_time/end_time  the bar's [start, end) window
   AggregatedCandleBuilder._current_target  Candle|None being assembled
   AggregatedCandleBuilder._completed       deque[Candle] (maxlen=max_bars)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   stdlib only:  deque · datetime/timedelta · threading.Lock
   internal:     CandleBuilder._maybe_update_bar → align_to_timeframe
                                                 → BarBuilder.update / .build
                 add_tick → _maybe_update_bar → _emit_callbacks → cb(candle)
                 align_fn_for → align_to_timeframe (closure)
   no project modules, no broker, no Gateway, no network, no disk.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed/candles/__init__.py     ▷ the ONLY importer (re-export surface)
   add_tick / CandleBuilder     ▷ NO graph callers — not wired into the
                                   live engine (engine consumes raw ticks,
                                   not aggregated bars). Dormant / future use.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Two seal points.  A bar completes either in _maybe_update_bar step 9
     (next tick past end_time) OR inside BarBuilder.update step 11 (the
     boundary tick itself). Both append + count; step 9 also fires callbacks.
   • Callback isolation.  _emit_callbacks try/excepts each subscriber and
     only prints — a throwing callback never propagates into the feed.
   • Thread-safe writes.  _maybe_update_bar + all readers hold _lock; safe
     under a multi-threaded tick source.
   • ⚠ Latent bug — TimeframeConfig.__init__ (line 111) references `align_fn`
     before it is a parameter; `align_fn_for` is also called with no guard.
     This class is never constructed in-tree, so the bug is inert today.
     align_to_timeframe / align_fn_for (the real path) are correct.
   • Pure leaf.  No side effects beyond memory + a print; deterministic given
     the tick stream. Safe to unit-test in isolation.
   • Links:  tick source → [[handler]] · the bot that ignores these bars →
     [[engine]] · package surface → [[__init__]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
