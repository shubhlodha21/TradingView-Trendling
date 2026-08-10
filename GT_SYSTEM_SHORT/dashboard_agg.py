#!/usr/bin/env python3
"""
GT Multi-Symbol Aggregator Dashboard

Reads per-symbol state + live snapshots + audit CSVs + alerts JSONL produced
by one or more `run_live.py` processes, and renders ONE consolidated view.

Architecture (file polling, zero coupling to live processes):
    .gt_state_<SYM>_<CID>.json    written by engine state writer (event-driven)
    .gt_live_<SYM>_<CID>.json     written by LiveTrader.write_live_snapshot (5Hz)
    data/audit/order_<SYM>_*.csv  written by AuditManager
    data/audit/state_<SYM>_*.csv  written by AuditManager
    data/alerts/alerts_*.jsonl    written by AlertManager (shared across symbols)

Views:
    Default — multi-symbol summary table (one row per symbol)
              + combined orders (last 8 across all symbols)
              + combined alerts (last 5)
    [1-9]   — drill into symbol N (full single-symbol dashboard)
    [0]     — back to summary
    [q]     — quit

Runs in a separate terminal from the per-symbol `run_live.py` processes.
You can keep one tmux pane per symbol AND one pane running this aggregator.

Usage:
    python dashboard_agg.py
"""
import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # zoneinfo unavailable; header will fall back to fixed offset

sys.path.insert(0, 'src')

# Reuse existing rendering primitives + State + panels
from dashboard import (
    HOME, CLEAR_SCREEN, Q, Glyph,
    State, FeedStats, MicrostructureStats, TapeEntry,
    AlertView,
    build_frame, vis, truncate, _row,
    V2_TOTAL_W,
)
from src.config.models import session_is_open, seconds_until_session_open, COMMISSION_PER_SHARE, calc_ibkr_commission
from src.config.audit import read_recent_orders


# ═══════════════════════════════════════════════════════════════════════════
# SymbolWatcher — polls one symbol's files
# ═══════════════════════════════════════════════════════════════════════════

class SymbolWatcher:
    """Polls one symbol's state + live + audit files.

    Caches each file by mtime so repeated polls within the same mtime don't
    re-parse. File-format failures are tracked in `_state_errors` /
    `_live_errors` and surfaced in the footer so a partial write or
    transient corruption doesn't crash the aggregator AND doesn't hide.
    """

    # Consider a symbol "inactive" if its state file hasn't been touched in
    # this many seconds. Aggregator uses this to flag live-vs-offline; a
    # process whose state file goes silent for more than this is no longer
    # writing → presumed killed.
    # Overridable via GT_AGG_STALENESS_S env var — useful when running
    # symbols whose tick rate is slow (eg illiquid names) and the 30s
    # default would mark them inactive between state writes.
    STALENESS_S = int(os.environ.get("GT_AGG_STALENESS_S", "30"))

    # Hide state files older than this from the aggregator entirely — keeps
    # yesterday's (or last week's) killed sessions from cluttering today's
    # view. Default 12h covers a Friday-late kill being visible through
    # Saturday morning while still rolling over for next-week sessions.
    # Set to a very large number (eg 999999) to never expire stale rows.
    OFFLINE_WINDOW_S = int(os.environ.get("GT_AGG_OFFLINE_S", str(12 * 3600)))

    # tail_orders() re-walks up to 45 days of audit files. A symbol with a
    # single resting bracket never reaches `n` records, so it never breaks
    # early and pays the full walk — per call, per frame. The files are tiny,
    # but the dashboard renders in a loop, so cache the result briefly. 5s is
    # far below human notice for an ORDERS panel and turns ~45 stat()s per
    # frame into ~45 every 5s.
    ORDERS_CACHE_TTL_S = float(os.environ.get("GT_AGG_ORDERS_TTL_S", "5"))

    __slots__ = (
        'symbol', 'client_id', 'state_path', 'live_path',
        '_state_cache', '_state_mtime', '_live_cache', '_live_mtime',
        '_state_errors', '_live_errors', '_last_error',
        # tail_orders() result cache. The backward walk is bounded by
        # max_days_back, and a symbol whose only activity is one resting
        # bracket never collects `n` records — so it scans the full window
        # (~45 stat()s) on every call. Files are tiny, but the dashboard
        # re-renders continuously and there's no reason to re-walk at frame
        # rate. Cached for ORDERS_CACHE_TTL_S.
        '_orders_cache', '_orders_cache_at',
    )

    def __init__(self, state_path: Path):
        self.state_path = state_path
        # Filename format: .gt_state_NVDA_1.json -> symbol=NVDA, client_id=1
        stem = state_path.stem.replace('.gt_state_', '')
        parts = stem.rsplit('_', 1)
        self.symbol = parts[0] if parts else stem
        self.client_id = parts[1] if len(parts) > 1 else "?"
        self.live_path = state_path.parent / f".gt_live_{self.symbol}_{self.client_id}.json"
        self._state_cache: dict = {}
        self._state_mtime: float = 0
        self._live_cache: dict = {}
        self._live_mtime: float = 0
        # Error diagnostics — counted, not silenced. `_last_error` keeps
        # the most recent exception string so the footer can show *why*.
        self._state_errors: int = 0
        self._live_errors: int = 0
        self._last_error: str = ""
        self._orders_cache: list[dict] = []
        self._orders_cache_at: float = 0.0

    @property
    def is_active(self) -> bool:
        if not self.state_path.exists():
            return False
        try:
            return (time.time() - self.state_path.stat().st_mtime) < self.STALENESS_S
        except OSError:
            return False

    @property
    def is_within_offline_window(self) -> bool:
        """True if state file was touched recently enough to still show in
        the OFFLINE zone (default 12h). Files older than that are dropped
        from discovery entirely so the aggregator doesn't accumulate dead
        sessions from prior days."""
        if not self.state_path.exists():
            return False
        try:
            return (time.time() - self.state_path.stat().st_mtime) < self.OFFLINE_WINDOW_S
        except OSError:
            return False

    @property
    def last_seen_str(self) -> str:
        """HH:MM:SS of the state file's last mtime — used in the OFFLINE
        badge so the operator knows when the process died.

        Returns '--:--:--' if the file is missing or unreadable.
        """
        if not self.state_path.exists():
            return "--:--:--"
        try:
            mt = self.state_path.stat().st_mtime
        except OSError:
            return "--:--:--"
        if _ET is not None:
            return datetime.fromtimestamp(mt, tz=_ET).strftime("%H:%M:%S")
        return datetime.fromtimestamp(mt).strftime("%H:%M:%S")

    @property
    def offline_for_s(self) -> int:
        """Seconds since the state file was last written. 0 if live."""
        if not self.state_path.exists():
            return 0
        try:
            return max(0, int(time.time() - self.state_path.stat().st_mtime))
        except OSError:
            return 0

    def poll_state(self) -> dict:
        if not self.state_path.exists():
            return self._state_cache
        try:
            mtime = self.state_path.stat().st_mtime
            if mtime == self._state_mtime:
                return self._state_cache
            with open(self.state_path) as f:
                self._state_cache = json.load(f)
            self._state_mtime = mtime
        except Exception as e:
            self._state_errors += 1
            self._last_error = f"state: {type(e).__name__}"
        return self._state_cache

    def poll_live(self) -> dict:
        if not self.live_path.exists():
            return self._live_cache
        try:
            mtime = self.live_path.stat().st_mtime
            if mtime == self._live_mtime:
                return self._live_cache
            with open(self.live_path) as f:
                self._live_cache = json.load(f)
            self._live_mtime = mtime
        except Exception as e:
            self._live_errors += 1
            self._last_error = f"live: {type(e).__name__}"
        return self._live_cache

    def tail_orders(self, n: int = 12) -> list[dict]:
        """Return the last N audit order events for this symbol.

        Uses `read_recent_orders` — the SAME backward day-walk + supersession
        filter the individual terminal uses — so the two views agree. It used
        to read only TODAY's CSV, which meant a resting order placed weeks ago
        (the bracket protecting an open position) rendered as "no order events
        yet" here while the terminal showed it. Reading today-only also showed
        superseded/cancelled SUBMITTEDs that the terminal correctly hid.

        `with_rows=True` gives us `(OrderRecord, raw_csv_row)` pairs. The
        record drives the filter; the DISPLAY values come from the raw row,
        because OrderRecord doesn't carry every CSV column — `pnl`, `reason`
        and `slippage` are dropped by the row→record conversion, and the
        renderers below use `slippage` and `signal_price`. Building the dicts
        from the raw row keeps this panel's columns intact.

        Row format from src/config/audit.py OrderWriter:
            timestamp, event, order_id, side, qty, order_type,
            limit_price, stop_price, signal_price, fill_price, slippage,
            commission, pnl, reason, exchange, state_at_time, position_at_time
        """
        now = time.time()
        if self._orders_cache_at and (now - self._orders_cache_at) < self.ORDERS_CACHE_TTL_S:
            return self._orders_cache[-n:]
        try:
            pairs = read_recent_orders(self.symbol, n=max(n, 20), with_rows=True)
        except Exception as e:
            self._last_error = f"orders: {type(e).__name__}"
            return self._orders_cache[-n:]  # last good result beats a blank panel

        rows = []
        for _rec, row in pairs:
            # Pass the raw CSV strings straight through: the renderers already
            # coerce (float(...)/int(float(...))) and treat '' as "missing",
            # so this preserves their existing tolerance for older rows.
            rows.append({
                'ts': (row.get('timestamp') or ''),
                'event': (row.get('event') or ''),
                'order_id': (row.get('order_id') or ''),
                'side': (row.get('side') or ''),
                'qty': (row.get('qty') or ''),
                'order_type': (row.get('order_type') or ''),
                'limit_price': (row.get('limit_price') or ''),
                'stop_price': (row.get('stop_price') or ''),
                'signal_price': (row.get('signal_price') or ''),
                'fill_price': (row.get('fill_price') or ''),
                'slippage': (row.get('slippage') or ''),
                'commission': (row.get('commission') or ''),
                'pnl': (row.get('pnl') or ''),
                'reason': (row.get('reason') or ''),
            })
        self._orders_cache = rows
        self._orders_cache_at = now
        return rows[-n:]


