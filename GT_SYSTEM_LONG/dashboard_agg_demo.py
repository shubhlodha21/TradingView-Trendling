#!/usr/bin/env python3
"""
GT Aggregator Dashboard — live visual demo.

Stands up a self-contained simulation of 3 tickers running side-by-side
(TSLA, NVDA, AAPL), each with its own state file + live snapshot, and
renders the multi-symbol aggregator against them. Exercises every visual
feature that the real aggregator depends on:

    • LIVE zone: 2-3 active tickers with their state, position, posval
    • OFFLINE zone: scheduled kill at t≈25s — AAPL stops writing → moves
      to the dimmed "OFF HH:MM:SS" zone after ~30s of staleness
    • Account pills in the header: eq / exp / bp (sums across all
      watchers — equity / buying-power picked from any live snapshot)
    • TOTAL ACTIVE EXPOSURE summary line below the rows (drops when
      a terminal goes offline)
    • COMBINED ORDERS section: synthetic audit CSV rows generated per
      simulated fill, oldest at top → newest at bottom, mixed across
      tickers and sorted by timestamp + symbol tiebreak
    • COMBINED ALERTS section: a scheduled CRITICAL alert ~10s in to
      show the colour + formatting

Everything lives under a fresh temp directory (via tempfile.mkdtemp +
os.chdir) so this demo never touches your real working state files.
On Ctrl+C the directory is removed.

Run:
    python dashboard_agg_demo.py

Stop:
    Ctrl+C
"""
import asyncio
import json
import math
import os
import random
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Resolve the project root and prepend BEFORE the chdir to tmp — otherwise
# imports of `dashboard_agg` would fail because the cwd is no longer this
# repo's root.
_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# Use the same per-share commission rate as the rest of the system so the
# demo's audit rows + total_commission numbers match what the dashboards
# would render against real fills.
from src.config.models import COMMISSION_PER_SHARE


