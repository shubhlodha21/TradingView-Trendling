#!/usr/bin/env python3
"""
GT System - Production Live Trading

Single entry point wiring all components:
    Gateway → FeedHandler → PipelineChain → Strategy Engine → Orders
                                        ↕ Dashboard (10Hz)

Pipeline stages (validated for < 0.004ms latency):
    SequenceMonitor → Deduplicator → FastValidator

Usage:
    python run_live.py AAPL --trigger 220.00
    python run_live.py NVDA --trigger 150.00 --port 4002
"""
import asyncio
import argparse
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import warnings


warnings.filterwarnings(
    "ignore",
    message="'asyncio.iscoroutinefunction' is deprecated",
    category=DeprecationWarning,
)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # zoneinfo unavailable; fall back to fixed offset where needed

sys.path.insert(0, 'src')

from src.config.models import Config, TradeState
from src.config.persistence import StateStore, AuditLog
from src.config.audit import AuditManager
from src.execution.broker import Gateway
from src.feed.handler import FeedHandler, Tick
from src.feed.connection import ConnectionManager, ConnectionConfig
from src.feed.production import ProductionFeed
from src.strategy.engine import Engine, NakedPositionError, ConflictingOpenOrderError
from src.strategy.risk import RiskCheck, PortfolioReader, PortfolioLimitsReader
from src.strategy.logging import QuantLogger
from src.infra.alerts import build_default_alert_manager, AlertSeverity
from dashboard import State, build_frame, HOME, CLEAR_SCREEN
from src.feed.connection import ConnectionState


# ═══════════════════════════════════════════════════════════════════════════
# ANSI
# ═══════════════════════════════════════════════════════════════════════════

R = '\033[0m'
B = '\033[1m'
D = '\033[2m'
G = '\033[32m'
R_ = '\033[31m'
Y = '\033[33m'
C = '\033[36m'

CTRL_C = f"{D}Ctrl+C{R}"


# ═══════════════════════════════════════════════════════════════════════════
# LIVE TRADER
# ═══════════════════════════════════════════════════════════════════════════

