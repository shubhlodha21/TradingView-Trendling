#!/usr/bin/env python3
"""
GT Trading Dashboard - Production Terminal UI

4-panel layout:
┌─────────────────────────────────┬──────────┐
│ FEED (LTP, BBO, volume, rate)   │ SYSTEM   │
├─────────────────────────────────┼──────────┤
│ ORDERS (live order table)       │ P&L      │
└─────────────────────────────────┴──────────┘

Wire to engine + feed via run_live.py.
"""
import asyncio
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

# America/New_York via ZoneInfo so the ET clock auto-switches between
# EST (UTC-5) and EDT (UTC-4). The old hardcoded `timezone(timedelta(hours=-4))`
# silently drifted by an hour every winter — this fixes it once for both
# header renderers (legacy and v2).
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = timezone(timedelta(hours=-5))  # EST fallback; better to be a winter clock all year than a summer one
from typing import TYPE_CHECKING, Optional

sys.path.insert(0, 'src')
from src.config.models import OrderStatus, COMMISSION_PER_SHARE, calc_ibkr_commission

if TYPE_CHECKING:
    from src.feed.handler import Tick
    from src.strategy.engine import Engine
    from src.feed.production import ProductionFeed


# ═══════════════════════════════════════════════════════════════════════════
# ANSI — senior-quant palette
#
# Two coexisting palettes for backwards compatibility:
#   - Legacy short names (R, B, D, G, R_, Y, C, W) used by the original
#     panel renderers — kept untouched so we don't risk regressing what works.
#   - New `Q.*` 256-color palette used by the redesigned panels for refined
#     pastel tones (Bloomberg-terminal aesthetic). Selected from the xterm
#     256-color cube so they render consistently across modern terminals.
#
# Color semantics are strict, not decorative:
#   green  = profit / connected / long position / "ok"
#   red    = loss / disconnected / danger / rejection
#   yellow = warning / approaching limit
#   orange = HIGH-severity alert
#   cyan   = active state / live signal
#   gray   = dimmed / static / inactive
# ═══════════════════════════════════════════════════════════════════════════

R = '\033[0m'
B = '\033[1m'
D = '\033[2m'
G = '\033[32m'
R_ = '\033[31m'
Y = '\033[33m'
C = '\033[36m'
W = '\033[97m'


class Q:
    """Senior-quant 256-color ANSI palette. Used by the redesigned panels.

    Picked from the xterm 256 cube for visual coherence: cool blues/cyans
    for system state, warm reds/oranges for danger, soft greens for ok,
    a graduated gray scale for dimmed text. Avoids harsh primaries.
    """
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    UNDER = '\033[4m'
    # Grayscale (xterm 232=near-black, 255=near-white)
    GRAY_1 = '\033[38;5;236m'   # near-black (separators)
    GRAY_2 = '\033[38;5;240m'   # dim
    GRAY_3 = '\033[38;5;244m'   # neutral
    GRAY_4 = '\033[38;5;249m'   # light
    GRAY_5 = '\033[38;5;253m'   # near-white (body text)
    WHITE  = '\033[38;5;255m'   # bright
    # Status / accent
    CYAN   = '\033[38;5;45m'    # system, monitoring
    BLUE   = '\033[38;5;39m'    # ladder, lines
    TEAL   = '\033[38;5;37m'    # neutral active
    GREEN  = '\033[38;5;42m'    # profit / connected / long
    G_DIM  = '\033[38;5;29m'    # less-bright green
    RED    = '\033[38;5;203m'   # loss / danger
    R_DIM  = '\033[38;5;88m'    # less-bright red
    YELLOW = '\033[38;5;221m'   # warning / approaching
    ORANGE = '\033[38;5;215m'   # HIGH severity alert
    PURPLE = '\033[38;5;141m'   # CRITICAL alert (rare, eye-catching)
    PINK   = '\033[38;5;211m'   # special states (paused, etc.)
    # Background highlights (used sparingly, for current-row markers)
    BG_DIM = '\033[48;5;236m'
    BG_HI  = '\033[48;5;238m'


HOME = '\033[H'
CLEAR_SCREEN = '\033[J'

STRIP = re.compile(r'\x1b\[[0-9;]*m')


# ─────── Status icons (unicode, rendered with appropriate color) ───────
class Glyph:
    DOT       = '●'      # filled circle (status)
    CIRCLE    = '○'      # empty circle (status off)
    HALF      = '◐'      # half circle (partial/transition)
    CHECK     = '✓'
    CROSS     = '✗'
    UP        = '▲'      # rising
    DOWN      = '▼'      # falling
    RIGHT     = '▶'
    LEFT      = '◀'
    DIAMOND   = '◆'
    STAR      = '★'      # current/active marker
    BULL      = '↑'
    BEAR      = '↓'
    FLAT      = '→'
    BAR_V     = '│'
    BAR_H     = '─'
    LADDER    = '┼'
    WARN      = '⚠'


def vis(s):
    return len(STRIP.sub('', s))


def p(s, w):
    return s + ' ' * max(0, w - vis(s))


def truncate(s: str, w: int) -> str:
    """Truncate a string to visible width w, preserving ANSI codes.

    Walks the string char-by-char, accumulating visible chars up to w.
    ANSI sequences are passed through transparently (they're zero-width).
    Adds an ellipsis if truncation occurred.
    """
    if vis(s) <= w:
        return s
    out = []
    visible = 0
    i = 0
    while i < len(s):
        if s[i] == '\033':
            end = s.find('m', i) + 1
            out.append(s[i:end])
            i = end
            continue
        if visible >= w - 1:
            out.append('…')
            break
        out.append(s[i])
        visible += 1
        i += 1
    return ''.join(out)


# ═══════════════════════════════════════════════════════════════════════════
# LAYOUT
# ═══════════════════════════════════════════════════════════════════════════

TW = 88
LW = 54
RW = 32


# ═══════════════════════════════════════════════════════════════════════════
# FEED STATS (updated from ProductionFeed callback)
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# RENDERING HELPERS
# ═══════════════════════════════════════════════════════════════════════════

# Sparkline blocks (8 levels). Index = round(normalized_value × 7).
_SPARK_BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline(values, width: int = 30) -> str:
    """Render a sequence of floats as a Unicode sparkline.

    Normalizes to the min/max of the supplied window. Empty / single-value
    inputs render as spaces (no signal to show). Width caps the output;
    if `values` is shorter, the line left-pads with spaces.
    """
    if not values or len(values) < 2:
        return " " * width
    vals = list(values)[-width:]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 0:
        return _SPARK_BLOCKS[4] * len(vals)
    out = []
    for v in vals:
        idx = int(round((v - lo) / span * 7))
        out.append(_SPARK_BLOCKS[idx])
    return ('' if len(vals) >= width else ' ' * (width - len(vals))) + ''.join(out)


def progress_bar(used: float, mx: float, width: int = 10) -> str:
    """Compact ASCII progress bar: [██░░░░░░░░] for used/mx ratio."""
    if mx <= 0:
        return '[' + '─' * width + ']'
    ratio = min(max(used / mx, 0.0), 1.0)
    filled = int(round(ratio * width))
    return '[' + '█' * filled + '░' * (width - filled) + ']'


# ─────── Braille high-resolution chart ───────
#
# A single Braille char is a 2x4 dot pattern:
#   1 4
#   2 5
#   3 6
#   7 8
# Encoded as bits 0-7 of (codepoint - 0x2800). We use this to render
# mini line charts with up to 2× horizontal and 4× vertical resolution
# vs block sparklines. 30-char Braille chart = 60 columns × 4 rows of
# resolution = enough to see real trend curvature.

_BRAILLE_BASE = 0x2800
# Bit positions for each (col, row) pair in the 2x4 cell.
# Column 0 = bits 0,1,2,6; Column 1 = bits 3,4,5,7.
_BRAILLE_BITS = [
    [0x01, 0x02, 0x04, 0x40],  # left column, rows 0-3
    [0x08, 0x10, 0x20, 0x80],  # right column, rows 0-3
]


def braille_line(values, width: int, height: int = 4) -> list[str]:
    """Render a series of floats as a Braille-pattern line chart.

    Args:
        values: Sequence of numeric samples. Empty → all blanks.
        width: Number of Braille chars wide. Each char = 2 sample columns,
               so effective resolution is `2 * width` sample columns.
        height: Number of Braille chars tall. Each char = 4 sub-rows. The
                default 4 gives 16 vertical resolution levels — usually plenty.

    Returns:
        List of `height` strings, each `width` Braille chars wide.
        Most-recent values right-align in the chart.
    """
    if height < 1:
        height = 1
    if not values or len(values) < 2:
        return [' ' * width for _ in range(height)]

    vals = list(values)
    cols = width * 2  # Braille chars are 2 sample columns each
    if len(vals) > cols:
        vals = vals[-cols:]

    lo, hi = min(vals), max(vals)
    span = hi - lo
    rows = height * 4  # 4 sub-rows per char vertically

    # Map each sample to its row (0 = top, rows-1 = bottom).
    def _row(v: float) -> int:
        if span <= 0:
            return rows // 2
        # Flip so high values appear at the top.
        norm = (v - lo) / span
        return int(round((1.0 - norm) * (rows - 1)))

    # Build a row × col grid of bools (lit dots).
    grid = [[False] * cols for _ in range(rows)]
    prev_r = _row(vals[0])
    for col_idx, v in enumerate(vals):
        r = _row(v)
        # Light the dot, plus interpolate vertically for continuity
        rmin, rmax = sorted((prev_r, r))
        for rr in range(rmin, rmax + 1):
            grid[rr][col_idx] = True
        prev_r = r

    # If we have fewer samples than columns, left-pad with blanks.
    left_pad = cols - len(vals)

    # Walk the grid in (char_row × char_col) groups of 4×2 dots and assemble
    # the Braille code points.
    out_lines = []
    for cr in range(height):
        line_chars = []
        for cc in range(width):
            mask = 0
            for sub_col in (0, 1):
                col_idx = cc * 2 + sub_col - left_pad
                if 0 <= col_idx < len(vals):
                    for sub_row in range(4):
                        r = cr * 4 + sub_row
                        if grid[r][col_idx]:
                            mask |= _BRAILLE_BITS[sub_col][sub_row]
            line_chars.append(chr(_BRAILLE_BASE + mask))
        out_lines.append(''.join(line_chars))
    return out_lines


# ─────── Vertical histogram bars (e.g. latency buckets) ───────

_VBAR_BLOCKS = ' ▁▂▃▄▅▆▇█'


def vbar(value: float, max_value: float, height: int = 8) -> str:
    """Single-char tall vertical bar approximating `value / max_value`.

    Returns a string of `height` chars, where each row is either ' ',
    or a partial block char. Suitable for stacking side-by-side as a
    poor-man's bar histogram.
    """
    if max_value <= 0:
        return ' ' * height
    ratio = max(0.0, min(1.0, value / max_value))
    total_units = ratio * height * 8  # 8 sub-levels per row
    out = []
    units_left = total_units
    for _ in range(height):
        if units_left >= 8:
            out.append(_VBAR_BLOCKS[8])
            units_left -= 8
        elif units_left > 0:
            out.append(_VBAR_BLOCKS[int(round(units_left))])
            units_left = 0
        else:
            out.append(' ')
    # Bottom row first when reading top-to-bottom — but we want top of bar
    # higher in the terminal output. Reverse to put filled blocks at the bottom.
    return ''.join(reversed(out))


def horizontal_histogram(buckets: list[float], width: int) -> str:
    """Inline horizontal histogram using block characters.

    Renders `buckets` as bars in a single line. Each bucket maps to one
    character, normalized to the max bucket. Used for latency distribution.
    """
    if not buckets:
        return ' ' * width
    take = buckets[-width:] if len(buckets) > width else buckets
    mx = max(take) if take else 0
    if mx <= 0:
        return ' ' * len(take)
    chars = []
    for v in take:
        idx = int(round(min(1.0, v / mx) * 8))
        chars.append(_VBAR_BLOCKS[idx])
    return ''.join(chars)


# ─────── Price ladder (vertical levels with current marker) ───────

def price_ladder(levels: list[tuple[float, str, str]], current: float, width: int = 28) -> list[str]:
    """Render a vertical price ladder with the current price marked.

    Args:
        levels: List of (price, label, color) tuples. Sorted high-to-low
                automatically. e.g. [(232.10, "HIGH", G), (227.90, "TRIG", C), ...]
        current: Current LTP. Inserted into the ladder at its sorted position,
                 with a star marker.
        width: Total visible width per ladder line.

    Returns:
        List of strings, one per ladder rung (high→low).
    """
    # Build the full set of rows: levels + current price (if not already in).
    rows: list[tuple[float, str, str, bool]] = []
    for px, label, color in levels:
        rows.append((px, label, color, False))
    if current > 0:
        # Insert current price marker
        rows.append((current, "LTP", Q.WHITE, True))
    # Sort high-to-low
    rows.sort(key=lambda r: r[0], reverse=True)

    out = []
    for px, label, color, is_current in rows:
        marker = Q.YELLOW + Glyph.STAR + Q.RESET if is_current else Q.GRAY_2 + Glyph.LEFT + Q.RESET
        px_str = f"${px:>8.2f}"
        if is_current:
            line = f"  {Q.BOLD}{Q.WHITE}{px_str}{Q.RESET}  {marker}  {Q.BOLD}{color}{label}{Q.RESET}"
        else:
            line = f"  {color}{px_str}{Q.RESET}  {marker}  {color}{label}{Q.RESET}"
        out.append(line)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# EVENT STREAM
# ═══════════════════════════════════════════════════════════════════════════

class EventStream:
    """Bounded ring-buffer of engine log messages for dashboard rendering.

    Wired to `engine.set_log_callback()` so every `engine._log()` call lands
    here instead of stdout (where it would scroll past the dashboard).
    Dashboard renders the most recent ~8 entries in a footer panel.
    """
    __slots__ = ('_buf',)

    def __init__(self, maxlen: int = 32):
        self._buf: deque = deque(maxlen=maxlen)

    def push(self, msg: str) -> None:
        self._buf.append((datetime.now(), msg))

    def tail(self, n: int = 8) -> list:
        return list(self._buf)[-n:]


# ─────── Equity sampler (session P&L over time) ───────

class EquitySeries:
    """Timestamped session P&L samples for the equity curve panel.

    Sampled once per `sync_engine()` call (every ~300ms). 720 samples
    covers a 6-minute window at 300ms cadence; for the full session view
    we down-sample to per-minute or per-5-minute buckets when rendering.

    Stored as a flat deque of floats — index order = time order — so the
    Braille chart can read it as-is. Memory: 720 * 8 bytes ≈ 6 KB.
    """
    __slots__ = ('_pnl', '_max_session', '_min_session', '_session_started')

    def __init__(self, maxlen: int = 720):
        self._pnl: deque = deque(maxlen=maxlen)
        self._max_session: float = 0.0  # session peak (for high-water-mark display)
        self._min_session: float = 0.0  # session trough
        self._session_started = datetime.now()

    def sample(self, total_pnl: float) -> None:
        """Record a P&L sample. Called from State.sync_engine."""
        self._pnl.append(total_pnl)
        if total_pnl > self._max_session:
            self._max_session = total_pnl
        if total_pnl < self._min_session:
            self._min_session = total_pnl

    @property
    def values(self) -> deque:
        return self._pnl

    @property
    def peak(self) -> float:
        return self._max_session

    @property
    def trough(self) -> float:
        return self._min_session

    @property
    def latest(self) -> float:
        return self._pnl[-1] if self._pnl else 0.0