def discover_watchers(state_dir: Path = Path('.')) -> list[SymbolWatcher]:
    """Find all .gt_state_*_*.json files modified within the offline window.

    Returns a flat list sorted with LIVE tickers first (alphabetical), then
    OFFLINE tickers (most-recently-killed first). The flat ordering matters
    because the drill-down keys [1-9] index into this list — operators expect
    "1" to be the first live ticker, not the first alphabetical including
    offline. Offline tickers come AFTER all live so killing a process doesn't
    renumber the remaining live ones.

    State files older than `OFFLINE_WINDOW_S` (default 12h) are dropped entirely
    so yesterday's killed sessions don't haunt today's screen.
    """
    candidates = []
    for path in state_dir.glob('.gt_state_*_*.json'):
        w = SymbolWatcher(path)
        if w.is_within_offline_window:
            candidates.append(w)
    live = sorted([w for w in candidates if w.is_active], key=lambda w: w.symbol)
    # Offline: most-recently-killed first (smallest offline_for_s) — those are
    # the ones the operator most likely just Ctrl-C'd and wants to see.
    offline = sorted([w for w in candidates if not w.is_active], key=lambda w: w.offline_for_s)
    return live + offline


def tail_alerts(n: int = 5) -> list[dict]:
    """Tail today's shared alerts.jsonl across all symbols."""
    today = datetime.now().strftime("%Y%m%d")
    path = Path(f"data/alerts/alerts_{today}.jsonl")
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = f.readlines()
        out = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════
# Multi-symbol summary view
# ═══════════════════════════════════════════════════════════════════════════

# Use the same total width as the live single-symbol dashboard. Allows the
# user to swap views without resizing the terminal.
AGG_TOTAL_W = V2_TOTAL_W


def _border(left: str, right: str, w: int = AGG_TOTAL_W) -> str:
    """A simple horizontal border row of width `w`."""
    return f"{Q.GRAY_2}{left}{'═' * (w - 2)}{right}{Q.RESET}"


def _line(text: str, w: int = AGG_TOTAL_W) -> str:
    """Wrap a line of content in `║ ... ║` border at width `w`."""
    return f"{Q.GRAY_2}║{Q.RESET} {_row(text, w - 4)} {Q.GRAY_2}║{Q.RESET}"


_STATE_COLORS = {
    "MONITORING": Q.CYAN,
    "WAITING_REENTRY": Q.CYAN,
    "IN_POSITION": Q.GREEN,
    "EXIT_POSITION": Q.YELLOW,
    "ORDER_ENTRY": Q.YELLOW,
    "STOPPED": Q.GRAY_3,
    "EMERGENCY_STOP": Q.RED,
    "IDLE": Q.GRAY_3,
}


