# RTH Trendline Signals

Draw a trendline between two points and get a trade signal when price crosses it
— with the line advancing in **market time**, not wall-clock time.

## The problem this solves

You have two data points: `(t_past, price_past)` and `(t_future, price_future)`,
plus a ticker, an asset class and a direction (UP/DOWN). You want the line
between them, and a signal when price crosses it.

The trap is the time axis. Interpolating on wall-clock seconds bakes closed
market time into the slope:

```
Anchor 1: Fri 2025-01-03 20:00 UTC @ 100.00
Anchor 2: Mon 2025-01-06 15:00 UTC @ 105.00

wall clock : 241,200 seconds   (67 hours)
market time:   5,400 seconds   (1h Friday + 30min Monday)  <- 97.8% is dead time
```

At Monday's open a wall-clock line reads **104.96** — it has spent almost all of
its move on a weekend the market never traded. The correct value is **103.33**.
That is 1.63 of drift on a 5.00 range: the line is wrong by a third of its own
height before the week even starts.

So the whole system runs on an RTH clock:

```
span    = rth_seconds(t_past → t_future)          # weekends/holidays = 0
slope   = (price_future - price_past) / span      # price per market second
line(t) = price_past + slope * rth_seconds(t_past → t)
```

The line holds flat across every gap and resumes exactly where it left off.

## Running it

Two front ends over the same engine: a headless CLI ([live.py](live.py)) and a
Streamlit UI ([app.py](app.py)).

### Headless, against Interactive Brokers

This is the one to run on a server. No UI, no browser — one line per instrument
per second on stdout.

Each instrument is five inputs: **ticker**, **time1 + price1** (the past
anchor), **time2 + price2** (the future one), and **UP/DOWN**. Everything else
— which calendar applies, the slope in market seconds, the line's price right
now — is derived.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python3 live.py --ticker AAPL \
    --start "2025-01-03 20:00" --start-price 100.00 \
    --end   "2025-01-06 15:00" --end-price   105.00 \
    --direction UP
```

```
ticker=AAPL market=EQ:XNYS mode='RTH (regular session)' direction=UP trigger=last feed=ibkr
line: 2025-01-03 20:00:00Z @ 100.0000  ->  2025-01-06 15:00:00Z @ 105.0000   [5,400 market
seconds over 2 sessions, 97.8% of the wall clock excluded, slope +3.3333/market hour]
[status] connected: AAPL STK SMART/USD conId=265598

time (utc)                rth_s        line       price         gap  state    age
----------------------------------------------------------------------------------
2025-01-06 14:59:58Z       5398    103.3315    103.2900     -0.0415  below     0s
2025-01-06 14:59:59Z       5399    103.3324    103.3100     -0.0224  below     0s
2025-01-06 15:00:00Z       5400    103.3333    103.3900     +0.0567  above     1s

*** SIGNAL BUY AAPL *** 2025-01-06 15:00:00Z  price 103.3900 vs line 103.3333
(gap +0.0567, 5,400 market seconds in)
```

Each row is the comparison the signal is made of: the market-time offset, the
**trendline's datapoint** at that instant, the **live IBKR price**, and the gap
between them. `--format json` emits the same thing as one JSON object per line,
with the bid/ask/last/volume from IB and both `epoch` and `rth_seconds` timing:

```bash
python3 live.py --ticker EURUSD --sec-type CASH --format json \
    --start "2025-01-03 08:00" --start-price 1.0850 \
    --end   "2025-01-08 17:00" --end-price   1.0990 | tee ticks.jsonl