# ─────────────────────────────────────────────────────────────────────────
# Synthetic per-ticker state
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class TickerSim:
    """One simulated ticker. Owns its price walk, state machine, and the
    two files the aggregator polls (`.gt_state_*.json`, `.gt_live_*.json`).

    The state machine is intentionally simple — enough variety to drive
    the aggregator panels but not a faithful clone of the real engine.
    """
    symbol: str
    client_id: int
    trigger: float
    qty: int
    # Initial state — can be any of MONITORING / IN_POSITION / WAITING_REENTRY
    initial_state: str = "MONITORING"
    initial_entry: float = 0.0
    initial_position_open: bool = False

    # Per-walk state (mutated each tick)
    state: str = field(init=False)
    last_price: float = field(init=False)
    open_px: float = field(init=False)
    entry_price: float = field(init=False)
    quantity_held: int = field(init=False)
    stop_price: float = field(init=False)
    highest: float = field(init=False)
    breakout: float = field(init=False)
    pnl: float = field(init=False)
    comm: float = field(init=False)
    trades_today: int = field(init=False)
    wins: int = field(init=False)
    losses: int = field(init=False)
    # When True, this ticker stops writing files — simulating a killed
    # `run_live.py` process. Set externally by the AggSimSession.
    killed: bool = field(init=False, default=False)

    def __post_init__(self):
        self.state = self.initial_state
        self.last_price = self.trigger
        self.open_px = self.trigger
        self.entry_price = self.initial_entry
        self.quantity_held = self.qty if self.initial_position_open else 0
        # Stop = entry × (1 - 1%) if we're already long
        self.stop_price = round(self.initial_entry * 0.99, 2) if self.initial_position_open else 0.0
        self.highest = self.initial_entry if self.initial_position_open else self.trigger
        self.breakout = 0.0
        self.pnl = 0.0
        # Entry-side commission = qty × per-share rate (scales with shares,
        # no flat $0.35 floor anymore). For 50 shares at $0.35/share that's
        # $17.50, not $0.35.
        self.comm = (self.qty * COMMISSION_PER_SHARE
                     if self.initial_position_open else 0.0)
        self.trades_today = 0
        self.wins = 0
        self.losses = 0

    # ── Price walk: small mean-reverting Brownian motion ──
    def walk_price(self) -> None:
        sigma = 0.06   # per-tick stdev (dollars)
        # Mean revert toward trigger ± small drift
        anchor = (self.trigger + (self.initial_entry or self.trigger)) / 2.0
        pull = (anchor - self.last_price) * 0.002
        shock = random.gauss(0.0, sigma)
        new_px = self.last_price + pull + shock
        # Clamp to a reasonable band so the demo never wanders silly
        band = max(self.trigger * 0.05, 5.0)
        new_px = max(self.trigger - band, min(self.trigger + band, new_px))
        self.last_price = round(new_px, 2)

        # Update high water for an open position
        if self.quantity_held > 0:
            self.highest = max(self.highest, self.last_price)

    # ── State machine — entry / exit / re-entry ──
    def step_machine(self) -> list[dict]:
        """Advance the state machine one tick. Returns any audit-row dicts
        emitted during this tick (FILLED/SUBMITTED events) so the caller
        can persist them into the audit CSV for the COMBINED ORDERS panel.
        """
        events: list[dict] = []
        now_iso = datetime.now().isoformat(timespec='microseconds')

        # MONITORING: cross trigger upward → FILLED BUY → IN_POSITION
        if self.state == "MONITORING" and self.last_price >= self.trigger:
            fill_px = round(self.last_price + random.uniform(0.0, 0.03), 2)
            self.entry_price = fill_px
            self.quantity_held = self.qty
            self.stop_price = round(fill_px * 0.99, 2)
            self.highest = fill_px
            # Per-share commission scales with qty.
            self.comm += self.qty * COMMISSION_PER_SHARE
            self.state = "IN_POSITION"
            # Emit SUBMITTED + FILLED audit rows so the COMBINED ORDERS
            # section shows both.
            base_id = f"ENTRY_BUY_{self.qty}_{self.symbol}"
            events.append({
                'timestamp': now_iso, 'event': 'SUBMITTED', 'order_id': base_id,
                'side': 'BUY', 'qty': self.qty, 'order_type': 'STOP_LIMIT',
                'limit_price': self.trigger + 0.05, 'stop_price': self.trigger,
                'signal_price': self.trigger, 'fill_price': '',
                'slippage': '', 'commission': '', 'pnl': '', 'reason': '',
                'exchange': '', 'state_at_time': 'MONITORING',
                'position_at_time': 'FLAT',
            })
            events.append({
                'timestamp': now_iso, 'event': 'FILLED', 'order_id': base_id,
                'side': 'BUY', 'qty': self.qty, 'order_type': 'LIMIT',
                'limit_price': fill_px, 'stop_price': '',
                'signal_price': self.trigger, 'fill_price': fill_px,
                'slippage': '', 'commission': '', 'pnl': '', 'reason': '',
                'exchange': 'SMART', 'state_at_time': 'IN_POSITION',
                'position_at_time': 'FLAT',
            })
            # Also submit the protective SELL stop right away
            sl_id = f"SL_SELL_{self.qty}_{self.symbol}"
            events.append({
                'timestamp': now_iso, 'event': 'SUBMITTED', 'order_id': sl_id,
                'side': 'SELL', 'qty': self.qty, 'order_type': 'STOP_LIMIT',
                'limit_price': self.stop_price - 0.05, 'stop_price': self.stop_price,
                'signal_price': self.stop_price, 'fill_price': '',
                'slippage': '', 'commission': '', 'pnl': '', 'reason': '',
                'exchange': '', 'state_at_time': 'IN_POSITION',
                'position_at_time': 'LONG',
            })
            return events

        # IN_POSITION: cross stop downward → FILLED SELL → WAITING_REENTRY
        if (self.state == "IN_POSITION"
                and self.stop_price > 0
                and self.last_price <= self.stop_price):
            fill_px = round(self.stop_price - random.uniform(0.0, 0.04), 2)
            gross = (fill_px - self.entry_price) * self.quantity_held
            # Both sides scale with the qty held — round-trip = 2 × qty × rate.
            trade_comm = self.quantity_held * COMMISSION_PER_SHARE
            self.pnl += gross - trade_comm - trade_comm  # exit + prior entry
            self.comm += trade_comm
            self.trades_today += 1
            if gross > trade_comm:
                self.wins += 1
            else:
                self.losses += 1
            self.breakout = self.highest
            sl_id = f"SL_SELL_{self.quantity_held}_{self.symbol}"
            events.append({
                'timestamp': now_iso, 'event': 'FILLED', 'order_id': sl_id,
                'side': 'SELL', 'qty': self.quantity_held, 'order_type': 'STOP_LIMIT',
                'limit_price': fill_px, 'stop_price': self.stop_price,
                'signal_price': self.entry_price, 'fill_price': fill_px,
                'slippage': fill_px - self.entry_price, 'commission': trade_comm,
                'pnl': gross - 2 * trade_comm, 'reason': 'STOP_LOSS',
                'exchange': 'SMART', 'state_at_time': 'IN_POSITION',
                'position_at_time': 'LONG',
            })
            self.quantity_held = 0
            self.entry_price = 0.0
            self.stop_price = 0.0
            self.state = "WAITING_REENTRY"
            return events

        # WAITING_REENTRY: cross breakout upward → FILLED BUY → IN_POSITION
        if (self.state == "WAITING_REENTRY"
                and self.breakout > 0
                and self.last_price >= self.breakout):
            fill_px = round(self.last_price + random.uniform(0.0, 0.03), 2)
            self.entry_price = fill_px
            self.quantity_held = self.qty
            self.stop_price = round(fill_px * 0.99, 2)
            self.highest = fill_px
            # Re-entry side: same per-share scaling.
            self.comm += self.qty * COMMISSION_PER_SHARE
            base_id = f"REENTRY_BUY_{self.qty}_{self.symbol}"
            events.append({
                'timestamp': now_iso, 'event': 'FILLED', 'order_id': base_id,
                'side': 'BUY', 'qty': self.qty, 'order_type': 'LIMIT',
                'limit_price': fill_px, 'stop_price': '',
                'signal_price': self.breakout, 'fill_price': fill_px,
                'slippage': '', 'commission': '', 'pnl': '', 'reason': '',
                'exchange': 'SMART', 'state_at_time': 'IN_POSITION',
                'position_at_time': 'FLAT',
            })
            self.state = "IN_POSITION"
            return events

        return events

    # ── Persist current state + live snapshot ──
    def write_files(self, base_dir: Path, equity: float, buying_power: float) -> None:
        """Write `.gt_state_<SYM>_<CID>.json` + `.gt_live_<SYM>_<CID>.json`.

        Skipped entirely when `self.killed` so the aggregator's staleness
        gate (30s) eventually moves the ticker to the offline zone.
        """
        if self.killed:
            return
        position_open = self.quantity_held > 0
        position_notional = self.quantity_held * self.last_price if position_open else 0.0

        state_payload = {
            "state": self.state,
            "position_open": position_open,
            "entry_price": self.entry_price if position_open else None,
            "highest_price": self.highest if position_open else None,
            "stop_loss": self.stop_price if position_open else None,
            "previous_breakout_level": self.breakout if self.breakout > 0 else None,
            "quantity": self.quantity_held,
            "trades_today": self.trades_today,
            "wins": self.wins,
            "losses": self.losses,
            "pnl": round(self.pnl, 2),
            "total_commission": round(self.comm, 2),
            "trigger_price": self.trigger,
            "config_quantity": self.qty,
            "updated_at": datetime.now().isoformat(),
        }
        live_payload = {
            "ts": datetime.now().isoformat(),
            "symbol": self.symbol,
            "last": self.last_price,
            "bid": round(self.last_price - 0.01, 2),
            "ask": round(self.last_price + 0.01, 2),
            "bid_size": random.randint(100, 800),
            "ask_size": random.randint(100, 800),
            "volume": random.randint(500_000, 5_000_000),
            "open": self.open_px,
            "high": max(self.open_px, self.last_price),
            "low": min(self.open_px, self.last_price),
            "rate": round(random.uniform(50, 300), 1),
            "trigger_price": self.trigger,
            "stop_loss_pct": 0.01,
            "quantity": self.qty,
            "equity": equity,
            "buying_power": buying_power,
            "position_notional": position_notional,
            "exposure_pct": (position_notional / equity * 100.0) if equity > 0 else 0.0,
            "bp_used_pct": (position_notional / buying_power * 100.0) if buying_power > 0 else 0.0,
            "connected": True,
            "heartbeat_age": 0.1,
            "in_session": True,
            "paused": False,
            "vwap": self.last_price,
            "tick_rate": round(random.uniform(50, 300), 1),
            "trade_rate": round(random.uniform(20, 80), 1),
            "bbo_rate": round(random.uniform(30, 250), 1),
            "buy_pct": round(random.uniform(40, 70), 1),
            "sell_pct": round(random.uniform(30, 60), 1),
            "tape": [],
            "latency": {"p50_ms": 0.05, "p95_ms": 0.12, "p99_ms": 0.40, "max_ms": 3.2},
        }
        sp = base_dir / f".gt_state_{self.symbol}_{self.client_id}.json"
        lp = base_dir / f".gt_live_{self.symbol}_{self.client_id}.json"
        # Atomic-ish write via temp file + rename, same pattern as the
        # real engine's write_live_snapshot. Avoids partial-read flicker
        # on the aggregator side.
        for path, payload in ((sp, state_payload), (lp, live_payload)):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, default=str))
            tmp.replace(path)


