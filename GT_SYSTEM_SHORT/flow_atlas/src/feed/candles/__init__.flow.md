━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 36 ·  src/feed/candles/__init__.py
  the candles package door — re-exports the bar-builder surface, hides the impl
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  41 lines · 0 classes · 0 functions defined here · a pure namespace façade.
  Everything real lives one file down in candles/builder.py; this file only
  chooses what the outside world is allowed to name.

要 Require ┊ src.feed.candles.builder must import cleanly (its six public names)
出 Provides┊ the package symbol  src.feed.candles  with a curated __all__:
          ┊   Candle · BarBuilder · CandleBuilder · AggregatedCandleBuilder
          ┊   TIMEFRAMES · align_to_timeframe
          ┊ callers write  `from src.feed.candles import CandleBuilder, Candle`
          ┊ and never need to know the builder module exists.

─── 部  Modules used ─────────────────────────────────────────────────────
   feed.candles.builder          ┊ the one and only import — the implementation
                                 ┊ Candle (OHLCV dataclass) · BarBuilder (one bar)
                                 ┊ CandleBuilder (multi-timeframe) · TIMEFRAMES
                                 ┊ AggregatedCandleBuilder · align_to_timeframe

─── 算  Algorithm · what import-time does ─────────────────────────────────
 Require: builder.py present and importable
 Ensure : the six public names resolve; nothing else leaks from the package.

  1: import src.feed.candles            ▷ Python executes this __init__ once
  2:   from .builder import (…)         ▷ pull the six names into package scope
     │      Candle, BarBuilder, CandleBuilder,
     │      AggregatedCandleBuilder, TIMEFRAMES, align_to_timeframe
  3:   __all__ = [ …same six… ]         ▷ define the export contract
     │                                     ▷ `from src.feed.candles import *`
     │                                       yields exactly these, no more
  4: return — the namespace is ready    ▷ no state, no side effects, no I/O

─── 関  Functions / classes defined ──────────────────────────────────────
   (none defined in this file — it declares no class and no function)
   re-exported from builder.py:
     Candle                  ┊ OHLCV candlestick data
     BarBuilder              ┊ single-bar tick aggregation
     CandleBuilder           ┊ multi-timeframe builder (the headline export)
     AggregatedCandleBuilder ┊ higher → lower timeframe roll-up
     TIMEFRAMES              ┊ the supported-timeframe table
     align_to_timeframe      ┊ snap a timestamp down to a bar boundary

─── 変  Variables / state created ────────────────────────────────────────
   __all__   list[str]   the six exported names — the package's public API
   (no module-level mutable state; import is idempotent and pure)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   feed.candles.builder        ▷ IMPORTS_FROM (line 24) — the sole edge out

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   feed/__init__.py            ▷ IMPORTS_FROM (line 69) — the feed package
                                 re-exports the candle surface one level up
   (any caller writing `from src.feed.candles import …` rides this façade)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Façade only.  This file is a re-export shim — keep it logic-free; the
     OHLCV math, tick aggregation, and timeframe alignment all live in
     [[builder]]. Edit behaviour there, not here.
   • Contract = __all__.  The import list and __all__ must stay in lockstep;
     adding a name to one without the other silently breaks `import *` or
     leaves a dangling export.
   • One door up.  [[feed]] (src/feed/__init__.py) imports through here, so the
     candles surface is reachable as both src.feed.candles and src.feed.
   • Consumers.  CandleBuilder feeds bar-driven logic; ticks arrive via
     [[handler]] and ultimately serve the [[engine]] state-machine.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
