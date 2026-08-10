#!/usr/bin/env bash
#
# Launch the RTH trendline runner inside tmux.
#
# Two layouts:
#
#   ./tmux.sh lines.csv                 one process for the whole basket, in a
#                                       window split between the tick stream and
#                                       a live tail of the signal log
#
#   ./tmux.sh --per-ticker lines.csv    one window per instrument, each its own
#                                       process, plus a signals window
#
# Which to use:
#
#   The single process is the default and usually the right answer. Every
#   instrument shares one IB connection, one comparison cycle and one signal
#   log, which is exactly how IB wants to be talked to.
#
#   --per-ticker buys isolation instead: each instrument gets its own scrollback,
#   can be restarted on its own, and cannot be taken down by another's crash.
#   The cost is one IB API connection per instrument, so each process is given a
#   distinct --ib-client-id automatically (TWS refuses a duplicate id and drops
#   the older connection, which is a confusing failure to debug at 09:30).
#
# Anything after the config file is passed straight through to live.py:
#
#   ./tmux.sh lines.csv --delayed --interval 2
#   ./tmux.sh --per-ticker lines.csv --ib-port 4002 --trigger touch
#
# Including the execution hand-off, which stays opt-in:
#
#   ./tmux.sh lines.csv --gt-root ~/Code                     # print the command
#   ./tmux.sh lines.csv --gt-root ~/Code --on-signal tmux --yes
#
# With --on-signal tmux the launched bots open as further windows in this same
# session, named TICKER-LONG / TICKER-SHORT, so the whole desk is one attach.
#
set -euo pipefail

SESSION="${RTH_SESSION:-rth}"
BASE_CLIENT_ID="${IB_CLIENT_ID:-17}"
LOG_DIR="${RTH_LOG_DIR:-logs}"
PYTHON="${PYTHON:-python3}"
PER_TICKER=0

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

usage() {
    sed -n '2,37p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit "${1:-0}"
}

die() { echo "error: $*" >&2; exit 1; }

# -- arguments ---------------------------------------------------------------

CONFIG=""
EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --per-ticker) PER_TICKER=1; shift ;;
        --session)    SESSION="$2"; shift 2 ;;
        -h|--help)    usage 0 ;;
        *)
            if [[ -z "$CONFIG" && -f "$1" ]]; then
                CONFIG="$1"; shift
            else
                EXTRA+=("$1"); shift
            fi
            ;;
    esac
done

command -v tmux >/dev/null 2>&1 || die "tmux is not installed (apt install tmux)"
[[ -n "$CONFIG" ]] || die "no config file given. Try: $0 lines.csv  (see live.py --example-config)"

# Activate a venv if the project has one, so panes inherit the right interpreter.
ACTIVATE=""
[[ -f .venv/bin/activate ]] && ACTIVATE="source .venv/bin/activate; "

if tmux has-session -t "$SESSION" 2>/dev/null; then
    die "session '$SESSION' already exists. Attach with 'tmux attach -t $SESSION', \
kill it with 'tmux kill-session -t $SESSION', or pass --session NAME."
fi

mkdir -p "$LOG_DIR"

# Validate the config once, up front, rather than watching every pane die.
mapfile -t TICKERS < <(${PYTHON} live.py --config "$CONFIG" --list-instruments) \
    || die "could not read $CONFIG -- run '${PYTHON} live.py --config $CONFIG --list-instruments' to see why"
[[ ${#TICKERS[@]} -gt 0 ]] || die "$CONFIG defines no instruments"

# `exec bash` keeps the pane alive after the runner exits, so a crash message
# stays on screen instead of the window vanishing with it.
hold() { echo "$1; echo; echo '[pane finished -- press enter or Ctrl-D to close]'; exec bash"; }

echo "session:     $SESSION"
echo "config:      $CONFIG  (${#TICKERS[@]} instruments: ${TICKERS[*]})"
echo "logs:        $LOG_DIR/"

if [[ $PER_TICKER -eq 1 ]]; then
    # -- one window per instrument -------------------------------------------
    echo "layout:      one window per instrument, client ids from $BASE_CLIENT_ID"

    tmux new-session -d -s "$SESSION" -n "signals"
    tmux send-keys -t "$SESSION:signals" \
        "cd '$here'; tail -F $LOG_DIR/signals-*.csv 2>/dev/null || tail -f /dev/null" C-m

    client_id=$BASE_CLIENT_ID
    for ticker in "${TICKERS[@]}"; do
        cmd="${ACTIVATE}${PYTHON} live.py --config $(printf '%q' "$CONFIG")"
        cmd+=" --only $(printf '%q' "$ticker")"
        cmd+=" --ib-client-id $client_id"
        cmd+=" --log-signals $(printf '%q' "$LOG_DIR/signals-$ticker.csv")"
        cmd+=" --record $(printf '%q' "$LOG_DIR/ticks")"
        for arg in ${EXTRA+"${EXTRA[@]}"}; do cmd+=" $(printf '%q' "$arg")"; done

        tmux new-window -t "$SESSION" -n "$ticker"
        tmux send-keys -t "$SESSION:$ticker" "cd '$here'; $(hold "$cmd")" C-m
        client_id=$((client_id + 1))
    done
    tmux select-window -t "$SESSION:signals"
else
    # -- one process, split window -------------------------------------------
    echo "layout:      single process, client id $BASE_CLIENT_ID"

    cmd="${ACTIVATE}${PYTHON} live.py --config $(printf '%q' "$CONFIG")"
    cmd+=" --ib-client-id $BASE_CLIENT_ID"
    cmd+=" --log-signals $(printf '%q' "$LOG_DIR/signals.csv")"
    cmd+=" --record $(printf '%q' "$LOG_DIR/ticks")"
    for arg in ${EXTRA+"${EXTRA[@]}"}; do cmd+=" $(printf '%q' "$arg")"; done

    tmux new-session -d -s "$SESSION" -n "rth"
    tmux send-keys -t "$SESSION:rth.0" "cd '$here'; $(hold "$cmd")" C-m

    # Bottom third: the signal log as it is written. -l NN% needs tmux >= 3.1;
    # -p is the older spelling and is deprecated in current releases.
    tmux split-window -v -l 30% -t "$SESSION:rth" 2>/dev/null \
        || tmux split-window -v -p 30 -t "$SESSION:rth"
    tmux send-keys -t "$SESSION:rth.1" \
        "cd '$here'; touch '$LOG_DIR/signals.csv'; tail -F '$LOG_DIR/signals.csv'" C-m
    tmux select-pane -t "$SESSION:rth.0"
fi

echo
echo "attach:      tmux attach -t $SESSION"
echo "detach:      Ctrl-b d          switch window: Ctrl-b n / Ctrl-b <number>"
echo "stop all:    tmux kill-session -t $SESSION"

[[ -n "${TMUX:-}" ]] && exit 0     # already inside tmux; do not nest
exec tmux attach -t "$SESSION"