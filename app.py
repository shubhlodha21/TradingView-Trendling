"""RTH Trendline Signals -- Streamlit front end.

Enter two anchor points (time + price, UTC) and a direction. The app measures
the gap between them in *market* seconds only -- weekends, holidays, half-days
and closed hours all count as zero -- projects the trendline across that
compressed axis, and signals when live price crosses it.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from rth import (
    ASSET_CLASSES,
    Anchor,
    CrossDetector,
    RthClock,
    SimulatedFeed,
    Trendline,
    YFinanceFeed,
    get_market,
    guess_market,
    list_markets,
)
from rth.crossing import events_to_frame
from rth.feeds import CsvFeed

st.set_page_config(page_title="RTH Trendline Signals", page_icon="📈", layout="wide")

UP, DOWN = "UP", "DOWN"
MAX_CHART_POINTS = 4000


# --------------------------------------------------------------------------- #
# sidebar: instrument and session definition
# --------------------------------------------------------------------------- #

st.sidebar.title("Instrument")

ticker = st.sidebar.text_input("Ticker", value="AAPL", help="Label for the feed.")
auto_detect = st.sidebar.checkbox(
    "Auto-detect market from ticker", value=True,
    help="EURUSD -> spot FX, AAPL -> Nasdaq, VOD.L -> LSE, XAUUSD -> metals, and so on.",
)

guessed_id = guess_market(ticker)
guessed = get_market(guessed_id)

if auto_detect:
    asset_class = guessed.asset_class
    st.sidebar.selectbox("Asset class", ASSET_CLASSES,
                         index=ASSET_CLASSES.index(asset_class), disabled=True)
else:
    asset_class = st.sidebar.selectbox("Asset class", ASSET_CLASSES)

markets = list_markets(asset_class)
market_ids = [m.market_id for m in markets]
default_idx = market_ids.index(guessed_id) if guessed_id in market_ids else 0
market = st.sidebar.selectbox(
    "Market / venue", markets, index=default_idx,
    format_func=lambda m: m.label, disabled=auto_detect,
)

session_mode = st.sidebar.selectbox(
    "Session definition", list(market.modes),
    help="What counts as 'open' for this instrument. Everything outside it "
         "measures zero seconds.",
)

st.sidebar.divider()
st.sidebar.title("Signal")

direction = st.sidebar.radio(
    "Direction", [UP, DOWN], horizontal=True,
    help="UP = go long when price crosses above the line. "
         "DOWN = go short when it breaks below.",
)
trigger_mode = st.sidebar.radio(
    "Trigger on", ["last", "touch"], horizontal=True,
    format_func=lambda m: "Last price through line" if m == "last" else "Wick touches line",
)
repeat = st.sidebar.checkbox("Signal on every re-cross", value=False)
cooldown = st.sidebar.number_input(
    "Cooldown between signals (market seconds)", 0, 86400, 0, step=60,
    disabled=not repeat,
)
step_seconds = st.sidebar.select_slider(
    "Resolution", options=[1, 5, 15, 30, 60, 300], value=1,
    format_func=lambda s: f"{s}s",
    help="Spacing of the market-time grid. 1s is the finest.",
)

st.sidebar.divider()
st.sidebar.title("Price feed")

feed_kind = st.sidebar.radio(
    "Source", ["Simulated", "Upload CSV", "Yahoo Finance"],
    help="Simulated runs with no credentials and is seeded, so it is "
         "reproducible. Swap in your broker via rth.feeds.BrokerFeed.",
)
uploaded = vol = seed = None
if feed_kind == "Simulated":
    vol = st.sidebar.slider("Volatility", 0.1, 4.0, 1.0, 0.1)
    seed = st.sidebar.number_input("Random seed", 0, 9999, 7)
elif feed_kind == "Upload CSV":
    uploaded = st.sidebar.file_uploader("Tick / second bars (UTC)", type=["csv"])
    st.sidebar.caption("Columns: `timestamp`, `price`, and optionally `high`, `low`.")
else:
    st.sidebar.caption(
        "Yahoo serves 1-minute bars at best, delayed, last ~7 days only. "
        "Fine for a sanity check against real prices; not an execution feed."
    )


# --------------------------------------------------------------------------- #
# anchors
# --------------------------------------------------------------------------- #

st.title("RTH Trendline Signals")
st.caption(
    "Two points define a line. The clock between them counts open-market seconds "
    "only — weekends, holidays, half-days and closed hours are worth zero."
)

now = pd.Timestamp.now(tz="UTC").floor("s")
price_fmt = "%.5f" if asset_class == "Currency" else "%.4f"
default_price = 1.0850 if asset_class == "Currency" else 100.0

left, right, meta = st.columns([1.1, 1.1, 0.8])

with left:
    st.subheader("Point 1 — past")
    d1 = st.date_input("Date (UTC)", (now - pd.Timedelta(days=3)).date(), key="d1")
    t1 = st.time_input("Time (UTC)", dt.time(14, 30), key="t1", step=60)
    p1 = st.number_input("Price", value=float(default_price), format=price_fmt, key="p1")

with right:
    st.subheader("Point 2 — future")
    d2 = st.date_input("Date (UTC)", (now + pd.Timedelta(days=1)).date(), key="d2")
    t2 = st.time_input("Time (UTC)", dt.time(19, 0), key="t2", step=60)
    p2 = st.number_input(
        "Price",
        value=float(default_price * (1.03 if direction == UP else 0.97)),
        format=price_fmt, key="p2",
    )

with meta:
    st.subheader("Clock")
    inside_now = None
    clock_mode = st.radio(
        "Drive the signal from", ["Replay", "Live"],
        help="Replay walks the window at a chosen speed so you can see the whole "
             "path immediately. Live uses the real UTC clock — it only produces "
             "ticks while the market is genuinely open.",
    )
    speed = st.select_slider(
        "Replay speed", options=[1, 10, 60, 300, 900, 3600], value=300,
        format_func=lambda s: f"{s}x", disabled=clock_mode != "Replay",
    )
    refresh = st.select_slider("Refresh", options=[1, 2, 5, 10], value=1,
                               format_func=lambda s: f"{s}s")

anchor_1 = pd.Timestamp.combine(d1, t1).tz_localize("UTC")
anchor_2 = pd.Timestamp.combine(d2, t2).tz_localize("UTC")


# --------------------------------------------------------------------------- #
# build the line
# --------------------------------------------------------------------------- #

@st.cache_resource(show_spinner=False)
def build_clock(market_id: str, mode: str) -> RthClock:
    return RthClock(get_market(market_id).provider(mode))


clock = build_clock(market.market_id, session_mode)

try:
    line = Trendline(clock, Anchor(anchor_1, p1), Anchor(anchor_2, p2), direction)
except ValueError as exc:
    st.error(str(exc))
    st.stop()

summary = line.summary()

if line.start_was_snapped or line.end_was_snapped:
    notes = []
    if line.start_was_snapped:
        notes.append(f"Point 1 {line.raw_start.time:%Y-%m-%d %H:%M} → **{line.start.time:%Y-%m-%d %H:%M}**")
    if line.end_was_snapped:
        notes.append(f"Point 2 {line.raw_end.time:%Y-%m-%d %H:%M} → **{line.end.time:%Y-%m-%d %H:%M}**")
    st.info(
        "Anchor(s) landed on a weekend, holiday or closed hour and were moved to "
        "the next open: " + "; ".join(notes) +
        ". The measured span is unchanged — that time was worth zero seconds anyway."
    )

if not line.direction_matches_slope:
    st.warning(
        f"The line **{'rises' if line.rises else 'falls'}** between your two points but you "
        f"asked for a **{direction}** signal. That is a valid "
        f"{'breakout' if direction == UP else 'breakdown'} setup — flagging it only in "
        f"case the prices are the wrong way round."
    )


# --------------------------------------------------------------------------- #
# headline numbers
# --------------------------------------------------------------------------- #

st.subheader("Market time between the two points")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("RTH seconds", f"{summary['rth_seconds']:,.0f}")
c2.metric("RTH hours", f"{summary['rth_hours']:,.2f}")
c3.metric("Sessions spanned", f"{summary['sessions']:,}")
c4.metric("Wall-clock seconds", f"{summary['wall_seconds']:,.0f}",
          help="What a naive calculation would have used.")
c5.metric("Closed time excluded", f"{summary['closed_pct']:.1f}%",
          delta=f"-{summary['closed_seconds']:,.0f}s", delta_color="off")

d1c, d2c, d3c = st.columns(3)
d1c.metric("Slope per market second", f"{line.slope:,.8f}")
d2c.metric("Slope per market hour", f"{line.slope_per_hour:,.5f}")
d3c.metric("Move per 6.5h session", f"{line.slope_per_session_day:,.5f}")

# The number that justifies the whole exercise: how far off a wall-clock line
# would be at the end anchor, having burned slope on closed time.
wall_span = summary["wall_seconds"]
if wall_span > 0 and summary["closed_seconds"] > 0:
    naive_slope = (p2 - p1) / wall_span
    drift = abs((p1 + naive_slope * summary["rth_seconds"]) - p2)
    st.caption(
        f"A wall-clock trendline would mis-price this line by **{drift:,.5f}** "
        f"at the end anchor — it spends {summary['closed_pct']:.1f}% of its slope on "
        f"time the market was shut."
    )

with st.expander(f"Sessions counted ({summary['sessions']})"):
    spans = clock.sessions_between(line.start.time, line.end.time)
    st.dataframe(
        pd.DataFrame([
            {
                "session_open_utc": o,
                "session_close_utc": c,
                "seconds": (c - o).total_seconds(),
                "hours": (c - o).total_seconds() / 3600,
            }
            for o, c in spans
        ]),
        width="stretch", hide_index=True,
    )


# --------------------------------------------------------------------------- #
# live / replay state
# --------------------------------------------------------------------------- #

signature = (
    market.market_id, session_mode, str(anchor_1), str(anchor_2), p1, p2, direction,
    trigger_mode, repeat, cooldown, step_seconds, feed_kind, vol, seed, clock_mode,
    speed, ticker, uploaded.name if uploaded else None,
)

if st.session_state.get("signature") != signature:
    if feed_kind == "Simulated":
        feed = SimulatedFeed(line, volatility=float(vol), seed=int(seed))
    elif feed_kind == "Upload CSV":
        feed = CsvFeed(uploaded) if uploaded is not None else None
    else:
        feed = YFinanceFeed(ticker)

    st.session_state.update(
        signature=signature,
        feed=feed,
        detector=CrossDetector(line, mode=trigger_mode, repeat=repeat,
                               cooldown_seconds=float(cooldown)),
        trendline=line,
        ticks=[],
        started_wall=pd.Timestamp.now(tz="UTC"),
        running=False,
    )

feed = st.session_state.feed
detector = st.session_state.detector

if feed is None:
    st.warning("Upload a CSV in the sidebar to drive the signal.")
    st.stop()


def current_market_time() -> pd.Timestamp:
    """Where the signal engine believes 'now' is, on the market clock."""
    if clock_mode == "Live":
        return pd.Timestamp.now(tz="UTC").floor("s")
    wall_elapsed = (pd.Timestamp.now(tz="UTC") - st.session_state.started_wall).total_seconds()
    return line.time_at_offset(min(wall_elapsed * speed, line.span_seconds))


run_col, reset_col, status_col = st.columns([0.18, 0.18, 0.64])
if run_col.button("▶ Start" if not st.session_state.running else "⏸ Pause",
                  width="stretch", type="primary"):
    if not st.session_state.running:
        st.session_state.started_wall = pd.Timestamp.now(tz="UTC")
    st.session_state.running = not st.session_state.running
    st.rerun()

if reset_col.button("↻ Reset", width="stretch"):
    detector.reset()
    st.session_state.ticks = []
    st.session_state.started_wall = pd.Timestamp.now(tz="UTC")
    st.session_state.running = False
    st.rerun()


# --------------------------------------------------------------------------- #
# chart
# --------------------------------------------------------------------------- #

def display_step(span: float) -> float:
    """Thin the *drawing* only. The detector always runs at full resolution."""
    return max(step_seconds, span / MAX_CHART_POINTS)


def build_chart(ticks: pd.DataFrame, marker_events, cursor_offset: float | None):
    draw_step = display_step(line.span_seconds)
    stamps, offsets = clock.grid(line.start.time, line.end.time, draw_step)
    line_prices = line.start.price + line.slope * offsets

    fig = go.Figure()

    # Session boundaries, so the compressed axis stays readable.
    running = 0.0
    for open_ts, close_ts in clock.sessions_between(line.start.time, line.end.time):
        running += (close_ts - open_ts).total_seconds()
        if running < line.span_seconds:
            fig.add_vline(x=running, line=dict(color="rgba(128,128,128,0.35)",
                                               width=1, dash="dot"))

    fig.add_trace(go.Scatter(
        x=offsets, y=line_prices, name="Trendline", mode="lines",
        line=dict(color="#2f80ed", width=2.5),
        customdata=np.array(stamps.strftime("%Y-%m-%d %H:%M:%S")),
        hovertemplate="<b>Trendline</b> %{y:.5f}<br>%{customdata} UTC<br>"
                      "%{x:,.0f} market s<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[0, line.span_seconds], y=[line.start.price, line.end.price],
        mode="markers", name="Anchors",
        marker=dict(size=12, color="#2f80ed", symbol="diamond",
                    line=dict(width=2, color="white")),
        hovertemplate="Anchor %{y:.5f}<extra></extra>",
    ))

    if not ticks.empty:
        fig.add_trace(go.Scatter(
            x=ticks["rth_seconds"], y=ticks["price"], name=f"{ticker} price",
            mode="lines", line=dict(color="#f2994a", width=1.4),
            customdata=np.array(pd.DatetimeIndex(ticks["timestamp"])
                                .strftime("%Y-%m-%d %H:%M:%S")),
            hovertemplate="<b>Price</b> %{y:.5f}<br>%{customdata} UTC<extra></extra>",
        ))

    if marker_events:
        buys = [e for e in marker_events if e.direction == UP]
        sells = [e for e in marker_events if e.direction == DOWN]
        for group, colour, symbol, label in (
            (buys, "#27ae60", "triangle-up", "BUY signal"),
            (sells, "#eb5757", "triangle-down", "SELL signal"),
        ):
            if group:
                fig.add_trace(go.Scatter(
                    x=[e.rth_seconds for e in group], y=[e.price for e in group],
                    mode="markers", name=label,
                    marker=dict(size=16, color=colour, symbol=symbol,
                                line=dict(width=1.5, color="white")),
                    hovertemplate=f"<b>{label}</b> @ %{{y:.5f}}<extra></extra>",
                ))

    if cursor_offset is not None:
        fig.add_vline(x=cursor_offset,
                      line=dict(color="rgba(242,153,74,0.8)", width=1.5))

    # Tick labels carry the wall-clock time; positions are market seconds, so
    # the weekend gap simply is not on the axis.
    n_ticks = 8
    tick_offsets = np.linspace(0, line.span_seconds, n_ticks)
    tick_times = [clock.advance(line.start.time, o) for o in tick_offsets]
    fig.update_layout(
        height=520, margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis=dict(
            title="Market time elapsed (closed hours removed)",
            tickmode="array", tickvals=tick_offsets,
            ticktext=[t.strftime("%b %d<br>%H:%M") for t in tick_times],
        ),
        yaxis=dict(title="Price"),
    )
    return fig


# --------------------------------------------------------------------------- #
# the live loop
# --------------------------------------------------------------------------- #

@st.fragment(run_every=refresh if st.session_state.get("running") else None)
def live_panel():
    detector = st.session_state.detector
    feed = st.session_state.feed
    ticks = st.session_state.ticks

    market_now = current_market_time()
    at_end = market_now >= line.end.time

    if st.session_state.running and not at_end:
        # Catch the engine up over every market second since the last tick, so a
        # crossing between refreshes is never stepped over.
        last_seen = ticks[-1]["timestamp"] if ticks else line.start.time
        window = feed.bars(last_seen, min(market_now, line.end.time), step_seconds)
        if not window.empty:
            for event in detector.scan(window):
                st.toast(
                    f"{'🟢 BUY' if event.direction == UP else '🔴 SELL'} {ticker} "
                    f"@ {event.price:,.5f} — line {event.line_price:,.5f}",
                    icon="📈" if event.direction == UP else "📉",
                )
            offsets = clock.elapsed_from(line.start.time,
                                         pd.DatetimeIndex(window["timestamp"]))
            ticks.extend(
                {"timestamp": t, "price": float(p), "rth_seconds": float(o)}
                for t, p, o in zip(window["timestamp"], window["price"], offsets)
            )
            st.session_state.ticks = ticks[-400_000:]

    frame = pd.DataFrame(ticks) if ticks else pd.DataFrame(
        columns=["timestamp", "price", "rth_seconds"])
    cursor = clock.elapsed(line.start.time, min(market_now, line.end.time))
    cursor = float(np.clip(cursor, 0, line.span_seconds))

    # --- status strip
    s1, s2, s3, s4 = st.columns(4)
    line_now = line.price_at_offset(cursor)
    last_price = float(frame["price"].iloc[-1]) if not frame.empty else float("nan")
    gap = last_price - line_now

    s1.metric("Market clock", market_now.strftime("%Y-%m-%d %H:%M:%S"),
              help="UTC. Advances only while the market is open.")
    s2.metric("Trendline price", f"{line_now:,.5f}")
    s3.metric(f"{ticker} price",
              "—" if np.isnan(last_price) else f"{last_price:,.5f}",
              delta=None if np.isnan(gap) else f"{gap:+,.5f} vs line")
    s4.metric("Progress", f"{cursor / line.span_seconds * 100:,.1f}%",
              help=f"{cursor:,.0f} of {line.span_seconds:,.0f} market seconds")

    if not st.session_state.running:
        st.info("Press **Start** to drive the line and watch for crossings.")
    elif at_end:
        st.success("Reached the end anchor. The trendline is finished — no further signals.")
    elif clock_mode == "Live" and not clock.is_open(market_now):
        st.warning(
            f"{market.label} is closed right now, so the market clock is frozen and "
            f"no ticks are being consumed. Next open: "
            f"**{clock.snap_forward(market_now):%Y-%m-%d %H:%M} UTC**."
        )

    st.plotly_chart(build_chart(frame, detector.events, cursor),
                    width="stretch", key="chart")

    # --- trade log
    st.subheader("Trade signals")
    log = events_to_frame(detector.events)
    if log.empty:
        st.caption("No crossing yet.")
    else:
        st.dataframe(log, width="stretch", hide_index=True)
        st.download_button("Download trade log (CSV)", log.to_csv(index=False),
                           file_name=f"{ticker}_signals.csv", mime="text/csv")


live_panel()


# --------------------------------------------------------------------------- #
# the data points themselves
# --------------------------------------------------------------------------- #

st.divider()
st.subheader(f"Trendline data points — every {step_seconds}s of open market")

try:
    points = line.series(step_seconds=step_seconds)
except ValueError as exc:
    st.error(f"{exc}")
else:
    st.caption(
        f"**{len(points):,}** points between the anchors. Consecutive rows are "
        f"{step_seconds}s apart *in market time* — the wall clock jumps wherever the "
        f"market was shut."
    )
    st.dataframe(points.head(2000), width="stretch", hide_index=True)
    if len(points) > 2000:
        st.caption(f"Showing the first 2,000 of {len(points):,}. Download for all.")
    st.download_button(
        "Download all data points (CSV)", points.to_csv(index=False),
        file_name=f"{ticker}_rth_points_{step_seconds}s.csv", mime="text/csv",
    )
