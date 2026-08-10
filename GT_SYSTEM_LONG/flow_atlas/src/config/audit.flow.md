━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 23 ·  src/config/audit.py
  the black box — every tick, state, order, and P&L snapshot, on disk
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1340 lines · 8 classes · ~30 methods + 3 module funcs · the audit spine.
  Four independent background-writer threads, one per stream (feed/state/
  order/pnl), each with its own bounded queue. The trading loop only ever
  enqueues — never touches disk — so logging adds <0.01 ms to the hot path.
  Also the READ side: rebuilds the dashboard ORDERS panel from yesterday's
  CSVs on restart.

要 Require ┊ a symbol, a writable directory (default data/audit + data/report)
          ┊ tick/state/order objects from the engine; config.models for read-back
出 Provides┊ class AuditManager — log_feed · log_state · log_order · log_pnl · close
          ┊ read_recent_orders() — hydrate _order_history across restarts
          ┊ daily-rotated + gzipped CSVs · append-only per-symbol report log

─── 部  Modules used ──────────────────────────────────────────────────────
   threading · queue              ┊ one daemon Thread + bounded Queue per stream
   csv                            ┊ row writer + DictReader (read-back)
   gzip · shutil                  ┊ compress rotated day-files (compresslevel=6)
   pathlib                        ┊ <dir>/<YYYYMMDD>/<SYMBOL>/<type>.csv layout
   datetime · time                ┊ isoformat stamps · rollover · retention cutoff
   json · os · sys                ┊ fallback row encode · stderr drop-warnings
   config.models  (lazy import)   ┊ OrderRecord · OrderSide/Type/Status (read-back only)

─── 算  Algorithm · queue in, file out, CSV back ──────────────────────────
 Require: symbol, enabled flag, directory
 Ensure : every logged row reaches disk OR is loudly counted as DROPPED;
          the trading loop never blocks on I/O.

  1: AuditManager(symbol, enabled=True, …)     ▷ if enabled → _start()
  2: _start()                                  ▷ spawn the writer fleet:
     │   FeedWriter   batch_size=200           ▷ high-volume, batched
     │   StateWriter  batch_size=1             ▷ flush-now for live monitoring
     │   OrderWriter  batch_size=1
     │   PnlWriter    batch_size=1
     │   ReportOrderWriter (if report_directory)▷ parallel, independent thread
  3: ── WRITE path (hot, non-blocking) ──
  4:   audit.log_feed(tick) / log_state / log_order / log_pnl
     │     build a dict row (isoformat timestamp + fields)
  5:     writer.log(row)                       ▷ queue.put_nowait
     │       queue.Full → _dropped++ ; warn to stderr on 1st & every 1000th
     │       ▷ silent loss is a compliance risk — surfaces it loudly
  6:   log_order also MIRRORS the row into _report_writer.log(row)
     │     ▷ independent queue; a stall there never delays the daily return
  7: ── writer thread run() loop (background) ──
  8:   row ← queue.get(timeout=0.1) ; drain up to batch_size more
  9:   _ensure_file()                          ▷ open/rotate today's file
     │     date rolled over? → flush+close old → _compress_old (gzip)
     │                       → _gc_retention (drop >90-day .gz, prune empty dirs)
     │     new file → write _get_header() (per-subclass schema)
 10:   for row in batch: writer.writerow(_format_row(row)) ; _written++
     │     _format_row → _price_str (full FX precision) · _money_str (2dp $)
 11:   flush ; clear batch ; loop
 12: ── shutdown ──  audit.close()
 13:   each writer.stop() → _running=False → join(2s)
     │     run() drains the queue tail, final flush+close
 14:   return {feed/state/order/pnl/report: {written, dropped, path}}
 15: ── READ path (engine startup, separate) ──
 16:   read_recent_orders(symbol, n=20, max_days_back=7)
     │     walk back day-by-day (new hierarchical path, else legacy flat)
 17:     each CSV row → _audit_row_to_order_record()
     │       drop _PROCESS_EVENTS (COMMISSION_REPORT, MODIFIED, … phantoms)
     │       qty via int(float(s)) (tolerate "100.0") ; map event→OrderStatus
 18:     _filter_to_current_cycles()           ▷ drop superseded/cancelled
     │       SUBMITTEDs ; keep real SUBMITTED→FILLED pairs + resting orders
 19:   return list[OrderRecord] chronological → engine repopulates dashboard

