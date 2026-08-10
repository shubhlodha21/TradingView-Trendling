━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 26 ·  src/config/persistence.py
  the durability floor — crash-safe state snapshots + append-only audit trail
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  199 lines · 2 classes (StateStore · AuditLog) · 11 methods · a leaf node.
  Nothing the engine trusts after a restart exists unless it passed through
  here first.  The whole reconciliation story (流 engine steps 4-8) reads back
  what this file wrote.  Two jobs, two disciplines: StateStore wants ATOMIC +
  DURABLE single-snapshot writes off the hot path; AuditLog wants a cheap
  append-only forensic tape.

要 Require ┊ a writable directory for .gt_state.json (+ .tmp sibling)
          ┊ a writable path for .gt_audit.csv
          ┊ a daemon thread slot (StateStore spawns one writer)
出 Provides┊ class StateStore · save / load / clear / close
          ┊ class AuditLog  · append / read
          ┊ the only crash-safe write primitive in the system (_write_to_disk)

─── 部  Modules used ──────────────────────────────────────────────────────
   csv                           ┊ AuditLog row writer / DictReader
   json                          ┊ state snapshot serialize · audit data blob
   os                            ┊ fsync · replace · open · remove · exists
   queue                         ┊ bounded(8) handoff hot-path → writer thread
   sys                           ┊ stderr for writer-thread error prints
   threading                    ┊ the background StateWriter daemon
   datetime                      ┊ AuditLog timestamp (self._ts = datetime.now)
   typing.Optional               ┊ load() / _last_state nullable returns

─── 算  Algorithm · two write paths, both off the event loop ───────────────
 Require: a path on a real filesystem
 Ensure : a reader always sees the OLD snapshot or a COMPLETE new one —
          never a half-written file, never a renamed-but-empty file.

  1: StateStore(path=".gt_state.json")        ▷ __init__
  2:   _queue ← queue.Queue(maxsize=8)         ▷ tiny on purpose (snapshot, not log)
  3:   _running ← True ; _dropped ← 0 ; _written ← 0 ; _last_state ← None
  4:   _thread ← Thread(_writer_loop, daemon) ; .start()  ▷ writer lives now
  5: ── hot path ──  engine calls save(state)  ▷ from asyncio loop, must NOT block
  6:   _last_state ← state                      ▷ in-flight read without racing writer
  7:   try _queue.put_nowait(state) → return True
  8:   except queue.Full:                       ▷ converge on LATEST, drop stale
     │      _queue.get_nowait() ; _dropped++    ▷ evict the oldest pending snapshot
     │      put_nowait(state) → True            ▷ fresh one takes its slot
     │      if still Full → _dropped++ ; return False
  9: ── writer thread ──  _writer_loop()        ▷ the only thing touching disk
 10:   while _running:                          ▷ blocking get with 0.25s timeout
     │      state ← _queue.get(timeout=0.25)    ▷ Empty → loop (lets _running flip)
     │      _write_to_disk(state) ; _written++  ▷ exception → print to stderr, survive
 11:   on shutdown drain: flush whatever remains in the queue, then exit
 12: _write_to_disk(state)                       ▷ THE crash-safe primitive
     │      temp ← path + ".tmp"
 13:     open(temp,"w") → json.dump(indent=2, default=str)
 14:       f.flush() → os.fsync(f.fileno())      ▷ bytes hit the DEVICE before rename
     │        ▷ except OSError/AttributeError → pass (rare fs; keep atomicity, lose
     │          only the power-loss guarantee — never fail a write over a missing fsync)
 15:     os.replace(temp, path)                  ▷ atomic on POSIX + Windows
 16:     fsync the PARENT directory               ▷ so the rename itself survives a crash
     │        ▷ open(dirname, O_RDONLY) → fsync(dir_fd) → close
     │        ▷ except OSError/PermissionError/AttributeError → pass (Windows)
 17: load() → Optional[dict]                     ▷ restart reads this (流 engine step 4)
     │      not exists → None
     │      json.load ; except JSONDecodeError/IOError → None  ▷ corrupt = cold start
 18: clear()                                      ▷ --reset path: remove the file if present
 19: close(timeout=2.0) → {written, dropped, path}▷ _running ← False ; join writer ; report
 20: ── separate tape ──  AuditLog(path=LOG_FILE) ▷ __init__
 21:   _ts ← datetime.now                         ▷ bound once
 22:   if file absent → write HEADER row          ▷ "timestamp,event,trade_id,data"
 23: append(event, trade_id="", data=None)        ▷ open("a") → csv.writerow:
     │      [ _ts().isoformat(), event, trade_id, json.dumps(data or {}) ]
     │      ▷ append-only, no fsync — forensic tape, not the source of truth
 24: read(limit=100) → list[dict]                 ▷ DictReader → tail(limit)