def render_summary(watchers: list[SymbolWatcher]) -> str:
    """Multi-symbol summary table + combined orders + combined alerts.

    Two-zone layout:
      • LIVE zone   — tickers whose run_live.py is currently writing state.
                      Full colour, real-time LAST/UNREAL from .gt_live_*.json.
      • OFFLINE zone — tickers killed recently (within OFFLINE_WINDOW_S).
                      Dimmed styling, "last seen HH:MM:SS" badge, no LAST/UNREAL
                      (those would be stale). The state file + order CSVs are
                      still tailed so the operator can see what the process
                      was doing before it died.

    Combined orders pulls from BOTH zones so a kill doesn't erase order history.
    """
    rows: list[str] = []

    # Partition once — same ordering as discover_watchers (live first).
    live_watchers = [w for w in watchers if w.is_active]
    offline_watchers = [w for w in watchers if not w.is_active]

    # ─── Header strip ─
    # Use ZoneInfo("America/New_York") so the ET clock switches between
    # EST/EDT correctly — the previous hardcoded UTC-4 silently drifted
    # by one hour during winter sessions.
    now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if _ET is not None:
        now_et = datetime.now(_ET).strftime("%H:%M:%S")
    else:
        now_et = datetime.now(timezone(timedelta(hours=-5))).strftime("%H:%M:%S")
    # Realized P&L + commission spans LIVE AND OFFLINE — operator wants the
    # full session view, not just what's currently breathing.
    total_real = sum(w.poll_state().get('pnl', 0) or 0 for w in watchers)
    total_comm = sum(w.poll_state().get('total_commission', 0) or 0 for w in watchers)
    pnl_c = Q.GREEN if total_real >= 0 else Q.RED
    n_active = len(live_watchers)
    n_offline = len(offline_watchers)
    in_sess = session_is_open()
    sess_badge = f"{Q.GREEN}{Glyph.DOT} ETH{Q.RESET}" if in_sess else f"{Q.PINK}{Glyph.HALF} CLOSED{Q.RESET}"

    # ─── Account-level metrics (equity, exposure %, BP used %) ──────────
    # Equity and buying_power are ACCOUNT-WIDE — every live snapshot for
    # this account reports the same numbers. So we take the freshest value
    # we can find (any live watcher; falls back to most-recent offline if
    # nothing's live). Position notional, by contrast, IS per-symbol and
    # gets summed across ALL watchers (live + offline) so a killed-but-
    # still-LONG ticker still contributes to the exposure number.
    #
    # Why sum across offline too: if you killed AAPL while LONG 100 shares,
    # your account is still exposed to those shares — pretending exposure
    # is 0 just because the bot is dead would be dangerously optimistic.
    # The position is in the saved state file; we read it from there.
    account_equity = 0.0
    account_bp = 0.0
    # IBKR account daily P&L (realized+unrealized, auto-reset each session).
    # Account-wide, so any live bot's value is THE portfolio day P&L — take
    # the first non-null one. None (never seen) → rendered "--".
    account_day_pnl = None
    for w in live_watchers + offline_watchers:
        snap = w.poll_live()
        if not account_equity:
            account_equity = float(snap.get('equity', 0) or 0)
        if not account_bp:
            account_bp = float(snap.get('buying_power', 0) or 0)
        if account_day_pnl is None and snap.get('daily_pnl_ibkr') is not None:
            try:
                account_day_pnl = float(snap.get('daily_pnl_ibkr'))
            except (TypeError, ValueError):
                account_day_pnl = None
        if account_equity and account_bp and account_day_pnl is not None:
            break

    # Sum position notional across ALL watchers. For live watchers we use
    # the snapshot's pre-computed `position_notional` (which used the live
    # LTP). For offline watchers the live snapshot is stale, so we fall
    # back to entry_price × quantity from the state file — not perfectly
    # mark-to-market, but the right number for "how much capital is tied
    # up at this position" and free from feed-staleness concerns.
    #
    # Exposure counts FILLED positions only. A working-but-unfilled entry
    # (`pending_notional`) is deliberately NOT added, matching what
    # PortfolioReader.open_notional() sums on the risk-gate side, so the
    # displayed exposure stays aligned with the gate's view.
    gross_notional = 0.0
    for w in live_watchers:
        snap = w.poll_live()
        gross_notional += float(snap.get('position_notional', 0) or 0)
    for w in offline_watchers:
        st = w.poll_state()
        if st.get('position_open'):
            qty = float(st.get('quantity', 0) or 0)
            entry = float(st.get('entry_price', 0) or 0)
            gross_notional += qty * entry

    exposure_pct = (gross_notional / account_equity * 100.0) if account_equity > 0 else 0.0
    bp_used_pct = (gross_notional / account_bp * 100.0) if account_bp > 0 else 0.0

    # Color thresholds chosen for a typical day-trading margin account.
    # Tune in code if your account profile is different.
    exp_c = Q.GREEN if exposure_pct < 25 else Q.YELLOW if exposure_pct < 75 else Q.RED
    bp_c = Q.GREEN if bp_used_pct < 25 else Q.YELLOW if bp_used_pct < 50 else Q.RED

    # Offline count gets its own pill, only when > 0 — keeps the header
    # uncluttered for the common "everything running" case.
    offline_pill = (
        f"  {Q.GRAY_3}{Glyph.HALF}{Q.RESET} {Q.GRAY_4}{n_offline} offline{Q.RESET}"
        if n_offline else ""
    )

    # Account pills (equity / EXP / BP USED) only render when we have the
    # numbers — keeps paper sessions and pre-connect renders from showing
    # a confusing "$0.00" header.
    if account_equity > 0:
        account_pills = (
            f"  {Q.GRAY_3}eq{Q.RESET} {Q.WHITE}${account_equity:,.0f}{Q.RESET}  "
            f"{Q.GRAY_3}exp{Q.RESET} {exp_c}{exposure_pct:.2f}%{Q.RESET}"
        )
        if account_bp > 0:
            account_pills += f"  {Q.GRAY_3}bp{Q.RESET} {bp_c}{bp_used_pct:.2f}%{Q.RESET}"
        # IBKR account daily P&L (the same number the risk gate's daily-loss
        # circuit breaker uses). Green when up, red when down. "--" until the
        # reqPnL feed lands.
        if account_day_pnl is not None:
            day_c = Q.GREEN if account_day_pnl >= 0 else Q.RED
            account_pills += (
                f"  {Q.GRAY_3}day P&L{Q.RESET} {day_c}${account_day_pnl:+,.2f}{Q.RESET}"
            )
    else:
        account_pills = ""

    # Risk-cap pill — surfaces the portfolio-wide caps currently in force
    # via `.gt_portfolio_limits.json`. Empty when the file is absent (each
    # bot then uses its own config / CLI). When present, displayed in teal
    # so it's clearly an operator-set override vs the per-bot defaults.
    risk_pills = ""
    try:
        # Lazy import keeps the dashboard render path light when nobody
        # has set portfolio limits yet.
        from src.strategy.risk import PortfolioLimitsReader as _PLR
        _lim = _PLR().read()
        bits = []
        if _lim.get("max_position_value_usd"):
            bits.append(f"{Q.GRAY_3}cap{Q.RESET} {Q.CYAN}${int(_lim['max_position_value_usd']):,}{Q.RESET}")
        if _lim.get("max_daily_loss_usd"):
            bits.append(f"{Q.GRAY_3}stop{Q.RESET} {Q.CYAN}-${int(_lim['max_daily_loss_usd']):,}{Q.RESET}")
        if bits:
            risk_pills = "  " + "  ".join(bits)
    except Exception:
        # Reader failure must never break the dashboard render — silently
        # leave the pill empty.
        risk_pills = ""

    header = (
        f"{Q.CYAN}{Q.BOLD}GT MULTI{Q.RESET}  "
        f"{Q.GREEN}{Glyph.DOT}{Q.RESET} {Q.WHITE}{n_active}{Q.RESET} {Q.GRAY_3}active{Q.RESET}"
        f"{offline_pill}  "
        f"{Q.GRAY_3}realized{Q.RESET} {pnl_c}{Q.BOLD}${total_real:+.2f}{Q.RESET}  "
        f"{Q.GRAY_3}comm{Q.RESET} {Q.GRAY_4}${total_comm:.2f}{Q.RESET}"
        f"{account_pills}{risk_pills}  {sess_badge}  "
        f"{Q.GRAY_3}{now_utc} UTC{Q.RESET} {Q.GRAY_2}{Glyph.BAR_V}{Q.RESET} "
        f"{Q.GRAY_4}{now_et} ET{Q.RESET}"
    )

    rows.append(_border('╔', '╗'))
    rows.append(_line(header))
    rows.append(_border('╠', '╣'))

    # ─── Table layout ─
    # Single source of truth for column widths used by BOTH the header
    # and every per-symbol row. Previously the header had hand-padded
    # spaces while rows used a mix of `_row()`-padded cells and unpadded
    # variable-width cells (notably W/L), so the columns drifted out of
    # vertical alignment whenever any variable cell's width changed.
    # Now every cell goes through `_row()` against the SAME width entry
    # from this dict — the totals are guaranteed identical row-to-header.
    COL_W = [
        ("#",        3),    # row index, "1  "
        ("SYM",      6),    # ticker, "TSLA  "
        ("CID",      5),    # client_id, "c2   "
        ("STATE",   15),    # "MONITORING    "
        ("POSITION", 22),   # "LONG 1 @ $416.95     "
        # POSVAL = per-ticker position notional (qty × LTP for live tickers,
        # qty × entry for offline since live LTP isn't available). Grouped
        # next to POSITION because they're the two views of the same thing:
        # POSITION says "what shares at what entry", POSVAL says "how many
        # dollars that's worth right now". The bottom summary line sums the
        # POSVAL column across LIVE rows only — close a terminal and its
        # contribution drops out of the total immediately.
        ("POSVAL",  13),    # "  $20,450.00 "
        ("LAST",    10),    # " $416.95  "
        ("REAL$",   12),    # "  $+1234.56 "
        ("UNREAL$", 12),    # "  $+1234.56 "
        ("TRD",      5),    # " 12  "
        ("W/L",      9),    # "  3/5    "
        ("COMM",     9),    # " $12.34  "
        ("TRIG",    10),    # " $416.95  "
    ]

    # Header — each cell wrapped in _row() so its visible width is exact.
    tbl_header = "".join(
        _row(f"{Q.GRAY_3}{Q.BOLD}{name}{Q.RESET}", width)
        for name, width in COL_W
    )
    rows.append(_line(tbl_header))

    # ─── Per-symbol rows ─
    def _render_row(w, idx, *, offline: bool) -> str:
        """Render one symbol row. `offline=True` dims the row + replaces
        live-only fields (LAST, UNREAL) with placeholders, since the feed
        is dead so any cached value would be stale + misleading."""
        st = w.poll_state()
        live = w.poll_live()
        state = st.get('state', '--') or '--'

        pos_open = st.get('position_open', False)
        entry = st.get('entry_price') or 0
        qty = st.get('quantity', 0) or 0
        # REAL$ = engine._pnl (from closed cycles) minus the entry
        # commission of the OPEN cycle, when in position. Matches the
        # per-ticker dashboard's accounting: realized = sunk costs we've
        # already paid (broker debited them at fill time), unrealized =
        # pure mark-to-market on the open position.
        # IBKR Tiered formula (max($0.35, qty×$0.0035) + CAT + clearing) —
        # the old `qty × $0.35` was billing 50-100× too much per side.
        pnl_base = st.get('pnl', 0) or 0
        if pos_open and qty > 0:
            pnl = pnl_base - calc_ibkr_commission(qty, entry, "BUY")
        else:
            pnl = pnl_base
        trades = st.get('trades_today', 0)
        wins = st.get('wins', 0)
        losses = st.get('losses', 0)
        comm = st.get('total_commission', 0)
        trig = (st.get('previous_breakout_level')
                or live.get('trigger_price') or 0)

        if offline:
            # Dim everything to GRAY_3/GRAY_4 — clearly differentiates from
            # live rows at a glance. LAST/UNREAL replaced with the offline
            # badge because their last cached value is stale by definition.
            #
            # Badge is "OFF HH:MM:SS" (12 chars) not "OFFLINE HH:MM:SS"
            # (16 chars) so it fits inside the 15-char STATE column without
            # ellipsis-truncation. The full word "OFFLINE" still appears in
            # the section divider above, so there's no ambiguity.
            state_label = f"OFF {w.last_seen_str}"
            pos_str = (
                f"{Q.GRAY_4}SHORT {qty} @ ${entry:.2f}{Q.RESET}"  # SHORT INVERSION (P11)
                if pos_open and entry > 0 else
                f"{Q.GRAY_3}FLAT{Q.RESET}"
            )
            # POSVAL for offline: fall back to qty × entry_price (LIVE LTP
            # is stale by definition). Use the LAST known position_notional
            # from the snapshot if present (computed at the moment before
            # the bot died → freshest possible), else qty × entry.
            stale_notional = float(live.get('position_notional', 0) or 0)
            if stale_notional <= 0 and pos_open and qty > 0 and entry > 0:
                stale_notional = qty * entry
            posval_str = (
                f"{Q.GRAY_4}${stale_notional:>10,.2f}{Q.RESET}"
                if stale_notional > 0 else
                f"{Q.GRAY_3}${0:>10,.2f}{Q.RESET}"
            )
            cells = [
                f"{Q.GRAY_3}{idx}{Q.RESET}",
                f"{Q.GRAY_4}{w.symbol}{Q.RESET}",
                f"{Q.GRAY_3}c{w.client_id}{Q.RESET}",
                f"{Q.GRAY_4}{state_label}{Q.RESET}",
                pos_str,
                posval_str,
                f"{Q.GRAY_3}stale{Q.RESET}",
                f"{Q.GRAY_4}${pnl:+8.2f}{Q.RESET}",
                f"{Q.GRAY_3}    --   {Q.RESET}",
                f"{Q.GRAY_4}{trades}{Q.RESET}",
                f"{Q.GRAY_4}{wins}/{losses}{Q.RESET}",
                f"{Q.GRAY_3}${comm:.2f}{Q.RESET}",
                f"{Q.GRAY_4}${trig:.2f}{Q.RESET}",
            ]
        else:
            state_c = _STATE_COLORS.get(state, Q.GRAY_3)
            if pos_open and entry > 0:
                pos_str = (
                    # SHORT INVERSION (P11): label SHORT (red) for a short-only book.
                    f"{Q.RED}SHORT{Q.RESET} {Q.WHITE}{qty}{Q.RESET} @ "
                    f"{Q.WHITE}${entry:.2f}{Q.RESET}"
                )
            else:
                pos_str = f"{Q.GRAY_4}FLAT{Q.RESET}"

            last = live.get('last', 0) or 0
            if pos_open and last > 0 and entry > 0:
                # Pure mark-to-market. Entry commission is NOT subtracted
                # here — it's accounted for in the REAL$ column (which
                # reads `pnl` from the state file, already net of all
                # closed-cycle commissions; mid-cycle adjustment for the
                # entry-side fee shows up in the live dashboard's
                # per-ticker drill-down via state.realized_pnl).
                # SHORT INVERSION (P11): a short profits as price FALLS →
                # (entry − last). The dashboard computes its own unrealized PnL
                # here (it does NOT read get_status), so it must be inverted or
                # it shows red-when-winning for every short.
                unreal = (entry - last) * qty
            else:
                unreal = 0
            last_str = f"${last:.2f}" if last > 0 else "--"
            unr_c = Q.GREEN if unreal >= 0 else Q.RED
            pnl_c = Q.GREEN if pnl >= 0 else Q.RED

            # POSVAL for live: position_notional (qty × LTP for the
            # already-filled position) ONLY. A working-but-unfilled entry
            # (pending_notional) is NOT counted — exposure reflects filled
            # position only, matching the risk-gate's view. When flat →
            # renders as $0 in dim, even if a pending entry is resting.
            live_notional = float(live.get('position_notional', 0) or 0)
            posval_str = (
                f"{Q.WHITE}${live_notional:>10,.2f}{Q.RESET}"
                if live_notional > 0 else
                f"{Q.GRAY_4}${0:>10,.2f}{Q.RESET}"
            )

            cells = [
                f"{Q.GRAY_3}{idx}{Q.RESET}",
                f"{Q.WHITE}{w.symbol}{Q.RESET}",
                f"{Q.GRAY_4}c{w.client_id}{Q.RESET}",
                f"{state_c}{state}{Q.RESET}",
                pos_str,
                posval_str,
                f"{Q.GRAY_5}{last_str}{Q.RESET}",
                f"{pnl_c}${pnl:+8.2f}{Q.RESET}",
                f"{unr_c}${unreal:+8.2f}{Q.RESET}",
                f"{Q.WHITE}{trades}{Q.RESET}",
                f"{Q.GREEN}{wins}{Q.RESET}/{Q.RED}{losses}{Q.RESET}",
                f"{Q.GRAY_4}${comm:.2f}{Q.RESET}",
                f"{Q.YELLOW}${trig:.2f}{Q.RESET}",
            ]
        return "".join(_row(cell, w_) for cell, (_, w_) in zip(cells, COL_W))

    if not watchers:
        rows.append(_line(f"  {Q.YELLOW}No symbols found.{Q.RESET} "
                           f"{Q.GRAY_3}Looking for .gt_state_*_*.json files modified within the last "
                           f"{SymbolWatcher.OFFLINE_WINDOW_S//3600}h.{Q.RESET}"))
        rows.append(_line(f"  {Q.GRAY_3}Start a symbol: {Q.RESET}{Q.GRAY_4}python run_live.py NVDA --trigger 230 --port 7497 --client-id 1{Q.RESET}"))
    else:
        # Continuous numbering across live → offline so drill-down keys
        # [1-9] still line up sensibly. The whole `watchers` list is
        # already ordered live-first by discover_watchers().
        idx = 0
        for w in live_watchers:
            idx += 1
            rows.append(_line(_render_row(w, idx, offline=False)))

        if offline_watchers:
            # Sub-divider before the offline zone so the visual break is
            # clear even on a tiny terminal where colour might not render.
            rows.append(_line(
                f"{Q.GRAY_2}{Glyph.HALF} OFFLINE {Q.RESET}"
                f"{Q.GRAY_3}(killed processes — state + orders preserved from disk, "
                f"feed/LAST are stale){Q.RESET}"
            ))
            for w in offline_watchers:
                idx += 1
                rows.append(_line(_render_row(w, idx, offline=True)))

        # ─── Active-exposure summary ─────────────────────────────────────
        # Sum of position_notional across LIVE watchers ONLY. Exposure
        # counts FILLED positions only — a working-but-unfilled entry does
        # NOT contribute until it fills, matching the portfolio risk gate,
        # so this number and the gate's "combined exposure" stay aligned.
        #
        # When the operator kills a ticker terminal, that ticker's
        # contribution drops out of this total immediately — for "what
        # am I currently actively running" the dead bot doesn't count.
        # Shares may still be at the broker (reflected in the
        # account-wide `exp` pill in the header) but the bot isn't
        # managing them.
        #
        # Header pill `exp Y%` = LIVE + OFFLINE  (account exposure).
        # This summary    = LIVE only       (actively-managed exposure).
        # Both are useful and intentionally distinct.
        active_notional = sum(
            float(w.poll_live().get('position_notional', 0) or 0)
            for w in live_watchers
        )
        # "Contributing" count = bots with a FILLED position open. A
        # pending-but-unfilled entry does not count as exposure.
        active_long_count = sum(
            1 for w in live_watchers
            if (w.poll_live().get('position_notional', 0) or 0) > 0
        )
        # Two compact pills: total $ and count of tickers contributing.
        # The count is useful because $0 could mean either "no one is in
        # position" or "I forgot to start anything" — the count disambiguates.
        rows.append(_border('╠', '╣'))
        summary = (
            f" {Q.BOLD}{Q.GRAY_5}TOTAL ACTIVE EXPOSURE{Q.RESET}  "
            f"{Q.WHITE}{Q.BOLD}${active_notional:>12,.2f}{Q.RESET}  "
            f"{Q.GRAY_3}from{Q.RESET} "
            f"{Q.WHITE}{active_long_count}{Q.RESET} "
            f"{Q.GRAY_3}of {len(live_watchers)} live tickers"
            f"{Q.RESET}  {Q.GRAY_2}(killed terminals excluded — "
            f"see header `exp` pill for account-wide number){Q.RESET}"
        )
        rows.append(_line(summary))

        # ─── Short-availability alerts ────────────────────────────────────
        # At-a-glance borrow/availability across ALL live symbols so an
        # operator sees a hard-to-borrow or not-shortable name without
        # drilling in. Each flagged symbol shows: NOT-SHORTABLE (red) or a
        # borrow-rate badge + HTB when elevated. Symbols that are freely
        # shortable at a trivial fee contribute nothing (keeps it quiet).
        # Full detail (share count, margin) lives in the [1-9] drill-down.
        short_flags: list[str] = []
        for w in live_watchers:
            req = w.poll_live().get('short_requirement') or {}
            if not req:
                continue
            avail = req.get('shortable_available')
            htb = req.get('hard_to_borrow')
            try:
                rate = float(req.get('carry_rate_annual') or 0)
            except (TypeError, ValueError):
                rate = 0.0
            live_rate = req.get('borrow_rate_live')
            if avail is False:
                short_flags.append(f"{Q.RED}{w.symbol} NOT-SHORTABLE{Q.RESET}")
            elif htb or (live_rate and rate > 0.01):
                # bps/yr, badge HTB in red. Only show when it's the live rate
                # (an offline placeholder rate isn't worth alerting on).
                bps = f"{rate * 10000:.0f}bps" if live_rate else "HTB"
                short_flags.append(f"{Q.YELLOW}{w.symbol} {bps}{Q.RESET}")
        if short_flags:
            rows.append(_line(
                f" {Q.BOLD}{Q.GRAY_5}SHORT{Q.RESET}  " + "  ".join(short_flags)
            ))

    # ─── Combined orders ─
    rows.append(_border('╠', '╣'))
    rows.append(_line(
        f"{Q.BOLD}{Q.GRAY_5}COMBINED ORDERS{Q.RESET} "
        f"{Q.GRAY_2}(last 8 across all symbols, newest at bottom){Q.RESET}"
    ))

    # Process-level events that historically also landed in the orders CSV
    # but aren't real order activity. Filter them out at render time so the
    # COMBINED ORDERS section only shows BUY/SELL placements + fills.
    _PROCESS_EVENTS = {
        "STARTUP_REFUSED_NAKED", "STARTUP_REFUSED_CONFLICT",
        "TRIPWIRE_LOST_PENDING", "POSITION_MISMATCH",
        "SNAPSHOT", "GIVE_UP", "MODIFIED", "MODIFY_FAILED",
    }

    combined: list[tuple[str, dict]] = []
    for w in watchers:
        for o in w.tail_orders(n=20):
            if (o.get('event') or '').strip().upper() in _PROCESS_EVENTS:
                continue
            combined.append((w.symbol, o))
    # Primary: ISO timestamp (string-sortable). Tiebreak: symbol alphabetical
    # so two orders timestamped to the same microsecond render in a stable,
    # predictable order (AAPL before NVDA before TSLA). Without this tiebreak
    # the sort was non-deterministic — same data could shuffle row order
    # frame-to-frame, which is jarring on a live dashboard.
    combined.sort(key=lambda x: (x[1].get('ts', ''), x[0]))
    if not combined:
        rows.append(_line(f"  {Q.GRAY_2}no order events yet{Q.RESET}"))
    else:
        for sym, o in combined[-8:]:
            ts_raw = o.get('ts', '')
            ts_short = ts_raw.split('T')[1].split('.')[0] if 'T' in ts_raw else (ts_raw[-8:] if len(ts_raw) >= 8 else '--:--:--')
            event = o.get('event', '?')
            side = o.get('side', '')
            # Coerce qty for display — CSV may have "10.0" from older runs.
            qty_raw = o.get('qty', '?')
            try:
                qty = str(int(float(qty_raw))) if qty_raw not in ('', '?') else '?'
            except (TypeError, ValueError):
                qty = str(qty_raw)
            sig_px = o.get('signal_price', '') or '--'
            fill_px = o.get('fill_price', '') or '--'
            slip = o.get('slippage', '') or ''
            side_c = Q.GREEN if side == 'BUY' else Q.RED if side == 'SELL' else Q.GRAY_4
            event_c = (Q.GREEN if event == 'FILLED' else
                       Q.YELLOW if event == 'SUBMITTED' else
                       Q.RED if event in ('REJECTED', 'CANCELLED') else
                       Q.GRAY_4)
            # Show fill_price when FILLED, else signal_price
            shown_px = fill_px if event == 'FILLED' else sig_px
            try:
                shown_px = f"${float(shown_px):.2f}"
            except (TypeError, ValueError):
                shown_px = f"${shown_px}" if shown_px not in ('--', '') else '--'

            slip_str = ''
            if slip and event == 'FILLED':
                try:
                    s = float(slip)
                    slip_c = Q.RED if abs(s) > 0.05 else Q.YELLOW if abs(s) > 0.01 else Q.GRAY_4
                    slip_str = f"  {Q.GRAY_3}slip{Q.RESET} {slip_c}{s:+.4f}{Q.RESET}"
                except ValueError:
                    pass

            row = (
                f"  {Q.GRAY_2}{ts_short}{Q.RESET}  "
                f"{Q.WHITE}{Q.BOLD}{sym:<5}{Q.RESET}  "
                f"{side_c}{Q.BOLD}{side:<4}{Q.RESET} {Q.WHITE}{qty:>3}{Q.RESET} @ "
                f"{Q.GRAY_5}{shown_px:<8}{Q.RESET}  "
                f"{event_c}{event:<9}{Q.RESET}{slip_str}"
            )
            rows.append(_line(row))

    # ─── Combined alerts ─
    rows.append(_border('╠', '╣'))
    rows.append(_line(
        f"{Q.BOLD}{Q.GRAY_5}COMBINED ALERTS{Q.RESET} "
        f"{Q.GRAY_2}(last 5 across all symbols){Q.RESET}"
    ))
    alerts = tail_alerts(n=5)
    sev_dot = {
        'CRITICAL': (Q.PURPLE, 'CRIT'),
        'HIGH':     (Q.ORANGE, 'HIGH'),
        'MEDIUM':   (Q.YELLOW, 'MED '),
        'LOW':      (Q.CYAN,   'LOW '),
    }
    if not alerts:
        rows.append(_line(f"  {Q.GRAY_2}no alerts{Q.RESET}"))
    else:
        for a in reversed(alerts):
            sev = a.get('severity', 'LOW')
            color, badge = sev_dot.get(sev, (Q.GRAY_3, sev[:4]))
            ts = a.get('timestamp', '')
            if 'T' in ts:
                ts_short = ts.split('T')[1].split('.')[0]
            else:
                ts_short = ts[-8:] if len(ts) >= 8 else '--:--:--'
            code = a.get('code', '?')
            msg = a.get('message', '')
            # Some alerts have a 'context.ticker' field — surface it
            ctx = a.get('context', {}) or {}
            ticker = ctx.get('ticker', '')
            ticker_str = f" {Q.WHITE}{ticker}{Q.RESET}" if ticker else ''
            row = (
                f"  {color}{Glyph.DOT}{badge}{Q.RESET} {Q.GRAY_2}{ts_short}{Q.RESET}{ticker_str}  "
                f"{Q.GRAY_5}{code}{Q.RESET}  {Q.GRAY_4}{msg}{Q.RESET}"
            )
            rows.append(_line(row))

    # ─── Footer / shortcuts (+ error diagnostics) ─
    rows.append(_border('╠', '╣'))
    # Surface watcher errors. A non-zero count means a state or live JSON
    # file is corrupt / partially written / missing — used to be silently
    # swallowed, leaving the operator wondering why a row went stale.
    err_total = sum((w._state_errors + w._live_errors) for w in watchers)
    if err_total > 0:
        err_lines = []
        for w in watchers:
            if w._state_errors or w._live_errors:
                err_lines.append(
                    f"{w.symbol}:{w._state_errors}s/{w._live_errors}l({w._last_error})"
                )
        rows.append(_line(
            f" {Q.ORANGE}{Glyph.DOT} parse errors{Q.RESET}  "
            f"{Q.GRAY_4}{' '.join(err_lines)}{Q.RESET}"
        ))
    footer = (
        f" {Q.GRAY_3}Keys:{Q.RESET}  "
        f"{Q.WHITE}{Q.BOLD}[1-9]{Q.RESET} {Q.GRAY_4}drill into symbol{Q.RESET}     "
        f"{Q.WHITE}{Q.BOLD}[0]{Q.RESET} {Q.GRAY_4}back to summary{Q.RESET}     "
        f"{Q.WHITE}{Q.BOLD}[q]{Q.RESET} {Q.GRAY_4}quit{Q.RESET}     "
        f"{Q.GRAY_2}offline={SymbolWatcher.STALENESS_S}s · "
        f"window={SymbolWatcher.OFFLINE_WINDOW_S//3600}h{Q.RESET}"
    )
    rows.append(_line(footer))
    rows.append(_border('╚', '╝'))

    return '\n'.join(rows)


