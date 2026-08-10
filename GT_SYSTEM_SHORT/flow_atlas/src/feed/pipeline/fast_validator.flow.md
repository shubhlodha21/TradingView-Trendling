━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 40 ·  src/feed/pipeline/fast_validator.py
  the sub-millisecond gate — drop the impossible tick, pass everything else
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  45 lines · 1 class (FastValidator) · 2 methods · a leaf in the feed pipeline.
  A stripped-down Validator built for <0.004 ms per tick: it checks only the
  two things that would corrupt a strategy decision, and trusts IBKR for the
  rest. One stage in the chain that carries a tick from the socket to _on_tick.

要 Require ┊ a Tick (tick_type, last, bid, ask) flowing in from the prior stage
          ┊ PipelineStage base (give it __slots__, stats, set_next, process)
          ┊ MessageType enum (TRADE / TICK discriminator)
出 Provides┊ class FastValidator — drop-in PipelineStage for hot-path feeds
          ┊ _process(tick) → tick | None   (None = swallow, do not forward)
          ┊ a guarantee: no LTP<=0 trade and no crossed book reach the engine

─── 部  Modules used ──────────────────────────────────────────────────────
   typing                         ┊ Optional[Tick] return annotation only
   feed.pipeline.base             ┊ PipelineStage — superclass; owns process()
                                  ┊ counters, and the set_next() chaining
   feed.handler                   ┊ Tick (the payload) · MessageType (TRADE/TICK)

─── 算  Algorithm · one tick, two checks, three exits ─────────────────────
 Require: a single Tick handed up by PipelineStage.process()
 Ensure : return the same tick if plausibly valid; return None to drop it;
          never mutate the tick, never block, never raise.

  1: __init__()                               ▷ super().__init__("FastValidator")
     │                                           names the stage; base wires stats
     │                                           + the next_stage pointer; __slots__
     │                                           = () keeps the object weightless
  2: ── per tick ──  base.process(tick) calls _process(tick)   ▷ 流 base owns loop
  3: _process(tick)                           ▷ the whole hot path lives here
  4:   if tick.tick_type == MessageType.TRADE ▷ branch A — a trade print
  5:      if tick.last <= 0.0  → return None  ▷ DROP: zero/negative LTP is garbage;
     │                                           strategy keys off LTP, must not see it
  6:      else → return tick                  ▷ PASS the print downstream
  7:   elif tick.tick_type == MessageType.TICK▷ branch B — a BBO (bid/ask) tick
  8:      b, a ← tick.bid, tick.ask
  9:      if b > 0 and a > 0 and b > a → None  ▷ DROP: crossed book (bid above ask)
     │                                           is an invalid quote state
 10:      else → return tick                  ▷ PASS the quote (incl. one-sided 0)
 11:   else → return tick                     ▷ branch C — any other type: pass-through,
     │                                           do not judge what you don't model
 12: return — the surviving tick continues to the next stage → eventually _on_tick.

   ▷ skips (vs the standard Validator): datetime-age checks, percentage-change
     sanity bounds, deep object inspection — all assumed unnecessary on IBKR data.

─── 関  Functions / classes defined ──────────────────────────────────────
   class FastValidator(PipelineStage)   __slots__ = ()   weightless hot-path stage
     __init__(self)                     register the stage name "FastValidator"
     _process(self, tick) -> tick|None  the validate-or-drop kernel (steps 3–11)

─── 変  Variables / state created ────────────────────────────────────────
   (no module-level state · no instance state — __slots__ = () by design)
   b, a                 float   local bind of tick.bid / tick.ask in branch B
   — all mutable counters (passed/dropped) live on the PipelineStage base, not here

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   PipelineStage.__init__       ▷ via super() — names stage, inits base counters
   MessageType.TRADE / .TICK    ▷ enum comparison only (the tick_type discriminator)
   (no Gateway, no I/O, no allocation — that is the point of this file)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed.production.py           ▷ imports + constructs FastValidator into the
     (importer, line 21)          production feed pipeline (the hot path)
   PipelineStage.process()      ▷ base loop invokes _process per tick (indirect;
                                   no direct caller edge recorded in the graph)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pass-by-default.  Unknown tick types and one-sided quotes (a 0 on either
     leg) flow through — this stage only removes the provably impossible.
   • Crossed-book test is strict (b > a), not (b >= a): bid == ask is allowed.
   • Pure & total.  No mutation, no exception, no await — safe to drop anywhere
     in a chain; returning None is the only side effect (a swallowed tick).
   • Latency is the contract.  __slots__=(), no datetime/pct/inspection work;
     the standard Validator is the slow, thorough sibling for non-hot feeds.
   • Trust boundary.  Correctness leans on IBKR sending sane data; this is a
     gate, not a scrubber.
   • Links:  base stage → [[base]] · tick source + types → [[handler]]
     pipeline assembly → [[production]] · downstream consumer → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
