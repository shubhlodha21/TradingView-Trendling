━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 38 ·  src/feed/pipeline/base.py
  the tick pipeline spine — Chain of Responsibility for market data
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  270 lines · 3 classes (PipelineEvent · PipelineStats · PipelineStage)
  + 1 manager (PipelineChain) · pure data plumbing, no broker, no asset.
  Each stage is one responsibility; ticks flow [Tick]→[stage]→[stage]→[consumer],
  any stage may reject (return None) and stop propagation. The concrete stages
  (dedup · validate · sequence · normalize) all subclass PipelineStage here.

要 Require ┊ a Tick (from feed.handler) · nothing else — no I/O, no broker
          ┊ subclasses must implement _process(tick) → Tick | None
出 Provides┊ PipelineStage (ABC) · PipelineChain (linked-list manager)
          ┊ PipelineStats (per-stage counters) · PipelineEvent (enum)
          ┊ the contract every feed/pipeline/* stage is built on

─── 部  Modules used ─────────────────────────────────────────────────────
   abc                            ┊ ABC · abstractmethod — enforce _process()
   dataclasses                    ┊ @dataclass(slots=True) for PipelineStats
   datetime                       ┊ datetime.now — last_processed timestamp
   enum                           ┊ Enum base for PipelineEvent
   typing                         ┊ Optional · Callable — type hints only
   feed.handler                   ┊ Tick · TickHandler (the tick type flowing through)

─── 算  Algorithm · how a tick travels the chain ─────────────────────────
 Require: a built chain of PipelineStage subclasses, a Tick to push
 Ensure : a tick is either accepted by ALL stages (returned) or rejected by
          one (None) — stats always advance, an exception never escapes.

  1: build ── PipelineChain()                  ▷ empty: _stages=[] _head=_tail=None
  2:   chain.add_stage(stage)                   ▷ append; first → _head
     │      if _tail exists → _tail.set_next(stage)  ▷ link prev → new (one-way list)
     │      _tail ← stage ; return self          ▷ fluent: add_stage().add_stage()
  3: push ── chain.process(tick)                ▷ the entry point per tick
  4:   if no _head → return tick                ▷ empty chain is a pass-through
  5:   return _head.process(tick)               ▷ hand to first stage (→ step 6)
  6: PipelineStage.process(tick)                ▷ the per-stage envelope
  7:   if not _enabled → return tick            ▷ disabled stage bypasses (no count)
  8:   _stats.ticks_processed += 1
  9:   result ← self._process(tick)             ▷ subclass logic (abstract here)
     │      ▷ dedup / validate / sequence / normalize live in sibling files
 10:   _stats.last_processed ← _ts()            ▷ cached datetime.now ref
 11:   if result is not None:                    ▷ ACCEPTED
     │      _stats.ticks_accepted += 1
     │      if _next → return _next.process(result)  ▷ recurse to next stage (→ 6)
     │      else    → return result             ▷ tail reached — tick survives
 12:   else:                                     ▷ REJECTED — stop propagation
     │      _stats.ticks_rejected += 1 ; return None
 13:   except Exception e:                        ▷ no error ever escapes a stage
     │      _stats.last_error ← str(e) ; ticks_rejected += 1
     │      if _error_callback → _error_callback(self.name, e)
     │      return None                          ▷ a throwing stage = silent reject
 14: report ── chain.get_full_report()           ▷ list of every stage.get_report()
     │      get_report() → {name, enabled, processed, accepted, rejected, …}
 15: control ── enable_all / disable_all / reset_all_stats fan out to each stage.

─── 関  Functions / classes defined ──────────────────────────────────────
   class PipelineEvent(Enum)       ▷ TICK_ACCEPTED · _REJECTED · _DUPLICATE
                                     _GAP_DETECTED · _INVALID · _STALE · PIPELINE_ERROR
   class PipelineStats             ▷ @dataclass(slots=True) — per-stage counters
       fields: ticks_processed · ticks_accepted · ticks_rejected
               last_processed · last_error
   class PipelineStage(ABC)        ▷ the contract every stage implements
       __init__(name)              ▷ name · _stats · _next · _enabled · _error_callback · _ts
       stats            @property  ▷ read-only PipelineStats
       next_stage       @property  ▷ the linked _next stage (or None)
       set_next(stage)             ▷ link + return stage (chainable)
       set_error_callback(cb)      ▷ register (name, Exception) handler
       process(tick)               ▷ envelope: enabled-gate · count · try/except · recurse
       _process(tick)  @abstract   ▷ subclass-supplied logic — return tick | None
       enable() · disable()        ▷ flip _enabled (disable = pass-through)
       reset_stats()               ▷ fresh PipelineStats()
       get_report()                ▷ dict snapshot of this stage
   class PipelineChain             ▷ owns + drives an ordered list of stages
       __init__()                  ▷ _stages[] · _head · _tail
       add_stage(stage)            ▷ append + link tail → stage (chainable)
       process(tick)               ▷ push tick into _head (or pass-through if empty)
       get_stage(name)             ▷ linear lookup by stage.name
       enable_all() · disable_all()▷ fan-out toggle
       reset_all_stats()           ▷ fan-out reset
       get_full_report()           ▷ {stages:[…], total_stages:n}

─── 変  Variables / state created ────────────────────────────────────────
   PipelineStage
     name             str          stage identity (used by get_stage lookup)
     _stats           PipelineStats live counters, replaced on reset
     _next            PipelineStage|None  forward link — the chain backbone
     _enabled         bool         True; False = transparent bypass
     _error_callback  Callable|None invoked on a caught _process exception
     _ts              callable     cached datetime.now (avoids global lookup)
   PipelineChain
     _stages          list         insertion-ordered stage registry
     _head / _tail    PipelineStage|None  first / last link for O(1) append

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   datetime.now                    ▷ via _ts(), stamps last_processed
   self._process(tick)             ▷ polymorphic — resolves to the concrete stage
   self._next.process(result)      ▷ recursive hand-off down the chain
   self._error_callback(name, e)   ▷ optional error notification
   (no broker · no asset · no I/O — this file is pure in-memory plumbing)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed.production            ▷ PipelineEntryHandler.on_tick → PipelineChain.process
                                (the live wiring: feed ticks enter the chain here)
   feed.pipeline.deduplicator ▷ class Deduplicator(PipelineStage)
   feed.pipeline.fast_validator▷ class FastValidator(PipelineStage)
   feed.pipeline.validator    ▷ class Validator(PipelineStage)
   feed.pipeline.sequence     ▷ class SequenceMonitor(PipelineStage)
   feed.pipeline.normalizer   ▷ class Normalizer(PipelineStage)
   feed.__init__              ▷ re-exports PipelineStage · PipelineChain

─── 注  Notes · invariants ───────────────────────────────────────────────
   • One-way chain.  set_next builds a singly-linked list; there is no back link
     and no cycle guard — the manager (PipelineChain) is the only safe builder.
   • Reject = stop.  A stage returning None ends propagation; downstream stages
     never see that tick. An exception is treated identically (silent reject).
   • Exceptions never escape.  process() swallows every error into _stats.last_error
     + optional callback, so one bad stage can't crash the feed loop.
   • disable() ≠ remove.  A disabled stage passes ticks through un-counted; it
     stays in the chain and can be re-enabled live.
   • Pure & testable.  No broker, no asset spec, no clock beyond datetime.now —
     each stage is unit-testable in isolation (the design's stated goal).
   • Links:  ticks in → [[handler]] · live wiring → [[production]]
     concrete stages → [[deduplicator]] · [[validator]] · [[sequence]] · [[normalizer]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
