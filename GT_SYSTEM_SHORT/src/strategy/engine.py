import asyncio
from datetime import datetime
from typing import Optional, Callable, TYPE_CHECKING
from uuid import uuid4

from src.config.models import (
    TradeState, Config, OrderRegistry, OrderRecord,
    OrderSide, OrderType, OrderStatus,
    session_is_open, seconds_until_session_open, ET_ZONE,
    entries_allowed, seconds_until_entry_cutoff,
    ENTRY_CUTOFF_BUFFER_MIN,
)
from src.config.persistence import StateStore, AuditLog

if TYPE_CHECKING:
    from src.feed.handler import Tick


class NakedPositionError(Exception):
    """Raised at engine startup when the broker holds a FILLED position
    that doesn't match this client_id's saved state — i.e. the position
    was opened by some OTHER session (different client_id, manual TWS
    trade, a different bot, etc.) and we cannot safely add fresh exposure.

    Carries the diagnostics needed for the operator-facing error banner:
    the broker quantity, what we expected (if anything), and the recovery
    steps. Caught by LiveTrader.start() which prints the banner and exits
    with non-zero status — no dashboard, no feed subscription, no orders.

    NOT raised when:
      - saved state matches broker (adopted as orphan — engine continues)
      - paper mode (no real positions to clash with)
      - state IS already IN_POSITION (we're resuming our own cycle)
    """
    __slots__ = ('ticker', 'broker_qty', 'saved_qty', 'saved_entry', 'client_id')

    def __init__(self, ticker, broker_qty, saved_qty, saved_entry, client_id):
        self.ticker = ticker
        self.broker_qty = broker_qty
        self.saved_qty = saved_qty
        self.saved_entry = saved_entry
        self.client_id = client_id
        super().__init__(
            f"Broker holds {broker_qty} {ticker} but saved state for "
            f"client_id={client_id} doesn't match "
            f"(saved entry=${saved_entry or 0:.2f}, saved qty={saved_qty}). "
            f"Refusing to start to prevent double exposure."
        )


class ConflictingOpenOrderError(Exception):
    """Raised at engine startup when the broker has an ACTIVE (un-filled)
    BUY order for this ticker placed by a DIFFERENT client_id (or by manual
    TWS — client_id=0). Filled position is zero so `NakedPositionError`
    doesn't fire, but as soon as the trigger crosses, BOTH orders would
    fill and we'd end up with 2× the intended exposure.

    The other client owns the order — fill events route to ITS API session
    only — so we can't adopt it the way we adopt a position. Recovery is:
    cancel the conflicting order at the originating client, or wait for it
    to fill / expire / be cancelled in TWS.

    Caught by LiveTrader.start() with the same handler as NakedPositionError,
    a slightly different banner. No dashboard, no feed, exits with code 1.
    """
    __slots__ = ('ticker', 'orders', 'client_id')

    def __init__(self, ticker, orders, client_id):
        self.ticker = ticker
        self.orders = orders          # list[dict] of conflicting open orders
        self.client_id = client_id    # OUR client_id (the one being refused)
        ords_desc = ", ".join(
            f"{o.get('action','?')} {o.get('qty','?')} "
            f"(client_id={o.get('owning_client_id','?')}, "
            f"order_id={o.get('broker_id','?')})"
            for o in orders
        )
        super().__init__(
            f"Broker has active {ticker} order(s) from another client: "
            f"{ords_desc}. Refusing to start client_id={client_id} — "
            f"two clients armed on the same trigger would double-fill."
        )


class Engine:
    """
    Fixed Stop-Loss Breakout Re-Entry Engine.

    LTP-First Design: Uses Last Trade Price (from tick-by-tick) as the
    primary signal for entry/exit decisions.

    Strategy Logic (per doc):
    1. Monitor LTP (last trade price)
    2. If LTP >= trigger_price -> BUY
    3. Set fixed stop_loss = entry * (1 - stop_loss_pct)
    4. Track highest LTP during position
    5. If LTP <= stop_loss -> SELL
    6. Store highest LTP as previous_breakout_level
    7. If LTP >= previous_breakout_level -> BUY again
    8. Repeat

    State machine:
    IDLE -> MONITORING -> ORDER_ENTRY -> IN_POSITION -> EXIT -> WAITING_REENTRY -> MONITORING
    """

    __slots__ = (
        'config', 'gateway', 'state_store', 'audit_log',
        'registry', 'risk', 'logger',
        '_state', '_running', '_position_open', '_paused',
        '_entry_price', '_highest_price', '_stop_loss', '_quantity',
        '_previous_breakout_level', '_prev_ltp',
        '_trades_today', '_wins', '_losses', '_pnl', '_total_commission',
        # Commission accumulator from the in-flight BUY, applied to the
        # eventual SELL fill so per-cycle P&L deducts BOTH sides correctly.
        # Reset to 0.0 after each SELL fill. Persisted across restarts so
        # a crash mid-cycle doesn't lose the entry-side commission.
        '_pending_buy_commission',
        '_cycle_id', '_log_callback', '_ts',
        '_pending_side', '_pending_exit_reason', '_pending_stop',
        # Synchronously-set boolean that closes the TOCTOU race in
        # `_place_protective_stop`. The function has an `await` between
        # its idempotency guard (`if _pending_stop: return None`) and
        # the actual `_pending_stop = {...}` write — without this flag,
        # 8 different call sites (proactive on-fill, reactive _exit,
        # health-check re-arm, partial-fill give-up, reconcile, …)
        # could all pass the guard before any one of them set
        # `_pending_stop`, then each submit a MARKET SELL. Observed
        # in production 2026-05-26 (NFLX): 5 concurrent fallback
        # submissions sold 200×5=1000 shares against a 200-share
        # position → account went SHORT 800 shares.
        # The flag is set synchronously the instant we pass the guard
        # (before any await) and cleared in a `finally` so a thrown
        # exception doesn't leave it stuck. Per-instance, ~ns to read.
        '_protective_stop_placing',
        # Mirror of _protective_stop_placing for ENTRY placements. Set
        # synchronously the moment we pass the entry guard (before any
        # await), cleared in finally so an exception doesn't leave it
        # stuck. Closes the EURUSD 2026-06-09 double-entry race:
        # after SL fired, two trigger checks both passed the
        # `_pending_stop is None` guard within the same millisecond and
        # both called `_place_entry_stop_limit` → two brackets submitted
        # (n4, n5) instead of one. The phantom-SELL guard contained the
        # damage but the audit log was noisy. With this flag, only the
        # first caller wins; the second returns None immediately.
        '_entry_placing',
        # --market is a ONE-SHOT for the launch entry. An upstream detector
        # (the RTH trendline runner) saw exactly one breakdown and started this
        # bot for it; that entry has nothing left to wait for and goes in at
        # market. Every LATER entry — the re-entry after a cover, the
        # session-open placement — is the engine's own decision on a level
        # price has not reached yet, and MUST rest a STP-LMT. Firing those at
        # market would short instantly at the inflated post-cover bid, the
        # mirror of the regression the re-entry comment records.
        '_market_entry_used',
        # A62 (2026-06-10): set True while `_reconcile_open_orders` is
        # walking broker state on startup. The invariant sweep skips
        # iterations while this is True — without it, the sweep races
        # against reconcile and cancels brackets we're about to adopt
        # (parent goes into _pending_stop in the per-order loop, child
        # adoption happens in the post-loop bracket-pair step, but the
        # sweep firing in between sees the child as orphan → cancels →
        # A57 cascades to parent → engine re-places fresh bracket).
        '_reconciling',
        # Fencing-token peer of gateway.connection_epoch (SHORT mirror of the
        # LONG EURUSD 2026-07-28 naked short). Holds the connection epoch the
        # ledger was last reconciled against. Actuators may act only when this
        # equals gateway.connection_epoch. NOT persisted — re-inits to -1 every
        # start so a restart begins gate-closed until the first reconcile.
        '_ledger_epoch',
        # Snapshot of _pending_stop as it was on the LAST disk save.
        # Reconciliation populates _pending_stop fresh from broker truth;
        # comparing against this tells us if a saved intent didn't survive
        # (rejected order, mid-placement crash, user cancel in TWS).
        '_pending_stop_intent',
        # Bracket-order child SELL stop tracking. Set when a bracket
        # entry is submitted (parent STP-LMT BUY + child SELL STP-market)
        # via `gateway.place_bracket_buy_stop_market`. The child rests at
        # the broker the instant the bracket is accepted — so even if
        # the engine disconnects between parent placement and parent
        # fill, the position is protected without engine intervention.
        # On parent BUY fill (is_complete=True), the child is promoted
        # to `_pending_stop` and its trigger is modified to use the
        # actual fill VWAP (`modify_stop_trigger`). Set to None outside
        # the "bracket submitted, parent not yet fully filled" window.
        # Persisted to state file so recovery sees the bracket on restart.
        '_bracket_child',
        # Client-id suffix for engine_id construction (multi-bot disambig).
        '_cid_suffix',
        # Monotonic per-cycle counter folded into engine_ids as `_n{N}` so a
        # leftover order from cycle N (e.g. an un-cancelled bracket child)
        # cannot have its broker fill misattributed to cycle N+1's same-
        # named order. Incremented every time `_place_entry_stop_limit`
        # starts a new entry attempt. Persisted to state so restart
        # preserves uniqueness across the boundary; if state is missing /
        # corrupt we start from 0 — fine because previous cycles' orders
        # would have been swept by --reset / verify_symbol_flat already.
        # Was the root cause of the 2026-05-27 TSLA $44k phantom-PnL
        # incident: cycle B's leftover BR_SELL fired in cycle C's window
        # and got booked against cycle C's empty entry state.
        '_cycle_seq',
        # Per-cycle override of `config.stop_loss_pct`. When restored from
        # a saved state file on restart, this carries the pct that was
        # active when the CURRENT position's bracket child was placed —
        # so a user restart with a different --stop-pct CLI flag doesn't
        # silently move the protective stop on an already-open position.
        # Cleared to None on SELL fill (position close) so the NEXT cycle
        # picks up whatever config the user is now running with.
        # 2026-05-27 META postmortem: original SL armed at $617.31 (0.20%
        # below entry) was silently replaced on restart with $612.36
        # (1.00%) because _load_state recomputed from current config.
        '_active_stop_pct',
        # Snapshot of `_feed.high` (IBKR's BBO-aggregated daily-high field)
        # at the moment the CURRENT cycle's BUY first filled. Used as the
        # baseline in `_track_high`: if IBKR reports a daily high that
        # has RISEN above this baseline during the cycle, the rise can
        # only have come from a trade that printed during this cycle, so
        # we adopt it as the new `_highest_price`.
        # Closes a real under-tracking bug confirmed 2026-06-01 on IBM —
        # IBKR reported a $327.98 print 320 ms after BUY fill, but our
        # tick-by-tick TRADE feed only captured up to $327.91, so the
        # next re-entry trigger was set 7 cents too low.
        # Cleared (set to 0) on SELL fill so the NEXT cycle re-baselines.
        '_feed_high_at_entry',
        # Partial-fill chase: dict[order_id -> asyncio.Task] of pending
        # chase timers, and dict[order_id -> int] of attempt counts.
        # Must be in __slots__ since the class declares __slots__ — otherwise
        # the first `self._partial_fill_chases = {}` raises AttributeError.
        '_partial_fill_chases', '_partial_fill_chase_count',
        # Throttle timestamp for active reconnect attempts. Set in
        # _try_active_reconnect to enforce min-10s gap between calls
        # to gateway.connect() during a TWS-restart window.
        '_last_reconnect_attempt_ts',
        # Stale-feed self-heal (health-check Probe 3). `_stale_feed_repair`
        # is an injected hook (run_live wires it to
        # ConnectionManager.request_repair) because the engine detects the
        # deaf feed but does NOT own the feed/supervisor that can rebuild
        # it. `_last_stale_repair_ts` cools down repeat attempts so a
        # non-socket root cause degrades to one reconnect per cooldown
        # instead of a reconnect every health tick.
        '_stale_feed_repair', '_last_stale_repair_ts',
        # A44 — throttle POSITION_MISMATCH alerts so the same persistent
        # divergence doesn't spam Slack/Teams every reconcile tick. Stores
        # (last_alert_ts, last_broker_qty, last_engine_qty). Re-alert only
        # if 60s elapsed OR the numbers changed (escalation).
        '_last_mismatch_alert',
        # Per-startup session id (4 hex chars). Folded into engine_ids
        # via `_make_engine_id` so a stale fill from a prior session
        # cannot collide with a fresh cycle in the current session.
        # Live regression 2026-06-09 AUDUSD: state file was lost
        # between restarts, _cycle_seq reset to 0, and `_n1` was
        # reused — caused audit-log confusion attributing fills to
        # the wrong cycle. Generated once in __init__; never changes
        # during the engine's lifetime.
        '_session_id',
        '_feed',  # production_feed reference for latency stats
        '_order_history',  # order history for dashboard
        '_audit',  # comprehensive audit manager
        '_state_save_counter',  # tick counter for periodic state saves
        # Bounded tick queue for backpressure (drop-oldest)
        '_tick_queue', '_tick_consumer_task', '_ticks_dropped',
        # Scheduled daily reset task (rolls counters at midnight ET).
        '_daily_reset_task',
        # Session-window controller: sleeps until next ETH open, flips
        # PAUSED on/off, opt-out via config flag for testing.
        '_session_controller_task', '_rth_only',
        # Health-check loop: invariant probes every 30s.
        '_health_check_task',
        # A46 invariant-sweep task — runs at 250ms cadence to enforce
        # "if engine is FLAT and no bracket is pending, no SELL STP
        # should exist at the broker for our symbol". Kills any orphan
        # bracket child that survives parent cancellation.
        '_invariant_sweep_task',
        # Periodic state-file save loop (every 2s while engine is running).
        # All other `_save_state()` calls are EVENT-DRIVEN — they fire on
        # fills, new highs, state transitions, etc. The gap between events
        # used to be unbounded (could be minutes during MONITORING), so a
        # restart inside that window would lose any in-memory mutations
        # that hadn't yet triggered a save. The 1-second cadence caps
        # that gap to 1s, making the ZM-class memory-drift bug
        # (_bracket_child cleared between events, restart loses it,
        # legacy fallback places duplicate SELL) essentially impossible
        # without a hard crash inside the 1-second window.
        '_periodic_state_save_task',
        # AlertManager + market-clock hooks set by run_live wiring.
        '_alerts', '_session_start_equity',
        # Wall-clock timestamp captured at engine.start(). Used as the
        # lower bound for fill-replay when the saved state file has no
        # 'updated_at' (i.e. fresh start / --reset). Without this floor,
        # _reconcile_missed_fills() walks ALL of today's broker fills and
        # replays them as if they just happened — populating _order_history
        # with phantom BUY→SELL→BUY entries that the user sees in the
        # dashboard but that never actually re-executed at the broker.
        '_engine_started_at',
        # FL9 — True iff this session resumed WITH an open position (saved
        # state had position_open). Gates the A43 execution-sum session-start
        # floor; see the init-site comment for the full rationale.
        '_started_with_position',
        # Multi-asset abstraction (D2-PM). Resolved once in __init__
        # from the config's ticker via SpecRegistry. Carries all
        # asset-class-dependent behavior — contract construction,
        # tick rounding, price source selection, sizing math,
        # commission estimate, session calendar. The hot path is one
        # attribute access (`self._asset_spec.tick.round_to_tick(p)`),
        # cached at the class level — no per-tick dict lookup.
        # For equity (the existing case), the spec composes policies
        # that produce byte-identical behavior to the previous
        # hardcoded `round(x, 2)` / `Stock(...)` paths.
        '_asset_spec',
        # Authoritative short-margin preview from IBKR's whatIf Order
        # Preview (PDF §6), captured best-effort at start() and cached
        # here. dict|None. Surfaced in get_status() so the dashboard can
        # show the REAL init/maint margin (the offline ShortPolicy is
        # only a conservative estimate). Never affects order placement.
        '_short_margin_whatif',
        # LIVE shortability + borrow cost from IBKR (generic tick 236 +
        # FEE_RATE feed), captured best-effort at start() alongside the
        # whatIf preview. dict|None. Replaces the hardcoded hard_to_borrow
        # bool and the 25-bps borrow-rate placeholder: the offline
        # ShortRequirement is recomputed with these live inputs, and the
        # dashboard shows the real availability + fee. Never affects orders.
        '_short_shortable',
        # Entry-rejection backoff (Probe 2 of the health-check loop).
        # When IBKR rejects an ENTRY_BUY (e.g. invalid FX price grid,
        # margin failure, contract spec mismatch), the health-check used
        # to blindly re-place every 30s — flooding the broker with the
        # same doomed order until somebody intervened. We now record
        # the most recent ENTRY rejection here so Probe 2 can skip
        # auto re-placement for ENTRY_REJECTION_BACKOFF_SECONDS and
        # surface a clearer alert. Reset to None on:
        #   - successful ENTRY_BUY placement (cleared by _place_entry_stop_limit)
        #   - operator-driven --reset or pause/resume
        #   - SELL fill (cycle close — fresh slate for the next cycle).
        '_last_entry_rejected_at',
        '_last_entry_rejected_reason',
        # FL2 — persistent, exactly-once fill ledger. Durable journal of
        # OUR executions; the position truth for FX (positions() is blind
        # to the currency graph's cycle space, dim E-V+C) and the gap-free
        # backbone under A43's get_our_position_via_executions. One file
        # per (symbol, port, client_id) so 1..32 bots coexist with no
        # contention. Purely additive — a ledger fault never disturbs the
        # fill path. None when GT_DISABLE_FILL_LEDGER=1 or init fails.
        '_fill_ledger',
    )

    def __init__(
        self,
        config: Config,
        gateway: "Gateway",
        state_store: Optional[StateStore] = None,
        audit_log: Optional[AuditLog] = None,
        audit: Optional["AuditManager"] = None,
        risk_check: "RiskCheck" = None,
        logger: "QuantLogger" = None,
    ):
        self.config = config
        self.gateway = gateway
        self.state_store = state_store or StateStore()
        self.audit_log = audit_log
        self._audit = audit  # Comprehensive audit manager

        # ── Multi-asset wiring (D2-PM) ────────────────────────────────
        # Resolve the AssetSpec for this engine's ticker. Default
        # behavior (no explicit hint) routes US equity tickers to
        # USEquitySpec which preserves the previous round(x, 2) /
        # Stock(...) / SMART/USD assumptions byte-identically. For
        # FX / CFDs / Futures, the appropriate spec is selected
        # automatically by symbol shape (EURUSD → ForexSpec, etc.).
        #
        # Falls back to None ONLY if resolution fails (unknown symbol,
        # unsupported asset class). Engine code that calls into the
        # spec must guard for None or rely on resolution succeeding
        # — for production all symbols should resolve. A None spec
        # downgrades the engine to legacy hardcoded behavior so
        # nothing crashes during the migration window.
        try:
            from src.assets import resolve as _resolve_spec
            self._asset_spec = _resolve_spec(config.ticker)
        except Exception as _spec_err:
            # Defensive: never let spec resolution kill the engine.
            # Legacy paths will run if _asset_spec is None.
            self._asset_spec = None
            # ...but say so, loudly. A None spec silently reverts every
            # session check to the hardcoded US-equity window (09:30-16:00
            # ET). For an FX or futures bot that means sitting idle through
            # its real trading hours, refusing entries with no clue why. The
            # logger is wired later in __init__, so this goes to stderr --
            # which is what the operator is watching in the pane anyway.
            import sys as _sys
            print(
                f"\n*** WARNING: no AssetSpec for {config.ticker!r} "
                f"({type(_spec_err).__name__}: {_spec_err}).\n"
                f"*** Falling back to US-equity behaviour: session hours "
                f"09:30-16:00 ET, 2dp tick rounding, SMART/USD routing.\n"
                f"*** If {config.ticker!r} is NOT a US equity, entries will be "
                f"refused outside those hours and prices may round wrong.\n",
                file=_sys.stderr, flush=True,
            )

        # Authoritative short-margin preview (IBKR whatIf); populated
        # best-effort by preview_short_margin() after connect. None until
        # then — get_status() falls back to the offline ShortPolicy.
        self._short_margin_whatif = None

        # Live shortability + borrow cost (IBKR generic tick 236 +
        # FEE_RATE); populated best-effort by preview_short_shortable()
        # after connect. None until then — the ShortPolicy uses its offline
        # placeholders as the fallback.
        self._short_shortable = None

        # Entry-rejection backoff state (see __slots__ docstring).
        self._last_entry_rejected_at = None
        self._last_entry_rejected_reason = None

        # ── FL2: persistent fill ledger ───────────────────────────────
        # Durable, exactly-once journal of OUR executions. One file per
        # (symbol, port, client_id) — scales to the 32-client TWS cap with
        # no contention (matches the A79 single-writer model). Purely
        # additive: a ledger fault never affects fill processing. Opt-out
        # via GT_DISABLE_FILL_LEDGER=1. Lives beside the state file so the
        # monitor can read it with no broker connection (frees a slot).
        self._fill_ledger = None
        try:
            import os as _os
            if _os.environ.get("GT_DISABLE_FILL_LEDGER", "") not in ("1", "true", "TRUE"):
                from src.execution.fill_ledger import FillLedger
                _sp = getattr(self.state_store, "path", "") or ""
                _data_dir = _os.path.dirname(_sp) or "."
                _port = int(getattr(self.gateway, "port", 0) or 0)
                _cid = int(getattr(self.config, "ibkr_client_id", 0) or 0)
                _lpath = FillLedger.path_for(_data_dir, self.config.ticker, _port, _cid)
                self._fill_ledger = FillLedger(_lpath)
                # FL4 — lend the ledger to the gateway so its A43 truth
                # source (get_our_position_via_executions) is durable for
                # every caller. Read-only borrow; gateway never owns it.
                try:
                    self.gateway._fill_ledger = self._fill_ledger
                except Exception:
                    pass
        except Exception:
            # Never let ledger init kill the engine — legacy paths run fine.
            self._fill_ledger = None

        # Throttle for active-reconnect attempts (see _try_active_reconnect).
        # None sentinel: self._ts() returns a datetime, so we can't init
        # with a float (subtracting would TypeError). _try_active_reconnect
        # guards with `if last is not None`.
        self._last_reconnect_attempt_ts = None

        # Stale-feed self-heal hook (see __slots__). None ⇒ detection-only
        # behaviour, i.e. exactly what the engine did before the watchdog
        # was wired: alert and carry on. Tests and paper runs that never
        # call set_stale_feed_repair() are therefore unaffected.
        self._stale_feed_repair: Optional[Callable] = None
        self._last_stale_repair_ts = None
        # A44 — last POSITION_MISMATCH alert state: (ts, broker_qty, engine_qty)
        # for throttling. None = no prior alert.
        self._last_mismatch_alert = None

        # Correlation ID for this trading cycle
        self._cycle_id = str(uuid4())[:8]

        # Order tracking
        self.registry = OrderRegistry()
        self._order_history: list[OrderRecord] = []  # Separate history for dashboard

        # Risk management
        self.risk = risk_check

        # Structured logging
        self.logger = logger
        if logger:
            logger.set_cycle_id(self._cycle_id)

        # Cached timestamp function
        self._ts = datetime.now

        # Core trading state
        self._state = TradeState.IDLE
        self._running = False
        self._position_open = False
        self._paused = False

        # Entry tracking
        self._entry_price: Optional[float] = None
        self._highest_price: Optional[float] = None
        self._stop_loss: Optional[float] = None
        self._quantity: int = 0

        # Re-entry tracking (per doc section 11-12)
        self._previous_breakout_level: Optional[float] = None

        # LTP tracking for crossing detection
        self._prev_ltp: float = 0.0

        # State save counter (for periodic persistence)
        self._state_save_counter: int = 0

        # Feed reference for latency stats (set by run_live.py)
        self._feed = None

        # Stats
        self._trades_today: int = 0
        self._wins: int = 0
        self._losses: int = 0
        self._pnl: float = 0.0
        self._total_commission: float = 0.0
        # BUY-side commission for the open cycle; added to the SELL-side
        # commission on exit to form the round-trip cost. Reset after SELL.
        self._pending_buy_commission: float = 0.0

        # Logging (legacy)
        self._log_callback: Optional[Callable] = None

        # Pending fill tracking (set BEFORE place_order so paper sync fills work)
        self._pending_side: Optional[str] = None    # 'BUY' or 'SELL'
        self._pending_exit_reason: Optional[str] = None
        # _pending_stop is the live "we have a resting protective/breakout
        # stop-limit at the broker" handle. MUST be initialized here even
        # though __slots__ declares it — otherwise the first read in
        # _save_state() (called by _gap_fill_highest_price() during
        # _reconcile_open_orders() at startup, BEFORE any code has assigned
        # to it) raises AttributeError. This was the TSLA IN_POSITION
        # restore crash.
        self._pending_stop: Optional[dict] = None
        self._pending_stop_intent: Optional[dict] = None
        # See __slots__ docstring — atomic guard against the
        # 8-call-site TOCTOU race in `_place_protective_stop`.
        self._protective_stop_placing: bool = False
        # Entry-placement atomic claim (mirrors _protective_stop_placing).
        # See __slots__ docstring for the EURUSD 2026-06-09 incident.
        self._entry_placing: bool = False
        # --market applies to the launch entry only. See __slots__.
        self._market_entry_used: bool = False
        # A62: reconcile-in-progress flag — invariant sweep skips while True.
        self._reconciling: bool = False
        # Fencing-token peer of gateway.connection_epoch (see __slots__).
        # -1 so the actuation gate is CLOSED until the first reconcile stamps
        # it; deliberately NOT persisted (kept out of _save_state /
        # _load_state) so every restart re-earns "trusted" via a fresh
        # reconcile rather than trusting a value from a previous process.
        self._ledger_epoch: int = -1
        # Bracket-order child SELL stop (see __slots__ for full rationale).
        # None outside the bracket-active window.
        self._bracket_child: Optional[dict] = None

        # Engine-id suffix carrying the client_id so two bots on the
        # same ticker (e.g. different timeframes / strategies) don't
        # share engine_ids in audit logs and registry lookups. Example:
        # NVDA with client_id=1 → "_c1", client_id=2 → "_c2".
        # Format: ENTRY_BUY_1000_NVDA_c1
        # Reconcile recognizes BOTH old format (no suffix — pre-migration
        # state files) and new format, so a saved state from before this
        # change still loads cleanly.
        self._cid_suffix: str = f"_c{config.ibkr_client_id}"

        # Monotonic per-cycle counter for engine_id disambiguation.
        # Bumped at the head of every `_place_entry_stop_limit` invocation
        # so each cycle's parent/child/SL trio gets a fresh `_n{N}` suffix.
        # See `_make_engine_id` for the actual id-construction helper.
        self._cycle_seq: int = 0

        # Per-startup session id. Folded into engine_ids so a stale fill
        # from a prior session can't collide with the current session's
        # n-counter (which resets to 0 when state is lost). 4 hex chars
        # = 65k unique sessions before any chance of collision, plenty.
        self._session_id: str = uuid4().hex[:4]

        # Per-cycle SL pct override (see __slots__ for rationale). None
        # outside the "saved position restored from disk" window.
        self._active_stop_pct: Optional[float] = None

        # Snapshot of IBKR's daily-high at cycle entry (see __slots__).
        # Reset to 0 outside an active cycle.
        self._feed_high_at_entry: float = 0.0

        # Partial-fill chase bookkeeping. Initialized empty so the chase
        # code can `self._partial_fill_chases[oid] = task` without the
        # lazy `hasattr(...)` guard tripping on __slots__.
        self._partial_fill_chases: dict = {}
        self._partial_fill_chase_count: dict = {}

        # Bounded tick queue with drop-oldest backpressure. The feed used
        # to schedule one asyncio.Task per tick via create_task — at NVDA
        # rates (~10K ticks/s × 2 layers) that's 20K task allocations/sec
        # with no upper bound, so a slow tick processor would let the task
        # queue grow until the loop drowned. Now `on_tick` is a sync push
        # to this queue; a single consumer task drains it.
        # Depth 500 ≈ 50ms of NVDA ticks: gives some slack for transient
        # bursts but discards stale ticks if we fall further behind.
        # Drop-oldest semantics so the strategy always sees fresh market
        # state, never a stale one queued behind newer arrivals.
        self._tick_queue: Optional[asyncio.Queue] = None  # created in start()
        self._tick_consumer_task: Optional[asyncio.Task] = None
        self._ticks_dropped: int = 0
        # Background task that fires at midnight to reset daily risk
        # counters (so the bot can rest GTC orders overnight without the
        # counters going stale).
        self._daily_reset_task: Optional[asyncio.Task] = None

        # Session window controller: auto-PAUSE outside the asset's session
        # (US equity spec = NYSE RTH 09:30-16:00 ET Mon-Fri). Default ON.
        #
        # Set GT_RTH_ONLY=0 to DISABLE — the engine then never pauses on
        # session, so it will place entries in pre-market / after-hours (or
        # for assets whose real session differs from the equity RTH the spec
        # enforces). Useful when the dashboard's ETH window (04:00-20:00)
        # looks "open" but the engine is pausing because equity RTH is
        # closed. NOTE: a live equity STOP-LIMIT still only *fills* in
        # extended hours if the order carries outsideRth=True (see broker).
        self._session_controller_task: Optional[asyncio.Task] = None
        import os as _os
        self._rth_only: bool = _os.environ.get(
            "GT_RTH_ONLY", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        # Health-check task — periodic invariant probes (position→SL,
        # MONITORING→entry, feed-staleness, equity-drawdown).
        self._health_check_task: Optional[asyncio.Task] = None
        self._invariant_sweep_task: Optional[asyncio.Task] = None
        # Periodic state-file save task — every 1 second. See __slots__
        # entry for the full rationale.
        self._periodic_state_save_task: Optional[asyncio.Task] = None
        # Optional AlertManager (file/stdout/slack). Wired by run_live.
        # When None, alert calls are no-ops — engine still works standalone.
        self._alerts = None
        # Equity at session start, used by the drawdown health-check.
        self._session_start_equity: float = 0.0
        self._engine_started_at: Optional[datetime] = None  # set in start()
        # FL9 — was this bot's session resumed WITH an open position (state
        # file had position_open=True), or did it boot FLAT? Set once in
        # start() right after _load_state(). Drives the A43 execution-sum
        # floor: a FLAT boot floors the exec lookup at _engine_started_at so
        # stale pre-restart fills on a REUSED clientId can't be adopted as a
        # phantom; a resumed-WITH-position boot keeps the full-history lookup
        # (since=None) so its real pre-restart entry is still recovered.
        self._started_with_position: bool = False

        # Wire gateway callbacks. _on_fill handles successful fills; the
        # status hook handles terminal non-fill outcomes (Rejected,
        # Cancelled, Inactive) so a rejected SL doesn't leave the position
        # unprotected with _pending_stop set forever.
        gateway._on_fill = self._on_gateway_fill
        gateway._on_order_status = self._on_order_status_change
        # Late-arriving commission reports. IBKR sends commissionReport as
        # a SEPARATE message ~100ms-1s after the fillEvent — so at fillEvent
        # time `fill.commissionReport` is usually None, our path falls back
        # to the modeled equity estimate ($92 on EURUSD instead of $2), and
        # the PnL is silently wrong forever. The Gateway now subscribes to
        # commissionReportEvent on every order it places; this hook is
        # called once per execution with the true broker-charged commission,
        # and the engine writes it into the OrderRecord. Subsequent calls
        # to `order.calculate_commission()` then return truth, not the
        # modeled fallback. See `_on_gateway_commission` for the handler.
        gateway._on_commission = self._on_gateway_commission

    @property
    def state(self) -> TradeState:
        return self._state

    def set_log_callback(self, cb: Callable):
        self._log_callback = cb

    def set_stale_feed_repair(self, cb: Callable):
        """Wire the stale-feed watchdog to a repair path (Probe 3).

        `cb(reason: str) -> bool` should start a full connection repair —
        re-establish the socket AND re-subscribe market data — returning
        True if one was started. run_live wires this to
        ConnectionManager.request_repair. Left unset, Probe 3 only alerts.

        NOTE: a repair that reconnects the socket without rebuilding the
        FeedHandler does NOT heal a deaf feed (the handler keeps its
        reference to the old IB instance), and the watchdog would then
        reconnect on every health tick forever. Any hook installed here
        must go through the supervisor's on_reconnect path.
        """
        self._stale_feed_repair = cb

    def _log(self, msg: str):
        if self._log_callback:
            self._log_callback(msg)
        else:
            print(f"[{self._ts():%H:%M:%S}] {msg}")

    def _session_is_open(self, now=None) -> bool:
        """Spec-aware session check (D4-extra).

        Routes through self._asset_spec.session.is_open_at() when a
        spec is available, so FX uses ForexContinuousSession (24/5),
        futures use their venue calendar, etc. Falls back to the
        global US-equity session_is_open() helper when no spec
        (legacy code paths, unknown symbols).

        The bug we're closing: before D4, the engine called the
        global session_is_open() unconditionally, which is hardcoded
        to NYSE 09:30-16:00 ET. An FX bot launched at 17:14 ET saw
        "session closed, pause" and never started — even though FX
        is open 24/5 and SHOULD have been trading.
        """
        from datetime import datetime, timezone
        if self._asset_spec is not None:
            try:
                ts = now or datetime.now(tz=timezone.utc)
                # Ensure tz-aware (asset session policies require it)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return self._asset_spec.session.is_open_at(ts)
            except Exception:
                pass  # fall through to global
        return session_is_open()

    def _seconds_until_session_open(self) -> float:
        """Spec-aware countdown to next session open."""
        from datetime import datetime, timezone
        if self._asset_spec is not None:
            try:
                now = datetime.now(tz=timezone.utc)
                next_open = self._asset_spec.session.next_open(now)
                return max(0.0, (next_open - now).total_seconds())
            except Exception:
                pass
        return seconds_until_session_open()

    def _entries_allowed(self) -> bool:
        """Spec-aware entries-allowed check.

        The end-of-session entry cutoff (block new BUYs within the last
        5 min of close) is now OPT-IN via GT_ENTRY_CUTOFF. DEFAULT (no
        env var) → entries are allowed right up to the close, for BOTH
        equity (NYSE close) and FX (Friday-22:00-UTC weekly close). The
        old "give the protective SELL time to land before close" reason
        no longer applies: the SELL is the bracket CHILD, submitted
        atomically with the parent BUY, so it is already resting at the
        broker the instant the entry exists — there is no naked window.
        Set GT_ENTRY_CUTOFF=1 to restore the 5-min block. This is the
        SAME switch that controls cancelling a resting BUY at the cutoff,
        so the whole end-of-session cutoff is all-or-nothing.
        """
        from datetime import datetime, timezone
        import os as _eco
        _cutoff_on = bool(_eco.environ.get("GT_ENTRY_CUTOFF", "").strip())
        if self._asset_spec is not None:
            try:
                now = datetime.now(tz=timezone.utc)
                # Block entries within last 5 min of session — ONLY when the
                # opt-in cutoff is enabled (default OFF → allow up to close).
                if _cutoff_on and self._asset_spec.session.is_within_n_minutes_of_close(now, 5):
                    return False
                return self._asset_spec.session.is_open_at(now)
            except Exception:
                pass
        return entries_allowed()

    async def _try_active_reconnect(self) -> bool:
        """Actively call gateway.connect() to re-establish socket after
        TWS restart.

        BACKGROUND (live regression 2026-06-08):
            Even after we added _try_recover_connection_status (which
            checks if ib_async's socket has come back), users observed
            "○ IBKR disconnected" persisting forever after TWS restart.
            ROOT CAUSE: ib_async does NOT auto-reconnect when TWS dies.
            isConnected() stays False; the socket never comes back on
            its own. We must actively call connectAsync ourselves.

        THIS METHOD:
            Throttled to once per 10s (don't hammer TWS). Calls the
            full gateway.connect() handshake — connectAsync + market
            data subscribe + contract pre-qualify. On success, the
            gateway's normal _on_connect callback fires and the engine
            resumes its event handlers.

        Returns True if reconnect succeeded, False otherwise. Best-
        effort: never raises; failure leaves status as-is and we'll
        try again on the next health-check tick.
        """
        now = self._ts() if hasattr(self, '_ts') else datetime.now()
        last = getattr(self, '_last_reconnect_attempt_ts', None)
        # Use .total_seconds() — `now` is a datetime, so subtracting 0 (the
        # old sentinel) raised TypeError: unsupported operand type(s) for -:
        # 'datetime.datetime' and 'float'. None sentinel + explicit seconds
        # check avoids this on the very first call.
        if last is not None and (now - last).total_seconds() < 10.0:
            return False
        self._last_reconnect_attempt_ts = now
        try:
            ib = getattr(self.gateway, '_ib', None)
            if ib is not None and ib.isConnected():
                return False  # socket already up; passive recovery handles
            # ── PER-BOT STAGGER (live regression 2026-06-09 multi-bot) ──
            # When 8 bots simultaneously try to reconnect to TWS after a
            # restart, IBKR's connection rate limit rejects some of them
            # (Error 326: Already connected, or silent drop). Result: only
            # 3 of 8 bots came back online.
            # Stagger by client_id so the 8 bots spread their reconnect
            # attempts across a ~4.5s window. With client_ids 80-87, the
            # spread is 0.0s, 0.5s, 1.0s, 1.5s, 2.0s, 2.5s, 3.0s, 3.5s —
            # at most 2 simultaneous attempts per second, well under TWS's
            # silent connection-rate cap.
            client_id = int(getattr(self.gateway, 'client_id', 0) or 0)
            stagger_s = (client_id % 10) * 0.5
            if stagger_s > 0:
                self._log(
                    f"[RECONNECT] staggering {stagger_s:.1f}s by client_id="
                    f"{client_id} to avoid TWS rate limit on simultaneous "
                    f"multi-bot reconnects"
                )
                await asyncio.sleep(stagger_s)
                # Re-check after sleep — maybe a passive recovery slipped in
                if ib is not None and ib.isConnected():
                    return False
            self._log(
                "[RECONNECT] Attempting active reconnect to TWS "
                f"({self.gateway.host}:{self.gateway.port} client_id="
                f"{self.gateway.client_id})..."
            )
            # A52 lifecycle log: reconnect attempt boundary. Pair with the
            # broker's CONNECT line to compute the actual offline window
            # from this bot's perspective.
            print(
                f"[BRACKET_LIFECYCLE] RECONNECT_ATTEMPT  "
                f"cid={self.gateway.client_id}  sym={self.config.ticker}  "
                f"at={now.isoformat()}  position_open={self._position_open}  "
                f"_bracket_child={getattr(self, '_bracket_child', None)}  "
                f"_pending_stop={self._pending_stop}"
            )
            ok = await self.gateway.connect()
            if ok:
                self._log("[RECONNECT] gateway.connect() SUCCEEDED — "
                          "socket up, contract qualified, account "
                          "subscription armed. Engine resuming.")
                if self._alerts:
                    try:
                        from src.infra.alerts import AlertSeverity
                        self._alerts.raise_alert(
                            code="CONNECTION_RECOVERED",
                            severity=AlertSeverity.MEDIUM,
                            message=("Active reconnect to TWS succeeded "
                                     "after disconnect window."),
                            context={"ticker": self.config.ticker},
                            correlation_id=getattr(self, '_cycle_id', ''),
                        )
                    except Exception:
                        pass
                return True
            self._log("[RECONNECT] gateway.connect() returned False "
                      "(TWS likely still down). Will retry in 10s.")
            return False
        except Exception as e:
            self._log(f"[RECONNECT] active reconnect failed ({type(e).__name__}: "
                      f"{e}). Will retry in 10s.")
            return False

    # Min gap between stale-feed repair attempts. The health loop probes
    # every 30s; if the root cause ISN'T the socket (e.g. an IBKR market-data
    # entitlement/subscription problem) a repair won't heal it, and without a
    # cooldown we'd tear down and rebuild the connection every 30s forever.
    # 120s bounds that to one reconnect per 2 min while still healing a
    # transient deaf socket within ~1 probe. We never stop retrying: staying
    # deaf forever is the bug we're fixing.
    STALE_REPAIR_COOLDOWN_S = 120.0

    def _maybe_repair_stale_feed(self, age: float) -> bool:
        """Trigger the connection repair for a CONNECTED-but-deaf feed.

        Called from health Probe 3 once staleness is detected. Every guard
        here exists to keep the watchdog from firing when silence is
        legitimate:

          * no hook          → detection-only (unchanged legacy behaviour)
          * gateway not connected → the DISCONNECTED path already owns this;
            repairing here would race it
          * session closed   → silence is EXPECTED (nights/weekends). Without
            this the bot would reconnect every cooldown all night. Uses
            `_session_is_open()` (pure/race-free, spec-aware so FX's 24x5
            calendar is honoured) — deliberately NOT `_paused`, which the
            session controller sets asynchronously and can lag reality.
          * cooldown         → bounds thrash when repair can't fix the cause

        Best-effort: never raises into the health loop.
        """
        cb = self._stale_feed_repair
        if cb is None:
            return False
        if not getattr(self.gateway, 'connected', False):
            return False
        try:
            if not self._session_is_open():
                return False
        except Exception:
            return False  # can't prove the session is open → don't touch it

        now = self._ts()
        last = self._last_stale_repair_ts
        if last is not None and (now - last).total_seconds() < self.STALE_REPAIR_COOLDOWN_S:
            return False
        self._last_stale_repair_ts = now

        self._log(
            f"[STALE_FEED] connected but no tick for {age:.0f}s during open "
            f"session — requesting connection repair (re-establish socket + "
            f"re-subscribe market data)."
        )
        try:
            started = bool(cb("stale_feed"))
        except Exception as e:
            self._log(f"[STALE_FEED] repair request failed ({type(e).__name__}: {e}).")
            return False
        if started:
            self._log("[STALE_FEED] repair started; feed should resume within ~seconds.")
        else:
            self._log("[STALE_FEED] repair declined (one already in flight).")
        return started

    def _try_recover_connection_status(self) -> bool:
        """Try to detect and recover from a stale-disconnect state.

        BACKGROUND (live regression 2026-06-06):
            User killed TWS → gateway._status flipped to DISCONNECTED.
            User restarted TWS → ib_async's underlying socket reconnected
            BUT gateway._status was never flipped back to CONNECTED. The
            engine stayed in "disconnected" state forever (dashboard
            showed "disconnected" 2+ minutes after TWS was back up).

        THIS METHOD:
            Reads the underlying ib_async socket state directly (which
            is event-driven and reflects reality). If the socket is up
            but our Gateway._status is still DISCONNECTED, that's a
            stale-disconnect — flip the status back, re-arm whatever
            needs re-arming, and let the next health check resume normal
            operation.

        Returns True if a recovery happened, False otherwise. Best-
        effort: never raises; failure leaves status as-is.
        """
        try:
            ib = getattr(self.gateway, '_ib', None)
            if ib is None:
                return False
            socket_alive = bool(ib.isConnected())
            engine_thinks_connected = getattr(self.gateway, 'connected', False)
            if socket_alive and not engine_thinks_connected:
                # Real reconnect detected. Flip status.
                from src.config.models import ConnectionStatus
                self.gateway._status = ConnectionStatus.CONNECTED
                try:
                    self.gateway._last_heartbeat = self._ts()
                except Exception:
                    pass
                self._log(
                    "[RECONNECT] ib_async socket is back up; Gateway._status "
                    "was stale at DISCONNECTED — flipping to CONNECTED. "
                    "Health check + reconcile will resume on next tick."
                )
                if self._alerts:
                    try:
                        from src.infra.alerts import AlertSeverity
                        self._alerts.raise_alert(
                            code="CONNECTION_RECOVERED",
                            severity=AlertSeverity.MEDIUM,
                            message=(
                                "Broker connection recovered after a stale-"
                                "disconnect window. Engine state was preserved "
                                "throughout; resuming normal operation."
                            ),
                            context={"ticker": self.config.ticker},
                            correlation_id=getattr(self, '_cycle_id', ''),
                        )
                    except Exception:
                        pass
                return True
            return False
        except Exception:
            # Recovery is best-effort. Never bubble — better to stay in
            # the safe (disconnected) state than crash trying to recover.
            return False

    def _price_epsilon(self) -> float:
        """Half a tick — the smallest meaningful price delta on this
        asset. Used as the "is the stop change worth re-modifying?"
        threshold so we don't spam IBKR with no-op modifies on
        sub-tick noise.

        Equity (0.01 tick) → 0.005 (the legacy hardcoded value, byte-identical).
        EURUSD (0.00005 tick) → 0.000025 — captures every half-pip move.
        USDJPY (0.005 tick)   → 0.0025
        ES futures (0.25 tick) → 0.125

        Without this method-level resolution, the engine used the
        equity 0.005 everywhere — and on FX where every legitimate
        stop adjustment is < 0.0001, the threshold was 50x too large,
        so the engine silently never re-modified after partial fills.
        """
        # Runtime tick (venue-reported via ContractDetails) wins over
        # the spec's hardcoded default — same rationale as `_round_to_tick`.
        # `getattr` so the method works on any object with _asset_spec
        # (covers unit-test stubs that don't subclass Engine).
        _rt_fn = getattr(self, '_runtime_min_tick', None)
        rt = _rt_fn() if callable(_rt_fn) else None
        if rt is not None and rt > 0:
            return max(rt * 0.5, 1e-9)

        if self._asset_spec is None:
            return 0.005
        try:
            # `tick_size(price)` is the uniform Protocol method — works
            # for DecimalTickPolicy (equity), PipTickPolicy (FX), and
            # FuturesTickPolicy (futures). The earlier `.tick` attribute
            # access only worked for FX and silently failed for futures
            # (returned 0.005 fallback — wrong by a factor of 25 on ES).
            from src.assets.types import price as _to_price
            return max(float(self._asset_spec.tick.tick_size(_to_price(1.0))) * 0.5, 1e-9)
        except Exception:
            return 0.005

    def _runtime_min_tick(self) -> Optional[float]:
        """Venue-reported minimum tick discovered at qualify time.

        Returns None when:
          - The gateway hasn't qualified the contract yet (engine boot).
          - The gateway is in paper mode without a real contract.
          - The ContractDetails RPC failed silently.

        Engine math (round_to_tick, price_epsilon, etc.) prefers this
        value over the spec's hardcoded default. The spec's value is a
        safe offline-test fallback only — venue is the source of truth
        at runtime, because account type / contract listing / venue
        routing all affect the actual valid price grid.
        """
        try:
            getter = getattr(self.gateway, 'get_runtime_min_tick', None)
            if getter is None:
                return None
            return getter()
        except Exception:
            return None

    def _protective_stop_price(self, entry_price, stop_pct) -> float:
        """Compute the protective BUY-STOP price for a SHORT with **zero float drift**.

        ── SHORT INVERSION (see SHORT_CONVERSION_CHANGES.md, P2) ──
        For a short position the protective stop is a BUY STOP sitting
        ABOVE the entry: if price RISES against us by `stop_pct` we cover.
        So the formula flips from the long `entry × (1 - stop_pct)`
        (stop below) to `entry × (1 + stop_pct)` (stop above).
        Flowchart reference: 299.70 × (1 + 0.0025) = 300.44.

        This method does the math in **Decimal** (exact arithmetic) and
        snaps in **integer-tick space** (the senior-quant pattern used
        by every HFT/hedge-fund OMS):

            1. price → Decimal (exact, no float drift)
            2. stop_pct → Decimal (exact)
            3. raw = price × (1 + stop_pct) (exact Decimal multiply)
            4. tick = Decimal of venue's minTick (runtime-discovered)
            5. n_ticks = round(raw / tick) using ROUND_HALF_UP — INTEGER
            6. exact_price = n_ticks × tick (exactly on grid, no drift)
            7. return float(exact_price) — float conversion only at
               the boundary, no math after

        Result is GUARANTEED to be on the venue's tick grid with the
        most precise math possible.

        Accepts either float or Decimal/str input; converts via
        str() to avoid binary-float representation errors.
        """
        from decimal import Decimal as _D, ROUND_HALF_UP as _HALF_UP
        # Promote inputs to Decimal exactly. `Decimal(str(x))` avoids
        # the binary-float artifacts that `Decimal(x)` would introduce
        # (e.g. Decimal(0.1) = Decimal('0.10000000000000000555...')).
        if not isinstance(entry_price, _D):
            entry_price = _D(str(entry_price))
        if not isinstance(stop_pct, _D):
            stop_pct = _D(str(stop_pct))
        # SHORT: stop sits ABOVE entry → (1 + stop_pct)
        raw = entry_price * (_D("1") + stop_pct)

        # Tick grid: prefer venue's reported minTick, fall back to spec.
        tick_d: Optional[_D] = None
        rt = self._runtime_min_tick()
        if rt is not None and rt > 0:
            tick_d = _D(str(rt))
        elif self._asset_spec is not None:
            try:
                from src.assets.types import price as _to_price
                tick_d = _D(str(self._asset_spec.tick.tick_size(_to_price(1.0))))
            except Exception:
                tick_d = None
        if tick_d is None or tick_d <= 0:
            # No tick info — fall back to legacy 2dp equity behavior
            # so equity paths stay byte-identical when spec is absent.
            return float(raw.quantize(_D("0.01"), rounding=_HALF_UP))

        # Snap in integer-tick space — the result is EXACTLY on grid.
        n_ticks = (raw / tick_d).quantize(_D("1"), rounding=_HALF_UP)
        exact = n_ticks * tick_d
        return float(exact)

    def _round_to_tick(self, value: float) -> float:
        """Round `value` to the asset's valid tick grid via the AssetSpec.

        Replaces the hardcoded `round(value, 2)` calls scattered through
        engine.py. For US equity (the legacy case) returns mathematically
        identical output to round(value, 2) — 0.01 grid, banker's
        rounding. For other asset classes, snaps to the asset's true
        tick grid (e.g. 0.00005 for EURUSD, 0.25 for ES futures).

        Float-in / float-out so the existing engine math (which is
        float-based throughout) doesn't need to change at the call site
        — only the rounding call itself is replaced.

        If the spec failed to resolve at __init__ (shouldn't happen in
        production), we fall through to the legacy 2dp behavior so
        nothing crashes.

        Perf: ~1µs per call (Decimal conversion dominates). Engine
        invokes this ~10 times per cycle, so total cost is ~10µs/cycle
        — negligible vs the network RPCs in the same cycle (~10ms).
        """
        # Runtime-tick precedence: the venue's reported minTick beats
        # the spec's hardcoded default. Real HFT systems always defer
        # to the venue at runtime — account type, contract listing, and
        # venue routing all affect the actual valid price grid. The
        # spec's hardcoded value is the safe offline-test default only.
        # (E.g. EURUSD spec assumes 0.00005 half-pip, but the venue
        # might allow 0.00001 sub-pip for certain account types — we
        # should round to whatever the venue actually accepts.)
        rt = self._runtime_min_tick()
        if rt is not None and rt > 0:
            # Half-up rounding on a uniform grid: snap to nearest tick.
            from decimal import Decimal as _D, ROUND_HALF_UP
            tick_d = _D(str(rt))
            val_d = _D(str(value))
            n = (val_d / tick_d).quantize(_D("1"), rounding=ROUND_HALF_UP)
            return float(n * tick_d)

        if self._asset_spec is None:
            return round(value, 2)
        # Import here, not at module top, so the assets module is loaded
        # lazily — keeps engine.py importable in degraded environments
        # where src/assets may be partially set up.
        from src.assets.types import price as _to_price
        from src.assets.policies.tick import RoundDirection as _RD
        rounded = self._asset_spec.tick.round_to_tick(
            _to_price(value), _RD.NEAREST,
        )
        return float(rounded)

    def _effective_stop_pct(self) -> float:
        """Return the SL pct that should be used RIGHT NOW for the open
        cycle's protective stop.

        Priority:
          1. `_active_stop_pct` if set — the value frozen in when the
             current position's bracket was placed (or restored from a
             saved state file on restart). Honoured for as long as the
             position stays open so the SL doesn't move under the user
             across a restart with a different --stop-pct CLI arg.
          2. `self.config.stop_loss_pct` — the CLI default. Used for
             fresh cycles (MONITORING / WAITING_REENTRY entries) and as
             a fallback when no active override exists.

        Cleared to None on SELL fill (in `_on_gateway_fill` SELL branch)
        so the NEXT cycle adopts whatever the user is running with now.
        """
        if self._active_stop_pct is not None and self._active_stop_pct > 0:
            return float(self._active_stop_pct)
        return float(self.config.stop_loss_pct)

    def _make_engine_id(self, prefix: str, qty: int) -> str:
        """Build a per-cycle-unique engine_id.

        Format: `{prefix}_{qty}_{ticker}{cid_suffix}_n{cycle_seq}`
            e.g. `ENTRY_BUY_100_TSLA_c4_n7`
                 `BR_SELL_100_TSLA_c4_n7`
                 `SL_SELL_100_TSLA_c4_n7`

        Why this exists: pre-fix, all three engine_ids in a cycle shared
        `{prefix}_{qty}_{ticker}{cid_suffix}` — identical strings across
        cycles. A leftover bracket child from cycle N (e.g. when modify
        failed mid-cycle and the SL fallback didn't cancel the orphan)
        would have its broker fill mapped back via _order_id_map to the
        SAME engine_id that cycle N+1 just registered → fill booked
        against the wrong cycle. In the 2026-05-27 TSLA incident this
        cost us $44,031.30 of phantom PnL when an orphan child SELL from
        cycle B fired during cycle C's WAITING_REENTRY window.

        The `_n{cycle_seq}` suffix is bumped at the head of every entry
        attempt in `_place_entry_stop_limit`, so:
            * Cycle B's child id is `BR_SELL_100_TSLA_c4_n3`
            * Cycle C's child id is `BR_SELL_100_TSLA_c4_n4`
        Registry lookups are now disjoint. A stale fill from cycle B
        either (a) finds its original record still in the registry and
        gets booked correctly (caller's position already closed, the
        phantom-SELL guard catches it), or (b) hits the "Unknown order
        id" guard and is dropped with a loud audit row.
        """
        # Format: `{prefix}_{qty}_{ticker}_c{cid}_n{seq}_s{session}`
        # The trailing _s{session} disambiguates across restarts: when
        # state is lost, _cycle_seq resets to 0 → without session-id,
        # fresh _n1 collides with prior session's _n1. With it, every
        # restart gets a unique namespace for its cycle counter.
        return (
            f"{prefix}_{int(qty)}_{self.config.ticker}"
            f"{self._cid_suffix}_n{self._cycle_seq}_s{self._session_id}"
        )

    def _engine_id_matches_cycle_pattern(self, eid: str, prefix: str) -> bool:
        """Helper for reconcile: tell whether a foreign engine_id matches
        one of OUR per-cycle id patterns. Accepts ALL three formats so
        state files from any era load cleanly:
          * 2026-06+: `{prefix}_{qty}_{sym}{cid}_n{N}_s{session}`
          * 2026-05+: `{prefix}_{qty}_{sym}{cid}_n{N}`
          * legacy:   `{prefix}_{qty}_{sym}{cid}`
        Session-id matching is intentionally NOT strict here — reconcile
        should claim foreign orders that belong to our (symbol, client_id)
        pair regardless of which session created them, otherwise a restart
        would orphan its own prior-session orders.
        """
        if not eid:
            return False
        base = f"{prefix}_"
        sym = f"_{self.config.ticker}{self._cid_suffix}"
        if not eid.startswith(base):
            return False
        return sym in eid

    async def _log_json(self, event: str, **kwargs):
        """Emit structured log if logger available (async for high-freq events)."""
        if self.logger:
            await self.logger.log_async(event, **kwargs)

    def _on_gateway_commission(
        self, engine_id: str, commission: float, exec_id: Optional[str]
    ) -> None:
        """Accept a late-arriving commission report from IBKR.

        IBKR sends `commissionReport` as a SEPARATE wire message after
        each execution, typically 100-1000ms after the fillEvent. The
        Gateway subscribes to that event and forwards it here once per
        execution (already deduped on exec_id at the gateway).

        Action: accumulate the commission into the OrderRecord's
        `broker_commission` field. From this moment on, every call to
        `order.calculate_commission()` returns IBKR's authoritative
        number — the modeled equity-formula fallback is bypassed.

        Critical for accurate round-trip PnL: when the SELL fills and
        we close the cycle, `_pending_buy_commission` will reflect the
        true BUY-side commission rather than the $92-vs-$2 modeled
        nonsense we used to ship to PnL on FX trades.

        Best-effort: every failure path swallowed. Worst case we keep
        the modeled estimate.
        """
        try:
            order = self.registry.get(engine_id) if self.registry else None
            if order is None:
                return
            prev = order.broker_commission or 0.0
            new_total = prev + float(commission)
            order.broker_commission = new_total
            # If this order was a BUY entry and the engine is currently
            # tracking its commission as `_pending_buy_commission` (for
            # round-trip math on the future SELL fill), refresh that
            # too so the SELL closes with the correct number. The
            # registry's `total_commission` is the canonical source.
            from src.config.models import OrderSide as _OS
            if order.side == _OS.BUY and self._position_open:
                # Recompute pending BUY commission as the order's now-true
                # broker_commission (accumulated across this order's fills).
                self._pending_buy_commission = float(new_total)
            if self._audit:
                try:
                    side_str = order.side.value if hasattr(order.side, 'value') else str(order.side)
                    self._audit.log_order(
                        event="COMMISSION_REPORT",
                        order_id=engine_id,
                        side=side_str,
                        qty=order.filled_qty,
                        commission=float(commission),
                        reason=(
                            f"true broker commission for execId={exec_id}; "
                            f"order total now ${new_total:.4f} "
                            f"(was estimate ${prev:.4f})"
                        ),
                        state_at_time=self._state.value,
                        position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
                    )
                except Exception:
                    pass
        except Exception as e:
            self._log(
                f"[COMMISSION] handler failed for {engine_id}: "
                f"{type(e).__name__}: {e}"
            )

    def _on_order_status_change(self, engine_id: str, status: str, message: str) -> None:
        """Handle terminal NON-FILL status (Rejected / Cancelled / Inactive).

        Critical for protective stops: if IBKR rejects the SL (margin,
        invalid stop price, halted symbol, etc.), the engine had been
        assuming the position was protected. Without this handler,
        `_pending_stop` stays set forever and a stop hit goes through.

        Actions:
            1. Mark the order in registry as REJECTED/CANCELLED.
            2. Audit it loudly with the IBKR error text.
            3. If the failed order matches our active `_pending_stop`, CLEAR
               it. This unsticks the helper's idempotency guard so a retry
               (manual or via the reactive _track_position path) can fire.
            4. If we're in IN_POSITION and the rejected order was the
               protective SL, schedule an immediate re-arm — the position
               is naked until a new SL lands.
        """
        # DIAG (2026-06-17, audit-only, never alters control flow): persist
        # the cause of EVERY terminal non-fill so we can see fleet-wide why
        # orders cancel (the re-entry churn under investigation). A non-empty
        # ibkr_msg or status=Rejected ⇒ broker-side reason/error code;
        # status=ApiCancelled with empty msg ⇒ engine/API-initiated cancel.
        try:
            if getattr(self, '_audit', None) and status in (
                    'Cancelled', 'ApiCancelled', 'Rejected', 'Inactive'):
                self._audit.log_order(
                    event="CANCEL_DIAG", order_id=engine_id, side="", qty=0,
                    order_type="",
                    reason=(f"status={status}; ibkr_msg={message or '(none)'}; "
                            f"pending_stop={(getattr(self, '_pending_stop', None) or {}).get('order_id')}; "
                            f"bracket_child={(getattr(self, '_bracket_child', None) or {}).get('order_id')}"),
                    state_at_time=self._state.value,
                    position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
                )
        except Exception:
            pass

        was_protective_stop = False
        pending = getattr(self, '_pending_stop', None)
        if pending and pending.get('order_id') == engine_id:
            # SHORT INVERSION (P11): the protective leg is a BUY cover, so a
            # rejected/cancelled protective order is a BUY (was SELL for long).
            # Without this flip the on-rejection naked re-arm (below) never fires
            # for a short → the live short sits unprotected until a health-check.
            was_protective_stop = (pending.get('side') == OrderSide.BUY)
            self._pending_stop = None

        # Update registry status (idempotent)
        if status == 'Rejected':
            self.registry.on_reject(engine_id)
            self._log(f"!!! ORDER REJECTED: {engine_id} — {message or '(no reason given)'}")
            # Record ENTRY rejection so health-check Probe 2 doesn't
            # blindly re-place every 30s into a known-bad request.
            # The id format is ENTRY_<SIDE>_<qty>_<ticker>_c<cid>_n<seq>.
            # SHORT INVERSION (P11): the short entry is a SELL (ENTRY_SELL_), not
            # BUY — without this the SSR/locate/borrow-reject backoff never
            # engages and the engine re-spams the rejected short entry every 30s.
            if isinstance(engine_id, str) and engine_id.startswith(f"ENTRY_{OrderSide.SELL.value}_"):
                self._last_entry_rejected_at = self._ts()
                self._last_entry_rejected_reason = message or "(no reason given)"
        elif status in ('Cancelled', 'ApiCancelled'):
            self.registry.on_cancel(engine_id)
            self._log(f"Order cancelled: {engine_id} — {message or '(no reason)'}")
            # ── A45: If the cancelled order was a bracket PARENT BUY, the
            # CHILD SELL STP may still be alive at IBKR. IDEALPRO does NOT
            # reliably auto-cancel the child via parentId for spot FX
            # (live regression 2026-06-09 NZDUSD: parent cancelled but
            # child fired 2 min later as orphan → naked short).
            # Explicitly cancel the child by broker_id, and clear our
            # in-memory _bracket_child marker so A39 doesn't keep blocking
            # legitimate next-cycle entries.
            bracket_child = getattr(self, '_bracket_child', None)
            pending_stop_local = getattr(self, '_pending_stop', None)
            is_bracket_parent_cancel = bool(
                bracket_child
                and bracket_child.get('parent_order_id') == engine_id
            )
            # Also handle the case where the engine has already promoted
            # _bracket_child → _pending_stop on parent fill, then later
            # parent gets a stale Cancelled notification: in that case
            # do NOT cancel _pending_stop (it's the live protective SL).
            already_promoted = bool(
                pending_stop_local
                and pending_stop_local.get('from_bracket')
                and pending_stop_local.get('order_id') == bracket_child.get('order_id') if bracket_child else False
            )
            if is_bracket_parent_cancel and not already_promoted:
                child_id = bracket_child.get('order_id', '')
                self._log(
                    f"[A45] bracket parent {engine_id} cancelled — "
                    f"explicitly cancelling child {child_id} so it can't "
                    f"fire as orphan (IDEALPRO doesn't reliably auto-cancel)"
                )
                # Cancel by engine_id via gateway helper
                try:
                    if hasattr(self.gateway, 'cancel_order'):
                        # Fire-and-forget; gateway.cancel_order may be sync
                        # or async — handle both shapes.
                        result = self.gateway.cancel_order(child_id)
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
                except Exception as e:
                    self._log(
                        f"[A45] child cancel attempt raised "
                        f"({type(e).__name__}: {e}) — child {child_id} may "
                        f"still be active at broker. Operator should verify."
                    )
                # Clear marker regardless — the child is either cancelled
                # or our cancel attempt failed; either way the engine's
                # bracket-pending state is no longer accurate.
                self._bracket_child = None
                if self._audit:
                    try:
                        self._audit.log_order(
                            event="BRACKET_CHILD_FORCE_CANCEL",
                            order_id=child_id,
                            side="SELL",
                            qty=bracket_child.get('qty', 0),
                            stop_price=bracket_child.get('stop_price'),
                            reason=(
                                f"parent {engine_id} was cancelled; explicitly "
                                f"cancelling child to prevent orphan firing"
                            ),
                        )
                    except Exception:
                        pass
        elif status == 'Inactive':
            # Inactive = order accepted but conditions not met / parent
            # waiting. Treat like cancelled for the engine's purposes;
            # if the user wants it back they can re-place.
            self.registry.on_cancel(engine_id)
            self._log(f"Order inactive: {engine_id} — {message}")

        if self._audit:
            self._audit.log_order(
                event=status.upper(),
                order_id=engine_id,
                side="",  # not known here without registry lookup
                qty=0,
                reason=message,
                state_at_time=self._state.value,
                position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
            )

        # Alert on rejection — operator needs to know within seconds, not
        # at end-of-session log review. CRITICAL if it was the protective
        # stop on an open position (we'll re-arm below but the operator
        # should still see this). HIGH otherwise.
        if status == 'Rejected' and self._alerts:
            from src.infra.alerts import AlertSeverity
            sev = AlertSeverity.CRITICAL if (was_protective_stop and self._position_open) else AlertSeverity.HIGH
            self._alerts.raise_alert(
                code="ORDER_REJECTED",
                severity=sev,
                message=f"IBKR rejected {engine_id}: {message or '(no reason)'}",
                context={
                    "order_id": engine_id,
                    "status": status,
                    "ibkr_message": message,
                    "was_protective_stop": was_protective_stop,
                    "position_open": self._position_open,
                    "entry_price": self._entry_price,
                    "state": self._state.value,
                },
                correlation_id=self._cycle_id,
            )

        # If the failed order was the protective SL on an open position,
        # the position is now NAKED. Re-arm immediately.
        #
        # A68 (2026-06-10): SUPPRESS the re-arm when the "Cancelled" status
        # is actually IBKR rejecting our `modify_stop_trigger` because the
        # underlying SELL stop has ALREADY TRIGGERED (Error 201 / "Stop
        # price revision is disallowed after order has triggered"). In
        # that case the order is in the process of filling — the position
        # will close itself within milliseconds. Re-arming a fresh
        # SL_SELL_* at this exact moment places a SECOND protective SELL
        # that fires immediately (the first one already triggered) →
        # broker ends up SHORT by the bracket size.
        #
        # Live regression 2026-06-10 AVGO equity stress: parent BUY filled
        # @ 374.58, modify_stop_trigger ran, IBKR returned Error 201, child
        # went Cancelled with filled=0 momentarily, this re-arm fired
        # SL_SELL_100_AVGO_*, original child filled @ 374.16, then SL_SELL
        # ALSO filled in 3 partial chunks (40+50+10) @ 374.17-19 → broker
        # SHORT -100 AVGO + 3 PHANTOM_SELL_REJECTED audit rows.
        #
        # The narrow suppression: ONLY skip when the error matches the
        # known "modify after triggered" signature. Every other reject /
        # cancel cause still re-arms. If somehow our Error 201 detection
        # misfires (false-negative — fails to suppress), behavior is the
        # legacy bug. If it false-positives (suppresses something else),
        # the 30s health-check Probe 1 will still catch any genuinely
        # missing SL and place one — defense in depth.
        is_modify_after_triggered = (
            isinstance(message, str)
            and ("after order has triggered" in message.lower()
                 or "error 201" in message.lower())
        )
        if (
            was_protective_stop
            and self._position_open
            and self._entry_price
            and not is_modify_after_triggered
        ):
            self._log(f"!!! NAKED POSITION — re-arming protective stop after rejection")
            if self._running:
                try:
                    asyncio.create_task(self._place_protective_stop("STOP_LOSS_RETRY"))
                except RuntimeError:
                    # No loop (unit test) — reactive path will catch it on next tick
                    pass
        elif (
            was_protective_stop
            and self._position_open
            and is_modify_after_triggered
        ):
            # A68 suppression — log so post-mortem can confirm.
            self._log(
                f"A68_SUPPRESS_NAKED_REARM  cid={getattr(self.gateway, 'client_id', '?')}  "
                f"engine_id={engine_id}  "
                f"reason='child Cancelled with Error 201 (modify-after-triggered) "
                f"— underlying order is filling, not actually rejected; skipping "
                f"naked-rearm to avoid placing a second SL on a closing position'"
            )

    def _on_gateway_fill(self, order_id: str, qty: int, price: float, exec_id: Optional[str] = None,
                          fill_time: Optional[datetime] = None,
                          broker_commission: Optional[float] = None):
        """
        Called by Gateway when an order fills (both paper and live).

        `exec_id` is IBKR's per-execution unique ID; the registry uses it
        to discard fill replays after a reconnect.

        `fill_time` (optional) is `fill.execution.time` from ib_async — the
        broker's authoritative fill timestamp. When provided (live + replay
        paths), the registry's order.filled_at and the dashboard's history
        record both use it instead of `self._ts()`. For replays after a
        disconnect this is critical: without it the audit row records "when
        we processed the fill" (often seconds or minutes later), not "when
        IBKR actually filled". Defaults to None → use `now` (paper-sim and
        in-session live fills, where "now" matches broker time anyway).

        `broker_commission` (optional, $) is IBKR's
        `fill.commissionReport.commission` for THIS execution. The registry
        accumulates it on the order so `order.calculate_commission()`
        returns the penny-exact total IBKR charged instead of the modeled
        formula. None for paper mode + the brief window before
        commissionReport lands → formula fallback in `calculate_commission`.
        """
        # A52 lifecycle log: EVERY fill entering the engine, before any
        # state mutation. Tag with current engine state so we can see
        # phantom paths (SELL arriving while _position_open=False).
        try:
            _exec_short = (exec_id or '')[:14] if exec_id else 'none'
            print(
                f"[BRACKET_LIFECYCLE] ENGINE_FILL_IN  sym={self.config.ticker}  "
                f"order_id={order_id}  qty={qty}  price={price}  "
                f"execId={_exec_short}  fill_time={fill_time}  "
                f"position_open={self._position_open}  "
                f"_pending_stop={self._pending_stop}  "
                f"_bracket_child={getattr(self, '_bracket_child', None)}"
            )
        except Exception:
            pass

        # Strip tzinfo so registry/history datetimes are all naive (avoids
        # mixed-tz TypeError on subtraction in dashboard latency calc).
        # We deliberately do NOT convert UTC→local here — the user asked
        # for the broker's native timestamp clock values to be preserved.
        fill_time_naive: Optional[datetime] = None
        if fill_time is not None:
            fill_time_naive = (
                fill_time.replace(tzinfo=None)
                if getattr(fill_time, 'tzinfo', None) is not None
                else fill_time
            )

        # Record fill in registry (idempotent on exec_id when provided).
        # Pass fill_time so registry.order.filled_at matches the broker truth.
        # Pass broker_commission so the order's running commission tracks
        # IBKR's exact dollar amount across partial fills (None in paper mode
        # → registry leaves order.broker_commission None → calculate_commission
        # falls back to the modeled formula).
        self.registry.on_fill(
            order_id, qty, price, exec_id,
            fill_time=fill_time_naive,
            broker_commission=broker_commission,
        )

        # Invalidate the portfolio-level cache so the NEXT risk check
        # (which may fire within milliseconds — e.g. an immediate re-entry
        # attempt after a SELL closes) sees fresh state instead of the
        # stale "open notional includes the just-closed position" view.
        # Production failure mode 2026-05-26 (NFLX): re-entry after SELL
        # fill got rejected with "Combined exposure $20302" because the
        # PortfolioReader's TTL hadn't expired yet.
        if self.risk and getattr(self.risk, 'portfolio', None) is not None:
            try:
                self.risk.portfolio.invalidate()
            except Exception:
                # Best-effort — never block fill processing on cache mgmt.
                pass

        # Add to order history for dashboard (keeps all orders, even with same ID)
        order = self.registry.get(order_id)
        if order:
            # Make a copy for history (since registry reuses same order_id on re-entry).
            # IMPORTANT: copy signal_price too. Without it the dashboard's
            # slippage tracker (which keys off `signal_price and avg_fill_price`)
            # silently skips every fill, and the SLIPPAGE panel stays "no fills
            # yet" forever even when there were dozens of fills. Slippage is
            # (fill - signal) for BUY and (signal - fill) for SELL — we need
            # signal_price on every history record.
            # Also carry stop/limit prices for full audit context.
            # filled_at uses the broker timestamp when available so the
            # SLIPPAGE / order-latency panels reflect actual execution time,
            # not replay-processing time.
            history_order = OrderRecord(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                order_type=order.order_type,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                status=OrderStatus.FILLED,
                submitted_at=order.submitted_at,
                filled_at=fill_time_naive if fill_time_naive is not None else self._ts(),
                avg_fill_price=price,
                filled_qty=qty,
                commission=order.calculate_commission(),
                signal_price=order.signal_price,
            )
            self._order_history.append(history_order)

        # Branch on the order's actual side from the registry, NOT _pending_side.
        # _pending_side is a single mutable string that gets overwritten the
        # moment any new order is placed — so if a BUY is in flight when _exit
        # places a SELL stop-limit, _pending_side flips to "SELL" and the
        # arriving BUY fill gets booked as a SELL exit (wrong P&L, wrong audit
        # row, phantom trade). The OrderRecord remembers the order's true side
        # regardless of timing.
        if order is None:
            # Unknown order id (shouldn't happen with id-map fix, but bail safely)
            self._pending_side = None
            return

        side_value = order.side.value if hasattr(order.side, 'value') else str(order.side)
        # Clear pending so legacy callers don't re-fire on the next tick.
        self._pending_side = None

        # FL2 — durable, exactly-once record of THIS execution (partial-fill
        # grained; the ledger sums by execId → net). This is the FX position
        # truth + the gap-free backbone under A43. Additive and guarded: a
        # ledger fault never disturbs fill processing. exec_id is None in
        # paper mode → record() no-ops (paper has no FX cash-ledger quirk).
        _ledger = getattr(self, '_fill_ledger', None)
        if _ledger is not None and exec_id:
            try:
                _ledger.record(
                    exec_id=exec_id,
                    symbol=self.config.ticker,
                    side=side_value,
                    shares=qty,
                    price=price,
                    time=str(fill_time_naive) if fill_time_naive is not None else None,
                    order_id=order_id,
                    source='live',
                )
            except Exception:
                pass

        # SHORT INVERSION (P3): the SELL fill OPENS the short (was BUY for long).
        if side_value == OrderSide.SELL.value:
            # Read CUMULATIVE values from the registry, not the partial-fill
            # args. IBKR can split a 100-share BUY into 40 + 60 (or any
            # combination) — `qty`/`price` here are just THIS partial. The
            # registry's `OrderRegistry.on_fill` already maintains the
            # weighted-average fill price and cumulative filled quantity.
            # Using these matches your senior's exact instruction:
            #   "take the average of prices if the full quantity was
            #    filled, like 40 * p1 + 60 * p2 / total qty"
            # That's what `order.avg_fill_price` is — IBKR-standard VWAP
            # across this order's executions.
            cum_qty = order.filled_qty
            avg_price = order.avg_fill_price or price
            is_first_partial = (self._quantity == 0 or not self._position_open)
            is_complete = cum_qty >= order.qty

            # Per doc section 8: Calculate fixed stop loss from the WEIGHTED
            # AVG fill price (which equals the single fill price for orders
            # that fill in one go). The very first partial of a NEW
            # cycle's BUY freezes the SL pct: `_active_stop_pct = current
            # config.stop_loss_pct`. From this point on, every modify in
            # the cycle (subsequent partials, bracket-child retarget on
            # complete, health-check re-arm, restart recovery) reads
            # `_effective_stop_pct()` which returns the frozen value —
            # NOT a CLI flag the user may have changed since.
            self._entry_price = avg_price
            # SHORT INVERSION (P4): seed the trough; only LOWER it on
            # subsequent partials (the low tracker only moves DOWN via
            # _track_high/_track_low anyway).
            if self._highest_price is None or avg_price < self._highest_price:
                self._highest_price = avg_price
            if self._active_stop_pct is None:
                # First fill of this cycle — freeze the pct.
                #
                # RESTART-SAFETY (SHORT): if a bracket child is already
                # resting at the broker (adopted via reconcile when disk
                # state was lost), its BUY-STOP (cover) encodes the pct the
                # operator ACTUALLY placed the cycle with — which may differ
                # from the CLI --stop-pct of whatever session we restarted
                # into. Back that pct out of the resting child rather than
                # defaulting to config.stop_loss_pct, otherwise a restart
                # into the default (1%) silently widens a tighter stop.
                # SHORT INVERSION: the child stop sits ABOVE entry
                # (entry × (1 + pct)), so pct = child_stop/fill − 1 (mirror
                # of the long side's 1 − child_stop/fill).
                child = getattr(self, '_bracket_child', None)
                child_stop = child.get('stop_price') if child else None
                if child_stop and avg_price and avg_price > 0:
                    self._active_stop_pct = max(
                        0.0, round((float(child_stop) / float(avg_price)) - 1.0, 6),
                    )
                    self._log(
                        f"SL pct recovered from resting bracket child (SHORT): "
                        f"stop ${float(child_stop):.5f} / fill ${avg_price:.5f} "
                        f"-> {self._active_stop_pct * 100:.4f}% (NOT the CLI default "
                        f"{self.config.stop_loss_pct * 100:.4f}%)"
                    )
                else:
                    self._active_stop_pct = float(self.config.stop_loss_pct)
            # SHORT INVERSION (P4): snapshot IBKR's reported daily-LOW at cycle
            # entry. _track_high (now low-tracking) uses this baseline to
            # detect when feed.low FALLS during the cycle (which can only
            # happen from in-cycle trades) and bump _highest_price (trough)
            # even if our TRADE-tick feed didn't see the lower print. Set
            # ONCE on the first partial.
            if is_first_partial:
                try:
                    self._feed_high_at_entry = float(
                        self._feed.low if self._feed and self._feed.low else 0.0
                    )
                except Exception:
                    self._feed_high_at_entry = 0.0
            self._stop_loss = self._protective_stop_price(avg_price, self._effective_stop_pct())
            # Coerce to int — IBKR's `fill.execution.shares` comes back as
            # a float ("10.0" for a 10-share execution), and any downstream
            # f-string-built order_id (`SL_SELL_10.0_NVDA`) or audit row
            # (`qty=10.0`) leaks that float across the system. The position
            # size is always whole shares; force int once at the source.
            self._quantity = int(cum_qty)
            self._position_open = True
            self._state = TradeState.IN_POSITION

            # Commission accumulates per fill (IBKR tiered: $0.003/share,
            # $0.35 min). The pending_buy_commission is what the SELL fill
            # will deduct from gross P&L for accurate round-trip cost.
            this_partial_commission = 0.0
            if order is not None:
                this_partial_commission = order.calculate_commission() - self._pending_buy_commission
                # `calculate_commission` returns the cumulative commission
                # for this order's full filled_qty. Subtract what we've
                # already stashed to get the incremental cost of THIS partial.
                if this_partial_commission < 0:
                    this_partial_commission = 0.0
                self._pending_buy_commission = order.calculate_commission()
            self._total_commission += this_partial_commission

            # Update risk counters BEFORE the next risk.check() can run.
            # Only count the trade once (on first partial); subsequent
            # partials are the same trade.
            if self.risk and is_first_partial:
                self.risk.record_fill(avg_price, cum_qty, "SELL")

            self._save_state()  # Persist position
            self._log(
                f"FILLED SELL (SHORT ENTRY) {qty} @ ${price:.2f} "
                f"(cum: {cum_qty}/{order.qty} @ avg ${avg_price:.4f})"
                f"{' — COMPLETE' if is_complete else ''}, "
                f"stop={self._effective_stop_pct()*100:.4f}% = ${self._stop_loss:.2f}"
            )

            # Log order FILLED to audit (non-blocking)
            if self._audit:
                self._audit.log_order(
                    event="FILLED",
                    order_id=order_id,
                    side="SELL",
                    qty=qty,
                    order_type="LIMIT",
                    limit_price=price,  # fill was at limit price
                    signal_price=price,
                    fill_price=price,
                    state_at_time=self._state.value,
                    position_at_time="FLAT",
                )

            # Slack ping for the BUY fill — non-blocking, rate-limited.
            # Signal price ≈ fill price here (we already entered through a
            # stop-limit so the engine doesn't carry a separate "intended"
            # entry price), but we still send `signal_price` so the format
            # is consistent with SELL fills which have a real slip number.
            if getattr(self, '_alerts', None) is not None:
                try:
                    # SHORT ENTRY (SELL): slippage positive = bad = sold LOWER
                    # than signal → signal - fill.
                    self._alerts.notify_trade(
                        "FILLED",
                        symbol=self.config.ticker,
                        side="SELL",
                        qty=qty,
                        fill_price=price,
                        signal_price=getattr(order, "signal_price", None) if order else None,
                        slippage=((order.signal_price or price) - price) if order and order.signal_price else 0.0,
                        state=self._state.value,
                        cycle_id=getattr(self, '_cycle_id', ''),
                    )
                except Exception as e:
                    # Notification never breaks fill processing.
                    print(f"[notify_trade FILLED SELL/SHORT-ENTRY] {e}")

            # Protective-stop placement — senior-quant pattern:
            #
            #   "Let them fill, then compute weighted avg, then place ONE
            #    SELL for the total quantity at the stop derived from that
            #    weighted avg."
            #
            # So we do NOT resize / re-arm SL on each partial. Instead:
            #   * On every partial, update engine state (entry, qty, stop)
            #     using the cumulative weighted avg — but DON'T place SL yet.
            #   * When the BUY fully completes (cum_qty >= ordered qty), THEN
            #     place ONE SELL stop-limit sized to the total at the stop
            #     calculated from the final weighted-avg fill price.
            #   * If the BUY gets stuck partial (e.g. 95/100 filled and the
            #     last 5 won't go), the chase task bumps the BUY limit upward
            #     by `partial_fill_chase_offset` and waits another timeout.
            #     After `partial_fill_max_chases` give up: cancel the BUY
            #     remainder, then place the SL on the partial position.
            #
            # Brief naked window between first partial and final partial is
            # accepted as the cost of having one cleanly-sized SL instead
            # of N progressive resizes. The reactive _track_position
            # fallback covers crash-during-window via MARKET exit.
            if getattr(self, '_running', False):
                try:
                    import asyncio
                    if is_complete:
                        # Clear the entry order's `_pending_stop` marker
                        # — see legacy comment below for the 25-second
                        # SL-placement-delay bug this prevents.
                        ps = getattr(self, '_pending_stop', None)
                        if ps and ps.get('order_id') == order_id:
                            self._pending_stop = None

                        # ── Bracket-aware SL handling ────────────────
                        # If this BUY was placed as a bracket (the new
                        # default for live mode), the child SELL STP is
                        # already at the broker. We don't need to
                        # PLACE an SL — we need to MODIFY the child's
                        # trigger from the initial estimate (based on
                        # the BUY trigger) to the real fill VWAP. The
                        # child is then promoted to `_pending_stop` so
                        # all the existing "do I have a stop?" checks
                        # see it via the same field they used before
                        # the bracket migration.
                        child = getattr(self, '_bracket_child', None)
                        if child is not None:
                            child_id = child['order_id']
                            # Exact integer-tick math: avoids float drift
                            # in `avg_price * (1 - pct)`. Result is
                            # GUARANTEED on the venue's tick grid. See
                            # `_protective_stop_price` docstring for why.
                            new_stop = self._protective_stop_price(
                                avg_price, self._effective_stop_pct()
                            )
                            # Also resize child to the actual cumulative
                            # filled qty — handles the case where the
                            # parent never reaches the original qty
                            # (chase gave up part of the way through).
                            actual_qty = int(cum_qty)
                            # Log format precision matches asset class so
                            # the operator sees the actual modify target
                            # (5dp on FX, 2dp on equity — historical).
                            try:
                                _dec = int(self._asset_spec.tick.decimals_for_display(
                                    __import__('src.assets.types', fromlist=['price']).price(1.0)
                                )) if self._asset_spec else 2
                            except Exception:
                                _dec = 2
                            _old_stop = child.get('stop_price') or 0.0
                            self._log(
                                f"BRACKET CHILD PROMOTE (BUY cover): modifying {child_id} "
                                f"trigger ${_old_stop:.{_dec}f} → "
                                f"${new_stop:.{_dec}f} (fill VWAP ${avg_price:.{_dec}f} × "
                                f"(1+{self._effective_stop_pct()*100:.2f}%)), "
                                f"qty {child.get('qty')} → {actual_qty}"
                            )
                            asyncio.create_task(self._modify_bracket_child(
                                child_id=child_id,
                                new_stop=new_stop,
                                new_qty=actual_qty,
                                old_stop=child.get('stop_price'),
                            ))
                            # Promote child → _pending_stop. From here
                            # on, all the engine's existing "is there a
                            # stop?" checks see the child via the same
                            # field (`_pending_stop`) they used before
                            # the bracket migration. _bracket_child is
                            # cleared since the bracket-as-a-pair phase
                            # is over.
                            self._pending_stop = {
                                'order_id': child_id,
                                'qty': actual_qty,
                                'stop_price': new_stop,
                                'side': OrderSide.BUY,  # SHORT: protective cover
                                'order_type': 'STP',
                                'from_bracket': True,
                            }
                            self._bracket_child = None
                            self._cancel_partial_fill_chase(order_id)
                        else:
                            # No bracket (paper mode, legacy state on
                            # restart, or _bracket_child cleared by
                            # some path). Fall back to the old "place
                            # a fresh SL" flow.
                            asyncio.create_task(self._place_protective_stop("STOP_LOSS"))
                            self._cancel_partial_fill_chase(order_id)
                    else:
                        # ── Partial parent fill — defensive child resize ──
                        # The bracket child is ALREADY activated at the
                        # broker the instant the parent gets its first
                        # fill (IBKR's bracket linkage triggers child
                        # activation on parent's first partial, not on
                        # parent complete). The child's qty is still the
                        # ORIGINAL bracket qty (e.g. 1000) — NOT the
                        # cum_qty we actually own (e.g. 600). Concrete
                        # risk (SHORT): if price RISES to the child stop
                        # right now, broker fires BUY 1000 against our
                        # -600 → goes LONG 400. IBKR does NOT auto-resize
                        # the child to match the parent's running fill.
                        #
                        # Defense: on EVERY partial, modify the child to:
                        #   - new qty       = cum_qty (matches what we own)
                        #   - new stop_px   = running VWAP × (1 + stop_pct)
                        #                     (avg_price is already the
                        #                      cum-weighted VWAP via the
                        #                      registry — same formula
                        #                      used for the post-complete
                        #                      modify, just running)
                        # Idempotency: only fire the modify if values
                        # actually changed by a meaningful amount (≥0.5¢
                        # or any qty change) — avoids spamming IBKR with
                        # no-op modifies on rapid micro-partials.
                        child = getattr(self, '_bracket_child', None)
                        if child is not None:
                            # Exact integer-tick math (see _protective_stop_price).
                            running_stop = self._protective_stop_price(
                                avg_price, self._effective_stop_pct()
                            )
                            actual_qty = int(cum_qty)
                            old_stop = float(child.get('stop_price') or 0.0)
                            old_qty = int(child.get('qty') or 0)
                            # Per-asset "is the change meaningful?" threshold.
                            # Equity 0.5¢, FX half-pip, futures half-tick.
                            stop_changed = abs(running_stop - old_stop) >= self._price_epsilon()
                            qty_changed = actual_qty != old_qty
                            if stop_changed or qty_changed:
                                try:
                                    _dec = int(self._asset_spec.tick.decimals_for_display(
                                        __import__('src.assets.types', fromlist=['price']).price(1.0)
                                    )) if self._asset_spec else 2
                                except Exception:
                                    _dec = 2
                                self._log(
                                    f"BRACKET CHILD MID-PARTIAL: modify "
                                    f"{child['order_id']} qty {old_qty}→{actual_qty}, "
                                    f"stop ${old_stop:.{_dec}f}→${running_stop:.{_dec}f} "
                                    f"(running VWAP ${avg_price:.{_dec}f}; "
                                    f"cum {cum_qty}/{order.qty})"
                                )
                                asyncio.create_task(self._modify_bracket_child(
                                    child_id=child['order_id'],
                                    new_stop=running_stop,
                                    new_qty=actual_qty,
                                    old_stop=old_stop,
                                ))
                                # Update in-memory snapshot so the next
                                # partial doesn't re-fire a redundant modify.
                                child['stop_price'] = running_stop
                                child['qty'] = actual_qty
                        # Schedule chase as before in case parent stalls.
                        self._schedule_partial_fill_chase(order_id)
                except RuntimeError:
                    # No running loop (e.g. unit test) — reactive path in
                    # _track_position will still cover us.
                    pass

        # SHORT INVERSION (P3): the BUY fill COVERS/closes the short (was SELL).
        elif side_value == OrderSide.BUY.value:
            # ── Phantom-cover (BUY) guard ──────────────────────────────
            # Defense-in-depth against orphan-bracket-child misattribution
            # (the 2026-05-27 TSLA $44k phantom-PnL incident root cause).
            #
            # Scenario: a leftover bracket child from a previous cycle
            # fires at the broker. Even with the per-cycle engine_id
            # disambiguator (`_n{cycle_seq}`) and the orphan-cancel-on-
            # modify-failure path both in place, a race or a path we
            # haven't anticipated could still resolve the broker fill
            # back to a registry entry while the engine is FLAT.
            #
            # If we book that fill via the normal SELL branch:
            #   gross_pnl = (price - self._entry_price) * qty
            # with `_entry_price = None` → 0.0 → gross_pnl = price * qty.
            # That's how a $440 share at qty=100 yielded the $44k phantom.
            #
            # Refuse to book the fill, fire a CRITICAL alert, and write a
            # forensic PHANTOM_SELL_REJECTED audit row so post-trade
            # analysis can grep for it. We DON'T raise — the registry
            # already recorded the exec_id (via on_fill above), so
            # dropping out of this branch leaves the registry consistent
            # without polluting P&L / state. The operator must
            # investigate (likely manually reconcile the actual broker
            # position vs the engine's _quantity).
            if not self._position_open or not self._entry_price:
                # SHORT: the broker just told us a BUY (cover) filled, but the
                # engine never recorded the matching SELL short entry. Tag it
                # so the post-mortem grep correlates with the upstream
                # BACKFILL/SCAN entries from this same reconnect cycle.
                print(
                    f"[BRACKET_LIFECYCLE] PHANTOM_COVER  sym={self.config.ticker}  "
                    f"order_id={order_id}  qty={qty}  price={price}  "
                    f"_state={self._state.value}  "
                    f"_position_open={self._position_open}  "
                    f"_entry_price={self._entry_price}  "
                    f"_bracket_child={getattr(self, '_bracket_child', None)}  "
                    f"_pending_stop={self._pending_stop}  "
                    f"cycle_id={getattr(self, '_cycle_id', '')}"
                )
                self._log(
                    f"[PHANTOM-COVER] REJECTED: BUY cover fill {order_id} "
                    f"{qty} @ ${price:.2f} arrived while engine FLAT "
                    f"(_position_open={self._position_open}, "
                    f"_entry_price={self._entry_price}, "
                    f"_state={self._state.value}). "
                    f"Leftover bracket child suspected — NOT booking PnL."
                )
                if self._audit:
                    try:
                        self._audit.log_order(
                            event="PHANTOM_SELL_REJECTED",
                            order_id=order_id,
                            side="BUY",
                            qty=qty,
                            order_type=(
                                order.order_type.value
                                if order and hasattr(order.order_type, 'value')
                                else (str(order.order_type) if order else "UNKNOWN")
                            ),
                            fill_price=price,
                            signal_price=0.0,
                            reason=(
                                f"BUY cover fill arrived while engine FLAT — "
                                f"likely orphan bracket child from prior cycle. "
                                f"state={self._state.value}, "
                                f"position_open={self._position_open}, "
                                f"entry_price={self._entry_price}"
                            ),
                            state_at_time=self._state.value,
                            position_at_time="FLAT",
                        )
                    except Exception as e:
                        self._log(f"[PHANTOM-SELL] audit log failed: {e}")
                if self._alerts:
                    try:
                        from src.infra.alerts import AlertSeverity
                        self._alerts.raise_alert(
                            code="PHANTOM_SELL_REJECTED",
                            severity=AlertSeverity.CRITICAL,
                            message=(
                                f"PHANTOM BUY-cover fill rejected on "
                                f"{self.config.ticker}: {qty}@${price:.2f} via "
                                f"{order_id} while engine was FLAT. Suspected "
                                f"orphan bracket child from a previous cycle. "
                                f"Engine state preserved; manual reconcile "
                                f"recommended (verify actual broker position)."
                            ),
                            context={
                                "ticker": self.config.ticker,
                                "order_id": order_id,
                                "qty": int(qty),
                                "price": float(price),
                                "engine_state": self._state.value,
                                "position_open": self._position_open,
                                "entry_price": self._entry_price,
                            },
                            correlation_id=getattr(self, '_cycle_id', ''),
                        )
                    except Exception as e:
                        self._log(f"[PHANTOM-SELL] alert raise failed: {e}")
                return

            # === Partial-fill guard ====================================
            # IBKR can split a 200-share SELL into multiple executions
            # (e.g. 100 + 100, or 132 + 68 if liquidity is thin). Each
            # partial fires `_on_gateway_fill` once with THIS partial's
            # qty, but the close-cycle processing below ONLY makes sense
            # on the order as a WHOLE — gross P&L, commission, win/loss
            # bookkeeping, state transition to WAITING_REENTRY, breakout-
            # level snapshot, Slack ping, etc. Running it per-partial
            # caused commission to be double-counted (the registry's
            # cumulative `order.calculate_commission()` was re-added on
            # every partial) AND set `_position_open=False` after only
            # the first partial, hiding the remaining LONG exposure if
            # the order ended up cancelled / rejected mid-fill.
            #
            # Fix: on partial fills, decrement `_quantity` so the
            # engine + dashboard reflect the half-closed position,
            # persist state for crash recovery, audit the partial, then
            # bail. When the FINAL partial arrives (cum_qty >= order.qty)
            # we fall through to the close-cycle path below with qty
            # rewritten to the cumulative total and price to the order's
            # VWAP — same logic as the BUY branch's partial handling.
            cum_qty = order.filled_qty            # cumulative across partials
            is_complete = cum_qty >= order.qty
            if not is_complete:
                remaining_at_broker = order.qty - cum_qty
                # Engine view of position: started at config qty, now
                # less by this partial. Never below 0 even if accounting
                # drifts (defensive: floor at 0).
                self._quantity = max(0, int(self._quantity) - int(qty))
                self._log(
                    f"BUY COVER PARTIAL FILL: {qty} @ ${price:.2f} "
                    f"(cumulative {cum_qty}/{order.qty}, "
                    f"engine qty now {self._quantity}, "
                    f"broker still working {remaining_at_broker})"
                )
                if self._audit:
                    self._audit.log_order(
                        event="PARTIAL_FILL",
                        order_id=order_id, side="BUY", qty=qty,
                        order_type=order.order_type.value if hasattr(order.order_type, 'value') else str(order.order_type),
                        fill_price=price,
                        signal_price=self._entry_price or 0.0,
                        state_at_time=self._state.value,
                        position_at_time="SHORT",
                    )
                # Persist the decremented qty so crash recovery sees the
                # half-closed position rather than the original 200.
                try:
                    self._save_state()
                except Exception as e:
                    self._log(f"[SELL PARTIAL] state save failed (non-fatal): {e}")
                return

            # === Full fill — process the cycle close ====================
            # Override `qty` and `price` with the registry's cumulative
            # values so single-fill and multi-partial orders take the
            # identical path below. avg_fill_price is IBKR-standard VWAP
            # (Σ exec.qty × exec.price / Σ exec.qty) — same as the BUY
            # branch's senior-approved averaging rule.
            qty = cum_qty
            price = order.avg_fill_price or price

            exit_reason = self._pending_exit_reason or "STOP_LOSS"
            self._pending_exit_reason = None

            # SHORT INVERSION (P3): if the protective BUY cover fired while the
            # entry SELL still had unfilled qty resting at the broker, cancel
            # that remainder. Otherwise we'd: cover N shares, then later the
            # remaining SELL would fill (if price came back down to its limit),
            # leaving us SHORT the remainder with no protective stop.
            # Senior-quant invariant: never be short without an SL.
            try:
                # Current cycle's entry id — use the cycle_seq-aware helper
                # so we look up THIS cycle's order, not a stale legacy one
                # that happens to share the qty/symbol/cid.
                entry_id = self._make_engine_id(f"ENTRY_{OrderSide.SELL.value}", self.config.quantity)
                pending_sell = self.registry.get(entry_id) if self.registry else None
                if pending_sell and pending_sell.filled_qty < pending_sell.qty and pending_sell.status not in (
                    OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED
                ):
                    self._log(
                        f"[SAFETY] BUY cover fired while SELL entry {entry_id} still had "
                        f"{pending_sell.qty - pending_sell.filled_qty} unfilled shares — "
                        f"cancelling SELL remainder"
                    )
                    if getattr(self, '_running', False):
                        try:
                            import asyncio
                            asyncio.create_task(self.gateway.cancel_order(entry_id))
                        except RuntimeError:
                            pass
                    self._cancel_partial_fill_chase(entry_id)
            except Exception as e:
                # Best-effort — don't break the cover fill processing.
                self._log(f"[SAFETY] SELL-remainder cancel check failed: {e}")

            # Calculate P&L from actual fill price (not signal price).
            # SHORT INVERSION (P8): a short profits when the cover price is
            # BELOW the entry, so gross = (entry - cover) × qty (long was
            # (cover - entry) × qty).
            entry = self._entry_price or 0.0
            gross_pnl = (entry - price) * qty
            # Normalize quote-ccy P&L → USD for non-USD-quoted FX (USDJPY→JPY,
            # crosses) so the DISPLAYED/recorded P&L is in USD. Equity and
            # USD-quoted FX pass through unchanged; commission below is already
            # USD. Best-effort — returns the raw value on any error.
            try:
                from src.strategy.risk import fx_quote_pnl_to_usd as _pnl_usd
                gross_pnl = _pnl_usd(self.config.ticker, gross_pnl, price, qty)
            except Exception:
                pass

            # Real round-trip commission: SELL-side from THIS order's
            # filled_qty × $0.003 (with $0.35 floor) PLUS the BUY-side
            # commission stashed when the entry filled. Pulls from
            # OrderRecord.calculate_commission which respects MIN_COMMISSION.
            sell_commission = order.calculate_commission()
            commission = sell_commission + self._pending_buy_commission
            self._pending_buy_commission = 0.0  # Round-trip closed
            pnl = gross_pnl - commission
            self._pnl += pnl
            self._total_commission += sell_commission
            self._trades_today += 1

            if self.risk:
                # record_fill updates daily PnL cache + trade counter; the
                # win/loss counter (used for max_consecutive_losses gate) is
                # tracked separately.
                self.risk.record_fill(price, qty, "BUY")
                if pnl < 0:
                    self.risk.record_loss()
                else:
                    self.risk.record_win()

            if pnl > 0:
                self._wins += 1
            else:
                self._losses += 1

            # SHORT INVERSION (P5): store the cycle TROUGH as the re-entry
            # breakdown level (long stored the peak). _highest_price now
            # holds the lowest price seen this cycle.
            breakout_level = self._highest_price
            self._previous_breakout_level = breakout_level

            self._log(
                f"FILLED BUY (COVER) {qty} @ ${price:.2f} [{exit_reason}], "
                f"P&L: ${pnl:+.2f} (gross: ${gross_pnl:+.2f}, comm: ${commission:.2f}), "
                f"breakdown=${breakout_level or 0:.2f}, total_pnl=${self._pnl:+.2f}"
            )

            # Log order FILLED to audit (non-blocking)
            if self._audit:
                # Get order type from registry if available
                order_rec = self.registry.get(order_id) if self.registry else None
                order_type = order_rec.order_type.value if order_rec and hasattr(order_rec, 'order_type') else "STOP_LIMIT"
                stop_price = getattr(order_rec, 'stop_price', 0) if order_rec else 0
                limit_price = getattr(order_rec, 'limit_price', 0) if order_rec else 0

                self._audit.log_order(
                    event="FILLED",
                    order_id=order_id,
                    side="BUY",
                    qty=qty,
                    order_type=order_type,
                    stop_price=stop_price,
                    limit_price=limit_price,
                    signal_price=entry,
                    fill_price=price,
                    commission=commission,
                    pnl=pnl,
                    reason=exit_reason,
                    state_at_time=self._state.value,
                    position_at_time="SHORT",
                )

            self.audit("EXIT_FILLED", {
                "fill_price": price,
                "pnl": pnl,
                "gross_pnl": gross_pnl,
                "commission": commission,
                "reason": exit_reason,
                "order_id": order_id,
                "reentry_level": breakout_level,  # SHORT INVERSION (P11): trough, not a breakout
            })

            # Slack ping — SELL fill carries the round-trip P&L. Slippage
            # here is signed by side: positive = bad for us (sold lower
            # than the trigger/signal). entry is used as the "signal" price
            # because that's what the stop was calibrated against.
            if getattr(self, '_alerts', None) is not None:
                try:
                    # SHORT COVER (BUY): slippage positive = bad = paid HIGHER
                    # than entry to cover → fill - entry.
                    slip = price - entry
                    self._alerts.notify_trade(
                        "FILLED",
                        symbol=self.config.ticker,
                        side="BUY",
                        qty=qty,
                        fill_price=price,
                        signal_price=entry,
                        slippage=slip,
                        pnl=pnl,
                        reason=exit_reason,
                        state=self._state.value,
                        cycle_id=getattr(self, '_cycle_id', ''),
                    )
                except Exception as e:
                    print(f"[notify_trade FILLED BUY/COVER] {e}")

            # Reset position state. Also reset `_quantity` to 0 so the
            # engine's view is unambiguous: position_open=False ↔ qty=0.
            # The previous cycle's actual filled qty (which may have been
            # less than config.quantity if a partial fill was abandoned)
            # is recorded in pnl / audit / order_history. The next entry
            # cycle reads `config.quantity` for its target size, NOT
            # `self._quantity`, so the partial outcome doesn't poison the
            # next cycle.
            self._position_open = False
            self._entry_price = None
            self._highest_price = None
            self._stop_loss = None
            self._quantity = 0
            self._pending_stop = None  # Clear pending stop-limit
            # Cycle is closed — unfreeze the per-cycle SL pct so the NEXT
            # cycle picks up whatever --stop-pct the user is now running
            # with. _active_stop_pct is only meant to preserve the SL
            # config of an OPEN cycle across restart, not lock the bot
            # into the same pct forever.
            self._active_stop_pct = None
            # Reset feed.high baseline so the next cycle re-snapshots at
            # its own entry moment (rather than carrying forward the
            # previous cycle's baseline).
            self._feed_high_at_entry = 0.0
            self._state = TradeState.WAITING_REENTRY
            self._save_state()  # Persist state after exit

            # SHORT INVERSION (P5): place re-entry SELL STOP-LIMIT immediately
            # at the previous breakdown level (prior cycle's trough) so the
            # order rests at the broker and only fills when LTP FALLS back to
            # the breakdown. _on_gateway_fill is sync, so schedule.
            if breakout_level and hasattr(self, '_running') and self._running:
                import asyncio
                asyncio.create_task(self._place_entry_stop_limit(breakout_level))

            self._save_state()

    async def start(self):
        """Start engine - begin monitoring with LIMIT order at trigger."""
        self._running = True
        # Capture engine start time as the absolute floor for fill replay.
        # _reconcile_missed_fills uses last_saved_ts when state has been
        # restored, but on a fresh start (or after --reset) last_saved_ts
        # is None and without this floor we'd replay every broker fill
        # from today — producing phantom orders in the dashboard.
        self._engine_started_at = self._ts()

        # Synchronously initialize _paused from current session state BEFORE
        # any awaits below. The session controller task created a few lines
        # down also sets this on its first iteration, but its first iteration
        # may not run until AFTER start() finishes _place_entry_stop_limit
        # (e.g. live mode where reconcile has no internal awaits). Setting
        # it here closes that race so the entry gate inside
        # _place_entry_stop_limit sees a consistent view.
        if self._rth_only and not self._session_is_open():
            self._paused = True

        # Spin up the bounded tick queue + single consumer task. on_tick
        # pushes into the queue (sync); _tick_consumer drains it.
        if self._tick_queue is None:
            self._tick_queue = asyncio.Queue(maxsize=500)
            self._tick_consumer_task = asyncio.create_task(self._tick_consumer())

        # Session controller: auto-PAUSE outside ETH (04:00-20:00 ET
        # Mon-Fri) when `_rth_only=True`. Resting GTC orders at IBKR
        # stay in place — only the engine pauses (no new entries placed
        # outside session). On session open the controller resumes
        # cleanly and the strategy loop picks up from the next tick.
        if self._rth_only and self._session_controller_task is None:
            self._session_controller_task = asyncio.create_task(
                self._session_controller_loop()
            )

        # Health-check loop: 30s cadence invariant probes. Wired only
        # when an AlertManager is attached (otherwise no audience for
        # the alerts and the broker probes are wasted RPCs).
        if self._alerts is not None and self._health_check_task is None:
            self._health_check_task = asyncio.create_task(
                self._health_check_loop()
            )

        # A46 invariant-sweep loop: 250ms cadence enforcement of the
        # invariant "engine FLAT + no bracket pending ⟹ no SELL STP at
        # broker". Kills orphan bracket children that survived parent
        # cancellation. Wired UNCONDITIONALLY (not gated by alerts) —
        # this is structural safety, not informational.
        if not self.gateway.paper and self._invariant_sweep_task is None:
            self._invariant_sweep_task = asyncio.create_task(
                self._invariant_sweep_loop()
            )

        # Short-selling data previews (availability / borrow / margin) — ON by
        # default: fetch live from IBKR (whatIf, tick-236, FEE_RATE) and fall
        # back to the offline estimate when IBKR returns nothing. These hit
        # IBKR over the single API socket, so running them AT connect would
        # contend with the initial reconcile + FIRST ORDER placement (slow
        # start on a high-latency link). We therefore run them DEFERRED +
        # serial, well past startup (see _deferred_short_data_previews), so
        # order flow is never delayed. GT_DISABLE_SHORT_DATA=1 skips them.
        asyncio.create_task(self._deferred_short_data_previews())

        # Periodic state-file save: every 1 second force a save so that
        # in-memory mutations between explicit event-driven saves can't
        # be lost on restart. Wired unconditionally — no audience needed,
        # this is a pure disk-write safety net. See __slots__ entry for
        # the full rationale (closes the ZM-class drift window).
        if self._periodic_state_save_task is None:
            self._periodic_state_save_task = asyncio.create_task(
                self._periodic_state_save_loop()
            )

        # Schedule daily counter reset. Critical for bots that rest GTC
        # orders overnight — without this, the risk counters
        # (trades_today, daily_pnl, consecutive_losses) only reset on the
        # next risk.check() call after midnight, which may not fire for
        # hours if there's no signal.
        if self._daily_reset_task is None:
            self._daily_reset_task = asyncio.create_task(self._daily_reset_scheduler())

        # Load persisted state if available
        self._load_state()

        # FL9 — snapshot whether we resumed WITH an open position. Captured
        # exactly here: AFTER _load_state() has restored _position_open from
        # the saved file, but BEFORE any live fill/reconcile can mutate it.
        # This is the session-start floor gate for the A43 execution-sum
        # (see _reconcile_position_state): FLAT boot → floor at
        # _engine_started_at (ignore stale pre-restart fills on a reused
        # clientId); resumed-with-position boot → full history (since=None)
        # so the genuine pre-restart entry is still recovered.
        self._started_with_position = bool(getattr(self, '_position_open', False))

        # ── State-aware ORDERS-panel hydration ─────────────────────────
        # `_order_history` is in-memory only and resets on every restart.
        # Whether to repopulate it from the audit CSV depends on what
        # state we're starting in:
        #
        #   FRESH START / POST-RESET
        #     (no saved position, no pending stop, state IDLE/MONITORING
        #      with no breakout level) → DO NOT hydrate. Any audit rows
        #     from prior runs are abandoned attempts ("--reset means start
        #     clean"); showing them clutters the panel with noise.
        #
        #   RESUMING ACTIVE CYCLE
        #     (saved entry_price > 0 OR saved pending_stop OR state in
        #      {IN_POSITION, EXIT_POSITION, WAITING_REENTRY}) → hydrate
        #     the audit. The current cycle's entry/SL/exit/re-entry rows
        #     are real activity the operator wants to see continuity for.
        #
        # The read_recent_orders() function applies a supersession filter
        # internally so cancelled-before-fill SUBMITTEDs (from `--reset`
        # cycles or restart-before-fill) are dropped automatically. What
        # remains is just real SUBMITTED→FILLED pairs + currently-resting
        # orders, which is exactly what the operator's "current cycle" is.
        is_active_resume = (
            (self._entry_price and self._entry_price > 0)
            or getattr(self, '_pending_stop', None) is not None
            or getattr(self, '_pending_stop_intent', None) is not None
            or self._state in (
                TradeState.IN_POSITION, TradeState.EXIT_POSITION,
                TradeState.WAITING_REENTRY,
            )
        )
        if is_active_resume:
            try:
                from src.config.audit import read_recent_orders
                recent = read_recent_orders(self.config.ticker, n=20)
                if recent:
                    self._order_history.extend(recent)
                    self._log(
                        f"Restored {len(recent)} prior order events from audit "
                        f"(most recent: {recent[-1].submitted_at.strftime('%Y-%m-%d %H:%M:%S')})"
                    )
            except Exception as e:
                # Best-effort — never let history hydration break startup.
                self._log(f"[hydrate] order history restore failed: {e}")
        else:
            self._log(
                "[hydrate] fresh/reset start detected — skipping audit "
                "hydration; ORDERS panel starts empty"
            )

        # Promote a non-active restored state to MONITORING before reconcile.
        self._promote_resumable_state()

        # Reconcile with broker BEFORE deciding whether to place new orders.
        # This re-wires fill routing for any GTC stop-limits resting at IBKR
        # from a prior session and populates _pending_stop, so the placement
        # helpers below correctly no-op via their idempotency guards.
        await self._reconcile_open_orders()

        # Tripwire: if the saved state recorded a pending stop intent but
        # reconciliation found NO matching broker order, something happened
        # between the last disk save and now — the order may have been
        # rejected silently, the bot crashed during placement, or someone
        # cancelled it in TWS. The position could be unprotected; log loudly.
        intent = getattr(self, '_pending_stop_intent', None)
        if intent and not getattr(self, '_pending_stop', None):
            self._log(
                f"!!! TRIPWIRE: saved state had pending {intent.get('side')} "
                f"stop-limit {intent.get('order_id')} "
                f"(trigger=${intent.get('stop_price') or 0:.2f}, "
                f"limit=${intent.get('limit_price') or 0:.2f}) — "
                f"BUT NO MATCHING ORDER AT BROKER. Position may be unprotected."
            )
            if self._audit:
                self._audit.log_order(
                    event="TRIPWIRE_LOST_PENDING",
                    order_id=intent.get('order_id', ''),
                    side=intent.get('side').value if hasattr(intent.get('side'), 'value') else '',
                    qty=intent.get('qty', 0),
                    order_type="STOP_LIMIT",
                    stop_price=intent.get('stop_price', 0),
                    limit_price=intent.get('limit_price', 0),
                    reason="Saved pending order absent from broker on restart",
                    state_at_time=self._state.value,
                    position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
                )
            # Escalate via AlertManager — file + stdout + Slack (if env set).
            # Severity CRITICAL because an unprotected position is worth paging
            # someone for, even at 2 AM ET. Position-protection invariant
            # broken = always loud.
            if self._alerts:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="TRIPWIRE_LOST_PENDING",
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"On restart, saved pending {intent.get('side')} order "
                        f"{intent.get('order_id')} is NOT at the broker. "
                        f"Position may be naked."
                    ),
                    context={
                        "order_id": intent.get('order_id'),
                        "stop_price": intent.get('stop_price'),
                        "limit_price": intent.get('limit_price'),
                        "qty": intent.get('qty'),
                        "position_open": self._position_open,
                        "entry_price": self._entry_price,
                    },
                    correlation_id=self._cycle_id,
                )
        # Snapshot the saved intent BEFORE we clear it, because the
        # naked-position guard below needs it for the "lost-fill recovery"
        # adoption path. SHORT INVERSION (P9): the entry is a SELL, so the
        # lost-fill signature is a saved SELL intent (engine placed the SELL
        # short entry, broker filled, engine crashed before persisting
        # entry_price/quantity). Variable name kept as `saved_buy_intent`
        # to minimise edits; it now holds the SELL entry intent.
        saved_buy_intent = None
        intent_snap = getattr(self, '_pending_stop_intent', None)
        if intent_snap:
            side = intent_snap.get('side')
            side_val = side.value if hasattr(side, 'value') else str(side)
            if side_val == OrderSide.SELL.value:
                saved_buy_intent = {
                    'qty': int(intent_snap.get('qty') or 0),
                    'stop_price': float(intent_snap.get('stop_price') or 0),
                    'limit_price': float(intent_snap.get('limit_price') or 0),
                    'order_id': intent_snap.get('order_id'),
                }

        # Either way, the intent has served its purpose now.
        self._pending_stop_intent = None

        # ── Naked-position guard (senior-quant safety) ────────────────
        # Before placing ANY new entry order, check broker position one
        # more time. The rules:
        #
        #   1. broker FLAT + engine FLAT  → safe, place new entry
        #   2. broker FLAT + engine IN_POS → existing branch below handles
        #      it (skip new entry + arm SL from saved state)
        #   3. broker has position + engine FLAT  → unexpected. Two cases:
        #      (a) state has evidence of prior bot ownership of this
        #          position (entry_price + qty saved): ADOPT it as ours,
        #          arm a protective SELL stop, skip new entry. This is
        #          the "previous cycle crashed before persisting state"
        #          case the user wants protected.
        #      (b) no state evidence: it's likely the user's personal
        #          stake (or someone else's manual trade). REFUSE to
        #          place new entry — placing one would double exposure
        #          on the symbol. The user must --reset or manually
        #          resolve before the bot will trade this symbol.
        #
        # Live mode only. Paper has no real broker positions to check.
        # GT_SKIP_NAKED_GUARD=1 — operator opt-out (set by stress test
        # drivers when standing FX cash balances would otherwise cause
        # STARTUP_REFUSED_NAKED for every fresh bot). Bypasses BOTH
        # the adoption decision and the hard abort below.
        try:
            _skip_naked = bool(os.environ.get("GT_SKIP_NAKED_GUARD", "").strip())
        except Exception:
            _skip_naked = False
        adopted_orphan = False
        if _skip_naked and not self.gateway.paper:
            self._log(
                "[NAKED-GUARD] BYPASSED via GT_SKIP_NAKED_GUARD env var. "
                "Engine will treat any pre-existing broker position as "
                "unrelated and place fresh entries on top. Use only for "
                "stress testing on paper accounts — NEVER on live."
            )
        if (not _skip_naked) and (not self.gateway.paper) and self._state not in (
            TradeState.IN_POSITION, TradeState.EXIT_POSITION
        ):
            broker_qty = 0
            broker_avg_cost = 0.0
            try:
                positions = await self.gateway.get_positions()
                for p in positions:
                    if p.symbol == self.config.ticker:
                        broker_qty = int(p.quantity)
                        # avg_cost = broker-reported average fill price.
                        # Used for the "lost-fill recovery" adoption path
                        # below — when we don't have entry_price saved
                        # but the broker knows exactly what we paid.
                        broker_avg_cost = float(getattr(p, 'avg_cost', 0.0) or 0.0)
                        break
            except Exception as e:
                self._log(f"[NAKED-GUARD] get_positions failed (best effort): {e}")

            # SHORT INVERSION (P9): a healthy short shows as a NEGATIVE broker
            # qty. broker_short is the positive magnitude we compare against
            # the engine's (positive) _quantity.
            broker_short = -broker_qty if broker_qty < 0 else 0
            # SHORT INVERSION (P11): a short-only strategy must NEVER see a
            # POSITIVE broker position. A long here is unaccounted (manual trade,
            # wrong-side residual, an over-cover from a prior bug) — HARD ABORT
            # rather than open a short ON TOP of it. Mirrors the broker_qty<0
            # lost-fill refusal below: audit + CRITICAL alert, then raise so
            # LiveTrader exits (no feed/dashboard/orders); state left untouched.
            if broker_qty > 0:
                self.audit("STARTUP_REFUSED_NAKED", {
                    "ticker": self.config.ticker,
                    "broker_qty": broker_qty,
                    "saved_qty": self._quantity,
                    "saved_entry": self._entry_price,
                    "client_id": self.config.ibkr_client_id,
                    "state_at_time": self._state.value,
                    "note": "unexpected LONG for short-only strategy",
                })
                if self._alerts:
                    from src.infra.alerts import AlertSeverity
                    self._alerts.raise_alert(
                        code="STARTUP_REFUSED_NAKED",
                        severity=AlertSeverity.CRITICAL,
                        message=(
                            f"Refused to start {self.config.ticker} on "
                            f"client_id={self.config.ibkr_client_id}: broker holds a "
                            f"LONG {broker_qty} but this is a SHORT-only strategy. "
                            f"No dashboard, no orders — operator must reconcile."
                        ),
                        context={
                            "ticker": self.config.ticker,
                            "broker_qty": broker_qty,
                            "client_id": self.config.ibkr_client_id,
                        },
                        correlation_id=self._cycle_id,
                    )
                raise NakedPositionError(
                    ticker=self.config.ticker,
                    broker_qty=broker_qty,
                    saved_qty=self._quantity,
                    saved_entry=self._entry_price,
                    client_id=self.config.ibkr_client_id,
                )
            if broker_qty < 0:
                # Is there a resting BUY cover stop at the broker for this short?
                has_sell_stop = bool(
                    getattr(self, '_pending_stop', None)
                    and self._pending_stop.get('side') == OrderSide.BUY
                )

                # ── Adoption criteria — two acceptable shapes ────────────
                # (1) STANDARD: saved entry_price + saved quantity both set
                #     AND saved qty matches the broker short magnitude.
                state_evidence_standard = (
                    self._entry_price and self._entry_price > 0
                    and self._quantity and self._quantity == broker_short
                )

                # (2) LOST-FILL RECOVERY: saved state shows NO position
                #     (quantity=0, entry=None) — but the saved pending_stop
                #     was a BUY whose qty equals the broker's broker_qty.
                #     That's the signature of: engine placed BUY → broker
                #     filled it → engine crashed/exited before persisting
                #     the fill (entry_price/quantity never got written).
                #     This client_id IS the one that opened the position;
                #     it's safe to adopt. We pull the actual fill price
                #     from broker.get_positions().avg_cost (canonical IBKR
                #     reported number) rather than guessing from the
                #     intent's limit_price.
                state_evidence_lost_fill = bool(
                    saved_buy_intent
                    and saved_buy_intent['qty'] == broker_short
                    and not (self._entry_price and self._entry_price > 0)
                )

                state_evidence = state_evidence_standard or state_evidence_lost_fill

                if state_evidence:
                    # For lost-fill, hydrate entry/qty from broker truth
                    # before we proceed. For standard, the saved values
                    # are already populated by _load_state earlier.
                    if state_evidence_lost_fill:
                        # Prefer broker avg_cost; fall back to the intent's
                        # limit_price if avg_cost is somehow 0 (shouldn't
                        # happen but defensive).
                        recovered_entry = broker_avg_cost
                        if recovered_entry <= 0 and saved_buy_intent:
                            recovered_entry = saved_buy_intent.get('limit_price', 0)
                        self._entry_price = self._round_to_tick(float(recovered_entry))
                        self._quantity = broker_short  # positive magnitude
                        self._log(
                            f"!!! LOST-FILL RECOVERY: saved state had pending SELL "
                            f"{saved_buy_intent['qty']} {self.config.ticker} but no "
                            f"recorded fill — broker now reports SHORT {broker_short} shares "
                            f"(signed {broker_qty}) at avg ${broker_avg_cost:.2f}. The fill "
                            f"happened during the prior session but state wasn't persisted. "
                            f"Hydrating entry_price=${self._entry_price:.2f}, "
                            f"quantity={self._quantity}."
                        )
                    else:
                        self._log(
                            f"!!! ORPHAN SHORT DETECTED: broker is short {broker_short} "
                            f"(signed {broker_qty}) {self.config.ticker} matching saved state "
                            f"(entry=${self._entry_price:.2f}, qty={self._quantity}). "
                            f"Adopting as IN_POSITION — fresh entry SUPPRESSED."
                        )

                    # Adopt the orphan: set IN_POSITION, arm SL if missing,
                    # skip the new entry placement below.
                    self._state = TradeState.IN_POSITION
                    self._position_open = True
                    if not self._stop_loss:
                        # Exact integer-tick math, no float drift.
                        self._stop_loss = self._protective_stop_price(
                            self._entry_price, self._effective_stop_pct()
                        )
                    if not self._highest_price:
                        self._highest_price = self._entry_price
                    self._save_state()
                    adopted_orphan = True
                    if self._alerts:
                        from src.infra.alerts import AlertSeverity
                        adoption_kind = (
                            "lost-fill recovery" if state_evidence_lost_fill
                            else "orphan position"
                        )
                        self._alerts.raise_alert(
                            code="CUSTOM_ORPHAN_ADOPTED",
                            severity=AlertSeverity.HIGH,
                            message=(
                                f"Adopted {adoption_kind}: broker has {broker_qty} "
                                f"{self.config.ticker} at avg ${broker_avg_cost or self._entry_price:.2f}; "
                                f"protective stop will be armed."
                            ),
                            context={
                                "ticker": self.config.ticker,
                                "broker_qty": broker_qty,
                                "entry_price": self._entry_price,
                                "broker_avg_cost": broker_avg_cost,
                                "kind": adoption_kind,
                                "had_sell_stop": has_sell_stop,
                            },
                            correlation_id=self._cycle_id,
                        )
                else:
                    # Unknown position — broker holds shares this client_id
                    # never opened (different client_id, manual TWS trade,
                    # different bot, state corruption, …). HARD ABORT.
                    #
                    # Previously this path flipped to MONITORING and let the
                    # engine keep running with "entry blocked" — but the feed
                    # was still subscribed, the dashboard still rendered, and
                    # on a tick that crossed the trigger the breakout-re-entry
                    # path could STILL place an order (the entry-blocked flag
                    # didn't cover every placement code path). Safer: raise,
                    # let LiveTrader catch + print a banner + exit. No feed,
                    # no dashboard, no order placement.
                    #
                    # Fire the alert + audit log BEFORE raising — Slack/file
                    # records persist beyond the process exit so the operator
                    # has a paper trail even if they miss the terminal output.
                    #
                    # NOTE: routed through `self.audit(...)` (engine event log)
                    # NOT `self._audit.log_order(...)` (orders CSV). This is a
                    # process-level startup refusal, not an order event —
                    # logging it as an order made the ORDERS panel show a
                    # phantom "STARTUP_REFUSED_NAKED" row that the operator
                    # had to mentally filter out.
                    self.audit("STARTUP_REFUSED_NAKED", {
                        "ticker": self.config.ticker,
                        "broker_qty": broker_qty,
                        "saved_qty": self._quantity,
                        "saved_entry": self._entry_price,
                        "client_id": self.config.ibkr_client_id,
                        "state_at_time": self._state.value,
                    })
                    if self._alerts:
                        from src.infra.alerts import AlertSeverity
                        self._alerts.raise_alert(
                            code="STARTUP_REFUSED_NAKED",
                            severity=AlertSeverity.CRITICAL,
                            message=(
                                f"Refused to start {self.config.ticker} on "
                                f"client_id={self.config.ibkr_client_id}: broker "
                                f"holds {broker_qty} shares unaccounted for by "
                                f"saved state. No dashboard, no orders."
                            ),
                            context={
                                "ticker": self.config.ticker,
                                "broker_qty": broker_qty,
                                "saved_qty": self._quantity,
                                "saved_entry": self._entry_price,
                                "saved_state": self._state.value,
                                "client_id": self.config.ibkr_client_id,
                                "has_sell_stop_at_broker": has_sell_stop,
                            },
                            correlation_id=self._cycle_id,
                        )
                    # DO NOT mutate _state or _save_state() here. The state
                    # file should stay exactly as the operator left it so
                    # they can inspect it and decide what to do. Writing
                    # MONITORING would obscure the previous IN_POSITION
                    # context if any.
                    raise NakedPositionError(
                        ticker=self.config.ticker,
                        broker_qty=broker_qty,
                        saved_qty=self._quantity,
                        saved_entry=self._entry_price,
                        client_id=self.config.ibkr_client_id,
                    )

        # ── Conflicting open-order guard (live mode only) ───────────────
        # The naked-position check above only sees FILLED positions. If
        # another client_id (or manual TWS) has a pending BUY for this
        # ticker that hasn't filled yet, broker_qty is still 0 — looks
        # safe — but the moment the trigger crosses, BOTH orders fill and
        # we end up with double the intended exposure.
        #
        # Calls reqAllOpenOrders to see every client's resting orders for
        # this account, filters to OUR ticker + ACTIVE statuses, and refuses
        # to start if any of them were placed by a different client_id.
        # Our own previously-placed orders were already re-bound by
        # _reconcile_open_orders() earlier in start() — those carry
        # owning_client_id == self.config.ibkr_client_id and are excluded.
        #
        # Applies in EVERY engine state (FRESH, MONITORING, WAITING_REENTRY,
        # IN_POSITION, EXIT_POSITION) because the conflict is real regardless
        # of what we think we own: we just don't want two API clients armed
        # on the same symbol.
        if not self.gateway.paper:
            try:
                all_orders = await self.gateway.fetch_all_open_orders_for_symbol(
                    self.config.ticker
                )
            except Exception as e:
                self._log(f"[CONFLICT-GUARD] fetch_all_open_orders failed (best effort): {e}")
                all_orders = []
            our_cid = int(self.config.ibkr_client_id)
            # `_order_id_map` is populated by `_reconcile_open_orders()` above —
            # every broker_id in there is an order we've ALREADY adopted into
            # our registry (it was placed by THIS client_id in a prior
            # session). IBKR's `clientId` field on those orders is sometimes
            # unreliable across sessions: orders placed by client_id=3 in a
            # previous run may come back from `reqAllOpenOrders` with
            # `clientId=0` (or some other quirk), which would make the
            # `owning_client_id != our_cid` filter wrongly flag them as
            # foreign and refuse to restart.
            #
            # Adding the `broker_id in our_broker_ids` exclusion handles that:
            # if reconciliation has bound a broker order to our engine, it's
            # ours by construction, regardless of what IBKR claims clientId
            # is. Conflict only fires for orders we did NOT adopt.
            our_broker_ids = set(
                str(bid) for bid in getattr(self.gateway, '_order_id_map', {}).keys()
            )
            foreign = [
                o for o in all_orders
                if int(o.get('owning_client_id', 0)) != our_cid
                and str(o.get('broker_id', '')) not in our_broker_ids
            ]
            if foreign:
                # Build a compact diagnostic string for the audit + alert.
                ord_summary = "; ".join(
                    f"{o.get('action','?')} {o.get('qty','?')} "
                    f"@ trig=${o.get('stop_price') or 0:.2f} "
                    f"lim=${o.get('limit_price') or 0:.2f} "
                    f"[cid={o.get('owning_client_id','?')}, id={o.get('broker_id','?')}, "
                    f"status={o.get('status','?')}]"
                    for o in foreign
                )
                self._log(
                    f"!!! CONFLICTING OPEN ORDER(S) AT BROKER: {ord_summary} — "
                    f"refusing to start on client_id={our_cid}"
                )
                # Process-level refusal — route to the engine event log,
                # NOT the orders audit CSV. Logging it as an order made
                # the dashboard ORDERS panel show a phantom row for what's
                # really just "we declined to start"; the operator's eye
                # then had to filter those out from real trade activity.
                self.audit("STARTUP_REFUSED_CONFLICT", {
                    "ticker": self.config.ticker,
                    "our_client_id": our_cid,
                    "state_at_time": self._state.value,
                    "conflicting_orders": foreign,
                    "summary": ord_summary,
                })
                if self._alerts:
                    from src.infra.alerts import AlertSeverity
                    self._alerts.raise_alert(
                        code="STARTUP_REFUSED_CONFLICT",
                        severity=AlertSeverity.CRITICAL,
                        message=(
                            f"Refused to start {self.config.ticker} on "
                            f"client_id={our_cid}: another client has "
                            f"active order(s) for this symbol — would "
                            f"double-fill on trigger crossing."
                        ),
                        context={
                            "ticker": self.config.ticker,
                            "our_client_id": our_cid,
                            "conflicting_orders": foreign,
                        },
                        correlation_id=self._cycle_id,
                    )
                raise ConflictingOpenOrderError(
                    ticker=self.config.ticker,
                    orders=foreign,
                    client_id=our_cid,
                )

        # Choose the entry trigger. If we restored WAITING_REENTRY from a
        # prior cycle, the right entry level is the saved breakout — NOT
        # config.trigger_price (which is the *initial* cycle's setup).
        if self._state == TradeState.WAITING_REENTRY and self._previous_breakout_level:
            trigger_price = self._previous_breakout_level
            entry_label = "restored breakout"
        else:
            trigger_price = self.config.trigger_price
            entry_label = "configured trigger"

        # Do NOT place a fresh entry order if a position was restored from
        # disk OR was just adopted by the naked-position guard above.
        if self._state in (TradeState.IN_POSITION, TradeState.EXIT_POSITION):
            self._log(
                f"Restored {self._state.value} @ ${self._entry_price or 0:.2f} "
                f"— skipping initial entry order (position already open)"
            )
            # If reconciliation didn't find a resting protective SL, we have
            # an open position with no stop. Arm one now from restored state.
            # Also covers the just-adopted-orphan case from the guard above.
            if (
                self._state == TradeState.IN_POSITION
                and not getattr(self, '_pending_stop', None)
                and self._entry_price
                and self._quantity
            ):
                reason = "STOP_LOSS_ADOPTED_ORPHAN" if adopted_orphan else "STOP_LOSS"
                self._log(f"No protective stop found at broker for restored position — arming one now ({reason})")
                await self._place_protective_stop(reason)
        else:
            # SHORT INVERSION (P1): SELL STOP-LIMIT placement — idempotent: if
            # reconciliation already attached a resting SELL stop-limit
            # (_pending_stop is set), the helper returns None.
            self._log(f"Placing SELL STOP_LIMIT (short entry) @ {entry_label} ${trigger_price:.2f}")
            await self._place_entry_stop_limit(trigger_price)

        self._log(f"Engine started - {self._state.value} (trigger=${trigger_price})")
        await self._log_json("STRATEGY_STARTED", ticker=self.config.ticker, trigger=trigger_price)
        self.audit("STARTED", {"ticker": self.config.ticker, "trigger": trigger_price})

    def _load_state(self) -> bool:
        """Load persisted state from disk and restore engine fields.

        Returns True if state was loaded, False if no state file existed.

        Single canonical state-restore path (the older public `load_state`
        method has been folded into this one). Restores the saved
        TradeState — NOT a hardcoded IN_POSITION — so a session that ended
        in EXIT_POSITION or WAITING_REENTRY resumes correctly.

        Stop-loss handling: recalculates `_stop_loss` from CURRENT
        config.stop_loss_pct rather than the saved value. This lets you
        change `--stop` between sessions and have the new pct take effect
        on the existing position (the saved stop value would be from the
        old config). Saved value is only used as a fallback when entry
        price isn't known.
        """
        state = self.state_store.load()
        if not state:
            return False

        # Restore state machine state
        saved_state = state.get("state")
        if saved_state:
            try:
                self._state = TradeState(saved_state)
            except ValueError:
                self._log(f"Unknown saved state '{saved_state}' — keeping {self._state.value}")

        # Always restore breakout level (used in WAITING_REENTRY for re-entry
        # price, independent of whether a position is currently open).
        self._previous_breakout_level = state.get("previous_breakout_level")

        # Restore daily counters / PnL so the dashboard and risk gates pick
        # up where we left off (within the same trading day).
        self._trades_today = state.get("trades_today", 0)
        self._wins = state.get("wins", 0)
        self._losses = state.get("losses", 0)
        self._pnl = state.get("pnl", 0.0)
        self._total_commission = state.get("total_commission", 0.0)
        # Restore in-flight BUY-side commission so a crash between BUY fill
        # and SELL fill doesn't lose entry-side cost from the eventual P&L.
        self._pending_buy_commission = state.get("pending_buy_commission", 0.0)
        self._cycle_id = state.get("cycle_id", self._cycle_id)
        # Restore the monotonic cycle counter so a restart between cycle
        # bumps doesn't reset the sequence and reuse a previous cycle's
        # `_n{seq}` suffix. Defaults to 0 for legacy state files written
        # before the cycle-seq migration.
        self._cycle_seq = int(state.get("cycle_seq", 0) or 0)
        # Restore feed.high cycle-entry baseline so the augmented
        # _track_high keeps comparing against the right anchor after a
        # mid-cycle restart. Defaults to 0 (no augmentation effect) for
        # legacy state files written before this field existed.
        try:
            self._feed_high_at_entry = float(state.get("feed_high_at_entry", 0) or 0)
        except (TypeError, ValueError):
            self._feed_high_at_entry = 0.0

        # Restore position fields only when a position was actually open.
        if state.get("position_open"):
            self._position_open = True
            self._entry_price = state.get("entry_price")
            self._highest_price = state.get("highest_price")
            # int() coerces in case the JSON deserialized to float
            # (state files written by earlier code had `quantity: 10.0`).
            self._quantity = int(state.get("quantity", 0) or 0)
            # ── Preserve the ORIGINAL bracket SL across restart ──────────
            # Previously: recomputed `_stop_loss` from CURRENT
            # `config.stop_loss_pct` so a new --stop-pct CLI flag would
            # "stick". That silently moved the protective stop on
            # already-open positions (2026-05-27 META incident: original
            # SL @ $617.31 was replaced by $612.36 because restart's CLI
            # default was 1% vs original cycle's 0.20%).
            #
            # Now: trust the saved state. Use the saved stop PRICE
            # directly, and persist the saved pct as `_active_stop_pct`
            # so any subsequent SL re-arm in this cycle (e.g. health-
            # check fresh-SL, bracket-modify-failed fallback) uses the
            # same pct. `_active_stop_pct` is cleared on SELL fill so
            # the next cycle picks up the current CLI config.
            saved_stop = state.get("stop_loss")
            saved_pct = state.get("stop_loss_pct")
            if self._entry_price:
                if saved_stop:
                    self._stop_loss = float(saved_stop)
                else:
                    # No saved price (legacy state file) — fall back to
                    # computing from saved pct (if any) or current config.
                    pct_for_calc = float(saved_pct) if saved_pct else self.config.stop_loss_pct
                    self._stop_loss = self._protective_stop_price(self._entry_price, pct_for_calc)
            else:
                self._stop_loss = saved_stop
            if saved_pct and float(saved_pct) > 0:
                self._active_stop_pct = float(saved_pct)
            else:
                # Legacy state file with no persisted pct. Infer from the
                # saved entry + stop so the in-cycle override is still
                # consistent even though the file pre-dates this fix.
                if self._entry_price and self._stop_loss and self._entry_price > 0:
                    self._active_stop_pct = max(
                        0.0,
                        round(1.0 - (self._stop_loss / self._entry_price), 6),
                    )
                else:
                    self._active_stop_pct = None
            stop_str = f"{self._stop_loss:.2f}" if self._stop_loss else "None"
            pct_str = (
                f"{self._active_stop_pct*100:.4f}% (preserved from saved state)"
                if self._active_stop_pct is not None
                else f"{self.config.stop_loss_pct*100:.4f}% (from CLI)"
            )
            self._log(
                f"Restored {self._state.value}: qty={self._quantity} @ "
                f"${self._entry_price:.2f}, stop=${stop_str} ({pct_str})"
            )

        # Restore the saved pending-order intent into a SEPARATE field.
        # We deliberately do NOT touch _pending_stop here — broker
        # reconciliation (run from start() right after this) is the
        # authoritative source of resting orders, and we don't want a
        # stale disk entry to mask a broker-side absence. Instead, after
        # reconcile we compare _pending_stop (broker truth) against
        # _pending_stop_intent (last saved intent); a mismatch raises a
        # loud alert so the user can investigate.
        saved_pending = state.get("pending_stop")
        if saved_pending:
            try:
                self._pending_stop_intent = {
                    'order_id': saved_pending.get('order_id'),
                    'qty': saved_pending.get('qty'),
                    'stop_price': saved_pending.get('stop_price'),
                    'limit_price': saved_pending.get('limit_price'),
                    'side': OrderSide(saved_pending.get('side')),
                    # Carry the bracket marker through so reconcile can
                    # tell the bracket parent from the bracket child
                    # from a legacy non-bracket pending order.
                    'bracket_parent': bool(saved_pending.get('bracket_parent', False)),
                    'from_bracket': bool(saved_pending.get('from_bracket', False)),
                    'order_type': saved_pending.get('order_type'),
                }
            except (ValueError, TypeError):
                self._pending_stop_intent = None
        self._pending_exit_reason = state.get("pending_exit_reason")

        # Restore _bracket_child if saved state had an active bracket
        # at write time. The reconcile pass (in start()) then verifies
        # the child is actually still resting at the broker and adopts
        # it as _pending_stop. Missing field → legacy state file written
        # before the bracket migration → leave _bracket_child = None
        # (the engine still works, just without bracket awareness on
        # this particular cycle).
        saved_bracket = state.get("bracket_child")
        if saved_bracket:
            try:
                self._bracket_child = {
                    'order_id': saved_bracket.get('order_id'),
                    'qty': int(saved_bracket.get('qty') or 0),
                    'stop_price': saved_bracket.get('stop_price'),
                    'side': OrderSide(saved_bracket.get('side')),
                    'order_type': saved_bracket.get('order_type'),
                    'parent_order_id': saved_bracket.get('parent_order_id'),
                }
                self._log(
                    f"Restored bracket child intent: {self._bracket_child['order_id']} "
                    f"({self._bracket_child['qty']} sh @ stop ${self._bracket_child['stop_price']:.2f}) "
                    f"— reconcile will verify it's still at the broker"
                )
            except (ValueError, TypeError) as e:
                self._log(f"Failed to restore bracket_child from state: {e} — clearing")
                self._bracket_child = None

        # ── Restore the frozen SL pct for a bracket whose parent had NOT
        #    filled at save time ──────────────────────────────────────────
        # The pct restore above lives inside `if position_open:` — correct
        # for an OPEN cycle, but it SKIPS the submit→parent-fill window
        # (position not yet open). Without this, a restart in that window
        # loses the frozen pct, and the parent-fill freeze then defaults to
        # config.stop_loss_pct (e.g. 1%), silently widening a tighter user
        # stop. Direction-agnostic: only restores the persisted pct value.
        if self._bracket_child is not None and self._active_stop_pct is None:
            saved_pct = state.get("stop_loss_pct")
            try:
                if saved_pct and float(saved_pct) > 0:
                    self._active_stop_pct = float(saved_pct)
                    self._log(
                        f"Restored frozen SL pct {self._active_stop_pct * 100:.4f}% "
                        f"for resting bracket {self._bracket_child.get('order_id')} "
                        f"(prevents parent-fill retarget from defaulting to "
                        f"{self.config.stop_loss_pct * 100:.2f}%)"
                    )
            except (TypeError, ValueError):
                pass

        return True

    async def _reconcile_open_orders(self) -> None:
        """Rebuild registry / order-id map / _pending_stop from broker state.

        On restart, the engine's in-memory data structures are empty but the
        broker may still hold GTC stop-limits placed in a prior session. If
        we don't re-link them, an eventual fill comes through `fillEvent`
        with a broker_id we don't recognize, falls through to the
        "Unknown order id, bail safely" guard in `_on_gateway_fill`, and the
        position state never advances — silent drift between engine and broker.

        Strategy:
            1. Ask the gateway for all working orders on our symbol.
            2. For each recognized stop-limit, derive its engine_id from the
               naming scheme used by `_place_entry_stop_limit` /
               `_place_protective_stop`, register it in the OrderRegistry,
               and re-attach the fillEvent via `register_existing_order`.
            3. If the order's side matches our current state context
               (SELL while IN_POSITION/EXIT_POSITION; BUY while
               MONITORING/WAITING_REENTRY), populate `_pending_stop` so the
               idempotency guards in the placement helpers fire correctly
               and `_track_position` knows the SL is at the broker.
            4. Unrecognized orders (manual TWS orders, leftover from other
               clients) are NOT registered — fills against them will still
               hit the "Unknown order id" guard. Log loudly so the user can
               investigate / cancel.

        Paper mode: no-op (no broker-side persistence to reconcile with).
        """
        if self.gateway.paper:
            return

        # A62: gate the invariant sweep while reconcile is rebuilding
        # _pending_stop / _bracket_child from broker truth. Sweep firing
        # mid-reconcile saw a SELL at broker not yet in legitimate_sell_ids
        # (because bracket-child adoption is a post-loop step) and
        # cancelled it as orphan, cascading via A57 to the parent. The
        # try/finally ensures the flag clears even on exception so a
        # raised reconcile doesn't leave the sweep silenced forever.
        self._reconciling = True
        # Fencing token (SHORT mirror of LONG EURUSD naked-short 2026-07-28):
        # capture the connection epoch we are about to reconcile against.
        # Stamp _ledger_epoch only on CLEAN completion (below) so a raised
        # reconcile leaves the gate closed — a half-built ledger is never
        # marked "trusted" for this connection. If the connection flaps
        # mid-reconcile gateway.connection_epoch advances past this value, so
        # the stamp won't match → _actuation_allowed() stays closed → another
        # reconcile is forced. Capturing at START (not end) is what makes it safe.
        _epoch_at_start = getattr(self.gateway, 'connection_epoch', 0)
        try:
            await self._reconcile_open_orders_inner()
            self._ledger_epoch = _epoch_at_start
        finally:
            self._reconciling = False

    def _actuation_allowed(self) -> bool:
        """Fencing-token gate: may an actuator act on ledger-derived position
        state right now?

        Returns True iff the ledger has been reconciled against broker
        executions for the CURRENT connection. Every gateway (re)connect
        bumps gateway.connection_epoch; `_reconcile_open_orders` stamps
        `_ledger_epoch` to match once it has replayed any fills missed while
        disconnected. Until they match, the ledger may be missing a fill that
        landed while we were away, so no order-placing / cancelling actuator
        may act — it must DEFER. Deferring is fail-safe: the open (short)
        position stays covered by the resting broker bracket in the meantime.

        Special cases:
          * Paper (internal sim): always True — the engine IS the source of
            truth; there is no broker position to go stale against.
          * Gateway without connection_epoch (legacy / test double): fail
            OPEN so this can never freeze a gateway predating the token.
        """
        if getattr(self.gateway, 'paper', False):
            return True
        if not getattr(self.gateway, 'connected', False):
            return False
        if getattr(self, '_reconciling', False):
            return False
        epoch = getattr(self.gateway, 'connection_epoch', None)
        if epoch is None:
            return True  # gateway predates the fencing token — legacy behavior
        return epoch == self._ledger_epoch

    async def _reconcile_open_orders_inner(self) -> None:
        # Step 0 (NEW): Gap-fill _highest_price for the disconnect window.
        # Must run BEFORE _reconcile_missed_fills so that when a SELL fill
        # is replayed, it uses the corrected peak — not the pre-disconnect
        # value — when stashing _previous_breakout_level for re-entry.
        await self._gap_fill_highest_price()

        # Step 0a: Catch up on fills that happened while we were
        # disconnected. This MUST run before the open-orders walk so the
        # engine's _position_open / _entry_price / counters reflect any
        # missed fills before we try to figure out what's still resting.
        # Safe to call always — exec_id dedup makes it a no-op when there
        # are no truly-new fills (e.g., normal reconnect with no activity).
        await self._reconcile_missed_fills()

        # Step 0a-bis: Recompute realized PnL from broker truth.
        # The state file's `_pnl` was written by whatever commission
        # model the previous session used. Old buggy sessions (before
        # the commissionReportEvent wiring) recorded modeled-estimate
        # commissions ($92.58 on EURUSD instead of $2). Those wrong
        # numbers persist across restarts via the state file. The
        # hedge-fund-grade fix: on every startup, walk today's
        # broker-side executions (which now carry the real commission
        # reports) and recompute realized PnL from authoritative
        # source — overwrite whatever the state file claims.
        await self._recompute_realized_pnl_from_broker()

        # Step 0b: Compare broker-side position to engine-side belief.
        # If they differ (and missed-fill replay didn't close the gap),
        # something's drifted — fire a CRITICAL alert so the operator
        # can investigate. This is the "I see 1 NVDA in my IBKR account
        # but the engine thinks I'm FLAT" case.
        await self._reconcile_position_state()

        # Disconnect-safe fetch. fetch_open_orders now RAISES
        # ConnectionError on disconnect (post-2026-06-06 fix). Catching
        # it here means startup reconcile defers rather than concluding
        # "no open orders" on unknown data. The next health-check tick
        # will retry after reconnect.
        try:
            open_orders = self.gateway.fetch_open_orders()
        except ConnectionError as e:
            self._log(
                f"Reconcile: gateway disconnected during fetch_open_orders "
                f"({e}). Skipping reconcile; will retry on next tick."
            )
            return
        if not open_orders:
            self._log("Reconcile: no open broker orders found")
            return

        self._log(f"Reconcile: found {len(open_orders)} open broker order(s)")

        sym = self.config.ticker
        # Track whether we found a bracket child at the broker. If we did,
        # and the engine restored `_bracket_child` from state, we'll
        # verify the stop_price is current (post-VWAP) vs stale (initial
        # estimate) after the loop and re-modify if needed.
        bracket_child_found_broker_id: Optional[str] = None
        # A64 (2026-06-10): separate "we saw the saved _bracket_child at
        # broker" detection, INDEPENDENT of state_matches. The old logic
        # used `bracket_child_found_broker_id` for both half-fill recovery
        # AND the defensive "not present → cancel" branch — but the
        # variable only got set inside the state_matches=True adoption
        # block (line ~3052). In WAITING_REENTRY/MONITORING state where
        # the SELL STP's state_matches is False, the variable stays
        # None even though the child IS physically resting at IBKR.
        # The defensive cancel block then fires erroneously and kills
        # the bracket child by id → A57 cascades to parent → entire
        # adopted bracket gone → user reports "every reconcile cancels
        # the bracket". This separate flag is set whenever we encounter
        # any SELL STP whose engine_id matches the saved `_bracket_child`,
        # regardless of state.
        saved_bc_engine_id: Optional[str] = None
        if isinstance(getattr(self, '_bracket_child', None), dict):
            saved_bc_engine_id = self._bracket_child.get('order_id')
        bracket_child_seen_at_broker: bool = False
        for o in open_orders:
            action = o['action']
            otype_norm = (o['order_type'] or '').replace(' ', '').upper()
            qty = o['qty']
            broker_id = o['broker_id']

            # Auto-recognize STP-LMT (legacy protective stop) AND plain
            # STP (the bracket child SELL — market on trigger, the new
            # default for live entries). LMT/MKT recovered here would be
            # unusual; log and leave alone.
            if otype_norm not in ('STPLMT', 'STP'):
                self._log(
                    f"Reconcile: skipping {action} {o['order_type']} {qty} "
                    f"(broker_id={broker_id}) — only STP / STOP_LIMIT auto-recognized"
                )
                continue

            # Engine-id naming depends on type AND side:
            #   BUY  STP_LMT  → ENTRY_BUY_{qty}_{sym}            (bracket parent OR legacy)
            #   SELL STP_LMT  → SL_SELL_{qty}_{sym}              (legacy post-fill protective)
            #   SELL STP      → BR_SELL_{qty}_{sym}              (bracket child, market on trigger)
            #
            # The bracket-child prefix `BR_` distinguishes it from the
            # post-fill `SL_` (which is the legacy `_place_protective_stop`
            # output and the `STOP_LOSS_MARKET_FALLBACK` gap-down path).
            # ── Engine-id recovery, preferring broker's orderRef ──────
            # We stash our engine_id in IBKR's `orderRef` at submission
            # time, so the broker round-trip returns the EXACT id —
            # including the `_n{cycle_seq}` per-cycle disambiguator. If
            # orderRef is empty (manual TWS order, or order placed by an
            # older build that didn't set it), fall back to convention
            # reconstruction using the CURRENT cycle_seq. The convention
            # path is "best effort" and may collide if multiple legacy
            # orders are resting; orderRef is the authoritative source.
            # SHORT INVERSION (P9): role inference flips —
            #   SELL STP_LMT → ENTRY_SELL (breakdown entry; bracket parent/legacy)
            #   BUY  STP_LMT → SL_BUY    (legacy post-fill protective cover)
            #   BUY  STP     → BR_BUY    (bracket child cover, market on trigger)
            broker_ref = o.get('order_ref') or ''
            if action == 'SELL' and otype_norm == 'STPLMT':
                engine_id = broker_ref if broker_ref else self._make_engine_id(
                    f"ENTRY_{OrderSide.SELL.value}", qty
                )
                side = OrderSide.SELL
                otype = OrderType.STOP_LIMIT
                state_matches = self._state in (
                    TradeState.MONITORING, TradeState.WAITING_REENTRY
                )
                is_bracket_child = False
            elif action == 'BUY' and otype_norm == 'STPLMT':
                engine_id = broker_ref if broker_ref else self._make_engine_id(
                    f"SL_{OrderSide.BUY.value}", qty
                )
                side = OrderSide.BUY
                otype = OrderType.STOP_LIMIT
                state_matches = self._state in (
                    TradeState.IN_POSITION, TradeState.EXIT_POSITION
                )
                is_bracket_child = False
            elif action == 'BUY' and otype_norm == 'STP':
                # Bracket child cover — market on trigger. Prefer orderRef so
                # we recover the exact cycle_seq; otherwise reconstruct from
                # convention with the current seq (best-effort fallback
                # for legacy/manual orders).
                engine_id = broker_ref if broker_ref else self._make_engine_id(
                    f"BR_{OrderSide.BUY.value}", qty
                )
                side = OrderSide.BUY
                otype = OrderType.STOP
                state_matches = self._state in (
                    TradeState.IN_POSITION, TradeState.EXIT_POSITION
                )
                is_bracket_child = True
                # A64: mark the saved bracket child as "seen at broker"
                # whenever we encounter a SELL STP whose engine_id (from
                # orderRef or convention) matches `_bracket_child.order_id`,
                # regardless of state_matches. This stops the defensive
                # cancel block at line ~3260 from misfiring in
                # WAITING_REENTRY/MONITORING state.
                if (saved_bc_engine_id
                        and engine_id == saved_bc_engine_id):
                    bracket_child_seen_at_broker = True
            else:
                self._log(
                    f"Reconcile: unhandled combination {action} {otype_norm} on "
                    f"{broker_id}, skipping"
                )
                continue

            # ── A82: PRIOR-SESSION ORPHAN-SELL GUARD ────────────────────
            # A resting protective SELL STOP only makes sense when there is
            # a position to protect. The 2026-06-12 LLY/AUDUSD incident
            # (caught live by the three-truths monitor): a prior run left a
            # SELL STP resting at the broker; this run started FLAT, the qty
            # matched (same --quantity) so the qty-mismatch guard below let
            # it through, and `state_matches` was False (MONITORING) so it
            # was neither adopted nor cancelled — just left resting. It
            # later TRIGGERED with no long behind it → broker went SHORT.
            # The engine's phantom-sell rejection (A17) protected the
            # engine's books but NOT the venue (§1.3 caveat: the broker
            # trade has already happened by the time the fill arrives).
            #
            # Fix: a SELL STP whose engine_id carries a DIFFERENT session id
            # than this run (so it is NOT our own freshly-armed bracket
            # child), is NOT the saved bracket child we are legitimately
            # re-adopting after a same-bot restart (A54), and which we hold
            # NO position to protect against — is a prior-session orphan
            # that can ONLY fire into a short. Cancel it now, before it can
            # trigger. (Session-id alone is insufficient: a legit same-bot
            # restart also carries the prior session id on its own bracket,
            # hence the saved-bracket-child exemption.)
            # SHORT INVERSION (P9): the protective leg is now a BUY cover, so
            # a stray prior-session BUY STP/STP-LMT with no short behind it
            # would fire into a LONG. Target BUY (was SELL).
            if side == OrderSide.BUY and otype in (OrderType.STOP, OrderType.STOP_LIMIT):
                order_session = engine_id.rsplit('_s', 1)[-1] if '_s' in engine_id else ''
                is_prior_session = bool(order_session) and order_session != self._session_id
                is_saved_bracket = bool(saved_bc_engine_id) and engine_id == saved_bc_engine_id
                engine_holds = bool(self._position_open) and bool(self._quantity)
                if is_prior_session and not is_saved_bracket and not engine_holds:
                    self._log(
                        f"Reconcile: PRIOR-SESSION ORPHAN BUY COVER {action} {otype_norm} "
                        f"qty={qty} (broker_id={broker_id}, engine_id={engine_id}, "
                        f"order_session={order_session} != current {self._session_id}), "
                        f"engine holds NO position — cancelling before it can fire "
                        f"into a long (A82)."
                    )
                    try:
                        await self.gateway.cancel_order(broker_id)
                    except Exception as e:
                        self._log(f"Reconcile: A82 orphan BUY cover cancel raised: {e}")
                    if self._audit:
                        try:
                            self._audit.log_order(
                                event="ORPHAN_SELL_CANCELLED",
                                order_id=engine_id,
                                side="BUY",
                                qty=qty,
                                order_type=otype_norm,
                                reason=(
                                    f"A82: prior-session orphan BUY cover (session "
                                    f"{order_session} != current {self._session_id}), "
                                    f"engine FLAT — cancelled to prevent an unwanted LONG from the orphan cover firing."
                                ),
                                context={
                                    "ticker": self.config.ticker,
                                    "broker_id": broker_id,
                                    "order_session": order_session,
                                    "current_session": self._session_id,
                                },
                                correlation_id=getattr(self, '_cycle_id', ''),
                            )
                        except Exception:
                            pass
                    continue   # do NOT register/adopt — orphan killed

            # ── QTY-MISMATCH GUARD: reject stale orphans from a
            # different config (the 2026-06-02 PLTR incident).
            #
            # SHORT INVERSION (P11): the protective leg is a BUY cover, so this
            # qty-mismatch orphan guard must key on BUY (was SELL for long).
            # Scenario: a previous run used qty=100 → left a BUY cover for 100
            # resting at IBKR. Current run uses qty=30. If adopted, when it
            # triggers IBKR BUYs 100 against the −30 short → covers 30 AND goes
            # LONG 70 — the exact wrong-direction fill this guard exists to stop.
            # (The SELL leg is now the *entry*, where a qty mismatch is benign.)
            #
            # The fix: when adopting a BUY cover, require its qty to match the
            # engine's expected position size; else cancel + audit + skip.
            #
            # Expected position qty = self._quantity if we currently hold a
            # position, otherwise self.config.quantity. Both must match
            # whatever the engine would size a fresh cover to.
            if side == OrderSide.BUY and otype in (OrderType.STOP, OrderType.STOP_LIMIT):
                expected_qty = (
                    int(self._quantity)
                    if self._position_open and self._quantity
                    else int(self.config.quantity)
                )
                if int(qty) != expected_qty:
                    self._log(
                        f"Reconcile: REJECTING stale BUY-cover {action} {otype_norm} "
                        f"qty={qty} (broker_id={broker_id}) — engine expects "
                        f"qty={expected_qty}. Cancelling at broker (almost "
                        f"certainly an orphan from a previous strategy "
                        f"config with different --quantity)."
                    )
                    try:
                        await self.gateway.cancel_order(broker_id)
                    except Exception as e:
                        self._log(f"Reconcile: orphan BUY-cover cancel raised: {e}")
                    if self._audit:
                        try:
                            self._audit.log_order(
                                event="STALE_SELL_REJECTED",  # code kept (tests/analyzers depend on it)
                                order_id=engine_id,
                                side="BUY",  # SHORT INVERSION (P11): the stale leg is a BUY cover
                                qty=qty,
                                order_type=otype_norm,
                                stop_price=o.get('stop_price'),
                                signal_price=o.get('stop_price'),
                                reason=(
                                    f"Stale BUY-cover detected on reconcile. "
                                    f"broker_qty={qty}, engine_expected_qty={expected_qty}. "
                                    f"Cancelled at IBKR to prevent a qty-mismatch OVER-COVER "
                                    f"that would flip the book LONG (the 2026-06-02 PLTR class of bug)."
                                ),
                                state_at_time=self._state.value,
                                position_at_time=(  # SHORT INVERSION (P11)
                                    "SHORT" if self._position_open else "FLAT"
                                ),
                            )
                        except Exception:
                            pass
                    if self._alerts:
                        try:
                            from src.infra.alerts import AlertSeverity
                            self._alerts.raise_alert(
                                code="STALE_SELL_REJECTED",
                                severity=AlertSeverity.HIGH,
                                message=(
                                    f"Reconcile rejected stale BUY-cover on "
                                    f"{self.config.ticker}: broker has a BUY cover for "
                                    f"qty={qty} but engine expects qty={expected_qty}. "
                                    f"Cancelled at IBKR. Almost certainly an orphan "
                                    f"from a previous run with different config. "
                                    f"If it had been adopted and fired, we would have "
                                    f"OVER-COVERED by {qty - expected_qty} (flipping the book LONG)."
                                ),
                                context={
                                    "ticker": self.config.ticker,
                                    "broker_id": broker_id,
                                    "broker_qty": qty,
                                    "engine_expected_qty": expected_qty,
                                    "would_have_gone_long": qty - expected_qty,
                                },
                                correlation_id=getattr(self, '_cycle_id', ''),
                            )
                        except Exception:
                            pass
                    continue   # do NOT adopt — skip to next open order

            # Rewire fill routing back into the engine
            self.gateway.register_existing_order(broker_id, engine_id, o.get('_trade'))

            # Register in the in-memory order book
            self.registry.submit(OrderRecord(
                order_id=engine_id,
                symbol=sym,
                side=side,
                qty=qty,
                order_type=otype,
                stop_price=o['stop_price'] or 0.0,
                limit_price=o['limit_price'] or 0.0,
                status=OrderStatus.SUBMITTED,
                submitted_at=self._ts(),
            ))

            # Drive engine guards (_pending_stop) only if this order fits the
            # current state. Otherwise it's a stale/mismatched order — still
            # tracked in registry for visibility, but won't influence the
            # state machine.
            if state_matches and not getattr(self, '_pending_stop', None):
                self._pending_stop = {
                    'order_id': engine_id,
                    'qty': qty,
                    'stop_price': o['stop_price'],
                    'limit_price': o['limit_price'],
                    'side': side,
                    # Mark bracket child so downstream code (audit, modify,
                    # health check) knows this isn't the legacy SL_SELL_LMT.
                    'from_bracket': is_bracket_child,
                    'order_type': 'STP' if is_bracket_child else 'STPLMT',
                }
                # Surface the broker-side status (PendingSubmit vs Submitted
                # vs PreSubmitted) when adopting a bracket child — distinguishes
                # "child armed and waiting for parent fill" from "child active,
                # protecting open position".
                status_note = ""
                if is_bracket_child and o.get('status') in (
                    'PendingSubmit', 'PreSubmitted'
                ):
                    status_note = f" [status={o['status']} — armed pending parent fill]"
                elif is_bracket_child:
                    status_note = f" [status={o.get('status')} — active]"
                self._log(
                    f"Reconcile: re-attached {action} {otype_norm} "
                    f"stop=${o['stop_price'] or 0:.2f}"
                    + (f" limit=${o['limit_price']:.2f}" if o.get('limit_price') else "")
                    + f" (broker={broker_id} → {engine_id}); _pending_stop armed"
                    + (" [bracket child]" if is_bracket_child else "")
                    + status_note
                )
                if is_bracket_child:
                    bracket_child_found_broker_id = broker_id
                    # Clear _bracket_child since the child is now the
                    # active protective stop (promoted to _pending_stop).
                    # Saved-state _bracket_child was the "pre-parent-fill"
                    # marker; we're past that point if we have a position.
                    if self._position_open:
                        self._bracket_child = None
            else:
                self._log(
                    f"Reconcile: re-attached {action} {otype_norm} "
                    f"(broker={broker_id} → {engine_id}) but state={self._state.value} "
                    f"doesn't match this side — order tracked but won't drive engine. "
                    f"Cancel at IBKR if stale."
                )

        # ── A54 (2026-06-10): adopt bracket CHILD when we just adopted
        # the bracket PARENT in MONITORING/WAITING_REENTRY state.
        #
        # The per-order state_matches gating above adopts the parent
        # BUY (state matches MONITORING/WAITING_REENTRY) but skips the
        # child SELL STP (state needs IN_POSITION/EXIT_POSITION for
        # that side to match). Without explicit bracket-child adoption,
        # the invariant sweep sees a SELL STP at broker that's NOT in
        # its tracked set (`legitimate_sell_ids` only reads
        # `_pending_stop[side==SELL]` and `_bracket_child.order_id`),
        # marks it ORPHAN, and cancels it within 250ms. A57 then
        # cascades the cancel to the parent, the engine places a
        # fresh bracket with a new cycle_seq, and the user sees the
        # adopted bracket disappear.
        #
        # The fix: when we adopted a BUY parent (engine_id starts with
        # ENTRY_BUY_) into _pending_stop, scan open_orders for the
        # matching SELL STP child (parentId == parent's broker_id) and
        # populate _bracket_child with the same shape the placement
        # path uses. The sweep then recognises both legs and lets the
        # bracket continue cycling.
        # SHORT INVERSION (P9): the bracket parent is a SELL (ENTRY_SELL) and
        # the child is a BUY cover STP. Adopt the BUY child alongside it.
        ps = getattr(self, '_pending_stop', None)
        if (
            ps
            and ps.get('side') == OrderSide.SELL
            and isinstance(ps.get('order_id'), str)
            and ps['order_id'].startswith('ENTRY_SELL_')
            and not getattr(self, '_bracket_child', None)
        ):
            # Resolve the parent's broker_id via _order_id_map (inverse lookup).
            parent_engine_id = ps['order_id']
            parent_broker_id = None
            for bid, eid in getattr(self.gateway, '_order_id_map', {}).items():
                if eid == parent_engine_id:
                    parent_broker_id = bid
                    break
            if parent_broker_id is not None:
                expected_qty = int(ps.get('qty') or 0)
                for o in open_orders:
                    if (o.get('action') == 'BUY'
                            and (o.get('order_type') or '').replace(' ', '').upper() == 'STP'
                            and str(o.get('parent_id') or '') == str(parent_broker_id)
                            and int(o.get('qty') or 0) == expected_qty):
                        child_broker_id = o['broker_id']
                        # Recover the child's engine_id — preferring the
                        # broker's orderRef (carries the EXACT _n{seq}_s{sid})
                        # so the sweep's set-membership compares like-for-like.
                        child_engine_id = (
                            o.get('order_ref')
                            or self._make_engine_id(
                                f"BR_{OrderSide.BUY.value}", expected_qty
                            )
                        )
                        # Mirror the shape used by place_bracket_sell_stop_market.
                        self._bracket_child = {
                            'order_id': child_engine_id,
                            'qty': expected_qty,
                            'stop_price': o.get('stop_price') or 0.0,
                            'side': OrderSide.BUY,
                            'order_type': 'STP',
                            'parent_order_id': parent_engine_id,
                        }
                        self._log(
                            f"Reconcile: re-attached bracket CHILD "
                            f"(broker={child_broker_id} → {child_engine_id}) "
                            f"alongside its parent — both legs now in tracked set"
                        )
                        # Also register the order in the gateway so fill
                        # events for the child route correctly.
                        try:
                            self.gateway.register_existing_order(
                                str(child_broker_id), child_engine_id,
                                o.get('_trade'),
                            )
                        except Exception as e:
                            self._log(
                                f"Reconcile: child register_existing_order "
                                f"failed (non-fatal): {e}"
                            )
                        break

        # ── Half-filled bracket recovery ──────────────────────────────
        # If we restored a position (entry_price set) AND the bracket
        # child landed in _pending_stop above, verify the child's stop
        # reflects the actual fill VWAP — not the initial estimate the
        # bracket was submitted with. The two diverge if the engine
        # crashed between parent fill and the modify_stop_trigger call.
        # `modify_stop_trigger` is idempotent (no-op on equal values), so
        # this is safe to always call on bracket-child adoption.
        if (
            bracket_child_found_broker_id is not None
            and self._position_open
            and self._entry_price
            and self._entry_price > 0
        ):
            ps = self._pending_stop or {}
            current_stop = ps.get('stop_price') or 0.0
            current_qty = ps.get('qty') or 0
            # ── RESTART-SAFETY (SHORT): recover the frozen pct from the
            #    resting child BEFORE computing expected_stop ─────────────
            # On a broker-only recovery (parent filled during downtime, disk
            # state lost), reconcile recovers position_open/entry for the
            # PARENT from the broker — but the CHILD's frozen SL pct
            # (_active_stop_pct) has no other source and is still None here.
            # _effective_stop_pct() would then fall back to
            # config.stop_loss_pct (e.g. 1%), making expected_stop the 1%
            # level — which DIFFERS from the operator's real resting child,
            # so the block below would ACTIVELY re-modify the good stop to
            # the wrong one. Back the pct out of the resting child instead.
            # SHORT INVERSION: child stop is ABOVE entry (entry × (1 + pct)),
            # so pct = current_stop/entry − 1 (mirror of long 1 − stop/entry).
            if self._active_stop_pct is None and current_stop > 0:
                self._active_stop_pct = max(
                    0.0, round((current_stop / self._entry_price) - 1.0, 6),
                )
                self._log(
                    f"Reconcile: recovered frozen SL pct (SHORT) "
                    f"{self._active_stop_pct * 100:.4f}% from resting child stop "
                    f"${current_stop:.5f} / entry ${self._entry_price:.5f} "
                    f"(NOT config default {self.config.stop_loss_pct * 100:.2f}%)"
                )
            # Spec-aware: was round(..., 2). On FX the legacy code
            # produced expected_stop=1.16, then the >0.005 epsilon
            # below ALWAYS fired (broker stop ≠ 1.16), causing infinite
            # modify-on-restart loops with WRONG values.
            # Exact integer-tick math — see _protective_stop_price.
            expected_stop = self._protective_stop_price(
                self._entry_price, self._effective_stop_pct()
            )
            # Per-asset threshold — equity 0.5¢, FX half-pip.
            need_modify_stop = abs(current_stop - expected_stop) > self._price_epsilon()
            need_modify_qty = (
                self._quantity and self._quantity > 0
                and current_qty != self._quantity
            )
            if need_modify_stop or need_modify_qty:
                self._log(
                    f"[BRACKET RECOVERY] Bracket child stop=${current_stop:.2f} qty={current_qty} "
                    f"doesn't match recovered position (entry=${self._entry_price:.2f} → "
                    f"expected stop ${expected_stop:.2f}; qty {self._quantity}). "
                    f"Likely engine crashed between parent fill and child modify. "
                    f"Re-modifying child now."
                )
                try:
                    target_qty = self._quantity if need_modify_qty else None
                    ok = await self.gateway.modify_stop_trigger(
                        ps.get('order_id'),
                        new_stop_price=expected_stop,
                        new_qty=target_qty,
                    )
                    if ok:
                        ps['stop_price'] = expected_stop
                        if target_qty is not None:
                            ps['qty'] = target_qty
                        if self._audit:
                            self._audit.log_order(
                                event="CHILD_STOP_MODIFIED",
                                order_id=ps.get('order_id'),
                                side="BUY",
                                qty=target_qty or current_qty,
                                order_type="STP",
                                stop_price=expected_stop,
                                signal_price=expected_stop,
                                reason=(
                                    f"reconcile-time bracket recovery — child stale "
                                    f"({current_stop:.2f} → {expected_stop:.2f})"
                                ),
                                state_at_time=self._state.value,
                                position_at_time="SHORT",
                            )
                    else:
                        self._log(
                            f"[BRACKET RECOVERY] modify_stop_trigger returned False — "
                            f"the child may already have fired. Health check will catch "
                            f"the resulting naked state if so."
                        )
                except Exception as e:
                    self._log(f"[BRACKET RECOVERY] modify failed: {type(e).__name__}: {e}")

        # If we restored `_bracket_child` from state but DIDN'T find it
        # at the broker, the bracket was cancelled or fired between save
        # and now. Clear the in-memory marker so the engine doesn't keep
        # thinking it's protected via the bracket.
        #
        # A64 (2026-06-10): the detection variable is now
        # `bracket_child_seen_at_broker` (set whenever ANY SELL STP with
        # the saved engine_id was found in open_orders), NOT
        # `bracket_child_found_broker_id` (which only got set when the
        # SELL STP's state_matches=True, i.e. only in IN_POSITION). The
        # old gating caused this defensive cancel to misfire in
        # WAITING_REENTRY/MONITORING: the SELL STP was physically at the
        # broker but state_matches=False kept the variable None, so this
        # block ran and cancelled the live bracket child, A57 cascaded
        # to the parent, and the engine then re-placed a fresh bracket
        # with a new cycle_seq — the "every reconcile cancels the
        # bracket" pattern user reported 2026-06-10.
        if (
            self._bracket_child is not None
            and not bracket_child_seen_at_broker
            and not self.gateway.paper
        ):
            child_id = self._bracket_child.get('order_id', '')
            self._log(
                f"[BRACKET RECOVERY] Saved bracket_child {child_id} not "
                f"present in current openTrades — attempting explicit cancel "
                f"by id as belt-and-suspenders (in case IBKR's "
                f"reqAllOpenOrders cache is briefly stale), then clearing "
                f"in-memory marker."
            )
            # A45 belt-and-suspenders: even though reconcile didn't see
            # the child at broker, try to cancel it explicitly. If the
            # child is genuinely gone, cancel returns "not found" — fine.
            # If it's actually alive but the openOrders snapshot was
            # stale, cancel saves us from an orphan SELL firing later.
            try:
                if hasattr(self.gateway, 'cancel_order') and child_id:
                    result = self.gateway.cancel_order(child_id)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
            except Exception as e:
                self._log(
                    f"[BRACKET RECOVERY] explicit cancel of {child_id} "
                    f"raised ({type(e).__name__}: {e}) — proceeding."
                )
            self._bracket_child = None

        # ── Broker-truth overlay on dashboard's hydrated order history ──
        # Historical audit CSVs written by pre-2026-06-05 code stored
        # prices truncated to 2dp (1.16415 → "1.16"). When the engine
        # hydrates `_order_history` from those rows on restart, the
        # dashboard ORDERS panel inherits the truncated values — even
        # though IBKR has the true price on the still-resting order.
        #
        # Fix: for each currently-resting broker order, find the
        # matching `_order_history` entry by engine_id (via orderRef)
        # and refresh its `stop_price` / `limit_price` from the broker's
        # value. This corrects the dashboard display without modifying
        # the historical CSV (audit-as-record-of-truth is preserved;
        # display reflects current reality).
        if open_orders and self._order_history:
            by_engine_id: dict = {}
            for bo in open_orders:
                eid = bo.get('order_ref') or ''
                if eid:
                    by_engine_id[eid] = bo
            refreshed = 0
            for rec in self._order_history:
                bo = by_engine_id.get(rec.order_id)
                if bo is None:
                    continue
                changed = False
                bro_stop = bo.get('stop_price')
                if bro_stop and rec.stop_price != float(bro_stop):
                    rec.stop_price = float(bro_stop)
                    changed = True
                bro_lmt = bo.get('limit_price')
                if bro_lmt and rec.limit_price != float(bro_lmt):
                    rec.limit_price = float(bro_lmt)
                    changed = True
                if changed:
                    refreshed += 1
            if refreshed:
                self._log(
                    f"[HYDRATION OVERLAY] refreshed {refreshed} order_history "
                    f"price(s) from broker-truth — corrects dashboard display "
                    f"for historical rows truncated by old :.2f audit writer."
                )

    # ─── Reconcile helpers: position drift + missed-fill replay ───
    #
    # Both run from the top of `_reconcile_open_orders`. They close the
    # "fill happened while we were disconnected" race window — without
    # them, a fill during the ~10-60s gap between IBKR disconnect and our
    # supervisor's reconnect would silently slip past the engine's
    # `fillEvent` handler (which is bound to the OLD ib_async IB instance,
    # destroyed in the reconnect cycle). With them, we walk IB.fills() on
    # every reconnect and apply anything we don't already have an exec_id
    # for, then verify broker position matches engine belief.
    #
    # Paper mode: both are no-ops via gateway.paper short-circuits in the
    # underlying `get_all_fills` and `get_positions` calls.

    async def _gap_fill_highest_price(self) -> None:
        """Recover peaks reached during a disconnect by fetching historical bars.

        `_highest_price` is persisted to disk on every new high during normal
        operation (`_track_high` → `_save_state`). But ticks during a
        disconnect are invisible to the engine — a price spike that happens
        between our last state write and the moment we reconnect is lost.
        That matters because on SELL fill we set
        `_previous_breakout_level = _highest_price`, which becomes the next
        re-entry trigger. If we underestimate the cycle peak by even a few
        cents, the re-entry fires prematurely below the real breakout level.

        Fix: on every reconnect, while a position is open, ask IBKR for the
        intraday bars covering the disconnect window. Take max(high) across
        those bars. If it's higher than our stored value, update.

        Bar resolution adapts to the gap so we don't pay IBKR rate-limit
        cost for trivial reconnects:
            < 5  min gap → 5-sec bars  (max precision, intra-tick peaks)
            < 1  hour    → 30-sec bars (cheaper, still catches the move)
            < 1  day     → 1-min bars  (long outages)
            otherwise    → skip; the cycle is probably stale anyway

        Side effects:
            * `_highest_price` is bumped if a higher peak was found.
            * State persisted via `_save_state` so the new value survives
              the rest of the reconcile sequence.

        No-ops:
            * paper mode (no IBKR historical data — no gap to fill).
            * `_state` is not IN_POSITION (no active cycle).
            * no `_entry_price` (nothing to anchor the window to).
            * gap < 5 seconds (likely just a normal heartbeat-restart).
            * IBKR request fails (best-effort — log + continue).
        """
        if self.gateway.paper:
            return
        if self._state != TradeState.IN_POSITION:
            return
        if not self._entry_price:
            return

        # Compute the window we missed.
        saved = self.state_store.load() if self.state_store else None
        last_saved_iso = (saved or {}).get('updated_at', '') if saved else ''
        if not last_saved_iso:
            return
        try:
            last_saved = datetime.fromisoformat(last_saved_iso)
        except (TypeError, ValueError):
            return

        now = self._ts()
        # Both naive — last_saved came from self._ts().isoformat() at save
        # time, now is self._ts() at reconnect time. Subtraction is safe.
        try:
            gap_seconds = (now - last_saved).total_seconds()
        except Exception:
            return
        if gap_seconds < 5:
            return  # nothing meaningful could have happened

        # Pick bar size based on gap duration.
        if gap_seconds < 300:
            bar_size = '5 secs'
            duration = f"{max(60, int(gap_seconds) + 60)} S"
        elif gap_seconds < 3600:
            bar_size = '30 secs'
            duration = f"{int(gap_seconds) + 60} S"
        elif gap_seconds < 86400:
            bar_size = '1 min'
            duration = f"{int(gap_seconds) + 60} S"
        else:
            self._log(
                f"[GAP-FILL] skipping — disconnect > 1 day ({gap_seconds:.0f}s); "
                f"this cycle likely needs manual review"
            )
            return

        # Fetch bars. reqHistoricalDataAsync is the non-blocking variant.
        try:
            contract = await self.gateway._get_contract()
            bars = await self.gateway._ib.reqHistoricalDataAsync(
                contract,
                endDateTime='',         # ends "now"
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow='TRADES',    # match _highest_price source (LTP)
                useRTH=False,           # include pre/post market
                formatDate=2,           # epoch timestamps (we don't use the date here anyway)
            )
        except Exception as e:
            self._log(f"[GAP-FILL] reqHistoricalDataAsync failed (best effort): {e}")
            return

        if not bars:
            return

        # SHORT INVERSION (P4): MIN low across the window (long took max high).
        # Filter out zeros (incomplete bars).
        try:
            gap_high = min(b.low for b in bars if getattr(b, 'low', 0) > 0)
        except ValueError:
            return  # empty after filter

        prior = self._highest_price if self._highest_price is not None else float('inf')
        if gap_high < prior:
            self._log(
                f"[GAP-FILL] _highest_price (trough) ${prior:.2f} → ${gap_high:.2f} "
                f"(scanned {len(bars)} {bar_size} bars across {gap_seconds:.0f}s "
                f"disconnect; protected re-entry breakdown level from "
                f"premature trigger)"
            )
            self._highest_price = gap_high
            # Persist now so the eventual cover (BUY) replay sees the bumped
            # value even if reconcile spans multiple awaits.
            self._save_state()

            # Surface as informational alert so the operator sees a gap-fill
            # happened. Not CRITICAL — this is the system working correctly,
            # not a fault.
            if self._alerts:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="CUSTOM_GAP_FILL_HIGH",
                    severity=AlertSeverity.LOW,
                    message=(
                        f"_highest_price (trough) bumped from ${prior:.2f} to ${gap_high:.2f} "
                        f"during reconnect (disconnect was {gap_seconds:.0f}s); "
                        f"re-entry breakdown level is now accurate to the real trough."
                    ),
                    context={
                        "ticker": self.config.ticker,
                        "prior_high": prior,
                        "new_high": gap_high,
                        "gap_seconds": gap_seconds,
                        "bar_size": bar_size,
                        "n_bars": len(bars),
                    },
                    correlation_id=getattr(self, '_cycle_id', ''),
                )

    async def _gap_fill_peak_window(self, start_time, end_time) -> Optional[float]:
        """Recover the true intra-cycle peak between two specific fill timestamps.

        Used when BOTH the BUY entry AND the SELL exit happened during a
        disconnect: `_reconcile_missed_fills` replays the BUY first (which
        sets `_highest_price = avg_fill_price`), but without LTP tracking
        during the offline window we have no idea what peak the cycle
        actually reached. If we replay the SELL now,
        `_previous_breakout_level` locks in to ~entry_price — far below the
        real peak — and the next re-entry fires prematurely.

        Fix: download historical bars across EXACTLY the inter-fill window
        [start_time → end_time] and bump `_highest_price` to max(high)
        BEFORE the SELL replay runs. The SELL replay then computes the
        correct `_previous_breakout_level`.

        Distinct from `_gap_fill_highest_price`, which anchors at "now"
        and requires the engine to already be IN_POSITION. This helper
        accepts explicit endpoints so it can be called inside the
        reconcile loop between two replayed fills.

        Returns the new gap_high if `_highest_price` was bumped, else None.

        No-ops:
            * paper mode (no IBKR historical data — no broker to ask).
            * either timestamp is None.
            * window <= 0 seconds.
            * IBKR request fails (best-effort — log + continue).
        """
        if self.gateway.paper:
            return None
        if start_time is None or end_time is None:
            return None

        # Normalize both endpoints to tz-aware UTC. ib_async fill times are
        # tz-aware; saved state times are naive. Mixed-tz subtraction blows
        # up here just as it does downstream, so coerce once at the source.
        try:
            from datetime import timezone as _tz
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=_tz.utc)
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=_tz.utc)
            gap_seconds = (end_time - start_time).total_seconds()
        except Exception:
            return None
        if gap_seconds <= 0:
            return None

        # Pick bar size based on window duration — mirrors the policy in
        # `_gap_fill_highest_price`. Sub-5min uses 5-sec bars for
        # max precision on tight cycles; longer windows step down.
        if gap_seconds < 300:
            bar_size = '5 secs'
            duration = f"{max(60, int(gap_seconds) + 60)} S"
        elif gap_seconds < 3600:
            bar_size = '30 secs'
            duration = f"{int(gap_seconds) + 60} S"
        elif gap_seconds < 86400:
            bar_size = '1 min'
            duration = f"{int(gap_seconds) + 60} S"
        else:
            self._log(
                f"[RECONCILE GAP-FILL] skipping — inter-fill window > 1 day "
                f"({gap_seconds:.0f}s); cycle is stale, recommend manual review"
            )
            return None

        # IBKR's reqHistoricalData accepts a string formatted as
        # "YYYYMMDD HH:MM:SS UTC" for endDateTime, or empty for "now".
        # We explicitly anchor at the SELL fill time so the window doesn't
        # leak past the real cycle end and pick up post-exit volatility.
        try:
            contract = await self.gateway._get_contract()
            end_str = end_time.astimezone(_tz.utc).strftime('%Y%m%d %H:%M:%S UTC')
            bars = await self.gateway._ib.reqHistoricalDataAsync(
                contract,
                endDateTime=end_str,
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow='TRADES',    # match _highest_price source (LTP/trade)
                useRTH=False,           # include pre/post market
                formatDate=2,           # epoch (irrelevant — we only read .high)
            )
        except Exception as e:
            self._log(f"[RECONCILE GAP-FILL] reqHistoricalDataAsync failed (best effort): {e}")
            return None

        if not bars:
            return None

        # SHORT INVERSION (P4): inter-fill TROUGH = min(low) (long took max high).
        try:
            gap_high = min(b.low for b in bars if getattr(b, 'low', 0) > 0)
        except ValueError:
            return None  # all bars had low == 0

        prior = self._highest_price if self._highest_price is not None else float('inf')
        if gap_high < prior:
            self._log(
                f"[RECONCILE GAP-FILL] inter-fill trough ${prior:.2f} → ${gap_high:.2f} "
                f"(scanned {len(bars)} {bar_size} bars across {gap_seconds:.0f}s "
                f"between SELL entry and BUY cover fills; re-entry breakdown level "
                f"corrected to the real intra-cycle low)"
            )
            self._highest_price = gap_high
            # Persist immediately so the cover (BUY) replay (next call in the
            # reconcile loop) reads the bumped value when it computes
            # `_previous_breakout_level = self._highest_price`.
            self._save_state()

            if self._alerts:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="CUSTOM_GAP_FILL_HIGH",
                    severity=AlertSeverity.LOW,
                    message=(
                        f"Inter-fill trough recovered: ${prior:.2f} → ${gap_high:.2f}. "
                        f"Both SELL entry and BUY cover filled offline; re-entry breakdown "
                        f"level is now anchored to the real intra-cycle low (was about to "
                        f"lock in to entry price → would have triggered premature re-entry)."
                    ),
                    context={
                        "ticker": self.config.ticker,
                        "prior_high": prior,
                        "new_high": gap_high,
                        "window_seconds": gap_seconds,
                        "bar_size": bar_size,
                        "n_bars": len(bars),
                    },
                    correlation_id=getattr(self, '_cycle_id', ''),
                )
            return gap_high
        return None

    async def _recompute_realized_pnl_from_broker(self) -> None:
        """Recompute today's realized PnL from IBKR's authoritative
        execution history with TRUE commission reports.

        WHY: state file PnL is a running accumulator updated by
        whatever commission path was in use when each cycle closed.
        Pre-fix sessions used the modeled equity formula (45x too
        high on EURUSD = $92 instead of $2 per round-trip), and those
        wrong numbers persist across restarts in the state file.
        State-file truth ≠ broker truth.

        SOLUTION: walk today's executions from IBKR, pair them BUY→SELL
        FIFO into round-trips, and sum:
            trip_pnl = (sell_price - buy_price) × shares
                       - (buy_commission_pro_rata + sell_commission_pro_rata)
        Adopt the result. Audit `PNL_RECOMPUTED` with old/new so the
        operator can see the correction.

        Paper mode: skip — paper PnL is purely simulated, state file
        is the source of truth.

        Idempotent: identical input → identical output. Re-running
        produces the same number; cheap (single get_all_fills RPC,
        in-memory walk).
        """
        if self.gateway.paper:
            return
        try:
            all_fills = self.gateway.get_all_fills()
        except Exception as e:
            self._log(f"[PNL-RECOMPUTE] get_all_fills failed (keeping state value): {e}")
            return

        # Filter to OUR symbol via logical-ticker translation (FX needs
        # this; bare `fill.contract.symbol` is "EUR" for Forex("EURUSD")).
        try:
            from src.execution.broker import _logical_symbol_from_contract as _logical_sym
        except Exception:
            _logical_sym = lambda c: getattr(c, 'symbol', '')

        ours = []
        for f in all_fills:
            try:
                if _logical_sym(f.contract) != self.config.ticker:
                    continue
                ours.append(f)
            except Exception:
                continue
        if not ours:
            # No executions today on this symbol → realized PnL is 0
            # regardless of what the state file said.
            if abs(self._pnl) > 0.005:
                self._log(
                    f"[PNL-RECOMPUTE] no broker executions today on "
                    f"{self.config.ticker}; resetting stale state-file "
                    f"PnL ${self._pnl:.2f} → $0.00"
                )
                self._pnl = 0.0
                self._save_state()
            return

        # Sort chronologically for FIFO matching.
        try:
            ours.sort(key=lambda f: getattr(f.execution, 'time', None) or 0)
        except Exception:
            pass

        # SHORT INVERSION (P11): FIFO inventory of open SHORT ENTRIES (SLD).
        # Each COVER (BOT) matches the earliest open short lot. This mirrors
        # the live exit PnL (entry_sell − cover_buy). The OLD long form built
        # a BUY inventory and matched SELLs against it; for a short the SELL
        # entry arrives first, finds an empty inventory, and was dumped into
        # the "unmatched SELL = SHORT not supported, skip" branch → realized
        # recomputed to $0 and CLOBBERED the authoritative state-file PnL on
        # every restart (which then feeds the daily-loss circuit breaker).
        from collections import deque
        inventory: deque = deque()  # each: {'qty','price','comm'} — open SHORT entries
        realized = 0.0
        matched_trips = 0

        for f in ours:
            try:
                exec_obj = getattr(f, 'execution', None)
                if exec_obj is None:
                    continue
                # IBKR's execution.side is 'BOT' / 'SLD'
                ib_side = getattr(exec_obj, 'side', '')
                shares = float(getattr(exec_obj, 'shares', 0) or 0)
                price = float(getattr(exec_obj, 'price', 0) or 0)
                if shares <= 0 or price <= 0:
                    continue
                # True broker commission for this execution.
                cr = getattr(f, 'commissionReport', None)
                comm = 0.0
                if cr is not None:
                    val = getattr(cr, 'commission', None)
                    if val is not None and val > 0:
                        comm = float(val)
            except Exception:
                continue

            if ib_side == 'SLD':
                # Short ENTRY — open a short lot.
                inventory.append({'qty': shares, 'price': price, 'comm': comm})
            elif ib_side == 'BOT':
                # COVER — match against the earliest open short entry.
                remaining = shares
                cover_comm_per_share = (comm / shares) if shares > 0 else 0.0
                while remaining > 0 and inventory:
                    lot = inventory[0]
                    used = min(remaining, lot['qty'])
                    entry_comm_per_share = (
                        (lot['comm'] / lot['qty']) if lot['qty'] > 0 else 0.0
                    )
                    # Short round-trip PnL: (entry_sell − cover_buy) * units
                    #                       − proportional commissions.
                    trip = (lot['price'] - price) * used
                    # Normalize quote-ccy gross → USD for non-USD-quoted FX
                    # (USDJPY→JPY, crosses) BEFORE subtracting USD commissions,
                    # so the restart-recomputed P&L matches the live path.
                    # Equity / USD-quoted FX pass through unchanged.
                    try:
                        from src.strategy.risk import fx_quote_pnl_to_usd as _pnl_usd
                        trip = _pnl_usd(self.config.ticker, trip, price, used)
                    except Exception:
                        pass
                    trip -= entry_comm_per_share * used
                    trip -= cover_comm_per_share * used
                    realized += trip
                    matched_trips += 1
                    lot['qty'] -= used
                    lot['comm'] -= entry_comm_per_share * used
                    remaining -= used
                    if lot['qty'] <= 1e-9:
                        inventory.popleft()
                # If the COVER has unmatched qty (no open short lot), the
                # position was opened outside today's history (a short carried
                # from yesterday or a manual TWS trade). Log and skip the
                # remainder; the position-reconcile path catches the divergence.
                if remaining > 0:
                    self._log(
                        f"[PNL-RECOMPUTE] BUY (cover) execution has {remaining} "
                        f"units with no matching short ENTRY in today's history "
                        f"— likely a short carried from yesterday or a manual "
                        f"TWS trade. Skipping for PnL math."
                    )

        # Refresh `_pending_buy_commission` (the open position's ENTRY-side
        # commission — kept under this name across the long→short port) from
        # broker truth if a position is currently open. Without this, the next
        # COVER fill would close the round-trip using whatever stale value the
        # state file had — written by the OLD modeled-estimate code path ($92
        # instead of $2 for EURUSD). Inventory's remaining SHORT-entry lot(s)
        # carry the true broker commissions accumulated above.  # SHORT INVERSION (P11)
        if self._position_open and inventory:
            true_pending = sum(lot['comm'] for lot in inventory)
            stale_pending = self._pending_buy_commission
            if abs(true_pending - stale_pending) > 0.005:
                self._log(
                    f"[PNL-RECOMPUTE] open position BUY-side commission: "
                    f"state file ${stale_pending:.2f} → broker truth "
                    f"${true_pending:.2f} (refreshing so SELL closes "
                    f"the round-trip with the correct number)"
                )
                self._pending_buy_commission = float(true_pending)

        # Adopt broker truth.
        prev = self._pnl
        if abs(realized - prev) > 0.005:
            self._log(
                f"[PNL-RECOMPUTE] state file says ${prev:+.2f}, broker "
                f"truth says ${realized:+.2f} ({matched_trips} round-trip"
                f"{'s' if matched_trips != 1 else ''} matched). "
                f"Adopting broker value — your dashboard PnL was off by "
                f"${realized - prev:+.2f}."
            )
            self._pnl = float(realized)
            try:
                self._save_state()
            except Exception:
                pass
            if self._audit:
                try:
                    self._audit.log_state(
                        event="PNL_RECOMPUTED",
                        state=self._state,
                        position_open=self._position_open,
                        entry_price=self._entry_price,
                        pnl=realized,
                        old_pnl=prev,
                        delta=realized - prev,
                        matched_trips=matched_trips,
                    )
                except Exception:
                    pass
        else:
            self._log(
                f"[PNL-RECOMPUTE] state file ${prev:+.2f} matches broker "
                f"truth ({matched_trips} round-trip"
                f"{'s' if matched_trips != 1 else ''}); no change."
            )

    async def _reconcile_missed_fills(self) -> None:
        """Walk broker.get_all_fills() and replay any exec_id we haven't seen.

        OrderRegistry.on_fill dedups by exec_id — calling _on_gateway_fill
        with a fill we've already applied is a safe no-op. So this is
        idempotent: on a "clean" reconnect with no missed activity it
        finds nothing new and exits in <1ms.

        For each genuinely-new fill:
          1. Reconstruct the engine_id from side+qty+symbol using the same
             pattern `_place_entry_stop_limit` / `_place_protective_stop`
             would have used. If the engine_id isn't in the registry yet
             (which is the normal case for missed fills — we were offline
             when our placer ran, or this is a fresh start), submit a stub
             OrderRecord first so the registry has something to mark.
          2. Call `_on_gateway_fill` with the execution details. The engine
             state machine reacts as if the fill just arrived: BUY sets
             _position_open + _entry_price + arms protective SL; SELL
             closes position + records P&L + transitions to WAITING_REENTRY.

        Caveats:
          * exec_ids are persisted only in the live registry (not on disk).
            After a clean restart where the state file already reflects the
            position, replaying fills could double-count P&L. Guard: we
            check fill.execution.time against state.updated_at and only
            apply fills strictly newer than the last persisted state save.
          * MARKET orders from force_exit also produce fills here. They use
            a different engine_id pattern (FORCE_SELL_*) which we can't
            easily reconstruct without the cycle_id. Best-effort: still
            try the SL_SELL pattern; if that engine_id doesn't exist and
            we'd be creating a stub, that's still fine — registry tracks
            it, P&L gets booked.
        """
        if self.gateway.paper:
            return
        try:
            all_fills = self.gateway.get_all_fills()
        except Exception as e:
            self._log(f"[RECONCILE] get_all_fills failed: {e}")
            return
        if not all_fills:
            return

        # Flat set of every exec_id the registry has already counted
        # (across all order_ids). Used for O(1) dedup check below.
        seen: set = set()
        for s in self.registry._seen_execs.values():
            seen.update(s)

        # Lower bound for "new" fills. Two regimes:
        #
        # (A) Resume from saved state: last_saved_ts is the on-disk
        #     `updated_at` — the wall-clock moment we last persisted
        #     state, i.e. "we know we processed every fill up to here".
        #     Replay anything strictly newer.
        # (B) Fresh start (--reset / first run / cleared state file):
        #     last_saved_ts is None. WITHOUT a floor here, we'd walk every
        #     fill returned by ib.fills() for today and replay each one,
        #     populating _order_history with phantom BUY→SELL→BUY entries
        #     from old sessions. Use engine_started_at as the floor in
        #     this case ONLY — we only "missed" fills that occurred after
        #     we started.
        #
        # 2026-05-27 META postmortem: the previous logic used
        # `max(saved_ts, engine_started_at)`. After a RESTART (not a
        # within-process reconnect), engine_started_at > saved_ts, and
        # the max() then filtered out every fill that happened during
        # the offline gap — exactly the fills we needed to replay.
        # Specifically: META bracket child SELL filled at IBKR while
        # the user's shell was closed; restart's reconcile filtered
        # the fill out as "older than engine_started_at", engine kept
        # believing it was IN_POSITION, health-check armed a fresh SL
        # on a position that no longer existed. The fix: prefer
        # last_saved_ts unconditionally when present; engine_started_at
        # is only used as a fallback floor for fresh starts.
        last_saved_iso = (self.state_store.load() or {}).get('updated_at', '') if self.state_store else ''
        last_saved_ts = None
        if last_saved_iso:
            try:
                last_saved_ts = datetime.fromisoformat(last_saved_iso)
            except (TypeError, ValueError):
                last_saved_ts = None
        if last_saved_ts is not None:
            # Case (A) — resume: trust saved_ts as the "processed up to"
            # marker. Do NOT take the max with engine_started_at; that
            # was the bug.
            floor_ts = last_saved_ts
        else:
            # Case (B) — fresh start: anchor at engine_started_at so
            # we don't replay fills from earlier sessions today.
            floor_ts = self._engine_started_at

        # FL10 (2026-06-22) — absorb the COMPLETE broker execution history for
        # our clientId into the durable ledger, UNFLOORED. The ledger is the
        # authority and is provably perfect when the engine runs CONTINUOUSLY,
        # because every fill is recorded live and nothing is ever dropped. The
        # one and only failure mode is a fill that landed while the engine was
        # OFFLINE — and reqExecutions can always retrieve it (proven live), so
        # the ledger must simply absorb it. FL7's old `since=floor_ts` floor
        # defeated exactly that: a round-trip whose CLOSE landed after the floor
        # but whose OPEN landed before it kept the orphan close and dropped the
        # open → phantom short (AMD c217 2026-06-22: ledger net -10 vs broker
        # FLAT); the mirror case dropped a still-open BUY → disowned long
        # (AAPL/TSLA: broker +10 vs ledger flat). Removing the floor makes the
        # RESTART ledger byte-identical to the CONTINUOUS ledger — the invariant
        # we actually want.
        #
        # Why unfloored is SAFE — dedup, NOT the floor, is the double-count guard:
        #   • record() dedups by execId → re-merging fills we already hold is a
        #     no-op; this stays idempotent and re-runnable.
        #   • net() is itself UNFLOORED (sums every recorded fill), so a complete
        #     ledger's net IS the true position: closed round-trips self-cancel,
        #     an open leg remains. Writing BOTH legs of an old round-trip nets to
        #     zero — old fills cannot manufacture a phantom.
        # The 2026-06-12 GOOGL/EURUSD case the old floor guarded (our position
        # closed under a DIFFERENT clientId, e.g. an external flatten on cid 77)
        # is a per-cid-vs-account ambiguity that time-flooring cannot resolve
        # correctly anyway (it would also drop a legitimately-open leg). It is
        # caught downstream by the pre-flight SELL qty guard + POSITION_MISMATCH
        # alert — not by hiding the open fill from the ledger. Deduped by execId;
        # keyed by logical symbol; filtered to OUR client_id. A ledger fault must
        # never abort reconcile. NOTE: only the durable-ledger absorption is
        # unfloored — the state-machine replay below KEEPS floor_ts (its job is
        # the P&L double-count guard, which is unaffected by this change).
        _ledger = getattr(self, '_fill_ledger', None)
        if _ledger is not None:
            try:
                from src.execution.broker import _logical_symbol_from_contract as _lsc
                _our_cid = int(getattr(self.gateway, 'client_id', 0) or 0)
                # FL10.1 — unfloor ONLY on RESUME (state file present → recover
                # the offline fills, the whole point of FL10). On a FRESH/wiped
                # start (--reset / a deliberate ledger reset → last_saved_ts is
                # None) KEEP the engine_started floor so the rebuild starts truly
                # empty and cannot re-absorb a stale or corrupt pre-restart
                # execution window. This is the AMD c217 +20 loop: a wiped ledger
                # re-pulling an unbalanced broker window (closes aged out of the
                # 7-day reqExecutions window, opens remain) → constant phantom
                # long. Resume → since=None (unfloored). Fresh → engine_started.
                _merge_since = None if last_saved_ts is not None else floor_ts
                _n_new = _ledger.merge_broker_fills(
                    all_fills, symbol_of=_lsc, our_client_id=_our_cid,
                    since=_merge_since)
                if _n_new:
                    self._log(
                        f"[FILL-LEDGER] merged {_n_new} broker fill(s) "
                        f"(complete history, unfloored — FL10) into durable "
                        f"ledger (net "
                        f"{self.config.ticker}={_ledger.net(self.config.ticker)})")
            except Exception as e:
                self._log(f"[FILL-LEDGER] merge skipped (non-fatal): {e}")

        # Pre-filter + pre-sort fills chronologically. We need deterministic
        # BUY-before-SELL ordering so the inter-fill gap-fill (added below)
        # can detect "BUY just replayed, SELL is next" and correctly bump
        # _highest_price across the offline window. IBKR's `ib.fills()`
        # generally returns chronological order, but we don't rely on that —
        # an explicit sort by execution time is cheap and safe.
        from datetime import timezone as _tz
        candidates: list = []
        # Lazy import to avoid a top-level dependency cycle.
        from src.execution.broker import _logical_symbol_from_contract as _logical_sym

        # A52 — full scan diagnostics. Count every rejected fill with the
        # specific reason. After a TWS disconnect, if our missed BUY was in
        # all_fills but got filtered, the breakdown shows which gate killed
        # it. If it wasn't there at all, the totals will reveal it (e.g.
        # 0 fills returned by gateway = reqExecutionsAsync didn't backfill).
        total_fills = len(all_fills)
        rej_wrong_sym = 0
        rej_already_seen = 0
        rej_before_floor = 0
        my_sym_fills_seen: list = []  # list of (side, exec_id_short, time, reason_or_None)
        for fill in all_fills:
            try:
                # Logical-ticker match: bare `fill.contract.symbol` is "EUR"
                # for FX which would silently drop every FX fill replay on
                # restart, leaving the engine convinced no entry happened.
                if _logical_sym(fill.contract) != self.config.ticker:
                    rej_wrong_sym += 1
                    continue
                exec_id = fill.execution.execId
                fill_time = getattr(fill.execution, 'time', None) or getattr(fill, 'time', None)
                _side_short = 'BUY' if fill.execution.side == 'BOT' else 'SELL'
                if exec_id in seen:
                    rej_already_seen += 1
                    my_sym_fills_seen.append((_side_short, str(exec_id)[:14], fill_time, 'already_seen'))
                    continue
                if floor_ts is not None and fill_time is not None:
                    try:
                        if fill_time.tzinfo is not None and floor_ts.tzinfo is None:
                            cmp_floor = floor_ts.replace(tzinfo=_tz.utc)
                        else:
                            cmp_floor = floor_ts
                        if fill_time <= cmp_floor:
                            rej_before_floor += 1
                            my_sym_fills_seen.append((_side_short, str(exec_id)[:14], fill_time, f'before_floor({cmp_floor})'))
                            continue
                    except Exception:
                        pass
                my_sym_fills_seen.append((_side_short, str(exec_id)[:14], fill_time, 'CANDIDATE'))
                candidates.append((fill_time, fill))
            except Exception as e:
                self._log(f"[RECONCILE] error filtering fill: {e}")

        # A52 — dump the per-symbol scan summary so we can see EXACTLY
        # what was visible to the engine on this reconcile pass.
        # If the disconnect-window BUY is missing, this will show it.
        print(
            f"[BRACKET_LIFECYCLE] RECONCILE_SCAN  sym={self.config.ticker}  "
            f"total_fills={total_fills}  my_sym={len(my_sym_fills_seen)}  "
            f"candidates={len(candidates)}  rej_already_seen={rej_already_seen}  "
            f"rej_before_floor={rej_before_floor}  floor_ts={floor_ts}  "
            f"saved_ts={last_saved_ts}  engine_started={self._engine_started_at}"
        )
        for entry in my_sym_fills_seen:
            side, eid_short, ftime, reason = entry
            print(
                f"[BRACKET_LIFECYCLE]   SCAN_FILL  sym={self.config.ticker}  "
                f"side={side}  execId={eid_short}  time={ftime}  → {reason}"
            )
        # Sort chronologically; fills with no timestamp sink to the end so
        # they don't perturb a clean BUY→SELL ordering for timestamped ones.
        candidates.sort(
            key=lambda t: (
                t[0].astimezone(_tz.utc) if t[0] is not None and t[0].tzinfo is not None
                else (t[0].replace(tzinfo=_tz.utc) if t[0] is not None else datetime.max.replace(tzinfo=_tz.utc))
            )
        )

        replayed = 0
        # Track the most recently replayed BUY's execution time. When the
        # NEXT fill in the sorted sequence is a SELL for the same symbol,
        # call _gap_fill_peak_window(BUY_time → SELL_time) BEFORE the SELL
        # replay so _previous_breakout_level locks in to the true peak.
        last_buy_fill_time = None
        for fill_time, fill in candidates:
            try:
                exec_id = fill.execution.execId
                # Reconstruct engine_id from naming convention used by
                # _place_*_stop_limit. side='BOT' (bought) or 'SLD' (sold).
                side_str = 'BUY' if fill.execution.side == 'BOT' else 'SELL'
                qty = int(fill.execution.shares)
                price = float(fill.execution.price)

                # ── Inter-fill peak recovery ─────────────────────────────
                # Scenario: shell was killed while parent BUY was still
                # working; during the offline gap, the BUY filled AND the
                # bracket child SELL stop tripped. On restart we now have
                # two missed fills queued. Replaying the BUY first sets
                # _highest_price = avg_fill_price (no LTPs were tracked
                # during the gap, so this is the only signal we have).
                # If we replay the SELL immediately,
                # _previous_breakout_level = _highest_price = ~entry_price,
                # and the next re-entry order rests at ~entry rather than
                # at the real intra-cycle peak — premature re-entry risk.
                #
                # Fix: between the BUY replay and the SELL replay, download
                # historical bars across [BUY exec_time → SELL exec_time]
                # and bump _highest_price to max(high) found in the window.
                # _gap_fill_peak_window persists state, so the SELL replay
                # right after this reads the corrected high.
                if side_str == 'SELL' and last_buy_fill_time is not None and fill_time is not None:
                    try:
                        await self._gap_fill_peak_window(last_buy_fill_time, fill_time)
                    except Exception as e:
                        self._log(f"[RECONCILE] inter-fill gap-fill failed (best effort): {e}")
                    # One-shot per BUY: don't re-run for any subsequent
                    # SELL in the same cycle (partial fills of the same
                    # child SELL share the same engine_id; we only need
                    # one history fetch per BUY→SELL pair).
                    last_buy_fill_time = None
                # ── Recover engine_id, preferring authoritative sources ─
                # Priority:
                #   1. `fill.order.orderRef` (or `fill.contract` / `fill.order`
                #      depending on ib_async version) — set by us at submit
                #      time, carries the exact `_n{cycle_seq}` suffix.
                #   2. `_order_id_map[execution.orderId]` — in-memory mapping
                #      maintained by broker.py; only valid for orders we
                #      placed in THIS process (lost across restarts unless
                #      `_reconcile_open_orders` already adopted them).
                #   3. Convention reconstruction with the CURRENT cycle_seq.
                #      Best-effort fallback for legacy/manual orders; may
                #      collide with the just-placed cycle's id, which is
                #      then caught downstream by the phantom-SELL guard.
                engine_id = ''
                # Try (1): orderRef attached to the order on the fill
                try:
                    fill_order = getattr(fill, 'order', None)
                    if fill_order is not None:
                        ref = getattr(fill_order, 'orderRef', '') or ''
                        if ref:
                            engine_id = ref
                except Exception:
                    pass
                # Try (2): broker.py's broker_id → engine_id map
                if not engine_id:
                    try:
                        broker_oid = str(getattr(fill.execution, 'orderId', '') or '')
                        if broker_oid and broker_oid in getattr(self.gateway, '_order_id_map', {}):
                            engine_id = self.gateway._order_id_map[broker_oid]
                    except Exception:
                        pass
                # Try (3): convention reconstruction with current cycle_seq.
                # SHORT INVERSION (P11): a replayed BUY is the COVER (SL_BUY), a
                # replayed SELL is the short ENTRY (ENTRY_SELL). Long mapped
                # BUY→ENTRY / SELL→SL; inverted so a downtime fill is attributed
                # to the correct leg/cycle on restart (else entry price + cycle
                # accounting are corrupted).
                if not engine_id:
                    if side_str == 'BUY':
                        engine_id = self._make_engine_id(f"SL_{OrderSide.BUY.value}", qty)
                    else:
                        engine_id = self._make_engine_id(f"ENTRY_{OrderSide.SELL.value}", qty)

                # Ensure registry has an OrderRecord to mark filled. If
                # we placed this order before disconnecting, the record
                # may already exist (rebuilt from open-orders reconcile
                # earlier in this same call). If not, submit a stub.
                #
                # IMPORTANT: strip tzinfo from fill_time before storing.
                # ib_async hands us tz-aware UTC datetimes, but the rest of
                # the system (`self._ts()`, `datetime.now()`) is naive.
                # Mixing them blows up downstream subtraction in the
                # dashboard (`filled_at - submitted_at` → TypeError on the
                # very first render frame, crashing the entire trading
                # loop). One canonical form everywhere — naive local time.
                if engine_id not in self.registry._orders:
                    if fill_time is not None and getattr(fill_time, 'tzinfo', None) is not None:
                        submitted_naive = fill_time.replace(tzinfo=None)
                    else:
                        submitted_naive = fill_time or self._ts()
                    self.registry.submit(OrderRecord(
                        order_id=engine_id,
                        symbol=self.config.ticker,
                        side=OrderSide(side_str),
                        qty=qty,
                        order_type=OrderType.STOP_LIMIT,
                        status=OrderStatus.SUBMITTED,
                        submitted_at=submitted_naive,
                        signal_price=price,  # signal lost; fill price is best guess
                    ))

                self._log(
                    f"[RECONCILE] Replaying missed fill: {side_str} {qty} @ ${price:.2f} "
                    f"(exec_id={exec_id[:14]}…)"
                )

                # Explicit audit marker so post-mortems can distinguish
                # replayed fills (broker-driven, engine missed in-session)
                # from live fills. Without this, both look identical in
                # the order.csv stream and an audit-vs-broker reconcile
                # can't tell when the engine fell behind.
                # (Live regression 2026-06-09: AUDUSD audit log had BUYs
                # without matching SELLs because the SELL fired during
                # downtime — no easy way to verify until we see "REPLAY".)
                if self._audit:
                    try:
                        self._audit.log_order(
                            event="BROKER_FILL_REPLAY",
                            order_id=engine_id,
                            side=side_str,
                            qty=qty,
                            fill_price=price,
                            reason=(
                                f"Fill seen via reqExecutionsAsync replay "
                                f"(engine missed it live; exec_id={exec_id[:14]})"
                            ),
                        )
                    except Exception:
                        # Best-effort: never let audit failure block
                        # the actual fill processing.
                        pass

                # Drive the engine through its normal fill processing.
                # This sets _position_open + arms SL after BUY, or closes
                # position + records P&L + sets breakout level after SELL.
                # Pass fill_time (ib_async tz-aware UTC) so the registry's
                # order.filled_at and the dashboard's history record reflect
                # the actual broker fill timestamp, not "now" (which would
                # often be seconds/minutes later when replayed on reconnect).
                self._on_gateway_fill(engine_id, qty, price, exec_id, fill_time=fill_time)
                replayed += 1

                # Stash this BUY's exec_time so the next SELL fill in the
                # sorted sequence can trigger the inter-fill peak recovery.
                # We only need the MOST RECENT BUY before a SELL — partial
                # BUY fills of the same parent share an order_id and add
                # cumulatively to _highest_price via _on_gateway_fill, so
                # the latest BUY exec_time is the right anchor for the
                # historical-bar window's start.
                if side_str == 'BUY':
                    last_buy_fill_time = fill_time
            except Exception as e:
                self._log(f"[RECONCILE] error replaying fill: {e}")

        if replayed:
            self._log(f"[RECONCILE] Replayed {replayed} missed fill(s) from broker history")
            if self._alerts:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="ORDER_FILL_TIMEOUT",
                    severity=AlertSeverity.HIGH,
                    message=(
                        f"Reconcile replayed {replayed} fill(s) missed during disconnect — "
                        f"engine state has been brought back in sync."
                    ),
                    context={
                        "ticker": self.config.ticker,
                        "fills_replayed": replayed,
                        "engine_state_now": self._state.value,
                        "position_open": self._position_open,
                    },
                    correlation_id=self._cycle_id,
                )
            self._save_state()  # persist updated position/P&L

    # ── REMOVED 2026-06-09 (A53): _fold_engine_to_flat + _cancel_orphan_after_fold
    #
    # The auto-flatten was wrong: when broker briefly reports 0 on a real
    # position (positions() lag, FX cash-ledger quirk, transient query),
    # folding the engine to FLAT cancelled the bracket child / protective
    # SL — stripping protection off a live position. Pre-flight SELL
    # refusal alone is the actual short-prevention guard; folding state
    # on top is redundant and risk-positive. Operator now reconciles
    # manually on any persistent POSITION_MISMATCH alert. The engine
    # never mutates _position_open / _bracket_child / _pending_stop in
    # response to a `broker_qty=0` reading. If the engine tries to arm
    # a SELL against a phantom position, pre-flight refuses it; no
    # shorts placed, no protective stops destroyed.

    async def _broker_qty_for_symbol(self) -> Optional[int]:
        """Return the broker's current SIGNED share count for `self.config.ticker`.

        SHORT INVERSION (P9): this is SIGNED — negative for a short position
        (ib_async `pos.position` is signed). Callers must handle negatives.

        Returns:
            int        — signed broker holding (negative = short, 0 = flat)
            None       — query failed (network glitch, disconnect). Caller
                         decides: pre-flight checks treat this as "allow
                         placement" so we don't block trading on a single
                         API glitch; reconcile loops treat it as no-op.

        Paper mode: returns the simulated qty if the symbol is tracked,
        else 0. Mirrors `get_positions()`'s paper-aware behavior.
        """
        try:
            positions = await self.gateway.get_positions()
        except Exception as e:
            self._log(f"[BROKER QTY] get_positions failed (treating as unknown): {e}")
            return None
        for p in positions:
            if p.symbol == self.config.ticker:
                try:
                    return int(p.quantity)
                except (TypeError, ValueError):
                    return None
        return 0

    async def _preflight_sell_allowed(
        self, qty_to_sell: int, *, context: str
    ) -> bool:
        """SHORT INVERSION (P9): pre-flight guard for the protective BUY COVER.

        NAMING NOTE: kept the original name `_preflight_sell_allowed` and the
        `qty_to_sell` arg (single call site) but the SEMANTICS are inverted.
        For the short strategy the protective leg is a BUY cover, and the
        danger is accidentally going LONG by covering MORE than we are short.

        Confirms the broker is actually SHORT at least `qty_to_sell` shares
        (signed broker_qty <= -qty_to_sell) before we submit a BUY cover.
        If the broker is short fewer than that (or flat / long), covering
        would flip us LONG — refuse, preserve engine state, fire CRITICAL.

        Returns:
            True  — safe to proceed (broker is short >= qty_to_sell shares,
                    OR query failed and we're letting it through to avoid
                    blocking trading on a network glitch).
            False — refuse the BUY cover. Engine state PRESERVED.

        Paper mode: always returns True.
        """
        if self.gateway.paper:
            return True

        engine_qty = self._quantity if self._position_open else 0
        broker_qty = await self._broker_qty_for_symbol()  # signed: short < 0

        if broker_qty is None:
            # Couldn't query — log and allow. Don't block legitimate
            # trading on a single API hiccup.
            self._log(
                f"[PRE-FLIGHT/{context}] broker qty query failed — "
                f"allowing BUY cover (will recheck on next attempt)"
            )
            return True

        # SHORT: broker is short `short_qty` shares (positive magnitude).
        short_qty = -broker_qty if broker_qty < 0 else 0
        if short_qty >= qty_to_sell:
            # Broker is short enough — safe to cover this many.
            return True

        # ── FX CASH-LEDGER GUARD (2026-06-09) ──────────────────────
        # Spot FX positions live in the account currency cash ledger,
        # not in positions(). For FX, broker_qty from positions() is
        # almost always 0 — refusing the SELL on that basis would
        # block every legitimate FX exit. Treat 0 as "unknown" for
        # FX and allow the SELL through. The protective stop logic
        # upstream already established this position is real
        # (engine state says LONG with a known fill price).
        try:
            from src.assets.enum import AssetClass as _AC
            is_fx_cash = (
                getattr(self, '_asset_spec', None) is not None
                and getattr(self._asset_spec, 'asset_class', None)
                    is _AC.FX_CASH
            )
        except Exception:
            is_fx_cash = False
        if is_fx_cash and broker_qty == 0:
            # SHORT INVERSION (P11): positions() reads 0 for spot FX (cash-ledger
            # quirk), but we must NOT blindly allow a BUY cover — if the short was
            # already covered while we were down (phantom short), this buy goes
            # NAKED LONG (the mirror of the EURUSD -2500 naked short). Confirm
            # against the signed FillLedger net() (FL truth, survives restart):
            # allow ONLY if the ledger still shows a short >= qty_to_sell.
            _led_net = None
            try:
                _led = getattr(self.gateway, '_fill_ledger', None)
                if _led is not None and _led.count() > 0:
                    _led_net = int(_led.net(self.config.ticker))
            except Exception:
                _led_net = None
            _led_short = -_led_net if (_led_net is not None and _led_net < 0) else 0
            if _led_short >= qty_to_sell:
                self._log(
                    f"[PRE-FLIGHT/{context}] FX_CASH_LEDGER_GUARD: positions()=0 "
                    f"(cash-ledger quirk) but ledger net={_led_net} confirms SHORT "
                    f">= {qty_to_sell} — allowing BUY cover."
                )
                return True
            self._log(
                f"[PRE-FLIGHT/{context}] REFUSING BUY COVER {qty_to_sell} "
                f"{self.config.ticker}: positions()=0 AND ledger net={_led_net} "
                f"does NOT confirm a short >= qty. Covering could go NAKED LONG. "
                f"Engine state PRESERVED — operator reconciles."
            )
            return False

        # Broker is short fewer shares than we're about to cover (or is flat
        # / long). Covering would flip us LONG. Refuse, preserve engine state.
        would_go_long = qty_to_sell - short_qty
        self._log(
            f"[PRE-FLIGHT/{context}] REFUSING BUY COVER {qty_to_sell} {self.config.ticker} — "
            f"broker is short only {short_qty} shares (signed {broker_qty}; engine thought "
            f"{engine_qty}). Placing this order would go LONG {would_go_long} shares. "
            f"Engine state PRESERVED — operator reconciles manually."
        )
        if self._alerts:
            try:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="SHORTING_PREVENTED",
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Pre-flight blocked a BUY cover that would have gone LONG "
                        f"{would_go_long} {self.config.ticker} "
                        f"(context={context}, broker_qty={broker_qty}, "
                        f"engine_qty={engine_qty}, qty_to_cover={qty_to_sell}). "
                        f"Engine state preserved; manual reconcile required."
                    ),
                    context={
                        "ticker": self.config.ticker,
                        "context": context,
                        "broker_qty": broker_qty,
                        "engine_qty": engine_qty,
                        "qty_to_cover": qty_to_sell,
                    },
                    correlation_id=getattr(self, '_cycle_id', ''),
                )
            except Exception:
                pass
        if self._audit:
            try:
                self._audit.log_order(
                    event="SHORTING_PREVENTED",
                    order_id=f"PREFLIGHT_{context}",
                    side="BUY",
                    qty=qty_to_sell,
                    order_type="GUARD",
                    reason=(
                        f"Pre-flight refused BUY cover: broker={broker_qty}, "
                        f"engine={engine_qty}, qty_to_cover={qty_to_sell}, "
                        f"context={context}"
                    ),
                    state_at_time=self._state.value,
                    position_at_time="SHORT" if self._position_open else "FLAT",
                )
            except Exception:
                pass
        return False

    async def _reconcile_position_state(self) -> None:
        """Compare broker-side position quantity to engine's belief.

        After `_reconcile_missed_fills` has replayed any unseen fills,
        the engine's `_position_open` + `_quantity` should match the
        broker's actual position. If they still differ, something more
        complex happened (manual TWS trades by the user, partial fills
        we couldn't fully reconcile, etc.) — fire CRITICAL alert and
        let the operator investigate.

        Does NOT try to auto-fix the mismatch — that would require
        either flattening the broker position (dangerous) or fabricating
        engine state to match (corrupts P&L). Best behaviour is loud
        notification + leave the operator in control.
        """
        if self.gateway.paper:
            return

        # CRITICAL: refuse to reconcile while disconnected. Without this,
        # a disconnected get_positions used to silently return [] and
        # we'd fold engine to FLAT + re-enter while the broker was
        # actually still LONG and the protective stop was orphaned.
        # (Live regression 2026-06-06.) Defense in depth: we check
        # before calling AND get_positions raises if we forgot.
        if not getattr(self.gateway, 'connected', False):
            self._log(
                "[RECONCILE] skipped — gateway is DISCONNECTED. Engine "
                "state preserved; will retry on next health-check tick "
                "after reconnect."
            )
            return

        try:
            positions = await self.gateway.get_positions()
        except ConnectionError as e:
            # The gateway itself refused to answer (socket dead under
            # our `connected` flag). Same conclusion: don't touch engine
            # state on disconnected data.
            self._log(
                f"[RECONCILE] skipped — broker socket reported disconnect "
                f"mid-call: {e}. Engine state preserved."
            )
            return
        except Exception as e:
            self._log(f"[RECONCILE] get_positions failed: {e}")
            return

        broker_qty = 0
        broker_avg_cost = 0.0
        for p in positions:
            if p.symbol == self.config.ticker:
                broker_qty = int(p.quantity)
                broker_avg_cost = float(getattr(p, 'avg_cost', 0.0) or 0.0)
                break

        # ── TRUTH SOURCE TRIANGULATION (A43 → A42 → positions) ──────────
        # Three position-truth sources in order of reliability for THIS
        # client_id's position. Use the most reliable available; the
        # others remain as fallback chain.
        #
        # 1. A43 — EXECUTIONS FILTERED BY OUR CLIENT_ID
        #    Bulletproof: sum of fills our clientId actually executed.
        #    Works across restarts (reqExecutionsAsync re-fetches today's
        #    fills on connect). Uncontaminated by other bots on same
        #    account. Independent of FX cash-ledger quirks.
        #    THE primary truth source whenever available.
        #
        # 2. A42 — accountValues[base_ccy] for non-USD-base FX
        #    Fallback when executions cache is empty (e.g., older than
        #    24h, or reqExecutions failed). Reliable for non-USD-base
        #    FX SINGLE-bot scenarios; can be contaminated if multiple
        #    bots share base ccy.
        #
        # 3. positions() — original source, kept for non-FX (equity,
        #    futures, CFD) where it IS authoritative for discrete-share
        #    instruments. For FX it's structurally unreliable but the
        #    A18 cash-quirk guard prevents destructive folds.
        #
        # Live regression 2026-06-09 GBPUSD: broker had -25k (short),
        # engine state +25k (long), positions() returned 0. A18 prevented
        # destructive fold but DIDN'T detect the actual divergence.
        # A43 (executions sum) DOES detect it correctly.
        ticker = self.config.ticker or ""
        try:
            spec_class_name = (
                self._asset_spec.asset_class.name
                if self._asset_spec is not None else ""
            )
        except Exception:
            spec_class_name = ""
        is_fx_pair = (
            spec_class_name == "FX_CASH"
            and len(ticker) == 6 and ticker.isalpha() and ticker.isupper()
        )

        # Try A43 (executions) FIRST — works for every asset class
        truth_source = "positions()"
        # FL9 floor — choose the execution-sum floor with the SAME rule
        # _reconcile_missed_fills (FL3) uses, so A43 and the missed-fill
        # replay can never disagree about which fills are "ours this session":
        #
        #   • started WITH a position → since=None (full history) so the
        #     bot's own pre-save entry is recovered.
        #   • started FLAT, state file present (RESUME / overnight restart) →
        #     floor at last_saved_ts. Genuine dead-window fills that landed
        #     AFTER the last save are KEPT — this is the equity overnight
        #     case (a resting GTC entry fills pre-market while we're down).
        #   • started FLAT, no state (fresh / --reset / wiped — the FX
        #     same-day cid-reuse phantom) → floor at engine_started_at so
        #     stale pre-restart execs on a reused clientId are excluded.
        #
        # WHY NOT engine_started_at unconditionally: on a resume that floor
        # sits AFTER the dead-window fills and would silently drop them from
        # the sum — exactly the 2026-05-27 META postmortem bug (task #18)
        # that FL3 fixed by preferring last_saved_ts. We mirror it here.
        if getattr(self, '_started_with_position', False):
            _exec_since = None
        else:
            _saved_iso = ((self.state_store.load() or {}).get('updated_at', '')
                          if self.state_store else '')
            _saved_ts = None
            if _saved_iso:
                try:
                    _saved_ts = datetime.fromisoformat(_saved_iso)
                except (TypeError, ValueError):
                    _saved_ts = None
            _exec_since = (
                _saved_ts if _saved_ts is not None else self._engine_started_at
            )
        try:
            exec_qty = self.gateway.get_our_position_via_executions(
                ticker, since=_exec_since)
        except Exception:
            exec_qty = None
        if exec_qty is not None:
            positions_says = broker_qty
            broker_qty = int(exec_qty)
            truth_source = "executions[clientId]"
            if positions_says != broker_qty:
                self._log(
                    f"[RECONCILE] A43 executions truth: positions()="
                    f"{positions_says} → executions[c{self.config.ibkr_client_id}]"
                    f"={broker_qty} (uncontaminated per-bot truth)"
                )
        elif is_fx_pair and ticker[:3] != "USD":
            # A43 unavailable; fall back to A42 for non-USD-base FX
            try:
                fx_qty = self.gateway.get_fx_position_via_account_values(ticker)
            except Exception:
                fx_qty = None
            if fx_qty is not None:
                positions_says = broker_qty
                broker_qty = int(round(fx_qty))
                truth_source = f"accountValues[{ticker[:3]}]"
                if positions_says != broker_qty:
                    self._log(
                        f"[RECONCILE] A42 FX truth override: positions()="
                        f"{positions_says} → accountValues[{ticker[:3]}]"
                        f"={broker_qty} (cash-ledger truth used)"
                    )

        # SHORT INVERSION (P9): the engine stores `_quantity` as a POSITIVE
        # magnitude, but broker_qty (executions / positions) is SIGNED —
        # negative for a short. Compare in SIGNED space so a healthy short
        # (broker -100, engine -100) matches instead of mismatching forever.
        engine_qty = -self._quantity if self._position_open else 0

        if broker_qty != engine_qty:
            # A70 (2026-06-10) — throttle by DRIFT (broker − engine), not
            # absolute values. The chaos test live-ran 117 POSITION_MISMATCH
            # alerts to the Teams webhook in 10 minutes because the engine
            # was cycling normally (engine_qty: 0→100→0→100) on top of a
            # fixed-offset ghost position (broker_qty: 200→300→200→300).
            # Same drift of +200 the entire run, but the old A44 throttle
            # compared (last_broker, last_engine) == (broker, engine) which
            # changed every minute → never suppressed → spam. The drift
            # is what actually matters for an operator: "engine and broker
            # disagree by N shares" is the signal; the cycling absolute
            # values are just normal trading. Also bump cooldown 60s →
            # 300s so even when the drift LEGITIMATELY changes, re-alerts
            # are at most every 5 minutes.
            now = self._ts() if hasattr(self, '_ts') else datetime.now()
            last = getattr(self, '_last_mismatch_alert', None)
            should_alert = True
            current_drift = broker_qty - engine_qty
            if last is not None:
                last_ts, last_broker, last_engine = last
                try:
                    age_s = (now - last_ts).total_seconds()
                except Exception:
                    age_s = 9999.0  # fail open → alert
                last_drift = last_broker - last_engine
                same_drift = (last_drift == current_drift)
                if same_drift and age_s < 300.0:
                    should_alert = False
            if should_alert:
                self._last_mismatch_alert = (now, broker_qty, engine_qty)
            msg = (
                f"POSITION MISMATCH: IBKR reports {broker_qty} {self.config.ticker} "
                f"(via {truth_source}) but engine thinks {engine_qty}. This means a "
                f"fill happened the reconcile couldn't auto-replay (manual TWS "
                f"action? partial fill? stale state file? cruft from prior session?). "
                f"Manual review needed."
            )
            if should_alert:
                self._log(f"!!! {msg}")
            if self._alerts and should_alert:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="POSITION_MISMATCH",
                    severity=AlertSeverity.CRITICAL,
                    message=msg,
                    context={
                        "ticker": self.config.ticker,
                        "broker_qty": broker_qty,
                        "engine_qty": engine_qty,
                        "engine_state": self._state.value,
                        "entry_price": self._entry_price,
                    },
                    correlation_id=self._cycle_id,
                )
            if self._audit and should_alert:
                self._audit.log_state(
                    event="POSITION_MISMATCH",
                    state=self._state,
                    position_open=self._position_open,
                    entry_price=self._entry_price,
                    broker_qty=broker_qty,
                    engine_qty=engine_qty,
                )

            # A53 (2026-06-09): auto-fold REMOVED.
            #
            # Prior behaviour: when broker_qty=0 but engine_qty>0, the engine
            # would auto-fold to FLAT (after a 3s double-confirm + resting-stop
            # guard + FX cash-ledger guard). The intent was to prevent a stale
            # engine from arming a SELL on a phantom position.
            #
            # Why we removed it: the failure mode is asymmetric. When broker
            # reports 0 on a REAL position (positions() lag, FX cash-ledger
            # quirk, transient query glitch), folding to FLAT cancels the
            # bracket child / protective SL — stripping protection off a live
            # position. The pre-flight SELL guard alone prevents shorting
            # (it refuses to place a SELL when broker_qty < qty_to_sell);
            # folding state on top of that is redundant AND can DESTROY
            # the protective stop on a live position when it's wrong.
            #
            # The safer policy: alert loudly, leave engine state untouched,
            # operator reconciles manually. If the engine tries to arm a
            # SELL on a phantom position, pre-flight refuses it and the
            # alert above re-fires. No shorts get placed. No protective
            # stops get destroyed.
            #
            # Loud alert + manual reconcile is the right default for a
            # divergence we can't prove the direction of.
            #
            # FL8 (2026-06-13): SELF-HEAL when the truth source is the
            # ledger-backed execution sum. A53 refused to mutate because the
            # only signal was positions() (unreliable for FX → a wrong fold
            # could strip a live stop). That objection is VOID here: the
            # ledger is OUR own fills, gap-free + exactly-once (FL1-FL7), so
            # when it disagrees with the engine the engine is simply wrong
            # and MUST adopt — otherwise the MSFT-class drift (engine missed
            # a dead-window fill, kept arming brackets on top of a real
            # position → naked-short trap) can never heal. We adopt ONLY from
            # the ledger/exec source; positions()/accountValues keep A53.
            # SHORT INVERSION (P9): the healthy position is a NEGATIVE broker
            # qty. Adopt when broker is SHORT (broker_qty < 0); flag an
            # unexpected LONG (broker_qty > 0) as the anomaly.
            ledger_backed = (truth_source == "executions[clientId]")
            if ledger_backed and broker_qty < 0:
                # Ledger holds a SHORT the engine is blind to (or sized wrong).
                # Adopt it as IN_POSITION so the engine stops arming fresh
                # entries on top, and re-arm a CORRECTLY-sized BUY cover.
                old = engine_qty
                self._entry_price = (
                    self._round_to_tick(broker_avg_cost) if broker_avg_cost > 0
                    else (self._entry_price if (self._entry_price and self._entry_price > 0)
                          else self._round_to_tick(getattr(self, '_prev_ltp', 0) or 0))
                )
                self._quantity = int(-broker_qty)  # positive magnitude
                self._position_open = True
                self._state = TradeState.IN_POSITION
                if not self._highest_price:
                    self._highest_price = self._entry_price
                if self._entry_price and self._entry_price > 0:
                    self._stop_loss = self._protective_stop_price(
                        self._entry_price, self._effective_stop_pct())
                # Drop stale flat-cycle bracket tracking so the orphan sweep
                # cancels the mis-sized resting legs and Probe-1 re-arms a
                # protective BUY cover sized to the adopted quantity.
                self._pending_stop = None
                self._bracket_child = None
                self._save_state()
                self._log(
                    f"[FL8] ADOPTED ledger SHORT: {self.config.ticker} "
                    f"engine {old} → -{self._quantity} (entry≈${self._entry_price}); "
                    f"stale bracket dropped, protective cover will re-arm to size."
                )
                try:
                    await self._place_protective_stop("FL8_LEDGER_ADOPT")
                except Exception as e:
                    self._log(f"[FL8] protective re-arm deferred to health-check: {e}")
                if self._alerts:
                    from src.infra.alerts import AlertSeverity
                    self._alerts.raise_alert(
                        code="CUSTOM_LEDGER_ADOPTED",
                        severity=AlertSeverity.HIGH,
                        message=(f"FL8 self-heal: adopted SHORT {self._quantity} "
                                 f"{self.config.ticker} from ledger (engine was "
                                 f"{old}); protective cover re-armed."),
                        context={"ticker": self.config.ticker,
                                 "adopted_qty": -self._quantity, "was": old},
                        correlation_id=self._cycle_id,
                    )
            elif ledger_backed and broker_qty == 0 and engine_qty != 0:
                # Ledger says FLAT; engine thought it held a short → the
                # COVER was missed. Safe to fold to FLAT because the trusted
                # ledger confirms it (unlike A53's fear of positions() lying).
                old = engine_qty
                self._position_open = False
                self._quantity = 0
                self._entry_price = None
                self._stop_loss = None
                self._pending_stop = None
                self._bracket_child = None
                self._state = TradeState.WAITING_REENTRY
                self._save_state()
                self._log(
                    f"[FL8] ledger says FLAT but engine held {old} "
                    f"{self.config.ticker}; folded engine to FLAT (cover was "
                    f"missed). Re-entry will arm normally.")
            elif ledger_backed and broker_qty > 0:
                # Unexpected LONG on a short-only strategy. Adopt the knowledge
                # but do NOT arm (flattening = SELL; left to operator). Alert.
                self._log(
                    f"[FL8] LEDGER LONG {broker_qty} {self.config.ticker} — "
                    f"unexpected long detected on a SHORT strategy; "
                    f"entries stay blocked, alerting.")
                if self._alerts:
                    from src.infra.alerts import AlertSeverity
                    self._alerts.raise_alert(
                        code="CUSTOM_LEDGER_SHORT",
                        severity=AlertSeverity.CRITICAL,
                        message=(f"FL8: ledger shows UNEXPECTED LONG {broker_qty} "
                                 f"{self.config.ticker} on a SHORT strategy — "
                                 f"needs flatten (SELL)."),
                        context={"ticker": self.config.ticker, "long_qty": broker_qty},
                        correlation_id=self._cycle_id,
                    )
            # else: non-ledger truth source → keep A53 (alert only, above).
        else:
            self._log(
                f"[RECONCILE] Position check OK: broker={broker_qty}, engine={engine_qty}"
            )

    # (Older first _save_state removed — was a duplicate of the canonical
    # one below at the end of the class. Python kept the second definition
    # anyway; explicit removal makes the file easier to read.)

    # (_place_entry_limit removed — placed a BUY LIMIT at trigger price,
    # which is the wrong direction for breakout entry: a LIMIT BUY at $230
    # fills immediately at any ask <= $230. _check_entry / _enter were
    # removed in an earlier pass; this helper was the last leftover.
    # Active entry placement is `_place_entry_stop_limit` exclusively.)

    def _promote_resumable_state(self) -> None:
        """RS1 — on restart, promote a non-active restored state to MONITORING.

        Called by start() AFTER _load_state() but BEFORE reconcile. Promotes
        both IDLE (fresh) and STOPPED to MONITORING.

        Why STOPPED must be promoted: STOPPED is set in exactly ONE place —
        stop() (the graceful-shutdown path) — which then persists it via
        _save_state(). So a bot cleanly shut down while FLAT comes back with
        state=STOPPED on the next launch, and a STOPPED engine is frozen: it
        never evaluates entries AND never re-arms a bracket, which also
        disables the dead-window fill recovery (reqExecutions replay →
        recompute highest → re-arm) from ever acting. Observed 2026-06-16:
        NFLX/AMD/SPCX were gracefully stopped overnight while flat, came back
        STOPPED, and sat idle all day; AAPL/META only escaped because a real
        open position triggered orphan-adoption which forced IN_POSITION.
        STOPPED is purely a shutdown artifact — there is NO risk→STOPPED path
        — so on restart it means "resume monitoring", identical to IDLE.
        Active states (MONITORING / WAITING_REENTRY / IN_POSITION / etc.) are
        left untouched so a mid-session crash-restart keeps its real state.

        RS1b — restore the FAITHFUL flat label from the durable flags that
        stop() did NOT clobber: a FLAT bot with a saved previous_breakout_level
        was in WAITING_REENTRY (re-entry armed at the breakout), so resume
        there rather than generic MONITORING — the dashboard reads true and a
        re-arm (if ever needed) uses the breakout level, not config trigger.
        A bot with an OPEN position is left to MONITORING here and corrected to
        IN_POSITION by the reconcile/orphan-adoption path below (the existing,
        tested flow — unchanged). Pure label faithfulness; adoption & the
        position path are untouched.
        """
        if self._state in (TradeState.IDLE, TradeState.STOPPED):
            if (not self._position_open) and self._previous_breakout_level:
                self._state = TradeState.WAITING_REENTRY
            else:
                self._state = TradeState.MONITORING

    async def stop(self):
        """Stop engine."""
        self._running = False
        self._state = TradeState.STOPPED

        # Cancel the tick consumer; queue contents are discarded (we're
        # shutting down — strategy decisions on stale ticks are pointless).
        if self._tick_consumer_task is not None and not self._tick_consumer_task.done():
            self._tick_consumer_task.cancel()
            try:
                await self._tick_consumer_task
            except (asyncio.CancelledError, Exception):
                pass

        # Cancel the daily reset scheduler
        if self._daily_reset_task is not None and not self._daily_reset_task.done():
            self._daily_reset_task.cancel()
            try:
                await self._daily_reset_task
            except (asyncio.CancelledError, Exception):
                pass

        # Cancel the session controller (auto-PAUSE outside ETH window).
        if self._session_controller_task is not None and not self._session_controller_task.done():
            self._session_controller_task.cancel()
            try:
                await self._session_controller_task
            except (asyncio.CancelledError, Exception):
                pass

        # Cancel the health-check loop.
        if self._health_check_task is not None and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except (asyncio.CancelledError, Exception):
                pass

        # Cancel the periodic state-save loop AFTER one final save so
        # disk reflects the latest in-memory state at shutdown.
        if self._periodic_state_save_task is not None and not self._periodic_state_save_task.done():
            self._periodic_state_save_task.cancel()
            try:
                await self._periodic_state_save_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self._save_state()
        except Exception as e:
            self._log(f"[shutdown] final state save failed: {e}")

        if self._ticks_dropped:
            self._log(f"Engine: {self._ticks_dropped} ticks dropped under load")
        self._log(f"Engine stopped")
        await self._log_json("STRATEGY_STOPPED", trades=self._trades_today, pnl=self._pnl)
        self.audit("STOPPED", {"trades": self._trades_today, "pnl": self._pnl})

    async def _daily_reset_scheduler(self) -> None:
        """Background task: reset daily counters at midnight ET.

        Uses America/New_York wall-clock for the rollover point, NOT local
        time. The US trading day boundary is 00:00 ET — a bot running on
        EC2 in Virginia (ET) and a bot running on a dev laptop in IST
        should both reset at the SAME instant: the start of a new NYSE
        trading day. Previously this used `datetime.now()` (local time),
        which on an IST-time machine would reset counters at 09:30 IST =
        00:00 EDT, but during DST switches that misaligns; and any deploy
        in a non-ET timezone misalignments by hours.

        ZoneInfo handles EDT↔EST DST automatically. Sleeps until exact
        next 00:00 ET, fires reset, repeats. Self-rescheduling so it
        wakes at most a few seconds late even across DST transitions.

        Resets:
            - engine: _trades_today, _wins, _losses
            - risk: _cached_trades_today, _cached_daily_pnl, _consecutive_losses
        Does NOT reset:
            - _pnl, _total_commission (cumulative across days by design)
            - position state (carried overnight for swing strategies)
        """
        from datetime import datetime as _dt_cls, timedelta as _td
        while self._running:
            now_et = _dt_cls.now(ET_ZONE)
            tomorrow_et = now_et.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + _td(days=1)
            seconds_until = (tomorrow_et - now_et).total_seconds()
            try:
                await asyncio.sleep(seconds_until)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            # Reset risk counters AND engine-side daily counters
            try:
                if self.risk:
                    self.risk.reset_daily()
                self._trades_today = 0
                self._wins = 0
                self._losses = 0
                # Note: _pnl/_total_commission are cumulative across days
                # by design (audit log already captures per-day breakdown).
                # Position state is also intentionally NOT reset — swing
                # positions carry across the day boundary.
                self._log("[DAILY RESET] Risk + trade counters cleared at 00:00 ET")
                self._save_state()
            except Exception as e:
                self._log(f"[DAILY RESET] error: {e}")

    # === Session controller (ETH window auto-pause) ===

    async def _session_controller_loop(self) -> None:
        """Auto-PAUSE the engine outside the configured ETH window.

        Cadence: self-rescheduling — sleeps exactly until the next state
        transition (either session_open in N seconds, or one-minute poll
        inside the session to catch the 20:00 ET close). Resting GTC orders
        at IBKR are NEVER cancelled here — they keep protecting open
        positions overnight. Only the engine's strategy loop (entry
        placement, tick processing for entry triggers) is gated.

        On session OPEN:
            - clears _paused if it was True from session-close
            - resets self._session_start_equity for the drawdown probe
            - logs SESSION_OPENED to engine log + audit

        On session CLOSE (or initial-start outside session):
            - sets _paused = True (existing pause/resume infrastructure)
            - logs SESSION_CLOSED
            - does NOT cancel resting orders
        """
        import datetime as _dt
        # Initial reconciliation: align _paused to current session state.
        in_session = self._session_is_open()
        if in_session:
            self._paused = False
            # Snapshot equity at session start for the drawdown probe.
            try:
                self._session_start_equity = (
                    self.gateway.get_equity()
                    if hasattr(self.gateway, 'get_equity') else 0.0
                )
            except Exception:
                self._session_start_equity = 0.0
            self._log(f"[SESSION] Open at startup — engine ACTIVE (equity=${self._session_start_equity:.2f})")
        else:
            self._paused = True
            wait_s = self._seconds_until_session_open()
            self._log(
                f"[SESSION] Closed at startup — engine PAUSED. "
                f"Next open in {wait_s/3600:.1f}h. Resting GTC orders untouched."
            )

        last_state_in_session = in_session
        # Track entry-cutoff window transition independently so we can
        # cancel pending BUYs at exactly the 15:55 boundary (default).
        last_entries_allowed = self._entries_allowed()

        while self._running:
            now = datetime.now()  # local; comparison uses UTC inside helpers
            in_session = self._session_is_open()
            entries_ok = self._entries_allowed()

            # Entry-cutoff transition: T→F means we just entered the
            # last `ENTRY_CUTOFF_BUFFER_MIN` minutes. Active step:
            # cancel any working BUY entry order so it can't fill in
            # the wind-down window where the protective SELL might
            # not get placed before market close. Protective SELL
            # stops (side='SELL', already at the broker for any open
            # position) are LEFT ALONE — they remain GTC and continue
            # protecting overnight.
            if in_session and last_entries_allowed and not entries_ok:
                # Entry-cutoff cancel is now OPT-IN. DEFAULT (no env var) =
                # KEEP the resting SELL entry bracket through the close so it
                # persists overnight (the reconcile flow). Set GT_ENTRY_CUTOFF=1
                # to restore the old behaviour: cancel the pending SELL in the
                # last ENTRY_CUTOFF_BUFFER_MIN minutes before close. Either way
                # the protective BUY-cover stops and the session PAUSE below are
                # untouched, so no naked exposure is introduced (a fill carries
                # its bracket child stop). Set per-bot via the launcher env.
                # SHORT INVERSION (P11): entry is a SELL (was BUY) — the long
                # form matched only BUY here, so the SELL entry never cancelled.
                import os as _eco
                _do_cutoff_cancel = bool(_eco.environ.get("GT_ENTRY_CUTOFF", "").strip())
                pending = getattr(self, '_pending_stop', None)
                if not _do_cutoff_cancel:
                    self._log(
                        "[SESSION] Entry cutoff reached — cancel SKIPPED by default "
                        "(pending SELL entry left resting through the close; set "
                        "GT_ENTRY_CUTOFF=1 to cancel)"
                    )
                elif pending and (pending.get('side') == 'SELL' or pending.get('side') == OrderSide.SELL):
                    pending_id = pending.get('order_id') or pending.get('id') or ''
                    self._log(
                        f"[SESSION] Entry cutoff reached — cancelling pending entry "
                        f"SELL order {pending_id} (no new entries within last "
                        f"{ENTRY_CUTOFF_BUFFER_MIN} min)"
                    )
                    try:
                        await self.gateway.cancel_order(pending_id)
                        # Clear our reference so the next entry attempt
                        # (next session) starts clean.
                        self._pending_stop = None
                    except Exception as e:
                        self._log(f"[SESSION] cancel of pending entry failed: {e}")
                else:
                    self._log(
                        f"[SESSION] Entry cutoff reached — no pending SELL to cancel "
                        f"(state={self._state.value})"
                    )
            last_entries_allowed = entries_ok

            # Detect transition
            if in_session and not last_state_in_session:
                self._paused = False
                try:
                    self._session_start_equity = (
                        self.gateway.get_equity()
                        if hasattr(self.gateway, 'get_equity') else 0.0
                    )
                except Exception:
                    self._session_start_equity = 0.0
                self._log(f"[SESSION] Window OPENED — engine resumed (equity=${self._session_start_equity:.2f})")
                if self._alerts:
                    try:
                        from src.infra.alerts import AlertSeverity
                        self._alerts.raise_alert(
                            code="SESSION_OPENED",
                            severity=AlertSeverity.LOW,
                            message=f"ETH session opened. Engine resumed.",
                            context={"equity": self._session_start_equity, "ticker": self.config.ticker},
                            correlation_id=self._cycle_id,
                        )
                    except Exception:
                        pass

                # Place entry order on session open if MONITORING / WAITING_REENTRY
                # and nothing is resting. Without this, the bot would wait up to
                # 30s for the health-check loop to notice — the first 30s of an
                # active session is exactly when we want to be in the book.
                should_have_entry = (
                    self._state in (TradeState.MONITORING, TradeState.WAITING_REENTRY)
                    and not self._position_open
                    and not getattr(self, '_pending_stop', None)
                )
                if should_have_entry:
                    trigger = (
                        self._previous_breakout_level
                        if self._state == TradeState.WAITING_REENTRY and self._previous_breakout_level
                        else self.config.trigger_price
                    )
                    self._log(f"[SESSION] Placing entry order at session open: trigger=${trigger:.2f}")
                    try:
                        await self._place_entry_stop_limit(trigger)
                    except Exception as e:
                        self._log(f"[SESSION] Entry placement at session open failed: {e}")
            elif (not in_session) and last_state_in_session:
                self._paused = True
                self._log(f"[SESSION] Window CLOSED — engine paused. Resting GTC orders untouched.")
                if self._alerts:
                    try:
                        from src.infra.alerts import AlertSeverity
                        self._alerts.raise_alert(
                            code="SESSION_CLOSED",
                            severity=AlertSeverity.LOW,
                            message=f"ETH session closed. Engine paused.",
                            context={"ticker": self.config.ticker, "trades_today": self._trades_today, "pnl": self._pnl},
                            correlation_id=self._cycle_id,
                        )
                    except Exception:
                        pass

            last_state_in_session = in_session

            # Compute next wake time:
            #   in-session: prefer sleeping right to the entry-cutoff
            #     boundary (default 15:55 ET) so we cancel pending BUYs
            #     at the exact second rather than up to 60s late. Cap
            #     at 60s so we still catch the regular session-close
            #     transition + any clock drift.
            #   out-of-session: sleep until exact next-open (capped at
            #     1h so we re-check periodically if system clock drifts).
            if in_session:
                if entries_ok:
                    # Race the entry-cutoff deadline; never sleep past it.
                    wait_s = min(seconds_until_entry_cutoff(), 60.0)
                else:
                    # Already past cutoff — just poll for the session-close
                    # transition.
                    wait_s = 60.0
                wait_s = max(wait_s, 1.0)  # never busy-loop
            else:
                wait_s = min(self._seconds_until_session_open(), 3600.0)
                wait_s = max(wait_s, 10.0)  # never tighter than 10s

            try:
                await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                break

    # === Periodic state-file save loop ===

    async def _periodic_state_save_loop(self) -> None:
        """Force a state-file save every 1 second while the engine runs.

        Why this exists
        ---------------
        All other `_save_state()` calls are event-driven (fills, new highs,
        state transitions, order placements). Between events the on-disk
        state can lag in-memory state arbitrarily — sometimes by minutes
        during MONITORING. If a restart happens inside that window, the
        engine loads a stale snapshot and any in-memory mutation that
        didn't fire an explicit save is lost.

        The 2026-06-01 ZM double-short bug had this profile: a bracket
        was placed (state saved), some path mutated `_bracket_child`
        between events without re-saving, the engine restarted, the
        loaded state had stale `_bracket_child = None`, and the next
        BUY-complete branch took the legacy fallback path → duplicate
        SELL → orphan → short on later trigger.

        With this loop:
            * Worst-case drift between memory and disk = 1 second.
            * Crash window where state can be lost = at most one save
              interval (1s).
            * No correctness impact on the engine's main loop — saves
              run on a separate asyncio task, do not block tick processing.

        Cost: one disk write every 1s during trading hours. StateStore
        uses an atomic write (write-temp + rename), so partial writes
        on crash are impossible. Save time on a typical state file
        (~2-3 KB JSON) is sub-millisecond on SSD. ~60 writes/min, ~57k
        per 16h trading day = trivial vs SSD endurance ratings.

        Cadence: 1 second is the deliberate trade-off:
            * Fast enough that any drift between memory and disk is
              bounded to under one second — meaningfully tighter than
              an HFT-tolerant 2s window.
            * Slow enough that I/O load is negligible (~60 saves/min).
            * Even a sub-second pause between mutations and save is
              extremely unlikely to coincide with a crash, but if it
              does, only ≤1s of state is at risk.

        Paper mode: still runs — the state file is just as authoritative
        in paper as in live; no special-casing needed.
        """
        INTERVAL_SEC = 1.0
        while self._running:
            try:
                await asyncio.sleep(INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            try:
                self._save_state()
            except Exception as e:
                # Never let a save failure kill the loop. Log and continue —
                # the next tick will try again. A persistent failure here
                # is operationally critical but doesn't break the engine's
                # main strategy loop (which is the same as before this fix).
                self._log(f"[periodic-save] state save failed (continuing): {e}")

    # === A46: Invariant-enforcement sweep ===

    async def _invariant_sweep_loop(self) -> None:
        """A48 — Bilateral per-order-id invariant enforcement (BOTH sides).

        TIGHT INVARIANT:
            At every moment t, every order at the broker for our symbol
            must belong to the engine's tracked set:

                tracked_buys  = { _pending_stop.order_id  (if side=BUY),
                                  _bracket_child.parent_order_id }
                tracked_sells = { _bracket_child.order_id,
                                  _pending_stop.order_id  (if side=SELL) }

            Any broker order whose engine_id (orderRef) is NOT in the
            corresponding tracked set is an ORPHAN — kill it.

        WHY BILATERAL:
            A47 v2 only swept SELL orders. When it killed an orphan
            child SELL, the BUY parent (transmit=False, awaiting child's
            transmit signal) stayed orphaned in client-side "Transmit"
            limbo at IBKR. Live regression 2026-06-09: user saw 3 single
            BUY STP-LMT orders for USD.CAD/USD.CHF/USD.JPY without any
            matching child SELL — exactly the post-A47 orphan parent.
            A48 sweeps BOTH sides; an orphan parent gets killed in the
            same 250ms tick as the orphan child.

        BOUND:
            Maximum orphan exposure ≤ 250ms sweep + ~50ms broker cancel
            ≈ 300ms worst-case, both legs.

        BEHAVIOR:
            - Runs every 250ms while engine is _running
            - Compares broker open orders to engine tracked sets
            - Cancels any orphan via gateway.cancel_order(engine_id)
            - Writes ORPHAN_ORDER_INVARIANT_CANCEL audit row
            - Raises HIGH alert
            - Best-effort: never raises
        """
        SWEEP_INTERVAL_S = 0.25
        while self._running:
            try:
                await asyncio.sleep(SWEEP_INTERVAL_S)
                # Skip if disconnected — fetch_open_orders would raise
                if not getattr(self.gateway, 'connected', False):
                    continue
                # A49 — Skip while ANY placement is in flight. There's a
                # 1-10ms window between `placeOrder` returning a broker
                # id and the engine setting `_bracket_child`/`_pending_stop`
                # to mark the new orders as tracked. If the sweep fires
                # in that window it would kill the just-placed legitimate
                # order. Two flags cover this:
                #   _entry_placing            (A37) — set during entry/bracket placement
                #   _protective_stop_placing  (pre-existing) — set during SL placement
                #   _reconciling              (A62) — set during _reconcile_open_orders
                #                             so we don't race the bracket-pair adoption
                #                             and cancel a leg we're about to claim
                if (getattr(self, '_entry_placing', False)
                        or getattr(self, '_protective_stop_placing', False)
                        or getattr(self, '_reconciling', False)
                        # Fencing-token gate (SHORT mirror of LONG EURUSD
                        # naked-short 2026-07-28): also skip the orphan sweep
                        # whenever the ledger is not reconciled for the current
                        # connection epoch — a stale _bracket_child /
                        # _pending_stop would let it cancel a leg it is about
                        # to re-adopt. Superset of the _reconciling check
                        # above; never skips less.
                        or not self._actuation_allowed()):
                    continue
                # Compute the SETS of LEGITIMATE engine_ids on BOTH sides.
                # Anything at the broker NOT in the corresponding set is
                # an orphan to be killed.
                legitimate_sell_ids: set[str] = set()
                legitimate_buy_ids: set[str] = set()
                bc = getattr(self, '_bracket_child', None)
                if bc and bc.get('order_id'):
                    # The bracket child is a SELL
                    legitimate_sell_ids.add(str(bc['order_id']))
                    # ...AND its parent BUY (we placed both atomically)
                    parent_id = bc.get('parent_order_id')
                    if parent_id:
                        legitimate_buy_ids.add(str(parent_id))
                ps = getattr(self, '_pending_stop', None)
                if ps and ps.get('order_id'):
                    side = ps.get('side')
                    side_str = (side.value if hasattr(side, 'value')
                                else str(side)).upper()
                    if side_str == 'SELL':
                        legitimate_sell_ids.add(str(ps['order_id']))
                    elif side_str == 'BUY':
                        legitimate_buy_ids.add(str(ps['order_id']))

                # Fetch broker open orders
                try:
                    open_orders = self.gateway.fetch_open_orders()
                except ConnectionError:
                    continue
                except Exception as e:
                    self._log(
                        f"[INVARIANT SWEEP] fetch_open_orders failed "
                        f"(continuing): {type(e).__name__}: {e}"
                    )
                    continue

                for o in (open_orders or []):
                    # Only orders for OUR symbol
                    if o.get('symbol') != self.config.ticker:
                        continue
                    action = (o.get('action') or '').upper()
                    otype = (o.get('order_type') or '').replace(' ', '').upper()
                    # Only sweep stop-style orders (STP, STP-LMT). MKT and
                    # LMT orders are typically transient flatten requests
                    # that should complete on their own.
                    if otype not in ('STP', 'STPLMT'):
                        continue
                    broker_order_id = str(o.get('order_id', '') or '')
                    if not broker_order_id:
                        continue  # malformed
                    if action == 'SELL':
                        tracked = legitimate_sell_ids
                        kind = "SELL"
                    elif action == 'BUY':
                        tracked = legitimate_buy_ids
                        kind = "BUY"
                    else:
                        continue  # unknown side
                    if broker_order_id in tracked:
                        continue  # legitimately tracked, leave alone
                    # ORPHAN — not in our tracked set
                    self._log(
                        f"!!! [INVARIANT SWEEP] ORPHAN {kind} {otype} at broker: "
                        f"{broker_order_id} (not in tracked {kind.lower()}_ids="
                        f"{sorted(tracked) if tracked else '∅'}). "
                        f"State={self._state.value}, pos_open={self._position_open}. "
                        f"CANCELLING."
                    )
                    try:
                        result = self.gateway.cancel_order(broker_order_id)
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
                    except Exception as e:
                        self._log(
                            f"[INVARIANT SWEEP] cancel of {broker_order_id} "
                            f"raised ({type(e).__name__}: {e}) — will retry "
                            f"next sweep"
                        )
                    # Audit + alert
                    if self._audit:
                        try:
                            self._audit.log_order(
                                event="ORPHAN_ORDER_INVARIANT_CANCEL",
                                order_id=broker_order_id,
                                side=kind,
                                qty=o.get('qty', 0),
                                stop_price=o.get('stop_price'),
                                reason=(
                                    f"INVARIANT VIOLATION: {kind} {otype} at "
                                    f"broker not in engine's tracked set "
                                    f"({sorted(tracked)}). Force-cancelled."
                                ),
                            )
                        except Exception:
                            pass
                    if self._alerts:
                        try:
                            from src.infra.alerts import AlertSeverity
                            self._alerts.raise_alert(
                                code="ORPHAN_ORDER_INVARIANT_CANCEL",
                                severity=AlertSeverity.HIGH,
                                message=(
                                    f"Invariant sweep cancelled orphan {kind} "
                                    f"{otype} {broker_order_id} on "
                                    f"{self.config.ticker} — engine wasn't "
                                    f"tracking this order."
                                ),
                                context={
                                    "ticker": self.config.ticker,
                                    "order_id": broker_order_id,
                                    "side": kind,
                                    "order_type": otype,
                                    "stop_price": o.get('stop_price'),
                                    "engine_state": self._state.value,
                                    "tracked_sell_ids": sorted(legitimate_sell_ids),
                                    "tracked_buy_ids": sorted(legitimate_buy_ids),
                                },
                                correlation_id=getattr(self, '_cycle_id', ''),
                            )
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Loop must never die — log and continue
                self._log(
                    f"[INVARIANT SWEEP] loop error (continuing): "
                    f"{type(e).__name__}: {e}"
                )

    # === Health-check loop ===

    async def _health_check_loop(self) -> None:
        """Periodic invariant probes. Cadence: 30s.

        Invariants probed (paper mode: skipped, no broker truth to check):
            1. position_open ⇒ resting SELL stop-limit at broker matching `_pending_stop`
               If not: NAKED_POSITION alert + re-arm via _place_protective_stop.
            2. MONITORING/WAITING_REENTRY (and not paused) ⇒ resting BUY entry
               If not: ENTRY_ORDER_MISSING alert + re-place.
            3. Feed-staleness inside RTH: no tick in N seconds → STALE_FEED.
            4. Equity drawdown vs session start (every 10 ticks of this loop,
               i.e. every 5 min): >5% drop fires CIRCUIT_BREAKER_DRAWDOWN.

        All probes are best-effort: failures are logged but don't stop the
        loop. The point is to surface drift between engine state and broker
        truth — we don't want the engine to think it's protected when it isn't.
        """
        from src.infra.alerts import AlertSeverity
        equity_probe_counter = 0
        last_alerted_naked = False  # de-dup the noisy "still naked" alerts
        last_alerted_no_entry = False
        last_alerted_stale = False
        STALE_FEED_SECONDS = 60.0  # tick-by-tick should fire many times/sec during RTH

        while self._running:
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            if self.gateway.paper:
                continue  # no broker truth in paper mode

            # ---- Probe 0: position-state divergence ───────────────────
            # Cheap idempotent reconcile of engine position vs broker
            # position. Catches in-session divergences (manual TWS
            # action, missed fill events, the fill-processing race that
            # made the health-rearm short happen) BEFORE Probe 1 below
            # would arm a fresh SL on a non-existent position.
            #
            # `_reconcile_position_state` only fires the auto-FLAT path
            # when broker_qty == 0 AND engine_qty > 0 — the dangerous
            # direction. When they match it's a near-zero-cost no-op.
            try:
                await self._reconcile_position_state()
            except Exception as e:
                self._log(f"[HEALTH] periodic reconcile failed (best-effort): {e}")

            # ---- Probe 1: position_open ⇒ resting SELL stop-limit
            # Match BOTH 'STPLMT' (legacy protective stop-limit) AND 'STP'
            # (the bracket migration's market-on-trigger child). Without
            # 'STP' in the set the health check would always see "no SL"
            # for bracket-protected positions and try to re-arm — exactly
            # the failure mode that caused the 2026-05-27 NVDA naked-400.
            # CRITICAL: skip health probes when gateway is disconnected.
            # Probe 1 (NAKED_POSITION) and Probe 2 (ENTRY_ORDER_MISSING)
            # both decide based on whether broker has resting orders.
            # While disconnected, fetch_open_orders raises (post-2026-06-06
            # fix), so the bare-except below would let us continue — but
            # we'd skip the probe with NO information. Better to skip
            # entire iteration and try again next tick (when likely
            # reconnected).
            if not getattr(self.gateway, 'connected', False):
                # PASSIVE: did ib_async's socket already come back?
                # (Rare on TWS-kill — usually it stays down.)
                self._try_recover_connection_status()
                # ACTIVE: if still disconnected, actively call connectAsync.
                # Throttled to once per 10s inside the method.
                # (Live regression 2026-06-08: passive check alone never
                # saw socket come back because ib_async doesn't auto-reconnect.)
                if not getattr(self.gateway, 'connected', False):
                    await self._try_active_reconnect()
                if not getattr(self.gateway, 'connected', False):
                    self._log(
                        "[HEALTH] skipped — gateway DISCONNECTED. "
                        "Will retry on next tick."
                    )
                    continue

            # Fencing-token gate (SHORT mirror of LONG EURUSD naked-short
            # 2026-07-28). Probe 1 (NAKED_POSITION -> arm protective BUY to
            # cover) and Probe 2 (ENTRY_ORDER_MISSING -> place SELL) both READ
            # ledger position state to decide whether to actuate. Do NOT let
            # them act until the ledger has been reconciled against broker
            # executions for the CURRENT connection epoch. Probe 0 above is a
            # positions()-based check that is blind for FX (cash-ledger quirk),
            # so the epoch is the real gate. Firing a cover-BUY on a stale
            # IN_POSITION belief would BUY into a flat book -> naked LONG (the
            # short-side mirror of the LONG naked short). Deferring is fail-safe
            # — the position stays covered by the resting broker bracket; we
            # re-probe next tick, and _after_reconnect's reconcile opens the gate.
            if not self._actuation_allowed():
                self._log(
                    "[HEALTH] deferring probes — ledger not yet reconciled "
                    "for current connection epoch (post-reconnect)"
                )
                continue
            try:
                open_orders = self.gateway.fetch_open_orders() if hasattr(self.gateway, 'fetch_open_orders') else []
                _SL_TYPES = ('STPLMT', 'STP')
                has_sell_sl = any(
                    o.get('action') == 'SELL'
                    and (o.get('order_type') or '').replace(' ', '').upper() in _SL_TYPES
                    for o in open_orders
                )
                has_buy_sl = any(
                    o.get('action') == 'BUY'
                    and (o.get('order_type') or '').replace(' ', '').upper() in _SL_TYPES
                    for o in open_orders
                )
            except ConnectionError as e:
                # Socket died between our `connected` check above and
                # the actual call. Same conclusion: skip this iteration.
                self._log(f"[HEALTH] skipped — socket disconnected mid-fetch: {e}")
                continue
            except Exception as e:
                self._log(f"[HEALTH] fetch_open_orders failed: {e}")
                continue

            # ---- Probe 1b: STOP-PRICE DIVERGENCE detector ────────────
            # Engine state says the protective stop is at X. Broker says
            # the resting SELL STP's auxPrice is Y. If X != Y, something
            # silently mutated the order (broker-side modify rejection,
            # wire-precision truncation, manual TWS edit) — meaning the
            # ACTUAL stop loss is different from what the engine thinks.
            # On EURUSD that translates directly to "your real downside
            # is 30 pips worse than dashboard claims". Alert loudly.
            #
            # Why this check matters more than position-state divergence:
            # the engine's risk math (max drawdown projection, stop-pct
            # display, MAE/MFE backtest comparison) all read from
            # `_pending_stop.stop_price`. If broker's auxPrice has been
            # silently downgraded, every downstream number is wrong.
            #
            # Compares with a per-asset tolerance (half the tick grid)
            # so a 1-tick rounding difference between engine and broker
            # isn't flagged as divergence. Real divergences (precision
            # truncation, manual edits) are always >> 1 tick.
            # SHORT INVERSION (P9): the protective leg is a BUY cover.
            if has_buy_sl and self._pending_stop and self._pending_stop.get('side') == OrderSide.BUY:
                engine_stop = float(self._pending_stop.get('stop_price') or 0)
                # Find the resting BUY cover STP that matches OUR engine_id
                # (skip any unrelated BUY stops the user may have placed
                # manually in TWS — those aren't our concern).
                our_engine_id = self._pending_stop.get('order_id', '')
                broker_stop_for_ours = None
                for o in open_orders:
                    if o.get('action') != 'BUY':
                        continue
                    if (o.get('order_type') or '').replace(' ', '').upper() not in _SL_TYPES:
                        continue
                    # Match by engine_id stashed in orderRef
                    if o.get('order_ref') == our_engine_id:
                        broker_stop_for_ours = o.get('stop_price')
                        break
                if engine_stop > 0 and broker_stop_for_ours is not None:
                    broker_stop = float(broker_stop_for_ours)
                    # Per-asset tolerance: half the tick grid. Use the
                    # central `_price_epsilon()` helper rather than
                    # re-implementing — the helper uses tick_size()
                    # which works for every asset class (the earlier
                    # `.tick.tick` access only worked for FX).
                    tol = self._price_epsilon()
                    delta = abs(engine_stop - broker_stop)
                    if delta > tol:
                        from src.infra.alerts import AlertSeverity
                        msg = (
                            f"STOP PRICE DIVERGENCE: engine thinks stop is "
                            f"{engine_stop:.6f}, broker has resting BUY cover STP at "
                            f"{broker_stop:.6f} (delta={delta:.6f}, tolerance="
                            f"{tol:.6f}). Your real downside is "
                            f"{(broker_stop - engine_stop):+.6f} from the "
                            f"intended stop. Investigate IMMEDIATELY."
                        )
                        self._log(f"!!! {msg}")
                        if self._alerts:
                            self._alerts.raise_alert(
                                code="STOP_PRICE_DIVERGENCE",
                                severity=AlertSeverity.CRITICAL,
                                message=msg,
                                context={
                                    "engine_stop": engine_stop,
                                    "broker_stop": broker_stop,
                                    "delta": delta,
                                    "tolerance": tol,
                                    "order_id": our_engine_id,
                                    "entry": self._entry_price,
                                    "qty": self._quantity,
                                },
                                correlation_id=self._cycle_id,
                            )
                        if self._audit:
                            try:
                                self._audit.log_order(
                                    event="STOP_PRICE_DIVERGENCE",
                                    order_id=our_engine_id,
                                    side="BUY",
                                    qty=self._quantity,
                                    order_type="STP",
                                    stop_price=broker_stop,
                                    signal_price=engine_stop,
                                    reason=f"engine_stop={engine_stop:.6f} broker_stop={broker_stop:.6f} delta={delta:.6f}",
                                    state_at_time=self._state.value,
                                    position_at_time="SHORT",
                                )
                            except Exception:
                                pass

            # SHORT INVERSION (P9): position open ⇒ a resting BUY cover must
            # exist (was a SELL stop). Re-arm if it's missing (not has_buy_sl).
            if self._position_open and self._entry_price and not has_buy_sl:
                # CRITICAL: Don't re-arm if there's a working SELL short entry
                # still filling (mirror of the NVDA partial-fill case): a
                # SELL partial sets a smaller _quantity, and re-arming the
                # cover now would size it to the partial. Wait for the SELL
                # entry's `is_complete=True` branch to arm the cover at full
                # qty. Health re-arm is the SAFETY NET if that path failed.
                sell_entry_still_working = False
                try:
                    entry_id = self._make_engine_id(f"ENTRY_{OrderSide.SELL.value}", self.config.quantity)
                    entry_order = self.registry.get(entry_id) if self.registry else None
                    if entry_order is not None and entry_order.filled_qty < entry_order.qty:
                        # Order in the registry hasn't fully filled yet.
                        if entry_order.status not in (
                            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED
                        ):
                            sell_entry_still_working = True
                    # Also belt-and-suspenders: check the broker's open-orders
                    # for any SELL STP LMT that isn't ours by id but is on the
                    # same ticker.
                    if not sell_entry_still_working and has_sell_sl:
                        sell_entry_still_working = True
                except Exception as e:
                    self._log(f"[HEALTH] SELL-in-flight check failed (treating as safe to re-arm): {e}")

                if sell_entry_still_working:
                    self._log(
                        f"[HEALTH] Position open without cover, but SELL entry still "
                        f"filling — DEFERRING re-arm. The SELL's `is_complete=True` "
                        f"branch will place the cover at full qty when it completes."
                    )
                    # Don't reset last_alerted_naked yet — we'll alert if STILL
                    # naked on the next health tick (30s later) when the BUY
                    # should have either completed or been cancelled.
                    continue  # skip both Probe 1 actions and Probe 2

                if not last_alerted_naked and self._alerts:
                    self._alerts.raise_alert(
                        code="NAKED_POSITION",
                        severity=AlertSeverity.CRITICAL,
                        message=f"SHORT open ({self._quantity} {self.config.ticker} @ ${self._entry_price:.2f}) but NO protective BUY cover stop at broker.",
                        context={"qty": self._quantity, "entry": self._entry_price, "state": self._state.value},
                        correlation_id=self._cycle_id,
                    )
                    last_alerted_naked = True
                self._log(f"[HEALTH] NAKED POSITION detected — attempting re-arm")
                # Clear stale _pending_stop guard so re-arm proceeds
                self._pending_stop = None
                try:
                    await self._place_protective_stop("HEALTH_REARM")
                except Exception as e:
                    self._log(f"[HEALTH] re-arm failed: {e}")
            else:
                last_alerted_naked = False

            # ---- Probe 2: MONITORING/WAITING_REENTRY (not paused) ⇒ SELL entry resting
            # SHORT INVERSION (P9): the entry is a SELL stop-limit, so a missing
            # entry means `not has_sell_sl` (was not has_buy_sl).
            should_have_entry = (
                self._state in (TradeState.MONITORING, TradeState.WAITING_REENTRY)
                and not self._paused
                and not self._position_open
            )
            if should_have_entry and not has_sell_sl:
                # Rejection-aware backoff: if IBKR REJECTED our most recent
                # ENTRY_BUY within the backoff window, don't auto-replace.
                # Re-placing into the same bad request (e.g. invalid FX
                # tick grid, halted contract, margin failure) just floods
                # the broker and clutters the audit log. The operator gets
                # a distinct alert and decides — fix the input and call
                # /resume, or wait for the window to elapse.
                #
                # Window = 300s. Tuned so a transient IBKR hiccup (one bad
                # tick, fleeting margin check) auto-recovers, but a real
                # config mistake doesn't burn 600 rejected orders/hour.
                ENTRY_REJECTION_BACKOFF_SECONDS = 300.0
                rejected_recently = False
                rej_age_s = None
                if self._last_entry_rejected_at is not None:
                    try:
                        rej_age_s = (self._ts() - self._last_entry_rejected_at).total_seconds()
                        rejected_recently = rej_age_s < ENTRY_REJECTION_BACKOFF_SECONDS
                    except Exception:
                        rejected_recently = False

                if rejected_recently:
                    # Surface a SEPARATE alert from ENTRY_ORDER_MISSING so
                    # the operator can distinguish "we're not placing
                    # because the last one was rejected" from "we have no
                    # entry and we don't know why."
                    if not last_alerted_no_entry and self._alerts:
                        self._alerts.raise_alert(
                            code="ENTRY_REJECTION_BACKOFF",
                            severity=AlertSeverity.HIGH,
                            message=(
                                f"Skipping auto re-place: last ENTRY_BUY rejected "
                                f"{rej_age_s:.0f}s ago — {self._last_entry_rejected_reason}"
                            ),
                            context={
                                "state": self._state.value,
                                "rejected_age_s": rej_age_s,
                                "rejection_reason": self._last_entry_rejected_reason,
                                "backoff_window_s": ENTRY_REJECTION_BACKOFF_SECONDS,
                            },
                            correlation_id=self._cycle_id,
                        )
                        last_alerted_no_entry = True
                    self._log(
                        f"[HEALTH] Skipping entry re-place — last ENTRY_BUY "
                        f"rejected {rej_age_s:.0f}s ago ({self._last_entry_rejected_reason}). "
                        f"Will retry after backoff ({ENTRY_REJECTION_BACKOFF_SECONDS:.0f}s)."
                    )
                else:
                    if not last_alerted_no_entry and self._alerts:
                        self._alerts.raise_alert(
                            code="ENTRY_ORDER_MISSING",
                            severity=AlertSeverity.HIGH,
                            message=f"Engine in {self._state.value} but NO SELL (short) entry order resting at broker.",
                            context={"state": self._state.value, "trigger": self.config.trigger_price,
                                     "breakout_level": self._previous_breakout_level},
                            correlation_id=self._cycle_id,
                        )
                        last_alerted_no_entry = True
                    # Re-place at current intended trigger
                    trigger = (
                        self._previous_breakout_level
                        if self._state == TradeState.WAITING_REENTRY and self._previous_breakout_level
                        else self.config.trigger_price
                    )
                    self._log(f"[HEALTH] Entry order missing — re-placing at ${trigger:.2f}")
                    # DIAG (2026-06-17, audit-only): dump the EXACT broker
                    # open-orders snapshot the health-check judged before it
                    # re-arms. If a BUY STP/STPLMT is present here yet we still
                    # re-arm, has_buy_sl is a FALSE NEGATIVE — the root cause of
                    # the re-entry cancel/re-place churn. Never throws.
                    try:
                        if getattr(self, '_audit', None):
                            _snap = "; ".join(
                                f"{o.get('action')}/{o.get('order_type')}/{o.get('status')}"
                                for o in (open_orders or [])
                            ) or "(empty)"
                            self._audit.log_order(
                                event="HEALTH_REARM_DIAG", order_id="", side="SELL",
                                qty=0, order_type="",
                                reason=(f"health re-arm: has_sell_sl={has_sell_sl} "
                                        f"trigger={trigger} open_orders=[{_snap}]"),
                                state_at_time=self._state.value,
                                position_at_time="SHORT" if self._position_open else "FLAT",
                            )
                    except Exception:
                        pass
                    self._pending_stop = None
                    # Clear a STALE bracket-child marker so it can't trip the
                    # A39 entry guard forever. A bot that restarts FLAT in
                    # WAITING_REENTRY can restore a _bracket_child (engine_id)
                    # from a prior cycle that is no longer at the broker; the
                    # guard in _place_entry_stop_limit then refuses every
                    # re-entry and the ticker silently stops trading (observed
                    # 2026-06-18 on JNJ). SAFETY: only when FLAT (never touch the
                    # protective stop of an OPEN position) AND only when the
                    # broker's own open-orders snapshot confirms the leg's
                    # engine_id (orderRef) is NOT resting — so we drop only a
                    # genuine phantom, never a live order. Equity + FX share this
                    # path; the guards make it asset-agnostic and safe for both.
                    _bc = getattr(self, '_bracket_child', None)
                    if _bc and not self._position_open:
                        _bc_id = _bc.get('order_id') if isinstance(_bc, dict) else None
                        _still_resting = bool(_bc_id) and any(
                            (o.get('order_ref') == _bc_id) or (o.get('broker_id') == _bc_id)
                            for o in (open_orders or [])
                        )
                        if not _still_resting:
                            self._log(
                                f"[HEALTH] clearing stale _bracket_child {_bc_id} "
                                f"(flat, not resting at broker) so entry can re-arm"
                            )
                            self._bracket_child = None
                    try:
                        await self._place_entry_stop_limit(trigger)
                    except Exception as e:
                        self._log(f"[HEALTH] re-place entry failed: {e}")
            else:
                last_alerted_no_entry = False

            # ---- Probe 3: feed staleness during session
            if not self._paused and self._prev_ltp > 0:
                hb = getattr(self.gateway, '_last_heartbeat', None)
                if hb is not None:
                    age = (self._ts() - hb).total_seconds()
                    if age > STALE_FEED_SECONDS:
                        if not last_alerted_stale and self._alerts:
                            self._alerts.raise_alert(
                                code="STALE_FEED",
                                severity=AlertSeverity.HIGH,
                                message=f"No price update for {age:.0f}s during session.",
                                context={"age_s": age, "last_ltp": self._prev_ltp, "ticker": self.config.ticker},
                                correlation_id=self._cycle_id,
                            )
                            last_alerted_stale = True
                        # SELF-HEAL: a CONNECTED-but-deaf socket never trips
                        # the supervisor's heartbeat (isConnected() stays
                        # True), so nothing repaired it — 2026-06-09 sat deaf
                        # for 23h with this probe alerting into the void.
                        # Drive the same repair a real disconnect would.
                        self._maybe_repair_stale_feed(age)
                    else:
                        last_alerted_stale = False
                        self._last_stale_repair_ts = None  # feed healthy → re-arm

            # ---- Probe 4: equity drawdown (every 10 cycles = 5 min)
            equity_probe_counter += 1
            if equity_probe_counter >= 10 and self._session_start_equity > 0:
                equity_probe_counter = 0
                try:
                    current_equity = self.gateway.get_equity() if hasattr(self.gateway, 'get_equity') else 0.0
                    drawdown = (current_equity - self._session_start_equity) / self._session_start_equity
                    if drawdown < -0.05 and self._alerts:
                        self._alerts.raise_alert(
                            code="CIRCUIT_BREAKER_DRAWDOWN",
                            severity=AlertSeverity.CRITICAL,
                            message=f"Equity drawdown {drawdown*100:.1f}% from session start.",
                            context={
                                "session_start_equity": self._session_start_equity,
                                "current_equity": current_equity,
                                "drawdown_pct": drawdown * 100,
                            },
                            correlation_id=self._cycle_id,
                        )
                except Exception as e:
                    self._log(f"[HEALTH] equity probe failed: {e}")

    async def emergency_stop(self, reason: str = "MANUAL"):
        """Emergency stop - cancel all orders."""
        await self.gateway.cancel_all()
        self._running = False
        self._state = TradeState.EMERGENCY_STOP
        self._log(f"EMERGENCY STOP: {reason}")
        await self._log_json("EMERGENCY_STOP", reason=reason)
        self.audit("EMERGENCY_STOP", {"reason": reason})

    # === Manual Control Commands ===

    def pause(self) -> str:
        """Pause strategy - stop entries but continue monitoring."""
        if not self._running:
            return "Engine not running"
        if getattr(self, '_paused', False):
            return "Already paused"
        self._paused = True
        self._log("[MANUAL] Strategy PAUSED")
        return "Paused"

    def resume(self) -> str:
        """Resume strategy from paused state."""
        if not getattr(self, '_paused', False):
            return "Not paused"
        self._paused = False
        self._log("[MANUAL] Strategy RESUMED")
        return "Resumed"

    async def square_off(self) -> str:
        """Emergency: close ALL positions immediately at market price."""
        self._log("[MANUAL] EMERGENCY SQUARE-OFF triggered")
        if self._position_open and self._quantity > 0:
            await self._force_market_exit("MANUAL_SQUARE_OFF")
            return "Square-off executed"
        return "No position to square-off"

    async def force_exit(self) -> str:
        """Force exit current position at market price."""
        if not self._position_open:
            return "No position to exit"
        self._log("[MANUAL] Force exit triggered")
        await self._force_market_exit("MANUAL_EXIT")
        return "Force exit executed"

    async def _force_market_exit(self, reason: str) -> None:
        """Cancel the resting protective SL and submit a MARKET sell to flatten.

        Used by manual exit paths (square_off, force_exit). The proactive
        stop-limit placement makes `_exit → _place_protective_stop` a no-op
        once an SL is armed (idempotency guard), so manual exits MUST go
        through this dedicated path or they silently do nothing.

        Sequence:
            1. Cancel the resting SELL stop-limit at IBKR (if any) so we
               don't risk double-filling when the market order hits.
            2. Submit a MARKET SELL for the current position quantity.
            3. Flip state to EXIT_POSITION and save breakout level.

        Note on race: there's a small window (~50ms typical) between the
        cancel request and the broker acknowledging it, during which both
        the original SL AND the new market order could theoretically fill.
        For 1-share / small positions this is acceptable; for large
        positions, switch to cancel-then-wait via trade.cancelEvent.
        """
        if not self._position_open or not self._quantity:
            self._log(f"[{reason}] No open position — nothing to flatten")
            return

        qty = self._quantity

        # ── PRE-FLIGHT: don't MARKET-SELL into a flat broker position ──
        # If engine state diverged from broker truth (e.g. user closed
        # in TWS, fill event lost), this MARKET SELL would short us.
        # Pre-flight folds the engine to FLAT and refuses.
        if not await self._preflight_sell_allowed(
            int(qty), context=f"force_exit/{reason}"
        ):
            return

        ltp_for_log = getattr(self, '_prev_ltp', 0) or self._entry_price or 0

        # Step 1: cancel any resting protective SL
        pending = getattr(self, '_pending_stop', None)
        if pending:
            sl_id = pending.get('order_id')
            if sl_id:
                self._log(f"[{reason}] Cancelling resting SL {sl_id}")
                try:
                    await self.gateway.cancel_order(sl_id)
                except Exception as e:
                    self._log(f"[{reason}] SL cancel failed (continuing): {e}")
            self._pending_stop = None

        # Step 2: flip state + persist breakout level for re-entry on next cycle
        self._state = TradeState.EXIT_POSITION
        self._previous_breakout_level = self._highest_price
        self._pending_exit_reason = reason
        self._save_state()

        # Step 3: SHORT INVERSION (P3) — submit MARKET BUY to COVER. order_id
        # is unique-per-cycle so the registry doesn't collide with a prior
        # SL record. Use the _n{cycle_seq} suffix.
        market_id = self._make_engine_id(f"FORCE_{OrderSide.BUY.value}", qty)
        order = OrderRecord(
            order_id=market_id,
            symbol=self.config.ticker,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=ltp_for_log,
        )
        self.registry.submit(order)

        history_order = OrderRecord(
            order_id=market_id,
            symbol=self.config.ticker,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=ltp_for_log,
        )
        self._order_history.append(history_order)

        if self._audit:
            self._audit.log_order(
                event="SUBMITTED",
                order_id=market_id,
                side="BUY",
                qty=qty,
                order_type="MARKET",
                signal_price=ltp_for_log,
                reason=reason,
                state_at_time=self._state.value,
                position_at_time="SHORT",
            )

        self._log(f"[{reason}] Submitting MARKET BUY (COVER) {qty} {self.config.ticker} (LTP~${ltp_for_log:.2f})")

        try:
            await self.gateway.place_order(
                side=OrderSide.BUY,
                qty=qty,
                order_type=OrderType.MARKET,
                order_id=market_id,
            )
        except Exception as e:
            self._log(f"[{reason}] MARKET BUY (COVER) failed: {e}")
            # State rolled back so on_tick won't keep us in EXIT_POSITION forever
            self._state = TradeState.IN_POSITION

    async def cancel_all_orders(self) -> str:
        """Cancel all pending orders."""
        cancelled = await self.gateway.cancel_all()
        self._log(f"[MANUAL] Cancelled {cancelled} orders")
        return f"Cancelled {cancelled} orders"

    def get_summary(self) -> dict:
        """Get full trading summary."""
        return {
            "state": self._state.value,
            "position": "SHORT" if self._position_open else "FLAT",
            "entry_price": self._entry_price,
            "quantity": self._quantity,
            "unrealized_pnl": ((self._entry_price - self._prev_ltp) * self._quantity) if self._position_open and self._entry_price else 0,
            "realized_pnl": self._pnl,
            "trades_today": self._trades_today,
            "wins": self._wins,
            "losses": self._losses,
            "commission": self._total_commission,
            "paused": getattr(self, '_paused', False),
        }

    def on_tick(self, tick: "Tick") -> None:
        """SYNC entrypoint from the feed callback. Pushes the tick onto the
        bounded internal queue and returns immediately — the real strategy
        work runs in `_tick_consumer` on the event loop.

        Why sync + queue: the feed dispatch fires from the ib_async event
        loop. If we did the strategy work inline here, blocking calls
        (audit logging, broker RPCs from the reactive path) would stall
        market-data processing. If we did `asyncio.create_task(...)` like
        before, unbounded task creation would pile up under load. The
        bounded queue gives us backpressure: at most `maxsize` ticks
        pending, drop-oldest on full so the strategy always sees fresh
        market state. Engine guarantees: invoke once per tick, no
        re-entrancy on `_process_tick`.
        """
        if not self._running or getattr(self, '_paused', False):
            return
        if self._tick_queue is None:
            return  # start() not yet called

        try:
            self._tick_queue.put_nowait(tick)
        except asyncio.QueueFull:
            # Drop oldest, enqueue newest — never let stale ticks block fresh ones.
            try:
                self._tick_queue.get_nowait()
                self._ticks_dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._tick_queue.put_nowait(tick)
            except asyncio.QueueFull:
                self._ticks_dropped += 1

    async def _tick_consumer(self) -> None:
        """Single consumer task that drains the tick queue and runs the
        strategy loop. Replaces the previous one-task-per-tick pattern."""
        while self._running:
            try:
                tick = await asyncio.wait_for(self._tick_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await self._process_tick(tick)
            except Exception as e:
                # Don't let one bad tick kill the consumer
                self._log(f"_process_tick error: {e}")

    def _tick_to_snapshot(self, tick: "Tick"):
        """Adapt the engine's Tick struct to the spec layer's FeedSnapshot.

        Done at the per-tick boundary so the rest of the engine can keep
        passing simple float prices around (asset semantics are encoded
        by WHICH spec method we ask for the float).

        Critical: Tick uses 0.0 as its "missing" sentinel. For FX the
        per-tick `tick.last` is ALWAYS 0.0 (IDEALPRO is quote-driven).
        We convert 0.0 → None here so the spec's price policy can fall
        through to bid/ask instead of treating zero as a real price.
        """
        import math
        from src.assets.policies import FeedSnapshot
        from src.assets.types import price as _to_price

        def _nz(v):
            if v is None:
                return None
            if isinstance(v, float) and (v == 0.0 or math.isnan(v)):
                return None
            return _to_price(v)

        return FeedSnapshot(
            bid=_nz(tick.bid),
            ask=_nz(tick.ask),
            last=_nz(tick.last),
            bid_size=tick.bid_size or None,
            ask_size=tick.ask_size or None,
            last_size=None,         # Tick doesn't carry last_size separately
            volume=tick.volume or None,
            high=_nz(tick.high),
            low=_nz(tick.low),
            vwap=None,              # not in Tick
            ts=None,                # feed handler already filters stale ticks
        )

    async def _process_tick(self, tick: "Tick"):
        """
        Process tick — core strategy loop.

        Multi-asset (D4-AM): the "what price do I read?" question is
        asset-class-dependent. Equity has reliable tick.last and uses
        it for all three (track / buy-compare / sell-compare). Forex
        has NO real tick.last (IDEALPRO is quote-driven) so we read
        ask for BUY-trigger comparisons, bid for SELL-stop comparisons,
        and mid for neutral tracking / display. The AssetSpec's price
        policy encapsulates that asymmetry; here we ask for the right
        side per call.

        Backward compat: when self._asset_spec is None (resolver
        miss — shouldn't happen in production), fall back to the
        legacy single `tick.last` read so nothing crashes.
        """
        if not self._running or getattr(self, '_paused', False):
            return

        # ── Resolve the three price sides via AssetSpec ──────────────
        if self._asset_spec is None:
            # Legacy / fallback path — preserve previous behavior
            ref_px = tick.last
            buy_px = tick.last
            sell_px = tick.last
        else:
            snapshot = self._tick_to_snapshot(tick)
            try:
                ref_px = float(self._asset_spec.price.reference(snapshot))
                buy_px = float(self._asset_spec.price.buy_compare(snapshot))
                sell_px = float(self._asset_spec.price.sell_compare(snapshot))
            except Exception:
                # NoUsablePrice or similar — snapshot doesn't carry the
                # field this asset needs (e.g. FX with no bid yet). Skip
                # this tick; next one likely has it. Better silent skip
                # than a state machine that processes garbage.
                return

        if ref_px <= 0:
            return  # Invalid price

        # Log feed tick (non-blocking)
        if self._audit:
            self._audit.log_feed(tick)

        # Log state + PnL on every tick. Use the neutral reference
        # price (mid for FX, last for equity) so the audit log reads
        # consistently across assets.
        if self._audit:
            self._log_state_and_pnl(ref_px)

        # SHORT INVERSION (P7): the protective stop is a BUY cover, which
        # pays the ASK (buy_px). So _track_position + _check_stop_limit
        # evaluate against buy_px in the IN_POSITION branch (long used the
        # bid). The stop fires when the ask RISES to the BUY-STOP trigger.
        if self._state == TradeState.IN_POSITION:
            await self._track_position(buy_px, tick)
            if hasattr(self, '_pending_stop') and self._pending_stop:
                await self._check_stop_limit(buy_px)

        # Cover (BUY) submitted but not yet filled — keep tracking the
        # trough so the re-entry breakdown level reflects the full holding
        # period. The stop check intentionally does NOT run here: the exit
        # is already in flight at the broker and re-running it would place
        # duplicate stop-limits.
        elif self._state == TradeState.EXIT_POSITION:
            self._track_high(ref_px)

        # SHORT INVERSION (P7): pre-position SELL breakdown trigger. Use
        # the SELL-side price (bid for FX, last for equity) — the short
        # entry fires when the bid FALLS to the trigger because that's
        # what we'd actually RECEIVE selling short.
        elif self._state in (TradeState.MONITORING, TradeState.WAITING_REENTRY):
            if getattr(self, '_pending_stop', None):
                await self._check_stop_limit(sell_px)

        # Update prev_ltp for downstream reads (high-water, audit,
        # status display, run_live snapshot). Use reference price —
        # equity gets last; FX gets mid.
        self._prev_ltp = ref_px

        # NOTE: state save used to run every 10 ticks here, blocking the
        # event loop on sync disk I/O (~1000 writes/sec at NVDA tick rates,
        # which was the main source of order-placement jitter). The save
        # is now event-driven: on fills, on new highs (see _track_high),
        # on state transitions, and on shutdown. StateStore.save() itself
        # is now non-blocking (background writer thread), so the few saves
        # that do happen don't stall the hot path either.

    def _log_state_and_pnl(self, ltp: float):
        """Log state and PnL — throttled.

        Was firing on EVERY tick (10K+/sec on NVDA), which filled the
        bounded audit queues in milliseconds and made the writer threads
        churn through redundant rows. PnL only changes meaningfully at
        5-second granularity for analytics, and state-change rows are
        more valuable than tick-by-tick repeats of the same state.

        Strategy:
            log_pnl — gated by AuditManager.should_log_pnl() (5s default).
            log_state — only on actual state transitions or new-high events
            (caller decides). Removed from the on_tick hot path.

        The full tick-by-tick stream is still captured by `log_feed` for
        market reconstruction; this just trims the redundant state/pnl
        twins.
        """
        # PnL — throttled to should_log_pnl interval (default 5s)
        if not self._audit.should_log_pnl():
            return

        # Calculate unrealized P&L at the snapshot moment.
        # SHORT INVERSION (P8): a short gains as price falls → (entry - ltp).
        unrealized = 0.0
        if self._position_open and self._entry_price:
            unrealized = (self._entry_price - ltp) * self._quantity

        self._audit.log_pnl(
            state=self._state,
            position_open=self._position_open,
            entry_price=self._entry_price,
            current_price=ltp,
            unrealized_pnl=unrealized,
            realized_pnl=self._pnl,
            total_pnl=self._pnl + unrealized,
            wins=self._wins,
            losses=self._losses,
            trades_today=self._trades_today,
            comm_today=self._total_commission,
            highest_price=self._highest_price,
            stop_loss=self._stop_loss,
        )

        # State snapshot piggy-backs on the same 5s cadence — gives full
        # context alongside each PnL row in the audit log.
        self._audit.log_state(
            event="SNAPSHOT",
            state=self._state,
            ltp=ltp,
            prev_ltp=self._prev_ltp,
            position_open=self._position_open,
            entry_price=self._entry_price,
            highest_price=self._highest_price,
            stop_loss=self._stop_loss,
            trigger_price=self.config.trigger_price,
            breakout_level=self._previous_breakout_level,
            pnl=self._pnl,
            trades_today=self._trades_today,
            wins=self._wins,
            losses=self._losses,
            config_trigger=self.config.trigger_price,
            config_stop_pct=self.config.stop_loss_pct,
            config_qty=self.config.quantity,
        )

    def _track_high(self, ltp: float):
        """SHORT INVERSION (P4/P5): track the cycle LOW (trough), not the high.

        NAMING NOTE: the method + field names (`_track_high`,
        `_highest_price`, `_feed_high_at_entry`, `_previous_breakout_level`)
        are intentionally LEFT UNCHANGED so the state-file schema, dashboard,
        reconcile, and gap-fill code keep working without a sweeping rename.
        For the short strategy they now hold the LOWEST price seen this cycle,
        which becomes the next re-entry (breakdown) trigger. Read "high" as
        "tracked extreme" throughout. See SHORT_CONVERSION_CHANGES.md.

        Original doc (long): Update _highest_price if a new peak is detected.

        Called while the position is open (both IN_POSITION and EXIT_POSITION,
        the latter being the window between SELL submission and SELL fill).
        The peak across the whole holding period is what feeds the re-entry
        breakout level on SELL fill.

        Two sources are considered:

        1. **LTP** (our tick-by-tick TRADE feed). The standard signal — every
           trade print we receive.

        2. **`feed.high`** (IBKR's BBO-aggregated daily-high field). IBKR
           publishes a daily-high snapshot on the BBO ticker that includes
           prints across ALL reporting venues — including venues whose
           tick-by-tick TRADE stream we don't subscribe to. If `feed.high`
           has RISEN above `_feed_high_at_entry` (the value at the moment
           of cycle entry), the rise can only have come from a trade
           DURING this cycle, so it's safe to adopt as the cycle peak.

        Confirmed real on 2026-06-01 (IBM cycle n3 15:52:46–15:54:46):
            • Max LTP captured by `_track_high`:  $327.91
            • Max `feed.high` during cycle:       $327.98
            • Difference: 7 cents under-tracked → next re-entry trigger
              was set $0.07 too low.

        Persists on every new high — `_highest_price` is the one field that
        can't be reconstructed from broker state on restart (the broker
        knows positions and resting orders, but not "peak since fill").
        New highs are rare relative to ticks, and StateStore.save() is now
        non-blocking, so this is cheap.
        """
        candidate = ltp
        # SHORT INVERSION (P4): augment with IBKR's aggregated daily-LOW if
        # it has fallen BELOW the per-cycle baseline. Guard on _position_open
        # so this never leaks pre-cycle lows into _highest_price (the
        # baseline being whatever feed.low was at the moment of entry).
        if (
            self._position_open
            and self._feed is not None
        ):
            try:
                fl = float(self._feed.low or 0)
                # 0 is the "no data yet" sentinel for feed.low — ignore it.
                if fl > 0 and fl < (self._feed_high_at_entry or float('inf')) and fl < candidate:
                    candidate = fl
            except Exception:
                # Defensive — never let extreme-tracking math break the engine.
                pass
        # SHORT: a NEW low (smaller) is the meaningful extreme. Sentinel is
        # +inf so the first candidate always registers.
        if candidate < (self._highest_price if self._highest_price is not None else float('inf')):
            self._highest_price = candidate
            self._log(f"New low: ${candidate:.2f}")
            self._save_state()

    async def _track_position(self, ltp: float, tick: "Tick"):
        """
        Track open position.

        Per doc section 9: Update highest LTP
        Per doc section 10: Check stop loss (reactive backup only)
        """
        self._track_high(ltp)

        # Per doc section 10: Reactive stop-loss check — only fires if the
        # proactive SL placement on BUY fill did NOT succeed (network error,
        # async loop missing, etc.). In normal operation _pending_stop is
        # set the moment the SL is armed at the broker, so this branch is
        # a no-op and IBKR drives the exit fill via fillEvent.
        #
        # `self._position_open` gate is defensive: if engine state has
        # already transitioned to FLAT but _stop_loss hasn't been cleared
        # yet (theoretical asyncio interleaving, or the auto-FLAT fold
        # cleared _pending_stop and _quantity but a stray _stop_loss
        # value remains from a partial cleanup), we DON'T want this
        # branch to fire _exit → _place_protective_stop on a FLAT
        # position. The pre-flight broker-qty check inside
        # _place_protective_stop_inner would catch that, but stopping
        # one layer earlier is cheaper and clearer.
        # SHORT INVERSION (P6): a short's stop fires when price RISES to
        # the BUY-STOP trigger, so the reactive backup compares ltp >=
        # stop (long compared ltp <= stop). `ltp` here is buy_px (the ask
        # we'd pay to cover) per the IN_POSITION branch in on_tick.
        if (
            self._position_open
            and self._stop_loss
            and ltp >= self._stop_loss
            and not getattr(self, '_pending_stop', None)
            # Fencing-token gate (SHORT mirror of LONG EURUSD naked-short
            # 2026-07-28): this reactive fallback fires on the engine's BELIEF
            # of IN_POSITION (short). After a reconnect, before reconcile folds
            # a phantom position, that belief can be stale — firing _exit here
            # would BUY-to-cover into a flat book (naked LONG via the tick
            # path, the parallel door to the health re-arm). Only act once the
            # ledger is reconciled for the current connection epoch. Fail-safe:
            # defer until then.
            and self._actuation_allowed()
        ):
            self._log(f"STOP TRIGGERED (no active SL — reactive fallback): ltp={ltp:.2f} >= stop={self._stop_loss:.2f}")
            await self._exit(ltp, "STOP_LOSS", tick)

    async def _check_stop_limit(self, ltp: float):
        """
        Check if pending stop-limit order is triggered.

        Paper-mode simulator. In live mode `_pending_stop` is still set as
        a placement-tracking marker (to prevent double-placement), but the
        actual fill is driven by IBKR's `fillEvent` — running the simulator
        here would double-count the exit.

        Direction (SHORT regime — roles swapped vs long, geometry identical):
            SELL stop-limit (ENTRY/breakdown):   triggers when ltp <= stop_price.
            BUY  stop-limit (protective COVER):   triggers when ltp >= stop_price.
        The side→comparison mapping below is unchanged because it already
        keys purely on order side; only which side means "entry" vs
        "protective" flips for shorts.
        """
        if not self.gateway.paper:
            return
        pending = getattr(self, '_pending_stop', None)
        if not pending:
            return

        stop_price = pending.get('stop_price', 0)
        if not stop_price:
            return

        side = pending.get('side', OrderSide.SELL)
        if side == OrderSide.SELL:
            triggered = ltp <= stop_price
        else:  # BUY
            triggered = ltp >= stop_price

        if triggered:
            # Order type STP fills at market on trigger; in paper we
            # approximate "market" with stop_price (with the tiny slip
            # already baked into _paper_stop_market). STP-LMT pre-bracket
            # used limit_price (which equals stop_price - offset). When
            # limit_price is None (the new STP-MARKET path), fall back
            # to stop_price so the paper fill price is still defined.
            limit_price = pending.get('limit_price')
            fill_price = limit_price if limit_price is not None else stop_price
            ord_type = pending.get('order_type') or 'STP'
            self._log(f"{ord_type} TRIGGERED ({side.value}): ltp={ltp:.2f} vs stop={stop_price:.2f}")
            self._on_gateway_fill(pending['order_id'], pending['qty'], fill_price)
            self._pending_stop = None

    def _compute_limit_offset(
        self, anchor_price: float, stop_distance: Optional[float] = None
    ) -> float:
        """Compute the trigger↔limit buffer for a stop-limit order.

        Scales with context so the same engine works across the price
        spectrum ($5 microcap to $7,500 BRK.A) without a one-size-fits-all
        $0.05 that's too wide for cheap stocks and useless for expensive ones.

        Modes (selected by caller via `stop_distance` argument):
          SELL exit  (stop_distance given):   buffer = fraction × stop_distance
              Wider stops → wider buffers automatically. Couples to your
              risk tolerance rather than to absolute price.
          BUY entry  (stop_distance is None): buffer = pct × anchor_price
              Scales with price level (5 bps of $230 = $0.12; of $500 = $0.25),
              wide enough to bridge typical spreads at any price tier.

        Both modes are floored at `config.sl_limit_offset` (default $0.05 for
        equity; user overrides via --offset-fixed). The result is snapped to
        the asset's tick grid: 0.01 for US equity (legacy byte-identical),
        0.00005 for EURUSD/GBPUSD/etc., 0.005 for JPY pairs, 0.25 for ES, etc.

        BUG HISTORY: was previously `round(..., 2)`. On FX that collapsed a
        user-supplied 0.0005 offset to 0.00, producing limit_price == stop_price
        on every BUY STP-LMT — which IDEALPRO either rejects or leaves resting
        unfillable. Now uses `_round_to_tick` which delegates to the spec's
        PipTickPolicy (FX) / DecimalTickPolicy (equity).

        Set the fraction/pct configs to 0 to disable scaling and fall back
        to a flat `sl_limit_offset` buffer (legacy behavior).
        """
        cfg = self.config
        floor = getattr(cfg, 'sl_limit_offset', 0.05)

        if stop_distance is not None:
            fraction = getattr(cfg, 'offset_stop_fraction', 0.05)
            candidate = fraction * stop_distance
        else:
            pct = getattr(cfg, 'offset_entry_pct', 0.0005)
            candidate = pct * anchor_price

        snapped = self._round_to_tick(max(floor, candidate))
        # Defensive: if the tick rounder snaps to 0 (shouldn't happen with
        # a positive floor, but pathological configs are easy to imagine),
        # fall back to the floor itself so the BUY STP-LMT never gets
        # placed with limit_price == stop_price.
        if snapped <= 0 and floor > 0:
            return floor
        return snapped

    async def _place_entry_stop_limit(self, trigger_price: float) -> Optional[str]:
        """Atomic-claim wrapper around `_place_entry_stop_limit_inner`.

        Mirrors the proven `_place_protective_stop` / `_place_protective_stop_inner`
        split. Synchronous read+write of two guard flags BEFORE any await — a
        concurrent caller that arrives between this block and our eventual
        `_pending_stop` assignment trips the guard and early-returns. Without
        this, two trigger crossings in the same millisecond can both pass the
        `_pending_stop is None` check below and each submit a separate bracket.

        Live regression 2026-06-09 EURUSD: after n3 SL fired, two trigger
        checks fired within 1ms; both passed the guard; n4 AND n5 brackets
        submitted; both BUYs filled (50k EUR exposure); n4 SL fired closing
        n4, n5 SL fired but engine state was already WAITING_REENTRY → SELL
        recorded as PHANTOM_SELL_REJECTED. Net broker position was correct
        (flat) but audit log noisy. This wrapper eliminates the race entirely.

        Cleared in `finally` so a raised exception (network, IBKR error)
        doesn't leave the flag stuck blocking all future entries.
        """
        if trigger_price is None or trigger_price <= 0:
            return None
        # ATOMIC GUARD — synchronous claim before any await.
        #
        # Checks THREE flags (any one set ⇒ refuse the entry):
        #   _pending_stop     — a SELL stop is resting (standalone SL OR
        #                       bracket child promoted on parent fill)
        #   _entry_placing    — A37: another _place_entry_stop_limit call
        #                       is currently in flight in the same task chain
        #   _bracket_child    — A39: a bracket parent BUY is at the broker
        #                       but parent hasn't filled yet (_pending_stop
        #                       won't be set until promotion on fill).
        #                       Without this check, a tick arriving 100-500ms
        #                       after first bracket submission can place a
        #                       SECOND bracket — observed live 2026-06-09 on
        #                       USDCHF (n3 + n4 both at broker, both filled,
        #                       only n1/n2 SELLs ever fired → naked +50k CHF).
        if (getattr(self, '_pending_stop', None)
                or getattr(self, '_entry_placing', False)
                or getattr(self, '_bracket_child', None)):
            return None
        self._entry_placing = True
        try:
            return await self._place_entry_stop_limit_inner(trigger_price)
        finally:
            # Always release — even if placement raised — so the next
            # legitimate attempt can proceed.
            self._entry_placing = False

    async def _place_entry_stop_limit_inner(self, trigger_price: float) -> Optional[str]:
        """SHORT INVERSION (P1): place a SELL stop-limit to enter on breakdown DOWN.

        The strategy wants to SELL SHORT only when LTP **falls** to
        `trigger_price`. A plain SELL LIMIT would fill immediately at any bid
        >= trigger (i.e. fill near the current market, often well above the
        intended breakdown — the trap the long code documented in reverse).
        A SELL STOP-LIMIT rests at the broker and activates only when LTP
        falls to the trigger.

        Geometry (mirror of the long BUY entry, flipped):
            stop_price  = trigger_price             (activates on breakdown down)
            limit_price = trigger_price - sl_offset (floor we'll accept; IBKR
                          requires lmtPrice <= stopPrice for a SELL stop-limit)

        Idempotent: bails if `_pending_stop` already holds a resting order,
        so restart races (reconcile then start) can't double-place. Returns
        the order_id on success.
        """
        if trigger_price is None or trigger_price <= 0:
            return None
        if getattr(self, '_pending_stop', None):
            return None

        # CRITICAL: refuse to place orders while disconnected. Without
        # this, the engine would attempt placement during a TWS-killed
        # window — the order would fail in confusing ways and downstream
        # state would be inconsistent. (Live 2026-06-06.)
        if not getattr(self.gateway, 'connected', False):
            # Try to recover stale disconnect status first.
            recovered = self._try_recover_connection_status()
            if not recovered:
                self._log(
                    "Entry placement SKIPPED: gateway DISCONNECTED. "
                    "Will retry once connection recovers."
                )
                return None

        # Session gate: refuse to place NEW entries outside ETH window
        # (04:00-20:00 ET Mon-Fri) or while the engine is manually paused.
        # Protective SL placement (_place_protective_stop) is exempt — we
        # always protect an open position regardless of session.
        #
        # Two independent gates:
        #   (a) manual pause via Ctrl+\ (operator decision)
        #   (b) automatic ETH-window gate (rth-only mode)
        #
        # NOTE: do NOT AND these with `self._paused` to compute "outside
        # session". The session controller task sets _paused asynchronously,
        # and start()'s entry placement can race ahead of the controller's
        # first run when reconcile has no awaits to yield on. Use
        # session_is_open() directly — it's a pure function, race-free.
        # (We also sync _paused below so the dashboard / health check see
        # a consistent state.)
        if self._paused:
            self._log("Entry placement SKIPPED: engine paused")
            return None
        if self._rth_only and not self._session_is_open():
            self._paused = True  # keep _paused consistent with reality
            self._log("Entry placement SKIPPED: outside RTH session window (Mon-Fri ET)")
            return None
        # End-of-session entry cutoff: refuse new BUYs within the last
        # `ENTRY_CUTOFF_BUFFER_MIN` minutes of close. Guarantees the
        # protective SELL stop has time to land before market shuts —
        # the entire reason the RTH wind-down buffer exists. The
        # session controller will also actively cancel any working
        # BUY orders when this transitions True→False.
        if self._rth_only and not self._entries_allowed():
            self._log(
                f"Entry placement SKIPPED: end-of-session cutoff "
                f"(no new entries in last {ENTRY_CUTOFF_BUFFER_MIN} min — "
                f"reserved for protective-stop placement)"
            )
            return None

        # IMPORTANT: re-entries always target the originally-configured
        # quantity, NOT the partial qty from a prior cycle. If a previous
        # entry only managed to fill 95 of 100 (chase gave up), the NEXT
        # cycle still aims for the full 100 — `self.config.quantity` is
        # the source of truth, set once at startup from --qty / env var /
        # state file restore, and never modified by partial-fill outcomes.
        qty = self.config.quantity

        # PRE-TRADE AVAILABILITY GATE (short-only) — HARD BLOCK on a
        # NOT-SHORTABLE name. Uses IBKR's live shortability (tick 236)
        # captured in `_short_shortable` by preview_short_shortable().
        # Only blocks on a DEFINITIVE `available is False`; a missing/None
        # value (feed not entitled — e.g. a standalone paper account, or
        # the connect-time fetch hasn't landed yet) FAILS OPEN so we never
        # refuse every short just because the feed is unavailable. Gated on
        # the asset's ShortPolicy requiring a locate, so CFD/FX shorts —
        # which never borrow real shares — skip this entirely.
        _spec = getattr(self, '_asset_spec', None)
        _short_pol = getattr(_spec, 'short', None) if _spec is not None else None
        if _short_pol is not None and getattr(_short_pol, 'requires_locate', False):
            _avail = getattr(self, '_short_shortable', None) or {}
            if _avail.get('available') is False:
                _shares = _avail.get('shortable_shares')
                self._log(
                    f"ENTRY BLOCKED — {self.config.ticker} NOT SHORTABLE at IBKR "
                    f"(shortable_shares={_shares}); refusing short entry "
                    f"(trigger=${trigger_price:.2f}, qty={qty})"
                )
                rej_id = f"NOSHORT_{OrderSide.SELL.value}_{qty}_{self.config.ticker}_{self._cycle_id}"
                if self._audit:
                    self._audit.log_order(
                        event="REJECTED",
                        order_id=rej_id,
                        side="SELL",
                        qty=qty,
                        order_type="STOP_LIMIT",
                        signal_price=trigger_price,
                        reason="NOT_SHORTABLE (IBKR availability)",
                        state_at_time=self._state.value,
                        position_at_time="FLAT",
                    )
                self._order_history.append(OrderRecord(
                    order_id=rej_id,
                    symbol=self.config.ticker,
                    side=OrderSide.SELL,
                    qty=qty,
                    order_type=OrderType.STOP_LIMIT,
                    stop_price=trigger_price,
                    limit_price=trigger_price,
                    status=OrderStatus.REJECTED,
                    submitted_at=self._ts(),
                    signal_price=trigger_price,
                ))
                # Back off like an entry rejection so the health-check probe
                # doesn't re-spam placement every 30s into a known-bad name.
                self._last_entry_rejected_at = self._ts()
                self._last_entry_rejected_reason = "NOT_SHORTABLE"
                return None

        # PRE-TRADE RISK GATE — runs O(1) against cached values, target
        # <0.001ms. Blocks the entry if any of: position-size > 95% equity,
        # daily loss > limit, consecutive losses >= max, trades today >=
        # max, or price feed is stale. Without this gate the strategy
        # would happily keep placing entries past every safety threshold.
        if self.risk:
            result = self.risk.check(trigger_price, qty)
            if not result.allowed:
                self._log(f"ENTRY BLOCKED BY RISK: {result.reason} (trigger=${trigger_price:.2f}, qty={qty})")
                rej_id = f"RISK_BLOCK_{OrderSide.SELL.value}_{qty}_{self.config.ticker}_{self._cycle_id}"
                if self._audit:
                    self._audit.log_order(
                        event="REJECTED",
                        order_id=rej_id,
                        side="SELL",
                        qty=qty,
                        order_type="STOP_LIMIT",
                        signal_price=trigger_price,
                        reason=f"RISK: {result.reason}",
                        state_at_time=self._state.value,
                        position_at_time="FLAT",
                    )
                # Surface in dashboard order panel too (audit-only meant
                # silent rejection from the user's perspective).
                self._order_history.append(OrderRecord(
                    order_id=rej_id,
                    symbol=self.config.ticker,
                    side=OrderSide.SELL,
                    qty=qty,
                    order_type=OrderType.STOP_LIMIT,
                    stop_price=trigger_price,
                    limit_price=trigger_price,
                    status=OrderStatus.REJECTED,
                    submitted_at=self._ts(),
                    signal_price=trigger_price,
                ))
                # Circuit-breaker: the portfolio daily-loss gate signals
                # terminal for *new entries*, not for the whole engine.
                # We set `_paused = True` so every subsequent entry attempt
                # short-circuits without re-running the gate, but leave the
                # engine itself running so its existing protective-stop
                # management, partial-fill chase, reconnect handling, and
                # state persistence all continue working on whatever
                # position is currently open. The operator decides when
                # to fully shut down (Ctrl+C / explicit stop command).
                if getattr(result, 'circuit_break', False) and not self._paused:
                    self._paused = True
                    self._log(
                        f"CIRCUIT BREAKER TRIPPED: {result.reason} — engine PAUSED "
                        f"(no new entries; existing position management continues)"
                    )
                    if self._audit:
                        self._audit.log_order(
                            event="CIRCUIT_BREAK",
                            order_id=rej_id,
                            side="SELL",
                            qty=qty,
                            order_type="STOP_LIMIT",
                            signal_price=trigger_price,
                            reason=f"CIRCUIT_BREAK: {result.reason}",
                            state_at_time=self._state.value,
                            position_at_time="SHORT" if self._position_open else "FLAT",
                        )
                return None

        # Entry buffer scales with absolute price (e.g. 5 bps × trigger), floored.
        # SHORT INVERSION (P1): SELL stop-limit limit sits BELOW the trigger
        # (long BUY put it above). IBKR requires lmtPrice <= stopPrice for SELL.
        sl_offset = self._compute_limit_offset(trigger_price)
        stop_price = self._round_to_tick(trigger_price)
        limit_price = self._round_to_tick(stop_price - sl_offset)

        # MARKET entry (--market): the breakdown was detected upstream, so there
        # is nothing left to trigger on. stop/limit are still computed above —
        # they are recorded on the OrderRecord as the level this entry was
        # *about*, and the child's protective cover is sized from the trigger —
        # but neither is sent to IBKR for the parent leg.
        #
        # ONE-SHOT, and the guard matters more than the feature: this function
        # also places the re-entry after a cover and the session-open entry.
        # Those target a breakdown level price has NOT reached — firing them at
        # market would short instantly at the inflated post-cover bid. Only the
        # first entry of the process, the one the upstream signal launched us
        # for, may go in at market.
        entry_market = (
            bool(getattr(self.config, 'entry_market', False))
            and not self._market_entry_used
        )
        entry_order_type = OrderType.MARKET if entry_market else OrderType.STOP_LIMIT
        if entry_market:
            self._market_entry_used = True

        # ── Bump cycle counter BEFORE constructing any engine_id ────────
        # Each entry attempt gets a fresh `_n{seq}` suffix on all three
        # legs (ENTRY parent, BR_SELL child, SL_SELL fallback). Without
        # this bump, a leftover order from the previous cycle would share
        # an engine_id with the new cycle's order, and a stale broker fill
        # could be misattributed (2026-05-27 TSLA $44k phantom-PnL bug).
        # Persist immediately so a restart between bump and order placement
        # doesn't reuse the same seq on the retry.
        self._cycle_seq += 1
        try:
            self._save_state()
        except Exception as e:
            self._log(f"[CYCLE-SEQ] save_state after bump failed (non-fatal): {e}")

        # SHORT INVERSION (P1): entry order is now a SELL stop-limit.
        order_id = self._make_engine_id(f"ENTRY_{OrderSide.SELL.value}", qty)

        order = OrderRecord(
            order_id=order_id,
            symbol=self.config.ticker,
            side=OrderSide.SELL,
            qty=qty,
            order_type=entry_order_type,
            stop_price=stop_price,
            limit_price=limit_price,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=stop_price,
        )
        self.registry.submit(order)

        history_order = OrderRecord(
            order_id=order_id,
            symbol=self.config.ticker,
            side=OrderSide.SELL,
            qty=qty,
            order_type=entry_order_type,
            stop_price=stop_price,
            limit_price=limit_price,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=stop_price,
        )
        self._order_history.append(history_order)

        if self._audit:
            self._audit.log_order(
                event="SUBMITTED",
                order_id=order_id,
                side="SELL",
                qty=qty,
                order_type=entry_order_type.name,
                stop_price=stop_price,
                limit_price=limit_price,
                signal_price=stop_price,
                state_at_time=self._state.value,
                position_at_time="FLAT",
            )

        # ── Bracket placement (live mode) ──────────────────────────────
        # SHORT INVERSION (P1/P3): submit BOTH parent SELL STP-LMT (breakdown
        # entry) AND child BUY STP (market on trigger — protective cover)
        # atomically. The child rests at the broker the instant the bracket
        # is accepted — so even if this engine instance dies before the
        # parent fills, the cover is already armed at IBKR.
        #
        # Child's initial stop_price is computed from `trigger_price`
        # (the SELL's intended fire level) — best estimate before we
        # know actual fill VWAP. After parent fills, `_on_gateway_fill`
        # SELL branch calls `gateway.modify_stop_trigger` to retarget the
        # child to `avg_fill_price × (1 + stop_loss_pct)`.
        #
        # ── FREEZE the SL pct for this cycle at submission time ────────
        # Capture the CURRENT config.stop_loss_pct as `_active_stop_pct`
        # right when we submit the bracket. From this point until the
        # cycle's SELL fills, every SL calc reads `_effective_stop_pct()`
        # which returns the frozen value. If the engine is restarted
        # mid-cycle, the saved state's `stop_loss_pct` field carries the
        # frozen value into the new process — so a different --stop-pct
        # on the restart command can NEVER silently relocate this
        # cycle's protective stop. The freeze is cleared on SELL fill.
        self._active_stop_pct = float(self.config.stop_loss_pct)
        stop_pct = self._effective_stop_pct()
        # Exact integer-tick math (Decimal × Decimal, snap in tick space)
        # so the bracket child stop is GUARANTEED on the venue's grid
        # with no float drift. See `_protective_stop_price` docstring.
        child_initial_stop = self._protective_stop_price(trigger_price, stop_pct)
        # SHORT INVERSION: protective child is now a BUY cover.
        child_order_id = self._make_engine_id(f"BR_{OrderSide.BUY.value}", qty)

        self._log(
            (f"ENTRY BRACKET: parent SELL MARKET (--market; breakdown already "
             f"detected upstream at ${stop_price:.2f}, no resting trigger, "
             f"NO price floor)"
             if entry_market else
             f"ENTRY BRACKET: parent SELL STP-LMT trigger=${stop_price:.2f} "
             f"limit=${limit_price:.2f}")
            + f"; child BUY STP-MKT trigger=${child_initial_stop:.2f} "
              f"(initial — modified to fill-px × (1+{stop_pct*100:.2f}%) "
              f"when parent fills)"
        )

        # Wrap bracket placement so a transient broker failure (child placeOrder
        # raises after parent succeeds — broker.py A40 cancels the orphan
        # parent and re-raises) doesn't propagate as an unhandled exception.
        # Returning None signals "placement skipped, try again next tick" —
        # the wrapper's `finally` clears _entry_placing, so the next trigger
        # crossing reattempts cleanly. Live regression 2026-06-09 USDCHF.
        try:
            bracket_ids = await self.gateway.place_bracket_sell_stop_market(
                qty=qty,
                parent_stop_price=stop_price,
                parent_limit_price=limit_price,
                child_stop_price=child_initial_stop,
                parent_order_id=order_id,
                child_order_id=child_order_id,
                parent_market=entry_market,
            )
        except Exception as e:
            self._log(
                f"[ENTRY] Bracket placement FAILED ({type(e).__name__}: {e}) — "
                f"broker.py cancelled the orphan parent, no orders should be "
                f"left at IBKR. Engine will retry on next trigger crossing."
            )
            # Audit so post-mortems can correlate; non-fatal.
            if self._audit:
                try:
                    self._audit.log_order(
                        event="BRACKET_PLACE_FAILED",
                        order_id=order_id,
                        side="SELL",
                        qty=qty,
                        stop_price=stop_price,
                        limit_price=limit_price,
                        reason=f"{type(e).__name__}: {str(e)[:200]}",
                    )
                except Exception:
                    pass
            return None

        # Bracket accepted (or paper-mode legacy fallback below). Either
        # way the engine has shipped a fresh entry — clear any prior
        # rejection record so the health-check Probe 2 backoff lifts.
        # The rejection record will be re-set if IBKR rejects THIS one.
        self._last_entry_rejected_at = None
        self._last_entry_rejected_reason = None

        if bracket_ids is None:
            # Paper mode (bracket unsupported there) — fall back to the
            # legacy single-order placement. The protective SL gets
            # armed in the BUY branch's `is_complete=True` path as
            # before, accepting the small naked window that paper trading
            # already tolerates.
            # SHORT INVERSION (P1): paper entry leg is a SELL stop-limit.
            fill_price = await self.gateway.place_stop_limit(
                side=OrderSide.SELL,
                qty=qty,
                stop_price=stop_price,
                limit_price=limit_price,
                order_id=order_id,
            )
            if fill_price is None:
                self._pending_stop = {
                    'order_id': order_id,
                    'qty': qty,
                    'stop_price': stop_price,
                    'limit_price': limit_price,
                    'side': OrderSide.SELL,
                }
            else:
                self._log(f"ENTRY STOP_LIMIT FILLED @ ${fill_price:.2f} (paper)")
            return order_id

        # Live mode: bracket accepted by IBKR. Track BOTH legs.
        # SHORT INVERSION (P1/P3):
        #   - _pending_stop  = parent SELL (the resting breakdown entry)
        #   - _bracket_child = child BUY (protective cover; promoted to
        #     _pending_stop when the parent SELL fills)
        self._pending_stop = {
            'order_id': order_id,
            'qty': qty,
            'stop_price': stop_price,
            'limit_price': limit_price,
            'side': OrderSide.SELL,
            'bracket_parent': True,  # marker so downstream code can tell
        }
        self._bracket_child = {
            'order_id': child_order_id,
            'qty': qty,
            'stop_price': child_initial_stop,
            'side': OrderSide.BUY,
            'order_type': 'STP',  # market-on-trigger
            'parent_order_id': order_id,
        }

        # Register the child in the OrderRegistry so fill callbacks
        # have somewhere to land when the child eventually triggers.
        child_record = OrderRecord(
            order_id=child_order_id,
            symbol=self.config.ticker,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.STOP,
            stop_price=child_initial_stop,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=child_initial_stop,
        )
        self.registry.submit(child_record)

        # Also append to _order_history so the dashboard ORDERS panel
        # surfaces the child SELL the moment the bracket is submitted.
        # Without this, only the parent BUY rows appeared, which made the
        # operator think no protective stop was armed — even though the
        # child was already resting at IBKR. We mirror the same OrderRecord
        # used for the registry; the dashboard dedups by (order_id, side,
        # status, ts) so a single SUBMITTED row will render once.
        history_child = OrderRecord(
            order_id=child_order_id,
            symbol=self.config.ticker,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.STOP,
            stop_price=child_initial_stop,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=child_initial_stop,
        )
        self._order_history.append(history_child)

        # Audit the bracket submission as a single event so post-trade
        # analysis can grep BRACKET_SUBMITTED. Distinct from a plain
        # SUBMITTED so reviewers can tell which entries were bracket-
        # protected from the moment of submission.
        if self._audit:
            self._audit.log_order(
                event="BRACKET_SUBMITTED",
                order_id=child_order_id,
                side="BUY",
                qty=qty,
                order_type="STP",
                stop_price=child_initial_stop,
                signal_price=child_initial_stop,
                reason=f"bracket child (cover) of {order_id}; initial trigger from SELL trigger",
                state_at_time=self._state.value,
                position_at_time="FLAT",
            )

        # Persist immediately: the freeze (`_active_stop_pct`, set above at
        # bracket submission) and `_bracket_child` are in memory only until
        # now. Without this save, a restart in the submit→parent-fill window
        # loses the frozen SL pct — and the parent-fill freeze then defaults
        # to config.stop_loss_pct (e.g. 1%), silently widening a tighter
        # user stop. _save_state already serializes both fields
        # (stop_loss_pct + bracket_child); _load_state hydrates them on the
        # next start.
        self._save_state()

        return order_id

    # ── Partial-fill management (senior-quant pattern) ─────────────────
    #
    # A BUY entry stop-limit can fill in pieces. The classic pattern is:
    #   1. Arm a protective SELL stop sized to the FIRST partial immediately
    #      (no naked window).
    #   2. As subsequent partials arrive, MODIFY the SELL stop (atomic at
    #      IBKR — no cancel+replace window) to upsize qty and adjust stop
    #      trigger to track the weighted-avg fill price.
    #   3. If the BUY doesn't complete within `partial_fill_timeout_s`,
    #      MODIFY the BUY's limit upward by `partial_fill_chase_offset` —
    #      makes the remainder more aggressive without losing existing
    #      fills.
    #   4. After `partial_fill_max_chases` attempts give up: cancel the
    #      BUY remainder. The protective SL is already sized to filled qty,
    #      so the partial position is correctly protected.
    #
    # The chase task per order_id lives in `_partial_fill_chases` and is
    # cancelled when the order completes or is cancelled.

    def _schedule_partial_fill_chase(self, order_id: str) -> None:
        """(Re)schedule a chase task for an incomplete BUY entry. Called on
        every partial that doesn't complete the order. Cancels any existing
        task for the same order_id so the timer resets — fresh partial =
        fresh chance for more to come in without chasing."""
        if not hasattr(self, '_partial_fill_chases'):
            self._partial_fill_chases: dict = {}
            self._partial_fill_chase_count: dict = {}
        existing = self._partial_fill_chases.get(order_id)
        if existing and not existing.done():
            existing.cancel()
        try:
            import asyncio
            self._partial_fill_chases[order_id] = asyncio.create_task(
                self._chase_partial_fill_after_timeout(order_id)
            )
        except RuntimeError:
            pass  # no loop (unit test) — engine will retry on next fill

    def _cancel_partial_fill_chase(self, order_id: str) -> None:
        """Cancel a pending chase task — called when the order completes
        normally OR when we explicitly cancel the remainder."""
        if not hasattr(self, '_partial_fill_chases'):
            return
        task = self._partial_fill_chases.pop(order_id, None)
        if task and not task.done():
            task.cancel()
        if hasattr(self, '_partial_fill_chase_count'):
            self._partial_fill_chase_count.pop(order_id, None)

    async def _chase_partial_fill_after_timeout(self, order_id: str) -> None:
        """Wait `partial_fill_timeout_s`, then bump BUY limit upward.

        After `partial_fill_max_chases` unsuccessful bumps, cancel the
        remainder of the BUY. The protective SELL already sized to the
        filled portion stays in place — naked exposure is zero throughout.
        """
        try:
            import asyncio
            await asyncio.sleep(self.config.partial_fill_timeout_s)
        except asyncio.CancelledError:
            return

        # Did the order complete while we were sleeping?
        order = self.registry.get(order_id) if self.registry else None
        if not order or order.filled_qty >= order.qty:
            return  # done — nothing to chase

        # How many chases have we done for this order?
        if not hasattr(self, '_partial_fill_chase_count'):
            self._partial_fill_chase_count = {}
        attempts = self._partial_fill_chase_count.get(order_id, 0)

        remaining_qty = order.qty - order.filled_qty
        if attempts >= self.config.partial_fill_max_chases:
            # Out of attempts → accept the partial position, protect it.
            # CRITICAL ordering:
            #   1. Cancel the BUY remainder FIRST. If we placed SL first and
            #      then cancelled, a stray fill between the two steps would
            #      leave us with qty > SL_qty (the original bug, reversed).
            #   2. Wait briefly for the cancel to land at IBKR.
            #   3. Then place the protective SL sized to the partial that
            #      DID fill, at the stop computed from the weighted-avg
            #      fill price (engine state was already updated for that).
            self._log(
                f"[PARTIAL] {order_id}: max chases ({attempts}) reached; "
                f"cancelling remainder of {remaining_qty} shares, then "
                f"arming SL on the {order.filled_qty} that filled."
            )

            # Audit the GIVE_UP decision before issuing the cancel. The
            # IBKR-side CANCELLED status event will arrive separately and
            # produce its own audit row via _on_order_status — this row is
            # the engine's "I decided to give up" record with the reason.
            if self._audit:
                self._audit.log_order(
                    event="GIVE_UP",
                    order_id=order_id,
                    side="BUY",
                    qty=remaining_qty,
                    order_type="STOP_LIMIT",
                    limit_price=order.limit_price,
                    stop_price=order.stop_price,
                    reason=(
                        f"PARTIAL_FILL_GIVE_UP after {attempts} chase attempts: "
                        f"filled {order.filled_qty}/{order.qty}, "
                        f"abandoning {remaining_qty}. SL will be placed on partial."
                    ),
                    state_at_time=self._state.value,
                    position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
                )

            try:
                await self.gateway.cancel_order(order_id)
                # Brief settle so IBKR confirms the cancel before we place
                # the SL — defends against a stray fill landing between
                # steps and inflating the position size beyond what the
                # SL is about to cover.
                await asyncio.sleep(0.5)
            except Exception as e:
                self._log(f"[PARTIAL] cancel of remainder failed: {e}")
            self._cancel_partial_fill_chase(order_id)

            # Now place the protective SL on the partial position. The
            # engine state (_quantity, _entry_price, _stop_loss) was
            # already updated by the BUY-fill branch — `_place_protective_stop`
            # reads those.
            if self._position_open and self._quantity > 0 and self._entry_price:
                try:
                    await self._place_protective_stop("STOP_LOSS_PARTIAL_GIVE_UP")
                except Exception as e:
                    self._log(f"[PARTIAL] post-give-up SL placement failed: {e}")

            if self._alerts:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="CUSTOM_PARTIAL_FILL_GIVE_UP",
                    severity=AlertSeverity.HIGH,
                    message=(
                        f"Gave up on {remaining_qty} unfilled shares of "
                        f"{self.config.ticker} after {attempts} chase attempts. "
                        f"Position: {order.filled_qty} of intended {order.qty}; "
                        f"SL armed at ${self._stop_loss:.2f}."
                    ),
                    context={
                        "ticker": self.config.ticker,
                        "order_id": order_id,
                        "filled": order.filled_qty,
                        "intended": order.qty,
                        "abandoned": remaining_qty,
                        "attempts": attempts,
                        "stop_loss": self._stop_loss,
            # Persist the pct used when THIS cycle's SL was set so a
            # restart with different --stop-pct doesn't silently move
            # the stop. _load_state hydrates this into _active_stop_pct.
            "stop_loss_pct": (
                self._active_stop_pct
                if self._active_stop_pct is not None
                else (self.config.stop_loss_pct if self._position_open else None)
            ),
                    },
                    correlation_id=getattr(self, '_cycle_id', ''),
                )
            return

        # Resolve effective chase offset. If user explicitly set
        # `partial_fill_chase_offset` (env GT_PARTIAL_FILL_CHASE_OFFSET),
        # honor it. Otherwise (default sentinel 0.0) auto-scale to the
        # entry offset — same buffer that worked for entry should also
        # work for chase. So `--offset-fixed 0.20` produces $0.20 chases,
        # not $0.05.
        effective_chase = (
            self.config.partial_fill_chase_offset
            if self.config.partial_fill_chase_offset > 0
            else self.config.sl_limit_offset
        )

        # Bump the BUY's limit upward by the effective chase offset.
        new_limit = self._round_to_tick((order.limit_price or 0) + effective_chase)
        self._log(
            f"[PARTIAL] {order_id}: chasing {remaining_qty} unfilled shares "
            f"(attempt {attempts + 1}/{self.config.partial_fill_max_chases}): "
            f"limit ${order.limit_price:.2f} → ${new_limit:.2f} "
            f"(+${effective_chase:.2f} chase offset)"
        )
        try:
            ok = await self.gateway.modify_order(
                order_id,
                qty=order.qty,
                limit_price=new_limit,
                stop_price=order.stop_price,
            )
            if ok:
                # Update registry's view so subsequent partials chase from
                # the right baseline.
                old_limit = order.limit_price
                order.limit_price = new_limit
                self._partial_fill_chase_count[order_id] = attempts + 1

                # Audit the modification so the daily CSV captures the chase
                # history. Reuses the existing OrderWriter schema (no schema
                # change — `event` is free-text). Reading audit.csv later you
                # can group by order_id and see: SUBMITTED → FILLED → FILLED
                # → MODIFIED → MODIFIED → CANCELLED (or final FILLED).
                if self._audit:
                    self._audit.log_order(
                        event="MODIFIED",
                        order_id=order_id,
                        side="BUY",
                        qty=order.qty,
                        order_type="STOP_LIMIT",
                        limit_price=new_limit,
                        stop_price=order.stop_price,
                        signal_price=order.signal_price,
                        reason=(
                            f"PARTIAL_FILL_CHASE attempt {attempts + 1}/"
                            f"{self.config.partial_fill_max_chases}: "
                            f"limit ${old_limit:.2f}→${new_limit:.2f}, "
                            f"{remaining_qty} of {order.qty} unfilled"
                        ),
                        state_at_time=self._state.value,
                        position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
                    )

                # Also surface in the dashboard's ORDERS panel so the chase
                # is visible at-a-glance, not just in the audit log.
                self._order_history.append(OrderRecord(
                    order_id=order_id,
                    symbol=self.config.ticker,
                    side=OrderSide.BUY,
                    qty=order.qty,
                    order_type=OrderType.STOP_LIMIT,
                    stop_price=order.stop_price,
                    limit_price=new_limit,
                    status=OrderStatus.SUBMITTED,  # still working, just at a new price
                    submitted_at=self._ts(),
                    signal_price=order.signal_price,
                ))

                # Reschedule another chase in case the new limit still
                # doesn't fully fill.
                self._schedule_partial_fill_chase(order_id)
            else:
                # modify_order returned False — order may have completed or
                # been cancelled while we were sleeping. Log + audit so the
                # discrepancy is visible.
                self._log(f"[PARTIAL] modify_order returned False for {order_id}; order may be done")
                if self._audit:
                    self._audit.log_order(
                        event="MODIFY_FAILED",
                        order_id=order_id,
                        side="BUY",
                        qty=order.qty,
                        order_type="STOP_LIMIT",
                        limit_price=new_limit,
                        stop_price=order.stop_price,
                        reason="modify_order returned False (order possibly already terminal)",
                        state_at_time=self._state.value,
                        position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
                    )
        except Exception as e:
            self._log(f"[PARTIAL] modify_order failed: {e}")
            if self._audit:
                self._audit.log_order(
                    event="MODIFY_FAILED",
                    order_id=order_id,
                    side="BUY",
                    qty=order.qty,
                    order_type="STOP_LIMIT",
                    limit_price=new_limit,
                    reason=f"exception: {type(e).__name__}: {e}",
                    state_at_time=self._state.value,
                    position_at_time="SHORT" if self._position_open else "FLAT",  # SHORT INVERSION (P11)
                )

    async def _resize_protective_stop_to_cumulative(self) -> None:
        """Resize the existing protective SELL stop to match the current
        cumulative position. Called from subsequent BUY partials.

        Modifies in place (atomic at IBKR) rather than cancel+replace,
        so there's no instant where the position is unprotected.
        """
        pending = getattr(self, '_pending_stop', None)
        if not pending:
            return

        # New stop trigger from weighted-avg entry, new limit floor.
        stop_price = self._stop_loss
        sl_offset = self._compute_limit_offset(stop_price) if hasattr(self, '_compute_limit_offset') else self.config.sl_limit_offset
        limit_price = self._round_to_tick(stop_price - sl_offset)
        new_qty = self._quantity

        try:
            ok = await self.gateway.modify_order(
                pending['order_id'],
                qty=new_qty,
                stop_price=stop_price,
                limit_price=limit_price,
            )
            if ok:
                # Update our cached view of the resting SL.
                pending['qty'] = new_qty
                pending['stop_price'] = stop_price
                pending['limit_price'] = limit_price
                self._log(
                    f"[PARTIAL] Resized protective SELL stop → qty {new_qty}, "
                    f"stop ${stop_price:.2f}, limit ${limit_price:.2f}"
                )
            else:
                # Modify failed (order may have already fired or been cancelled).
                # Fall back to placing a fresh stop.
                self._log(f"[PARTIAL] modify SL failed; placing fresh stop")
                self._pending_stop = None
                await self._place_protective_stop("STOP_LOSS_RESIZE_RETRY")
        except Exception as e:
            self._log(f"[PARTIAL] resize SL exception: {e}; placing fresh stop")
            self._pending_stop = None
            await self._place_protective_stop("STOP_LOSS_RESIZE_RETRY")

    async def _place_protective_stop(self, reason: str = "STOP_LOSS") -> Optional[str]:
        """Place a SELL stop-limit at the broker to protect the open position.

        Called proactively from `_on_gateway_fill` right after a BUY fills,
        and from `_exit` as the manual/reactive backup path. The order rests
        at the broker until LTP drops to `stop_price`; on fill, the broker
        fires `fillEvent` → `_on_gateway_fill` → SELL branch closes out the
        position.

        Does NOT change engine state — the caller decides whether the
        position remains `IN_POSITION` (proactive mode) or transitions to
        `EXIT_POSITION` (manual exit).

        Idempotent. The guard is made atomic across concurrent callers
        by an additional synchronous boolean `_protective_stop_placing`:
        we set it the instant we pass the guard (before any `await`),
        and clear it in a `finally`. Without this, 8 separate call sites
        (proactive after BUY fill, reactive _exit, health-check re-arm,
        partial-fill give-up, reconcile, …) could all read
        `_pending_stop is None`, all yield on the await chain, then each
        submit a separate SELL order. Production hit 2026-05-26 NFLX: 5
        concurrent fallback submissions sold 200×5=1000 shares against
        a 200-share position. Flag-based atomic claim prevents that.

        **Gap-down fallback (senior-approved 2026-05-26):** before placing
        the STOP-LIMIT, peek at current LTP. If the market has already
        gapped through our intended stop (LTP ≤ stop_price), a STOP-LIMIT
        triggers immediately but the LIMIT (at limit_price, above market)
        sits unfilled — position bleeds. In that case we submit a MARKET
        SELL instead via `_place_protective_market_fallback`. Common at
        market-restart on a gap-down morning or after a long session
        pause when prices drifted past our stop while we were offline.
        """
        if not self._entry_price or not self._quantity:
            return None
        # ATOMIC GUARD: synchronous read+write of both flags before the
        # first await. A concurrent caller that arrives between this
        # block and our eventual _pending_stop assignment trips here
        # and early-returns — no second submission can race ours.
        if getattr(self, '_pending_stop', None) or self._protective_stop_placing:
            return None
        self._protective_stop_placing = True
        try:
            return await self._place_protective_stop_inner(reason)
        finally:
            # Always release the claim — even if placement raised — so
            # the next placement attempt can proceed.
            self._protective_stop_placing = False

    async def _place_protective_stop_inner(self, reason: str) -> Optional[str]:
        """Actual placement work — extracted so the public method can
        wrap it in the atomic-claim guard above without re-indenting
        the whole body."""
        entry_price = self._entry_price
        qty = self._quantity

        # ── PRE-FLIGHT 1: never arm a SELL on a non-existent position ──
        # Prevents the Risk-A health-rearm short. If the broker already
        # closed the position (e.g. bracket child fired while engine
        # was still processing the fill event), refuse this placement
        # and fold engine state to FLAT to match. Without this, the
        # fresh SL gets armed against 0 shares and the next stop hit
        # opens a short. Paper mode + query failures are passed through
        # (see `_preflight_sell_allowed` docstring).
        if qty and not await self._preflight_sell_allowed(int(qty), context=reason):
            return None

        # ── PRE-FLIGHT 2: never place a DUPLICATE SELL ────────────────
        # Closes the 2026-06-01 ZM double-shorting class of bug:
        #
        #   Cycle n1 placed a bracket (parent BUY + child SELL atomic).
        #   Engine restarted between bracket submission and parent fill.
        #   On restart, in-memory `_bracket_child` was lost — but the
        #   actual bracket child SELL was still resting at IBKR.
        #   When parent BUY filled, the BUY-complete branch saw
        #   `_bracket_child is None` and took the legacy fallback,
        #   submitting a SECOND SELL via `_place_protective_stop`.
        #   IBKR now had TWO SELLs for one position. The bracket child
        #   fired first, closed the position, but the legacy SELL was
        #   left orphaned. Six minutes later it fired on its own and
        #   the broker shorted us 100 shares we no longer had.
        #
        # The fix: before placing ANY new SELL, query IBKR directly for
        # any SELL STP/STP-LMT already resting on this symbol. Don't
        # trust engine memory alone — memory can drift from broker
        # reality across restarts. If we find an existing SELL, ADOPT
        # it (re-bind _pending_stop so downstream code knows it's
        # there) and return WITHOUT placing a duplicate.
        #
        # Behaviour matrix:
        #   • paper mode  → skipped (paper has no broker reality to check)
        #   • query fails → fall through to normal placement (don't block
        #                   trading on a single API glitch)
        #   • no existing SELL found → fall through to normal placement
        #                              (same behaviour as before this fix)
        #   • existing SELL found → adopt + audit + alert + return
        #                           (this is the new safety branch)
        #
        # The whole rest of this function continues unchanged for the
        # normal "no duplicate" case — this is purely additive.
        if not self.gateway.paper:
            try:
                existing = (
                    self.gateway.fetch_open_orders()
                    if hasattr(self.gateway, 'fetch_open_orders') else []
                )
                # SHORT INVERSION (P3/P9): the protective leg is a BUY cover.
                existing_sells = [
                    o for o in (existing or [])
                    if (o.get('action') == 'BUY'
                        and (o.get('order_type') or '').replace(' ', '').upper()
                            in ('STP', 'STPLMT'))
                ]
                if existing_sells:
                    o = existing_sells[0]
                    broker_id = str(o.get('broker_id') or '')
                    order_ref = o.get('order_ref') or ''
                    adopted_id = order_ref if order_ref else broker_id
                    stop_at_broker = float(o.get('stop_price') or 0.0)
                    qty_at_broker = int(o.get('qty') or 0)

                    self._log(
                        f"[{reason}] DUPLICATE-COVER GUARD: a BUY cover is "
                        f"already resting at IBKR for {self.config.ticker} "
                        f"(id={adopted_id}, stop=${stop_at_broker:.2f}, "
                        f"qty={qty_at_broker}). Adopting instead of placing "
                        f"a duplicate. Engine memory was clear; broker "
                        f"reality preserved."
                    )

                    # Re-bind engine state to the existing order so every
                    # downstream consumer (dashboard, _track_position,
                    # health check, reconcile) sees a coherent picture.
                    self._pending_stop = {
                        'order_id': adopted_id,
                        'qty': qty_at_broker or qty,
                        'stop_price': stop_at_broker,
                        'limit_price': o.get('limit_price'),
                        'side': OrderSide.BUY,
                        'order_type': (o.get('order_type') or 'STP').replace(' ', ''),
                        'adopted_existing': True,
                    }
                    if stop_at_broker:
                        self._stop_loss = stop_at_broker

                    if self._audit:
                        try:
                            self._audit.log_order(
                                event="DUPLICATE_SELL_GUARD_ADOPTED",
                                order_id=adopted_id,
                                side="BUY",
                                qty=qty_at_broker or qty,
                                order_type=(o.get('order_type') or 'STP').replace(' ', ''),
                                stop_price=stop_at_broker,
                                limit_price=o.get('limit_price'),
                                signal_price=stop_at_broker,
                                reason=(
                                    f"Existing BUY cover resting at broker — "
                                    f"refused to place duplicate (context="
                                    f"{reason}). Adopted instead. Closes the "
                                    f"ZM 2026-06-01 double-cover class of bug."
                                ),
                                state_at_time=self._state.value,
                                position_at_time=(
                                    "SHORT" if self._position_open else "FLAT"
                                ),
                            )
                        except Exception:
                            pass

                    if self._alerts:
                        try:
                            from src.infra.alerts import AlertSeverity
                            self._alerts.raise_alert(
                                code="DUPLICATE_SELL_GUARD_ADOPTED",
                                severity=AlertSeverity.MEDIUM,
                                message=(
                                    f"Duplicate BUY cover refused for "
                                    f"{self.config.ticker}: existing BUY cover "
                                    f"({adopted_id}, stop=${stop_at_broker:.2f}, "
                                    f"qty={qty_at_broker}) already at broker. "
                                    f"Adopted it. Engine memory had no record "
                                    f"of it — likely a restart between bracket "
                                    f"submission and parent fill. Context: {reason}."
                                ),
                                context={
                                    "ticker": self.config.ticker,
                                    "reason": reason,
                                    "adopted_id": adopted_id,
                                    "stop_at_broker": stop_at_broker,
                                    "qty_at_broker": qty_at_broker,
                                },
                                correlation_id=getattr(self, '_cycle_id', ''),
                            )
                        except Exception:
                            pass

                    return adopted_id
            except Exception as e:
                self._log(
                    f"[{reason}] duplicate-SELL guard query failed "
                    f"(falling through to placement): {e}"
                )

        # ── STP-MARKET geometry ────────────────────────────────────────
        # Match the bracket child's order type (StopOrder → STP, market
        # on trigger). Previously the re-arm path placed STP-LIMIT here
        # which (a) had the trigger-and-limit-both-skipped failure mode
        # in fast markets (the exact reason the bracket migration moved
        # to STP-MARKET for the child), and (b) made manual-cancel
        # recovery submit a DIFFERENT order type than the original
        # bracket — operator confusion + audit inconsistency.
        #
        # `trigger_price` is the only price we need (no separate limit).
        # We anchor it at entry × (1 - effective_stop_pct), same formula
        # the bracket child uses post-fill via `_modify_bracket_child`.
        # `stop_distance` retained for the gap-down guard log line.
        stop_pct = self._effective_stop_pct()
        # Exact integer-tick math — see `_protective_stop_price` for why
        # this beats `_round_to_tick(entry × (1 + pct))` on FX/futures.
        # SHORT: stop_price sits ABOVE entry, so distance = stop - entry.
        stop_price = self._protective_stop_price(entry_price, stop_pct)
        stop_distance = stop_price - entry_price

        # ── Gap-UP guard (SHORT INVERSION P6) ─────────────────────────
        # When current LTP has already crossed ABOVE our intended BUY-stop
        # (gap-up on open, or a move during downtime), a STP order resting
        # at IBKR triggers fine but the next print could be FAR above the
        # level we intended — extra slippage. The MARKET fallback covers
        # AT current LTP, which is at least observable.
        ltp_now = await self._current_ltp_best_effort()
        if ltp_now is not None and ltp_now > 0 and ltp_now >= stop_price:
            self._log(
                f"GAP-UP DETECTED: LTP=${ltp_now:.2f} ≥ intended stop trigger "
                f"${stop_price:.2f} — escalating to immediate MARKET BUY (COVER) "
                f"(rather than resting STP that would fire on next print at "
                f"unknown depth)"
            )
            return await self._place_protective_market_fallback(
                reason=reason, qty=qty, ltp=ltp_now,
                intended_stop_price=stop_price, intended_limit_price=stop_price,
            )

        # SL fallback shares the cycle of its parent entry — DON'T bump
        # _cycle_seq, just consume the current value.
        # SHORT INVERSION (P3): protective leg is a BUY cover.
        order_id = self._make_engine_id(f"SL_{OrderSide.BUY.value}", qty)

        order = OrderRecord(
            order_id=order_id,
            symbol=self.config.ticker,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.STOP,   # was STOP_LIMIT — see above
            stop_price=stop_price,
            limit_price=None,            # no limit on STP-MARKET
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=stop_price,
        )
        self.registry.submit(order)

        history_order = OrderRecord(
            order_id=order_id,
            symbol=self.config.ticker,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.STOP,
            stop_price=stop_price,
            limit_price=None,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=stop_price,
        )
        self._order_history.append(history_order)

        # Set exit reason BEFORE placement: paper mode may fire the fill
        # synchronously during place_stop_market, and the cover (BUY) branch
        # reads _pending_exit_reason to label the trade.
        self._pending_exit_reason = reason

        if self._audit:
            self._audit.log_order(
                event="SUBMITTED",
                order_id=order_id,
                side="BUY",
                qty=qty,
                order_type="STP",     # was STOP_LIMIT
                stop_price=stop_price,
                limit_price=None,
                signal_price=stop_price,
                state_at_time=self._state.value,
                position_at_time="SHORT",
            )

        self._log(
            f"STP-MARKET (BUY COVER) PLACED: trigger=${stop_price:.2f} "
            f"(entry=${entry_price:.2f}, stop_distance=${stop_distance:.2f}, "
            f"pct={stop_pct*100:.4f}%, reason={reason}) — matches bracket-child "
            f"order type, fires at market on trigger"
        )

        fill_price = await self.gateway.place_stop_market(
            side=OrderSide.BUY,
            qty=qty,
            stop_price=stop_price,
            order_id=order_id,
        )

        if fill_price is None:
            # Order is resting (live: at IBKR; paper: in sim).
            # _pending_stop marks "an SL is active" for both modes — guards
            # against double-placement (the registry test in this method's
            # preamble) and drives the paper-mode tick simulator. We carry
            # `order_type='STP'` through so reconcile + dashboard render
            # it consistently as a stop-market, not stop-limit.
            self._pending_stop = {
                'order_id': order_id,
                'qty': qty,
                'stop_price': stop_price,
                'limit_price': None,
                'side': OrderSide.BUY,  # SHORT: protective cover
                'order_type': 'STP',
            }
        else:
            # Paper mode fired _on_fill synchronously; the BUY (cover) branch
            # in _on_gateway_fill already ran and cleared position state.
            self._log(f"STP-MARKET (BUY COVER) FILLED @ ${fill_price:.2f}")

        return order_id

    async def _modify_bracket_child(
        self, child_id: str, new_stop: float, new_qty: int, old_stop: float,
    ) -> None:
        """Adjust the bracket child SELL STP's trigger + qty to match the
        actual parent BUY fill. Called from `_on_gateway_fill` BUY branch
        on parent completion AND on every partial fill (defensive
        re-target as VWAP refines).

        Behavior on failure (modify_stop_trigger returns False — broker
        rejected the modify, transient error, or the order was already
        terminal at IBKR):
          KEEP THE ORIGINAL ORDER. A "modify" semantics is "update this
          resting order's parameters" — if the update doesn't go through,
          the order is STILL THERE doing its job, just at the OLD
          parameters. Typical OLD vs intended diff is a few cents
          (trigger-based estimate vs fill-VWAP-based ideal).

          Concretely we:
            1. Log + audit `CHILD_STOP_MODIFY_FAILED` for visibility.
            2. Fire HIGH-severity alert so operator sees it in Slack.
            3. Sync `_pending_stop['stop_price']` and `self._stop_loss`
               back to `old_stop` so engine state matches broker reality.
            4. Return — DO NOT cancel, DO NOT place a fresh SL.

          If the original was actually cancelled/filled externally
          (rare edge), the periodic health-check Probe 1 (30s cadence)
          detects the missing SELL stop at IBKR and re-arms via
          `_place_protective_stop` (now STP-MARKET with pre-flight
          broker-qty check). No protection gap in steady state; up to
          30s recovery time in the rare external-cancel case.

        Audit always logs:
          * CHILD_STOP_MODIFIED on success
          * CHILD_STOP_MODIFY_FAILED on failure (with "kept original" reason)
        """
        try:
            ok = await self.gateway.modify_stop_trigger(
                child_id, new_stop_price=new_stop, new_qty=new_qty,
            )
        except Exception as e:
            self._log(f"[BRACKET] modify_stop_trigger({child_id}) raised: {type(e).__name__}: {e}")
            ok = False

        if ok:
            self._log(
                f"BRACKET CHILD MODIFIED: {child_id} trigger ${old_stop:.2f} → "
                f"${new_stop:.2f}, qty → {new_qty}"
            )
            if self._audit:
                self._audit.log_order(
                    event="CHILD_STOP_MODIFIED",
                    order_id=child_id,
                    side="BUY",
                    qty=new_qty,
                    order_type="STP",
                    stop_price=new_stop,
                    signal_price=new_stop,
                    reason=f"parent SELL filled; retarget cover from initial estimate ${old_stop:.2f}",
                    state_at_time=self._state.value,
                    position_at_time="SHORT",
                )
            return

        # ── Modify failed — KEEP THE ORIGINAL CHILD AS-IS ────────────
        # A "modify" operation that fails MUST NOT cancel or replace
        # the underlying order. The semantics are: "update this resting
        # order's parameters". If the update doesn't go through, the
        # order is still there, still doing its job — just at the OLD
        # parameters (typically a few cents off from the ideal VWAP-
        # based level, well within tolerance for protection).
        #
        # Previous behavior was to cancel + place a fresh SL, and on
        # cancel-verify timeout to MARKET-SELL the entire position.
        # That was wrong: it could close a healthy position just because
        # the modify response was slow, AND it could create a protection
        # gap during the cancel/place cycle.
        #
        # New behavior: log + audit + alert + sync engine state to the
        # old broker-side level + RETURN. If the original was actually
        # cancelled or filled externally (rare), the periodic health
        # check (Probe 1, 30s cadence) will detect the absence of a
        # SELL stop at IBKR and re-arm via the normal
        # `_place_protective_stop` path — which now uses STP-MARKET and
        # the pre-flight broker-qty check.
        old_pending_stop = (
            self._pending_stop.get('stop_price')
            if self._pending_stop else None
        )
        self._log(
            f"[BRACKET] modify failed for {child_id} — KEEPING original "
            f"resting at ${old_stop:.2f} (intended ${new_stop:.2f}, diff "
            f"${abs(new_stop - old_stop):.4f}). No cancel/replace performed; "
            f"position remains protected at the legacy level. If the order "
            f"was externally cancelled, health-check re-arm fires within 30s."
        )

        if self._audit:
            try:
                self._audit.log_order(
                    event="CHILD_STOP_MODIFY_FAILED",
                    order_id=child_id,
                    side="BUY",
                    qty=new_qty,
                    order_type="STP",
                    stop_price=new_stop,
                    signal_price=new_stop,
                    reason=(
                        f"modify_stop_trigger returned False — keeping "
                        f"original cover child resting at ${old_stop:.2f}. "
                        f"NO cancel/replace performed (was previous behavior). "
                        f"Diff vs intended: ${abs(new_stop - old_stop):.4f}. "
                        f"Health-check loop will detect external cancel within 30s."
                    ),
                    state_at_time=self._state.value,
                    position_at_time="SHORT" if self._position_open else "FLAT",
                )
            except Exception as e:
                self._log(f"[BRACKET] failed to audit CHILD_STOP_MODIFY_FAILED: {e}")

        if self._alerts:
            try:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="CUSTOM_BRACKET_CHILD_MODIFY_FAILED",
                    severity=AlertSeverity.HIGH,
                    message=(
                        f"Bracket child {child_id} modify failed on "
                        f"{self.config.ticker}: could not retarget trigger "
                        f"${old_stop:.2f} → ${new_stop:.2f} "
                        f"(diff ${abs(new_stop - old_stop):.4f}). "
                        f"ORIGINAL ORDER LEFT IN PLACE — position is still "
                        f"protected at the legacy stop level. No SHORT risk. "
                        f"Operator can manually refine via TWS if desired."
                    ),
                    context={
                        "ticker": self.config.ticker,
                        "child_id": child_id,
                        "old_stop": old_stop,
                        "new_stop": new_stop,
                        "qty": new_qty,
                        "action": "kept original, no cancel",
                    },
                    correlation_id=self._cycle_id,
                )
            except Exception:
                pass

        # ── Sync engine state to broker reality ─────────────────────
        # _pending_stop['stop_price'] was set to `new_stop` in the BUY-
        # complete branch BEFORE we knew the modify would fail. Reset
        # it to `old_stop` so the dashboard, health-check matching, and
        # reactive `_track_position` all see the actual broker-side
        # value. _stop_loss likewise.
        if self._pending_stop is not None:
            self._pending_stop['stop_price'] = float(old_stop)
            # qty stays at whatever was there — the original child's
            # qty at the broker is also still the original value.
        if self._stop_loss is not None:
            self._stop_loss = float(old_stop)
        try:
            self._save_state()
        except Exception as e:
            self._log(f"[BRACKET] state save after modify-fail sync: {e}")

        # NOTE: we intentionally do NOT touch _bracket_child here.
        # In the BUY-COMPLETE caller path it was already cleared
        # (line ~835). In the PARTIAL caller path it stays set so
        # the next partial's defensive modify can retry.
        return

    async def _current_ltp_best_effort(self) -> Optional[float]:
        """Best-effort current LTP, used by the gap-down guard at
        protective-stop placement time.

        Priority:
          1. `self._prev_ltp` — set by the tick consumer on every tick.
             Freshest possible if any tick has landed since startup.
          2. `gateway.get_price()` — IBKR 1-second-bar close, async
             RPC. ~50-200ms round-trip. Only used when _prev_ltp is 0
             (engine just connected, no ticks yet, but we need a
             quick read to decide STOP-LIMIT vs MARKET).

        Returns None if both fail — the caller falls back to the
        normal STOP-LIMIT placement (preserves prior behavior).
        """
        if self._prev_ltp and self._prev_ltp > 0:
            return float(self._prev_ltp)
        try:
            px = await self.gateway.get_price()
            if px and px > 0:
                return float(px)
        except Exception as e:
            self._log(f"[gap-down] get_price probe failed (continuing with STOP-LIMIT): {e}")
        return None

    async def _place_protective_market_fallback(
        self, reason: str, qty: int, ltp: float,
        intended_stop_price: float, intended_limit_price: float,
    ) -> Optional[str]:
        """Submit a MARKET SELL to exit the position when STOP-LIMIT
        won't work (LTP has gapped below the intended stop).

        Why this exists, in one paragraph: protecting a long position
        with a STOP-LIMIT relies on LTP touching the trigger from
        above. When the operator opens the bot to a market already
        below the stop (overnight gap, weekend news), the STOP-LIMIT
        trigger fires the instant it's accepted at the broker — but
        the LIMIT (intended_limit_price, set ABOVE current LTP) sits
        unfilled because no buyer wants to pay that price into a
        falling market. The position bleeds. A MARKET SELL exits at
        whatever the bid is right now, which is exactly what the
        operator would do manually in this scenario. Senior signed
        off on this path 2026-05-26.

        Audit + alert intentionally distinct from the normal SL path
        so post-trade analysis can grep `STOP_LOSS_MARKET_FALLBACK`
        and see exactly which exits used this escape hatch.
        """
        # ── PRE-FLIGHT: never MARKET-SELL into a non-existent position ─
        # MARKET orders fire instantly; if engine state is stale and
        # broker is already flat, this would short us by the full `qty`.
        # Refuse + fold to FLAT on mismatch.
        if qty and not await self._preflight_sell_allowed(
            int(qty), context=f"market_fallback/{reason}"
        ):
            return None

        # SHORT INVERSION (P3): gap-fallback exit is a MARKET BUY (cover).
        order_id = self._make_engine_id(f"SL_MKT_{OrderSide.BUY.value}", qty)

        # Register intent in the order registry + dashboard history. We
        # use signal_price = intended_stop_price (the price we WANTED to
        # exit at) so slippage analysis later compares fill price vs.
        # the planned stop, not vs. the post-gap market.
        order = OrderRecord(
            order_id=order_id, symbol=self.config.ticker,
            side=OrderSide.BUY, qty=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=intended_stop_price,
        )
        self.registry.submit(order)
        self._order_history.append(OrderRecord(
            order_id=order_id, symbol=self.config.ticker,
            side=OrderSide.BUY, qty=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.SUBMITTED,
            submitted_at=self._ts(),
            signal_price=intended_stop_price,
        ))

        # Set the exit reason BEFORE placement: paper mode may fire
        # the fill synchronously inside place_order, and the SELL
        # branch of _on_gateway_fill reads _pending_exit_reason to
        # label the trade. Distinct sentinel so audit consumers can
        # separate "stop hit normally" from "gap-down market fallback".
        self._pending_exit_reason = f"STOP_LOSS_MARKET_FALLBACK ({reason})"

        if self._audit:
            self._audit.log_order(
                event="SUBMITTED",
                order_id=order_id,
                side="BUY", qty=qty,
                order_type="MARKET",
                signal_price=intended_stop_price,
                reason=f"GAP_UP_LTP_${ltp:.2f}_INTENDED_STOP_${intended_stop_price:.2f}",
                state_at_time=self._state.value,
                position_at_time="SHORT",
            )

        # HIGH-severity alert — operator should see this on the dashboard
        # within the next render frame. The MARKET sell will fill at the
        # bid (not the intended stop), so this is information-loss vs
        # the modeled exit. Surface it so the operator can reconcile P&L.
        if self._alerts:
            try:
                from src.infra.alerts import AlertSeverity
                self._alerts.raise_alert(
                    code="STOP_LOSS_MARKET_FALLBACK",
                    severity=AlertSeverity.HIGH,
                    message=(
                        f"Gap-up protective cover on {self.config.ticker}: "
                        f"LTP ${ltp:.2f} ≥ intended stop ${intended_stop_price:.2f} "
                        f"at placement time. Submitted MARKET BUY (COVER) {qty} "
                        f"instead of STOP-LIMIT (limit ${intended_limit_price:.2f} "
                        f"would not fill into a rising market)."
                    ),
                    context={
                        "ticker": self.config.ticker,
                        "qty": int(qty),
                        "ltp": float(ltp),
                        "intended_stop_price": float(intended_stop_price),
                        "intended_limit_price": float(intended_limit_price),
                        "reason": reason,
                    },
                    correlation_id=self._cycle_id,
                )
            except Exception:
                pass

        self._log(
            f"PROTECTIVE STOP → MARKET FALLBACK: BUY (COVER) {qty} {self.config.ticker} "
            f"MARKET (LTP=${ltp:.2f}, intended trigger=${intended_stop_price:.2f})"
        )

        # A56 (2026-06-10): `place_order` returns the order_id STRING in both
        # paper and live mode — never None, never a fill price. The previous
        # `if fill_price is None` branch was unreachable in live mode, and
        # the else branch tried to format the order_id string as a float
        # (`${fill_price:.2f}`) and crashed with
        #     ValueError: Unknown format code 'f' for object of type 'str'
        # mid-flight. The crash bubbled out of the awaited task, leaving
        # `_pending_stop=None`. The very next fillEvent tick re-entered
        # this function and submitted another MARKET sell with the same
        # engine_id. Audit log shows 4 SUBMITTED rows in 100ms, all
        # executed at the broker → broker SHORT 100k on a 25k position.
        #
        # Fix: always set `_pending_stop` BEFORE the place_order call so
        # the idempotency guard at the top of `_place_protective_stop`
        # blocks re-entry even if any later step raises. Never log
        # fill_price as float — place_order doesn't return one.
        self._pending_stop = {
            'order_id': order_id,
            'qty': qty,
            'stop_price': intended_stop_price,
            'limit_price': intended_limit_price,
            'side': OrderSide.BUY,  # SHORT: cover
            'fallback': 'MARKET',
        }
        returned_oid = await self.gateway.place_order(
            side=OrderSide.BUY, qty=qty,
            order_type=OrderType.MARKET,
            order_id=order_id,
        )
        self._log(
            f"MARKET FALLBACK placed: {returned_oid} "
            f"(intended stop ${intended_stop_price:.5f})"
        )

        return order_id

    async def _exit(self, ltp: float, reason: str, tick: "Tick"):
        """
        Manual/reactive exit: transition to EXIT_POSITION and arm the
        protective stop if one isn't already at the broker.

        In normal proactive operation, `_place_protective_stop` was called
        on the BUY fill, so `_pending_stop` is already set and this method
        only changes state (no second broker order). Manual paths
        (`square_off`, `force_exit`) still flow through here — note that
        with a resting proactive SL, those currently won't force an
        immediate sell; cancel-and-resubmit support is a follow-up.
        """
        self._state = TradeState.EXIT_POSITION
        self._previous_breakout_level = self._highest_price
        self._save_state()

        try:
            await self._place_protective_stop(reason)
        except Exception as e:
            self._pending_exit_reason = None
            self._log(f"Exit failed: {e}")
            self._state = TradeState.IN_POSITION  # Retry on next tick

    def _save_state(self):
        """Persist state to disk for crash recovery.

        Includes _pending_stop and _pending_exit_reason so a crash between
        a placement intent and IBKR acknowledgment doesn't leave the
        engine blind. On restart, reconciliation reads the broker first
        (source of truth); if the broker has no record but our saved
        state DID have a pending order, we log a warning so the user can
        investigate.
        """
        # Serialize _pending_stop carefully — it contains OrderSide which
        # is a str-Enum, JSON-safe via default=str (StateStore handles).
        pending_stop = None
        if self._pending_stop:
            ps = self._pending_stop
            pending_stop = {
                'order_id': ps.get('order_id'),
                'qty': ps.get('qty'),
                'stop_price': ps.get('stop_price'),
                'limit_price': ps.get('limit_price'),
                'side': ps.get('side').value if hasattr(ps.get('side'), 'value') else str(ps.get('side')),
                # Carry the bracket marker through so reconcile can tell
                # "this _pending_stop is the bracket parent" from
                # "this _pending_stop is the post-fill child / legacy SL".
                'bracket_parent': bool(ps.get('bracket_parent', False)),
                'from_bracket': bool(ps.get('from_bracket', False)),
                'order_type': ps.get('order_type'),
            }

        # Bracket child — only set during the "bracket submitted, parent
        # not fully filled" window. On restart, recovery can use this to
        # detect that a bracket child is (or should be) resting at the
        # broker. Optional field, missing in legacy state files; new
        # code reads via `state.get('bracket_child')` so backward-compat
        # is preserved.
        bracket_child = None
        if self._bracket_child:
            bc = self._bracket_child
            bracket_child = {
                'order_id': bc.get('order_id'),
                'qty': bc.get('qty'),
                'stop_price': bc.get('stop_price'),
                'side': bc.get('side').value if hasattr(bc.get('side'), 'value') else str(bc.get('side')),
                'order_type': bc.get('order_type'),
                'parent_order_id': bc.get('parent_order_id'),
            }

        self.state_store.save({
            "state": self._state.value,
            "position_open": self._position_open,
            "entry_price": self._entry_price,
            "highest_price": self._highest_price,
            "stop_loss": self._stop_loss,
            # Persist the pct used when THIS cycle's SL was set so a
            # restart with different --stop-pct doesn't silently move
            # the stop. _load_state hydrates this into _active_stop_pct.
            "stop_loss_pct": (
                self._active_stop_pct
                if self._active_stop_pct is not None
                else (self.config.stop_loss_pct if self._position_open else None)
            ),
            "previous_breakout_level": self._previous_breakout_level,
            "quantity": self._quantity,           # current position size (0 when flat)
            "trades_today": self._trades_today,
            "wins": self._wins,
            "losses": self._losses,
            "pnl": self._pnl,
            "total_commission": self._total_commission,
            "pending_buy_commission": self._pending_buy_commission,
            "cycle_id": self._cycle_id,
            "cycle_seq": self._cycle_seq,
            "feed_high_at_entry": self._feed_high_at_entry,
            "pending_stop": pending_stop,
            "bracket_child": bracket_child,  # NEW — see _save_state above
            "pending_exit_reason": self._pending_exit_reason,
            # Persist the configured trigger so a restart doesn't require
            # the user to re-supply --trigger when resuming a cycle. For
            # WAITING_REENTRY the engine uses previous_breakout_level instead
            # (real per-cycle breakout); trigger_price here is the original
            # cycle setup, restored for display + cold-start fallback.
            "trigger_price": self.config.trigger_price,
            # Persist the configured TRADE SIZE explicitly. Earlier code
            # only saved `_quantity` (current open position size), which is
            # 0 when flat — so restarting a flat-but-monitoring symbol used
            # to wrongly drop the trade size to 0. `config_quantity` is the
            # user's --qty value, distinct from current holdings.
            "config_quantity": self.config.quantity,
            "updated_at": self._ts().isoformat(),
        })

    # Public load_state was a parallel implementation of _load_state. Their
    # divergent semantics (this one recalculated stop from current config;
    # _load_state overwrote with the saved value when called second) meant
    # user --stop changes between sessions were silently lost. Consolidated
    # into _load_state above as the canonical state-restore path.
    def load_state(self) -> bool:
        """Backwards-compat shim — delegates to the canonical _load_state."""
        return self._load_state()

    def audit(self, event: str, data: dict):
        """Append to audit log."""
        if self.audit_log:
            self.audit_log.append(event, "", data)

    def _short_requirement_snapshot(self) -> Optional[dict]:
        """Offline short-selling requirement for the CURRENT (or armed)
        short, computed from the AssetSpec's ShortPolicy (PDF §1–4).

        Uses the live position size + entry price when in a position,
        else the configured quantity at the trigger price (the level the
        short would fire at). Returns the ShortRequirement's audit dict,
        or None when there's no spec / the math can't be built. Fully
        guarded — a failure here must never perturb status reporting.

        LIVE INPUTS (no longer hardcoded): when `_short_shortable` has been
        captured from IBKR (generic tick 236 + FEE_RATE), the offline
        requirement is recomputed with the REAL annual borrow rate and the
        REAL hard-to-borrow flag instead of RegTEquityShort's 25-bps / False
        placeholders. Margin dollars still come authoritatively from the
        whatIf preview surfaced separately as `short_margin_whatif`.
        """
        spec = getattr(self, "_asset_spec", None)
        if spec is None or getattr(spec, "short", None) is None:
            return None
        try:
            from decimal import Decimal as _D
            from src.assets.types import Price as _Price, Quantity as _Qty

            qty_raw = self._quantity if self._position_open and self._quantity else self.config.quantity
            px_raw = self._entry_price if self._position_open and self._entry_price else self.config.trigger_price
            if not qty_raw or not px_raw:
                return None

            qty = _Qty(_D(str(abs(qty_raw))), spec.sizing.expected_unit)
            notional = spec.sizing.notional(qty, _Price(str(px_raw)))

            # Fold in live IBKR shortability when available: the annual
            # borrow rate replaces the offline placeholder, and IBKR's live
            # availability sets hard_to_borrow (which also bumps maintenance).
            live = getattr(self, "_short_shortable", None) or {}
            annual_rate = None
            fr = live.get("fee_rate_annual")
            if fr is not None:
                try:
                    annual_rate = _D(str(fr))
                except Exception:
                    annual_rate = None
            htb = bool(live.get("hard_to_borrow", False))

            req = spec.short.requirement(
                notional, annual_rate=annual_rate, hard_to_borrow=htb
            )
            snap = req.to_audit_dict()
            # Tag the audit dict so the dashboard can badge borrow-rate /
            # HTB provenance (live IBKR feed vs offline placeholder).
            snap["borrow_rate_live"] = annual_rate is not None
            snap["shortable_shares"] = live.get("shortable_shares")
            snap["shortable_available"] = live.get("available")
            return snap
        except Exception:
            return None

    async def _deferred_short_data_previews(self) -> None:
        """Run the short-selling IBKR previews OFF the startup hot path.

        Availability/borrow/margin are dashboard-only and hit IBKR over the
        single API socket. Firing them at connect contended with the initial
        reconcile + first order placement, slowing engine start and delaying
        (or failing) the first orders on a high-latency link (AWS→IBKR). So
        we wait until well past the startup window, then run the two previews
        SERIALLY (never two concurrent IBKR requests).

        DEFAULT ON (try IBKR, fall back to offline): the previews fetch the
        live availability / borrow / margin from IBKR and the dashboard shows
        the offline ShortPolicy estimate whenever IBKR doesn't return data
        (unentitled account, market closed, etc.). Because they run DEFERRED
        and SERIALLY (below), enabling them does NOT slow startup or orders.

        Controls:
          GT_DISABLE_SHORT_DATA=1   — skip entirely (zero IBKR calls, the
                                      absolute-fastest startup). Offline
                                      estimate still shows in the dashboard.
          GT_SHORT_DATA_DELAY_S=N   — seconds to wait before fetching
                                      (default 5). Raise it (e.g. 30) for
                                      more startup headroom on a very slow
                                      link; lower for the values sooner.

        Fully swallowed: this is best-effort dashboard data and must never
        perturb order flow.
        """
        try:
            import os as _os
            # ON by default; explicit opt-out skips all IBKR calls.
            if _os.environ.get("GT_DISABLE_SHORT_DATA", "").strip():
                return
            # Small default delay (5s) — long enough to clear connect +
            # reconcile so we don't contend with the first order, short
            # enough that the live IBKR values land quickly WITHOUT needing
            # to pass GT_SHORT_DATA_DELAY_S on the command line.
            try:
                delay = float(_os.environ.get("GT_SHORT_DATA_DELAY_S", "5"))
            except ValueError:
                delay = 5.0
            await asyncio.sleep(max(0.0, delay))
            # Serialize so we never issue two IBKR requests at once. Each is
            # best-effort: on failure/no-data the dashboard keeps the offline
            # estimate (see _short_requirement_snapshot + draw_v2_short_req).
            await self.preview_short_margin()
            await self.preview_short_shortable()
        except Exception:
            pass

    async def preview_short_margin(self) -> Optional[dict]:
        """Best-effort AUTHORITATIVE short-margin preview via IBKR's
        whatIf Order Preview (PDF §6). Previews a SELL entry for the
        configured quantity WITHOUT placing it and caches the result in
        `self._short_margin_whatif` for get_status().

        Safe to call after connect: `whatIf=True` never submits an order.
        Runs in paper mode too — paper connects to a real IBKR gateway and
        computes margin, so the preview is available (approximate to paper's
        margin model). Swallows every error — this is an overlay on top of
        the offline ShortPolicy estimate, never a dependency.
        """
        try:
            fn = getattr(self.gateway, "whatif_order_margin", None)
            if fn is None:
                return None
            snap = await fn("SELL", int(self.config.quantity), "MKT")
            if snap:
                self._short_margin_whatif = snap
            return snap
        except Exception:
            return None

    async def preview_short_shortable(self) -> Optional[dict]:
        """Best-effort LIVE short availability + borrow fee from IBKR.

        Populates `self._short_shortable` with IBKR's live shortability
        (generic tick 236: can we short it, how many shares) and stock-
        loan fee (FEE_RATE feed). `_short_requirement_snapshot()` folds
        these into the ShortRequirement, REPLACING the hardcoded 25-bps
        borrow-rate and hard_to_borrow=False placeholders, so the
        dashboard shows the REAL availability + borrow fee.

        Gated on `spec.short.requires_locate` — only equity shorts borrow
        real shares (CFDs are synthetic, FX/futures symmetric). Live-only
        (the gateway method self-guards paper mode → returns None) and
        fully swallowed: never blocks start() or affects order placement.
        """
        try:
            spec = getattr(self, "_asset_spec", None)
            short = getattr(spec, "short", None) if spec is not None else None
            # Only instruments you actually borrow need a locate; skip the
            # market-data subscription entirely for CFD/FX/futures.
            if short is None or not getattr(short, "requires_locate", False):
                return None
            fn = getattr(self.gateway, "get_shortable_info", None)
            if fn is None:
                return None
            info = await fn()
            if info:
                self._short_shortable = info
            return info
        except Exception:
            return None

    def get_status(self) -> dict:
        """Get full engine status."""
        # Avg-slippage attribution across filled orders this session.
        # Slippage = fill_price - signal_price for BUY (positive = paid more
        # than the trigger), and signal_price - fill_price for SELL (positive
        # = got less than the floor). Useful for assessing fill quality and
        # tuning the trigger/limit buffer geometry.
        slippages = []
        for o in self.registry._orders.values():
            if o.status != OrderStatus.FILLED:
                continue
            if not (o.signal_price and o.avg_fill_price):
                continue
            slip = (
                (o.avg_fill_price - o.signal_price) if o.side == OrderSide.BUY
                else (o.signal_price - o.avg_fill_price)
            )
            slippages.append(slip)
        avg_slip = sum(slippages) / len(slippages) if slippages else 0.0
        worst_slip = max(slippages, default=0.0)

        status = {
            "state": self._state.value,
            "cycle_id": self._cycle_id,
            "cycle_seq": self._cycle_seq,
            "feed_high_at_entry": self._feed_high_at_entry,
            "running": self._running,
            "position_open": self._position_open,
            "entry_price": self._entry_price,
            "highest_price": self._highest_price,
            "stop_loss": self._stop_loss,
            # Persist the pct used when THIS cycle's SL was set so a
            # restart with different --stop-pct doesn't silently move
            # the stop. _load_state hydrates this into _active_stop_pct.
            "stop_loss_pct": (
                self._active_stop_pct
                if self._active_stop_pct is not None
                else (self.config.stop_loss_pct if self._position_open else None)
            ),
            "previous_breakout_level": self._previous_breakout_level,
            "trigger_price": self.config.trigger_price,
            "quantity": self._quantity,
            "trades_today": self._trades_today,
            "wins": self._wins,
            "losses": self._losses,
            "pnl": self._pnl,
            "total_commission": self._total_commission,
            # True broker commission accumulated on the OPEN cycle's BUY
            # entry. Refreshed by `_on_gateway_commission` when IBKR's
            # commissionReport arrives — so this is the AUTHORITATIVE
            # value, not the modeled formula. Dashboard uses this for
            # the mid-cycle realized-PnL "entry commission deduction"
            # so FX trades don't show the $92 equity-formula phantom.
            "pending_buy_commission": self._pending_buy_commission,
            "trades_in_registry": len(self.registry._orders),
            "pending_side": self._pending_side,
            # Is the protective BUY-cover stop actually CONFIRMED RESTING at
            # the broker right now? True iff `_pending_stop` carries a BUY
            # order; this gets set when the broker acks the SL placement.
            # The dashboard uses this to distinguish "stop level computed
            # but not yet placed" (arming) from "stop is resting at IBKR".
            # SHORT INVERSION (P11): protective leg is a BUY cover (was SELL);
            # the long check made this permanently False for shorts so the
            # dashboard always showed the stop as NOT resting.
            "sl_resting_at_broker": bool(
                getattr(self, '_pending_stop', None)
                and self._pending_stop.get('side') == OrderSide.BUY
            ),
            "ticks_dropped": self._ticks_dropped,
            "tick_queue_depth": self._tick_queue.qsize() if self._tick_queue else 0,
            "avg_slippage": round(avg_slip, 4),
            "worst_slippage": round(worst_slip, 4),
            "fill_count": len(slippages),
            # Short-selling requirement (PDF §1–4): offline ShortPolicy
            # estimate of margin / restricted proceeds / borrow for the
            # current-or-armed short. `short_margin_whatif` is the
            # authoritative IBKR Order Preview (§6) when captured.
            "short_requirement": self._short_requirement_snapshot(),
            "short_margin_whatif": getattr(self, "_short_margin_whatif", None),
        }
        # Surface risk-gate state if RiskCheck is wired in
        if self.risk:
            status["risk"] = self.risk.status
            status["risk_limits"] = {
                "max_consec_losses": self.config.max_consecutive_losses,
                "max_trades_per_day": self.config.max_trades_per_day,
                "daily_loss_limit_pct": self.config.daily_loss_limit_pct,
            }
        return status

    def reset_cycle(self):
        """Reset breakout level for fresh cycle (call at market open)."""
        self._previous_breakout_level = None
        self._log("Cycle reset - using trigger price for next entry")
        self._save_state()  # Persist so restart also gets fresh state