```

```json
{"type":"tick","ticker":"EURUSD","time":"2025-01-06 15:00:00Z","epoch":1736175600.0,
 "rth_seconds":5400.0,"line_price":1.08573,"price":1.08610,"gap":0.00037,"state":"above",
 "beyond_line":true,"direction":"UP","age_seconds":0.2,"bid":1.08609,"ask":1.08611,
 "last":1.08610,"high":1.08612,"low":1.08604,"volume":null,"source":"ibkr"}
{"type":"signal","side":"BUY","ticker":"EURUSD","time":"2025-01-06 15:00:00Z", ...}
```

The IB socket streams on its own thread while the main loop walks the RTH clock,
so quotes never queue behind the printer and the printer never blocks on the
socket. Prerequisites: TWS or IB Gateway running with **Configure → API →
Settings → Enable ActiveX and Socket Clients** ticked, and `pip install
ib_async`. Ports: `7497` TWS paper, `7496` TWS live, `4002` Gateway paper,
`4001` Gateway live — set with `--ib-port`, or `IB_PORT` in the environment.
Add `--delayed` if the account holds no live market-data subscription.

### Many tickers at once

One connection, one thread, one comparison cycle — every instrument carrying its
own line, its own calendar and its own detector. Repeat `--line`:

```bash
python3 live.py \
    --line "AAPL,2026-08-07 14:30,205.00,2026-08-12 19:00,212.00,UP" \
    --line "MSFT,2026-08-07 14:30,410.00,2026-08-12 19:00,395.00,DOWN" \
    --line "EURUSD,2026-08-07 08:00,1.0850,2026-08-12 17:00,1.0990,UP"
```

or put the basket in a file — `python3 live.py --example-config > lines.csv`
gives you the template:

```csv
ticker,time1,price1,time2,price2,direction
AAPL,2026-08-07 14:30,205.00,2026-08-12 19:00,212.00,UP
MSFT,2026-08-07 14:30,410.00,2026-08-12 19:00,395.00,DOWN
EURUSD,2026-08-07 08:00,1.0850,2026-08-12 17:00,1.0990,UP
BTC-USD,2026-08-07 00:00,58000,2026-08-12 00:00,62000,UP
```

```bash
python3 live.py --config lines.csv
```

Column names are matched loosely, so `past_value`/`future_value`,
`p1`/`p2`, `start_price`/`end_price` and `t1`/`t2` all resolve. Optional extra
columns — `market`, `session_mode`, `trigger`, `sec_type`, `exchange`,
`currency`, `csv_file` — override the command-line defaults per instrument.
JSON configs work too (a list of objects, or `{"lines": [...]}`).

Output gains a ticker column and instruments interleave within each cycle:

```
time (utc)           ticker        rth_s        line       price         gap  state    age
[status] [AAPL] market closed at 2026-08-03 00:00:00Z -- next open 2026-08-03 13:30:00Z
[status] [EURUSD] the trendline starts at 2026-08-03 08:00:00Z -- not comparing yet
2026-08-03 08:32:09Z EURUSD         1929     1.08503     1.08896    +0.00393  above     0s
2026-08-03 08:32:09Z BTC-USD       30729  58129.3314  60711.6725  +2582.3412  above     0s

*** SIGNAL BUY EURUSD *** 2026-08-03 08:32:09Z  price 1.08896 vs line 1.08503 ...
```

Notice what the mixed basket does on its own: **each instrument keeps its own
calendar**, so US equities sit out the Asian session while FX and crypto keep
comparing, and each line goes live only once its own past anchor is reached.
Price precision follows the asset class (5 dp for FX, 4 otherwise).

Practical notes for baskets:

- **One socket, not N.** IB counts API *connections* against a client id, not
  symbols, so thirty tickers still means one `--ib-client-id`. A standard
  account allows 100 concurrent streaming lines; past that IB rejects the
  excess and the runner says so.
- **One bad ticker doesn't sink the run.** A symbol IB can't identify, or one
  you lack data permissions for, is reported against that instrument and
  skipped; the rest carry on. IB's own error messages are routed to the
  instrument that caused them.
- **`--stop-when-signalled`** retires each instrument after its first signal and
  exits when the whole basket is done. `--exit-on-signal` stops the moment any
  one of them fires.

Useful flags — `--help` lists them all:

| Flag | Effect |
|---|---|
| `--config FILE` / `--line SPEC` | Define a basket; `--line` is repeatable |
| `--record DIR\|FILE` | Collect every observation to disk; CSV recordings replay through `--feed csv` |
| `--signal-mode above` | Fire on *every* tick the price is past the line, not just the crossing tick |
| `--repeat` / `--cooldown N` | Keep signalling on re-crosses, no more than one per `N` market seconds |
| `--trigger touch` | The wick reaching the line is enough — how a resting stop would fill |
| `--log-signals FILE.csv` | Append every signal, from every instrument, to one CSV |
| `--export-line FILE.csv` | Dump every trendline: `ticker, timestamp, rth_seconds, line_price` |
| `--dump-line 20` | Print the first 20 datapoints of each line and exit |
| `--quiet` | Signals only, no per-tick rows — sensible for a large basket |
| `--feed simulated --replay-speed 300` | No broker needed — replay the window at 300× to see the whole path |
| `--exit-on-signal` / `--stop-when-signalled` | Stop on the first signal, or retire instruments as they fire |

Running it unattended:

```bash
nohup python3 live.py --config lines.csv --format json \
    --log-signals signals.csv >> ticks.jsonl 2>&1 &
