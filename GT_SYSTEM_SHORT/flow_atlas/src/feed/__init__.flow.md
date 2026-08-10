━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 31 ·  src/feed/__init__.py
  the feed façade — one import surface for the whole market-data subsystem
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  101 lines · 0 classes · 0 functions · pure re-export package.
  Defines nothing of its own — it gathers nine sibling modules into a single
  flat namespace and publishes 23 names through __all__. The docstring carries
  the canonical wiring recipe (pipeline → builder → tick loop) for callers.

要 Require ┊ the nine sibling modules under src/feed/ must import cleanly
          ┊ (connection · handler · pipeline/* · cache · candles)
出 Provides┊ from src.feed import {Connection… Handler… Pipeline… Cache… Candle…}
          ┊ a stable façade — callers never reach into submodules directly

─── 部  Modules used ──────────────────────────────────────────────────────
   feed.connection            ┊ ConnectionManager · ConnectionConfig
                              ┊ ConnectionObserver · ConnectionState
   feed.handler               ┊ FeedHandler · TickHandler · Tick
                              ┊ MessageType · TickFilter
   feed.pipeline.base         ┊ PipelineChain · PipelineStage · PipelineStats
   feed.pipeline.sequence     ┊ SequenceMonitor
   feed.pipeline.deduplicator ┊ Deduplicator
   feed.pipeline.normalizer   ┊ Normalizer
   feed.pipeline.validator    ┊ Validator
   feed.cache                 ┊ MarketDataCache · TickLogger · OHLCVBar · TickRecord
   feed.candles               ┊ Candle · CandleBuilder · TIMEFRAMES

─── 算  Algorithm · what import-time does ─────────────────────────────────
 Require: the nine submodules resolve without error
 Ensure : after import, all 23 façade names are bound in src.feed's namespace
          and only those 23 are re-exported (anything else stays private)

  1: import src.feed                          ▷ Python executes __init__.py top→bottom
  2:   from .connection import (…)            ▷ binds ConnectionManager + 3 siblings
  3:   from .handler import (…)               ▷ binds FeedHandler · Tick · MessageType …
  4:   from .pipeline.base import (…)         ▷ binds PipelineChain · Stage · Stats
  5:   from .pipeline.sequence import …       ▷ SequenceMonitor  (stage 1 of chain)
  6:   from .pipeline.deduplicator import …   ▷ Deduplicator     (stage 2)
  7:   from .pipeline.normalizer import …     ▷ Normalizer       (stage 3)
  8:   from .pipeline.validator import …      ▷ Validator        (stage 4)
  9:   from .cache import (…)                 ▷ MarketDataCache · TickLogger + records
 10:   from .candles import (…)               ▷ Candle · CandleBuilder · TIMEFRAMES
 11: __all__ = [ … 23 names … ]               ▷ the public contract; `import *` honours it
 12: return — namespace ready.                ▷ callers now `from src.feed import X`
     │
     │  ▷ docstring recipe (the intended runtime assembly, not run here):
     │      pipeline = PipelineChain()
     │      pipeline.add_stage(SequenceMonitor() · Deduplicator()
     │                         · Normalizer() · Validator())   ▷ order matters
     │      builder = CandleBuilder(symbol, timeframes=[…])
     │      builder.on_bar("5m", cb)
     │      for tick in ticks: r = pipeline.process(tick); builder.add_tick(r)

─── 関  Functions / classes defined ──────────────────────────────────────
   (none) — this file declares no class and no function.
   It only re-binds names imported from the nine submodules above.

─── 変  Variables / state created ─────────────────────────────────────────
   __all__              list[str]    the 23-name public export contract
   (all other module-level names are re-exported imports, no fresh state)

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   IMPORTS_FROM (graph, confidence 1.0):
     feed.connection · feed.handler
     feed.pipeline.base · .sequence · .deduplicator · .normalizer · .validator
     feed.cache · feed.candles
   no function calls — import statements only.

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   graph importers_of = 0  ▷ no module imports the package object directly;
                            callers pull individual names via `from src.feed
                            import …` (the docstring recipe). Grep confirms the
                            only `from src.feed import` is the docstring example.
   typical consumers (by name, not by edge):  run_live.py · the Engine's feed
   wiring · paper-test harnesses that build a PipelineChain + CandleBuilder.

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Façade only.  Touch this file when a submodule adds a public name worth
     re-exporting — add it to BOTH the import block and __all__, or it stays
     invisible to `import *`.
   • Pipeline order is semantic.  sequence → dedup → normalize → validate is the
     stages' intended chain; this file lists them in that order on purpose.
   • Single import surface.  Callers should never reach into src.feed.cache etc.
     directly — keep the coupling at this façade so submodules can move freely.
   • Zero logic.  No branches, no runtime state; failure here means a submodule
     failed to import (the real bug lives downstream).
   • Links:  ticks → [[handler]] · socket → [[connection]] · stages → [[base]]
     [[sequence]] · [[deduplicator]] · [[normalizer]] · [[validator]]
     caching → [[cache]] · bars → [[candles]] · consumer → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