# ═══════════════════════════════════════════════════════════════════════════
# Drill-down view — reconstruct a State and reuse build_frame()
# ═══════════════════════════════════════════════════════════════════════════

class _MockEngineForDrilldown:
    """Synthesizes the Engine interface State expects from polled files."""

    __slots__ = ('_watcher', 'config', 'gateway', '_paused', 'registry', '_order_history', '_feed')

    def __init__(self, watcher: SymbolWatcher):
        self._watcher = watcher
        live = watcher.poll_live()
        st = watcher.poll_state()
        self.config = SimpleNamespace(
            ticker=watcher.symbol,
            trigger_price=live.get('trigger_price', 0) or 0,
            stop_loss_pct=live.get('stop_loss_pct', 0.01) or 0.01,
            quantity=st.get('quantity', 1) or 1,
        )
        # Real equity now flows through the live snapshot (run_live.py
        # writes it from risk._get_cached_equity). Fall back to 0 if the
        # writer pre-dates this field — a `0` shows up as "--" on the
        # dashboard, which is the correct "unknown" signal rather than a
        # fake $100k that would lie to the operator.
        snap_equity = live.get('equity', 0) or 0
        self.gateway = SimpleNamespace(
            paper=False,
            connected=live.get('connected', False),
            _last_heartbeat=datetime.now() - timedelta(seconds=live.get('heartbeat_age', 0) or 0),
            get_equity=lambda: snap_equity,
        )
        self._paused = live.get('paused', False)
        # State.sync_engine looks for these — make empty stubs so it skips.
        self.registry = SimpleNamespace(_orders={})
        self._order_history: list = []
        # production_feed stub — exposes get_latency_stats so State picks up the live data
        self._feed = SimpleNamespace(get_latency_stats=lambda: live.get('latency', {}))

    def get_status(self) -> dict:
        st = self._watcher.poll_state()
        live = self._watcher.poll_live()
        # Real equity from live snapshot (run_live.py publishes it). 0 means
        # unknown — render layer shows it as "--".
        equity = live.get('equity', 0) or 0
        return {
            'state': st.get('state', 'IDLE') or 'IDLE',
            'cycle_id': st.get('cycle_id', ''),
            'running': True,
            'position_open': st.get('position_open', False),
            'entry_price': st.get('entry_price'),
            'highest_price': st.get('highest_price'),
            'stop_loss': st.get('stop_loss'),
            'previous_breakout_level': st.get('previous_breakout_level'),
            'trigger_price': live.get('trigger_price', 0) or 0,
            'quantity': st.get('quantity', 0),
            'trades_today': st.get('trades_today', 0),
            'wins': st.get('wins', 0),
            'losses': st.get('losses', 0),
            'pnl': st.get('pnl', 0),
            'total_commission': st.get('total_commission', 0),
            'pending_side': None,
            'ticks_dropped': 0,
            'tick_queue_depth': 0,
            'avg_slippage': 0,
            'worst_slippage': 0,
            'fill_count': 0,
            'risk': {
                'daily_pnl': st.get('pnl', 0),
                'trades_today': st.get('trades_today', 0),
                'consec_losses': 0,
                'equity': equity,
            },
            'risk_limits': {
                'max_consec_losses': 300,
                'max_trades_per_day': 50,
                'daily_loss_limit_pct': -0.90,
            },
        }


