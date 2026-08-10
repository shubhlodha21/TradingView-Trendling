━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 29 ·  src/execution/fill_ledger.py
  the durable, exactly-once journal of fills — the position-truth of record
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  261 lines · 1 class (FillLedger) · 1 free fn (_norm_side) · pure & standalone.
  Zero broker imports. One JSONL file per (symbol, port, client_id). The
  complete observable for spot FX, where positions() is blind to E-V+C
  cycle dimensions and only the pair-tagged execution stream tells the truth.

要 Require ┊ a writable path (.gt_fills_<sym>_<port>_<cid>.jsonl)
          ┊ caller-injected symbol_of() translator + our_client_id filter
          ┊ stdlib only — json · os · threading.  NO engine, NO ib_async.
出 Provides┊ class FillLedger · net() · count() · record() · merge_broker_fills()
          ┊ exactly-once, gap-free, fsync-durable integral of our fills
          ┊ readable with NO broker connection (frees a client-id slot)

─── 部  Modules used ─────────────────────────────────────────────────────
   json                          ┊ one record per fill, compact separators
   os                            ┊ path_for join · exists · fsync(fileno)
   threading                     ┊ one Lock guards seen-set / net / append
   typing                        ┊ Callable · Iterable · Optional (hints only)
   datetime.timezone (lazy)      ┊ imported inside merge only for tz-normalise

─── 算  Algorithm · the exactly-once integral ────────────────────────────
 Require: a path; fills arrive on the ib_async event thread while the
          strategy loop reads net() — hence every mutation is locked.
 Ensure : net(symbol) = Σ BOT(shares) − Σ SLD(shares) over OUR fills,
          counted once and only once, durable across hard kill.

  1: __init__(path)                           ▷ slots; _seen ∅ · _net {} · _count 0
     │                                           · _lock ; then → load()
  2:   load()                                 ▷ replay the on-disk journal
     │      clear seen/net/count under lock
     │      if file absent → return (behave as empty)
     │      for each line:  strip → json.loads
     │        ▷ malformed line skipped, never fatal (corruption-tolerant)
     │        _apply(rec, persist=False)      ▷ rebuild memory WITHOUT rewriting
     │      unreadable file → swallow, behave as empty (never crash the bot)
  3: ── write path A · record(**fill) ──       ▷ the live single-fill API
  4:   if not exec_id → return False
  5:   with lock: if exec_id in _seen → return False   ▷ idempotent on execId
  6:     build rec {exec_id,symbol,side,shares,price,time,order_id,source}
  7:     return _apply(rec, persist=True)
  8: ── write path B · merge_broker_fills(fills, symbol_of, our_client_id, since) ─
     │      ▷ absorb ib.fills() after reqExecutions; safe to call repeatedly
  9:   for f in fills:                          ▷ one bad fill never aborts merge
 10:     ex,con ← f.execution,f.contract        ▷ skip if either is None
 11:     eid ← ex.execId                         ▷ skip if missing
 12:     if our_client_id set and ex.clientId ≠ it → skip   ▷ only OUR executions
 13:     if since set and ex.time ≤ since → skip
     │        ▷ FL7 floor: normalise naive/aware tz before compare;
     │          a flattened restart must NOT replay STALE pre-restart fills
     │          (may have been closed by another clientId) → no phantom
 14:     sym ← symbol_of(con)  else  con.localSymbol or con.symbol
 15:     record(exec_id=eid, side=ex.side, source="replay", …)
     │        if newly counted → n += 1
 16:   return n                                  ▷ count of NEW fills counted
 17: ── core · _apply(rec, persist)  [lock held] ──
 18:   eid ← rec.exec_id ; if absent or in _seen → return False
 19:   _seen.add(eid)                            ▷ mark seen FIRST — junk never reprocessed
 20:   sign ← _norm_side(rec.side)               ▷ BOT/BUY/B +1 · SLD/SELL/S −1 · else 0
 21:   shares ← int(rec.shares) else 0           ▷ TypeError/ValueError → 0
 22:   counted ← symbol truthy AND sign≠0 AND shares>0
 23:   if counted:  _net[sym] += sign×shares ;  _count += 1
 24:   if persist:  _append_line(rec)            ▷ → step 25
 25:   return counted
 26: ── durability · _append_line(rec) ──
 27:   line ← json.dumps(rec, compact, default=str)
 28:   open(path,"a") → write line+\n → flush → os.fsync(fileno)
     │        ▷ fsync survives SIGKILL (tmux kill-session); at most the
     │          in-flight line is lost.  OSError on fsync swallowed.
 29: ── read path ──  net(symbol)                ▷ locked
 30:   symbol None → dict(_net) (copy) ;  else _net.get(symbol, 0)
 31:   count() → _count (locked) ;  __repr__ → path·fills·net