class LiveTrader:
    """
    Production trader: Gateway → Feed → Engine → Dashboard.

    Data flow:
        IBKR → FeedHandler → PipelineChain → Engine.on_tick() → Risk → Order
                                       ↕ Dashboard (throttled 10Hz)
    """

    __slots__ = ('config', 'gateway', 'conn_mgr', 'feed_handler',
                 'production_feed', 'engine', 'risk', 'logger',
                 'state_store', 'audit_log', 'audit', 'state', '_running',
                 # Captured handle so stop() can cancel feed.start() cleanly.
                 # Fire-and-forget create_task left this orphaned on shutdown
                 # — the task continued running while the event loop closed
                 # under it, producing "Task was destroyed but it is pending"
                 # noise and a RuntimeError from the tick consumer touching
                 # a half-closed loop. Holding the handle lets stop() cancel
                 # + await it in order.
                 '_feed_task',
                 # Serializes _after_reconnect. Two reconnect callbacks
                 # firing in quick succession (e.g. supervisor heartbeat
                 # races a manual reconnect) used to spawn two feed tasks
                 # against the same IB instance — duplicate subscriptions,
                 # double-counted ticks.
                 '_reconnect_lock',
                 # Snapshot-write diagnostics. The aggregator depends on
                 # this file being fresh; without a visible failure count
                 # a silently-broken writer is invisible until you wonder
                 # why dashboard_agg looks frozen.
                 '_snapshot_ok', '_snapshot_fail',
                 # Render-loop crash counter. The render path is wrapped in
                 # try/except so a render bug (e.g. mixed-tz datetime sub)
                 # can never kill the trading loop. Surface the count on
                 # shutdown so the user knows if frames were silently lost.
                 '_render_err_count',
                 # AlertManager wired to file + stdout + optional Slack.
                 # Engine pulls a reference for health-check + tripwire alerts.
                 'alerts',
                 # PortfolioReader for cross-bot risk gates. Reads peer
                 # .gt_state_*.json + .gt_live_*.json files to surface
                 # combined exposure + portfolio P&L so the $25k/-$2k
                 # gates apply across every running instance, not just
                 # this one.
                 'portfolio',
                 # PortfolioLimitsReader picks up shared cap overrides
                 # from .gt_portfolio_limits.json — set centrally (e.g.
                 # `dashboard_agg.py --exposure 50000`) and every bot
                 # respects it within ~1s, no relaunch required.
                 'portfolio_limits')

    def __init__(self, config: Config):
        self.config = config
        self._running = False

        # Gateway
        self.gateway = Gateway(
            host=config.ibkr_host,
            port=config.ibkr_port,
            client_id=config.ibkr_client_id,
            symbol=config.ticker,
            paper=config.paper_trading,
            cfd=getattr(config, 'cfd', False),
        )

        # Connection manager
        self.conn_mgr = ConnectionManager(
            ConnectionConfig(
                host=config.ibkr_host,
                port=config.ibkr_port,
                client_id=config.ibkr_client_id,
                max_reconnect_attempts=0,  # 0 means retry infinitely
            )
        )

        # Feed handler - created AFTER connect (needs ib reference)
        self.feed_handler: Optional[FeedHandler] = None
        # Production feed pipeline — built by setup_feed(); reconnect handler
        # checks `is not None` rather than hasattr so the lifecycle is explicit.
        self.production_feed: Optional[ProductionFeed] = None
        # Handle to the running feed task so stop() can cancel cleanly.
        self._feed_task: Optional[asyncio.Task] = None
        # Reconnect serializer — created lazily on first use because at
        # __init__ time there is no running event loop yet.
        self._reconnect_lock: Optional[asyncio.Lock] = None
        # Snapshot writer diagnostics.
        self._snapshot_ok: int = 0
        self._snapshot_fail: int = 0
        # Render-loop crash counter (incremented inside render_dashboard
        # try/except so a render exception never kills the trading loop).
        self._render_err_count: int = 0

        # Logger
        self.logger = QuantLogger(trade_cycle_id=f"LIVE_{config.ticker}")

        # State persistence — namespaced by symbol AND clientId so multiple
        # processes (one per ticker) don't clobber each other's state files.
        # Recipe: spawn separate `run_live.py SYMBOL --client-id N` processes
        # in tmux panes; each gets its own .gt_state_SYMBOL_N.json. See
        # README's "Running multiple symbols" section.
        state_file = f".gt_state_{config.ticker}_{config.ibkr_client_id}.json"
        self.state_store = StateStore(path=state_file)
        self.audit_log = AuditLog()

        # Comprehensive audit system (non-blocking, multi-threaded)
        self.audit = AuditManager(
            symbol=config.ticker,
            enabled=True,
            directory="data/audit",
            pnl_interval=5.0,  # PnL snapshot every 5 seconds
        )

        # Alerting: file + stdout (Slack inert unless GT_SLACK_WEBHOOK set).
        # Engine reads `alerts` for tripwire + health-check escalation.
        self.alerts = build_default_alert_manager(directory="data/alerts")

        # Engine
        self.engine = Engine(
            config=config,
            gateway=self.gateway,
            state_store=self.state_store,
            audit_log=self.audit_log,
            audit=self.audit,
            logger=self.logger,
        )
        # Hand the AlertManager to the engine so its health-check loop,
        # session controller, and reject-path alerts have somewhere to fire.
        self.engine._alerts = self.alerts

        # Risk (ultra-fast, cached values). PortfolioReader scans peer
        # .gt_state_*_*.json + .gt_live_*_*.json files in cwd so the
        # combined-exposure and daily-loss gates apply at the PORTFOLIO
        # level — every concurrent run_live.py contributes. Same files
        # dashboard_agg.py already aggregates for its header pills.
        self.portfolio = PortfolioReader()
        self.portfolio_limits = PortfolioLimitsReader()
        self.risk = RiskCheck(config=config, gateway=self.gateway,
                              portfolio=self.portfolio,
                              limits=self.portfolio_limits)
        self.engine.risk = self.risk

        # Dashboard state
        self.state = State(engine=self.engine)
        self.state.symbol = config.ticker
        self.state.client_id = config.ibkr_client_id

        # Wire AlertManager into the dashboard's AlertView so the alerts
        # panel can tail the most recent N events. Same `alerts` instance
        # the engine writes to — no new subscription needed.
        self.state.alerts.attach(self.alerts)

        # Capture engine logs into the dashboard's event stream instead of
        # letting them scroll past the dashboard. The deque is bounded so
        # we won't leak memory even on a long session with lots of events.
        self.engine.set_log_callback(self.state.events.push)

    async def connect(self) -> bool:
        """Connect gateway to IBKR and start connection supervisor.

        ConnectionManager runs in supervisor mode: it watches `gateway._ib`
        via a 5s heartbeat. When the heartbeat detects a dropped connection,
        it invokes our `_reconnect_gateway` callback (which re-establishes
        Gateway), then `_after_reconnect` (which re-subscribes the feed and
        tells the engine to reconcile resting orders). Backoff: 1s → 60s,
        retrying indefinitely until TWS comes back or the operator kills the
        process. The GTC stop-limit at IBKR continues protecting the position
        regardless of bot state during the disconnect window.
        """
        print(f"{C}Connecting to IBKR on port {self.config.ibkr_port}...{R}", end='', flush=True)

        if not await self.gateway.connect():
            print(f"\n{R_}Failed to connect! Status: {self.gateway.status}{R}")
            return False

        print(f" {G}Connected{R}")

        # Engage the supervisor — heartbeat + reconnect callbacks. The
        # supervisor never owns `_ib`; it just watches Gateway's instance
        # and asks us to repair it via callbacks. `on_reconnect_async` fires
        # after each successful reconnect to rebuild the feed and reconcile
        # resting orders.
        await self.conn_mgr.supervise(
            ib=self.gateway._ib,
            on_connect_async=self._reconnect_gateway,
            on_reconnect_async=self._after_reconnect,
        )

        # Arm the engine's stale-feed watchdog. The supervisor's heartbeat
        # only fires on isConnected() == False; a socket that stays up but
        # stops delivering ticks is invisible to it. The engine already
        # detects that (health Probe 3 → STALE_FEED) but owns neither the
        # feed nor the supervisor, so it needs this hook to act on what it
        # sees. request_repair drives the same _reconnect_gateway +
        # _after_reconnect pair the heartbeat would — critically including
        # _after_reconnect, which rebuilds the FeedHandler against the new
        # IB instance. A bare gateway.connect() would leave the handler
        # bound to the dead instance and never restore ticks.
        # Wired only here (live path); paper/test engines leave it unset
        # and keep the old detection-only behaviour.
        self.engine.set_stale_feed_repair(self.conn_mgr.request_repair)

        return True

    async def _reconnect_gateway(self) -> bool:
        """Supervisor callback: re-establish Gateway after a disconnect.

        If a FULLY-established connection already exists (the engine's
        active-reconnect beat us to it), adopts it and returns True without
        tearing anything down. Otherwise disconnects cleanly first (releases
        any half-open ib_async state), then reconnects. Either way the
        supervisor's IB reference is updated so the heartbeat loop watches
        the right object.

        On each failure, fires a HIGH alert so the operator can intervene.
        The supervisor retries indefinitely (max_reconnect_attempts=0), so
        there is no exhaustion path in the live config — a sustained outage
        shows up as repeated HIGH alerts rather than a single terminal
        CRITICAL.
        """
        # Idempotent guard: if a FULLY-established connection already exists —
        # e.g. the engine's active-reconnect (_try_active_reconnect) beat us to
        # it while we slept on our backoff — ADOPT it instead of tearing it
        # down. Skipping this takes a certain, gratuitous outage: disconnect()
        # below closes a healthy connection and forces a full re-handshake for
        # no reason (and can further snag on IBKR clientId reuse after an
        # unclean teardown).
        #
        # Returning True is what makes this safe: the supervisor's success path
        # then runs _after_reconnect(), which rebuilds the FeedHandler against
        # the adopted IB. Without that the engine's bare gateway.connect() would
        # leave the handler bound to the dead instance — connected, but no ticks.
        #
        # BOTH conditions are load-bearing:
        #   isConnected()          → the socket is open (necessary, not enough)
        #   self.gateway.connected → _status == CONNECTED, i.e. the handshake
        #                            actually finished (contract qualified,
        #                            account summary + P&L subscribed).
        # Socket-open-but-handshake-incomplete is a real, reachable state — an
        # aborted/hung connect (see Gateway.CONNECT_TIMEOUT_S) leaves the socket
        # up with steps 2-6 unrun and _status=ERROR. Adopting that would park
        # the bot "connected but half-configured": no qualified contract, stale
        # equity, can't place orders properly.
        ib = getattr(self.gateway, "_ib", None)
        if ib is not None and ib.isConnected() and self.gateway.connected:
            print(f"{G}[Reconnect] Gateway already connected — adopting live socket{R}")
            self.conn_mgr.set_ib(ib)
            return True

        print(f"\n{Y}[Reconnect] Re-establishing Gateway...{R}")
        try:
            await self.gateway.disconnect()
        except Exception as e:
            print(f"{Y}[Reconnect] disconnect cleanup error (continuing): {e}{R}")
        ok = await self.gateway.connect()
        if ok:
            print(f"{G}[Reconnect] Gateway reconnected{R}")
            self.conn_mgr.set_ib(self.gateway._ib)
        else:
            print(f"{R_}[Reconnect] Gateway reconnect FAILED{R}")
            if self.alerts:
                # First-failure alert. The supervisor will retry; if it
                # exhausts attempts, it sets state=FAILED and stops trying.
                # We fire HIGH (not CRITICAL) on each individual failure
                # because the supervisor's full retry budget hasn't been
                # spent yet. On exhaustion the supervisor logs FAILED and
                # the heartbeat loop exits — engine continues with no IBKR
                # connection, but resting GTC orders still protect the
                # position. Operator should investigate within minutes.
                self.alerts.raise_alert(
                    code="CONNECTION_LOST",
                    severity=AlertSeverity.HIGH,
                    message=f"Gateway reconnect attempt failed for {self.config.ticker}.",
                    context={"host": self.config.ibkr_host, "port": self.config.ibkr_port,
                             "client_id": self.config.ibkr_client_id, "ticker": self.config.ticker},
                    correlation_id=getattr(self.engine, '_cycle_id', ''),
                )
        return ok

    async def _after_reconnect(self) -> None:
        """Supervisor callback: re-subscribe market data and reconcile.

        After Gateway is back up, the FeedHandler still references the OLD
        `ib_async.IB` instance — its subscriptions are stale. We tear it
        down, build a fresh one, re-subscribe the symbol, and ask the
        engine to reconcile (re-attaches fill events to resting GTC
        orders so the engine state machine keeps advancing correctly).

        Serialized by `_reconnect_lock` so two reconnect callbacks firing
        back-to-back can't race and spawn duplicate feed tasks against the
        same IB instance.
        """
        if self._reconnect_lock is None:
            self._reconnect_lock = asyncio.Lock()
        async with self._reconnect_lock:
            # Tear down the prior feed task — without this, a fresh
            # production_feed.start() task would run alongside the old one
            # and both would dispatch ticks to the engine.
            if self._feed_task is not None and not self._feed_task.done():
                self._feed_task.cancel()
                try:
                    await self._feed_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._feed_task = None

            try:
                if self.production_feed is not None:
                    await self.production_feed.stop()
            except Exception:
                pass

            # Rebuild feed against the new IB instance and re-subscribe.
            await self.setup_feed()
            await self.production_feed.subscribe(self.config.ticker)
            # Start the new feed (mirrors what start() does for the initial
            # subscription). Captured so a subsequent shutdown can cancel it.
            self._feed_task = asyncio.create_task(self.production_feed.start())

            # Re-link engine to broker-side resting orders. Reconcile is paper-aware
            # (no-op in paper). Engine.start has already run once; we only need the
            # reconciliation step.
            try:
                await self.engine._reconcile_open_orders()
                print(f"{G}[Reconnect] Reconciled resting orders with broker{R}")
            except Exception as e:
                print(f"{R_}[Reconnect] Reconcile failed: {e}{R}")

    async def setup_feed(self) -> ProductionFeed:
        """Set up feed → pipeline → strategy."""
        # Feed handler - created here (AFTER gateway connect)
        self.feed_handler = FeedHandler(self.gateway._ib, self.conn_mgr)

        # ProductionFeed wires: FeedHandler + PipelineChain + dashboard callback
        def strategy_callback(tick: Tick):
            # Heartbeat on EVERY tick — must run BEFORE the `last > 0` gate.
            # Quote-driven assets (index/FX CFDs) have no trade prints, so
            # update_price() below never fires for them; without this the
            # risk gate's _is_price_fresh() freezes at connect time and
            # blocks all entries with "Price stale" ~60s after startup.
            self.gateway.mark_data_alive()
            # Update gateway cached price for paper fills
            if tick.last > 0:
                self.gateway.update_price(tick.last)
            # engine.on_tick is now SYNC: it pushes onto an internal bounded
            # queue and returns immediately. A single consumer task inside
            # the engine drains the queue. We used to do
            # `asyncio.create_task(...)` here — that spawned a task per tick
            # with no upper bound (~10K tasks/sec on NVDA), drowning the loop
            # under any sustained load. The queue caps the in-flight work.
            self.engine.on_tick(tick)

        # Dashboard callback - feed stats update at 10Hz
        def dashboard_callback(tick: Tick):
            self.state.on_tick(tick)

        # Quote-driven assets (index/FX CFDs, spot FX) never print a trade, so
        # `last` stays 0. Tell the pipeline to forward their bid/ask-only ticks
        # to the strategy; otherwise the engine gets NO ticks (no on_tick, no
        # heartbeat → "Price stale" blocks, no feed.csv). Equities keep last>0
        # ticks, so this stays False for them (no behaviour change).
        quote_driven = False
        try:
            from src.assets import resolve as _resolve_spec
            from src.assets.enum import AssetClass as _AC
            _ac = _resolve_spec(self.config.ticker).asset_class
            quote_driven = _ac in (_AC.FX_CASH, _AC.FX_CFD, _AC.INDEX_CFD)
        except Exception:
            quote_driven = False

        self.production_feed = ProductionFeed(
            feed_handler=self.feed_handler,
            strategy_callback=strategy_callback,
            dashboard_callback=dashboard_callback,
            forward_quote_ticks=quote_driven,
        )

        # Wire production_feed to engine for latency stats access
        self.engine._feed = self.production_feed

        return self.production_feed

    def _print_naked_position_banner(self, err: 'NakedPositionError') -> None:
        """Render a loud red banner explaining the naked-position refusal
        and the operator's recovery options. Called when engine.start()
        raises NakedPositionError — process is about to exit with code 1
        so the operator MUST see and read this.

        Visual: thick double border, red ANSI bold, all the diagnostics
        the operator needs to pick a recovery path. Width matches the
        live dashboard (140 chars) so it doesn't look out of place when
        printed mid-terminal."""
        bar = "═" * 78
        # Pull the state-file path from the engine's config for the
        # operator's "(c) edit the state file" recovery suggestion.
        state_file = f".gt_state_{err.ticker}_{err.client_id}.json"
        live_file = f".gt_live_{err.ticker}_{err.client_id}.json"
        print()
        print(f"{R_}{B}╔{bar}╗{R}")
        print(f"{R_}{B}║{R} {R_}{B}REFUSING TO START — NAKED POSITION DETECTED{R}                                  {R_}{B}║{R}")
        print(f"{R_}{B}╠{bar}╣{R}")
        print(f"{R_}{B}║{R} Broker holds {Y}{err.broker_qty}{R} shares of {Y}{B}{err.ticker}{R}, but this client_id has no record of opening")
        print(f"{R_}{B}║{R} that position. Starting the bot would risk DOUBLE EXPOSURE on the next breakdown.")
        print(f"{R_}{B}║{R}")
        print(f"{R_}{B}║{R} {D}Diagnostics:{R}")
        print(f"{R_}{B}║{R}   broker qty      : {Y}{err.broker_qty}{R}")
        print(f"{R_}{B}║{R}   saved qty       : {D}{err.saved_qty}{R}")
        print(f"{R_}{B}║{R}   saved entry     : {D}${err.saved_entry or 0:.2f}{R}")
        print(f"{R_}{B}║{R}   client_id       : {D}{err.client_id}{R}")
        print(f"{R_}{B}║{R}   state file      : {D}{state_file}{R}")
        print(f"{R_}{B}║{R}")
        print(f"{R_}{B}║{R} {C}{B}Recovery options (pick ONE):{R}")
        print(f"{R_}{B}║{R}   {G}(a){R} Flatten + start clean — closes the position at market:")
        print(f"{R_}{B}║{R}       {D}python run_live.py {err.ticker} --reset --port <port> --client-id {err.client_id}{R}")
        print(f"{R_}{B}║{R}   {G}(b){R} Manually close the position in TWS, then re-run normally.")
        print(f"{R_}{B}║{R}   {G}(c){R} If this position is YOUR bot's from a previous run (different client_id),")
        print(f"{R_}{B}║{R}       use that previous client_id to resume:")
        print(f"{R_}{B}║{R}       {D}python run_live.py {err.ticker} --port <port> --client-id <OLD_CID>{R}")
        print(f"{R_}{B}║{R}   {G}(d){R} If you KNOW this position is yours and want this client_id to adopt it,")
        print(f"{R_}{B}║{R}       edit {D}{state_file}{R} so its `quantity` + `entry_price`")
        print(f"{R_}{B}║{R}       match the broker, then re-run.")
        print(f"{R_}{B}║{R}")
        print(f"{R_}{B}║{R} No feed subscribed. No dashboard. No orders placed. Exiting with code 1.")
        print(f"{R_}{B}╚{bar}╝{R}")
        print()

    def _print_conflicting_order_banner(self, err: 'ConflictingOpenOrderError') -> None:
        """Render the banner for the conflicting-pending-order case.

        Different shape from the naked-position banner: there's no filled
        position to flatten — there's an order resting at IBKR placed by
        a different client_id (or manual TWS). Recovery is to cancel the
        conflicting order at its originating client, or let it
        fill / expire / be cancelled in TWS.
        """
        bar = "═" * 78
        print()
        print(f"{R_}{B}╔{bar}╗{R}")
        print(f"{R_}{B}║{R} {R_}{B}REFUSING TO START — CONFLICTING OPEN ORDER AT BROKER{R}                         {R_}{B}║{R}")
        print(f"{R_}{B}╠{bar}╣{R}")
        print(f"{R_}{B}║{R} Another API client (or manual TWS) has an active {Y}{B}{err.ticker}{R} order resting at")
        print(f"{R_}{B}║{R} the broker. Two clients armed at the same trigger would DOUBLE-FILL on")
        print(f"{R_}{B}║{R} crossing — fill events route to the originating client only, so we can't")
        print(f"{R_}{B}║{R} adopt it the way we'd adopt an orphan position.")
        print(f"{R_}{B}║{R}")
        print(f"{R_}{B}║{R} {D}Our client_id : {err.client_id}{R}")
        print(f"{R_}{B}║{R} {D}Conflicting orders ({len(err.orders)}):{R}")
        for o in err.orders:
            print(
                f"{R_}{B}║{R}   "
                f"{Y}{o.get('action','?'):4}{R} {Y}{o.get('qty','?'):>4}{R}  "
                f"trig=${o.get('stop_price') or 0:>8.2f}  "
                f"lim=${o.get('limit_price') or 0:>8.2f}  "
                f"{D}cid={o.get('owning_client_id','?')}  "
                f"id={o.get('broker_id','?')}  "
                f"status={o.get('status','?')}{R}"
            )
        print(f"{R_}{B}║{R}")
        print(f"{R_}{B}║{R} {C}{B}Recovery options (pick ONE):{R}")
        print(f"{R_}{B}║{R}   {G}(a){R} Cancel the conflicting order(s) in TWS, then re-run this command.")
        print(f"{R_}{B}║{R}   {G}(b){R} Cancel programmatically from the owning client_id:")
        for o in err.orders[:2]:   # show up to 2 examples to keep the banner short
            owner_cid = o.get('owning_client_id', '?')
            print(
                f"{R_}{B}║{R}       {D}python run_live.py {err.ticker} --reset "
                f"--port <port> --client-id {owner_cid}{R}"
            )
        print(f"{R_}{B}║{R}   {G}(c){R} Re-run THIS process using the existing owning client_id instead, so")
        print(f"{R_}{B}║{R}       the order rebinds to us via reconcile (no double-arm).")
        print(f"{R_}{B}║{R}   {G}(d){R} Wait for the order to fill / expire / be cancelled in TWS, then re-run.")
        print(f"{R_}{B}║{R}")
        print(f"{R_}{B}║{R} No feed subscribed. No dashboard. No orders placed. Exiting with code 1.")
        print(f"{R_}{B}╚{bar}╝{R}")
        print()

    async def start(self):
        """Start trading session."""
        self._running = True

        # Connect
        if not await self.connect():
            return False

        # Setup feed pipeline (AFTER connect so gateway._ib is set)
        feed = await self.setup_feed()
        await feed.subscribe(self.config.ticker)
        print(f"{C}Feed pipeline: Sequence → Dedup → FastValidator{R}")

        # State restore happens inside engine.start() via _load_state(). The
        # previous double-load (here, then again inside start()) caused the
        # second pass to overwrite this one's recalculated stop_loss with
        # the saved value — silently losing any --stop change between
        # sessions. Single load path now.
        #
        # engine.start() raises NakedPositionError when the broker holds a
        # position this client_id doesn't know about. We catch + print a
        # loud banner + return False — no feed-handler ticks consumed, no
        # dashboard render loop entered, no orders. The caller (main) will
        # exit with code 1.
        try:
            await self.engine.start()
        except NakedPositionError as e:
            self._print_naked_position_banner(e)
            try:
                await self.gateway.disconnect()
            except Exception:
                pass
            return False
        except ConflictingOpenOrderError as e:
            # Same handler shape — different banner — same exit semantics.
            # Another client has a resting order at IBKR for this ticker;
            # starting now would arm two clients at the same trigger and
            # double-fill on crossing.
            self._print_conflicting_order_banner(e)
            try:
                await self.gateway.disconnect()
            except Exception:
                pass
            return False

        if self.engine._state != TradeState.IDLE:
            print(f"{Y}State restored: {self.engine._state.value}{R}")

        # Spec-aware startup banner. Equity stays at 2dp; FX shows the
        # full pip grid (5dp for majors, 3dp for JPY pairs). Also
        # snap-warns if the user passed a price that isn't on the tick
        # grid (e.g. --trigger 1.17002 on EURUSD whose tick is 0.00005
        # — gets silently rounded to 1.17000, which is surprising if
        # you don't see the warning).
        spec = getattr(self.engine, '_asset_spec', None)
        if spec is not None:
            try:
                from src.assets.types import price as _to_price
                from src.assets.policies.tick import RoundDirection as _RD
                dec = int(spec.tick.decimals_for_display(_to_price(1.0)))
                tick = float(spec.tick.tick) if hasattr(spec.tick, 'tick') else 0.01
                raw = float(self.config.trigger_price)
                snapped = float(spec.tick.round_to_tick(_to_price(str(raw)), _RD.NEAREST))
                if abs(snapped - raw) > tick * 0.001:  # genuine snap
                    print(
                        f"  {Y}WARNING: --trigger {raw:.{dec}f} is NOT on the "
                        f"{self.config.ticker} tick grid (tick={tick:.{dec}f}). "
                        f"Engine will use {snapped:.{dec}f}. Pass a price ending in "
                        f"{tick:.{dec}f} multiples to be explicit.{R}"
                    )
            except Exception:
                dec = 2  # safe fallback
        else:
            dec = 2

        print(f"\n{G}Trading active!{R}")
        print(f"  Symbol: {self.config.ticker}")
        if getattr(self.config, 'entry_market', False):
            # Say it loudly: this entry has no price floor, and the trigger
            # below is a reference level, not something the broker waits for.
            print(f"  Entry: {Y}MARKET on placement (--market){R} — no resting "
                  f"trigger, no price floor")
            print(f"  Trigger: ${self.config.trigger_price:.{dec}f} "
                  f"(reference only; sizes the protective cover)")
        else:
            print(f"  Trigger: ${self.config.trigger_price:.{dec}f}")
        # Compute the protective stop preview at the same precision —
        # equity rounds to 2dp like before, FX shows the full 5dp
        # estimate so the operator sees how close to the spread they are.
        # SHORT INVERSION (P10): the protective BUY cover sits ABOVE entry → (1 + pct).
        stop_est = self.config.trigger_price * (1 + self.config.stop_loss_pct)
        if spec is not None:
            try:
                from src.assets.types import price as _to_price2
                from src.assets.policies.tick import RoundDirection as _RD2
                stop_est = float(spec.tick.round_to_tick(_to_price2(str(stop_est)), _RD2.NEAREST))
            except Exception:
                stop_est = round(stop_est, dec)
        else:
            stop_est = round(stop_est, dec)
        print(f"  Direction: {R_}SHORT{R} (SELL breakdown entry, BUY cover stop)")
        print(f"  Stop (BUY cover): ${stop_est:.{dec}f} ({self.config.stop_loss_pct * 100:.1f}% above entry)")
        print(f"  Qty: {self.config.quantity}")
        print(f"  Paper: {self.config.paper_trading}")

        # Pre-market / after-hours hint. IBKR delivers BBO outside RTH but
        # last-trade ticks are sparse to non-existent until 09:30 ET; an
        # LTP of $0.00 on the dashboard then is normal, not a feed bug.
        # Use ZoneInfo("America/New_York") so EST/EDT switches correctly;
        # the previous hardcoded UTC-4 wrongly drifted by an hour in winter.
        if _ET is not None:
            et = datetime.now(_ET)
        else:
            et = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-5)))
        is_weekday = et.weekday() < 5
        is_rth = is_weekday and (et.replace(hour=9, minute=30) <= et < et.replace(hour=16, minute=0))
        if not is_rth:
            print(
                f"  {Y}NOTE: outside regular US trading hours "
                f"({et.strftime('%H:%M ET')}). BBO may stream but LTP "
                f"will be sparse until 09:30 ET — \"$----\" in the dashboard "
                f"is expected pre-market.{R}"
            )
        print(f"\n Ctrl+C to stop{R}\n")

        # Start feed (keeps feed running until stopped). Capture the handle
        # so trader.stop() can cancel + await it; otherwise this task gets
        # orphaned when the event loop closes during shutdown.
        self._feed_task = asyncio.create_task(feed.start())

        return True

    async def stop(self):
        """Stop and cleanup — ordered shutdown.

        Order matters here:
            1. Mark `_running = False` so the main render loop exits.
            2. Cancel + await the feed task. Stopping the feed BEFORE the
               engine prevents one last burst of ticks from racing the
               engine's tick consumer shutdown.
            3. Stop the production feed pipeline (cancels its dispatch task).
            4. Stop the engine — cancels the bounded tick consumer and the
               daily-reset scheduler; both await cleanly.
            5. Stop the connection supervisor — cancels the heartbeat task
               so it doesn't try to call reconnect callbacks against a
               half-disconnected gateway.
            6. Disconnect the gateway last so prior cleanup steps can still
               touch broker state if needed.

        Every step is wrapped to swallow individual errors — partial
        shutdown should still try to flush as much as possible. The
        outer caller uses asyncio.shield to keep this whole sequence
        running even if main() itself was cancelled (Ctrl+C).
        """
        self._running = False

        # 1. Cancel feed task handle (orphans previously caused "destroyed
        # but pending" warnings on shutdown).
        if self._feed_task is not None and not self._feed_task.done():
            self._feed_task.cancel()
            try:
                await self._feed_task
            except (asyncio.CancelledError, Exception):
                pass
            self._feed_task = None

        # 2. Stop pipeline / its dispatcher (no-op if feed never started).
        if self.production_feed is not None:
            try:
                await self.production_feed.stop()
            except Exception as e:
                print(f"{Y}[stop] production_feed.stop error (continuing): {e}{R}")

        # 3. Stop engine — cancels tick consumer + daily-reset scheduler.
        try:
            await self.engine.stop()
        except Exception as e:
            print(f"{Y}[stop] engine.stop error (continuing): {e}{R}")

        # 4. Stop connection supervisor — cancels heartbeat task. New in
        # this patch: previously trader.stop() forgot the supervisor, so
        # ConnectionManager._heartbeat_loop ran until the event loop closed
        # under it, producing the "Task was destroyed but it is pending"
        # warning visible in your last run.
        try:
            await self.conn_mgr.disconnect("trader.stop")
        except Exception as e:
            print(f"{Y}[stop] conn_mgr.disconnect error (continuing): {e}{R}")

        # 5. Drop the broker connection.
        try:
            await self.gateway.disconnect()
        except Exception as e:
            print(f"{Y}[stop] gateway.disconnect error (continuing): {e}{R}")

        print(f"{D}Trader stopped.{R}")

    def render_dashboard(self, first=False):
        """Render dashboard frame.

        First render: full clear (HOME + erase screen) to avoid artifacts.
        Subsequent renders: HOME only — overwrite in-place at same height.

        Wrapped in try/except so the trading loop is NEVER killed by a
        render-side bug. A render exception is logged once (rate-limited)
        and the next frame just tries again — the engine, feed, audit and
        snapshot writer all keep running underneath. Without this guard,
        ANY exception in sync_engine() or build_frame() (e.g. a tz-aware
        vs naive datetime subtraction on a reconciled order) takes the
        whole process down on the very first frame.
        """
        try:
            self.state.sync_engine()
            frame = build_frame(self.state)
            if first:
                sys.stdout.write(HOME + CLEAR_SCREEN + frame)
            else:
                sys.stdout.write(HOME + frame)
            sys.stdout.write('\n')
            sys.stdout.flush()
        except Exception as e:
            import traceback
            # Rate-limited so a persistent render bug doesn't drown the
            # terminal — every 100th occurrence + the very first frame.
            self._render_err_count += 1
            n = self._render_err_count
            if n == 1 or n % 100 == 0:
                sys.stderr.write(
                    f"[render] frame {n} failed: {type(e).__name__}: {e}\n"
                )
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()

    def get_summary(self) -> dict:
        """Get session summary."""
        status = self.engine.get_status()
        lat = self.production_feed.get_latency_stats() if hasattr(self, 'production_feed') else {}
        return {**status, **lat}

    def write_live_snapshot(self) -> None:
        """Atomically write a live tick + microstructure snapshot to disk.

        Consumed by `dashboard_agg.py` — the multi-symbol aggregator polls
        this file every 200ms to render an external dashboard that doesn't
        share this process's IBKR connection. The aggregator can drill into
        any running symbol and see live BBO + tape + latency + rates
        without needing a second IBKR subscription.

        Cost: one JSON dump (~2 KB) + temp-file rename every 200ms.
        Measured at ~150-400 µs per call on a Mac M1 SSD. Negligible
        vs the 200ms cadence; called from the main render loop alongside
        the dashboard render, not from the tick hot path.

        Atomic via temp+rename — readers always see either an old or new
        complete file, never a partial write. No fsync (this is high-
        frequency ephemeral data, durability not needed).

        File naming mirrors the state file: `.gt_live_<SYMBOL>_<CLIENTID>.json`
        in the working directory.
        """
        import json as _json
        import os as _os
        try:
            f = self.state.feed
            m = self.state.micro
            # Pull equity + buying power from the risk module's caches
            # (1s TTL, both refreshed lazily). Aggregator uses these so its
            # drill-down view shows real account numbers, and so the EXP
            # and BP USED pills render in the multi-symbol header.
            try:
                equity = self.risk._get_cached_equity() if self.risk else 0.0
            except Exception:
                equity = 0.0
            try:
                buying_power = self.risk._get_cached_buying_power() if self.risk else 0.0
            except Exception:
                buying_power = 0.0

            # Daily P&L basis = IBKR `realizedPnL` (today's closed-trade P&L,
            # account-wide, auto-resets each session — matches TWS "Realized").
            # Same number the $2k gate uses, so the header pill and the circuit
            # breaker always agree. We avoid dailyPnL: on FX-heavy accounts it
            # reports a gross, unstable value. None until reqPnL lands → "--".
            try:
                daily_pnl_ibkr = self.gateway.get_realized_pnl()
            except Exception:
                daily_pnl_ibkr = None

            # Position notional (qty × current LTP) — written once here so
            # the aggregator can sum it across symbols without re-pulling LTP.
            # Falls back to 0 when flat or LTP unknown. Engine's `_quantity`
            # is the live position size (0 when FLAT) — distinct from
            # config.quantity which is the *intended* trade size.
            try:
                open_qty = int(getattr(self.engine, '_quantity', 0) or 0)
            except Exception:
                open_qty = 0
            last_px = getattr(self.state.feed, 'last', 0.0) or 0.0

            # ── Multi-asset notional (D4-PM) ─────────────────────────
            # Route through spec.sizing.notional() so futures pick up
            # their multiplier (ES = qty × price × 50, not just qty ×
            # price) and FX uses the right quote-currency math.
            # For equity / USD-quoted CFDs the result is identical to
            # the legacy `qty * last_px` so equity dashboards are
            # unchanged. Non-USD-quoted assets (USDJPY → JPY) need
            # CurrencyService conversion to base — D5 work.
            position_notional = 0.0
            if open_qty > 0 and last_px > 0:
                _spec = getattr(self.engine, '_asset_spec', None)
                if _spec is not None:
                    try:
                        from src.assets.types import Quantity as _Q, price as _P
                        from decimal import Decimal as _D
                        qty_typed = _Q(_D(str(open_qty)), _spec.sizing.expected_unit)
                        n_money = _spec.sizing.notional(qty_typed, _P(str(last_px)))
                        position_notional = float(n_money.amount)
                    except Exception:
                        # Spec sizing failed (wrong unit? bad price?) —
                        # fall through to legacy math.
                        position_notional = open_qty * last_px
                else:
                    position_notional = open_qty * last_px
                # ── FX → USD normalization (live regression 2026-06-09) ──
                # spec.sizing.notional returns Money in QUOTE ccy. For
                # USDJPY/EURJPY/USDCAD/USDCHF etc the raw amount is in JPY
                # /CAD/CHF and would inflate the portfolio risk sum (4M JPY
                # treated as $4M). Convert via the ticker-shape heuristic so
                # PortfolioReader sees a USD-comparable number.
                try:
                    from src.strategy.risk import RiskCheck as _RC
                    fx_usd = _RC._fx_to_usd_notional(
                        self.config.ticker, last_px, open_qty,
                    )
                    if fx_usd is not None:
                        position_notional = fx_usd
                except Exception:
                    pass

            # ── Pending notional ─────────────────────────────────────────
            # Capital that is COMMITTED but not yet deployed: a working BUY
            # entry resting at IBKR that hasn't filled. Without booking
            # this, the portfolio risk gate sees a fellow bot's pending
            # $19k BUY as "$0 of exposure" — and lets a second bot stack a
            # new entry that would breach the cap on both fills.
            # (Today's PLTR session at 14:53:35 sat with a working
            # $15k BUY for ~2 minutes while another ticker's snapshot saw
            # zero contribution from it.)
            #
            # Source: engine._pending_stop with side=BUY. Worst-case
            # capital outlay is qty × limit_price (STP_LMT BUY can fill
            # anywhere from stop to limit; limit is the upper bound).
            # Stop-only BUY (rare) falls back to stop_price. Bracket
            # parents and legacy SL fallbacks both flow through
            # _pending_stop, so this covers all entry shapes.
            #
            # Snapshot writes raw `pending_notional`; PortfolioReader sums
            # `position_notional + pending_notional` across every live
            # snapshot, so cross-bot aggregation is automatic.
            pending_notional = 0.0
            try:
                ps = getattr(self.engine, '_pending_stop', None)
                if ps:
                    side = ps.get('side')
                    side_str = side.value if hasattr(side, 'value') else str(side)
                    # SHORT INVERSION (P11): book only the pending ENTRY, which
                    # is now a SELL stop-limit (was BUY in the long form). The
                    # post-fill BUY cover that also lands in _pending_stop is the
                    # protective leg and is already counted in position_notional,
                    # so booking it would double-count. Booking 'BUY' here meant
                    # pending short entries contributed $0 to the exposure cap.
                    if side_str == 'SELL':
                        p_qty = int(ps.get('qty', 0) or 0)
                        # Prefer LIMIT (worst-case fill); fall back to STOP
                        p_price = float(
                            ps.get('limit_price')
                            or ps.get('stop_price')
                            or 0.0
                        )
                        if p_qty > 0 and p_price > 0:
                            # Same spec-routed math as position_notional —
                            # critical for futures so pending counts the
                            # correct multiplied notional.
                            _spec = getattr(self.engine, '_asset_spec', None)
                            if _spec is not None:
                                try:
                                    from src.assets.types import (
                                        Quantity as _Q, price as _P,
                                    )
                                    from decimal import Decimal as _D
                                    qty_typed = _Q(
                                        _D(str(p_qty)),
                                        _spec.sizing.expected_unit,
                                    )
                                    pn_money = _spec.sizing.notional(
                                        qty_typed, _P(str(p_price)),
                                    )
                                    pending_notional = float(pn_money.amount)
                                except Exception:
                                    pending_notional = p_qty * p_price
                            else:
                                pending_notional = p_qty * p_price
                            # FX → USD normalization (live regression 2026-06-09)
                            # — same rationale as position_notional above.
                            try:
                                from src.strategy.risk import RiskCheck as _RC
                                fx_usd = _RC._fx_to_usd_notional(
                                    self.config.ticker, p_price, p_qty,
                                )
                                if fx_usd is not None:
                                    pending_notional = fx_usd
                            except Exception:
                                pass
            except Exception:
                # Defensive — never let snapshot writes crash the engine
                pending_notional = 0.0

            # Two utilization ratios, pre-computed.
            # Exposure reflects FILLED position only (position_notional) —
            # a working-but-unfilled entry does NOT count toward exposure.
            # pending_notional is still written to the snapshot for
            # visibility, but it is deliberately excluded from these
            # utilization ratios and from the portfolio risk-gate total.
            exposure_pct = (position_notional / equity * 100.0) if equity > 0 else 0.0
            bp_used_pct = (position_notional / buying_power * 100.0) if buying_power > 0 else 0.0
            snap = {
                'ts': datetime.now().isoformat(),
                'symbol': self.config.ticker,
                # Top-of-book + session refs
                'last': f.last, 'bid': f.bid, 'ask': f.ask,
                'bid_size': f.bid_size, 'ask_size': f.ask_size,
                'volume': f.volume,
                'open': f.open_px, 'high': f.high, 'low': f.low,
                'rate': f.rate,
                # Config — useful for aggregator to show trigger price etc.
                'trigger_price': self.config.trigger_price,
                'stop_loss_pct': self.config.stop_loss_pct,
                'quantity': self.config.quantity,
                # Launch hints — consumed by dashboard_agg.py's "start
                # everything" shortcut to reconstruct the exact CLI the
                # bot was launched with, without the operator having to
                # remember per-ticker flags. Recovery flow then takes over
                # for trigger / qty / position state (those come from the
                # state file on disk, not from these hints).
                'ibkr_port': self.config.ibkr_port,
                'paper_trading': self.config.paper_trading,
                'sl_limit_offset': self.config.sl_limit_offset,
                'offset_stop_fraction': self.config.offset_stop_fraction,
                'offset_entry_pct': self.config.offset_entry_pct,
                # Account equity (cached) so the drill-down dashboard can
                # show real numbers instead of a $100k stub.
                'equity': equity,
                # Buying power + position-utilization metrics. Aggregator
                # sums position_notional across symbols, picks any live
                # equity/bp value (same account-wide number), and renders
                # the two pills `EXP X.X%` and `BP USED Y.Y%` in its header.
                'buying_power': buying_power,
                # IBKR account daily P&L (realized+unrealized, auto-reset each
                # session). Account-wide → aggregator shows it as the single
                # portfolio "day P&L". null until the reqPnL feed lands.
                'daily_pnl_ibkr': daily_pnl_ibkr,
                'position_notional': position_notional,
                # Worst-case capital outlay of a working unfilled entry.
                # Informational only — exposure (this snapshot's
                # exposure_pct and the portfolio risk gate) is computed
                # from FILLED position_notional alone, so a pending entry
                # does NOT count toward exposure until it fills.
                'pending_notional': pending_notional,
                'exposure_pct': exposure_pct,
                'bp_used_pct': bp_used_pct,
                # Short-selling requirement (availability + borrow fee +
                # margin, live-from-IBKR when captured). Carried in the
                # snapshot so dashboard_agg.py's drill-down renders the
                # SHORT REQ panel with the same numbers the per-symbol
                # dashboard shows. Both fields are fully guarded (None when
                # no spec / non-short asset / pre-fetch).
                'short_requirement': self.engine._short_requirement_snapshot(),
                'short_margin_whatif': getattr(self.engine, '_short_margin_whatif', None),
                # Microstructure aggregates
                'vwap': m.vwap,
                'tick_rate': m.tick_rate,
                'trade_rate': m.trade_rate,
                'bbo_rate': m.bbo_rate,
                'buy_pct': m.buy_pct,
                'sell_pct': m.sell_pct,
                # Last 6 tape entries (classified)
                'tape': [
                    {
                        'ts': t.ts, 'price': t.price, 'size': t.size,
                        'exchange': t.exchange, 'conditions': t.conditions,
                        'direction': t.direction,
                    }
                    for t in list(m.tape)[-6:]
                ],
                # Pipeline latency (most-recent stats)
                'latency': self.state._latency or {},
                # Connection state
                'connected': self.state.connected,
                'heartbeat_age': self.state.heartbeat_age_s,
                # Paused / session
                'paused': getattr(self.engine, '_paused', False),
                'in_session': self.state.in_session,
            }
            path = f".gt_live_{self.config.ticker}_{self.config.ibkr_client_id}.json"
            temp = path + ".tmp"
            with open(temp, 'w') as fh:
                _json.dump(snap, fh, default=str)
            _os.replace(temp, path)
            self._snapshot_ok += 1
        except Exception as e:
            # Snapshot is best-effort — don't crash trading if disk fails.
            # But DO surface repeated failures: the aggregator depends on
            # this file being fresh, and a silently-broken writer used to
            # be invisible until you wondered why dashboard_agg looked
            # frozen (the original bug: NameError on the missing datetime
            # import, swallowed here, every call). Print the first fail and
            # every 100th after — bounded log noise, fast diagnosis.
            self._snapshot_fail += 1
            if self._snapshot_fail == 1 or self._snapshot_fail % 100 == 0:
                print(
                    f"{Y}[snapshot] write failed (#{self._snapshot_fail}): "
                    f"{type(e).__name__}: {e}{R}",
                    file=sys.stderr,
                )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description='GT Production Trading')
    parser.add_argument('symbol', nargs='?', default='AAPL')
    parser.add_argument('--trigger', type=float, help='Entry trigger price')
    parser.add_argument('--stop', type=float, help='Stop loss %% (e.g. 0.02)')
    parser.add_argument(
        '--sl-limit-offset', type=float,
        help='Min buffer (floor) between stop trigger and limit price, in dollars (default 0.05). '
             'Effective offset is max(this, scaled_value) — see --offset-stop-fraction / --offset-entry-pct.'
    )
    parser.add_argument(
        '--offset-stop-fraction', type=float,
        help='BUY-cover exit buffer = this fraction of (stop-entry) distance (default 0.05 = 5%%). '
             'Scales the limit buffer with how far your stop sits ABOVE entry. Set 0 to disable.'
    )
    parser.add_argument(
        '--offset-entry-pct', type=float,
        help='SELL (breakdown) entry buffer = this fraction of trigger price (default 0.0005 = 5 bps). '
             'Scales the entry buffer with absolute price level. Set 0 to disable.'
    )
    parser.add_argument(
        '--offset-fixed', type=float,
        help='Convenience: pin both buffers to this flat dollar value (disables scaling). '
             'Equivalent to: --sl-limit-offset VALUE --offset-stop-fraction 0 --offset-entry-pct 0. '
             'Use when you want a constant buffer for all entries and exits (e.g. --offset-fixed 0.05).'
    )
    parser.add_argument('--qty', type=int, help='Quantity')
    parser.add_argument('--port', type=int, default=4001, help='IBKR port')
    parser.add_argument('--client-id', type=int, default=1)
    parser.add_argument('--paper', action='store_true', help='Paper trading')
    parser.add_argument(
        '--market', action='store_true',
        help='Enter with a MARKET order instead of resting a SELL STP-LMT at '
             '--trigger. For the case where something upstream (e.g. the RTH '
             'trendline runner) has ALREADY seen the breakdown and launched '
             'this bot in response: the trigger has been met, so there is '
             'nothing left to wait for, and a limit the tape has already '
             'passed would never fill. --trigger is still required — the '
             'protective child cover is sized from it until the real fill '
             'price is known. WARNING: a market order has no price floor. It '
             'fills at whatever the book bids when it lands, which can be '
             'worse than --trigger if the market moved while this bot was '
             'connecting. --offset-entry-pct is ignored in this mode. '
             'Default off.')
    parser.add_argument(
        '--cfd', action='store_true',
        help='Trade the CFD contract for this symbol instead of equity/spot. '
             'Resolves the generic CFD variant (e.g. NVDA share-CFD, not NVDA '
             'equity; IBUS500 index-CFD; XAUUSD gold-CFD). The broker qualifies '
             'the real contract + minTick. Default off → equity/FX as before.')
    parser.add_argument(
        '--reset', action='store_true',
        help='Wipe state for a clean slate. Connects to IBKR, previews working '
             'orders + open position for the symbol, prompts for confirmation, then: '
             '(1) cancels all working orders on this symbol, (2) market-sells any '
             'open position to flat, (3) deletes .gt_state_<SYMBOL>_<CLIENTID>.json '
             '(and legacy .gt_state.json if present). Exits without starting the engine. '
             'Use when state has drifted or you want a known-clean restart.'
    )
    parser.add_argument(
        '--uvloop', action='store_true',
        help='Use uvloop as the asyncio event loop (libuv-based, 2-3x faster '
             'for I/O-bound workloads). Drops pipeline latency p99 from ~0.4ms '
             'to ~0.15ms and shaves ~1-2ms off order placement overhead. '
             'Requires `pip install uvloop` (Unix only — not available on Windows). '
             'Stdlib asyncio is the default so A/B comparison is easy.'
    )
    parser.add_argument(
        '--risk', type=float, metavar='USD',
        help='Override portfolio daily-loss circuit-breaker (in USD). The '
             'engine pauses all new entries when the summed realized P&L '
             'across every running bot drops past -<RISK>. Default is $2000 '
             '(from Config.max_daily_loss_usd / GT_MAX_DAILY_LOSS_USD env). '
             'Use a smaller value like --risk 500 to verify the gate trips '
             'correctly in a controlled test session without waiting for the '
             'full $2k drawdown.'
    )
    parser.add_argument(
        '--exposure', type=float, metavar='USD',
        help='Override portfolio combined-exposure cap (in USD). The engine '
             'rejects a new entry when the sum of (existing open position '
             'notional across every running bot + this new order value) would '
             'exceed <EXPOSURE>. Default is $50000 (from '
             'Config.max_position_value_usd / GT_MAX_POSITION_VALUE_USD env). '
             'For portfolio-wide centralised control without re-launching every '
             'bot, prefer writing the shared limits file via dashboard_agg.py: '
             '`python dashboard_agg.py --exposure 50000` — every running bot '
             'picks it up within ~1s. The CLI flag here is for the case where '
             'you want a SINGLE bot to be even tighter than the portfolio cap '
             '(most-conservative wins).'
    )
    return parser.parse_args()