def populate_drilldown_state(watcher: SymbolWatcher) -> State:
    """Build a fully-populated State from polled file data so build_frame works."""
    mock_engine = _MockEngineForDrilldown(watcher)
    state = State(engine=mock_engine)
    state.symbol = watcher.symbol
    # Carry client_id through so the drill-down header + SYSTEM panel
    # display it (matches the live dashboard from run_live.py).
    try:
        state.client_id = int(watcher.client_id)
    except (TypeError, ValueError):
        state.client_id = 0
    state.connected = mock_engine.gateway.connected

    live = watcher.poll_live()
    st = watcher.poll_state()

    # ─── Feed snapshot ─
    state.feed.last = live.get('last', 0) or 0
    state.feed.bid = live.get('bid', 0) or 0
    state.feed.ask = live.get('ask', 0) or 0
    state.feed.bid_size = int(live.get('bid_size', 0) or 0)
    state.feed.ask_size = int(live.get('ask_size', 0) or 0)
    state.feed.volume = int(live.get('volume', 0) or 0)
    state.feed.open_px = live.get('open', 0) or 0
    state.feed.high = live.get('high', 0) or 0
    state.feed.low = live.get('low', 0) or 0
    state.feed.rate = live.get('rate', 0) or 0

    # ─── Microstructure: rebuild VWAP, rates, tape from live snapshot ─
    vwap = live.get('vwap', 0) or 0
    if vwap > 0:
        # We can't recover the actual price*volume products, but we can
        # set the running sums so the property returns the right vwap.
        # Use a synthetic volume of 1 — the ratio is what matters.
        state.micro._vwap_num = vwap
        state.micro._vwap_den = 1
    # Rates — synthesize timestamps in the deque to produce the same rate
    # at render time. Properties compute len/(last-first), so N samples
    # over 1 second window yields rate=N.
    now = time.monotonic()
    for source_rate, dq in (
        (live.get('tick_rate', 0) or 0, state.micro._tick_times),
        (live.get('bbo_rate', 0) or 0, state.micro._bbo_times),
        (live.get('trade_rate', 0) or 0, state.micro._trade_times),
    ):
        if source_rate > 0:
            spacing = 1.0 / source_rate
            n = min(int(source_rate), 100)  # cap to avoid huge deques
            for i in range(n):
                dq.append(now - i * spacing)
    # Buy/sell aggression
    bp = live.get('buy_pct', 0) or 0
    sp = live.get('sell_pct', 0) or 0
    np_pct = max(0, 100 - bp - sp)
    state.micro._buy_aggr = int(bp * 10)
    state.micro._sell_aggr = int(sp * 10)
    state.micro._neutral = int(np_pct * 10)
    # Tape entries
    for t in live.get('tape', []):
        state.micro._tape.append(TapeEntry(
            ts=t.get('ts', ''),
            price=t.get('price', 0) or 0,
            size=t.get('size', 0) or 0,
            exchange=t.get('exchange', '') or '',
            conditions=t.get('conditions', '') or '',
            direction=int(t.get('direction', 0) or 0),
        ))

    # ─── Latency stats ─
    state._latency = live.get('latency', {}) or {}

    # ─── Position context for ladder ─
    state.breakout_level = st.get('previous_breakout_level', 0) or 0
    state.highest_price = st.get('highest_price', 0) or 0

    # ─── Heartbeat / connection ─
    state.heartbeat_age_s = live.get('heartbeat_age', 0) or 0
    state.reconnect_count = 0

    # ─── Session ─
    state.in_session = session_is_open()
    state.next_session_change = seconds_until_session_open() if not state.in_session else 0
    state.paused = live.get('paused', False)

    # ─── Short-selling requirement (availability / borrow / margin) ─
    # Carry the snapshot's live-from-IBKR short data into _last_status so
    # build_frame's SHORT REQ panel renders in the drill-down exactly as in
    # the per-symbol dashboard (avail shares, borrow fee, init/maint margin).
    state._last_status = {
        'short_requirement': live.get('short_requirement'),
        'short_margin_whatif': live.get('short_margin_whatif'),
    }

    # ─── Risk + P&L from state file ─
    state._risk_status = mock_engine.get_status().get('risk', {})
    state._risk_limits = mock_engine.get_status().get('risk_limits', {})
    state.total_pnl = st.get('pnl', 0) or 0
    state.comm = st.get('total_commission', 0) or 0
    state.wins = st.get('wins', 0)
    state.losses = st.get('losses', 0)
    state.trades = st.get('trades_today', 0)
    # Unrealized = pure mark-to-market on the open position. Entry-side
    # commission is NOT subtracted here — it lives in `realized_pnl`
    # (see below) since the broker has already debited it. Matches the
    # accounting split used by the per-ticker live dashboard.
    if st.get('position_open') and state.feed.last > 0 and st.get('entry_price'):
        qty = st.get('quantity', 0) or 0
        # SHORT INVERSION (P11): short PnL = (entry − last) × qty (profit as
        # price falls). This drill-down computes its own unreal, not get_status.
        state.unrealized_pnl = (st['entry_price'] - state.feed.last) * qty
        # Deduct the buy-side commission from realized so the drill-down
        # view matches what the per-ticker dashboard renders. Uses the real
        # IBKR Tiered formula via calc_ibkr_commission — the old
        # `qty × COMMISSION_PER_SHARE` was billing 50-100× too much.
        state.realized_pnl = (st.get('pnl', 0) or 0) - calc_ibkr_commission(qty, st['entry_price'], "BUY")
        state.total_pnl = state.realized_pnl + state.unrealized_pnl
    else:
        state.realized_pnl = st.get('pnl', 0) or 0
        state.total_pnl = state.realized_pnl

    # ─── Orders from audit CSV ─
    # Drop process-level diagnostics that historically also landed in the
    # orders CSV — same filter as the COMBINED ORDERS section above.
    _DRILL_SKIP_EVENTS = {
        "STARTUP_REFUSED_NAKED", "STARTUP_REFUSED_CONFLICT",
        "TRIPWIRE_LOST_PENDING", "POSITION_MISMATCH",
        "SNAPSHOT", "GIVE_UP", "MODIFIED", "MODIFY_FAILED",
    }
    for o in watcher.tail_orders(n=20):
        if (o.get('event') or '').strip().upper() in _DRILL_SKIP_EVENTS:
            continue
        side = o.get('side', '?')
        event = o.get('event', '?')
        fill_px_s = o.get('fill_price', '')
        signal_px_s = o.get('signal_price', '')
        try:
            fill_px = float(fill_px_s) if fill_px_s else 0
        except ValueError:
            fill_px = 0
        try:
            signal_px = float(signal_px_s) if signal_px_s else 0
        except ValueError:
            signal_px = 0
        # SUBMITTED rows show signal price; FILLED rows show fill price.
        px = fill_px if event == 'FILLED' and fill_px > 0 else signal_px
        ts_raw = o.get('ts', '')
        if 'T' in ts_raw:
            ts_short = ts_raw.split('T')[1].split('.')[0]
        else:
            ts_short = ts_raw[-8:] if len(ts_raw) >= 8 else '--:--:--'
        # Accept both "10" and "10.0" — older CSV rows had the float form.
        try:
            qty_int = int(float(o.get('qty', 0) or 0))
        except (TypeError, ValueError):
            qty_int = 0
        state.orders.append({
            'side': side,
            'qty': qty_int,
            'px': px,
            'status': event,
            'ts': ts_short,
        })

    # ─── Alerts — filter to this symbol's context if present ─
    from src.infra.alerts import AlertManager, Alert, AlertSeverity
    alert_mgr = AlertManager()
    for a in tail_alerts(n=20):
        try:
            sev = AlertSeverity(a.get('severity', 'LOW'))
        except ValueError:
            sev = AlertSeverity.LOW
        ts_raw = a.get('timestamp', '')
        try:
            ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now()
        except ValueError:
            ts = datetime.now()
        ctx = a.get('context', {}) or {}
        # If the alert names a specific ticker that isn't ours, skip it
        ticker = ctx.get('ticker')
        if ticker and ticker != watcher.symbol:
            continue
        alert_mgr._history.append(Alert(
            code=a.get('code', '?'),
            severity=sev,
            message=a.get('message', ''),
            timestamp=ts,
            context=ctx,
        ))
    state.alerts.attach(alert_mgr)

    return state


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle shortcuts: [s] start everything, [k] kill everything
# ═══════════════════════════════════════════════════════════════════════════
# Replaces the operator workflow of opening one shell per ticker and pasting
# the full run_live.py command. With state + live files already on disk from
# any prior session, we can reconstruct the exact launch CLI by reading the
# port / paper / offset hints out of `.gt_live_<SYM>_<CID>.json`, then spawn
# the bot detached so it survives the dashboard's lifetime.
#
# What we DELIBERATELY don't reconstruct: --trigger and --qty. Those come
# from the bot's own recovery flow (reading `.gt_state_<SYM>_<CID>.json`)
# — that's the whole point of "recovery launch". The bot picks up
# entry_price, position_open, stop_loss, trigger from saved state and
# resumes exactly where it left off.