```

`SIGINT`/`SIGTERM` shut the IB connection down cleanly and print a run summary.

### Under tmux

[tmux.sh](tmux.sh) sets up the session for you — survives an SSH drop, which
`nohup` alone does not give you a way back into.

```bash
sudo apt install tmux
./tmux.sh lines.csv                    # or: bash tmux.sh lines.csv
```

That opens one window split between the live tick stream and a `tail -F` of the
signal log. Detach with `Ctrl-b d`, come back with `tmux attach -t rth`.

```bash
./tmux.sh --per-ticker lines.csv       # one window per instrument
```

Which to pick:

| | single process (default) | `--per-ticker` |
|---|---|---|
| IB connections | 1 | one per instrument |
| Restart one ticker | no — restarts all | yes, just that window |
| One ticker crashes | takes the rest down | others keep running |
| Signal log | one `logs/signals.csv` | `logs/signals-TICKER.csv` each |

The default is usually right: one connection is how IB prefers to be talked to,
and the basket already evaluates every instrument in the same cycle. Reach for
`--per-ticker` when you want to restart instruments independently or keep their
scrollback separate.

**The detail that bites:** every API connection needs a distinct client id. TWS
accepts a duplicate by silently dropping the older connection, which looks like
a feed that mysteriously goes quiet. In `--per-ticker` mode the script hands out
`17, 18, 19, …` automatically (`IB_CLIENT_ID` sets the base).

Anything after the config file passes straight through to `live.py`:

```bash
./tmux.sh lines.csv --delayed --interval 2 --trigger touch
./tmux.sh --per-ticker lines.csv --ib-port 4002
RTH_SESSION=fx ./tmux.sh fx-lines.csv          # a second, independent session
```

The two flags the script leans on are useful directly too: `--list-instruments`
prints the tickers a config resolves to, and `--only TICKER` (repeatable) runs
just part of a basket — which is how one config file drives several processes.

```bash
tmux kill-session -t rth               # stop everything
```

### Collecting the live data

`--record` writes every observation to disk as it arrives — the quote, the
trendline's price beside it, and the gap:

```bash
python3 live.py --config lines.csv --record data/          # a directory
python3 live.py --config lines.csv --record ticks.jsonl    # one file
```

A directory gets one file per instrument per UTC day
(`data/AAPL-20260803.csv`); a name ending `.csv` or `.jsonl` gets one combined
file. `--no-rotate` keeps one file per instrument for the whole run.

```csv
timestamp,price,high,low,ticker,rth_seconds,line_price,gap,state,beyond_line,direction,bid,ask,...
2026-08-03T15:15:05+00:00,206.7382,206.7461,206.7301,AAPL,2706.0,205.0835,1.6547,above,True,UP,...
```

Two things make this more than a log:

**It replays.** The first four columns are exactly what `CsvFeed` reads, so a
recording feeds straight back into the same engine that produced it:

```bash
python3 live.py --line "BTC-USD,2026-08-03 00:00,58000,2026-08-14 00:00,62000,UP" \
    --feed csv --csv-file data/BTC-USD-20260803.csv