─── 関  Functions / classes defined ──────────────────────────────────────
   _norm_side(side) -> int            module fn · side token → sign (+1/−1/0)
   class FillLedger                   durable exactly-once journal, one bot
     path conv     path_for(data_dir, symbol, port, client_id)  @staticmethod
     construct     __init__(path)              ▷ slots, then load()
     replay        load()                      ▷ rebuild seen/net from disk
     core apply    _apply(rec, persist)        ▷ lock-held dedup + integrate
     durability    _append_line(rec)           ▷ append + flush + fsync
     write live    record(**fill) -> bool      ▷ True iff newly counted
     write replay  merge_broker_fills(...) -> int  ▷ absorb ib.fills()
     read          net(symbol) · count() · __repr__

─── 変  Variables / state created ────────────────────────────────────────
   path        str        the JSONL journal location (slot)
   _seen       set        execIds already applied — the dedup memory
   _net        dict       symbol → signed net shares (the integral)
   _count      int        number of counted (valid) fills
   _lock       Lock       guards seen / net / count / file append
   __slots__   tuple      ("path","_seen","_net","_count","_lock") — no __dict__

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   json.loads / json.dumps           os.path.join · os.path.exists · os.fsync
   threading.Lock                    datetime.timezone (lazy, tz-normalise)
   _norm_side ← _apply               _apply ← load · record
   _append_line ← _apply             record ← merge_broker_fills
   ▷ NO calls into engine / broker / ib_async — purity is the design.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   strategy.engine        ▷ FillLedger.path_for → FillLedger(path); holds as
                            _fill_ledger; merge_broker_fills() in FL3 reconcile;
                            also hands the ledger to gateway._fill_ledger
   execution.broker       ▷ FL4 — get_our_position_via_executions consults the
                            injected _fill_ledger.net() (survives 24h cache loss)
   scripts.three_truths   ▷ FL5 — reads the .gt_fills_*.jsonl files directly,
                            NO broker connection, for FX broker-truth monitor
   tests/                 ▷ test_fill_ledger · _engine_integration · _fl8_selfheal
                            test_fl9_session_floor · test_ledger_graph · _three_truths_ledger

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Exactly-once.  Dedup on execId at BOTH read (load) and write (record).
     _seen.add() happens BEFORE integrate, so even a junk record is never
     reprocessed — replaying the same fills is always a no-op.
   • Gap-free + durable.  Append-only JSONL · fsync per line · 3-day backfill
     merge means: process restart, chaos respawn, IBKR ~24h cache eviction,
     reconnect double-callbacks — none lose or double-count a fill.
   • One writer per file.  Keyed (symbol, port, client_id) → no cross-writer
     contention; matches the A79 single-writer model; 1 bot ≡ 32 bots.
   • Corruption-tolerant.  A malformed line or unreadable file degrades to
     "empty", never crashes the bot.  source ∈ {"live","replay"}.
   • Why it exists.  positions() observes only flow-conservation per currency
     (rank V−C) and is BLIND to the cycle space (dim E−V+C) — 8 invisible
     dimensions on the 15-pair fleet.  This integral is the only complete
     observable.  It is wrong ONLY if a fill is missed or double-counted —
     which this module makes impossible.
   • Links:  reconcile + FL9 floor → [[engine]] · FL4 consult → [[broker]]
     FX broker-truth monitor → [[three_truths]] · ledger-graph → [[ledger_graph]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
