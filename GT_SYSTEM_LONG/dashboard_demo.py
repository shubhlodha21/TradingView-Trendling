#!/usr/bin/env python3
"""
GT Dashboard v2 — live visual demo

Runs the new senior-quant dashboard against a fully simulated session so you
can see exactly what it'll look like during the 4-day paper test, without
needing IBKR open or the market live.

Simulates:
    - Price walk around $228 with mean reversion + occasional breakouts
    - Realistic BBO + LTP history (LTP at ~50 ticks/s when active)
    - A handful of fills with credible slippage
    - Equity curve that walks up over the session
    - Alerts firing at varied severity levels
    - Connection health updates
    - State transitions (MONITORING → IN_POSITION → WAITING_REENTRY)
    - Session controller transitions (paused at startup → resumed)

Run:
    python dashboard_demo.py

Stop:
    Ctrl+C
"""
import asyncio
import math
import random
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, 'src')

from dashboard import State, build_frame, HOME, CLEAR_SCREEN
from src.config.models import OrderRecord, OrderSide, OrderType, OrderStatus, COMMISSION_PER_SHARE
from src.infra.alerts import AlertManager, AlertSeverity


class MockGateway:
    """Stand-in for src.execution.broker.Gateway during the demo.

    Surfaces only what State.sync_engine reads: `paper`, `connected`, and
    `_last_heartbeat`. No actual IBKR calls; everything is local.
    """
    paper = False
    connected = True

    def __init__(self):
        self._last_heartbeat = datetime.now()
        self._last_price = 228.0


class MockEngine:
    """In-memory engine that produces realistic data for the dashboard.

    Drives a synthetic trading cycle: places an entry, fills it, holds,
    exits on stop, places re-entry, and so on. Tick events update the
    feed; periodic ticks call `_advance()` to evolve internal state.
    """

    def __init__(self):
        self.gateway = MockGateway()
        self._paused = False
        self.config = SimpleNamespace(
            trigger_price=228.00,
            ticker="NVDA",
            stop_loss_pct=0.01,
            # Larger qty than the original `1` makes the new POSVAL / EXP /
            # BP USED pills visually meaningful — at qty=1 the exposure
            # number stays below 1% of equity and the BP utilization row
            # is too small to see colour change.
            quantity=50,
            ibkr_client_id=1,
        )

        # Account-level numbers — demo equivalents of the values that the
        # risk module's gateway-summary cache would carry in live. Drifted
        # mildly inside _walk_price so the PORTFOLIO footer renders with
        # visible motion rather than static placeholders. Buying power is
        # ~4× equity (a typical day-trading margin account).
        self._demo_equity = 100_000.0
        self._demo_buying_power = 400_000.0

        # State machine
        self._state = "MONITORING"
        self._cycle_id = "demoabcd"
        self._running = True
        self._position_open = False
        self._entry_price = None
        self._highest_price = None
        self._stop_loss = None
        self._quantity = 0
        self._previous_breakout_level = None

        # Per-session counters
        self._trades_today = 0
        self._wins = 0
        self._losses = 0
        self._pnl = 0.0
        self._total_commission = 0.0
        self._fill_count = 0

        # Order history (consumed by State.sync_engine for slip/latency samples)
        self._order_history = deque()

        # Registry stub
        class _Reg:
            def __init__(self, history):
                self._orders = {o.order_id: o for o in history}

            def add(self, order):
                self._orders[order.order_id] = order

        self.registry = _Reg(self._order_history)

        # Slippage attribution (engine's get_status surfaces these)
        self._avg_slip = 0.0
        self._worst_slip = 0.0

        # Latency stats for the production_feed callable
        self._feed_latency = {
            'p50_ms': 0.04,
            'p95_ms': 0.10,
            'p99_ms': 0.38,
            'max_ms': 3.10,
            'count': 0,
        }
        # Stub out the production_feed attribute that sync_engine looks for
        self._feed = SimpleNamespace(get_latency_stats=lambda: self._feed_latency)

    def get_status(self):
        return {
            'state': self._state,
            'cycle_id': self._cycle_id,
            'running': self._running,
            'position_open': self._position_open,
            'entry_price': self._entry_price,
            'highest_price': self._highest_price,
            'stop_loss': self._stop_loss,
            'previous_breakout_level': self._previous_breakout_level,
            'trigger_price': self.config.trigger_price,
            'quantity': self._quantity,
            'trades_today': self._trades_today,
            'wins': self._wins,
            'losses': self._losses,
            'pnl': self._pnl,
            'total_commission': self._total_commission,
            'trades_in_registry': len(self.registry._orders),
            'pending_side': None,
            'ticks_dropped': 0,
            'tick_queue_depth': random.randint(0, 5),
            'avg_slippage': self._avg_slip,
            'worst_slippage': self._worst_slip,
            'fill_count': self._fill_count,
            'risk': {
                'daily_pnl': self._pnl,
                'trades_today': self._trades_today,
                'consec_losses': 0,
                # equity + buying_power surface in the new PORTFOLIO footer
                # at the bottom of the POSITION LADDER column. Both update
                # mildly each tick so the operator can confirm the rows
                # aren't frozen / placeholder values.
                'equity': round(self._demo_equity, 2),
                'buying_power': round(self._demo_buying_power, 2),
            },
            'risk_limits': {
                'max_consec_losses': 300,
                'max_trades_per_day': 50,
                'daily_loss_limit_pct': -0.90,
            },
        }