# Per-symbol log destination for bot stdout. The bot's own audit + alert
# infrastructure handles persistent logging; we just need somewhere to
# direct stdout so the dashboard's terminal doesn't get polluted.
_BOT_LOG_DIR = Path("data/Logs/run_live")


def _spawn_bot_from_watcher(watcher: "SymbolWatcher") -> Optional[dict]:
    """Re-launch a bot for `(symbol, client_id)` using live-snapshot hints.

    Returns the spawn record on success, None on skip (already running /
    missing port hint / other expected failure). Never raises — callers
    are non-interactive UI code that needs to report instead of crash.
    """
    if watcher.is_active:
        return {"key": f"{watcher.symbol}_{watcher.client_id}",
                "result": "skip", "reason": "already running"}

    live = watcher.poll_live()
    port = live.get("ibkr_port")
    if not port:
        # Older live snapshots didn't carry the launch hints. Fall back
        # to the TWS paper default (7497) for safety — better to require
        # the operator to fix port via env than refuse to start.
        port = int(os.environ.get("GT_FALLBACK_PORT", "7497"))

    paper = bool(live.get("paper_trading", False))
    offset = live.get("sl_limit_offset")

    argv = [
        sys.executable, "run_live.py", watcher.symbol,
        "--port", str(int(port)),
        "--client-id", str(int(watcher.client_id)),
        "--uvloop",
    ]
    # Only pass --offset-fixed if we recorded one — leaves the bot's
    # default scaling logic intact otherwise. The bot's recovery uses
    # the saved state for trigger + qty so we don't pass those.
    if offset is not None and float(offset) > 0:
        argv += ["--offset-fixed", f"{float(offset):.4f}"]

    env = {**os.environ, "GT_PAPER": "true" if paper else "false",
           "PYTHONUNBUFFERED": "1"}

    _BOT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _BOT_LOG_DIR / f"{watcher.symbol}_{watcher.client_id}.out"
    try:
        # `start_new_session=True` detaches into its own process group
        # so a Ctrl+C in the dashboard terminal doesn't take the bots
        # with it. `tail -f` the log file to debug startup.
        with open(log_path, "ab") as logfile:
            proc = subprocess.Popen(
                argv, env=env, stdout=logfile, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return {"key": f"{watcher.symbol}_{watcher.client_id}",
                "result": "started", "pid": proc.pid,
                "argv": " ".join(argv), "paper": paper,
                "log": str(log_path)}
    except Exception as e:
        return {"key": f"{watcher.symbol}_{watcher.client_id}",
                "result": "error",
                "reason": f"{type(e).__name__}: {e}"}


def _start_all(watchers: list) -> dict:
    """Launch every dormant bot we have state for. Returns a summary
    dict the dashboard banner consumes."""
    started, skipped, errors = [], [], []
    for w in watchers:
        rec = _spawn_bot_from_watcher(w)
        if rec is None:
            continue
        if rec["result"] == "started":
            started.append(rec)
        elif rec["result"] == "skip":
            skipped.append(rec)
        else:
            errors.append(rec)
    return {"started": started, "skipped": skipped, "errors": errors}


def _stop_all() -> dict:
    """SIGTERM every `run_live.py` process belonging to the current user.

    Restricted to the current uid to avoid killing other users' bots on
    a shared box. Uses `pgrep -f` so it matches by command line rather
    than just process name (run_live.py is a script, not a binary)."""
    try:
        out = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", r"python.*run_live\.py"],
            capture_output=True, text=True, check=False,
        )
        pids = [int(p) for p in out.stdout.split() if p.strip()]
    except Exception as e:
        return {"stopped": [], "errors": [f"pgrep failed: {e}"]}

    stopped, errors = [], []
    for pid in pids:
        # Don't SIGTERM ourselves — pgrep -f matches dashboard_agg.py too
        # if it imports run_live by name (it doesn't, but be paranoid).
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except ProcessLookupError:
            pass  # already exited between pgrep and kill
        except Exception as e:
            errors.append(f"pid {pid}: {type(e).__name__}: {e}")
    return {"stopped": stopped, "errors": errors}