# ─────── Latency sampler (rolling order placement → fill timing) ───────

class OrderLatencyTracker:
    """Tracks per-order placement → fill latency in ms.

    Populated by State on each new fill. Engine emits `submitted_at` on
    submit and `filled_at` on fill; delta = order placement latency.
    Used by the latency panel to surface IBKR responsiveness alongside
    the (already-tracked) pipeline latency from ProductionFeed.
    """
    __slots__ = ('_samples', '_max_seen')

    def __init__(self, maxlen: int = 60):
        self._samples: deque = deque(maxlen=maxlen)
        self._max_seen: float = 0.0

    def add(self, latency_ms: float) -> None:
        if latency_ms < 0:
            return
        self._samples.append(latency_ms)
        if latency_ms > self._max_seen:
            self._max_seen = latency_ms

    @property
    def samples(self) -> list:
        return list(self._samples)

    @property
    def count(self) -> int:
        return len(self._samples)

    @property
    def median(self) -> float:
        if not self._samples:
            return 0.0
        sorted_s = sorted(self._samples)
        n = len(sorted_s)
        return sorted_s[n // 2]

    @property
    def p99(self) -> float:
        if not self._samples:
            return 0.0
        sorted_s = sorted(self._samples)
        return sorted_s[max(0, int(len(sorted_s) * 0.99) - 1)]

    @property
    def worst(self) -> float:
        return max(self._samples) if self._samples else 0.0


# ─────── Slippage tracker (per-fill slippage with buy/sell split) ───────

class SlippageTracker:
    """Per-fill slippage with side-split rolling stats.

    Slippage convention from the engine:
        BUY  slip = fill - signal_price (positive = paid more than intended)
        SELL slip = signal_price - fill (positive = got less than intended)

    A net-positive slip in either direction is "bad for us". We track
    separate buy and sell histories so the panel can show which side is
    worse and trend them over time.
    """
    __slots__ = ('_buy_slips', '_sell_slips', '_worst', '_worst_at')

    def __init__(self, maxlen: int = 32):
        self._buy_slips: deque = deque(maxlen=maxlen)
        self._sell_slips: deque = deque(maxlen=maxlen)
        self._worst: float = 0.0
        self._worst_at: str = ""  # e.g. "13:42 sell"

    def add_buy(self, slip: float, when: str = "") -> None:
        self._buy_slips.append(slip)
        if abs(slip) > abs(self._worst):
            self._worst = slip
            self._worst_at = f"{when} buy" if when else "buy"

    def add_sell(self, slip: float, when: str = "") -> None:
        self._sell_slips.append(slip)
        if abs(slip) > abs(self._worst):
            self._worst = slip
            self._worst_at = f"{when} sell" if when else "sell"

    @property
    def avg_buy(self) -> float:
        return sum(self._buy_slips) / len(self._buy_slips) if self._buy_slips else 0.0

    @property
    def avg_sell(self) -> float:
        return sum(self._sell_slips) / len(self._sell_slips) if self._sell_slips else 0.0

    @property
    def worst(self) -> float:
        return self._worst

    @property
    def worst_at(self) -> str:
        return self._worst_at

    @property
    def total_cost(self) -> float:
        # Sum of all signed slip values (positive = cost to us)
        return sum(self._buy_slips) + sum(self._sell_slips)

    @property
    def buy_history(self) -> list:
        # Map to abs values for histogram visualization (we care about magnitude)
        return [abs(s) for s in self._buy_slips]

    @property
    def sell_history(self) -> list:
        return [abs(s) for s in self._sell_slips]


# ─────── Microstructure sampler (L1 stream observations) ───────
#
# Accumulates the IBKR L1 feed (BBO + tick-by-tick AllLast) into a few
# senior-quant-meaningful aggregates. Everything is O(1) per tick — we
# never iterate the tape on the hot path. The dashboard panel reads
# pre-computed values at render time (5Hz), so adding this class to the
# `on_tick` callback costs at most ~1µs per tick on commodity hardware.
#
# What we capture:
#   - Session VWAP (volume-weighted average price)
#   - Per-second rates: ticks, BBO updates, trades (three separate windows)
#   - Buy/sell aggression: count of trades hitting ask vs bid
#   - Last N trades (the tape) — with classification: ▲ above ask, ─ within, ▼ below bid
#   - Spread in bps + imbalance ratio (bid_size / ask_size)
#   - Range position: where LTP sits in (low, high)
#
# Why these specific stats:
#   - VWAP gives a reference point. Slippage vs VWAP > slippage vs trigger
#     is what tells you whether you're getting fills better/worse than
#     the average price during the period of interest.
#   - Buy/sell aggression is the cleanest leading indicator of directional
#     pressure from the L1 stream alone (no L2 needed).
#   - The classified tape (above-ask / within / below-bid) compresses a
#     lot of order-flow info into one column.
#   - Spread/imbalance directly read liquidity quality at the top of book.


@dataclass(slots=True)
class TapeEntry:
    """One classified trade for the live tape display."""
    ts: str           # HH:MM:SS.mmm
    price: float
    size: float
    exchange: str
    conditions: str   # raw IBKR conditions string ("F", "I", etc.)
    direction: int    # +1 above-ask (buy aggression), -1 below-bid, 0 within


class MicrostructureStats:
    """O(1)-per-tick L1 market microstructure aggregator.

    Memory footprint: bounded — three rolling timestamp deques (max 200
    each ≈ 5 KB total) + a tape deque (max 12 entries) + 5 scalar
    accumulators. No dynamic allocation per tick beyond deque append.

    Thread-safety: not thread-safe. Called only from the asyncio event
    loop's feed callback (same thread as engine.on_tick).
    """
    __slots__ = (
        '_vwap_num', '_vwap_den',                    # running numerator + denom for VWAP
        '_tick_times', '_bbo_times', '_trade_times', # rolling per-second-rate windows
        '_buy_aggr', '_sell_aggr', '_neutral',       # tape direction counters
        '_tape',                                      # last N classified trades
        '_last_mid',                                  # cached mid for direction classification
    )

    def __init__(self):
        self._vwap_num: float = 0.0
        self._vwap_den: float = 0.0
        self._tick_times: deque = deque(maxlen=200)
        self._bbo_times: deque = deque(maxlen=200)
        self._trade_times: deque = deque(maxlen=200)
        self._buy_aggr: int = 0
        self._sell_aggr: int = 0
        self._neutral: int = 0
        self._tape: deque = deque(maxlen=12)
        self._last_mid: float = 0.0

    def on_tick(self, tick) -> None:
        """Accumulate from a single Tick. Branchless-ish; fewer than 20 ops."""
        from src.feed.handler import MessageType  # local import: avoids module-load cost during import-time
        now = time.monotonic()
        self._tick_times.append(now)

        if tick.tick_type == MessageType.TICK:
            self._bbo_times.append(now)
            # Update cached mid for next trade classification
            if tick.bid > 0 and tick.ask > 0:
                self._last_mid = (tick.bid + tick.ask) * 0.5
            return

        # TRADE tick — update VWAP, classify direction, push to tape
        if tick.tick_type == MessageType.TRADE and tick.last > 0:
            self._trade_times.append(now)
            sz = tick.last_size or 0
            if sz > 0:
                self._vwap_num += tick.last * sz
                self._vwap_den += sz

            # Direction classification.
            # ▲ above-ask: trade price >= ask    (lifted the offer; aggressive buy)
            # ▼ below-bid: trade price <= bid    (hit the bid; aggressive sell)
            # ─ within:    in between            (passive / midpoint)
            direction = 0
            if tick.ask > 0 and tick.last >= tick.ask:
                direction = +1
                self._buy_aggr += 1
            elif tick.bid > 0 and tick.last <= tick.bid:
                direction = -1
                self._sell_aggr += 1
            else:
                self._neutral += 1

            # Tape entry — kept small to avoid memory churn
            ts_str = (tick.timestamp.strftime("%H:%M:%S.%f")[:-3]
                      if hasattr(tick.timestamp, 'strftime') else str(tick.timestamp)[:12])
            self._tape.append(TapeEntry(
                ts=ts_str,
                price=tick.last,
                size=sz,
                exchange=tick.last_exchange or "",
                conditions=tick.last_conditions or "",
                direction=direction,
            ))

    # ─── Cheap derived properties (called once per render, 5Hz) ───

    @property
    def vwap(self) -> float:
        return self._vwap_num / self._vwap_den if self._vwap_den > 0 else 0.0

    def _rate(self, dq: deque) -> float:
        """Trailing rate per second over the deque window."""
        if len(dq) < 2:
            return 0.0
        window = dq[-1] - dq[0]
        return len(dq) / window if window > 0 else 0.0

    @property
    def tick_rate(self) -> float:
        return self._rate(self._tick_times)

    @property
    def bbo_rate(self) -> float:
        return self._rate(self._bbo_times)

    @property
    def trade_rate(self) -> float:
        return self._rate(self._trade_times)

    @property
    def buy_pct(self) -> float:
        total = self._buy_aggr + self._sell_aggr + self._neutral
        return (self._buy_aggr / total * 100.0) if total else 0.0

    @property
    def sell_pct(self) -> float:
        total = self._buy_aggr + self._sell_aggr + self._neutral
        return (self._sell_aggr / total * 100.0) if total else 0.0

    @property
    def tape(self) -> list:
        return list(self._tape)


# ─────── Alert subscriber (live view of last N alerts) ───────

class AlertView:
    """Reads from AlertManager._history and exposes the most recent N.

    AlertManager keeps an internal list; we don't mutate it. We just
    `tail()` for display. Severity is shown via a coloured dot icon and
    the message is truncated to fit the panel width.
    """
    __slots__ = ('_alert_mgr',)

    def __init__(self, alert_mgr=None):
        self._alert_mgr = alert_mgr

    def attach(self, alert_mgr) -> None:
        self._alert_mgr = alert_mgr

    def tail(self, n: int = 5) -> list:
        if self._alert_mgr is None:
            return []
        try:
            history = self._alert_mgr.get_history(count=n)
        except Exception:
            return []
        return list(history)


@dataclass(slots=True)
class FeedStats:
    """Lightweight feed statistics - updated from tick callback."""
    total: int = 0
    trade_count: int = 0
    bbo_count: int = 0
    rate: float = 0.0
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_size: int = 0
    ask_size: int = 0
    volume: int = 0
    open_px: float = 0.0
    high: float = 0.0
    low: float = 0.0
    last_exchange: str = ""
    last_size: float = 0.0
    last_conditions: str = ""
    _times: deque = field(default_factory=lambda: deque(maxlen=100))
    _trade_hist: deque = field(default_factory=lambda: deque(maxlen=8))
    # LTP history for the inline sparkline. 60 samples; renders to a single
    # row of Unicode block chars. Captures only TRADE ticks (LTP changes),
    # so each char represents one trade, not one BBO update.
    _ltp_history: deque = field(default_factory=lambda: deque(maxlen=60))

    def update(self, tick: 'Tick'):
        """Update from Tick object. Called from feed callback at 10Hz."""
        from src.feed.handler import MessageType

        self.total += 1
        now = datetime.now()
        self._times.append(time.monotonic())

        if tick.tick_type == MessageType.TRADE:
            self.trade_count += 1
            self.last = tick.last
            self.last_exchange = tick.last_exchange
            self.last_size = tick.last_size
            self.last_conditions = tick.last_conditions
            self._trade_hist.append({
                'time': tick.timestamp.strftime("%H:%M:%S.%f")[:-3],
                'price': tick.last,
                'size': tick.last_size,
                'exchange': tick.last_exchange,
            })
            self._ltp_history.append(tick.last)
        elif tick.tick_type == MessageType.TICK:
            self.bbo_count += 1
            if tick.bid > 0:
                self.bid = tick.bid
            if tick.ask > 0:
                self.ask = tick.ask
            if tick.bid_size > 0:
                self.bid_size = tick.bid_size
            if tick.ask_size > 0:
                self.ask_size = tick.ask_size
            # IBKR's BBO ticker also carries `last` (the most recent trade
            # price). Tick-by-tick (MessageType.TRADE) gives finer-grained
            # per-trade info (size, exchange, conditions), but tick-by-tick
            # is rate-limited at IBKR (error 10190: max 5 concurrent for
            # standard accounts). Past the 5th symbol, tick-by-tick is
            # rejected and ONLY BBO updates flow — without this branch the
            # LTP display stayed blank because we only set `last` from TRADE
            # ticks. Update from BBO too, so LTP works even when tick-by-tick
            # is unavailable.
            if tick.last > 0:
                self.last = tick.last
                self._ltp_history.append(tick.last)
            # MID FALLBACK for quote-driven assets (CFD INDEX, FX, anything
            # without a public trade stream). Without this, `feed.last`
            # never rises above 0 for INDEX_CFD — IBKR sends BBO updates
            # but no trade prints, so `tick.last` stays 0. Result before
            # fix: dashboard header shows "$--.--", unrealized PnL stays
            # $0 even with an open position whose mark-to-market is
            # clearly moving. (Live 2026-06-05 on IBUS500.)
            #
            # Mid is the standard mark-to-market reference for quote-
            # driven assets — same convention as TWS's "last/mid" column.
            elif self.bid > 0 and self.ask > 0:
                mid = (self.bid + self.ask) * 0.5
                self.last = mid
                self._ltp_history.append(mid)
        elif tick.tick_type == MessageType.QUOTE:
            # FX BidAsk tick-by-tick path (from `_dispatch_trade_ticks`'s
            # BidAsk branch). Has bid/ask but no trade. Same mid-fallback
            # logic as BBO above so feed.last reflects the moving market.
            self.bbo_count += 1
            if tick.bid > 0:
                self.bid = tick.bid
            if tick.ask > 0:
                self.ask = tick.ask
            if tick.bid_size > 0:
                self.bid_size = tick.bid_size
            if tick.ask_size > 0:
                self.ask_size = tick.ask_size
            if self.bid > 0 and self.ask > 0:
                mid = (self.bid + self.ask) * 0.5
                self.last = mid
                self._ltp_history.append(mid)

        if tick.volume > 0:
            self.volume = tick.volume
        if tick.open > 0 and self.open_px == 0:
            self.open_px = tick.open
        if tick.high > 0:
            self.high = tick.high
        if tick.low > 0:
            self.low = tick.low

        # Rate
        if len(self._times) >= 2:
            window = self._times[-1] - self._times[0]
            if window > 0:
                self.rate = len(self._times) / window


# ═══════════════════════════════════════════════════════════════════════════
# DASHBOARD STATE
# ═══════════════════════════════════════════════════════════════════════════

class State:
    """Dashboard state - syncs with Engine + FeedStats."""
    __slots__ = (
        'feed', 'engine', 'orders', '_order_keys',
        # realized_pnl = engine._pnl − (entry commission of the open
        # cycle, when in position). Pre-computed in sync_engine so every
        # downstream renderer (legacy + v2 panels) reads the same number.
        'daily_pnl', 'total_pnl', 'unrealized_pnl', 'realized_pnl',
        'wins', 'losses',
        'comm', 'trades', 'consec_loss',
        'connected', 'symbol', 'client_id',
        '_ts', '_last_engine_sync', '_latency',
        # Risk + backpressure state mirrored from engine.get_status()
        '_risk_status', '_risk_limits', 'ticks_dropped', 'tick_queue_depth',
        # Slippage stats + persistent event log
        'avg_slippage', 'worst_slippage', 'fill_count',
        'events',
        # Senior-quant samplers (added in v2 redesign)
        'equity_series',        # session P&L over time (deque of floats)
        'order_latency',         # OrderLatencyTracker — placement → fill ms
        'slippage_tracker',      # SlippageTracker — per-fill buy/sell slip
        'alerts',                # AlertView — tail of AlertManager._history
        'micro',                 # MicrostructureStats — L1 market microstructure
        '_seen_fill_ids',        # set: dedup fills so we only sample slip/latency once
        # Engine status snapshot cached for cross-panel consistency
        '_last_status',
        # Position / breakout convenience fields for the ladder panel
        'breakout_level', 'highest_price',
        # Connection telemetry surfaced by the supervisor + gateway
        'heartbeat_age_s', 'reconnect_count',
        # Session state for the SESSION panel
        'paused', 'in_session', 'next_session_change',
    )

    def __init__(self, engine: 'Engine' = None, feed: 'ProductionFeed' = None):
        from collections import deque
        self.feed = FeedStats()
        self.engine = engine
        self.orders = deque(maxlen=50)  # Keep more order history
        self._order_keys = set()  # Track seen order keys to avoid duplicates
        self.daily_pnl = 0.0
        self.total_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.comm = 0.0
        self.trades = 0
        self.consec_loss = 0
        self.connected = False
        self.symbol = "UNKNOWN"
        # IBKR client_id this dashboard belongs to. Surfaced in the header
        # (`SYM cN`) and in the aggregator's table so the operator can
        # tell at a glance which process is which when running multiple
        # tickers (and especially when two processes share a symbol on
        # different client_ids).
        self.client_id = 0
        self._ts = datetime.now
        self._last_engine_sync = 0.0
        self._latency = {'avg_ms': 0.0, 'max_ms': 0.0}
        self._risk_status: dict = {}
        self._risk_limits: dict = {}
        self.ticks_dropped: int = 0
        self.tick_queue_depth: int = 0
        self.avg_slippage: float = 0.0
        self.worst_slippage: float = 0.0
        self.fill_count: int = 0
        # Live event log (engine messages, risk blocks, alerts). Wired by
        # run_live.py via engine.set_log_callback(self.events.push).
        self.events: EventStream = EventStream(maxlen=32)

        # ─── Senior-quant samplers (v2 redesign) ───
        # All populated incrementally — engine-side samplers update during
        # `sync_engine` at 300ms cadence; market-side samplers update on
        # every tick (O(1) work per tick). Engine reference is optional;
        # samplers stay empty if not wired (e.g. unit tests).
        self.equity_series: EquitySeries = EquitySeries(maxlen=720)
        self.order_latency: OrderLatencyTracker = OrderLatencyTracker(maxlen=60)
        self.slippage_tracker: SlippageTracker = SlippageTracker(maxlen=32)
        self.alerts: AlertView = AlertView()
        # MicrostructureStats reads the L1 stream (BBO + tick-by-tick AllLast)
        # and accumulates VWAP, per-second rates, buy/sell aggression, and a
        # bounded classified tape. Same on-tick callback as FeedStats; cost
        # is ~1 μs/tick — negligible vs the engine's tick processing budget.
        self.micro: MicrostructureStats = MicrostructureStats()
        # Track fills we've already processed so we don't double-count on
        # repeat sync_engine() polls (registry is queried each tick).
        self._seen_fill_ids: set = set()
        # Cached engine snapshot — share between sync and renderers
        self._last_status: dict = {}
        # Position context fields populated from engine
        self.breakout_level: float = 0.0
        self.highest_price: float = 0.0
        # Connection telemetry (populated by run_live's reconnect path)
        self.heartbeat_age_s: float = 0.0
        self.reconnect_count: int = 0
        # Session state
        self.paused: bool = False
        self.in_session: bool = True
        self.next_session_change: float = 0.0  # seconds until next open/close

    def on_tick(self, tick: 'Tick'):
        """Called from feed callback on every tick — fast O(1) accumulators.

        Two updaters:
            - `feed.update(tick)`  — original FeedStats path (BBO state, recent
              trades list, simple LTP history sparkline source)
            - `micro.on_tick(tick)` — new microstructure aggregator (VWAP,
              per-second rates, buy/sell aggression, classified tape)

        Both are sub-microsecond. The combined hot-path cost of these two
        calls is ~1-2 μs per tick — well below the budget for tick-by-tick
        data at 5-10K msg/s during volatile market periods.
        """
        self.feed.update(tick)
        self.micro.on_tick(tick)

    def sync_engine(self):
        """Sync engine status every 300ms."""
        if not self.engine:
            return

        now = time.monotonic()
        if now - self._last_engine_sync < 0.3:
            return
        self._last_engine_sync = now

        s = self.engine.get_status()
        self.connected = getattr(self.engine.gateway, 'connected', False)

        # Get realized P&L baseline from engine (this is the round-trip
        # P&L from closed cycles only; mid-cycle entry commission has NOT
        # been folded in yet — we do that below).
        self.realized_pnl = s.get('pnl', 0)
        self.total_pnl = self.realized_pnl  # will += unrealized + commission below
        self.wins = s.get('wins', 0)
        self.losses = s.get('losses', 0)
        self.comm = s.get('total_commission', 0)

        # Risk + backpressure
        self._risk_status = s.get('risk', {}) or {}
        self._risk_limits = s.get('risk_limits', {}) or {}
        self.ticks_dropped = s.get('ticks_dropped', 0)
        self.tick_queue_depth = s.get('tick_queue_depth', 0)

        # Slippage attribution
        self.avg_slippage = s.get('avg_slippage', 0.0)
        self.worst_slippage = s.get('worst_slippage', 0.0)
        self.fill_count = s.get('fill_count', 0)

        # Count filled orders from registry (not from trades_today which only increments on exit)
        self.trades = 0
        if hasattr(self.engine, 'registry'):
            for order in self.engine.registry._orders.values():
                if order.status.value == 'FILLED':
                    self.trades += 1

        # Calculate unrealized + realized P&L when in position.
        #
        # Accounting convention (matches what real trading platforms show):
        #   unreal  = (entry - current) × qty       — pure mark-to-market (SHORT)
        #   real    = engine._pnl − entry_commission  (mid-cycle)
        #           = engine._pnl                     (when flat — exit
        #             commission already rolled into _pnl on the SELL fill)
        #
        # Earlier code put the entry-commission drag inside unrealized
        # (so unreal started negative even at zero price movement). That
        # made unreal hard to compare against the bid-ask, and meant
        # manual square-off required mentally undoing the embedded drag.
        # The user-preferred split: keep unreal as pure price-movement
        # P&L, push the entry commission into realized at fill time
        # (since it's a sunk cost the broker has already debited).
        self.unrealized_pnl = 0.0
        if s.get('position_open') and s.get('entry_price') and self.feed.last > 0:
            entry = s.get('entry_price')
            current = self.feed.last
            qty = s.get('quantity', 1)
            # Pure mark-to-market — no commission deduction here.
            # SHORT INVERSION (P11): short profits as price falls → (entry − current).
            self.unrealized_pnl = (entry - current) * qty
            # Deduct the entry-side commission from realized — the broker
            # already debited it, so it's a confirmed cash outflow even
            # though the cycle hasn't closed yet. On exit, engine._pnl will
            # have accumulated both commissions and this mid-cycle
            # adjustment goes away naturally (position_open flips to False).
            #
            # SOURCE: engine._pending_buy_commission — the TRUE broker-
            # charged commission, refreshed from IBKR's commissionReport
            # event. Was previously `calc_ibkr_commission(qty, entry, "BUY")`
            # — the equity-formula fallback, which on EURUSD 25k @ 1.16
            # returned $92.58 instead of the real $2.00 IBKR FX min.
            # That phantom $90 leaked into every FX open-position display
            # and made even profitable cycles look like losses. Live
            # 2026-06-05: dashboard real=−$95.33 vs engine truth=−$2.75.
            entry_commission = s.get('pending_buy_commission')
            if entry_commission is None or entry_commission <= 0:
                # Pre-fill window: commission report hasn't landed yet.
                # Fall back to the modeled formula so the display isn't
                # blank during the first ~1s after fillEvent. The same
                # equity-formula caveats apply (overstates FX/futures by
                # ~50x); the report-event refresh corrects it within ~1s.
                entry_commission = calc_ibkr_commission(qty, entry or 0.0, "BUY")
            self.realized_pnl -= entry_commission
            # Total = realized + unrealized
            self.total_pnl = self.realized_pnl + self.unrealized_pnl
        else:
            # Flat: total reflects engine._pnl as-is (already net of all
            # round-trip commissions from closed cycles).
            self.total_pnl = self.realized_pnl

        # Populate orders from engine registry (keep history, don't clear)
        if hasattr(self.engine, 'registry'):
            trigger_px = self.engine.config.trigger_price

            # Use order history (has both SUBMITTED and FILLED entries)
            if hasattr(self.engine, '_order_history'):
                for order in self.engine._order_history:
                    # Create unique key including timestamp so each order cycle is tracked
                    status_str = order.status.value if hasattr(order.status, 'value') else str(order.status)
                    if order.status == OrderStatus.FILLED:
                        ts = order.filled_at
                    else:
                        ts = order.submitted_at
                    ts_str = ts.strftime('%H:%M:%S') if ts else '--:--:--'

                    # Dedup key — MUST include enough precision to distinguish
                    # PARTIAL FILLS of the same parent order. When IBKR splits
                    # a 100-share BUY into 40 + 60 executions, the engine
                    # appends TWO OrderRecord rows to _order_history, both with:
                    #   • same order_id (the parent)
                    #   • same side (BUY)
                    #   • same status (FILLED)
                    #   • timestamps within milliseconds of each other
                    # The old key (HH:MM:SS precision only) collided on the
                    # second one → the second partial was silently dropped by
                    # the _order_keys dedup set, so the ORDERS panel showed
                    # only the first partial. Including microsecond timestamp
                    # + the partial's own filled_qty + avg_fill_price makes
                    # every partial render as its own row.
                    ts_key = ts.strftime('%H:%M:%S.%f') if ts else '--:--:--.000000'
                    qty_part = order.filled_qty if order.status == OrderStatus.FILLED else order.qty
                    px_part = (order.avg_fill_price if order.status == OrderStatus.FILLED
                               else order.signal_price or order.limit_price or 0)
                    key = (
                        f"{order.order_id}_{order.side.value}_{status_str}_"
                        f"{ts_key}_{qty_part}_{px_part}"
                    )

                    if key not in self._order_keys:
                        # Price column priority:
                        #   FILLED      → avg_fill_price (what we actually got)
                        #   SUBMITTED   → stop_price (the TRIGGER — the level the
                        #                 strategy targeted). For STOP-LIMIT this
                        #                 is the price that ARMS the order, which
                        #                 is what the operator cares about when
                        #                 monitoring "did LTP reach my trigger?".
                        #                 Falls back to limit_price for plain
                        #                 LIMIT orders that have no stop, then to
                        #                 signal_price, then to the config trigger.
                        #   fallback    → signal_price → trigger_px (legacy paths)
                        if order.status == OrderStatus.FILLED:
                            px = order.avg_fill_price
                        elif order.stop_price:
                            px = order.stop_price
                        elif order.limit_price:
                            px = order.limit_price
                        elif order.signal_price:
                            px = order.signal_price
                        else:
                            px = trigger_px
                        # Coerce qty to int — even if upstream hydration
                        # left a "10.0" float in OrderRecord (old CSV rows
                        # before the audit-writer fix), the panel displays
                        # whole shares.
                        try:
                            qty_display = int(order.qty)
                        except (TypeError, ValueError):
                            qty_display = order.qty
                        self.orders.append({
                            'id': key,
                            'side': order.side.value,
                            'qty': qty_display,
                            'px': px,
                            'status': status_str,
                            'ts': ts_str,
                        })
                        self._order_keys.add(key)

        # Update latency from production_feed (stored on engine)
        if self.engine and hasattr(self.engine, '_feed'):
            self._latency = self.engine._feed.get_latency_stats()
        elif hasattr(self.engine, 'production_feed'):
            self._latency = self.engine.production_feed.get_latency_stats()

        # ─── v2 samplers: equity, slippage, order latency, session ───
        self._last_status = s

        # 1. Sample session equity curve once per sync.
        self.equity_series.sample(self.total_pnl)

        # 2. Walk newly-filled orders, extract per-fill slippage + placement latency.
        if hasattr(self.engine, '_order_history'):
            for order in self.engine._order_history:
                if order.status != OrderStatus.FILLED:
                    continue
                # Use a fingerprint that survives engine_id reuse across cycles.
                fp = (
                    order.order_id,
                    order.side.value,
                    f"{order.avg_fill_price:.4f}" if order.avg_fill_price else "",
                    order.filled_at.isoformat() if order.filled_at else "",
                )
                if fp in self._seen_fill_ids:
                    continue
                self._seen_fill_ids.add(fp)

                # Order placement latency: submitted_at → filled_at, in ms.
                # Coerce both sides to naive — ib_async fills carry tz-aware
                # UTC while engine-side timestamps are naive `datetime.now()`,
                # and a raw subtraction on mixed-tz datetimes raises TypeError
                # which kills the render loop. The render path must never
                # crash; we'd rather lose one latency sample than the entire
                # trading dashboard.
                if order.submitted_at and order.filled_at:
                    try:
                        fa = order.filled_at
                        sa = order.submitted_at
                        if fa.tzinfo is not None:
                            fa = fa.replace(tzinfo=None)
                        if sa.tzinfo is not None:
                            sa = sa.replace(tzinfo=None)
                        delta_ms = (fa - sa).total_seconds() * 1000.0
                        if delta_ms >= 0:
                            self.order_latency.add(delta_ms)
                    except Exception:
                        # Latency tracking is best-effort. Never crash render.
                        pass

                # Per-side slippage (only when both signal + fill are known).
                if order.signal_price and order.avg_fill_price:
                    when = order.filled_at.strftime('%H:%M') if order.filled_at else ""
                    side = order.side.value if hasattr(order.side, 'value') else str(order.side)
                    if side == "BUY":
                        slip = order.avg_fill_price - order.signal_price
                        self.slippage_tracker.add_buy(slip, when=when)
                    else:
                        slip = order.signal_price - order.avg_fill_price
                        self.slippage_tracker.add_sell(slip, when=when)

        # 3. Position context (breakout / highest) for the ladder.
        self.breakout_level = s.get('previous_breakout_level') or 0.0
        self.highest_price = s.get('highest_price') or 0.0

        # 4. Session / connection state from engine + supervisor (when available).
        self.paused = getattr(self.engine, '_paused', False)
        # Session state, per ASSET CLASS. The engine's _session_is_open() routes
        # through the AssetSpec, so FX reports its 24x5 week and futures their
        # venue calendar. Reading the global session_is_open() here instead --
        # as this panel used to -- painted every FX bot "closed, opens in 8.4h"
        # all night while the engine underneath was correctly trading, which
        # reads exactly like the bot is broken.
        try:
            spec = getattr(self.engine, '_asset_spec', None)
            now_utc = datetime.now(timezone.utc)

            if hasattr(self.engine, '_session_is_open'):
                self.in_session = self.engine._session_is_open()
            else:
                from src.config.models import session_is_open
                self.in_session = session_is_open()

            if self.in_session:
                # Inside session: time-until-close is harder to compute generically;
                # we just show "open" in the panel and seconds until close on a best-effort basis.
                self.next_session_change = 0.0
            elif spec is not None:
                self.next_session_change = max(
                    0.0, (spec.session.next_open(now_utc) - now_utc).total_seconds()
                )
            else:
                from src.config.models import seconds_until_session_open
                self.next_session_change = seconds_until_session_open()
        except Exception:
            self.in_session = True
            self.next_session_change = 0.0

        # 5. Connection heartbeat age + reconnect count (from supervisor on engine).
        # The conn_mgr is owned by LiveTrader, not engine, so this is best-effort.
        # We populate from gateway._last_heartbeat directly.
        #
        # FALLBACK FOR QUOTE-DRIVEN ASSETS: gateway._last_heartbeat is
        # updated by `Gateway.update_price()` — but that only fires on
        # paper-mode poll or specific legacy paths. For CFD INDEX where
        # we receive BBO ticks but never call update_price, the
        # heartbeat would stay frozen at the connection time forever
        # (showing "943.9s ago" while ticks are arriving at 2.9/s).
        #
        # Treat the LATEST tick timestamp from FeedStats as a valid
        # heartbeat source too — if ticks are flowing, the connection
        # is alive regardless of which code path the price arrived
        # through. Use whichever signal is FRESHER.
        gw = self.engine.gateway if self.engine else None
        candidates: list[float] = []
        if gw and getattr(gw, '_last_heartbeat', None):
            try:
                candidates.append((datetime.now() - gw._last_heartbeat).total_seconds())
            except Exception:
                pass
        # If FeedStats has seen a tick recently, that's a fresher
        # heartbeat — convert monotonic timestamp to "seconds ago".
        if self.feed._times:
            try:
                tick_age = time.monotonic() - self.feed._times[-1]
                candidates.append(tick_age)
            except Exception:
                pass
        self.heartbeat_age_s = min(candidates) if candidates else 0.0

    @property
    def engine_state(self) -> str:
        if not self.engine:
            return "IDLE"
        return self.engine.get_status().get('state', 'IDLE')

    @property
    def position(self) -> str:
        if not self.engine:
            return "FLAT"
        # SHORT INVERSION (P11): short-only book — open position is SHORT.
        return "SHORT" if self.engine.get_status().get('position_open') else "FLAT"

    @property
    def entry(self) -> float:
        if not self.engine:
            return 0.0
        return self.engine.get_status().get('entry_price') or 0.0

    @property
    def trigger(self) -> float:
        if not self.engine:
            return 0.0
        status = self.engine.get_status()
        # Use breakout level if available (re-entry mode), otherwise use config trigger
        breakout = status.get('previous_breakout_level')
        if breakout:
            return breakout
        return status.get('trigger_price') or 0.0

    @property
    def stop(self) -> float:
        if not self.engine:
            return 0.0
        return self.engine.get_status().get('stop_loss') or 0.0

    @property
    def sl_resting(self) -> bool:
        """True only when the protective SELL stop is CONFIRMED resting at
        the broker (engine's `_pending_stop` has a SELL handle). The stop
        PRICE is computed immediately on BUY fill; the actual broker order
        placement happens a few milliseconds later via an asyncio task —
        and on slow paths can take longer. The dashboard should not claim
        "(resting GTC)" until the broker truly has the order.
        """
        if not self.engine:
            return False
        return bool(self.engine.get_status().get('sl_resting_at_broker'))

    @property
    def high(self) -> float:
        if not self.engine:
            return 0.0
        # Use engine's highest_price when in position, otherwise use feed's daily high
        hp = self.engine.get_status().get('highest_price')
        if hp:
            return hp
        # During monitoring, show feed's daily high
        return self.feed.high if self.feed.high > 0 else 0.0

    @property
    def dec(self) -> int:
        """Decimals of display precision for THIS bot's symbol.

        Resolves once via the AssetSpec registry — equity gets 2 (matches
        the legacy hardcoded behavior), EURUSD/GBPUSD etc. get 5,
        JPY pairs get 3, futures get the tick's natural precision.

        Without this property the dashboard formatted every price with
        `:.2f` — collapsing EURUSD's `1.16125` to `$1.16` and making
        the price ladder useless on FX. (Reported live 2026-06-05.)
        """
        # Cheap getattr with default — returns 2 in the unit-test
        # path where engine has no _asset_spec.
        spec = getattr(self.engine, '_asset_spec', None) if self.engine else None
        if spec is None:
            return 2
        try:
            # tick.decimals_for_display takes a Price — for the legacy
            # display path we just want the natural decimals of the
            # tick grid. Passing 1.0 as a representative value works
            # for every policy (none of them branch on magnitude).
            from src.assets.types import price as _to_price
            return int(spec.tick.decimals_for_display(_to_price(1.0)))
        except Exception:
            return 2


def fmt_px(s: 'State', value: float, width: int = 0, with_dollar: bool = True) -> str:
    """Spec-aware price formatter for the dashboard.

    Routes through `State.dec` so EURUSD renders at 5dp, JPY pairs at
    3dp, equity at 2dp (unchanged). `width` is the total field width
    INCLUDING the leading `$` if requested — we right-pad with spaces
    to align decimal points across rows of the same panel.

    Caller passes the SAME `width` it used in the legacy `${val:>7.2f}`
    formatter; this helper computes the right .{n}f component and
    expands the width as needed to fit the extra decimals without
    truncating significant digits. For equity (dec=2) the output is
    BYTE-IDENTICAL to the legacy formatter so historical screens look
    the same.
    """
    n = s.dec
    if value is None:
        value = 0.0
    # Equity legacy: dec=2, width unchanged. FX: dec=5, width bumps
    # by 3 to keep 5 significant digits visible.
    extra = max(0, n - 2)
    eff_width = max(0, width + extra)
    if with_dollar:
        # Reserve one char for $ so the numeric part is `eff_width - 1`.
        body = f"{value:>{max(0, eff_width - 1)}.{n}f}"
        return f"${body}"
    return f"{value:>{eff_width}.{n}f}"


# ═══════════════════════════════════════════════════════════════════════════
# DRAW HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def draw_header(s: State) -> str:
    # UTC and New York time (ET). _ET is ZoneInfo("America/New_York"),
    # so EST/EDT switches automatically — no winter drift.
    ts_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
    ts_ny = datetime.now(_ET).strftime("%H:%M:%S")
    dot = f"{G}●{R}" if s.connected else f"{R_}○{R}"
    # Header LTP: spec-aware precision (5dp on FX, 2dp on equity).
    px = fmt_px(s, s.feed.last, width=0) if s.feed.last > 0 else "$----"

    sc_map = {
        "MONITORING": C, "IN_POSITION": G, "ORDER_ENTRY": Y,
        "EXIT_POSITION": Y, "WAITING_REENTRY": C, "EMERGENCY_STOP": R_
    }
    sc = sc_map.get(s.engine_state, "")
    return f"{B}{C}GT{R} {dot} {sc}{s.engine_state}{R}  {B}{s.symbol}{R} {px}  {D}{ts_utc} UTC / {ts_ny} ET{R}"


def draw_feed_panel(s: State) -> list[str]:
    """Left-top: feed stats + LTP sparkline + recent trades.

    Standardized price precision: %.2f for equity prices (matches tick size),
    %.4f reserved for slippage/latency. Right-aligned numeric columns so the
    decimal point stays vertically aligned for fast scanning.
    """
    lines = []
    f = s.feed

    # Header: price | session change | spread (all 2-decimal, aligned)
    spread = f.ask - f.bid if f.bid > 0 and f.ask > 0 else 0
    change = f.last - f.open_px if f.open_px > 0 else 0
    chg_c = G if change > 0 else R_ if change < 0 else D

    lines.append(
        f"{C}{B}FEED{R}  "
        f"{B}${f.last:>8.2f}{R} "
        f"{chg_c}{change:+7.2f}{R}  "
        f"{D}spread:{R} ${spread:>4.2f}"
    )

    # LTP sparkline — 30 chars wide, last 30 trades
    spark = sparkline(f._ltp_history, width=30)
    if f._ltp_history:
        # Color sparkline by overall direction (last vs first in window)
        hist = list(f._ltp_history)
        spark_c = G if hist[-1] > hist[0] else R_ if hist[-1] < hist[0] else D
        lines.append(f"  {D}LTP:{R} {spark_c}{spark}{R}")
    else:
        lines.append(f"  {D}LTP:{R} {D}{spark}{R}")

    # BBO row
    if f.bid > 0 and f.ask > 0:
        lines.append(
            f"  {G}B: ${f.bid:>7.2f}{R}  "
            f"{R_}A: ${f.ask:>7.2f}{R}  "
            f"{D}sz:{R} {f.bid_size:>5,}/{f.ask_size:<5,}"
        )
    else:
        lines.append(f"{D}  waiting for BBO...{R}")

    # Recent trades — aligned columns: time, price, size, exchange
    lines.append(f"\n{C}RECENT TRADES{R}  {D}rate: {f.rate:>6.1f}/s{R}")
    for t in list(s.feed._trade_hist)[-5:]:
        cond_color = D
        if 'I' in t.get('conditions', ''):
            cond_color = Y
        elif 'F' in t.get('conditions', ''):
            cond_color = G
        lines.append(
            f"  {D}{t['time']}{R}  "
            f"${t['price']:>7.2f}  "
            f"{cond_color}{t['size']:>5.0f}{R}  "
            f"{D}{t['exchange']:<6}{R}"
        )

    lines.append(f"\n{D}Total: {f.total:>6,}  Trades: {f.trade_count:>6,}  BBO: {f.bbo_count:>5,}{R}")
    return lines


def draw_state_panel(s: State) -> list[str]:
    """Right-top: System state, position, live latency, backpressure."""
    sc_map = {
        "MONITORING": C, "IN_POSITION": G, "ORDER_ENTRY": Y,
        "EXIT_POSITION": Y, "WAITING_REENTRY": C, "EMERGENCY_STOP": R_
    }
    sc = sc_map.get(s.engine_state, "")
    pos = f"{R_}SHORT{R} @${s.entry:.2f}" if s.position == "SHORT" and s.entry else f"{D}FLAT{R}"  # SHORT INVERSION (P11)

    lines = [
        f"{C}{B}SYSTEM{R}",
        f"  State:    {sc}{s.engine_state}{R}",
        f"  Position: {pos}",
        f"  Trigger:  ${s.trigger:>7.2f}",
        f"  Stop:     ${s.stop:>7.2f}",
        f"  High:     ${s.high:>7.2f}",
    ]

    # Live latency — p50 is the typical case; p99 is the tail you actually
    # care about for order placement. Color the tail red when it gets nasty.
    lat = s._latency or {}
    p50 = lat.get('p50_ms', 0.0)
    p99 = lat.get('p99_ms', 0.0)
    if p99 > 0:
        p99_c = R_ if p99 > 2.0 else (Y if p99 > 1.0 else D)
        lines.append(
            f"  {D}Lat:{R}      "
            f"p50={p50:>5.2f}ms {p99_c}p99={p99:>5.2f}ms{R}"
        )

    # Backpressure indicators — only shown when actively dropping/queueing,
    # otherwise hidden (clean look in normal operation).
    if s.ticks_dropped > 0:
        lines.append(f"  {R_}Dropped:  {s.ticks_dropped:,} ticks{R}")
    if s.tick_queue_depth > 100:
        lines.append(f"  {Y}Q-depth:  {s.tick_queue_depth:,}{R}")

    lines.append("")
    return lines


def draw_orders_panel(s: State) -> list[str]:
    """Left-bottom: Live orders from engine registry."""
    lines = [f"{Y}{B}ORDERS{R} ({len(s.orders)})"]

    if not s.orders:
        lines.append(f"{D}no orders{R}")
        for _ in range(5):
            lines.append("")
        return lines

    # Show last 12 orders (more visibility)
    for o in list(s.orders)[-12:]:
        side_c = G if o.get('side') == "BUY" else R_
        sc = {"FILLED": G, "SUBMITTED": Y, "REJECTED": R_}.get(o.get('status', ''), "")
        px = o.get('px', 0)
        px_str = f"${px:.2f}" if px > 0 else "---"
        ts = o.get('ts', '--:--:--')
        lines.append(f"  {side_c}{o.get('side', ''):<4}{R} {o.get('qty', 0):>3} @{px_str}  {sc}{o.get('status', ''):<10}{R}  {ts}")
    while len(lines) < 14:
        lines.append("")
    return lines


def draw_risk_panel(s: State) -> list[str]:
    """Right-bottom: P&L and risk stats."""
    # Read realized directly — sync_engine already deducted any open-cycle
    # entry commission (matches the user-preferred accounting split:
    # realized = sunk costs, unrealized = pure mark-to-market).
    realized_pnl = s.realized_pnl

    pnl_c = G if s.total_pnl >= 0 else R_
    unreal_c = G if s.unrealized_pnl >= 0 else R_
    real_c = G if realized_pnl >= 0 else R_
    wr = f"{s.wins}/{s.losses}"
    lines = [
        f"{G}{B}P&L{R}",
        f"  Unreal: {unreal_c}${s.unrealized_pnl:+.2f}{R}",
        f"  Real:   {real_c}${realized_pnl:+.2f}{R}",
        f"  Total:  {pnl_c}${s.total_pnl:+.2f}{R}",
        f"  Trades: {s.trades} ({wr})",
        f"  Comm:   ${s.comm:.2f}",
    ]

    # ── RISK panel ─ shown when RiskCheck is wired ─────────────────
    if s._risk_status and s._risk_limits:
        r, lim = s._risk_status, s._risk_limits

        consec = r.get('consec_losses', 0)
        max_consec = lim.get('max_consec_losses', 0)
        trades_today = r.get('trades_today', 0)
        max_trades = lim.get('max_trades_per_day', 0)
        daily_pnl = r.get('daily_pnl', 0.0)
        equity = r.get('equity', 0.0)
        daily_loss_pct = lim.get('daily_loss_limit_pct', 0.0)
        # daily_loss_pct is negative (e.g. -0.02), so daily_loss_dollar < 0.
        daily_loss_dollar = equity * daily_loss_pct

        # Warn yellow when within 20% of any limit; red when at/over.
        def _gate(used, mx):
            if mx <= 0: return D
            ratio = used / mx
            if ratio >= 1.0: return R_
            if ratio >= 0.8: return Y
            return D

        consec_c = _gate(consec, max_consec)
        trades_c = _gate(trades_today, max_trades)

        # Daily-loss color: red if at/past limit, yellow at 80%, dim otherwise.
        if daily_loss_dollar < 0 and daily_pnl <= daily_loss_dollar:
            dpnl_c = R_
        elif daily_loss_dollar < 0 and daily_pnl <= 0.8 * daily_loss_dollar:
            dpnl_c = Y
        else:
            dpnl_c = G if daily_pnl >= 0 else R_

        # Progress bars give an at-a-glance read on how close we are to each
        # ceiling. Width=10 for compact display in the right column.
        consec_bar = progress_bar(consec, max_consec, width=10)
        trades_bar = progress_bar(trades_today, max_trades, width=10)
        # Daily-loss bar: ratio is |daily_pnl| / |daily_loss_dollar| when
        # in the red, else 0. We only show progress when losing.
        dpnl_ratio = (
            abs(daily_pnl / daily_loss_dollar)
            if (daily_loss_dollar < 0 and daily_pnl < 0) else 0.0
        )
        dpnl_bar = progress_bar(dpnl_ratio, 1.0, width=10)

        lines.extend([
            "",
            f"{Y}{B}RISK{R}",
            f"  ConsL:  {consec_c}{consec_bar}{R} {consec_c}{consec:>3}/{max_consec:<3}{R}",
            f"  Today:  {trades_c}{trades_bar}{R} {trades_c}{trades_today:>3}/{max_trades:<3}{R}",
            f"  Daily$: {dpnl_c}{dpnl_bar}{R} {dpnl_c}${daily_pnl:+8.2f}{R}",
        ])

    # ── SLIPPAGE attribution (when any fills) ──────────────────────
    if s.fill_count > 0:
        slip_c = R_ if s.avg_slippage > 0.10 else (Y if s.avg_slippage > 0.05 else D)
        lines.extend([
            "",
            f"{C}{B}FILL Q{R}",
            f"  Avg slip: {slip_c}${s.avg_slippage:+.4f}{R}",
            f"  Worst:    {slip_c}${s.worst_slippage:+.4f}{R}",
            f"  Fills:    {s.fill_count}",
        ])

    return lines


def draw_events_panel(s: State, width: int) -> list[str]:
    """Footer: persistent rolling log of engine events.

    Shows last 8 messages from `s.events` — engine logs (fills, state
    transitions, risk blocks, reconnects, etc.) that would otherwise scroll
    past the dashboard and be lost. Color-coded by content keyword.
    """
    entries = s.events.tail(8)
    lines = [f"{Y}{B}EVENTS{R} {D}(last 8){R}"]
    if not entries:
        lines.append(f"  {D}no events yet{R}")
    else:
        for ts, msg in entries:
            ts_str = ts.strftime('%H:%M:%S')
            # Heuristic coloring — keywords trump severity
            lm = msg.lower()
            if any(k in lm for k in ('rejected', 'naked', 'tripwire', 'failed', 'error')):
                c = R_
            elif any(k in lm for k in ('blocked', 'dropped', 'warning', 'cancel')):
                c = Y
            elif any(k in lm for k in ('filled', 'placed', 'restored', 'started')):
                c = G
            else:
                c = D
            # Truncate long messages so they fit one row
            avail = max(20, width - len(ts_str) - 4)
            if vis(msg) > avail:
                msg = msg[: avail - 1] + '…'
            lines.append(f"  {D}{ts_str}{R} {c}{msg}{R}")
    return lines


# ═══════════════════════════════════════════════════════════════════════════
# V2 PANEL RENDERERS — senior-quant aesthetic
#
# Each `draw_v2_*` function takes State and returns a list of lines. Lines
# include ANSI color codes; width is bounded by the panel parameter so the
# layout grid can pad / trim consistently.
# ═══════════════════════════════════════════════════════════════════════════


def draw_v2_header(s: State, total_width: int) -> str:
    """Top status strip: ticker, price, change, session badge, clocks."""
    # _ET is ZoneInfo("America/New_York") — auto-switches EST/EDT, no
    # winter drift. Previously hardcoded to UTC-4 (EDT) which silently
    # showed the wrong hour from Nov to early Mar.
    ts_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
    ts_et = datetime.now(_ET).strftime("%H:%M:%S")

    # Connection badge
    if s.connected:
        conn_badge = f"{Q.GREEN}{Glyph.DOT} LIVE{Q.RESET}"
    else:
        conn_badge = f"{Q.RED}{Glyph.CIRCLE} OFFLINE{Q.RESET}"

    # State badge (engine state machine)
    state_colors = {
        "MONITORING":      Q.CYAN,
        "WAITING_REENTRY": Q.CYAN,
        "IN_POSITION":     Q.GREEN,
        "EXIT_POSITION":   Q.YELLOW,
        "ORDER_ENTRY":     Q.YELLOW,
        "STOPPED":         Q.GRAY_3,
        "EMERGENCY_STOP":  Q.RED,
    }
    state = s.engine_state
    state_c = state_colors.get(state, Q.GRAY_3)
    state_badge = f"{state_c}{Q.BOLD}{state}{Q.RESET}"

    # Price + change — spec-aware precision (5dp for EURUSD, 2dp for AAPL).
    if s.feed.last > 0:
        chg = s.feed.last - s.feed.open_px if s.feed.open_px > 0 else 0
        pct = (chg / s.feed.open_px * 100) if s.feed.open_px > 0 else 0
        if chg > 0:
            chg_c, arrow = Q.GREEN, Glyph.UP
        elif chg < 0:
            chg_c, arrow = Q.RED, Glyph.DOWN
        else:
            chg_c, arrow = Q.GRAY_3, Glyph.FLAT
        chg_dec = s.dec
        price_block = (
            f"{Q.WHITE}{Q.BOLD}{fmt_px(s, s.feed.last, width=10)}{Q.RESET} "
            f"{chg_c}{arrow}{chg:+.{chg_dec}f} ({pct:+.2f}%){Q.RESET}"
        )
    else:
        price_block = f"{Q.GRAY_2}$--.--{Q.RESET}"

    spread = s.feed.ask - s.feed.bid if s.feed.bid > 0 and s.feed.ask > 0 else 0
    spread_block = (
        f"{Q.GRAY_3}spread{Q.RESET} {Q.GRAY_4}{fmt_px(s, spread, width=7)}{Q.RESET}"
        if spread > 0 else f"{Q.GRAY_2}spread --{Q.RESET}"
    )

    # Session badge
    if s.in_session:
        sess_badge = f"{Q.GREEN}{Glyph.DOT} ETH ACTIVE{Q.RESET}"
    elif s.paused:
        sess_badge = f"{Q.PINK}{Glyph.HALF} PAUSED{Q.RESET}"
    else:
        sess_badge = f"{Q.GRAY_3}{Glyph.CIRCLE} CLOSED{Q.RESET}"

    # Compose left + right halves of the header. Symbol now carries its
    # client_id as a dim suffix (`TSLA c2`) so when multiple ticker
    # processes are running side-by-side in tmux panes the operator can
    # tell which dashboard belongs to which process at a glance — and
    # critically distinguishes two processes that share a symbol but
    # different client_ids.
    cid_badge = f"{Q.GRAY_3}c{s.client_id}{Q.RESET}" if s.client_id else ""
    left = (
        f"{Q.CYAN}{Q.BOLD}GT{Q.RESET}  "
        f"{conn_badge}  {state_badge}  "
        f"{Q.WHITE}{s.symbol}{Q.RESET} {cid_badge}  {price_block}  {spread_block}"
    )
    right = (
        f"{Q.GRAY_3}{ts_utc} UTC{Q.RESET} {Q.GRAY_2}{Glyph.BAR_V}{Q.RESET} "
        f"{Q.GRAY_4}{ts_et} ET{Q.RESET}  {sess_badge}"
    )

    # Right-align the right block against total_width
    pad = max(1, total_width - vis(left) - vis(right))
    return left + (' ' * pad) + right


def draw_v2_portfolio_footer(s: State, width: int) -> list[str]:
    """Account-level pills (equity / exposure / BP utilization).

    ALWAYS returns exactly 4 rows (header + 3 data rows) regardless of
    whether equity/BP are populated. Placeholders ("--") fill in the gaps
    when gateway data isn't ready yet. Fixed row count is REQUIRED — the
    caller anchors this block to the bottom of the POSITION LADDER column
    via vertical padding, so changing the row count would un-anchor it.

    Rows:
      0:  PORTFOLIO section header
      1:  equity $X   day Y%
      2:  posval $X   (exposure %)
      3:  bp use $X   (utilization %)
    """
    realized = s.realized_pnl
    equity = s._risk_status.get('equity', 0.0) if s._risk_status else 0.0
    buying_power = s._risk_status.get('buying_power', 0.0) if s._risk_status else 0.0

    out: list[str] = [f"{Q.BOLD}{Q.GRAY_5}PORTFOLIO{Q.RESET}"]

    # Row 1: equity + day P&L %
    if equity > 0:
        day_pct = (realized / equity * 100)
        day_c = Q.GREEN if day_pct >= 0 else (Q.YELLOW if day_pct > -1 else Q.RED)
        out.append(
            f"  {Q.GRAY_3}equity{Q.RESET}  {Q.WHITE}${equity:>10,.2f}{Q.RESET}  "
            f"{Q.GRAY_3}day{Q.RESET} {day_c}{day_pct:+.3f}%{Q.RESET}"
        )
    else:
        out.append(
            f"  {Q.GRAY_3}equity{Q.RESET}  {Q.GRAY_2}{'--':>11}{Q.RESET}  "
            f"{Q.GRAY_3}day{Q.RESET} {Q.GRAY_2}  --   {Q.RESET}"
        )

    # Row 2: position value + exposure %
    if s.position == "SHORT" and s.entry > 0 and s.feed.last > 0:  # SHORT INVERSION (P11)
        qty = s.engine.get_status().get('quantity', 0) if s.engine else 0
        pos_value = s.feed.last * qty
        if equity > 0:
            exposure = (pos_value / equity * 100)
            exp_c = Q.GREEN if exposure < 1 else Q.YELLOW if exposure < 10 else Q.RED
            exp_str = f"{exp_c}({exposure:.3f}%){Q.RESET}"
        else:
            exp_str = f"{Q.GRAY_2}(  --  ){Q.RESET}"
        out.append(
            f"  {Q.GRAY_3}posval{Q.RESET}  {Q.WHITE}${pos_value:>10,.2f}{Q.RESET}  {exp_str}"
        )
    else:
        out.append(
            f"  {Q.GRAY_3}posval{Q.RESET}  {Q.GRAY_4}${0:>10,.2f}{Q.RESET}  "
            f"{Q.GRAY_2}( 0.000%){Q.RESET}"
        )

    # Row 3: buying power + BP utilization %
    # Color thresholds: green <25% (room), yellow <50% (tight), red ≥50%.
    if buying_power > 0:
        if s.position == "SHORT" and s.entry > 0 and s.feed.last > 0:  # SHORT INVERSION (P11)
            qty = s.engine.get_status().get('quantity', 0) if s.engine else 0
            bp_used = (s.feed.last * qty) / buying_power * 100
            bp_c = Q.GREEN if bp_used < 25 else Q.YELLOW if bp_used < 50 else Q.RED
            bp_str = f"{bp_c}({bp_used:.3f}%){Q.RESET}"
        else:
            bp_str = f"{Q.GREEN}( 0.000%){Q.RESET}"
        out.append(
            f"  {Q.GRAY_3}bp use{Q.RESET}  {Q.GRAY_5}${buying_power:>10,.2f}{Q.RESET}  {bp_str}"
        )
    else:
        out.append(
            f"  {Q.GRAY_3}bp use{Q.RESET}  {Q.GRAY_2}{'--':>11}{Q.RESET}  "
            f"{Q.GRAY_2}(  --  ){Q.RESET}"
        )

    return out


def draw_v2_position_ladder(s: State, width: int) -> list[str]:
    """Vertical price ladder showing key levels relative to LTP.

    Rungs (top→bottom by price):
        - Session high           (info, gray)
        - Highest since entry    (peak in cycle, blue)
        - Breakout level         (re-entry trigger if WAITING_REENTRY, cyan)
        - Trigger price          (entry, yellow)
        - LTP                    (current, white with ★ marker)
        - Stop limit             (limit price of resting SL, red)
        - Stop trigger           (red)

    Each rung shows: price, marker, label, and distance from LTP.
    """
    out = [f"{Q.BOLD}{Q.GRAY_5}POSITION LADDER{Q.RESET}"]

    # Use last trade price when available; in pre-market last=0 for hours
    # until a real trade prints, but BBO mid is a perfectly good "current
    # price" reference for the ladder. Without this fallback the ladder
    # showed "awaiting first LTP…" for the entire pre-open window even
    # though the BBO stream was flowing the whole time.
    ltp = s.feed.last
    if ltp <= 0 and s.feed.bid > 0 and s.feed.ask > 0:
        ltp = (s.feed.bid + s.feed.ask) * 0.5
    if ltp <= 0:
        out.extend([
            "",
            f"  {Q.GRAY_2}awaiting market data…{Q.RESET}",
            "",
        ])
        return out

    # Collect candidate levels — only include the ones currently meaningful.
    #
    # DAY HIGH comes from IBKR's BBO ticker `tick.high` field — an aggregated
    # daily snapshot the broker publishes periodically. PEAK comes from our
    # own real-time `_track_high` walk over every LTP trade print, so on a
    # fresh new-high LTP, PEAK can briefly lead DAY HIGH by a cent or two
    # before IBKR's daily-high snapshot catches up. Clamp the display so
    # DAY HIGH is always at least as high as PEAK — otherwise the panel
    # showed the logically-impossible "PEAK > DAY HIGH" inversion.
    levels: list[tuple[float, str, str]] = []
    day_high = s.feed.high
    if s.highest_price and s.highest_price > day_high:
        day_high = s.highest_price
    if day_high > 0:
        levels.append((day_high, "DAY HIGH", Q.GRAY_4))
    # SHORT INVERSION (P11): highest_price holds the cycle TROUGH (min low) for
    # a short — it sits BELOW the current LTP, not above.
    if s.highest_price > 0 and s.highest_price < ltp:
        levels.append((s.highest_price, "TROUGH", Q.BLUE))
    if s.position == "SHORT" and s.entry > 0:  # SHORT INVERSION (P11)
        levels.append((s.entry, "ENTRY", Q.GREEN))
    # Trigger / breakout: which is meaningful depends on state
    if s.engine_state == "WAITING_REENTRY" and s.breakout_level > 0:
        levels.append((s.breakout_level, "RE-BREAKDOWN", Q.YELLOW))  # SHORT INVERSION (P11)
    elif s.trigger > 0:
        levels.append((s.trigger, "TRIGGER", Q.YELLOW))
    if s.stop > 0:
        levels.append((s.stop, "STOP", Q.RED))
    if s.feed.low > 0 and s.feed.low < ltp * 0.98:
        levels.append((s.feed.low, "DAY LOW", Q.GRAY_4))

    # Merge LTP into the sorted set, marked
    rows: list[tuple[float, str, str, bool]] = []
    for px, label, color in levels:
        rows.append((px, label, color, False))
    rows.append((ltp, "LTP", Q.WHITE, True))
    rows.sort(key=lambda r: r[0], reverse=True)

    # Render — spec-aware precision so EURUSD shows 1.16125 not 1.16.
    # epsilon for "essentially zero diff" scales with tick: on FX with
    # 5dp the legacy 0.005 floor would hide every meaningful diff.
    eps = 10 ** (-s.dec) * 0.5
    diff_dec = s.dec
    out.append("")
    for px, label, color, is_current in rows:
        diff = px - ltp
        if abs(diff) < eps:
            diff_str = ""
        elif diff > 0:
            diff_str = f"{Q.GREEN}+{diff:>{4 + (diff_dec - 2)}.{diff_dec}f}{Q.RESET}"
        else:
            diff_str = f"{Q.RED}{diff:>{5 + (diff_dec - 2)}.{diff_dec}f}{Q.RESET}"

        px_str = fmt_px(s, px, width=9)
        if is_current:
            marker = f"{Q.YELLOW}{Q.BOLD}{Glyph.STAR}{Q.RESET}"
            line = (
                f"  {Q.BOLD}{Q.WHITE}{px_str}{Q.RESET} {marker} "
                f"{Q.BOLD}{color}{label:<10}{Q.RESET} {diff_str}"
            )
        else:
            marker = f"{Q.GRAY_2}{Glyph.LEFT}{Q.RESET}"
            line = (
                f"  {color}{px_str}{Q.RESET} {marker} "
                f"{color}{label:<10}{Q.RESET} {diff_str}"
            )
        out.append(line)
    return out


def draw_v2_microstructure(s: State, width: int) -> list[str]:
    """Live L1 market microstructure panel.

    Shows everything we extract from the IBKR L1 (BBO + tick-by-tick) feed,
    arranged for at-a-glance reading by an operator running a breakout-on-LTP
    strategy:

      Row 1  Bid/Mid/Ask + sizes        (top of book snapshot)
      Row 2  Spread (bps) + imbalance   (liquidity quality + directional pressure)
      Row 3  Session range slider       (where is LTP within today's range)
      Row 4  Open / VWAP / change       (session benchmarks)
      Row 5+ TAPE: classified last 6    (above-ask ▲ / within ─ / below-bid ▼)
      Last   Per-second rates + aggression %

    All cells use cached aggregates updated O(1) per tick by MicrostructureStats —
    render itself is pure formatting (no math beyond simple ratios).
    """
    f = s.feed
    m = s.micro
    out = [f"{Q.BOLD}{Q.GRAY_5}MARKET MICROSTRUCTURE{Q.RESET}"]

    # Render as soon as we have EITHER BBO or a last-trade price. The old
    # check `last <= 0 OR (bid <= 0 AND ask <= 0)` bailed whenever last
    # was zero, which is the entire pre-market window — even though BBO
    # was flowing the whole time. Bail only when we have neither side
    # of L1 data (the genuine "nothing yet" case).
    if f.last <= 0 and (f.bid <= 0 or f.ask <= 0):
        out.append(f"  {Q.GRAY_2}awaiting L1 stream…{Q.RESET}")
        return out

    # ─── Row 1: BBO snapshot ─
    mid = (f.bid + f.ask) * 0.5 if f.bid > 0 and f.ask > 0 else f.last
    # All BBO/mid/ask use spec-aware precision — `width=9` leaves 1 char
    # for the leading `$` so the numeric part has 8, fitting both
    # equity ("  230.45") and FX ("  1.16125") cleanly.
    out.append(
        f"  {Q.GREEN}B {Q.BOLD}{fmt_px(s, f.bid, width=9)}{Q.RESET}{Q.GRAY_2}×{Q.RESET}{Q.GRAY_4}{f.bid_size:>4}{Q.RESET}  "
        f"{Q.GRAY_3}MID{Q.RESET} {Q.WHITE}{fmt_px(s, mid, width=9)}{Q.RESET}  "
        f"{Q.RED}A {Q.BOLD}{fmt_px(s, f.ask, width=9)}{Q.RESET}{Q.GRAY_2}×{Q.RESET}{Q.GRAY_4}{f.ask_size:<4}{Q.RESET}"
    )

    # ─── Row 2: Spread (bps) + Imbalance ratio ─
    spread = f.ask - f.bid if f.bid > 0 and f.ask > 0 else 0
    spread_bps = (spread / mid * 10000) if mid > 0 else 0
    sprd_c = Q.GREEN if spread_bps < 5 else Q.YELLOW if spread_bps < 20 else Q.RED

    if f.bid_size > 0 and f.ask_size > 0:
        if f.bid_size >= f.ask_size:
            imb_ratio = f.bid_size / f.ask_size
            imb_label = "BID heavy"
            imb_arrow = Glyph.UP
            imb_c = Q.GREEN
        else:
            imb_ratio = f.ask_size / f.bid_size
            imb_label = "ASK heavy"
            imb_arrow = Glyph.DOWN
            imb_c = Q.RED
    else:
        imb_ratio, imb_label, imb_arrow, imb_c = 0, "", "", Q.GRAY_3

    # Spread also needs spec-aware precision — on FX a typical spread is
    # $0.00010 (1 pip), which `$0.00` would just hide.
    out.append(
        f"  {Q.GRAY_3}spread{Q.RESET} {sprd_c}{fmt_px(s, spread, width=7)}{Q.RESET} {Q.GRAY_2}({sprd_c}{spread_bps:>4.1f}{Q.RESET} {Q.GRAY_2}bps){Q.RESET}   "
        f"{Q.GRAY_3}imbalance{Q.RESET} {imb_c}{imb_arrow}{imb_ratio:>4.2f}{Q.RESET} {Q.GRAY_4}{imb_label}{Q.RESET}"
    )

    # ─── Row 3: Range slider ─ position of LTP within today's range
    if f.high > 0 and f.low > 0 and f.high > f.low:
        range_pos = (f.last - f.low) / (f.high - f.low)
        # 30-char track with LTP marker
        track_w = max(20, min(36, width - 24))
        marker_at = int(round(range_pos * (track_w - 1)))
        before = '─' * marker_at
        after = '─' * (track_w - marker_at - 1)
        slider = f"{Q.GRAY_3}{before}{Q.YELLOW}{Glyph.DIAMOND}{Q.GRAY_3}{after}{Q.RESET}"
        out.append(
            f"  {Q.GRAY_3}L{fmt_px(s, f.low, width=9)}{Q.RESET} {slider} {Q.GRAY_3}{fmt_px(s, f.high, width=9)}H{Q.RESET}  "
            f"{Q.YELLOW}{range_pos*100:>3.0f}%{Q.RESET}"
        )
    else:
        out.append(f"  {Q.GRAY_2}awaiting daily high/low…{Q.RESET}")

    # ─── Row 4: Session benchmarks (open / VWAP / change) ─
    chg = f.last - f.open_px if f.open_px > 0 else 0
    chg_pct = (chg / f.open_px * 100) if f.open_px > 0 else 0
    chg_c = Q.GREEN if chg > 0 else Q.RED if chg < 0 else Q.GRAY_3
    chg_arrow = Glyph.UP if chg > 0 else Glyph.DOWN if chg < 0 else Glyph.FLAT
    vwap = m.vwap
    vwap_str = fmt_px(s, vwap, width=9) if vwap > 0 else f"{Q.GRAY_2}   --   "
    # Color VWAP green if LTP > VWAP (above the average → buyers in control)
    vwap_c = Q.GREEN if f.last > vwap and vwap > 0 else Q.RED if vwap > f.last > 0 else Q.GRAY_4
    # `chg` is also a price diff — render at spec precision.
    chg_dec = s.dec
    out.append(
        f"  {Q.GRAY_3}open{Q.RESET} {Q.GRAY_5}{fmt_px(s, f.open_px, width=9)}{Q.RESET}  "
        f"{Q.GRAY_3}vwap{Q.RESET} {vwap_c}{vwap_str}{Q.RESET}  "
        f"{Q.GRAY_3}chg{Q.RESET} {chg_c}{chg_arrow}{chg:+.{chg_dec}f} ({chg_pct:+.2f}%){Q.RESET}"
    )

    # ─── Tape ─ last 6 trades classified by direction (no blank separator;
    # the TAPE header doubles as visual divider, saves a row of vertical real estate)
    out.append(f"  {Q.GRAY_3}{Q.BOLD}TAPE{Q.RESET}  {Q.GRAY_2}(▲ above ask  ─ within  ▼ below bid){Q.RESET}")
    tape = m.tape[-6:]
    if not tape:
        out.append(f"  {Q.GRAY_2}no trades yet{Q.RESET}")
    else:
        for t in reversed(tape):  # newest at top
            if t.direction > 0:
                arrow, arrow_c = Glyph.UP, Q.GREEN
            elif t.direction < 0:
                arrow, arrow_c = Glyph.DOWN, Q.RED
            else:
                arrow, arrow_c = Glyph.BAR_H, Q.GRAY_4
            # Condition flag color: F = regular trade, I = odd lot, T = extended hours
            cond_c = Q.GRAY_3
            if 'I' in t.conditions: cond_c = Q.YELLOW
            elif 'T' in t.conditions: cond_c = Q.CYAN
            out.append(
                f"   {Q.GRAY_2}{t.ts}{Q.RESET}  "
                f"{Q.GRAY_5}{fmt_px(s, t.price, width=9)}{Q.RESET} "
                f"{arrow_c}{arrow}{Q.RESET}  "
                f"{Q.WHITE}{int(t.size):>5}{Q.RESET}  "
                f"{Q.GRAY_4}{(t.exchange[:6]):<6}{Q.RESET}"
                f"{cond_c}{t.conditions:<3}{Q.RESET}"
            )

    # ─── Last row: rates + aggression ─ (no blank separator, saves a row)
    buy_c = Q.GREEN if m.buy_pct > m.sell_pct else Q.GRAY_4
    sell_c = Q.RED if m.sell_pct > m.buy_pct else Q.GRAY_4
    out.append(
        f"  {Q.GRAY_3}rates{Q.RESET} "
        f"{Q.GRAY_5}tick {m.tick_rate:>5.1f}{Q.RESET}{Q.GRAY_2}/s{Q.RESET}  "
        f"{Q.GRAY_5}trd {m.trade_rate:>4.1f}{Q.RESET}{Q.GRAY_2}/s{Q.RESET}  "
        f"{Q.GRAY_5}bbo {m.bbo_rate:>5.1f}{Q.RESET}{Q.GRAY_2}/s{Q.RESET}"
    )
    out.append(
        f"  {Q.GRAY_3}flow{Q.RESET}  "
        f"{buy_c}buy {m.buy_pct:>4.1f}%{Q.RESET}  "
        f"{sell_c}sell {m.sell_pct:>4.1f}%{Q.RESET}"
    )

    return out


def draw_v2_system_status(s: State, width: int) -> list[str]:
    """Right-column system block: state, position, trade counts, win rate."""
    out = [f"{Q.BOLD}{Q.GRAY_5}SYSTEM{Q.RESET}"]

    state_colors = {
        "MONITORING":      Q.CYAN,
        "WAITING_REENTRY": Q.CYAN,
        "IN_POSITION":     Q.GREEN,
        "EXIT_POSITION":   Q.YELLOW,
        "STOPPED":         Q.GRAY_3,
        "EMERGENCY_STOP":  Q.RED,
    }
    state_c = state_colors.get(s.engine_state, Q.GRAY_3)
    out.append(f"  {Q.GRAY_3}State{Q.RESET}     {state_c}{s.engine_state}{Q.RESET}")
    # Surface the IBKR client_id alongside state so operators running
    # multiple ticker processes can immediately see which connection
    # this dashboard belongs to (matches the .gt_state_<SYM>_<CID>.json
    # file name).
    if s.client_id:
        out.append(f"  {Q.GRAY_3}Client ID{Q.RESET} {Q.WHITE}c{s.client_id}{Q.RESET}")

    if s.position == "SHORT" and s.entry > 0:  # SHORT INVERSION (P11)
        out.append(
            f"  {Q.GRAY_3}Position{Q.RESET}  {Q.RED}SHORT{Q.RESET} "  # SHORT INVERSION (P11)
            f"{Q.WHITE}{s.engine.get_status().get('quantity', 0) if s.engine else 0}{Q.RESET} "
            f"@ {Q.WHITE}{fmt_px(s, s.entry)}{Q.RESET}"
        )
        if s.stop > 0:
            # Only label as "resting GTC" when the SL is CONFIRMED at the
            # broker (engine._pending_stop set). Until then, show "arming…"
            # in yellow so the operator sees the gap between "stop level
            # computed" and "stop actually placed". The computed level is
            # set synchronously on BUY fill; the broker placement runs via
            # asyncio.create_task and lands ~ms later in the happy path
            # (or longer if the loop is contended — a real signal worth
            # surfacing rather than hiding behind a misleading label).
            if s.sl_resting:
                badge = f"{Q.GRAY_2}(resting GTC){Q.RESET}"
            else:
                badge = f"{Q.YELLOW}(arming…){Q.RESET}"
            out.append(f"  {Q.GRAY_3}Stop SL{Q.RESET}   {Q.RED}{fmt_px(s, s.stop)}{Q.RESET} {badge}")
    else:
        out.append(f"  {Q.GRAY_3}Position{Q.RESET}  {Q.GRAY_4}FLAT{Q.RESET}")
        if s.trigger > 0:
            out.append(f"  {Q.GRAY_3}Trigger{Q.RESET}   {Q.YELLOW}{fmt_px(s, s.trigger)}{Q.RESET}")

    # Trades + win rate
    out.append("")
    wr = (s.wins / max(1, s.trades)) * 100 if s.trades else 0
    wr_c = Q.GREEN if wr >= 50 else Q.YELLOW if wr >= 30 else Q.RED
    out.append(
        f"  {Q.GRAY_3}Trades{Q.RESET}    {Q.WHITE}{s.trades}{Q.RESET} "
        f"{Q.GRAY_2}({Q.GREEN}{s.wins}W{Q.RESET}{Q.GRAY_2}/{Q.RED}{s.losses}L{Q.RESET}{Q.GRAY_2}){Q.RESET}  "
        f"{Q.GRAY_3}wr{Q.RESET} {wr_c}{wr:.0f}%{Q.RESET}"
    )
    out.append(f"  {Q.GRAY_3}Comm{Q.RESET}      {Q.GRAY_4}${s.comm:.2f}{Q.RESET}")

    # ─── Compact P&L block (no blank separator before, saves a row) ─
    realized = s.realized_pnl
    real_c = Q.GREEN if realized >= 0 else Q.RED
    unreal_c = Q.GREEN if s.unrealized_pnl >= 0 else Q.RED
    pnl_c = Q.GREEN if s.total_pnl >= 0 else Q.RED
    out.append(f"  {Q.GRAY_3}P&L{Q.RESET}")
    out.append(f"    {Q.GRAY_3}real{Q.RESET}    {real_c}${realized:+7.2f}{Q.RESET}")
    out.append(f"    {Q.GRAY_3}unreal{Q.RESET}  {unreal_c}${s.unrealized_pnl:+7.2f}{Q.RESET}")
    out.append(f"    {Q.GRAY_3}total{Q.RESET}   {pnl_c}{Q.BOLD}${s.total_pnl:+7.2f}{Q.RESET}")

    # Portfolio metrics (equity, exposure, BP utilization) live in their
    # own footer panel anchored to the bottom of the POSITION LADDER column.
    # See draw_v2_portfolio_footer() and build_frame()'s upper-row composition.
    # Keeping it OUT of the SYSTEM panel matters because gateway equity values
    # blink in and out between 1s polls; mixing variable-height content here
    # would shake everything below in this panel frame-to-frame.

    return out


def draw_v2_short_req(s: State, width: int) -> list[str]:
    """SHORT REQ panel — the short-selling requirement for the current
    (or armed) short: initial/maintenance margin, restricted proceeds,
    and borrow/carry per day. Sourced from the engine's ShortPolicy
    (PDF §1–4); shows the authoritative IBKR whatIf number (§6) when
    captured.

    Returns [] when the engine exposes no short_requirement (e.g. no
    spec resolved, or no qty/price yet) — the panel then contributes
    nothing to the layout.
    """
    st = getattr(s, '_last_status', None) or {}
    req = st.get('short_requirement')
    if not req:
        return []

    whatif = st.get('short_margin_whatif') or {}

    def _m(v):
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return "--"

    # Header badge: authoritative (IBKR whatIf) vs offline estimate.
    src_badge = (f"{Q.TEAL}(IBKR){Q.RESET}" if whatif.get('maint_margin') is not None
                 else f"{Q.GRAY_2}(est){Q.RESET}")
    out = [f"{Q.BOLD}{Q.GRAY_5}SHORT REQ{Q.RESET}  {src_badge}"]

    # Short-sale AVAILABILITY (most important) — live from IBKR tick 236.
    # `shortable_available` / `shortable_shares` are only populated in the
    # snapshot when the live feed responded (None in paper / pre-fetch).
    avail = req.get('shortable_available')
    shares = req.get('shortable_shares')
    if avail is False:
        out.append(f"  {Q.GRAY_3}avail{Q.RESET}     {Q.RED}NOT shortable{Q.RESET}")
    elif avail is True or shares is not None:
        try:
            shares_str = f"{int(float(shares)):,}" if shares else "yes"
        except (TypeError, ValueError):
            shares_str = "yes"
        out.append(
            f"  {Q.GRAY_3}avail{Q.RESET}     {Q.GREEN}{shares_str:>11}{Q.RESET} "
            f"{Q.GRAY_2}shares{Q.RESET} {Q.TEAL}IBKR{Q.RESET}"
        )

    # Initial margin (own equity to open). Prefer IBKR's authoritative
    # whatIf number; fall back to the offline Reg-T 50% estimate.
    init_auth = whatif.get('init_margin')
    if init_auth is not None:
        out.append(
            f"  {Q.GRAY_3}init{Q.RESET}      {Q.WHITE}{_m(abs(init_auth)):>11}{Q.RESET} "
            f"{Q.TEAL}IBKR{Q.RESET}"
        )
    else:
        out.append(f"  {Q.GRAY_3}init{Q.RESET}      {Q.WHITE}{_m(req.get('initial_margin')):>11}{Q.RESET}")

    # Maintenance — prefer the authoritative whatIf number when present.
    maint_auth = whatif.get('maint_margin')
    if maint_auth is not None:
        out.append(
            f"  {Q.GRAY_3}maint{Q.RESET}     {Q.WHITE}{_m(abs(maint_auth)):>11}{Q.RESET} "
            f"{Q.TEAL}IBKR{Q.RESET}"
        )
    else:
        out.append(f"  {Q.GRAY_3}maint{Q.RESET}     {Q.GRAY_4}{_m(req.get('maintenance_margin')):>11}{Q.RESET}")

    # Restricted proceeds (equity only — PDF §3). Only show when relevant.
    if req.get('proceeds_restricted'):
        out.append(
            f"  {Q.GRAY_3}proceeds{Q.RESET}  {Q.YELLOW}{_m(req.get('proceeds_collateral')):>11}{Q.RESET} "
            f"{Q.GRAY_2}restr{Q.RESET}"
        )

    # Borrow / financing / swap per day. Only show when non-zero.
    try:
        carry = float(req.get('daily_carry') or 0)
    except (TypeError, ValueError):
        carry = 0.0
    if carry > 0:
        htb = req.get('hard_to_borrow')
        htb_badge = f" {Q.RED}HTB{Q.RESET}" if htb else ""
        # Badge the borrow fee's provenance: live IBKR FEE_RATE feed vs the
        # offline 25-bps placeholder. `borrow_rate_live` is set True by the
        # engine only when the live rate actually replaced the default.
        live_badge = f" {Q.TEAL}IBKR{Q.RESET}" if req.get('borrow_rate_live') else f" {Q.GRAY_2}est{Q.RESET}"
        if req.get('requires_locate'):
            label, pad = "borrow/d", "  "
        else:
            label, pad = "carry/d", "   "
        out.append(f"  {Q.GRAY_3}{label}{Q.RESET}{pad}{Q.GRAY_4}{_m(carry):>11}{Q.RESET}{htb_badge}{live_badge}")

    # Collateral multiplier (150% for Reg-T equity) — the headline ratio.
    mult = req.get('collateral_multiplier')
    if mult:
        try:
            out.append(f"  {Q.GRAY_3}collat{Q.RESET}    {Q.GRAY_4}{float(mult) * 100:>10.0f}%{Q.RESET}")
        except (TypeError, ValueError):
            pass

    return out


def draw_v2_connection_health(s: State, width: int) -> list[str]:
    """Right-column connection block: IBKR status, heartbeat, feed rate."""
    out = [f"{Q.BOLD}{Q.GRAY_5}CONNECTION{Q.RESET}"]

    # IBKR status
    if s.connected:
        out.append(f"  {Q.GREEN}{Glyph.DOT}{Q.RESET} {Q.GRAY_5}IBKR connected{Q.RESET}")
        # Heartbeat age — green <5s, yellow <30s, red >30s
        age = s.heartbeat_age_s
        if age < 5:
            age_c = Q.GREEN
        elif age < 30:
            age_c = Q.YELLOW
        else:
            age_c = Q.RED
        out.append(f"    {Q.GRAY_3}heartbeat{Q.RESET} {age_c}{age:.1f}s ago{Q.RESET}")
    else:
        out.append(f"  {Q.RED}{Glyph.CIRCLE}{Q.RESET} {Q.RED}IBKR disconnected{Q.RESET}")

    if s.reconnect_count > 0:
        out.append(f"    {Q.GRAY_3}reconnects{Q.RESET} {Q.YELLOW}{s.reconnect_count}{Q.RESET}")

    # Feed
    if s.feed.rate > 0:
        rate_c = Q.GREEN if s.feed.rate > 1 else Q.YELLOW
        out.append(f"  {Q.GREEN}{Glyph.DOT}{Q.RESET} {Q.GRAY_5}Feed active{Q.RESET}")
        out.append(f"    {rate_c}{s.feed.rate:.1f}{Q.RESET} {Q.GRAY_3}ticks/s{Q.RESET}")
    else:
        out.append(f"  {Q.GRAY_3}{Glyph.CIRCLE}{Q.RESET} {Q.GRAY_3}Feed idle{Q.RESET}")

    if s.ticks_dropped > 0:
        out.append(f"    {Q.RED}{s.ticks_dropped:,} dropped{Q.RESET}")

    # Session window (no blank separator — header acts as divider)
    if s.in_session:
        out.append(f"  {Q.GREEN}{Glyph.DOT}{Q.RESET} {Q.GRAY_5}ETH session{Q.RESET}")
        out.append(f"    {Q.GRAY_3}04:00 to 20:00 ET{Q.RESET}")
    else:
        hours = s.next_session_change / 3600
        out.append(f"  {Q.PINK}{Glyph.HALF}{Q.RESET} {Q.GRAY_5}Outside session{Q.RESET}")
        out.append(f"    {Q.GRAY_3}opens in {Q.YELLOW}{hours:.1f}h{Q.RESET}")

    return out


def draw_v2_latency(s: State, width: int) -> list[str]:
    """Two latency profiles: pipeline (tick→strategy) and order (placement→fill)."""
    out = [f"{Q.BOLD}{Q.GRAY_5}LATENCY{Q.RESET}"]

    # Pipeline (sub-millisecond expected)
    lat = s._latency or {}
    p50 = lat.get('p50_ms', 0.0)
    p95 = lat.get('p95_ms', 0.0)
    p99 = lat.get('p99_ms', 0.0)
    mx = lat.get('max_ms', 0.0)

    def _lat_c(v_ms: float, thresh_warn: float, thresh_bad: float) -> str:
        if v_ms >= thresh_bad: return Q.RED
        if v_ms >= thresh_warn: return Q.YELLOW
        return Q.GREEN

    out.append(f"  {Q.GRAY_3}pipeline (tick to strategy){Q.RESET}")
    out.append(
        f"    {Q.GRAY_3}p50{Q.RESET} {_lat_c(p50, 0.5, 1.0)}{p50:>5.2f}ms{Q.RESET}  "
        f"{Q.GRAY_3}p95{Q.RESET} {_lat_c(p95, 1.0, 2.0)}{p95:>5.2f}ms{Q.RESET}"
    )
    out.append(
        f"    {Q.GRAY_3}p99{Q.RESET} {_lat_c(p99, 2.0, 5.0)}{p99:>5.2f}ms{Q.RESET}  "
        f"{Q.GRAY_3}max{Q.RESET} {_lat_c(mx, 5.0, 20.0)}{mx:>5.2f}ms{Q.RESET}"
    )

    # Order placement → fill (broker round-trip — much larger than pipeline)
    out.append("")
    out.append(f"  {Q.GRAY_3}order (placement to fill){Q.RESET}")
    if s.order_latency.count == 0:
        out.append(f"    {Q.GRAY_2}no fills yet{Q.RESET}")
    else:
        med = s.order_latency.median
        op99 = s.order_latency.p99
        worst = s.order_latency.worst
        out.append(
            f"    {Q.GRAY_3}p50{Q.RESET} {_lat_c(med, 50, 200)}{med:>6.0f}ms{Q.RESET}  "
            f"{Q.GRAY_3}p99{Q.RESET} {_lat_c(op99, 200, 500)}{op99:>6.0f}ms{Q.RESET}"
        )
        out.append(
            f"    {Q.GRAY_3}worst{Q.RESET} {_lat_c(worst, 500, 2000)}{worst:>5.0f}ms{Q.RESET}  "
            f"{Q.GRAY_3}n{Q.RESET} {Q.WHITE}{s.order_latency.count}{Q.RESET}"
        )

    return out


def draw_v2_slippage(s: State, width: int) -> list[str]:
    """Slippage panel: side-split avg, worst, total cost + small bar histograms."""
    out = [f"{Q.BOLD}{Q.GRAY_5}SLIPPAGE{Q.RESET}"]

    avg_buy = s.slippage_tracker.avg_buy
    avg_sell = s.slippage_tracker.avg_sell
    worst = s.slippage_tracker.worst
    total_cost = s.slippage_tracker.total_cost

    if s.slippage_tracker.buy_history or s.slippage_tracker.sell_history:
        # Color: positive slip = cost to us, color red. Zero/negative = good, green/gray.
        # Slippage precision matches the asset's tick grid — equity 4dp,
        # FX 5dp (so a 1-pip EURUSD slip prints as `+0.00010` not `+0.0001`
        # which loses sub-pip detail).
        slip_dec = max(4, s.dec)
        buy_c = Q.RED if avg_buy > 0.02 else (Q.YELLOW if avg_buy > 0 else Q.GREEN)
        sell_c = Q.RED if avg_sell > 0.02 else (Q.YELLOW if avg_sell > 0 else Q.GREEN)
        out.append(f"  {Q.GRAY_3}avg buy{Q.RESET}   {buy_c}${avg_buy:+.{slip_dec}f}{Q.RESET}")
        out.append(f"  {Q.GRAY_3}avg sell{Q.RESET}  {sell_c}${avg_sell:+.{slip_dec}f}{Q.RESET}")
        if worst:
            out.append(
                f"  {Q.GRAY_3}worst{Q.RESET}     {Q.RED}${worst:+.{slip_dec}f}{Q.RESET} "
                f"{Q.GRAY_2}({s.slippage_tracker.worst_at}){Q.RESET}"
            )
        cost_c = Q.RED if total_cost > 0 else Q.GREEN
        out.append(f"  {Q.GRAY_3}total cost{Q.RESET}   {cost_c}${total_cost:+.{slip_dec}f}{Q.RESET}")

        # Two histograms: buy on top, sell on bottom (last 20 fills each)
        hist_w = min(20, max(8, width - 12))
        buy_hist = horizontal_histogram(s.slippage_tracker.buy_history, hist_w)
        sell_hist = horizontal_histogram(s.slippage_tracker.sell_history, hist_w)
        out.append("")
        out.append(f"  {Q.GRAY_3}buy{Q.RESET}  {Q.GREEN}{buy_hist}{Q.RESET}")
        out.append(f"  {Q.GRAY_3}sell{Q.RESET} {Q.RED}{sell_hist}{Q.RESET}")
    else:
        out.append(f"  {Q.GRAY_2}no fills yet{Q.RESET}")

    return out


def draw_v2_alerts(s: State, width: int) -> list[str]:
    """Last N alerts with severity color and timestamp."""
    out = [f"{Q.BOLD}{Q.GRAY_5}ALERTS{Q.RESET}"]

    alerts = s.alerts.tail(5)
    if not alerts:
        out.append(f"  {Q.GRAY_2}no alerts{Q.RESET}")
        return out

    sev_dot = {
        "CRITICAL": (Q.PURPLE, "CRIT"),
        "HIGH":     (Q.ORANGE, "HIGH"),
        "MEDIUM":   (Q.YELLOW, "MED "),
        "LOW":      (Q.CYAN,   "LOW "),
    }
    for a in alerts:
        try:
            sev = a.severity.value if hasattr(a.severity, 'value') else str(a.severity)
            ts = a.timestamp.strftime('%H:%M:%S')
            color, badge = sev_dot.get(sev, (Q.GRAY_3, sev[:4]))
            msg = a.message
            code = a.code
        except Exception:
            continue
        avail = max(15, width - 24)
        msg_trunc = msg if len(msg) <= avail else msg[:avail - 1] + '…'
        out.append(
            f"  {color}{Glyph.DOT} {badge}{Q.RESET} {Q.GRAY_2}{ts}{Q.RESET} "
            f"{Q.GRAY_5}{code}{Q.RESET}"
        )
        out.append(f"      {Q.GRAY_3}{msg_trunc}{Q.RESET}")
    return out


def draw_v2_orders(s: State, width: int) -> list[str]:
    """Recent orders with side, qty, price, status, timestamp."""
    out = [f"{Q.BOLD}{Q.GRAY_5}ORDERS{Q.RESET} {Q.GRAY_2}(last 8){Q.RESET}"]

    if not s.orders:
        out.append(f"  {Q.GRAY_2}no orders yet{Q.RESET}")
        return out

    status_c = {
        "FILLED":    Q.GREEN,
        "SUBMITTED": Q.YELLOW,
        "REJECTED":  Q.RED,
        "CANCELLED": Q.GRAY_3,
        "PARTIAL":   Q.YELLOW,
    }

    for o in list(s.orders)[-8:]:
        side = o.get('side', '')
        side_c = Q.GREEN if side == "BUY" else Q.RED
        st = o.get('status', '')
        st_c = status_c.get(st, Q.GRAY_3)
        px = o.get('px', 0)
        # Spec-aware price formatting (5dp on EURUSD vs 2dp on AAPL).
        px_str = fmt_px(s, px, width=9) if px > 0 else f"{Q.GRAY_2}   --   {Q.RESET}"
        ts = o.get('ts', '--:--:--')
        qty = o.get('qty', 0)
        # Qty is in base ccy for FX (potentially 25000), so allow more width.
        out.append(
            f"  {Q.GRAY_2}{ts}{Q.RESET}  {side_c}{Q.BOLD}{side:<4}{Q.RESET} "
            f"{Q.WHITE}{qty:>6}{Q.RESET} @ {Q.GRAY_5}{px_str}{Q.RESET}  "
            f"{st_c}{st:<9}{Q.RESET}"
        )
    return out


def draw_v2_events(s: State, width: int) -> list[str]:
    """Engine event log — last 6."""
    out = [f"{Q.BOLD}{Q.GRAY_5}EVENTS{Q.RESET} {Q.GRAY_2}(last 6){Q.RESET}"]

    entries = s.events.tail(6)
    if not entries:
        out.append(f"  {Q.GRAY_2}no events yet{Q.RESET}")
        return out

    for ts, msg in entries:
        ts_str = ts.strftime('%H:%M:%S')
        lm = msg.lower()
        if any(k in lm for k in ('rejected', 'naked', 'tripwire', 'failed', 'error')):
            c = Q.RED
        elif any(k in lm for k in ('blocked', 'dropped', 'warning', 'cancel')):
            c = Q.YELLOW
        elif any(k in lm for k in ('filled', 'placed', 'restored', 'started', 'opened')):
            c = Q.GREEN
        else:
            c = Q.GRAY_4
        avail = max(20, width - len(ts_str) - 4)
        msg_trunc = msg if vis(msg) <= avail else msg[:avail - 1] + '…'
        out.append(f"  {Q.GRAY_2}{ts_str}{Q.RESET}  {c}{msg_trunc}{Q.RESET}")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# V2 FRAME BUILDER — multi-panel single-screen layout
#
# Grid (120 cols × ~46 rows):
#
#   ╔══════════════════════════════════════════════════════════════════════╗
#   ║ HEADER STRIP                                                          ║
#   ╠═══════════════════╤══════════════════════════╤═══════════════════════╣
#   ║ POSITION LADDER   │ EQUITY CURVE             │ SYSTEM                ║
#   ║                   │                          │                       ║
#   ║                   │                          ├───────────────────────╢
#   ║                   │                          │ CONNECTION            ║
#   ╠═══════════════════╪══════════════════════════┼───────────────────────╢
#   ║ LATENCY           │ SLIPPAGE                 │ ALERTS                ║
#   ║                   │                          │                       ║
#   ╠═══════════════════╧══════════════════════════╧═══════════════════════╣
#   ║ ORDERS                                  │ EVENTS                     ║
#   ╠══════════════════════════════════════════╧════════════════════════════╣
#   ║ Footer / shortcuts                                                    ║
#   ╚═══════════════════════════════════════════════════════════════════════╝
# ═══════════════════════════════════════════════════════════════════════════

# Layout widths.
#
# 150 chars total — gives every UI element more breathing room so the
# microstructure stats line, range slider, "Stop SL (resting GTC)" label,
# and rates row all fit without ellipsis truncation under normal market
# conditions. Cell math:
#   LEFT + MID + RIGHT + 10 (borders + intra-column spaces) = TOTAL
#   BLW  + BRW + 7 = TOTAL
V2_TOTAL_W = 147   # matches typical 14" laptop terminal width
V2_LEFT_W  = 37   # POSITION LADDER  (rungs + "DAY HIGH +X.XX" label)
V2_MID_W   = 62   # MARKET MICROSTRUCTURE (widest content: BBO row + tape)
V2_RIGHT_W = 38   # SYSTEM + CONNECTION block — 37 + 62 + 38 + 10 = 147

V2_BOTTOM_LEFT_W = 75   # ORDERS table
V2_BOTTOM_RIGHT_W = 65  # EVENTS list — 75 + 65 + 7 = 147


def _row(text: str, width: int) -> str:
    """Fit a single line to EXACTLY `width` visible chars inside a column.

    - If visible width > target: truncate with ellipsis (preserves ANSI codes).
    - If visible width < target: right-pad with spaces.
    - If equal: pass through.

    This is the defensive layer that guarantees no cell ever overflows its
    column border. Without this, a too-long label (e.g. a long alert message
    or wide microstructure stats line) pushes the right border off the grid
    and the terminal wraps content into adjacent cells, breaking alignment.
    """
    v = vis(text)
    if v > width:
        return truncate(text, width)
    if v < width:
        return text + ' ' * (width - v)
    return text


def _join_three(left: list[str], mid: list[str], right: list[str],
                lw: int, mw: int, rw: int, min_rows: int = 12) -> list[str]:
    """Join three vertical panels side-by-side with box-drawing borders."""
    rows = max(len(left), len(mid), len(right), min_rows)
    out = []
    for i in range(rows):
        l = left[i] if i < len(left) else ""
        m = mid[i] if i < len(mid) else ""
        r = right[i] if i < len(right) else ""
        out.append(
            f"{Q.GRAY_2}║{Q.RESET} {_row(l, lw)} "
            f"{Q.GRAY_2}│{Q.RESET} {_row(m, mw)} "
            f"{Q.GRAY_2}│{Q.RESET} {_row(r, rw)} "
            f"{Q.GRAY_2}║{Q.RESET}"
        )
    return out


def _join_two(left: list[str], right: list[str],
              lw: int, rw: int, min_rows: int = 10) -> list[str]:
    rows = max(len(left), len(right), min_rows)
    out = []
    for i in range(rows):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        out.append(
            f"{Q.GRAY_2}║{Q.RESET} {_row(l, lw)} "
            f"{Q.GRAY_2}│{Q.RESET} {_row(r, rw)} "
            f"{Q.GRAY_2}║{Q.RESET}"
        )
    return out


def _border(left_char: str, fill_char: str, sep_chars: list[tuple[int, str]], right_char: str) -> str:
    """Build a horizontal border row with vertical separators at given column offsets."""
    line = fill_char * (V2_TOTAL_W - 2)
    line_arr = list(line)
    for offset, sep_c in sep_chars:
        if 0 <= offset < len(line_arr):
            line_arr[offset] = sep_c
    return f"{Q.GRAY_2}{left_char}{''.join(line_arr)}{right_char}{Q.RESET}"


def build_frame(s: State, footer_override: Optional[str] = None) -> str:
    """V2 senior-quant single-screen dashboard.

    Layout: 3-column upper grid (ladder | equity | system+conn),
    3-column lower grid (latency | slippage | alerts),
    2-column orders/events at the bottom, footer with shortcuts.

    Args:
        s: dashboard State (samplers + engine reference)
        footer_override: optional alternate text for the footer row. When
            None, shows the engine shortcuts (Ctrl+\\, Ctrl+L, etc.) which
            apply when this dashboard is rendered inside `run_live.py`.
            The aggregator (`dashboard_agg.py`) passes its own nav keys
            instead, because the engine shortcuts don't work in a
            read-only viewer that doesn't own the engine process.
    """
    rows: list[str] = []

    # Compute separator column offsets for borders.
    # Top section: left (V2_LEFT_W+2) │ mid (V2_MID_W+2) │ right (V2_RIGHT_W+2)
    sep1_top_a = V2_LEFT_W + 3       # column index of the first │
    sep1_top_b = V2_LEFT_W + V2_MID_W + 6
    # Bottom section: V2_BOTTOM_LEFT_W (60) + V2_BOTTOM_RIGHT_W (58)
    sep_bot = V2_BOTTOM_LEFT_W + 3

    # ─── Top border + header strip ───
    rows.append(_border('╔', '═', [], '╗'))
    rows.append(
        f"{Q.GRAY_2}║{Q.RESET} {_row(draw_v2_header(s, V2_TOTAL_W - 4), V2_TOTAL_W - 4)} "
        f"{Q.GRAY_2}║{Q.RESET}"
    )
    rows.append(_border('╠', '═', [(sep1_top_a, '╤'), (sep1_top_b, '╤')], '╣'))

    # ─── Upper row: ladder + portfolio | microstructure | (system + conn) ───
    # MICROSTRUCTURE is the prime-real-estate centre panel. It pulls richer
    # signal from the L1 stream (BBO + tick-by-tick AllLast) than an empty
    # equity curve would early in a session.
    #
    # The PORTFOLIO footer (equity / exposure / BP) anchors to the BOTTOM of
    # the ladder column: ladder content first, then variable-height padding,
    # then the fixed 4-row portfolio block. Anchoring at the bottom keeps
    # the metric in the same screen position even when the ladder's row
    # count varies (LONG vs FLAT changes rung count) — no shake.
    ladder_body = draw_v2_position_ladder(s, V2_LEFT_W)
    portfolio = draw_v2_portfolio_footer(s, V2_LEFT_W)
    micro = draw_v2_microstructure(s, V2_MID_W)
    sys_block = draw_v2_system_status(s, V2_RIGHT_W)
    short_req_block = draw_v2_short_req(s, V2_RIGHT_W)
    conn_block = draw_v2_connection_health(s, V2_RIGHT_W)
    # No blank separator between panels — each panel's header acts as its
    # own visual divider, saves a row of vertical space. SHORT REQ sits
    # between SYSTEM and CONNECTION; it contributes zero rows when the
    # engine exposes no short_requirement (non-short assets / no spec).
    right = sys_block + short_req_block + conn_block

    # Decide the upper-row's target height ONCE, accounting for the portfolio
    # rows that will be appended to the ladder column. Then pad the ladder
    # so its portfolio block lands exactly at the bottom of that height.
    # If micro or right is taller than (ladder_body + portfolio + 1 spacer),
    # we expand `target` to match — the extra rows go into the ladder's
    # padding above the portfolio block, NOT after it.
    target_upper = max(
        len(ladder_body) + 1 + len(portfolio),   # ladder + ≥1 blank gap + portfolio
        len(micro),
        len(right),
        14,
    )
    spacer = max(1, target_upper - len(ladder_body) - len(portfolio))
    ladder = ladder_body + [""] * spacer + portfolio

    rows.extend(_join_three(ladder, micro, right,
                            V2_LEFT_W, V2_MID_W, V2_RIGHT_W, min_rows=target_upper))

    # Middle horizontal separator
    rows.append(_border('╠', '═', [(sep1_top_a, '╪'), (sep1_top_b, '╪')], '╣'))

    # ─── Lower row: latency | slippage | alerts ───
    lat = draw_v2_latency(s, V2_LEFT_W)
    slip = draw_v2_slippage(s, V2_MID_W)
    alerts = draw_v2_alerts(s, V2_RIGHT_W)
    rows.extend(_join_three(lat, slip, alerts,
                            V2_LEFT_W, V2_MID_W, V2_RIGHT_W, min_rows=10))

    # Separator before orders/events (collapse 3-col to 2-col)
    rows.append(_border('╠', '═', [(sep1_top_a, '╧'), (sep1_top_b, '╧'), (sep_bot, '╤')], '╣'))

    # ─── Bottom row: orders | events ───
    orders = draw_v2_orders(s, V2_BOTTOM_LEFT_W)
    events = draw_v2_events(s, V2_BOTTOM_RIGHT_W)
    rows.extend(_join_two(orders, events,
                          V2_BOTTOM_LEFT_W, V2_BOTTOM_RIGHT_W, min_rows=7))

    # Footer separator + shortcuts row
    rows.append(_border('╠', '═', [(sep_bot, '╧')], '╣'))
    if footer_override is not None:
        shortcuts = footer_override
    else:
        shortcuts = (
            f"{Q.GRAY_3}Shortcuts{Q.RESET}  "
            f"{Q.GRAY_2}Ctrl+\\{Q.RESET} {Q.GRAY_4}pause{Q.RESET}  "
            f"{Q.GRAY_2}Ctrl+Y{Q.RESET} {Q.GRAY_4}resume{Q.RESET}  "
            f"{Q.GRAY_2}Ctrl+L{Q.RESET} {Q.GRAY_4}force exit{Q.RESET}  "
            f"{Q.GRAY_2}Ctrl+X{Q.RESET} {Q.GRAY_4}cancel orders{Q.RESET}  "
            f"{Q.GRAY_2}Ctrl+Z{Q.RESET} {Q.GRAY_4}square off{Q.RESET}  "
            f"{Q.GRAY_2}Ctrl+C{Q.RESET} {Q.GRAY_4}quit{Q.RESET}"
        )
    rows.append(
        f"{Q.GRAY_2}║{Q.RESET} {_row(shortcuts, V2_TOTAL_W - 4)} "
        f"{Q.GRAY_2}║{Q.RESET}"
    )
    rows.append(_border('╚', '═', [], '╝'))

    return '\n'.join(rows)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN (standalone - for testing)
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='AAPL')
    args = parser.parse_args()

    print(f"{C}Dashboard ready - run via run_live.py for production use{R}\n")
    print(f"{D}Usage: python run_live.py {args.symbol}{R}")


if __name__ == "__main__":
    asyncio.run(main())