class SimSession:
    """Drives the demo: time, price walk, fills, alerts, state transitions."""

    def __init__(self):
        random.seed(7)
        self.engine = MockEngine()
        self.state = State(engine=self.engine)
        self.state.symbol = "NVDA"
        # Surface the client_id so the header carries the `c1` badge and
        # the System panel's "Client ID c1" row renders — both are new
        # additions for the multi-client-id world.
        self.state.client_id = self.engine.config.ibkr_client_id
        self.state.connected = True
        self.alerts_mgr = AlertManager()
        self.state.alerts.attach(self.alerts_mgr)

        # Wire engine log callback into the dashboard's event stream
        # (mirrors what run_live.py does for real engines).
        self._push_event = self.state.events.push

        # Seed feed
        self.state.feed.open_px = 227.55
        self.state.feed.high = 227.55
        self.state.feed.low = 227.55
        self.state.feed.last = 227.55
        self.state.feed.bid = 227.54
        self.state.feed.ask = 227.56
        self.state.feed.bid_size = 200
        self.state.feed.ask_size = 150
        self.state.feed.volume = 1_200_000
        self.engine.gateway._last_price = 227.55

        # Tick timing
        self._tick_count = 0
        self._last_render = 0.0
        self._t0 = time.monotonic()

        # Price walk parameters
        self._mu = 0.0          # drift
        self._sigma = 0.04      # per-tick stdev (cents)
        self._momentum = 0.0   # for occasional breakout pushes

        # Schedule the first "session_opened" alert for visual interest
        self._scheduled_alerts: list[tuple[float, str, AlertSeverity, str]] = [
            (1.0,  "SESSION_OPENED", AlertSeverity.LOW,
             "ETH session opened. Engine resumed. equity=$100000.00"),
            (10.0, "TRIPWIRE_LOST_PENDING", AlertSeverity.CRITICAL,
             "(demo) Saved pending SL order not found at broker."),
            (25.0, "ORDER_REJECTED", AlertSeverity.HIGH,
             "(demo) IBKR rejected SL_SELL_1_NVDA: thin liquidity."),
            (40.0, "STALE_FEED", AlertSeverity.MEDIUM,
             "(demo) No price update for 63s during session."),
            (55.0, "SESSION_OPENED", AlertSeverity.LOW,
             "ETH session opened (rolled). Engine resumed."),
        ]

    # ─── Price walk ───
    def _walk_price(self):
        """Geometric random walk + occasional regime push.

        Emits realistic L1 traffic: 2-3 BBO updates per trade tick, with
        sized depth, exchange tags, and condition flags. Goes through
        State.on_tick so the new MicrostructureStats samplers accumulate
        VWAP / rates / aggression exactly as they will in production.
        """
        from src.feed.handler import Tick, MessageType

        elapsed = time.monotonic() - self._t0
        self._mu = 0.005 + math.sin(elapsed / 30) * 0.01

        if random.random() < 0.005:
            self._momentum = random.choice([-1, 1]) * random.uniform(0.05, 0.2)

        shock = random.gauss(self._mu, self._sigma) + self._momentum
        self._momentum *= 0.95
        new_px = max(220.0, min(240.0, self.state.feed.last + shock))
        new_px = round(new_px, 2)

        # Build a fresh top-of-book around the new price. Spread varies
        # between $0.01 and $0.04 to give the panel realistic variance.
        spread = random.choice([0.01, 0.01, 0.01, 0.02, 0.02, 0.03])
        bid = round(new_px - spread / 2, 2)
        ask = round(new_px + spread / 2, 2)
        bid_size = random.randint(50, 800)
        ask_size = random.randint(50, 800)

        now = datetime.now()

        # Fire 1-2 BBO ticks (the "passive" layer of L1 traffic)
        for _ in range(random.choice([1, 1, 2])):
            tick = Tick(
                timestamp=now,
                symbol="NVDA",
                last=0.0,  # BBO tick — no trade content
                last_size=0.0,
                last_exchange="",
                last_conditions="",
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
                volume=self.state.feed.volume,
                open=self.state.feed.open_px,
                high=self.state.feed.high,
                low=self.state.feed.low,
                prev_last=self.state.feed.last,
                prev_bid=self.state.feed.bid,
                prev_ask=self.state.feed.ask,
                tick_type=MessageType.TICK,
                req_id=1,
            )
            self.state.on_tick(tick)

        # Fire a TRADE tick — the price-discovery layer. Classify direction
        # by where the trade landed vs the prior BBO (will be classified by
        # MicrostructureStats based on bid/ask in the tick).
        trade_size = random.choice([100, 100, 200, 50, 300, 500, 100])
        # Decide aggression: ~55% buys above mid, 45% sells below mid, with
        # occasional within-spread (passive) trades.
        roll = random.random()
        if roll < 0.5:
            trade_price = ask  # buyer lifted ask
        elif roll < 0.92:
            trade_price = bid  # seller hit bid
        else:
            trade_price = round((bid + ask) / 2, 2)  # midpoint / passive
        # Occasional odd lot
        cond = "I" if trade_size < 100 else "F"
        # Pick an exchange
        exchange = random.choice(["NASDAQ", "NYSE", "BATS", "ARCA", "IEX", "EDGX"])

        trade_tick = Tick(
            timestamp=now,
            symbol="NVDA",
            last=trade_price,
            last_size=trade_size,
            last_exchange=exchange,
            last_conditions=cond,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            volume=self.state.feed.volume + trade_size,
            open=self.state.feed.open_px,
            high=max(self.state.feed.high, trade_price),
            low=min(self.state.feed.low or trade_price, trade_price),
            prev_last=self.state.feed.last,
            prev_bid=bid,
            prev_ask=ask,
            tick_type=MessageType.TRADE,
            req_id=1,
        )
        self.state.on_tick(trade_tick)

        # Tracking — update what the state caches
        self.state.feed.high = max(self.state.feed.high, trade_price)
        self.state.feed.low = min(self.state.feed.low or trade_price, trade_price)
        self.engine.gateway._last_price = trade_price
        self.engine.gateway._last_heartbeat = now

        # Engine latency cosmetic update
        self.engine._feed_latency['count'] = self.state.feed.total
        self.engine._feed_latency['p50_ms'] = round(random.uniform(0.02, 0.08), 3)
        self.engine._feed_latency['p99_ms'] = round(random.uniform(0.20, 0.60), 3)
        self.engine._feed_latency['max_ms'] = round(random.uniform(1.5, 4.5), 3)

        # Track new highs when in position (mirrors engine._track_high)
        if self.engine._position_open and self.engine._highest_price is not None:
            if trade_price > self.engine._highest_price:
                self.engine._highest_price = trade_price

        # Drift demo equity by the unrealised P&L on the open position —
        # mirrors how IBKR's NetLiquidation actually moves in real time
        # (cash + mark-to-market positions). When flat, equity holds steady
        # at base_equity + realised P&L. Buying power follows at ~4×.
        # This makes the new PORTFOLIO footer visibly responsive to LTP
        # motion instead of staring at a frozen $100,000 forever.
        # Values live on the MockEngine (where get_status reads them), NOT
        # on SimSession.
        base_equity = 100_000.0
        unreal = 0.0
        if (self.engine._position_open
                and self.engine._entry_price
                and self.engine._quantity):
            unreal = (trade_price - self.engine._entry_price) * self.engine._quantity
        self.engine._demo_equity = base_equity + self.engine._pnl + unreal
        # Buying power = 4× equity for a typical reg-T day-trading account.
        # Slightly less than that when LONG since the open position consumes
        # some margin headroom — approximated by subtracting 25% of posval.
        posval = (self.engine._quantity * trade_price
                  if self.engine._position_open else 0.0)
        self.engine._demo_buying_power = 4.0 * self.engine._demo_equity - 0.25 * posval

    # ─── Tick rate ─
    # NOTE: removed _calc_rate; the on-tick path (FeedStats.update +
    # MicrostructureStats.on_tick called from State.on_tick) already
    # populates rates correctly from the same `_times` deque + the new
    # _tick_times/_bbo_times/_trade_times deques in MicrostructureStats.

    # ─── State transitions / fills ───
    def _maybe_transition(self):
        ltp = self.state.feed.last
        # Entry: when MONITORING and LTP crosses trigger upward
        if self.engine._state == "MONITORING" and ltp >= self.engine.config.trigger_price:
            self._fill_buy(ltp)
            return
        # Exit: when IN_POSITION and LTP drops to stop
        if self.engine._state == "IN_POSITION" and self.engine._stop_loss and ltp <= self.engine._stop_loss:
            self._fill_sell(ltp, reason="STOP_LOSS")
            return
        # Re-entry: when WAITING_REENTRY and LTP crosses breakout
        if (
            self.engine._state == "WAITING_REENTRY"
            and self.engine._previous_breakout_level
            and ltp >= self.engine._previous_breakout_level
        ):
            self._fill_buy(ltp, label="REENTRY")
            return

    def _fill_buy(self, ltp: float, label: str = "ENTRY"):
        # Add a small fill slippage to simulate IBKR's actual fill behaviour
        fill_px = round(ltp + random.uniform(0.00, 0.04), 2)
        qty = self.engine.config.quantity
        now = datetime.now()
        submitted_ts = now - timedelta(milliseconds=random.randint(15, 80))
        signal_px = (self.engine.config.trigger_price if label == "ENTRY"
                     else self.engine._previous_breakout_level)
        order = OrderRecord(
            order_id=f"BUY_{self.engine._trades_today + 1}",
            symbol="NVDA",
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.STOP_LIMIT,
            status=OrderStatus.FILLED,
            submitted_at=submitted_ts,
            filled_at=now,
            avg_fill_price=fill_px,
            signal_price=signal_px,
            filled_qty=qty,
            # Commission scales with shares filled (qty × per-share rate),
            # not a flat $0.35. For 50 shares this is 50 × $0.35 = $17.50.
            commission=qty * COMMISSION_PER_SHARE,
        )
        self.engine._order_history.append(order)
        self.engine.registry.add(order)
        self.engine._position_open = True
        self.engine._entry_price = fill_px
        self.engine._highest_price = fill_px
        self.engine._stop_loss = round(fill_px * (1 - self.engine.config.stop_loss_pct), 2)
        self.engine._quantity = qty
        self.engine._state = "IN_POSITION"
        self.engine._total_commission += qty * COMMISSION_PER_SHARE
        self.engine._fill_count += 1
        self._push_event(
            f"FILLED BUY {qty} @ ${fill_px:.2f}, stop=1.0% = ${self.engine._stop_loss:.2f}"
        )
        # Add BOTH rows for the dashboard ORDERS panel — mirrors production
        # behaviour where the engine writes both a SUBMITTED and a FILLED
        # OrderRecord into _order_history so the operator can see the
        # signal price vs the actual fill price (and infer slippage).
        # 1) SUBMITTED row at the signal price (trigger or breakout level)
        self.state.orders.append({
            'side': 'BUY', 'qty': qty, 'px': signal_px, 'status': 'SUBMITTED',
            'ts': submitted_ts.strftime('%H:%M:%S'),
        })
        # 2) FILLED row at the actual fill price (with slippage)
        self.state.orders.append({
            'side': 'BUY', 'qty': qty, 'px': fill_px, 'status': 'FILLED',
            'ts': now.strftime('%H:%M:%S'),
        })
        # 3) Protective SL placed immediately after — SUBMITTED only,
        # will get FILLED row when stop triggers.
        self.state.orders.append({
            'side': 'SELL', 'qty': qty, 'px': self.engine._stop_loss,
            'status': 'SUBMITTED', 'ts': now.strftime('%H:%M:%S'),
        })

    def _fill_sell(self, ltp: float, reason: str):
        fill_px = round(ltp - random.uniform(0.00, 0.05), 2)
        qty = self.engine._quantity
        entry = self.engine._entry_price
        now = datetime.now()
        submitted_ts = now - timedelta(milliseconds=random.randint(20, 120))
        stop_signal_px = self.engine._stop_loss
        order = OrderRecord(
            order_id=f"SELL_{self.engine._trades_today + 1}",
            symbol="NVDA",
            side=OrderSide.SELL,
            qty=qty,
            order_type=OrderType.STOP_LIMIT,
            status=OrderStatus.FILLED,
            submitted_at=submitted_ts,
            filled_at=now,
            avg_fill_price=fill_px,
            signal_price=stop_signal_px,
            filled_qty=qty,
            # Same per-share scaling as the BUY side — round-trip total
            # for an N-share trade is therefore 2 × N × COMMISSION_PER_SHARE.
            commission=qty * COMMISSION_PER_SHARE,
        )
        self.engine._order_history.append(order)
        self.engine.registry.add(order)

        gross = (fill_px - entry) * qty
        # Round-trip = entry-side + exit-side commission, each scaled by qty.
        round_trip_comm = 2 * qty * COMMISSION_PER_SHARE
        pnl = gross - round_trip_comm
        self.engine._pnl += pnl
        self.engine._total_commission += qty * COMMISSION_PER_SHARE
        self.engine._fill_count += 1
        self.engine._trades_today += 1
        if pnl > 0:
            self.engine._wins += 1
        else:
            self.engine._losses += 1

        # Set re-entry level to the cycle high
        self.engine._previous_breakout_level = self.engine._highest_price

        # Reset position
        self.engine._position_open = False
        self.engine._entry_price = None
        self.engine._highest_price = None
        self.engine._stop_loss = None
        self.engine._quantity = 0
        self.engine._state = "WAITING_REENTRY"

        self._push_event(
            f"FILLED SELL {qty} @ ${fill_px:.2f} [{reason}], P&L: ${pnl:+.2f} "
            f"(gross: ${gross:+.2f}, comm: ${round_trip_comm:.2f})"
        )
        self._push_event(
            f"Re-entry breakout at ${self.engine._previous_breakout_level:.2f}"
        )
        # Note: the SELL stop's SUBMITTED row was added earlier by _fill_buy
        # when the protective stop was placed. Here we just record the FILL.
        # This mirrors production: SUBMITTED + FILLED pair per stop-limit.
        self.state.orders.append({
            'side': 'SELL', 'qty': qty, 'px': fill_px, 'status': 'FILLED',
            'ts': now.strftime('%H:%M:%S'),
        })
        # Re-entry BUY at the previous breakout: SUBMITTED only at this
        # stage. When LTP next crosses it (in _fill_buy), the FILLED row
        # will be added — same SUBMITTED-then-FILLED pattern.
        self.state.orders.append({
            'side': 'BUY', 'qty': qty,
            'px': self.engine._previous_breakout_level,
            'status': 'SUBMITTED', 'ts': now.strftime('%H:%M:%S'),
        })

    # ─── Alerts schedule ───
    def _fire_scheduled_alerts(self):
        elapsed = time.monotonic() - self._t0
        while self._scheduled_alerts and self._scheduled_alerts[0][0] <= elapsed:
            delay, code, severity, msg = self._scheduled_alerts.pop(0)
            self.alerts_mgr.raise_alert(
                code=code, severity=severity, message=msg,
                context={"demo": True},
            )
            self._push_event(f"[ALERT {severity.value}] {code}")

    # ─── Main loop ───
    async def run(self, render_hz: float = 5.0, tick_hz: float = 30.0):
        sys.stdout.write(HOME + CLEAR_SCREEN)
        sys.stdout.flush()
        tick_interval = 1.0 / tick_hz
        render_interval = 1.0 / render_hz
        last_render = 0.0

        try:
            while True:
                now = time.monotonic()
                self._walk_price()
                self._maybe_transition()
                self._fire_scheduled_alerts()

                if now - last_render >= render_interval:
                    self.state.sync_engine()
                    # Force sync to run (it has its own 300ms throttle internally,
                    # so reset the throttle so we can render at our chosen pace).
                    self.state._last_engine_sync = 0
                    self.state.sync_engine()
                    frame = build_frame(self.state)
                    sys.stdout.write(HOME + frame + '\n')
                    sys.stdout.flush()
                    last_render = now

                await asyncio.sleep(tick_interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            sys.stdout.write('\n')
            print("[demo] stopped")


if __name__ == "__main__":
    print("\033[36mGT Dashboard v2 — visual demo\033[0m")
    print("\033[2mPress Ctrl+C to stop. The price walk + fills are randomised; refresh to see variation.\033[0m")
    print()
    time.sleep(1.0)
    try:
        asyncio.run(SimSession().run())
    except KeyboardInterrupt:
        print("\n[demo] exit")