```

Today's session becomes tomorrow's backtest, and a signal can be reproduced
rather than argued about. High/low are carried through, so `--trigger touch`
behaves the same on a replay as it did live.

**Quotes are perishable.** IB serves no second-resolution history beyond a short
window, and none at all of what your particular subscription saw. Unwritten, it
is gone.

Every row is flushed as it is written, so a `kill -9` loses at most the row in
flight, and a restart appends to the day's file rather than truncating it.
`--quiet` suppresses the printed rows but never the recording.

### Handing a signal to GT_SYSTEM_LONG / GT_SYSTEM_SHORT

A signal can start the execution bot for that instrument, in the folder that
matches the direction:

| Trendline | Signal | Folder |
|---|---|---|
| UP | BUY | `GT_SYSTEM_LONG` |
| DOWN | SELL | `GT_SYSTEM_SHORT` |

```bash
python3 live.py --config lines.csv --gt-root .        # print only
python3 live.py --config lines.csv --gt-root . --on-signal tmux
```

`--gt-root` is the folder holding `GT_SYSTEM_LONG` and `GT_SYSTEM_SHORT` — `.`
if they sit beside `live.py`, or `$GT_ROOT`.

`--on-signal` is the arming switch and **defaults to `print`** — it shows the
exact command and runs nothing:

```
*** SIGNAL BUY AAPL *** 2026-08-03 17:31:01Z  price 207.7985 vs line 205.3352 ...
[status] [AAPL] would run: (cd ~/Code/GT_SYSTEM_LONG && GT_PAPER=false python3 \
  run_live.py AAPL --trigger 205.34 --port 7496 --client-id 101 \
  --offset-entry-pct 0.001 --stop 0.0025 --uvloop --qty 512)
```

`--on-signal tmux` opens a window per trade (named `AAPL-LONG`, `MSFT-SHORT`),
`--on-signal process` spawns it detached with a log file. Both print a banner
and ask you to type `ARM` first; `--yes` skips the prompt for unattended runs.

#### Market entry

The launched bot enters **at market** by default (`--exec-entry market`), which
passes `--market` to `run_live.py`. The reasoning: GT normally rests a STP-LMT
at the trigger and lets IBKR detect the crossing — but in this arrangement
*this* runner already detected it, so there is nothing left to wait for, and a
limit the tape has already passed would never fill.

`--market` is a change to GT itself, present in both `GT_SYSTEM_LONG` and
`GT_SYSTEM_SHORT`: the bracket's parent leg becomes a plain MARKET order while
the protective child, the `parentId`/`transmit` handshake and the orphan-cancel
guard are all untouched. It is off unless the flag is passed, so GT's historical
behaviour is unchanged for every other caller.

**A market order has no price ceiling (long) or floor (short).** It fills at
whatever the book offers when it lands — process start, IB connect and contract
qualification all sit between the signal and the fill. On a fast tape that gap
is your slippage. `--exec-entry stop-limit` goes back to the resting STP-LMT
with `--offset-entry-pct`, trading certainty of fill for control of price.

**`--trigger` is the crossing price.** By default that is the *trendline level*
that was crossed — the level the order is meant to act on — rounded to the
instrument's quoting precision (cents for equities, pips for FX).
`--exec-trigger last` uses the traded price that confirmed the cross instead.

**The client id advances on every launch and persists across restarts**
(`logs/.gt_client_id`). This matters more than it looks: TWS resolves a
duplicate client id by dropping the *older* connection, so a reused id would
silently disconnect a bot that is holding a live position. The counter starts at
`--exec-client-id` (default 100), well clear of the signal runner's own
`--ib-client-id`.

Sizing comes from `--exec-qty`, `--exec-stop`, `--exec-offset-entry-pct`,
`--exec-port`, or per instrument from the config:

```csv
ticker,time1,price1,time2,price2,direction,qty,stop,offset_entry_pct
AAPL,2026-08-07 14:30,205.00,2026-08-12 19:00,212.00,UP,512,0.0025,0.001
MSFT,2026-08-07 14:30,410.00,2026-08-12 19:00,395.00,DOWN,128,0.005,0.002
```

The whole command is a template if your flags differ:

```bash
--exec-template "GT_PAPER=false python3 run_live.py {ticker} --trigger {trigger} \
  --port {port} --client-id {client_id} --stop {stop} --qty {qty} --cfd --uvloop"