# ═══════════════════════════════════════════════════════════════════════════
# Main loop with non-blocking keyboard input
# ═══════════════════════════════════════════════════════════════════════════

def _read_key_nonblocking() -> Optional[str]:
    """Return one keypress if available, else None. Requires cbreak mode."""
    if select.select([sys.stdin], [], [], 0)[0]:
        try:
            return sys.stdin.read(1)
        except Exception:
            return None
    return None


async def main():
    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        # Non-TTY (e.g. piped) — fall back to no keyboard input
        old_settings = None

    if old_settings is not None:
        # cbreak preserves Ctrl+C signal handling but disables line buffering
        # so single keystrokes are readable without Enter.
        tty.setcbreak(fd)

    try:
        sys.stdout.write(HOME + CLEAR_SCREEN)
        sys.stdout.flush()

        current_view = "summary"  # "summary" or "drill:<index>"
        last_render = 0.0
        render_interval = 0.2  # 5Hz refresh

        # Transient one-line banner printed above the next frame after
        # a lifecycle action. Lives for ~5s then clears so the dashboard
        # returns to normal layout. (banner_text, expires_at) or None.
        banner: Optional[tuple[str, float]] = None
        # When the banner expires, current_view is set so the next render
        # forces a CLEAR_SCREEN to remove the extra banner line cleanly.
        last_banner_cleared = True

        print(f"{Q.CYAN}GT Multi-Symbol Aggregator{Q.RESET}")
        print(f"{Q.GRAY_3}[1-9] drill  [0] summary  "
              f"{Q.GREEN}[s]{Q.GRAY_3} start all  "
              f"{Q.RED}[k]{Q.GRAY_3} kill all  "
              f"[q] quit{Q.RESET}")
        time.sleep(0.5)

        while True:
            key = _read_key_nonblocking() if old_settings is not None else None
            if key is not None:
                if key == 'q' or key == '\x03':  # q or Ctrl+C
                    break
                elif key == '0':
                    current_view = "summary"
                    # Clear the screen on view switch to avoid leftover chars
                    sys.stdout.write(HOME + CLEAR_SCREEN)
                    sys.stdout.flush()
                elif key.isdigit():
                    current_view = f"drill:{int(key) - 1}"
                    sys.stdout.write(HOME + CLEAR_SCREEN)
                    sys.stdout.flush()
                elif key in ('s', 'S'):
                    # Start every dormant bot we have a state file for.
                    # Reads launch hints from each watcher's live snapshot
                    # and spawns `run_live.py` detached. Bots' recovery
                    # flow picks up trigger/qty/position from saved state.
                    watchers_now = discover_watchers()
                    summary = _start_all(watchers_now)
                    n_started = len(summary["started"])
                    n_skip = len(summary["skipped"])
                    n_err = len(summary["errors"])
                    parts = []
                    if n_started:
                        parts.append(f"{Q.GREEN}started {n_started}{Q.RESET}: "
                                     + ", ".join(r["key"] for r in summary["started"]))
                    if n_skip:
                        parts.append(f"{Q.GRAY_3}skipped {n_skip}{Q.RESET} (already running)")
                    if n_err:
                        parts.append(f"{Q.RED}errors {n_err}{Q.RESET}: "
                                     + "; ".join(r["reason"] for r in summary["errors"][:3]))
                    if not parts:
                        parts.append(f"{Q.GRAY_3}no state files found — nothing to start{Q.RESET}")
                    banner = (f"  {Q.BOLD}[start]{Q.RESET}  " + "  ".join(parts), time.monotonic() + 5.0)
                    last_banner_cleared = False
                    sys.stdout.write(HOME + CLEAR_SCREEN)
                    sys.stdout.flush()
                elif key in ('k', 'K'):
                    # Graceful SIGTERM to every run_live.py in our session.
                    summary = _stop_all()
                    n_stopped = len(summary["stopped"])
                    n_err = len(summary["errors"])
                    parts = []
                    if n_stopped:
                        parts.append(f"{Q.RED}stopped {n_stopped}{Q.RESET} pid{'s' if n_stopped != 1 else ''}: "
                                     + ", ".join(str(p) for p in summary["stopped"][:6])
                                     + ("…" if n_stopped > 6 else ""))
                    else:
                        parts.append(f"{Q.GRAY_3}no run_live.py processes found{Q.RESET}")
                    if n_err:
                        parts.append(f"{Q.RED}errors{Q.RESET} {n_err}: " + "; ".join(summary["errors"][:3]))
                    banner = (f"  {Q.BOLD}[kill]{Q.RESET}   " + "  ".join(parts), time.monotonic() + 5.0)
                    last_banner_cleared = False
                    sys.stdout.write(HOME + CLEAR_SCREEN)
                    sys.stdout.flush()

            now = time.monotonic()
            if now - last_render >= render_interval:
                watchers = discover_watchers()

                if current_view == "summary":
                    frame = render_summary(watchers)
                else:
                    idx = int(current_view.split(":")[1])
                    if 0 <= idx < len(watchers):
                        state = populate_drilldown_state(watchers[idx])
                        # Override the engine-shortcut footer with the
                        # aggregator's nav keys — Ctrl+\ etc. don't work
                        # here (this process doesn't own the engine).
                        drill_footer = (
                            f"{Q.GRAY_3}{watchers[idx].symbol} drill-down{Q.RESET}  "
                            f"{Q.WHITE}{Q.BOLD}[0]{Q.RESET} {Q.GRAY_4}back to summary{Q.RESET}     "
                            f"{Q.WHITE}{Q.BOLD}[1-9]{Q.RESET} {Q.GRAY_4}switch symbol{Q.RESET}     "
                            f"{Q.WHITE}{Q.BOLD}[q]{Q.RESET} {Q.GRAY_4}quit aggregator{Q.RESET}     "
                            f"{Q.GRAY_2}(engine controls live in the {watchers[idx].symbol} run_live.py terminal){Q.RESET}"
                        )
                        frame = build_frame(state, footer_override=drill_footer)
                    else:
                        # Selected symbol disappeared (process stopped); fall back.
                        current_view = "summary"
                        frame = render_summary(watchers)

                # Transient lifecycle banner: rendered one line above
                # the frame for ~5s after [s] or [k]. When it expires
                # we issue a CLEAR_SCREEN so the dashboard returns to
                # its normal layout without a lingering banner row.
                if banner is not None:
                    text, expires_at = banner
                    if now >= expires_at:
                        banner = None
                        if not last_banner_cleared:
                            sys.stdout.write(HOME + CLEAR_SCREEN)
                            last_banner_cleared = True
                        sys.stdout.write(HOME + frame + '\n')
                    else:
                        sys.stdout.write(HOME + text + '\n' + frame + '\n')
                else:
                    sys.stdout.write(HOME + frame + '\n')
                sys.stdout.flush()
                last_render = now

            await asyncio.sleep(0.05)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if old_settings is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
        print(f"\n{Q.GRAY_3}[aggregator] stopped{Q.RESET}")