─── 関  Functions / classes defined ───────────────────────────────────────
   class LogType                  ▷ FEED · STATE · ORDER · PNL string constants
   class _AuditWriter(Thread)     ▷ base background writer; queue → daily CSV
     __init__ · log · _ensure_file · _compress_old · _gc_retention
     _get_header · run · _format_row · stop
   _price_str(v)                  ▷ full-precision price serialize (FX fix)
   _money_str(v, decimals=2)      ▷ fixed-dp currency serialize ($ amounts)
   class FeedWriter(_AuditWriter)  ▷ 15-col tick schema
   class StateWriter(_AuditWriter) ▷ state-machine snapshot schema
   class OrderWriter(_AuditWriter) ▷ full order-lifecycle schema
   class ReportOrderWriter(OrderWriter) ▷ append-only, FILLED-only, Time/Date split
     __init__ · log · _get_header · _format_row · _ensure_file
     _compress_old (no-op) · _gc_retention (no-op)
   class PnlWriter(_AuditWriter)   ▷ realized/unrealized P&L snapshot schema
   class AuditManager             ▷ unified front door over the 5 writers
     __init__ · _start · log_feed · log_state · log_order
     should_log_pnl · log_pnl · close · stats (property)
   read_recent_orders             ▷ multi-day CSV → OrderRecord history
   _filter_to_current_cycles      ▷ prune superseded/abandoned SUBMITTEDs
   _audit_row_to_order_record     ▷ one CSV row → OrderRecord (or None)

─── 変  Variables / state created ─────────────────────────────────────────
   _RETENTION_DAYS      int=90    gzipped logs older than this are GC'd on rollover
   REPORT_HEADER        list      lean senior-review schema (Time+Date, no forensics)
   _PROCESS_EVENTS      set       events that are NOT orders → dropped on read-back
   queue                Queue     bounded (maxsize=5000); put_nowait, drop if full
   _written / _dropped  int       per-writer counters (surfaced via stats / stop)
   _current_date        date|None rollover key; ReportOrderWriter pins it None
   _file / _writer / _path        the open day-file + csv.writer + Path
   _feed/_state/_order/_pnl/_report_writer  the 5 writer handles on AuditManager
   _last_pnl_snapshot   float     monotonic gate for pnl_interval throttle

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   stdlib only on the WRITE path: threading.Thread · queue.Queue
   csv.writer · gzip.open · shutil.copyfileobj · Path.mkdir/glob/rename/unlink
   datetime.now().isoformat · date.today · time.monotonic/time
   READ path (lazy): config.models.OrderRecord · OrderSide · OrderType · OrderStatus
   ▷ no edges into broker / engine / risk — audit is a pure sink + reader

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   run_live.py            ▷ sole graph importer — constructs AuditManager,
     │                        wires log_feed/state/order/pnl into the loop,
     │                        calls read_recent_orders() to hydrate the panel,
     │                        close() on shutdown
   strategy.engine        ▷ holds the AuditManager handle; every state/order
                             transition emits a log_* (the row sources above)

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Non-blocking always.  Writers NEVER block the trading loop — a full queue
     drops the row (counted + warned), it does not wait. Data integrity over
     completeness: a slow disk must not stall order placement.
   • Loud drops.  First drop AND every 1000th print to stderr — a writer that
     falls permanently behind can shed millions of rows; silence would be an
     audit/compliance hole.
   • Precision is load-bearing.  _price_str preserves full float repr; the old
     :.2f collapsed EURUSD 1.16415 → "1.16" and the dashboard hydrated the
     wrong price after every restart (live 2026-06-05). Currency stays 2dp.
   • Bounded storage.  Daily rotation → gzip → 90-day GC keeps a ~55 MB/day
     feed stream from becoming ~20 GB/year. ReportOrderWriter opts OUT — it is
     append-only forever (a long-hold trade can span ~60 daily files).
   • Two truths on read-back.  read_recent_orders prefers the hierarchical
     path, falls back to legacy flat; _PROCESS_EVENTS + _filter_to_current_cycles
     keep phantoms (commission reports, cancelled entries) out of the panel.
   • Never raise in a writer thread.  _format_row / _ensure_file swallow
     exceptions to stderr — an unhandled raise would kill the daemon silently.
   • Links:  orders sourced from → [[engine]] · OrderRecord shape → [[models]]
     constructed + driven by → [[run_live]] · P&L numbers → [[risk]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