```

Placeholders: `{ticker} {trigger} {client_id} {port} {qty} {stop}
{offset_entry_pct} {direction} {side}`. An unknown one is rejected at build
time, not at 09:30.

Guards, because this places real orders:

- Default is `print`. Arming is explicit, confirmed, and announced.
- A missing `GT_SYSTEM_LONG`/`GT_SYSTEM_SHORT`, or one without `run_live.py`, is
  caught at startup — armed mode refuses to run rather than failing at the first
  signal.
- One launch per ticker per run (`--exec-repeat` to allow more), so a re-cross
  cannot double the position.
- Every launch is appended to `logs/launches.csv`: time, ticker, direction,
  folder, client id, trigger, status, and the verbatim command.

**On a headless box**, note that IB Gateway is a desktop app. Either run it on a
machine with a display and point `--ib-host` at it, or drive it under
[IBC](https://github.com/IbcAlpha/IBC) with `Xvfb` — a tmux window is a fine
place to keep that too, but it is a separate concern from this runner.

### Streamlit UI

```bash
streamlit run app.py
```

Then in the sidebar: type a ticker, pick the session definition, set the two
anchors and a direction, and press **Start**.

### Tests

```bash
python3 tests/test_engine.py     # 61 assertions over clock/line/crossing
python3 tests/test_ibkr.py       # 69 assertions over the IB feed + CLI, no broker needed
python3 tests/test_execution.py  # 49 assertions over the GT hand-off, nothing executed
python3 tests/test_recording.py  # 23 assertions over collection and replay
```

## What counts as "open"

| Asset class | Source | Notes |
|---|---|---|
| Equities (65 venues) | `exchange_calendars` | Real holidays, half-days, lunch breaks (XTKS/XHKG/XSHG). US venues also offer an extended 04:00–20:00 ET book. |
| Index / metals / energy CFDs | `CMES`, `XEUR`, `IEPA` calendars | Genuine futures calendars, plus the 16:00–17:00 CT maintenance halt carved back out. Each also offers the underlying cash-session RTH. |
| Spot FX | Weekly template | Sun 17:00 → Fri 17:00 New York, DST-correct. Optional London / New York / Tokyo / overlap windows, since "RTH" for FX is a choice rather than a fact. |
| Crypto | 24/7 | No gaps at all. |

Excluded everywhere: weekends, exchange holidays, closed hours, lunch breaks,
and (for equity RTH) pre/post-market.

FX and CFD holiday sets are approximations — brokers differ by a few hours
around Christmas and Good Friday. Override `otc_holidays()` in
[rth/sessions.py](rth/sessions.py) to match yours.

## Answers to the setup questions

| Question | Answer |
|---|---|
| Timezone | UTC everywhere, in and out |
| Calendars | `exchange_calendars`, real half-days included |
| Output | Scalar RTH-second count **and** the full timestamp grid — both downloadable as CSV |
| Anchors on a weekend | Snapped forward to the next open. The dead time measured zero seconds anyway, so no arithmetic changes; the anchor just gets an honest timestamp. Flagged in the UI. |
| Granularity | 1 second (5s/15s/30s/60s/5m also available) |
| Line extent | Strictly `[t_past, t_future]`. Past the end anchor the line is finished and stops signalling. |
| Stack | Python + Streamlit + Plotly |

## Layout

```
live.py                Headless CLI — one or many tickers, line vs live price, signals
tmux.sh                tmux launcher — split window, or one window per instrument
app.py                 Streamlit UI
rth/sessions.py        Session calendars — what "open" means per instrument
rth/clock.py           RTH clock: elapsed / advance / grid / is_open
rth/trendline.py       The line, parameterised by market seconds
rth/crossing.py        Cross detection (streaming + batch)
rth/feeds.py           Price feeds: simulated, CSV, Yahoo, broker stub
rth/ibkr.py            Interactive Brokers: one session, many instruments
rth/execution.py       Signal -> GT_SYSTEM_LONG / GT_SYSTEM_SHORT hand-off
rth/recording.py       Live-feed collection, in a replayable format
tests/test_engine.py    61 assertions over the engine
tests/test_ibkr.py      69 assertions over the IB feed and the CLI
tests/test_execution.py 49 assertions over the execution hand-off
tests/test_recording.py 23 assertions over collection and replay
```

### The API directly

```python
from rth import RthClock, Anchor, Trendline, CrossDetector, get_market
import pandas as pd

