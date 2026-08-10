━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 32 ·  src/feed/cache.py
  the rolling-window memory — ticks in · OHLCV bars out · disk on the side
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  375 lines · 3 classes (OHLCVBar · TickRecord · TickLogger · MarketDataCache)
  · a passive observer.  It decides nothing; it remembers.  Ticks arrive, it
  appends them to a bounded deque and folds each one into five live bars.
  When a bar's clock turns over it is sealed, stored, and announced.

要 Require ┊ a stream of Tick objects (symbol, timestamp, bid/ask/last, volume)
          ┊ optionally a writable directory for CSV persistence
出 Provides┊ class MarketDataCache — recent ticks + multi-timeframe OHLCV bars
          ┊ class OHLCVBar (to_dict/from_dict) · class TickRecord · TickLogger
          ┊ on_bar_completed callbacks — bar sealed → subscriber notified

─── 部  Modules used ─────────────────────────────────────────────────────
   csv                           ┊ TickLogger writes rows to per-symbol CSV
   threading                     ┊ Lock — every read/write is guarded
   collections.deque             ┊ the rolling window (maxlen evicts the old)
   dataclasses                   ┊ @dataclass(slots=True) on the three records
   datetime / timedelta          ┊ bar-boundary alignment + TIMEFRAMES table
   pathlib.Path                  ┊ directory + per-symbol filename
   typing                        ┊ Optional · Callable
   feed.handler  (Tick)          ┊ the tick type it consumes  (→ 流 handler)

─── 算  Algorithm · the remembering spine ─────────────────────────────────
 Require: a Tick with .symbol .timestamp .last .volume
 Ensure : last tick_window ticks per symbol are recoverable; each timeframe
          holds at most bar_window sealed bars; a forming bar is never lost.

  1: __init__(symbols, tick_window=1000, bar_window=500, logger?)
     │                                       ▷ empty dicts; one Lock; ts ← now
  2: add_tick(tick)                          ▷ the single ingest door
  3:   if sym unknown → add_symbol(sym)      ▷ self-registers new symbols
  4:   if logger → logger.log(tick)          ▷ side-channel CSV persist (step 11)
  5:   _ticks[sym].append(tick)              ▷ deque drops oldest past maxlen
  6:   _update_bars(tick)                    ▷ fold tick into all 5 timeframes
  7: _update_bars(tick)                      ▷ for each tf in TIMEFRAMES:
     │      bar_start ← _align_to_bar(ts, delta)   ▷ snap ts to bar boundary
  8:     if no current bar OR bar_start moved on:
     │        if a current bar exists → _complete_bar(key, current)  ▷ seal old
     │        _current_bar[key] ← fresh OHLCVBar  (o=h=l=c=last, n=1)
  9:     else → extend current bar:
     │        high=max · low=min · close=last · volume+= · tick_count+=
 10: _align_to_bar(ts, delta)                ▷ 1d→midnight; ≥1h→hour bucket;
     │                                          else→minute bucket (floor div)
 11: _complete_bar(key, bar)                 ▷ append to _bars[key] (bounded)
     │      for cb in _bar_callbacks[tf]: cb(bar)   ▷ announce — errors caught,
     │                                                 printed, never raised
 12: ── readers (any thread, all under _lock) ──
 13:   get_ticks(sym, count)                 ▷ last N raw ticks
 14:   get_bars(sym, tf, count)              ▷ last N sealed bars
 15:   get_latest_bar / get_current_bar      ▷ newest sealed · the forming bar
 16:   get_stats()                           ▷ counts of symbols/ticks/bars
 17: close()                                 ▷ logger.close(); seal every
     │                                          still-forming bar before exit
 18: ── TickLogger (independent disk path) ──
 19:   log(tick)  → open(sym_YYYYMMDD.csv,'a'); header once; writerow(...)
 20:   close()    → flush + close all open file handles

