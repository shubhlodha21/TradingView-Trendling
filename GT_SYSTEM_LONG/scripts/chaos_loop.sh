#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# chaos_loop.sh — continuous FX chaos test runner for long-duration EC2
#
# Re-runs `python -m tests.paper.chaos_test --scenario restart-positions`
# on a configurable cadence (default 15 min) until Ctrl+C / SIGTERM.
# Survives single-run failures. Logs each iteration separately + emits a
# rolling summary line to the master log.
#
# USAGE
# ─────
#   ./scripts/chaos_loop.sh                     # 15-min cadence, foreground
#   ./scripts/chaos_loop.sh 1800                # 30-min cadence
#   CADENCE=900 ./scripts/chaos_loop.sh         # env-var override
#   SCENARIO=tws-disconnect ./scripts/chaos_loop.sh  # different chaos mode
#
# EC2 BACKGROUND DEPLOYMENT
# ─────────────────────────
#   # via nohup:
#   nohup ./scripts/chaos_loop.sh > logs/chaos_loop_main.log 2>&1 &
#   echo $! > logs/chaos_loop.pid
#
#   # via tmux (preferred — survives SSH disconnect, can reattach):
#   tmux new -d -s chaos_loop ./scripts/chaos_loop.sh
#   tmux attach -t chaos_loop      # to watch
#   # detach without killing: Ctrl+B then d
#
#   # stop:
#   kill -INT $(cat logs/chaos_loop.pid)    # nohup case
#   tmux send-keys -t chaos_loop C-c        # tmux case
#
# OUTPUT
# ──────
#   logs/chaos_loop/loop_<ts>.log           — master log (one per run)
#   logs/chaos_loop/loop_latest.log         — symlink to the active one
#   logs/chaos_loop/iter<N>_<ts>.log        — per-iteration full output
#   logs/chaos_loop/summary.csv             — append-only: iter,ts,verdict,elapsed
# ─────────────────────────────────────────────────────────────────────────

set -uo pipefail   # NOT -e — we WANT to survive single-iteration failures

# ── Resolve project root (script lives in <root>/scripts/) ──────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ── Config ──────────────────────────────────────────────────────────────
CADENCE="${1:-${CADENCE:-900}}"           # 900s = 15min default
SCENARIO="${SCENARIO:-restart-positions}"  # which chaos_test scenario
CMD_MODULE="${CMD_MODULE:-tests.paper.chaos_test}"   # FX by default
PYTHON_BIN="${PYTHON_BIN:-python3}"

LOG_ROOT="logs/chaos_loop"
mkdir -p "$LOG_ROOT"

LOOP_TS="$(date +%Y%m%d_%H%M%S)"
LOOP_LOG="$LOG_ROOT/loop_${LOOP_TS}.log"
SUMMARY_CSV="$LOG_ROOT/summary.csv"

# Symlink to active loop log so an EC2 operator can `tail -f loop_latest.log`
ln -sfn "$(basename "$LOOP_LOG")" "$LOG_ROOT/loop_latest.log"

# Initialize summary CSV header if file doesn't exist
if [[ ! -f "$SUMMARY_CSV" ]]; then
    echo "iter,timestamp,verdict,elapsed_s,exit_code,log_file" > "$SUMMARY_CSV"
fi

# ── Counters ────────────────────────────────────────────────────────────
iteration=0
passes=0
fails=0
errors=0
loop_start=$(date +%s)

# ── Helpers ─────────────────────────────────────────────────────────────
log_both() {
    # Print to stdout AND append to master loop log
    echo "$@" | tee -a "$LOOP_LOG"
}

print_summary() {
    local label="$1"
    local now=$(date +%s)
    local total=$((now - loop_start))
    local hh=$((total / 3600))
    local mm=$(((total % 3600) / 60))
    log_both ""
    log_both "════════════════════════════════════════════════════════"
    log_both "[$(date)] ${label}"
    log_both "  iterations: $iteration"
    log_both "  PASS  : $passes"
    log_both "  FAIL  : $fails"
    log_both "  ERROR : $errors"
    log_both "  uptime: ${total}s (${hh}h ${mm}m)"
    log_both "════════════════════════════════════════════════════════"
}