async def reset_flow(config) -> int:
    """Execute the --reset flow: cancel orders, flatten position, delete state file.

    Returns 0 on success, 1 on failure or user abort. Caller should pass this
    as the process exit code.
    """
    from src.execution.broker import Gateway

    gw = Gateway(
        host=config.ibkr_host,
        port=config.ibkr_port,
        client_id=config.ibkr_client_id,
        symbol=config.ticker,
        paper=config.paper_trading,
        cfd=getattr(config, 'cfd', False),
    )

    if config.paper_trading:
        # Paper has no broker-side persistence. Just delete the state file.
        print(f"{Y}Paper mode — skipping broker reset, only clearing local state.{R}")
    else:
        print(f"{C}Connecting to IBKR on port {config.ibkr_port}...{R}", end='', flush=True)
        if not await gw.connect():
            print(f"\n{R_}Failed to connect — cannot reset broker state. "
                  f"Local state will NOT be deleted to avoid drift.{R}")
            return 1
        print(f" {G}Connected{R}")
        # Give ib_async a beat to sync open orders/positions.
        # Use asyncio.sleep (we're inside async reset_flow); the ib_async
        # `ib.sleep()` wrapper calls `loop.run_until_complete` internally
        # which can't nest in an already-running loop — would raise
        # "This event loop is already running" since nest_asyncio was removed.
        await asyncio.sleep(0.5)

    # ── Preview ──────────────────────────────────────────────────────────
    open_orders = gw.fetch_open_orders() if not config.paper_trading else []
    positions = await gw.get_positions() if not config.paper_trading else []
    sym_positions = [p for p in positions if p.symbol == config.ticker]

    # Match LiveTrader's per-symbol-per-clientId naming so --reset deletes
    # only the state file belonging to THIS (ticker, client_id) — not a
    # sibling process's file. Also probe the legacy single-file path so
    # users upgrading from earlier versions don't get a stale state file
    # silently surviving.
    state_file = f".gt_state_{config.ticker}_{config.ibkr_client_id}.json"
    legacy_state_file = ".gt_state.json"
    # The 5Hz live snapshot file (consumed by dashboard_agg.py). Not engine
    # state — just the most recent market snapshot. Leaving it around after
    # --reset confused dashboard_agg.py into showing stale "active" symbol
    # rows for tickers that had been reset, and confused operators who
    # expected a true clean slate. Reset now nukes it too.
    live_snapshot_file = f".gt_live_{config.ticker}_{config.ibkr_client_id}.json"
    state_exists = os.path.exists(state_file)
    legacy_exists = os.path.exists(legacy_state_file)
    live_exists = os.path.exists(live_snapshot_file)

    print()
    print(f"{C}═══ RESET PREVIEW: {config.ticker} ═══{R}")
    if open_orders:
        print(f"  Working orders ({len(open_orders)}):")
        for o in open_orders:
            stop = f"stop=${o['stop_price']:.2f}" if o['stop_price'] else ""
            lmt = f"limit=${o['limit_price']:.2f}" if o['limit_price'] else ""
            print(f"    • {o['action']:4} {o['qty']:>4} {o['order_type']:8} {stop} {lmt} (broker_id={o['broker_id']})")
    else:
        print(f"  Working orders: {D}none{R}")
    if sym_positions:
        print(f"  Open position:")
        for p in sym_positions:
            print(f"    • {p.symbol}: qty={p.quantity} @ avg_cost=${p.avg_cost:.2f}")
    else:
        print(f"  Open position: {D}none{R}")
    print(f"  Local state file:    {state_file} {'exists' if state_exists else D + 'absent' + R}")
    print(f"  Live snapshot file:  {live_snapshot_file} {'exists' if live_exists else D + 'absent' + R}")
    if legacy_exists:
        print(f"  {Y}Legacy state file:{R} {legacy_state_file} {Y}exists (will also be removed){R}")
    print()

    if (not open_orders and not sym_positions and not state_exists
        and not legacy_exists and not live_exists):
        print(f"{G}Nothing to reset.{R}")
        if not config.paper_trading:
            await gw.disconnect()
        return 0

    # ── Confirm ──────────────────────────────────────────────────────────
    print(f"{Y}This will:{R}")
    if open_orders:
        print(f"  • Cancel {len(open_orders)} working order(s) at IBKR")
    if sym_positions:
        for p in sym_positions:
            side = "SELL" if p.quantity > 0 else "BUY"
            print(f"  • {side} {abs(int(p.quantity))} {p.symbol} at MARKET to flatten")
    if state_exists:
        print(f"  • Delete {state_file}")
    if live_exists:
        print(f"  • Delete {live_snapshot_file}")
    if legacy_exists:
        print(f"  • Delete legacy {legacy_state_file}")
    print()
    response = input(f"{Y}Type 'yes' to proceed, anything else to abort: {R}").strip().lower()
    if response != 'yes':
        print(f"{D}Aborted.{R}")
        if not config.paper_trading:
            await gw.disconnect()
        return 1

    # ── Execute ──────────────────────────────────────────────────────────
    print()
    if open_orders:
        n = gw.cancel_open_orders_for_symbol(config.ticker)
        print(f"{C}Cancelled {n} working order(s){R}")
        # Verify the cancels actually took effect at the broker.
        # Bracket-aware: cancelling the parent of an unfilled bracket
        # implicitly cancels the child via IBKR's parent-child linkage,
        # but our explicit `cancelOrder(child)` still fires — IBKR
        # accepts it as a no-op. The verify step polls openTrades to
        # confirm no residual working orders before deleting state files,
        # so we never delete local state while a stray order is still
        # alive at the broker (which would silently fire mid-day).
        flat = await gw.verify_symbol_flat_at_broker(config.ticker, timeout_s=3.0)
        if not flat:
            residual = gw.fetch_open_orders()
            print(f"{R_}WARNING: cancel acknowledged but {len(residual)} order(s) "
                  f"still working at IBKR after 3s:{R}")
            for o in residual:
                print(f"    • {o['action']} {o['qty']} {o['order_type']} "
                      f"(broker_id={o['broker_id']}, status={o.get('status')})")
            print(f"{Y}Local state will NOT be deleted. Cancel manually in TWS, "
                  f"then re-run --reset.{R}")
            await gw.disconnect()
            return 1
        print(f"{G}Confirmed: all working orders for {config.ticker} are cancelled{R}")

    if sym_positions and not config.paper_trading:
        result = await gw.flatten_position(config.ticker)
        if result:
            print(f"{C}Submitted {result['side']} {result['qty']} {config.ticker} MARKET to flatten "
                  f"(broker_id={result['broker_id']}){R}")
            # Wait briefly for the fill so the IBKR account is actually flat by the time we exit
            await asyncio.sleep(2.0)

    if state_exists:
        os.remove(state_file)
        print(f"{C}Removed {state_file}{R}")
    if live_exists:
        try:
            os.remove(live_snapshot_file)
            print(f"{C}Removed {live_snapshot_file}{R}")
        except OSError as e:
            # If another live process is still writing this (unlikely on
            # --reset, but possible if you forgot to kill it first),
            # the unlink might race. Warn loudly so the operator can
            # clean it up manually.
            print(f"{Y}Could not remove {live_snapshot_file}: {e} "
                  f"(is the live process still running? kill it first){R}")
    if legacy_exists:
        os.remove(legacy_state_file)
        print(f"{C}Removed legacy {legacy_state_file}{R}")

    if not config.paper_trading:
        await gw.disconnect()

    print()
    print(f"{G}Reset complete. You can now start fresh with:{R}")
    cmd = f"  python run_live.py {config.ticker} --trigger {config.trigger_price} --port {config.ibkr_port} --client-id {config.ibkr_client_id}"
    print(f"{cmd}")
    return 0


