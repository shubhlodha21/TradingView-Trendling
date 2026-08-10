━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 41 ·  src/feed/pipeline/normalizer.py
  one pipeline stage — bends every raw Tick into one shape, one timezone
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  119 lines · 2 classes (NormalizerConfig · Normalizer) · 7 methods
  a leaf of the feed pipeline. It carries no broker, no state machine —
  it takes one Tick in and returns one Tick out, rounded and squared.
  The one place it is allowed to invent data (fill-missing) is now OFF
  by default, because invented quotes once became phantom liquidity.

要 Require ┊ a Tick (timestamp, symbol, bid/ask/last, sizes, volume)
          ┊ a NormalizerConfig (precisions, tz, fill/normalize switches)
          ┊ a PipelineStage base supplying name + get_report scaffolding
出 Provides┊ class Normalizer · class NormalizerConfig
          ┊ a Tick guaranteed: UTC-aware ts · UPPER symbol · ≥0 prices/sizes
          ┊ add_symbol_mapping — alias table · get_report — stage telemetry

─── 部  Modules used ─────────────────────────────────────────────────────
   dataclasses                   ┊ @dataclass(slots=True) for the config
   datetime · timezone           ┊ tz-attach + astimezone(UTC)
   typing.Optional               ┊ config + nullable Tick return
   feed.pipeline.base            ┊ PipelineStage — the stage contract (_process)
   feed.handler                  ┊ Tick — the dataclass moved through the stage

─── 算  Algorithm · one tick, squared ─────────────────────────────────────
 Require: a raw Tick from the upstream stage
 Ensure : same Tick shape out — UTC ts, normalized symbol, no negative
          numbers; never fabricate a quote unless explicitly told to.

  1: __init__(name, config)                   ▷ super().__init__(name); config
     │                                           or default; _symbol_map ← {}
  2: ── on each tick ──  _process(tick)        ▷ the stage entry point (base calls it)
  3:   build a fresh Tick, field by field:
  4:     timestamp ← _normalize_timestamp(ts)  ▷ naive → assume UTC; else astimezone UTC
  5:     symbol    ← _normalize_symbol(sym)     ▷ if normalize off → as-is
     │                                           else _symbol_map hit → alias
     │                                           else → sym.upper().strip()
  6:     bid/ask/last ← _round_price(p)         ▷ p ≤ 0 → 0.0 ; else round(p, precision)
  7:     bid_size/ask_size/volume ← _normalize_size(s) ▷ s ≤ 0 → 0 ; else int(s)
  8:     tick_type, req_id ← copied through      ▷ untouched passengers
  9:   if config.fill_missing_prices:            ▷ default False — the safe stance
 10:     _fill_missing_prices(normalized)         ▷ mutates the new Tick in place
 11:       if bid>0 and ask>0 and last≤0:         ▷ ONLY safe synthesis kept
     │         last ← (bid + ask) / 2             ▷ midpoint — a defensible value
     │       ▷ bid=last / ask=last branches REMOVED — they faked liquidity
 12:   return normalized                          ▷ one Tick out (Optional by contract)

   ── off the tick path ──
 13: add_symbol_mapping(from, to)               ▷ _symbol_map[from.upper()] = to.upper()
 14: get_report()                               ▷ base report + price_precision +
     │                                            len(_symbol_map)

─── 関  Functions / classes defined ──────────────────────────────────────
   class NormalizerConfig (@dataclass slots)
     price_precision:int=2 · size_precision:int=0 · timestamp_tz=UTC
     fill_missing_prices:bool=False · normalize_symbols:bool=True
   class Normalizer(PipelineStage)
     lifecycle    __init__
     stage hook   _process               — the per-tick transform (base dispatches here)
     primitives   _round_price · _normalize_size · _normalize_timestamp
                  _normalize_symbol · _fill_missing_prices
     api          add_symbol_mapping · get_report

─── 変  Variables / state created ────────────────────────────────────────
   _config        NormalizerConfig   precisions, tz, fill/normalize switches
   _symbol_map    dict               from-symbol → to-symbol alias table (UPPER keys)
   (NormalizerConfig.fill_missing_prices defaults False — see 注)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   PipelineStage.__init__ · PipelineStage.get_report   (super)
   feed.handler.Tick                                   (constructed each tick)
   datetime.replace / .astimezone · round · int        (stdlib primitives)
   ▷ graph: imports_of → base.py · handler.py (+ dataclasses, datetime, typing)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed/__init__.py        ▷ re-exports Normalizer / NormalizerConfig (line 62)
   ▷ graph: importers_of → feed/__init__.py (sole importer)
   ▷ callers_of _process → none in graph; the base PipelineStage drives it
     polymorphically (the call site is the pipeline runner, not a direct edge)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Idempotent shape.  Output Tick has the same fields as input — this stage
     reshapes values, never the schema.  Safe to chain after any feed stage.
   • Never negative.  _round_price and _normalize_size both floor at 0 — a
     bad/absent quote becomes 0, a clean sentinel downstream stages can reject.
   • No fabricated liquidity.  fill_missing_prices defaults False as of this
     patch.  Only the (bid,ask)→last midpoint survives; bid=last / ask=last
     were removed — they quoted prices nobody offered, and strategy code that
     crossed them saw fills it could never get.  Turn on only when the consumer
     truly tolerates synthetic midpoints.
   • UTC always.  Naive timestamps are assumed UTC, never local — keeps the
     whole feed on one clock before it reaches the engine.
   • Stateless but for the map.  _symbol_map is the only mutable state; it is
     additive (add_symbol_mapping) and read-only on the tick path.
   • Links:  the moved datum → [[handler]] (Tick) · the stage contract →
     [[base]] · downstream consumer of clean ticks → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