# ─────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────

class AggSimSession:
    """Runs three ticker sims in parallel and renders dashboard_agg's
    `render_summary` against the resulting file tree.

    Schedule:
        t=0      : 3 tickers start LIVE (TSLA monitoring, NVDA in_position,
                   AAPL waiting_reentry).
        t≈10s    : a CRITICAL alert lands in alerts.jsonl.
        t≈25s    : AAPL gets "killed" — stops writing files. After ~30s of
                   staleness (35-55s), it moves to the OFFLINE zone.
        ongoing  : prices walk, fills happen, audit CSV grows, the active
                   exposure total moves with the live LONG positions.
    """
    def __init__(self):
        random.seed(31)
        self.tmpdir = Path(tempfile.mkdtemp(prefix="gt_agg_demo_"))
        # Subdirs the aggregator + audit writer expect
        (self.tmpdir / "data" / "audit").mkdir(parents=True, exist_ok=True)
        (self.tmpdir / "data" / "alerts").mkdir(parents=True, exist_ok=True)
        os.chdir(self.tmpdir)

        self.t0 = time.monotonic()
        self.equity = 100_000.0
        self.buying_power = 400_000.0

        # The three demo tickers — varied initial state for visual interest.
        self.tickers: list[TickerSim] = [
            TickerSim(symbol="TSLA", client_id=2, trigger=408.20, qty=50,
                      initial_state="IN_POSITION", initial_entry=408.20,
                      initial_position_open=True),
            TickerSim(symbol="NVDA", client_id=1, trigger=222.85, qty=100,
                      initial_state="MONITORING"),
            TickerSim(symbol="AAPL", client_id=3, trigger=297.50, qty=20,
                      initial_state="WAITING_REENTRY",
                      initial_entry=0.0, initial_position_open=False),
        ]
        # AAPL starts with a breakout level so its re-entry logic can fire
        self.tickers[2].breakout = 298.00
        self.tickers[2].last_price = 297.40
        self.tickers[2].open_px = 297.40

        # Audit CSVs (one per symbol, daily, append-only) + alerts.jsonl.
        # We pre-write the headers so the aggregator's `tail_orders` finds
        # a parseable file even before any events land.
        self.audit_dir = self.tmpdir / "data" / "audit"
        self.alerts_path = (
            self.tmpdir / "data" / "alerts"
            / f"alerts_{datetime.now().strftime('%Y%m%d')}.jsonl"
        )
        self.alerts_path.write_text("")
        today = datetime.now().strftime("%Y%m%d")
        for t in self.tickers:
            path = self.audit_dir / f"order_{t.symbol}_{today}.csv"
            path.write_text(
                "timestamp,event,order_id,side,qty,order_type,limit_price,"
                "stop_price,signal_price,fill_price,slippage,commission,"
                "pnl,reason,exchange,state_at_time,position_at_time\n"
            )

        # One scheduled alert ~10s in to demonstrate the colour in the
        # COMBINED ALERTS panel. Tuple shape: (elapsed_s, severity, code, msg).
        self.scheduled_alerts: list[tuple[float, str, str, str]] = [
            (10.0, "HIGH", "DEMO_PRICE_GAP",
             "Demo: synthetic 35-bps gap detected on NVDA — investigating."),
            (22.0, "CRITICAL", "DEMO_NAKED_POSITION",
             "Demo: orphan position adopted on TSLA (50 @ $408.20). SL armed."),
        ]
        # Schedule the AAPL kill: after this point, AAPL stops writing
        # files, so the aggregator's 30s staleness threshold moves it
        # into the OFFLINE zone ~30s later.
        self.kill_aapl_at = 25.0

    # ── Persist a freshly-emitted order event to today's audit CSV ──
    def _append_audit_rows(self, symbol: str, rows: list[dict]) -> None:
        if not rows:
            return
        today = datetime.now().strftime("%Y%m%d")
        path = self.audit_dir / f"order_{symbol}_{today}.csv"
        fields = [
            'timestamp', 'event', 'order_id', 'side', 'qty', 'order_type',
            'limit_price', 'stop_price', 'signal_price', 'fill_price',
            'slippage', 'commission', 'pnl', 'reason', 'exchange',
            'state_at_time', 'position_at_time',
        ]
        with path.open("a") as fh:
            for r in rows:
                fh.write(
                    ",".join(str(r.get(f, "")) for f in fields) + "\n"
                )

    # ── Append a scheduled alert to today's alerts.jsonl ──
    def _maybe_fire_alerts(self) -> None:
        elapsed = time.monotonic() - self.t0
        while self.scheduled_alerts and self.scheduled_alerts[0][0] <= elapsed:
            _, severity, code, msg = self.scheduled_alerts.pop(0)
            entry = {
                "timestamp": datetime.now().isoformat(),
                "severity": severity,
                "code": code,
                "message": msg,
                "context": {"demo": True},
            }
            with self.alerts_path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")

    # ── Equity drift driven by aggregate mark-to-market ──
    def _update_account(self) -> None:
        mtm = 0.0
        realized = 0.0
        for t in self.tickers:
            if t.killed:
                # Even killed tickers' positions are still at the broker
                # technically, but for the demo we only walk LIVE tickers.
                # mtm contribution doesn't matter visually since the
                # tickers' own POSVAL renders from saved files anyway.
                continue
            if t.quantity_held > 0 and t.entry_price:
                mtm += (t.last_price - t.entry_price) * t.quantity_held
            realized += t.pnl
        # Base equity floats with realised + unrealised — mirrors NetLiq.
        self.equity = 100_000.0 + realized + mtm
        # Buying power = 4× equity, minus 25% of gross posval (initial
        # margin consumed) for any open positions.
        gross_pos = sum(t.quantity_held * t.last_price
                        for t in self.tickers if not t.killed)
        self.buying_power = 4.0 * self.equity - 0.25 * gross_pos

    # ── One tick of simulation for every ticker ──
    def step(self) -> None:
        elapsed = time.monotonic() - self.t0
        # Schedule the AAPL kill
        for t in self.tickers:
            if t.symbol == "AAPL" and not t.killed and elapsed >= self.kill_aapl_at:
                t.killed = True
        for t in self.tickers:
            if t.killed:
                continue
            t.walk_price()
            new_events = t.step_machine()
            if new_events:
                self._append_audit_rows(t.symbol, new_events)
        self._update_account()
        self._maybe_fire_alerts()
        # Persist files for non-killed tickers. The killed one's mtime
        # stops advancing — eventually crossing the staleness gate.
        for t in self.tickers:
            t.write_files(self.tmpdir, self.equity, self.buying_power)

    # ── Main loop ──
    async def run(self, tick_hz: float = 4.0, render_hz: float = 4.0) -> None:
        # Import inside run() so the sys.path is already set up by the time
        # we resolve dashboard_agg (which imports dashboard.py + others).
        from dashboard_agg import discover_watchers, render_summary
        # HOME + CLEAR_SCREEN from dashboard.py — same primitives the real
        # aggregator uses for redraw-without-scroll.
        from dashboard import HOME, CLEAR_SCREEN

        sys.stdout.write(HOME + CLEAR_SCREEN)
        sys.stdout.flush()

        tick_interval = 1.0 / tick_hz
        render_interval = 1.0 / render_hz
        last_render = 0.0

        try:
            while True:
                now = time.monotonic()
                self.step()

                if now - last_render >= render_interval:
                    watchers = discover_watchers()
                    frame = render_summary(watchers)
                    sys.stdout.write(HOME + frame + "\n")
                    sys.stdout.flush()
                    last_render = now

                await asyncio.sleep(tick_interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            sys.stdout.write("\n")
            print(f"[demo] stopped. Cleaning up temp dir: {self.tmpdir}")
            shutil.rmtree(self.tmpdir, ignore_errors=True)


if __name__ == "__main__":
    print("\033[36mGT Aggregator Dashboard — visual demo\033[0m")
    print(
        "\033[2mSimulates 3 tickers (TSLA, NVDA, AAPL) with prices walking, "
        "fills, alerts, and a scheduled kill of AAPL at t=25s so you can "
        "see the OFFLINE zone + active-exposure total update. "
        "Ctrl+C to stop.\033[0m"
    )
    print()
    time.sleep(1.0)
    sess = AggSimSession()
    try:
        asyncio.run(sess.run())
    except KeyboardInterrupt:
        shutil.rmtree(sess.tmpdir, ignore_errors=True)
        print("\n[demo] exit")
