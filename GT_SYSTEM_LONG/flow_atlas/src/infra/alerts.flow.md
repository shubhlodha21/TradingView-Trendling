━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 49 ·  src/infra/alerts.py
  the nervous system — every anomaly named, fanned out, never swallowed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1091 lines · Jane-Street-style event monitoring: enumerate every edge case,
  alert on SYMPTOMS not causes, defense-in-depth, "trade too good" detection.
  One AlertManager fans each Alert to many channels; none may block the hot
  path, none may raise. Every order is critical — nothing fails silently.

要 Require ┊ nothing hard — pure-stdlib (queue · threading · urllib · json)
          ┊ optional env: GT_TEAMS_WEBHOOK_URL (primary), GT_SLACK_WEBHOOK (legacy)
出 Provides┊ AlertManager · AlertSeverity · Alert · AnomalyDetector
          ┊ channels: File · Print · Slack · Teams   · build_default_alert_manager()

─── 部  Modules used ─────────────────────────────────────────────────────
   stdlib only ┊ typing · dataclasses · datetime · enum · collections.defaultdict
               ┊ queue · threading · time   (urllib/json imported lazily in workers)
   no project imports → a LEAF of the graph (everyone calls in; it calls nothing back)

─── 算  Algorithm · the alert path ───────────────────────────────────────
 Require: an AlertManager with ≥1 channel attached
 Ensure : the alert reaches every channel, off the hot path, without raising

  1: build_default_alert_manager(dir, stdout)   ▷ wiring, at boot
  2:   add_channel(FileChannel)                  ▷ always — daily JSONL, thread-locked
  3:   add_channel(PrintChannel)                 ▷ if stdout — ANSI by severity
  4:   if GT_TEAMS_WEBHOOK_URL → add TeamsChannel ▷ primary (Adaptive Cards)
  5:   if GT_SLACK_WEBHOOK     → add SlackChannel ▷ legacy fallback (Block Kit)
  6: ── anomaly path ──  caller: AlertManager.raise_alert(code, sev, msg, ctx, cid)
  7:   if code ∉ ALERT_CODES and not code.startswith("CUSTOM_") → print warning ▷ catalog discipline
  8:   Alert(...) built  ; appended to _history ; _alert_counts[code]++   (under _lock)
  9:   for channel in _channels: channel.send(alert)   ▷ each wrapped in try — a bad
     │                                                    channel can never break the others
 10: ── operational path ──  caller: AlertManager.notify_trade(event, **fields)
 11:   fans to channels that implement send_trade()   ▷ FILLED/REJECTED/SESSION_*; does
     │                                                    NOT touch _history (not an anomaly)
 12: ── channel internals (Slack / Teams, identical shape) ──
 13:   send()/send_trade() → _enqueue(payload)         ▷ NON-BLOCKING; returns instantly
 14:   _enqueue: put_nowait → queue.Full ⇒ _dropped++  ▷ drop, never block; print 1st & 100th
 15:   _worker_loop (daemon thread): get → _take_token (token bucket 4/s, burst 10)
 16:       → urllib POST(timeout 5) ; 200/202 ⇒ _sent++ ; 429 ⇒ sleep 1s + re-enqueue once
 17:   close(timeout): drain queue to deadline → _stopped ; join ; return drop stats
 18: ── FileChannel ──  send(): _ensure_open (rolls daily) → write JSON line → flush  (under _lock)
 19: ── AnomalyDetector ──  set_baseline → record_trade → check_all:
 20:   check_pnl_anomaly()    ▷ rate > baseline×(1+50%) ⇒ PNL_TOO_GOOD (CRITICAL — "we made
     │                          too much money" = a bug in the stack); too-low ⇒ PNL_LOSS_EXCESSIVE
 21:   check_volume_anomaly() ▷ our share > 10% of market volume ⇒ VOLUME_ANOMALY (CRITICAL)

─── 関  Functions / classes defined ──────────────────────────────────────
   enum   AlertSeverity            CRITICAL · HIGH · MEDIUM · LOW
   data   Alert                    code·severity·message·timestamp·context·correlation_id · to_dict()
   base   AlertChannel             .send(alert)  (interface; raises NotImplementedError)
   chan   SlackChannel             queue+daemon worker+token-bucket; _format_alert/_format_trade (Block Kit)
   chan   TeamsChannel             same shape; Adaptive Cards; _style/_color_for_severity · _wrap_card
   chan   PrintChannel             ANSI stdout by severity
   chan   FileChannel              daily JSONL under data/alerts/, _ensure_open · thread-locked send
   fn     build_default_alert_manager(directory, enable_stdout) → AlertManager
   class  AlertManager             add_channel · raise_alert · notify_trade · close
                                   get_history · get_counts ; ALERT_CODES catalog (~40 codes)
   class  AnomalyDetector          set_baseline · record_trade · check_pnl_anomaly
                                   check_volume_anomaly · check_all

─── 変  Variables / state created ────────────────────────────────────────
   AlertManager._channels   list[AlertChannel]   fan-out targets
   AlertManager._history    list[Alert]          anomaly log (dashboard ALERTS panel)
   AlertManager._alert_counts defaultdict[int]   per-code tally  (get_counts)
   AlertManager.ALERT_CODES dict                 the catalog — every edge case named
   *Channel._queue/_tokens/_sent/_dropped/_http_errors   bounded queue + token bucket
   AlertSeverity colour/style maps              severity → ANSI / hex / Adaptive-Card style

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   (leaf) only stdlib — urllib POST to the webhook; file write; print. No project calls.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   strategy.engine        ▷ raise_alert(NAKED_POSITION, POSITION_MISMATCH, ENTRY_ORDER_MISSING,
                             STALE_FEED, CONNECTION_LOST, CUSTOM_LEDGER_SHORT, PHANTOM_SELL_REJECTED…)
   execution.broker       ▷ connection / order anomalies
   feed.handler/connection▷ STALE_FEED · CONNECTION_LOST · RECONNECTION_FAILED
   run_live.py            ▷ build_default_alert_manager at boot · close() on shutdown
   tests + dashboard      ▷ read data/alerts/*.jsonl (FileChannel output)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Symptom, not cause.  Codes name what was OBSERVED (NAKED_POSITION), so a new
     bug still trips an existing alarm. Catalog every edge case in ALERT_CODES; a
     `CUSTOM_*` prefix is the only sanctioned un-cataloged code.
   • Never block, never raise.  Webhook sends are queued to daemon workers; queue-full
     drops are counted, not awaited. raise_alert wraps every channel in try.
   • Alert vs trade.  raise_alert → _history (anomalies, dashboard). notify_trade →
     channels only (operational FILLED/REJECTED noise stays out of the anomaly log).
   • "Trade too good."  PNL_TOO_GOOD is CRITICAL — unexpected profit means a stack bug,
     not luck. The same detector also catches excessive loss and volume share.
   • The JSONL at data/alerts/alerts_<date>.jsonl is the off-line truth we grep when
     diagnosing live episodes (e.g. the 310 CONNECTION_LOST / phantom-short post-mortems).
   • Links:  raised from → [[engine]] · [[broker]] · [[handler]] · persistence → [[audit]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
