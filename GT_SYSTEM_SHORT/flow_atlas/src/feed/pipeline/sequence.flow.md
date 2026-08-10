━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 42 ·  src/feed/pipeline/sequence.py
  the sequence-watcher — one pipeline stage that guards tick continuity
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  328 lines · 2 classes (GapInfo · SequenceMonitor) · 11 methods
  a Chain-of-Responsibility link: it watches the per-symbol tick ordering,
  records gaps, flags replays / out-of-order ticks, and passes the tick on.
  Pure data-integrity. It never blocks a tick — every path returns the tick.

要 Require ┊ a Tick stream (symbol, timestamp, req_id) from the feed handler
          ┊ a PipelineStage base (name, _stats, set_next, get_report)
出 Provides┊ class SequenceMonitor (a pipeline stage) · class GapInfo (record)
          ┊ gap / replay / out-of-order detection · get_gaps · get_report

─── 部  Modules used ─────────────────────────────────────────────────────
   dataclasses                   ┊ @dataclass(slots=True) for GapInfo
   datetime                      ┊ datetime.now (cached as _ts) · timedelta window
   typing                        ┊ Optional[...] hints
   feed.pipeline.base            ┊ PipelineStage (super) · PipelineEvent (event enum)
   feed.handler                  ┊ Tick (the unit of flow: .symbol .timestamp .req_id)

─── 算  Algorithm · per-tick sequence check ──────────────────────────────
 Require: a Tick with symbol, timestamp, req_id
 Ensure : the tick is always returned (never dropped); gaps/replays/out-of-
          order are recorded + surfaced via events + callbacks, side-effect only.

  1: __init__(name, max_gap=100, oo_window=5s, detect_replays=True)
     │                                       ▷ slots via base; _ts ← datetime.now (cached)
     │      _last_seq{} _pending{} _gaps[]   ▷ plain dicts/lists for speed
     │      _gap/_replay/_out_of_order_callback ← None  (wired by set_* setters)
  2: ── per tick ──  _process(tick)          ▷ the base PipelineStage calls this hook
  3:   symbol ← tick.symbol
  4:   seq ← _make_sequence(tick)            ▷ NOT IBKR tickId — a synthetic proxy:
     │                                          int(timestamp×1000) + req_id  (ms + req_id)
  5:   if detect_replays:                    ▷ replay / out-of-order branch
     │      last ← _last_seq.get(symbol, 0)
     │      if seq == last → TICK_DUPLICATE   ▷ exact replay: emit, fire _replay_callback,
     │          │                                accept, do NOT advance _last_seq, return tick
     │      elif seq < last → TICK_REJECTED   ▷ out-of-order: emit (reason out_of_order),
     │                                           fire _out_of_order_callback, STILL accept,
     │                                           advance _last_seq ← seq, return tick
  6:   ── gap branch ──  last ← _last_seq.get(symbol, 0)
  7:   if last > 0 and seq > last:
     │      gap_size ← seq − last
     │      if gap_size > 1:                  ▷ a hole in the sequence
     │          build GapInfo(expected=last+1, actual=seq, first/last_missing, detected_at)
     │          _gaps.append(gap)
     │          emit TICK_GAP_DETECTED ; fire _gap_callback(gap)
     │          if gap_size > max_gap → _stats.last_error ← "Large gap: …"  ▷ warn only
  8:   _last_seq[symbol] ← seq               ▷ advance the per-symbol cursor
  9:   _process_pending(tick, symbol, seq)   ▷ release any held out-of-order ticks
     │      drop entries older than _out_of_order_window (cutoff = now − window)
     │      partition into ready (seq ≤ current) vs still_pending; keep still_pending
     │      ▷ NOTE: ready ticks are sorted but NOT re-injected — stub ("pass") today
 10:   return tick                           ▷ ALWAYS — this stage is observe-only

 ── query side (no flow, read the records) ──
 11: get_gaps(symbol?)      ▷ filtered copy of _gaps
 12: get_last_sequence(sym) ▷ the cursor for one symbol (0 if unseen)
 13: reset(symbol?)         ▷ drop one symbol, or clear all + reset_stats()
 14: get_report()           ▷ base report + gaps_detected, last_gap, symbols_tracking

─── 関  Functions / classes defined ──────────────────────────────────────
   @dataclass GapInfo
     fields  symbol · expected_seq · actual_seq · gap_size · detected_at
             first_missing · last_missing   (slots=True, immutable-ish record)

   class SequenceMonitor(PipelineStage)
     init         __init__ — config + per-symbol state + callbacks
     wiring       set_gap_callback · set_replay_callback · set_out_of_order_callback
     hook         _process — the per-tick check (replay → out-of-order → gap → advance)
     internals    _make_sequence — synthetic seq = ms(timestamp) + req_id
                  _process_pending — age-out + partition held ticks (re-inject is a stub)
                  _emit_event — pipeline-event sink (stub: "pass" today)
     query        get_gaps · get_last_sequence · reset · get_report

─── 変  Variables / state created ────────────────────────────────────────
   _max_gap              int            warn threshold; gap above this → _stats.last_error
   _out_of_order_window  timedelta      age-out horizon for _pending (default 5 s)
   _detect_replays       bool           gates the replay / out-of-order branch
   _ts                   callable       cached datetime.now (avoids attr lookup per tick)
   _last_seq             dict[str,int]  per-symbol sequence cursor (the heart of state)
   _pending              dict[str,list] per-symbol held out-of-order ticks (seq,tick,at)
   _gaps                 list[GapInfo]  append-only gap history for reporting
   _gap/_replay/_out_of_order_callback  optional observer hooks (None until set)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   PipelineStage.__init__ · .get_report · .reset_stats   ▷ via super() (→ 流 base)
   self._make_sequence · self._process_pending · self._emit_event
   self._gap_callback / _replay_callback / _out_of_order_callback  (if wired)
   datetime.now (as _ts) · timedelta · GapInfo(...)
   _stats.last_error / _stats.ticks_rejected               ▷ base-owned stats record

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed/production.py     ▷ imports SequenceMonitor; builds it into the live pipeline
   feed/__init__.py       ▷ re-exports SequenceMonitor / GapInfo from the package
   (the pipeline driver calls .process(tick) on the base, which dispatches _process)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Observe-only.  Every code path returns the tick — this stage never drops or
     rewrites data. It only records and signals. Safe to insert anywhere in the chain.
   • Synthetic sequence.  IBKR exposes no per-tick sequence number, so _make_sequence
     fabricates one from timestamp(ms)+req_id. Two ticks in the same ms from the same
     req_id collide → read as a replay. The "sequence" is a proxy, not a true counter.
   • Replays don't advance.  A duplicate seq is accepted but _last_seq is left intact,
     so the next real tick still measures the gap from the last DISTINCT sequence.
   • Out-of-order is accepted.  seq < last is flagged but kept (valid data, late) and
     it advances the cursor backward — by design, late data is still data.
   • Stubs.  _emit_event and the re-inject tail of _process_pending are placeholders
     ("pass"); events/callbacks are the only live signal today. The _pending map is
     populated nowhere in this file, so _process_pending is effectively dormant.
   • Links:  base stage → [[base]] · tick source → [[handler]] · live wiring → [[production]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
