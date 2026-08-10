# GT_SYSTEM

Algo trading engine for IBKR. LTP-driven breakout-and-reentry strategy with
proactive STOP-LIMIT protection.

## Quick start (single symbol)

```bash
GT_PAPER=false python run_live.py NVDA --trigger 230.00 --stop 0.01 --qty 1 \
    --port 7497 --client-id 1 \
    --offset-fixed 0.05
```

- `--trigger`     entry price (BUY STOP-LIMIT activates when LTP rises to this)
- `--stop`        stop-loss percent of fill price (0.01 = 1%)
- `--qty`         share quantity per cycle
- `--port`        IBKR TWS / Gateway port (7497 = TWS Live, 7496 = TWS Paper, 4002 = IB Gateway Live)
- `--client-id`   IBKR client ID (must be unique per concurrent process)
- `--offset-fixed`     fixed dollar buffer between stop trigger and limit
- `--offset-stop-fraction`   alternative: scale buffer with stop distance (default 0.05 = 5%)
- `--offset-entry-pct`       scale entry buffer with price (default 0.0005 = 5 bps)
- `--paper`       force paper mode (else `GT_PAPER` env var decides)
- `--reset`       cancel orders + flatten + clear local state, then exit

State file: `.gt_state_<SYMBOL>_<CLIENTID>.json` (per process, no clobbering).

## Running multiple symbols

The current architecture is **multi-process**: one `run_live.py` invocation per
ticker, each with its own clientId. Each process is fully isolated — own
engine, own state file, own dashboard, own audit stream.

### Why multi-process, not multi-symbol-single-process

| | Multi-process (today) | Multi-symbol single process (future) |
|---|---|---|
| Risk isolation | One crash, one ticker | One crash, all stop |
| Account-wide risk gates | Per-ticker only | Aggregate possible |
| Code complexity | Zero refactor | ~500–700 LOC orchestration |
| Resource use | 1 process / ticker | Single process |
| Best for | Today, while validating | Many symbols, mature risk |

We're at Phase 1; multi-process is the right choice.

### Recipe (tmux / iTerm / separate terminals)

```bash
# Each in its own pane / terminal
GT_PAPER=false python run_live.py NVDA  --trigger 230.00 --stop 0.01 --qty 1 \
    --port 7497 --client-id 1 --offset-fixed 0.05

GT_PAPER=false python run_live.py TSLA  --trigger 280.00 --stop 0.01 --qty 1 \
    --port 7497 --client-id 2 --offset-fixed 0.05

GT_PAPER=false python run_live.py AAPL  --trigger 195.00 --stop 0.01 --qty 1 \
    --port 7497 --client-id 3 --offset-fixed 0.05
```

State files end up as:
```
.gt_state_NVDA_1.json
.gt_state_TSLA_2.json
.gt_state_AAPL_3.json
```

Audit files (already per-symbol):
```
data/audit/feed_NVDA_20260517.csv  / feed_TSLA_20260517.csv  / ...
data/audit/order_*  state_*  pnl_*
```

### Caveats (known limitations of multi-process)

1. **Risk gates are per-process, not account-wide.** `--max-trades-per-day 50`
   on NVDA + 50 on TSLA = up to 100 trades against your account, not 50.
   Same for `daily_loss_limit_pct` (each process treats it as % of full
   equity). Set per-symbol caps deliberately conservative if you care.

2. **clientId must be unique per process.** Reusing one clientId across two
   running processes will get the second one disconnected by IBKR.

3. **TWS connection limit.** TWS allows ~32 simultaneous API clientIds.
   IB Gateway has its own limits. For >10 symbols, consider IB Gateway with
   memory budget tuned.

4. **`--reset` is per-ticker.** Each process gets its own `--reset` run.

## Reset a single ticker

```bash
python run_live.py NVDA --reset --port 7497 --client-id 1
```

Cancels working orders for NVDA, flattens the position, removes the local
`.gt_state_NVDA_1.json`. Prompts for confirmation. Does not touch other tickers.

## Strategy summary

1. Place BUY STOP-LIMIT at `--trigger` (activates only when LTP rises to it).
2. On BUY fill, immediately place SELL STOP-LIMIT at `entry × (1 − stop_pct)`.
3. Track `_highest_price` while in position. The peak becomes the
   `previous_breakout_level` on SELL fill.
4. On SELL fill, place BUY STOP-LIMIT at `previous_breakout_level`.
5. Repeat.

Both entry and exit use STOP-LIMIT with a trigger ↔ limit buffer (see
`--offset-fixed` / `--offset-stop-fraction` / `--offset-entry-pct`). All
orders are placed with `tif=GTC` so they survive overnight; reconciliation
on restart re-attaches resting orders to the engine's fill pipeline.

## Files / layout

```
src/
  config/         # models, persistence, audit, loader
  execution/      # broker.py — IBKR gateway, order placement
  feed/           # IBKR market data + validation pipeline
  strategy/       # engine.py — state machine, risk gate
dashboard.py      # terminal UI
run_live.py       # main entry point
data/audit/       # per-symbol CSV audit logs (daily, gzipped after rollover)
.gt_state_*.json  # per-process strategy state (auto-managed)
```
