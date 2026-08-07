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

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then in the sidebar: type a ticker, pick the session definition, set the two
anchors and a direction, and press **Start**.

Verify the engine independently:

```bash
python tests/test_engine.py      # 61 assertions
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
app.py                 Streamlit UI
rth/sessions.py        Session calendars — what "open" means per instrument
rth/clock.py           RTH clock: elapsed / advance / grid / is_open
rth/trendline.py       The line, parameterised by market seconds
rth/crossing.py        Cross detection (streaming + batch)
rth/feeds.py           Price feeds: simulated, CSV, Yahoo, broker stub
tests/test_engine.py   61 assertions over the above
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

For real signals, either upload second/tick bars as CSV (`timestamp`, `price`,
and optionally `high`, `low`, in UTC), or implement `BrokerFeed` in
[rth/feeds.py](rth/feeds.py) against your broker: two methods, `bars()` and
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