# A79 (2026-06-12): single-writer guard. The state file is keyed by
# (symbol, client_id), so two processes on the SAME symbol but DIFFERENT
# client_ids each get their own state file and run independently — both
# managing the SAME broker position. That is a single-writer violation:
# cancelling one engine's protective stop confuses ownership of the
# re-arm and can leave the position naked (the GBPUSD 2026-06-12 incident,
# where two engines — client_id 1 and 3 — ran the same pair).
#
# Lock granularity is (symbol, port): the same symbol on the same broker
# account (port) is refused regardless of client_id; the same symbol on a
# DIFFERENT port (a genuinely different account / gateway — e.g. paper
# TWS 7497 vs a second gateway 4002) is allowed.
#
# flock auto-releases when the process exits (the kernel closes the fd),
# so a crashed or SIGKILL'd bot frees the lock immediately — the chaos
# rolling-kill respawn re-acquires cleanly after its settle window, and
# the 32-symbol stress fleet never collides (each bot owns a unique
# symbol). Opt out with GT_SKIP_SYMBOL_LOCK=1 for deliberate multi-writer
# or specialised test scenarios.
_SYMBOL_LOCK_FD = None  # held for process lifetime so GC doesn't release the flock


def _acquire_symbol_lock(ticker: str, port: int, client_id: int) -> None:
    """Refuse to start if another live process already owns (symbol, port).

    On collision, prints who holds the lock and exits(2). On success,
    records this process's identity in the lock file and keeps the fd open
    for the lifetime of the process.
    """
    global _SYMBOL_LOCK_FD
    if os.environ.get("GT_SKIP_SYMBOL_LOCK") == "1":
        print(f"{Y}GT_SKIP_SYMBOL_LOCK=1 — single-writer guard bypassed for "
              f"{ticker} on port {port}.{R}")
        return
    import fcntl
    import time as _time
    lock_path = f".gt_lock_{ticker}_{port}.lock"
    fd = open(lock_path, "a+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            fd.seek(0)
            held = fd.read().strip() or "(another live process)"
        except Exception:
            held = "(another live process)"
        fd.close()
        print(f"\n{R_}{B}╔══════════════════════════════════════════════════════════════╗{R}")
        print(f"{R_}{B}║  REFUSED TO START — {ticker} is already running on port {port}{R}")
        print(f"{R_}{B}╠══════════════════════════════════════════════════════════════╣{R}")
        print(f"{R_}{B}║{R}  Single-writer guard (A79): another engine already owns this")
        print(f"{R_}{B}║{R}  symbol on this broker account. Running a second engine on a")
        print(f"{R_}{B}║{R}  different client_id would let two engines manage the SAME")
        print(f"{R_}{B}║{R}  broker position — the GBPUSD double-engine incident.")
        print(f"{R_}{B}║{R}  Holder: {D}{held}{R}")
        print(f"{R_}{B}║{R}  This attempt: {D}client_id={client_id} pid={os.getpid()}{R}")
        print(f"{R_}{B}║{R}")
        print(f"{R_}{B}║{R}  {G}Fix:{R} use the existing process, or stop it first, or run on a")
        print(f"{R_}{B}║{R}       different port (account). Override with {D}GT_SKIP_SYMBOL_LOCK=1{R}")
        print(f"{R_}{B}║{R}       only if you truly intend two writers.")
        print(f"{R_}{B}╚══════════════════════════════════════════════════════════════╝{R}")
        sys.exit(2)
    # Acquired — stamp our identity, keep the fd open (module global).
    try:
        fd.seek(0)
        fd.truncate()
        fd.write(f"pid={os.getpid()} ticker={ticker} port={port} "
                 f"client_id={client_id} acquired={_time.strftime('%Y-%m-%dT%H:%M:%S')}")
        fd.flush()
    except Exception:
        pass
    _SYMBOL_LOCK_FD = fd
    print(f"{G}Single-writer lock acquired: {ticker} @ port {port} "
          f"(client_id={client_id}).{R}")


async def main():
    # nest_asyncio.apply() used to live here. Removed because it monkey-
    # patches asyncio internals and its last asyncio-compat update was for
    # Python 3.9; on Python 3.14 (which rewrote task management for eager
    # tasks) it cancels the main task within ~1s of startup — which is
    # exactly the "automatically closes after a sec" symptom we hit.
    # nest_asyncio is only needed when running inside an already-running
    # loop (Jupyter / IDE REPLs); CLI launches don't nest.

    args = parse_args()

    # Build config from args (env vars are already loaded by Config defaults)
    config = Config.from_env()

    # Override with CLI args if provided
    config.ticker = args.symbol.upper()  # CLI args take precedence
    config.ibkr_port = args.port
    config.ibkr_client_id = args.client_id
    if args.trigger:
        config.trigger_price = args.trigger
    if args.stop:
        config.stop_loss_pct = args.stop
    if args.sl_limit_offset is not None:
        config.sl_limit_offset = args.sl_limit_offset
    if args.offset_stop_fraction is not None:
        config.offset_stop_fraction = args.offset_stop_fraction
    if args.offset_entry_pct is not None:
        config.offset_entry_pct = args.offset_entry_pct
    # --offset-fixed is a convenience overlay: it last-wins over the three
    # underlying knobs and pins everything to a flat constant. The modular
    # scaling code stays intact; this just collapses it to a degenerate case
    # (fraction=0, pct=0, floor=VALUE).
    if args.offset_fixed is not None:
        config.sl_limit_offset = args.offset_fixed
        config.offset_stop_fraction = 0.0
        config.offset_entry_pct = 0.0
    if args.qty:
        config.quantity = args.qty
    if args.paper:
        config.paper_trading = True
    if getattr(args, 'cfd', False):
        config.cfd = True
    if getattr(args, 'market', False):
        config.entry_market = True

    # --risk overrides the portfolio daily-loss circuit-breaker. Lower
    # values (e.g. --risk 500) are useful for verifying the gate trips
    # without putting $2000 of P&L at risk. The override only affects
    # THIS bot instance's RiskCheck — peer bots use whatever they were
    # launched with. Since the gate reads PORTFOLIO-WIDE realized P&L,
    # the lowest --risk across all running bots is effectively the
    # binding limit (whichever bot tries to enter next will trip first).
    if args.risk is not None:
        if args.risk <= 0:
            print(f"WARNING: --risk {args.risk} ≤ 0 is rejected; using default ${config.max_daily_loss_usd}", file=sys.stderr)
        else:
            config.max_daily_loss_usd = args.risk
            print(f"Risk: daily-loss circuit breaker = -${config.max_daily_loss_usd:.0f} (overridden via --risk)")

    # --exposure overrides the portfolio combined-exposure cap. Same
    # semantics as --risk: per-bot override. For portfolio-wide control,
    # write `.gt_portfolio_limits.json` via `dashboard_agg.py --exposure
    # NNN` — every running bot picks that up within ~1s. RiskCheck takes
    # the MIN of (config value, file value), so a per-bot tighter cap
    # set here is never silently relaxed by a more permissive file.
    if args.exposure is not None:
        if args.exposure <= 0:
            print(f"WARNING: --exposure {args.exposure} ≤ 0 is rejected; using default ${config.max_position_value_usd}", file=sys.stderr)
        else:
            config.max_position_value_usd = args.exposure
            print(f"Risk: combined-exposure cap = ${config.max_position_value_usd:.0f} (overridden via --exposure)")

    # ── Trigger + quantity resolution ──────────────────────────────────
    # Shared helper: peek into the saved state file once. We use it for
    # both trigger and quantity resume, so a single restart can recover
    # everything without re-supplying ANY flags.
    _saved_state: dict = {}
    _state_file_path = f".gt_state_{config.ticker}_{config.ibkr_client_id}.json"
    if os.path.exists(_state_file_path):
        try:
            import json as _json
            with open(_state_file_path) as _fh:
                _saved_state = _json.load(_fh)
        except Exception as e:
            print(f"{Y}Couldn't read state file {_state_file_path}: {e}{R}")
            _saved_state = {}

    # If no --trigger was supplied AND no GT_TRIGGER_PRICE env var, try
    # to recover the trigger from the saved state file. This is what makes
    # restart-mid-cycle work without re-supplying the trigger:
    #   - IN_POSITION restored: the cycle's trigger isn't actually needed
    #     to manage the open position, but we still want a sane display
    #     value and a fallback for the eventual next cycle.
    #   - WAITING_REENTRY restored: engine uses previous_breakout_level
    #     for the next BUY (not config.trigger_price), so this is purely
    #     a display fallback — but we still want it correct.
    #   - MONITORING restored from a prior session: must have its trigger
    #     to place the initial BUY stop-limit; this is the case where
    #     state-file resume is genuinely necessary.
    #
    # Refuse to start if trigger is still 0 after all sources are tried.
    # Previously the dataclass default of 225.00 silently kicked in here,
    # placing real orders at $225 regardless of the actual symbol. That's
    # the bug this fix closes.
    if args.trigger is None and "GT_TRIGGER_PRICE" not in os.environ:
        saved_state_name = _saved_state.get("state", "")
        # Prefer the explicit saved trigger; fall back to
        # previous_breakout_level (the WAITING_REENTRY trigger)
        # so legacy state files without trigger_price still work.
        saved_trigger = _saved_state.get("trigger_price") or _saved_state.get("previous_breakout_level") or 0
        if saved_trigger and saved_trigger > 0:
            config.trigger_price = float(saved_trigger)
            print(
                f"{C}Trigger restored from state file: "
                f"${config.trigger_price:.2f} (state={saved_state_name or 'IDLE'}){R}"
            )

    # ── Trigger-mismatch guard ─────────────────────────────────────────
    # If the saved state file shows active management (an open position OR a
    # non-IDLE engine state OR a resting pending stop) AND the user explicitly
    # passes --trigger that differs from what's already armed, REFUSE TO START.
    #
    # Previously the new --trigger silently overwrote the saved one — the
    # engine kept managing the EXISTING broker order at the OLD trigger, but
    # the dashboard, state file, and log lines showed the NEW value. Visually
    # incoherent: the operator could see "trigger $330" on screen while the
    # broker still had a $320 stop-limit resting. The bot didn't double-fill
    # (reconciliation rebinds the existing order), but the displayed and
    # actually-armed triggers disagreed.
    #
    # Skip if --reset is set (the operator's deliberately starting clean).
    # Skip if the saved trigger is 0 (legacy state file, no anchor to compare to).
    # Skip in paper mode (no real broker order to be coherent with).
    if (
        args.trigger is not None
        and not args.reset
        and not args.paper
        and _saved_state
    ):
        saved_trigger = float(_saved_state.get("trigger_price") or 0)
        saved_state_name = (_saved_state.get("state") or "").upper()
        position_open = bool(_saved_state.get("position_open"))
        pending_stop = _saved_state.get("pending_stop")
        # "Active management" = anything that means the engine is mid-cycle:
        # holding a position, monitoring for entry, waiting for re-entry,
        # already armed a protective stop, etc. IDLE / STOPPED / unset are
        # the only states where the trigger isn't currently "owned" by the
        # bot, so changing it via CLI is safe.
        ACTIVE_STATES = {
            "MONITORING", "ORDER_ENTRY", "IN_POSITION",
            "EXIT_POSITION", "WAITING_REENTRY",
        }
        is_active = (
            position_open
            or pending_stop is not None
            or saved_state_name in ACTIVE_STATES
        )
        # Float tolerance — IBKR rounds to penny ticks, but a paranoid 1e-4
        # also catches legitimate user mistakes like "330.4" vs "330.41".
        if (
            is_active
            and saved_trigger > 0
            and abs(args.trigger - saved_trigger) > 1e-4
        ):
            state_file = f".gt_state_{config.ticker}_{config.ibkr_client_id}.json"
            bar = "═" * 78
            print()
            print(f"{R_}{B}╔{bar}╗{R}")
            print(f"{R_}{B}║{R} {R_}{B}REFUSING TO START — TRIGGER MISMATCH ON ACTIVE STATE{R}                         {R_}{B}║{R}")
            print(f"{R_}{B}╠{bar}╣{R}")
            print(f"{R_}{B}║{R} You passed {Y}--trigger {args.trigger:.2f}{R}, but the saved state file for "
                  f"{Y}{B}{config.ticker}{R} on")
            print(f"{R_}{B}║{R} client_id {Y}{config.ibkr_client_id}{R} shows an active cycle armed at "
                  f"{Y}${saved_trigger:.2f}{R}.")
            print(f"{R_}{B}║{R}")
            print(f"{R_}{B}║{R} {D}Saved state    : {saved_state_name or 'IDLE'}{R}")
            print(f"{R_}{B}║{R} {D}Position open  : {position_open}{R}")
            if position_open:
                qty = _saved_state.get("quantity", 0)
                entry = _saved_state.get("entry_price", 0)
                print(f"{R_}{B}║{R} {D}Position qty   : {qty}{R}")
                print(f"{R_}{B}║{R} {D}Entry price    : ${entry or 0:.2f}{R}")
            if pending_stop:
                ps_stop = pending_stop.get("stop_price") or 0
                ps_lim = pending_stop.get("limit_price") or 0
                ps_qty = pending_stop.get("qty") or 0
                ps_side = pending_stop.get("side") or "?"
                print(f"{R_}{B}║{R} {D}Resting order  : {ps_side} {ps_qty}  "
                      f"trig=${ps_stop:.2f}  lim=${ps_lim:.2f}{R}")
            print(f"{R_}{B}║{R} {D}Saved trigger  : ${saved_trigger:.2f}{R}")
            print(f"{R_}{B}║{R} {D}CLI --trigger  : ${args.trigger:.2f}{R}")
            print(f"{R_}{B}║{R}")
            print(f"{R_}{B}║{R} Changing the trigger now would NOT re-arm the broker order — reconciliation")
            print(f"{R_}{B}║{R} rebinds the existing one at {Y}${saved_trigger:.2f}{R} — but it WOULD make the dashboard")
            print(f"{R_}{B}║{R} and state file lie about what's armed. Refusing rather than letting the")
            print(f"{R_}{B}║{R} display drift from broker truth.")
            print(f"{R_}{B}║{R}")
            print(f"{R_}{B}║{R} {C}{B}Recovery options (pick ONE):{R}")
            print(f"{R_}{B}║{R}   {G}(a){R} Resume the existing cycle at its armed trigger — omit --trigger:")
            print(f"{R_}{B}║{R}       {D}python run_live.py {config.ticker} --port {config.ibkr_port} "
                  f"--client-id {config.ibkr_client_id}{R}")
            print(f"{R_}{B}║{R}   {G}(b){R} Or pass the matching value: "
                  f"{D}--trigger {saved_trigger:.2f}{R}")
            print(f"{R_}{B}║{R}   {G}(c){R} Start a fresh cycle at the new trigger — flattens position and")
            print(f"{R_}{B}║{R}       cancels resting orders:")
            print(f"{R_}{B}║{R}       {D}python run_live.py {config.ticker} --reset --trigger "
                  f"{args.trigger:.2f} --port {config.ibkr_port} --client-id {config.ibkr_client_id}{R}")
            print(f"{R_}{B}║{R}")
            print(f"{R_}{B}║{R} No connection made. No dashboard. No orders. Exiting with code 1.")
            print(f"{R_}{B}╚{bar}╝{R}")
            print()
            return 1

    # Hard guard: if no trigger was provided AND we couldn't recover one
    # from state, refuse to start. Don't fall back to 225.
    if config.trigger_price <= 0 and not args.reset:
        print(
            f"{R_}ERROR: no trigger price specified for {config.ticker}.{R}\n"
            f"  Provide one of:\n"
            f"    • --trigger PRICE\n"
            f"    • GT_TRIGGER_PRICE=PRICE in the environment\n"
            f"    • an existing .gt_state_{config.ticker}_{config.ibkr_client_id}.json with state from a prior session\n"
            f"  The hardcoded $225 default was removed because it placed real "
            f"orders at the wrong price when this flag was forgotten."
        )
        return 1

    # ── Quantity resolution ───────────────────────────────────────────
    # Same pattern as trigger. Default was env GT_QUANTITY=1, dataclass
    # default 100 — inconsistent and silent. Both are now 0 sentinel,
    # and we either: (a) take --qty, (b) take env GT_QUANTITY, (c) restore
    # from state file, or (d) refuse. No silent phantom 1 or 100.
    #
    # The state file stores TWO qty-related fields, which is the source of
    # the bug we just fixed:
    #   `quantity`         = engine's _quantity (CURRENT POSITION SIZE,
    #                        0 when flat — useless as a config restore source
    #                        if the symbol ended yesterday flat-but-monitoring)
    #   `config_quantity`  = config.quantity (USER'S TRADE SIZE from --qty,
    #                        persisted explicitly since today's fix)
    #   `pending_stop.qty` = the qty on the broker-side stop-limit if one
    #                        was pending — same value as config.quantity at
    #                        the time the order was placed, so this is a
    #                        valid back-compat source for older state files
    #                        that pre-date the config_quantity field.
    # Fallback chain: config_quantity → pending_stop.qty → quantity.
    if args.qty is None and "GT_QUANTITY" not in os.environ:
        saved_cfg_qty = _saved_state.get("config_quantity") or 0
        saved_pending = _saved_state.get("pending_stop") or {}
        saved_pending_qty = (saved_pending.get("qty") or 0) if isinstance(saved_pending, dict) else 0
        saved_pos_qty = _saved_state.get("quantity") or 0

        # Pick the first non-zero source in priority order.
        if saved_cfg_qty and saved_cfg_qty > 0:
            chosen_qty, source = int(saved_cfg_qty), "config_quantity"
        elif saved_pending_qty and saved_pending_qty > 0:
            chosen_qty, source = int(saved_pending_qty), "pending_stop.qty (legacy)"
        elif saved_pos_qty and saved_pos_qty > 0:
            chosen_qty, source = int(saved_pos_qty), "quantity (legacy position size)"
        else:
            chosen_qty, source = 0, ""

        if chosen_qty > 0:
            config.quantity = chosen_qty
            saved_state_name = _saved_state.get("state", "")
            print(
                f"{C}Quantity restored from state file: "
                f"{config.quantity} via {source} (state={saved_state_name or 'IDLE'}){R}"
            )

    if config.quantity <= 0 and not args.reset:
        print(
            f"{R_}ERROR: no quantity specified for {config.ticker}.{R}\n"
            f"  Provide one of:\n"
            f"    • --qty N\n"
            f"    • GT_QUANTITY=N in the environment\n"
            f"    • an existing .gt_state_{config.ticker}_{config.ibkr_client_id}.json "
            f"with quantity from a prior session\n"
            f"  The hardcoded default of 1 was removed because it placed real "
            f"orders at the wrong size when this flag was forgotten."
        )
        return 1

    # --reset short-circuits before any engine wiring: connect, preview,
    # confirm with the user, cancel orders, flatten position, delete state,
    # exit. See reset_flow() for details.
    if args.reset:
        rc = await reset_flow(config)
        sys.exit(rc)

    # A79: single-writer guard — refuse to start if another live engine
    # already owns this (symbol, port). Placed AFTER --reset (a cleanup
    # path that must be able to run even when a stale lock file exists)
    # and BEFORE any broker connection or engine start, so a duplicate
    # launch fails fast and cheap.
    _acquire_symbol_lock(config.ticker, config.ibkr_port, config.ibkr_client_id)

    # Surface the offset config so it shows up in run logs for A/B comparison.
    print(
        f"{C}Config: ticker={config.ticker}, trigger=${config.trigger_price:.2f}, "
        f"stop={config.stop_loss_pct*100:.1f}%, qty={config.quantity}, paper={config.paper_trading}{R}"
    )
    if config.offset_stop_fraction == 0 and config.offset_entry_pct == 0:
        print(f"{C}Offset: fixed=${config.sl_limit_offset:.2f} (scaling disabled){R}")
    else:
        print(
            f"{C}Offset: floor=${config.sl_limit_offset:.2f}, "
            f"stop_fraction={config.offset_stop_fraction*100:.2f}%, "
            f"entry_pct={config.offset_entry_pct*10000:.1f}bps{R}"
        )
    # Partial-fill chase config — surface so the operator can confirm at
    # launch what the chase behaviour will be if a BUY entry stalls.
    effective_chase = (
        config.partial_fill_chase_offset
        if config.partial_fill_chase_offset > 0
        else config.sl_limit_offset
    )
    chase_source = "explicit" if config.partial_fill_chase_offset > 0 else "auto=sl_limit_offset"
    print(
        f"{C}Partial-fill: timeout={config.partial_fill_timeout_s:.0f}s, "
        f"max_chases={config.partial_fill_max_chases}, "
        f"chase_offset=${effective_chase:.2f} ({chase_source}){R}"
    )

    # Create trader
    trader = LiveTrader(config)

    # Signal handlers - set up after event loop is running
    shutdown_requested = False

    def shutdown(sig, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            print(f"\n{D}Force exit.{R}", flush=True)
            os._exit(1)
        print(f"\n{D}Shutting down... (press Ctrl+C again to force exit){R}", flush=True)
        shutdown_requested = True
        trader._running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Capture the running event loop so signal-scheduled coroutines have
    # a guaranteed target. Previously the async handlers (square_off,
    # cancel_all_orders, force_exit) called `asyncio.create_task` from a
    # Unix signal-handler context, which runs between bytecodes on the
    # main thread and doesn't reliably have a running loop attached —
    # the print fired but the task never scheduled (the "Squaring off..."
    # message-with-no-action bug). Using `loop.add_signal_handler` for
    # the async-scheduling shortcuts dispatches them as normal event-loop
    # callbacks, where `create_task` always works.
    _loop = asyncio.get_running_loop()

    # ── Async-scheduling shortcuts (need event-loop context) ──────────
    # Wrapped as plain zero-arg callbacks for loop.add_signal_handler.
    def _on_squareoff():
        print(f"\n{D}[Ctrl+Z] Emergency square-off...{R}", flush=True)
        _loop.create_task(trader.engine.square_off())

    def _on_cancel():
        print(f"\n{D}[Ctrl+X] Cancelling orders...{R}", flush=True)
        _loop.create_task(trader.engine.cancel_all_orders())

    def _on_force_exit():
        print(f"\n{D}[Ctrl+L] Force exit...{R}", flush=True)
        _loop.create_task(trader.engine.force_exit())

    # ── Sync-only shortcuts (no awaits, safe in any signal context) ──
    # These mutate engine state synchronously (pause/resume just flip a
    # bool; status/orders/stats just read + print). Kept on signal.signal
    # because they don't need event-loop access.
    def handle_pause(sig, frame):
        result = trader.engine.pause()
        print(f"\n{D}[Ctrl+\\] {result}{R}")

    def handle_resume(sig, frame):
        result = trader.engine.resume()
        print(f"\n{D}[Ctrl+Y] {result}{R}")

    def handle_status(sig, frame):
        summary = trader.engine.get_summary()
        print(f"\n{D}{'='*50}{R}")
        print(f"{B}STATUS SNAPSHOT{R}")
        print(f"  State:   {summary['state']}")
        print(f"  Position: {summary['position']} {'@$' + f'{summary['entry_price']:.2f}' if summary['entry_price'] else ''}")
        print(f"  Qty:     {summary['quantity']}")
        print(f"  Unreal:  ${summary['unrealized_pnl']:+.2f}")
        print(f"  Real:   ${summary['realized_pnl']:+.2f}")
        print(f"  Total:  ${summary['realized_pnl'] + summary['unrealized_pnl']:+.2f}")
        print(f"  Trades: {summary['trades_today']} ({summary['wins']}W/{summary['losses']}L)")
        print(f"  Paused: {summary['paused']}")
        print(f"{D}{'='*50}{R}\n")

    def handle_orders(sig, frame):
        history = getattr(trader.engine, '_order_history', [])
        print(f"\n{D}{'='*50}{R}")
        print(f"{B}ORDER HISTORY ({len(history)} orders){R}")
        for o in list(history)[-10:]:
            status = o.status.value if hasattr(o.status, 'value') else str(o.status)
            px = o.avg_fill_price if status == 'FILLED' else (o.signal_price or 0)
            print(f"  {o.side.value:4} {o.qty:3} @ ${px:.2f} {status:10} {o.submitted_at.strftime('%H:%M:%S') if o.submitted_at else 'N/A'}")
        print(f"{D}{'='*50}{R}\n")

    def handle_stats(sig, frame):
        summary = trader.engine.get_summary()
        print(f"\n{D}{'='*50}{R}")
        print(f"{B}TRADE STATS{R}")
        print(f"  Trades:   {summary['trades_today']}")
        print(f"  W/L:      {summary['wins']}/{summary['losses']}")
        print(f"  Win Rate: {summary['wins']/max(1, summary['trades_today'])*100:.1f}%")
        print(f"  Real P&L: ${summary['realized_pnl']:+.2f}")
        print(f"  Comm:     ${summary['commission']:.2f}")
        print(f"{D}{'='*50}{R}\n")

    # Register signal handlers. Two mechanisms:
    #
    #   loop.add_signal_handler(sig, cb)
    #     For shortcuts that schedule coroutines. Dispatches `cb` as a
    #     normal event-loop callback, so `create_task` inside it ALWAYS
    #     works. POSIX-only (which is fine — we're on darwin/linux).
    #
    #   signal.signal(sig, cb)
    #     For sync-only shortcuts (pause, resume, status print). Runs in
    #     signal-handler C context; safe because these only print or
    #     toggle bool flags.
    def safe_signal(sig, handler):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # Signal not available on this platform

    def safe_loop_signal(sig, cb):
        try:
            _loop.add_signal_handler(sig, cb)
        except (ValueError, NotImplementedError, RuntimeError, OSError):
            # Fallback to signal.signal — wrap cb so it matches the (sig, frame)
            # contract. Signal-context create_task is what we're trying to AVOID,
            # but if loop.add_signal_handler is unavailable (e.g. Windows or
            # uvloop edge case), this is better than no handler at all.
            try:
                signal.signal(sig, lambda s, f: cb())
            except (ValueError, OSError):
                pass

    # Async-scheduling: routed through the event loop (this is the fix).
    safe_loop_signal(signal.SIGTSTP, _on_squareoff)   # Ctrl+Z = Emergency square-off
    safe_loop_signal(signal.SIGUSR1, _on_cancel)      # Ctrl+X = Cancel orders
    safe_loop_signal(signal.SIGUSR2, _on_force_exit)  # Ctrl+L = Force exit

    # Sync-only: classic signal handler is fine.
    safe_signal(signal.SIGQUIT, handle_pause)     # Ctrl+\ = Pause
    safe_signal(signal.SIGURG, handle_resume)     # Ctrl+Y = Resume
    safe_signal(signal.SIGWINCH, handle_status)   # Ctrl+T = Status
    safe_signal(signal.SIGIO, handle_orders)      # Ctrl+] = Orders
    safe_signal(signal.SIGALRM, handle_stats)     # Stats

    print(f"{D}Shortcuts: Ctrl+Z=sqoff Ctrl+\\=pause Ctrl+Y=resume Ctrl+X=cancel Ctrl+L=force-exit Ctrl+T=status Ctrl+]=orders ...{R}")

    # Start
    ok = await trader.start()
    if not ok:
        return 1

    # Display loop: render dashboard at 3Hz, sync engine at 300ms intervals,
    # write live snapshot for multi-symbol aggregator at 5Hz, refresh state
    # file mtime every 15s as a heartbeat so the aggregator doesn't drop us
    # from its active-symbols list during quiet periods.
    print(f"\n{G}Streaming...{R}\n")
    last_render = 0.0
    last_live_write = 0.0
    last_heartbeat = 0.0
    # 50ms render cadence (~20Hz). Previously 333ms (3Hz) which made panel
    # updates feel laggy — between a tick arriving and being visible on the
    # dashboard you could wait up to a full frame. 20Hz is well below any
    # CPU concern (rendering is pure string formatting, ~0.5ms per frame)
    # and matches what real trading terminals do. Going faster than this
    # gives no visible benefit because terminal redraw itself is the floor.
    render_interval = 0.05
    # Snapshot for dashboard_agg.py: keep at 5Hz — that's the aggregator's
    # poll cadence, no reason to write faster than it reads.
    live_write_interval = 0.2
    heartbeat_interval = 15.0  # touch state file every 15s

    # Run the render loop inside try/finally so Ctrl+C (CancelledError) and
    # signal-driven shutdown both still execute the full cleanup path.
    # Without this the previous version orphaned the feed / heartbeat /
    # tick-consumer / daily-reset tasks when the loop's `await asyncio.sleep`
    # got cancelled — producing the "Task was destroyed but it is pending"
    # warnings and a closed-event-loop RuntimeError on exit.
    try:
        while trader._running:
            now = time.monotonic()

            if now - last_render > render_interval:
                trader.render_dashboard(first=(last_render == 0.0))
                last_render = now

            # Write the live snapshot file slightly more often than we
            # render — so the aggregator dashboard sees fresh data even
            # while our own dashboard render is mid-cycle.
            if now - last_live_write > live_write_interval:
                trader.write_live_snapshot()
                last_live_write = now

            # Heartbeat write — keep the state file mtime fresh so the
            # aggregator's "active in last 30s" filter doesn't drop us
            # during quiet periods (MONITORING with no LTP changes).
            # Cheap: re-saves the same state via StateStore's bounded
            # writer thread; non-blocking from our perspective.
            if now - last_heartbeat > heartbeat_interval:
                try:
                    trader.engine._save_state()
                except Exception:
                    pass
                last_heartbeat = now

            # 20ms loop tick so the 50ms render interval lands on time.
            # Was 50ms, which meant the loop could miss a render window
            # by up to a full tick — fine at 3Hz, sloppy at 20Hz.
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        # Reraised by asyncio when main task is cancelled. Expected on
        # Ctrl+C via SIGINT, but also fires if a misbehaving library
        # (e.g. nest_asyncio on Py 3.14) cancels the task internally —
        # dump the stack to stderr so a mysterious cancel is debuggable.
        # `shutdown_requested` is True only if our SIGINT handler ran;
        # if it's False the cancel came from somewhere else.
        import traceback as _tb
        sys.stderr.write(
            f"\n[main] CancelledError — running cleanup (shutdown_requested="
            f"{shutdown_requested})\n"
        )
        _tb.print_stack(file=sys.stderr)
        sys.stderr.flush()
        print(f"{D}[main] cancelled — running cleanup...{R}")
    except KeyboardInterrupt:
        # Defensive: if the SIGINT handler somehow doesn't catch first.
        print(f"\n{D}[main] keyboard interrupt — running cleanup...{R}")
    finally:
        # Shield cleanup from further cancellation (e.g. a second Ctrl+C
        # while we're shutting down). If even shield fails, fall back to
        # best-effort sync cleanup of the writer threads so audit + state
        # at least flush to disk.
        try:
            await asyncio.shield(_shutdown(trader))
        except asyncio.CancelledError:
            print(f"{R_}[main] cleanup was itself cancelled — flushing audit/state synchronously...{R}")
            try:
                if hasattr(trader, 'audit'):
                    trader.audit.close()
                if hasattr(trader, 'state_store') and hasattr(trader.state_store, 'close'):
                    trader.state_store.close()
            except Exception:
                pass
        except Exception as e:
            print(f"{R_}[main] cleanup error: {e}{R}")

    return 0


async def _shutdown(trader):
    """Centralised, idempotent shutdown sequence.

    Order: (1) trader.stop — cancels feed/engine/supervisor tasks and
    disconnects the gateway. (2) Audit manager close — flushes the four
    background writer threads. (3) State store close — drains the state
    writer thread. (4) Print the final session summary.

    Splitting this out of main() lets us wrap it in `asyncio.shield()` so a
    second Ctrl+C while we're cleaning up doesn't cancel the cleanup itself.
    """
    # Grab the summary BEFORE we stop the engine (after stop, registry /
    # status access is undefined). Latency stats too — pulled from the
    # still-living production_feed reference.
    summary = trader.get_summary()
    lat = trader.production_feed.get_latency_stats() if trader.production_feed is not None else {}

    # 1. Stop trader (feed + pipeline + engine + supervisor + gateway).
    await trader.stop()

    # 2. Flush audit writer threads. Done AFTER engine.stop so any final
    # FILLED / state events the engine emitted during stop land first.
    if hasattr(trader, 'audit'):
        audit_stats = trader.audit.close()
        print(f"\n{D}Audit logs:{R}")
        for log_type, stats in audit_stats.items():
            print(f"  {log_type.upper()}: {stats.get('written', 0):,} written, {stats.get('dropped', 0):,} dropped")

    # 3. Flush state-store writer thread so the final snapshot lands.
    if hasattr(trader, 'state_store') and hasattr(trader.state_store, 'close'):
        ss_stats = trader.state_store.close()
        print(f"  STATE: {ss_stats.get('written', 0):,} written, {ss_stats.get('dropped', 0):,} dropped")

    # 4. Drain the Slack worker queue. Done AFTER engine + audit so any
    # last FILLED / REJECTED notifications land. close() returns stats
    # including drops — a non-zero drop count means the webhook was slow
    # or unreachable during the session and some messages were lost.
    if hasattr(trader, 'alerts') and hasattr(trader.alerts, 'close'):
        try:
            alert_stats = trader.alerts.close()
            for ch_name, s in alert_stats.items():
                if ch_name == "SlackChannel":
                    sent = s.get('sent', 0)
                    dropped = s.get('dropped', 0)
                    errs = s.get('http_errors', 0)
                    leftover = s.get('queued_at_exit', 0)
                    color = G if (dropped == 0 and errs == 0) else (Y if dropped + errs < sent else R_)
                    print(
                        f"  SLACK: {color}{sent:,} sent, {dropped:,} dropped, "
                        f"{errs:,} errors{R} (queued at exit: {leftover})"
                    )
        except Exception as e:
            print(f"  {Y}[alerts.close] {e}{R}")

    # 4. Session summary.
    print(f"\n{D}{'=' * 60}{R}")
    print(f"{B}Session Summary{R}")
    print(f"  Trades: {summary.get('trades_today', 0)}")
    print(f"  P&L:    ${summary.get('pnl', 0):+.2f}")
    print(f"  W/L:    {summary.get('wins', 0)}/{summary.get('losses', 0)}")
    print(f"  Comm:   ${summary.get('total_commission', 0):.2f}")
    print(
        f"  Pipeline latency: "
        f"p50={lat.get('p50_ms', 0):.3f}ms  "
        f"p95={lat.get('p95_ms', 0):.3f}ms  "
        f"p99={lat.get('p99_ms', 0):.3f}ms  "
        f"max={lat.get('max_ms', 0):.3f}ms  "
        f"(n={lat.get('count', 0):,})"
    )
    # Snapshot writer health — `dashboard_agg.py` depends on this file
    # being fresh. A non-zero fail count tells you the aggregator was
    # starved during this session.
    snap_ok = getattr(trader, '_snapshot_ok', 0)
    snap_fail = getattr(trader, '_snapshot_fail', 0)
    snap_color = G if snap_fail == 0 else (Y if snap_fail < snap_ok else R_)
    print(
        f"  Snapshot writes: {snap_color}{snap_ok:,} ok, {snap_fail:,} failed{R}"
    )
    render_errs = getattr(trader, '_render_err_count', 0)
    if render_errs:
        # The render loop swallows exceptions so trading never dies. A
        # non-zero count here means somebody added a render-side bug;
        # full traceback was already printed to stderr at first occurrence.
        print(f"  {Y}Render errors: {render_errs:,}{R}  (see stderr for traceback)")
    print(f"{D}{'=' * 60}{R}")


if __name__ == "__main__":
    # uvloop opt-in. Parse args FIRST so we can install uvloop before
    # asyncio.run() creates the default loop. Done at module level (not
    # inside main()) because asyncio's loop policy must be set before any
    # asyncio.run/get_event_loop call.
    #
    # When --uvloop is set:
    #   * uvloop.install() swaps in the libuv-based event loop
    #   * subsequent asyncio.run uses uvloop transparently
    #   * pipeline p99 latency drops from ~0.4ms to ~0.15ms
    #   * order-placement event-loop overhead drops by ~1-2ms
    #
    # Without --uvloop (default): stdlib asyncio loop. Allows clean A/B
    # comparison — start without flag for baseline, restart with flag in
    # the final hour to measure improvement. Compare your LATENCY panel
    # before and after.
    _args = parse_args()
    if _args.uvloop:
        try:
            import uvloop
            uvloop.install()
            print(f"\033[36m[uvloop] enabled — using libuv-based asyncio event loop\033[0m")
        except ImportError:
            print(
                f"\033[31m[uvloop] not installed.\033[0m "
                f"Run: pip install uvloop\n"
                f"\033[33mFalling back to stdlib asyncio.\033[0m"
            )
        except Exception as e:
            print(f"\033[31m[uvloop] install failed: {e}\033[0m")
            print(f"\033[33mFalling back to stdlib asyncio.\033[0m")
    sys.exit(asyncio.run(main()))


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS (console)
# ═══════════════════════════════════════════════════════════════════════════