clock = RthClock(get_market("EQ:XNYS").provider("RTH (regular session)"))
line = Trendline(
    clock,
    Anchor(pd.Timestamp("2025-01-03 20:00", tz="UTC"), 100.0),
    Anchor(pd.Timestamp("2025-01-06 15:00", tz="UTC"), 105.0),
    direction="UP",
)

line.span_seconds                 # 5400.0   market seconds, not 241200
line.price_at("2025-01-04 12:00") # 103.33   Saturday reads Friday's close
line.series(step_seconds=1)       # every valid timestamp + offset + line price

det = CrossDetector(line, mode="last")
det.update(timestamp, price)      # returns a CrossEvent, or None
```

## Price feeds

`SimulatedFeed` is the default so the app runs with no credentials — a seeded
mean-reverting walk around the line, which reproduces exactly for a given seed.

[rth/ibkr.py](rth/ibkr.py) is the live one — streaming quotes from TWS or IB
Gateway, with `tick()` returning the latest price plus the high/low seen since
the previous call, which is what touch-mode triggers need. Contracts are routed
automatically: `AAPL` → `STK/SMART/USD`, `EURUSD` → `CASH/IDEALPRO`, `BTC-USD` →
`CRYPTO/PAXOS`, `VOD.L` → `STK/LSE/GBP`; override any of it with `--sec-type`,
`--exchange`, `--currency`, `--primary-exchange`.

`IBKRSession` owns the socket and `IBKRTicker` is one instrument on it, so a
basket shares a single connection:

```python
from rth.ibkr import IBKRSession

session = IBKRSession(port=7497)
aapl = session.add("AAPL")
eur  = session.add("EURUSD")
session.start()

aapl.tick(now)   # {'price': ..., 'high': ..., 'low': ..., 'bid': ..., 'age_seconds': ...}
session.failures # instruments IB rejected, each with .error explaining why
session.stop()
```

`IBKRFeed("AAPL")` is the single-instrument shorthand for the same thing.

Otherwise, upload second/tick bars as CSV (`timestamp`, `price`, and optionally
`high`, `low`, in UTC), or implement `BrokerFeed` in
[rth/feeds.py](rth/feeds.py) against another broker: two methods, `bars()` and
`tick()`.

The Yahoo Finance feed is real data but delayed and 1-minute at best. Use it to
sanity-check the line against actual prices, not to size trades off a crossing
timestamp.

## Two implementation notes

**pandas 3.0 resolution.** `DatetimeIndex` now defaults to *microsecond*
resolution while `Timestamp.value` is always nanoseconds. Mixing them scales
results by 1000 with no error raised. All conversions go through `_as_ns()` in
[rth/clock.py](rth/clock.py).

**Closed-market prints cannot signal.** A Saturday print carries the same RTH
offset as Friday's close — strictly greater than the previous bar's — so a
monotonicity filter alone would let it through and signal a cross the market
never saw. `CrossDetector.scan` drops closed-market rows explicitly.