def _parse_cli_and_persist_limits() -> None:
    """Pre-main CLI parse for `--exposure` / `--risk` overrides.

    When passed, persist into `.gt_portfolio_limits.json` so every
    running bot picks the new caps up within ~1s (each bot's RiskCheck
    re-reads the file via PortfolioLimitsReader's 1s-TTL cache). Then
    fall through to the normal dashboard render — the file persists,
    so closing + reopening the dashboard does NOT clear the override.

    Pass 0 or a negative value to CLEAR an override:
        python dashboard_agg.py --exposure 0       # remove exposure cap
        python dashboard_agg.py --risk 0           # remove loss cap

    No flags = no write. Just shows the current effective caps in the
    header (read by render_summary via the same limits file).
    """
    import argparse as _ap
    p = _ap.ArgumentParser(
        prog="dashboard_agg.py",
        description="Multi-symbol GT aggregator. Optional --exposure / --risk "
                    "flags persist portfolio-wide caps via .gt_portfolio_limits.json",
    )
    p.add_argument(
        "--exposure", type=float, metavar="USD",
        help="Portfolio combined-exposure cap (USD). Writes to "
             ".gt_portfolio_limits.json — every running bot picks it up within "
             "~1s without relaunch. Pass 0 to clear a previous override.",
    )
    p.add_argument(
        "--risk", type=float, metavar="USD",
        help="Portfolio daily-loss circuit-breaker (USD, positive number). "
             "When portfolio realized P&L drops past -<RISK>, the engine "
             "pauses new entries. Pass 0 to clear a previous override.",
    )
    args = p.parse_args()
    if args.exposure is None and args.risk is None:
        return

    # Lazy-import here so the dashboard's hot path is unaffected when
    # no CLI overrides are present.
    from src.strategy.risk import PortfolioLimitsReader
    limits = PortfolioLimitsReader()
    fields: dict = {}
    if args.exposure is not None:
        fields["max_position_value_usd"] = float(args.exposure)
    if args.risk is not None:
        fields["max_daily_loss_usd"] = float(args.risk)
    updated = limits.write(updated_by=f"dashboard_agg.py {' '.join(sys.argv[1:])}", **fields)
    print(f"{Q.CYAN}[limits]{Q.RESET} wrote {limits.path.name}: "
          f"exposure=${updated.get('max_position_value_usd', '—')} · "
          f"risk=-${updated.get('max_daily_loss_usd', '—')}")
    print(f"{Q.GRAY_3}        every running bot reads this within ~1s "
          f"(RiskCheck takes min(file, per-bot config)){Q.RESET}")
    time.sleep(1.2)  # let the operator read the confirmation


if __name__ == "__main__":
    _parse_cli_and_persist_limits()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