─── 関  Functions / classes defined ───────────────────────────────────────
   class StateStore                              file-based snapshot, threaded writer
     __init__        spawn StateWriter daemon, bounded queue, counters
     save            non-blocking enqueue; drop-oldest on Full; converge latest
     _writer_loop    drain queue → disk; timeout poll; shutdown flush
     _write_to_disk  temp → fsync → os.replace → parent fsync (atomic+durable)
     load            read snapshot; None on missing/corrupt
     clear           delete the state file
     close           stop+join writer, return {written, dropped, path}
   class AuditLog                                lean append-only CSV trail
     __init__        bind timestamp fn; write header if new
     append          one CSV row per event; data → json blob
     read            DictReader; return last `limit` entries

─── 変  Variables / state created ─────────────────────────────────────────
   StateStore.__slots__  path _queue _thread _running _dropped _written _last_state
   path                 str        snapshot file (default .gt_state.json)
   _queue               Queue(8)   hot-path → writer handoff; depth bounded on purpose
   _thread              Thread     daemon "StateWriter" — the sole disk writer
   _running             bool       writer-loop kill switch; flipped by close()
   _dropped             int        stale snapshots evicted under backpressure
   _written             int        snapshots flushed to disk (close() reports both)
   _last_state          dict|None  most-recent state, readable without racing writer
   AuditLog.__slots__   path _ts
   LOG_FILE             ".gt_audit.csv"   class default path
   HEADER               "timestamp,event,trade_id,data\n"   written once on create
   _ts                  callable   datetime.now, bound at init

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   stdlib only — no project imports (leaf node).
   json.dump/json.load · os.fsync/os.replace/os.open/os.remove/os.path.*
   queue.Queue.put_nowait/get_nowait/get · threading.Thread
   csv.writer/csv.DictReader · datetime.now · print(…, file=sys.stderr)
   (internal) AuditLog.read → AuditLog.append is the only intra-file CALLS edge

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   imports_of (6 files):
     run_live.py            ▷ constructs StateStore + AuditLog per bot
     live_trading.py        ▷ legacy live entrypoint
     src/config/__init__.py ▷ re-exports the package surface  → [[__init__]]
     src/strategy/engine.py ▷ the principal consumer          → [[engine]]
     test_live.py · test_paper_simulation.py
   StateStore.save  ← Engine._save_state (engine.py:2005)   ▷ the ONE caller
   AuditLog.append  ← 47 callers across the fleet:
     engine: _on_gateway_fill · _force_market_exit · _place_entry_stop_limit
             · _place_protective_stop · audit · get_status
     broker: get_positions · fetch_open_orders · _check_pending_limits → [[broker]]
     alerts.raise_alert → [[alerts]] · audit._AuditWriter.run → [[audit]]
     (note: many "append" edges are list.append name-collisions in feed/*,
      dashboard, tests — only the AuditLog instances are true callers)

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Hot path stays lock-free.  save() never touches disk; the asyncio loop
    can call it on every state transition without spiking order latency.
   • Drop-the-OLDEST, never the newest.  State is a single snapshot — if the
    writer falls behind, the freshest state is what disk must converge on.
    _dropped is expected to be > 0 under load and is NOT an error.
   • Atomic + durable, in that order.  os.replace gives "old or new, never
    partial"; the two fsyncs (file then parent dir) defend the bytes AND the
    rename against power loss.  Skip fsync silently on filesystems that lack
    it — degrade the guarantee, never the write.
   • A renamed-but-empty .gt_state is the nightmare: load() would return a
    near-{} snapshot, the engine boots IDLE, and reconcile re-places orders
    against a position it forgot.  Step 14's file-fsync exists to prevent
    exactly this.  See 流 engine steps 4-8 for the read-back contract.
   • Corrupt snapshot = cold start, not a crash.  load() swallows
    JSONDecodeError/IOError → None; the engine then reconciles from broker
    + ledger truth rather than trusting a torn file.
   • AuditLog is a TAPE, not truth.  Append-only, no fsync, no rotation here;
    it is forensic, read after the fact.  The durable source of truth for
    position is the FillLedger → [[fill_ledger]], reconciled in [[engine]].
   • Same thread-off-hot-path pattern as the audit subsystem → [[audit]].
   • Links:  state read-back → [[engine]] · package surface → [[__init__]]
    orders writing audit → [[broker]] · alerts → [[alerts]] · tape → [[audit]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
