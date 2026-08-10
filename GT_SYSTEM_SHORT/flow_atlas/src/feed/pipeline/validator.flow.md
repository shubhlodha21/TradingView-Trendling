━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 43 ·  src/feed/pipeline/validator.py
  the gatekeeper — one tick in, one tick or nothing out · bad data dies here
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  305 lines · 3 classes (ValidationConfig · ValidationResult · Validator)
  · 11 methods · one stage in the feed pipeline's chain-of-responsibility.
  Sits between the raw handler and the trading engine; a tick that fails any
  one of five checks returns None and never reaches a trading decision.

要 Require ┊ a Tick (symbol · bid · ask · last · timestamp) from the feed
          ┊ a ValidationConfig (bounds, age, change %, required-field rules)
          ┊ PipelineStage base — set_next() chains the next stage
出 Provides┊ class Validator · ValidationConfig · ValidationResult
          ┊ _process(tick) → Tick | None — the filtered tick or rejection
          ┊ get_report() — per-reason rejection counters

─── 部  Modules used ─────────────────────────────────────────────────────
   dataclasses                   ┊ @dataclass(slots=True) for config + result
   datetime  (datetime, timezone)┊ now() clock · tz-aware → local for age math
   typing  (Optional)            ┊ Optional[Tick] · Optional[ValidationConfig]
   feed.pipeline.base            ┊ PipelineStage (parent) · PipelineEvent enum
   feed.handler                  ┊ Tick — the unit of data validated

─── 算  Algorithm · the five-gate sieve ───────────────────────────────────
 Require: a Tick and a ValidationConfig.
 Ensure : returns the SAME tick untouched if all gates pass, else None;
          every rejection increments exactly one counter + emits an event.

  1: __init__(name, config)                    ▷ slots; config or defaults
     │                                           _last_tick ← {} (per-symbol)
     │                                           four reject counters ← 0
     │                                           _ts ← datetime.now (clock handle)
  2: ── per tick ──  _process(tick)            ▷ the sieve, in fixed order
  3:   _check_required_fields(tick)            ▷ gate 1
     │      if require_bid_ask and bid≤0 or ask≤0 → "bid_or_ask_missing"
     │      if require_last and last≤0 → "last_price_missing"
     │      ▷ fail → _rejected_missing++ ; emit ; return None
  4:   _check_price_bounds(tick)               ▷ gate 2
     │      for bid, ask, last: skip if ≤0 (zero handled elsewhere)
     │      price < min_price → "{name}_below_min"
     │      price > max_price → "{name}_above_max"
     │      ▷ fail → _rejected_bounds++ ; emit ; return None
  5:   _check_price_consistency(tick)          ▷ gate 3
     │      if bid>0 and ask>0 and bid>ask → "bid_greater_than_ask"
     │      spread_pct >0.10 → still VALID, warning "large_spread_…"
     │      ▷ fail → _rejected_consistency++ ; emit ; return None
  6:   _check_timestamp(tick)                  ▷ gate 4
     │      now ← _ts() ; tz-aware ts → astimezone → naive local
     │      age < −5s (future) → "timestamp_in_future"  (unless allow_future)
     │      age > max_timestamp_age_seconds → "timestamp_too_old_…s"
     │      ▷ fail → _rejected_timestamp++ ; emit ; return None
  7:   _check_price_change(tick)               ▷ gate 5 — stateful
     │      first tick for symbol → pass (nothing to compare)
     │      else change_pct = |last − prev.last| / prev.last
     │      change_pct > max_price_change_pct → "price_change_…_exceeds_limit"
     │      ▷ fail → _rejected_bounds++ (shares the bounds counter) ; return None
  8:   _last_tick[tick.symbol] ← tick          ▷ remember for next gate-5 compare
  9:   return tick                             ▷ survived all five → pass downstream
 10: ── on any rejection ──  _emit_invalid_event(tick, reason)
 11:   _emit_event(TICK_INVALID, tick, {reason, symbol})
     │      ▷ _emit_event is a NO-OP stub (pass) — wiring point, see 注
 12: ── housekeeping ──
 13:   reset()        ▷ clear _last_tick + zero all counters
 14:   get_report()   ▷ super().get_report() + per-reason rejects + symbols_tracked

─── 関  Functions / classes defined ──────────────────────────────────────
   class ValidationConfig (dataclass, slots)
     fields         min_price · max_price · max_price_change_pct · min_size
                    max_size · max_timestamp_age_seconds · allow_future_timestamps
                    require_bid_ask · require_last
   class ValidationResult (dataclass, slots)
     fields         valid · reason · warnings
     __post_init__  warnings ← [] when None (mutable-default guard)
   class Validator (PipelineStage)
     lifecycle      __init__
     pipeline       _process — the five-gate sieve, returns Tick|None
     gates          _check_required_fields · _check_price_bounds
                    _check_price_consistency · _check_timestamp · _check_price_change
     events         _emit_invalid_event · _emit_event (no-op stub)
     housekeeping   reset · get_report

─── 変  Variables / state created ────────────────────────────────────────
   _config                ValidationConfig  the active ruleset (or defaults)
   _last_tick             dict[sym, Tick]   per-symbol last VALID tick (gate 5)
   _rejected_bounds       int               gate 2 + gate 5 failures (shared)
   _rejected_consistency  int               gate 3 failures (bid>ask)
   _rejected_timestamp    int               gate 4 failures (future / stale)
   _rejected_missing      int               gate 1 failures (absent fields)
   _warnings              int               reserved; not incremented in _process
   _ts                    callable          datetime.now handle (injectable clock)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   self._check_required_fields · _check_price_bounds · _check_price_consistency
   self._check_timestamp · _check_price_change   ▷ the five gates
   self._emit_invalid_event → self._emit_event (stub) → PipelineEvent.TICK_INVALID
   super().get_report()                          ▷ base stage stats
   datetime.now (via _ts) · ts.astimezone        ▷ timestamp age math

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   src/feed/__init__.py   ▷ re-exports Validator / ValidationConfig (line 63)
   (graph shows no in-repo caller of _process — wired by whoever assembles
    the pipeline at runtime via PipelineStage.set_next / .process)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pure filter.  _process never mutates the tick; it returns the identical
     object on pass, None on fail.  Order of gates is fixed and short-circuits.
   • One reason per reject.  Each failure path bumps exactly one counter and
     emits one TICK_INVALID — except gate 5, which reuses _rejected_bounds.
   • Stateful gate.  _check_price_change carries _last_tick across ticks; first
     tick per symbol always passes.  reset() wipes this memory.
   • Dead wiring.  _emit_event is a `pass` stub — invalid-tick events are
     counted but not yet published anywhere.  This is the integration seam.
   • _warnings counter and ValidationConfig.min_size / max_size are declared
     but never read in _process — latent / future rules.
   • Clock seam.  _ts holds datetime.now so tests can inject a fixed clock for
     deterministic timestamp-age assertions.
   • Links:  ticks → [[handler]] · pipeline contract → [[base]]
     consumes-filtered-ticks → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