─── 関  Functions / classes defined ──────────────────────────────────────
   class OHLCVBar (slots)   one aggregated price/volume bar for a period
     to_dict                  → compact dict (ts,symbol,tf,o,h,l,c,v,n)
     from_dict (classmethod)  ← rebuild a bar from that dict (typed parse)
   class TickRecord (slots) persistent tick shape (ts,sym,bid,ask,last,vol)
     to_dict                  → compact dict for serialization
   class TickLogger         thread-safe per-symbol CSV writer
     __init__                 mkdir directory; Lock; open-file registry
     log                      lazily open file + header, append one tick row
     close                    close every handle, clear the registry
   class MarketDataCache    rolling tick window + multi-timeframe aggregator
     __init__                 deques, current-bar map, callback map, Lock
     add_symbol               register symbol + allocate its tick deque
     add_tick                 ingest one tick (log · store · update bars)
     _update_bars             fold tick into all five live bars
     _align_to_bar            floor a timestamp to its bar boundary
     _complete_bar            seal a bar, store it, fire callbacks
     on_bar_completed         subscribe a callback to a timeframe
     get_ticks                last N ticks for a symbol
     get_bars                 last N sealed bars for (symbol, timeframe)
     get_latest_bar           newest sealed bar (or None)
     get_current_bar          the still-forming bar (or None)
     get_stats                cache size statistics
     close                    seal all forming bars; close the logger

─── 変  Variables / state created ────────────────────────────────────────
   TIMEFRAMES        dict      class const: 1m·5m·15m·1h·1d → timedelta
   _symbols          set       symbols currently tracked
   _tick_window      int       max ticks kept per symbol (deque maxlen)
   _bar_window       int       max sealed bars kept per (symbol, timeframe)
   _ticks            dict      symbol → deque[Tick]  (rolling raw history)
   _bars             dict      (symbol, tf) → deque[OHLCVBar]  (sealed)
   _current_bar      dict      (symbol, tf) → OHLCVBar | None  (forming)
   _bar_callbacks    dict      tf → [Callable]  (bar-completion subscribers)
   _lock             Lock      guards every container mutation + read
   _logger           TickLogger|None  optional disk persistence
   _ts               callable  cached datetime.now reference
   TickLogger._files dict      symbol → (open file, csv.writer)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   feed.handler.Tick                ▷ the consumed tick type (import only)
   collections.deque                ▷ bounded windows for ticks + bars
   datetime / timedelta             ▷ _align_to_bar boundary math
   csv.writer · pathlib.Path · open ▷ TickLogger CSV persistence
   threading.Lock                   ▷ acquired in every public method
   (self) add_symbol · _update_bars · _align_to_bar · _complete_bar
   callback(bar)                    ▷ subscriber-supplied bar-completion fn

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   src/feed/__init__.py   ▷ re-exported as part of the feed namespace
   ▷ graph records no in-repo *callers* of add_tick — wired at runtime:
     the live feed pipeline pushes ticks in; strategy code reads bars out.
     (consumer edges are dynamic/observer-style, invisible to static import)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Passive memory.  This file never places an order, never decides; it only
    records what the feed delivers and serves it back on request.
   • Bounded forever.  Both deques carry maxlen — memory is O(window), the
    oldest tick/bar is silently evicted; no unbounded growth across a session.
   • Atomic seal.  A bar is completed exactly once — when a later tick crosses
    its boundary, or at close().  A forming bar lives in _current_bar until then.
   • Callbacks are best-effort.  A throwing subscriber is caught + printed; it
    never corrupts the cache or blocks the next callback.
   • get_stats bars_<tf> keys index (None, tf) → effectively always 0 (bars are
    stored under real symbols); a cosmetic stat, not a correctness path.
   • Two independent disk/​memory paths.  TickLogger (CSV, per-symbol files) and
    MarketDataCache (in-memory window) share a tick but lock separately.
   • Links:  tick source → [[handler]] · namespace → [[__init__]] ·
    bar consumers → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
