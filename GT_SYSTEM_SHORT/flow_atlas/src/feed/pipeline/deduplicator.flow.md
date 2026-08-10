━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 39 ·  src/feed/pipeline/deduplicator.py
  the gate that lets each logical tick through exactly once
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  236 lines · 2 classes (DedupConfig · Deduplicator) · 11 methods
  one stage in the feed pipeline (community: feed-order spine). A pure,
  stateful filter: a tick goes in, the same tick or None comes out. It
  exists so candle volume and entry triggers never double-count an echo.

要 Require ┊ a PipelineStage base (set_next / _process contract)
          ┊ a Tick (symbol · bid · ask · last · volume · timestamp)
          ┊ a wall-clock (datetime.now) to stamp + expire cache entries
出 Provides┊ class Deduplicator — a chain-of-responsibility stage
          ┊ class DedupConfig — window / precision / which fields to key on
          ┊ duplicate & unique counters · duplicate-rate · get_report()

─── 部  Modules used ─────────────────────────────────────────────────────
   dataclasses                   ┊ @dataclass(slots=True) DedupConfig
   datetime  (datetime, timedelta)┊ now() stamp · window arithmetic
   typing  (Optional)            ┊ nullable tick / config signatures
   feed.pipeline.base            ┊ PipelineStage (super) · PipelineEvent enum
   feed.handler                  ┊ Tick — the stream's unit datum

─── 算  Algorithm · one tick through the gate ────────────────────────────
 Require: a Tick from the upstream stage
 Ensure : an echo of a tick already seen inside the window is dropped;
          a genuinely new tick is remembered and passed downstream.

  1: __init__(name, config)                   ▷ slots base; _config ← config
     │                                           or DedupConfig() defaults
  2:   _ts ← datetime.now                      ▷ cached clock function
  3:   _seen ← {}                              ▷ (symbol, key) → stamp datetime
     │   _duplicates ← 0 ; _unique ← 0          ▷ running statistics
  4:   _duplicate_callback ← None              ▷ optional observer hook
  5:   _last_cleanup ← _ts()                   ▷ throttle for the sweep
  6: ── per tick ──  _process(tick)            ▷ the hot path, base calls this
  7:   _maybe_cleanup()                        ▷ first, age out the cache
     │      if (now − _last_cleanup) < 1.0 s → return  ▷ at most once/sec
     │      cutoff ← now − window_seconds
     │      _seen ← { k:v in _seen if v > cutoff }   ▷ rebuild, drop expired
  8:   key ← _make_key(tick)                   ▷ the logical-identity string
     │      "t{ts // precision_ms}"             ▷ timestamp rounded to bucket
     │      if compare_prices → append l/b/a for each >0 leg
     │      if compare_volume → append "v{volume}"
     │      join with "|"                       ▷ e.g. t17... | l101.2 | v300
  9:   cache_key ← (tick.symbol, key)          ▷ symbol-scoped identity
 10:   if cache_key in _seen:                   ▷ ECHO — already seen this cycle
     │      _duplicates += 1
     │      _emit_duplicate_event(tick, key)    ▷ → _emit_event (TICK_DUPLICATE)
     │      if _duplicate_callback → callback(tick, key)
     │      return None                          ▷ swallow it; chain stops here
 11:   else:                                     ▷ NEW — first sight in window
     │      _seen[cache_key] ← _ts()             ▷ remember, stamped now
     │      _unique += 1
     │      return tick                          ▷ pass through to next stage
 12: ── observability (off the hot path) ──
     │   get_duplicate_count / get_unique_count / get_duplicate_rate
     │   reset()      ▷ clear cache + zero counters
     │   get_report() ▷ super().get_report() + dup/unique/rate/cache_size

─── 関  Functions / classes defined ──────────────────────────────────────
   class DedupConfig (dataclass, slots)
     window_seconds · max_cache_size · timestamp_precision_ms
     compare_prices · compare_volume · allow_immediate_retransmit
   class Deduplicator(PipelineStage)
     lifecycle      __init__ · set_duplicate_callback
     hot path       _process · _make_key · _maybe_cleanup
     events         _emit_duplicate_event · _emit_event (stub: pass)
     stats          get_duplicate_count · get_unique_count · get_duplicate_rate
     control        reset · get_report

─── 変  Variables / state created ────────────────────────────────────────
   _config              DedupConfig  window / precision / key-field policy
   _ts                  callable     cached datetime.now reference
   _seen                dict         (symbol, key) → stamp; the rolling window
   _duplicates          int          echoes dropped (lifetime)
   _unique              int          ticks passed through (lifetime)
   _duplicate_callback  callable|None optional per-duplicate observer
   _last_cleanup        datetime     throttle anchor for _maybe_cleanup

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   super().__init__ (PipelineStage)     super().get_report()
   self._emit_event(PipelineEvent.TICK_DUPLICATE, …)   ▷ stub today (pass)
   datetime.now · timedelta             _duplicate_callback(tick, key)
   ▷ note: max_cache_size & allow_immediate_retransmit are declared in
     DedupConfig but not yet consulted by the algorithm — config ahead of code.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed.__init__            ▷ re-exports Deduplicator (line 61)
   feed.production          ▷ constructs + chains it into the live pipeline (line 20)
   ▷ _process itself shows no graph caller — it is invoked polymorphically
     by PipelineStage.process(), not by name. (see [[base]])

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Identity is logical, not literal.  The key buckets timestamps to
     timestamp_precision_ms (default 100ms) so jittered retransmits of the
     same quote collapse to one — but two real moves inside a bucket with
     different price/volume keep distinct keys and both pass.
   • Symbol-scoped.  Cache key is (symbol, key); two symbols never collide.
   • Window, not forever.  _maybe_cleanup evicts anything older than
     window_seconds (default 1.0s), swept at most once per second — so memory
     is bounded by the per-second tick rate, not by uptime.
   • O(1) test, O(n) sweep.  Lookup is a dict membership; the periodic
     rebuild is the only linear cost and it is rate-limited.
   • Soft cap.  max_cache_size is documented as the memory limit but is NOT
     enforced in code — the window sweep is what actually bounds _seen.
   • Fail-open shape.  A tick with all-zero price legs and compare_volume off
     keys on timestamp bucket alone — coarse, but never raises.
   • Links:  base contract → [[base]] · tick datum → [[handler]] ·
     wired into the live stream → [[production]] · downstream consumer of
     deduped ticks → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