# ── Signal handling — Ctrl+C / SIGTERM ──────────────────────────────────
stop_requested=0
on_signal() {
    stop_requested=1
    log_both ""
    log_both "[$(date)] signal received — finishing current iteration then exiting"
}
trap on_signal INT TERM

# ── Banner ──────────────────────────────────────────────────────────────
log_both "════════════════════════════════════════════════════════"
log_both "[$(date)] FX CHAOS LOOP START"
log_both "  cadence : ${CADENCE}s  ($(($CADENCE / 60))min)"
log_both "  command : $PYTHON_BIN -u -m $CMD_MODULE --scenario $SCENARIO"
log_both "  log dir : $LOG_ROOT"
log_both "  master  : $LOOP_LOG"
log_both "  pid     : $$"
log_both "════════════════════════════════════════════════════════"

# ── Main loop ───────────────────────────────────────────────────────────
while [[ $stop_requested -eq 0 ]]; do
    iteration=$((iteration + 1))
    iter_ts=$(date +%Y%m%d_%H%M%S)
    iter_log="$LOG_ROOT/iter${iteration}_${iter_ts}.log"
    iter_start=$(date +%s)

    log_both ""
    log_both "════════════════════════════════════════════════════════"
    log_both "[$(date)] ITER #$iteration  →  $iter_log"
    log_both "  rolling: PASS=$passes  FAIL=$fails  ERROR=$errors"
    log_both "════════════════════════════════════════════════════════"

    # Run chaos test. Tee output to per-iter log (and to terminal so a
    # `tail -f loop_latest.log` operator can still see progress).
    # PIPESTATUS captures the chaos_test exit code, not tee's.
    "$PYTHON_BIN" -u -m "$CMD_MODULE" --scenario "$SCENARIO" 2>&1 | tee "$iter_log"
    exit_code=${PIPESTATUS[0]}

    iter_end=$(date +%s)
    elapsed=$((iter_end - iter_start))

    # Derive verdict from output
    if [[ $exit_code -ne 0 ]]; then
        errors=$((errors + 1))
        verdict="ERROR_exit_${exit_code}"
    elif grep -q "OVERALL: PASS" "$iter_log"; then
        passes=$((passes + 1))
        verdict="PASS"
    elif grep -q "OVERALL: FAIL" "$iter_log"; then
        fails=$((fails + 1))
        verdict="FAIL"
    else
        errors=$((errors + 1))
        verdict="ERROR_no_verdict"
    fi

    # Append summary row
    echo "${iteration},${iter_ts},${verdict},${elapsed},${exit_code},$(basename "$iter_log")" >> "$SUMMARY_CSV"

    log_both "────────────────────────────────────────────────────────"
    log_both "[$(date)] ITER #$iteration DONE  verdict=$verdict  elapsed=${elapsed}s"
    log_both "  cumulative: PASS=$passes  FAIL=$fails  ERROR=$errors  (of $iteration)"
    log_both "────────────────────────────────────────────────────────"

    # Stop if signal received during the run
    if [[ $stop_requested -eq 1 ]]; then
        break
    fi

    # Maintain cadence — sleep what's left of CADENCE since iter_start.
    # If iteration overran cadence, start the next one immediately.
    if [[ $elapsed -lt $CADENCE ]]; then
        remaining=$((CADENCE - elapsed))
        log_both "[$(date)] sleeping ${remaining}s until next iteration"
        # Sleep in background + wait so Ctrl+C interrupts sleep cleanly
        sleep "$remaining" &
        sleep_pid=$!
        wait $sleep_pid 2>/dev/null || true
    else
        log_both "[$(date)] iter ran ${elapsed}s ≥ ${CADENCE}s cadence — next starts immediately"
    fi
done

# ── Clean exit summary ──────────────────────────────────────────────────
print_summary "FX CHAOS LOOP STOPPED"
exit 0
