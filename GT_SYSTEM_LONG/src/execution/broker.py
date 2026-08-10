import asyncio
import json
import subprocess
import sys
import threading
from datetime import datetime
from typing import Optional, Callable
from dataclasses import dataclass, field

from src.config.models import ConnectionStatus, OrderType, OrderSide, OrderStatus, Position


def _paper_slippage_for_symbol(symbol: str):
    """Return (max_slip, snap_to_tick) for paper-mode fill simulation.

    Resolves the symbol via the spec registry so a paper EURUSD trade
    simulates a half-pip of slippage (not 2 cents — which would be
    1,000 pips on FX), and snaps the resulting fill price to the
    correct tick grid (5dp for FX, 2dp for equity).

    Fallback: (0.02, round-to-2dp) for unresolvable symbols. That's
    the legacy equity behavior — preserves byte-identical paper output
    for every equity ticker that was working before the multi-asset
    abstraction landed.
    """
    try:
        from src.assets import resolve
        from src.assets.types import price as _to_price
        from src.assets.policies.tick import RoundDirection as _RD
        spec = resolve(symbol)
        tick = float(spec.tick.tick_size(_to_price(1.0)))
        # 2 ticks of plus-or-minus slippage. For equity: 0.02 (== legacy).
        # For EURUSD: 0.0001. For ES futures: 0.50. Realistic per-asset.
        max_slip = tick * 2.0

        def snap(px: float) -> float:
            try:
                return float(spec.tick.round_to_tick(_to_price(str(px)), _RD.NEAREST))
            except Exception:
                return round(px, 2)

        return max_slip, snap
    except Exception:
        return 0.02, (lambda px: round(px, 2))


def _logical_symbol_from_contract(ib_contract) -> str:
    """Translate an ib_async Contract back to the LOGICAL ticker the
    engine uses (matches `config.ticker` and the engine's `_quantity`
    bookkeeping).

    For every asset class except FX, the broker contract's `symbol`
    field already IS the logical ticker — passthrough. FX is the
    exception: `Forex("EURUSD")` stores `symbol="EUR"` (base ccy) and
    `currency="USD"` (quote), and a passthrough makes reconcile think
    we hold "EUR" not "EURUSD" → POSITION_MISMATCH → fold → orphan
    stop cancel. (Confirmed live 2026-06-05.)

    Iterates through every registered contract policy's `identify()`
    method (added 2026-06-05 to all four contract policies). Returns
    the first claim, or falls back to the raw broker symbol for
    forward-compat with asset classes that don't have an identify()
    method yet.
    """
    # Walk known contract policies. Each `identify()` returns None
    # unless the contract matches its secType — so this is O(#asset
    # classes), very cheap. We import lazily to avoid a top-level
    # dependency cycle (broker → assets → broker on some paths).
    try:
        from src.assets.forex import IDEALPROForexContract
        from src.assets.us_stock import SMARTStockContract
        from src.assets.cfds import CFDContract
        from src.assets.future import FuturesContractPolicy
        for policy in (
            IDEALPROForexContract,
            SMARTStockContract,
            CFDContract,
            FuturesContractPolicy,
        ):
            claimed = policy.identify(ib_contract)
            if claimed:
                return claimed
    except Exception:
        pass
    # Fallback — passthrough the raw symbol. Preserves legacy
    # behaviour for any contract type we haven't added identify() to.
    return getattr(ib_contract, 'symbol', '') or ''


@dataclass(slots=True)
class MarketData:
    """Encapsulates a single market data snapshot."""
    ticker: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: int = 0
    timestamp: datetime = field(default_factory=datetime.now)


class Gateway:
    """Manages IBKR connection, market data, and order execution."""

    __slots__ = (
        'host', 'port', 'client_id', 'symbol', 'paper', 'cfd',
        '_ib', '_status', '_connected_at', '_last_heartbeat', '_running',
        '_on_connect', '_on_disconnect', '_on_fill', '_on_error',
        '_on_order_status',
        # Async commission report callback — fires LATER than fillEvent
        # (IBKR sends commissionReport as a separate message ~100-1000ms
        # after the execution). At fillEvent time, `fill.commissionReport`
        # is usually None — which is why our modeled estimate kept
        # leaking into PnL. Subscribing to `commissionReportEvent`
        # separately gives us the true broker-charged commission as
        # soon as IBKR sends it, and the engine updates the
        # OrderRecord's `broker_commission` field in place. Subsequent
        # `order.calculate_commission()` calls then return the true
        # value, not the equity-formula fallback.
        # Signature: (engine_id: str, commission: float, exec_id: str|None)
        '_on_commission',
        # Dedup commission reports across reconnect replays.
        '_commission_exec_ids_seen',
        '_paper_positions', '_poll_proc', '_poll_callback', '_poll_queue',
        '_ts', '_last_price', '_has_price', '_mkt_data', '_contract',
        '_pending_limits',  # Pending LIMIT orders waiting to fill
        '_order_id_map',  # Map broker IDs to engine IDs for fill callbacks
        # Track engine_ids we've already dispatched a terminal status for, so
        # status updates that fire multiple times (typical from IBKR) only
        # notify the engine once.
        '_terminal_statuses_seen',
        # Account-summary cache + subscription flag. One subscribe at connect
        # time, event-driven updates keep the dict fresh, every get_*() is
        # a pure dict read. Avoids IBKR error 322 from repeated subscribes.
        '_account_cache', '_account_summary_subscribed',
        # Per-contract P&L (reqPnLSingle) cache + bookkeeping. IBKR streams
        # dailyPnL / unrealizedPnL / realizedPnL for a single conId — the
        # AUTHORITATIVE broker numbers, correct across multi-day holds
        # (carry, marks, cost-basis adjustments all handled by IBKR). Keyed
        # by conId so a per-symbol gateway only ever tracks its own contract,
        # but the dict shape supports more if ever needed. `_pnl_single_conids`
        # is the set of conIds we hold a live subscription for (so we can
        # cancel on disconnect and avoid double-subscribing). `_account_code`
        # is the IBKR account reqPnLSingle is scoped to, discovered from
        # managedAccounts() at connect time.
        '_pnl_single_cache', '_pnl_single_conids', '_account_code',
        # In-flight handshake handle for connect()'s single-flight gate.
        # Concurrent callers (engine throttle, supervisor repair) share one
        # handshake instead of each racing a fresh IB() and leaking clientId
        # slots. Completed tasks are never reused — see connect().
        '_connect_task',
        # Runtime-discovered minTick from IBKR's ContractDetails. The
        # AssetSpec ships a hardcoded fallback (0.00005 for FX majors,
        # 0.01 for equity, etc.) — but the VENUE is the authoritative
        # source at runtime. Account type, contract listing, and venue
        # routing all affect the actual valid price grid. Captured once
        # during qualify_contract via SpecRegistry.cross_validate's
        # broker_truth dict; None if discovery failed or hasn't run yet.
        # Engine reads via `get_runtime_min_tick()` and uses it in
        # `_round_to_tick` / `_price_epsilon` in preference to the
        # spec's hardcoded value. Standard HFT pattern.
        '_runtime_min_tick',
        # FL4 — optional reference to the engine's persistent FillLedger.
        # Set by the engine after construction. When present and populated,
        # get_our_position_via_executions() prefers the durable ledger net
        # (gap-free, survives IBKR's ~24h execution-cache eviction) over the
        # in-cache ib.fills() sum. None ⇒ legacy live-sum behavior. A
        # read-only borrow — the gateway never owns the ledger's lifecycle.
        '_fill_ledger',
    )

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 1,
        symbol: str = "AAPL",
        paper: bool = True,
        cfd: bool = False,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.symbol = symbol
        self.paper = paper
        # --cfd: resolve the CFD variant of `symbol` (NVDA-equity → NVDA SHARE_CFD,
        # IBUS500 → INDEX_CFD, EURUSD → FX_CFD) instead of the equity/spot contract.
        # Default False → byte-identical legacy resolution (equity/FX untouched).
        self.cfd = cfd

        # FL4 — set by the engine after construction (read-only borrow).
        self._fill_ledger = None

        self._ib = None
        self._status = ConnectionStatus.DISCONNECTED
        self._connected_at: Optional[datetime] = None
        self._last_heartbeat: Optional[datetime] = None
        self._running = False
        self._ts = datetime.now

        # Cached qualified IBKR contract. Set once after connect, reused on
        # every order. Avoids a 50–200ms ContractDetails RPC per order which
        # was the dominant source of order-placement latency.
        self._contract = None

        # Callbacks
        self._on_connect: Optional[Callable] = None
        self._on_disconnect: Optional[Callable] = None
        # Fired when an order reaches a terminal non-fill status
        # (Rejected, Cancelled, Inactive). Engine wires this to
        # clear _pending_stop and surface the rejection.
        # Signature: (engine_id: str, status: str, message: str)
        self._on_order_status: Optional[Callable] = None
        self._terminal_statuses_seen: set = set()
        self._on_fill: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        # Late-arriving commission report callback (see __slots__ docstring).
        # Fires once per execution, ~100-1000ms after the fillEvent.
        self._on_commission: Optional[Callable] = None
        self._commission_exec_ids_seen: set = set()

        # Paper position tracking
        self._paper_positions: dict[str, dict] = {}

        # Polling
        self._poll_proc: Optional[subprocess.Popen] = None
        self._poll_callback: Optional[Callable] = None
        self._poll_queue: Optional[asyncio.Queue] = None

        # Cached price for paper trading.
        # _has_price is the authoritative "have we seen real market data yet?"
        # flag. Previously this code used `_last_price != 100.0` as a sentinel
        # — fine until the stock actually traded at exactly $100.00, at which
        # point all paper fills would silently freeze. Explicit flag avoids
        # that class of bug entirely.
        self._last_price: float = 0.0
        self._has_price: bool = False

        # Pending LIMIT orders waiting to fill
        self._pending_limits: dict[str, dict] = {}

        # Map broker order IDs to engine order IDs for fill callbacks
        self._order_id_map: dict[str, str] = {}

        # Account-summary subscription bookkeeping.
        # We issue a SINGLE `reqAccountSummary` at connect time and rely on
        # ib_async's event stream to keep `_account_cache` fresh — every
        # subsequent `get_account_value` lookup is an O(1) local dict read,
        # never a fresh IBKR request. This avoids IBKR error 322
        # ("Maximum number of account summary requests exceeded") which
        # used to fire when equity + buying-power caches both called
        # `_ib.accountSummary()` at 1s TTL — some ib_async versions
        # lazily auto-subscribe on each call, and IBKR caps active
        # subscriptions at one per tag group.
        self._account_cache: dict[str, float] = {}
        self._account_summary_subscribed: bool = False
        # Per-contract IBKR P&L (reqPnLSingle). See __slots__ for rationale.
        self._pnl_single_cache: dict[int, dict] = {}
        self._pnl_single_conids: set[int] = set()
        self._account_code: str = ""

        # In-flight handshake handle for connect()'s single-flight gate.
        self._connect_task: Optional[asyncio.Task] = None

        # Runtime-discovered minTick (see __slots__ docstring). None
        # until qualify_contract completes and ContractDetails returns.
        self._runtime_min_tick: Optional[float] = None

    def get_runtime_min_tick(self) -> Optional[float]:
        """The venue's reported minimum price increment for this
        contract, discovered from IBKR's ContractDetails at qualify
        time. None if discovery hasn't run yet (engine still booting)
        or failed (RPC timeout, paper mode without contract). Engine
        reads this in `_round_to_tick` / `_price_epsilon` and prefers
        it over the AssetSpec's hardcoded default. Standard HFT pattern:
        venue is the source of truth at runtime.
        """
        return self._runtime_min_tick

    @property
    def connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    @property
    def status(self) -> str:
        return self._status.value

    def set_callbacks(
        self,
        on_connect: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None,
        on_fill: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
    ):
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_fill = on_fill
        self._on_error = on_error

    def _apply_async_patches(self):
        """No-op. Previously called nest_asyncio.apply() + a timeouts patch.

        Both have been removed:
          * `asyncio.timeouts.timeout` monkey-patch: process-global, masked
            real timeouts, dropped earlier.
          * `nest_asyncio.apply()`: nest_asyncio's last asyncio-compat update
            was for Python 3.9. On Python 3.14 (which rewrote task management
            for eager tasks) the patched event loop cancels the main task
            within ~1s of startup. ib_async supports Py 3.14 natively, so
            we don't need the patch at all.

        Method kept as a no-op so existing call sites don't have to change.
        """
        return

    # Hard ceiling on ONE full handshake — socket + open-orders + executions
    # backfill + account summary + contract qualify + P&L subscribe.
    #
    # WHY (live regression 2026-07-26, IBKR weekend restart): _connect_inner
    # bounds only connectAsync (8s, see the wait_for below it). Steps 2-6 were
    # unbounded. A TWS that has just restarted accepts the TCP socket BEFORE it
    # has finished logging in, then answers nothing — no error, no close, just
    # silence. try/except cannot catch silence; only a timer can. Both reconnect
    # drivers (ConnectionManager's supervise loop and the engine's
    # _try_active_reconnect) await connect() with no timeout of their own, so
    # both parked forever mid-handshake: 22h offline with the process alive and
    # NOT retrying. 60s is ~12x a healthy handshake (<5s) while still
    # guaranteeing the retry loops get control back.
    CONNECT_TIMEOUT_S = 60.0

    async def connect(self, timeout: Optional[float] = None) -> bool:
        """Connect to IBKR. Single-flight: concurrent callers share one handshake.

        A completed task is never reused — reconnects must run a fresh
        handshake, not replay the first one's result. The shared task is
        shielded so a cancelled caller (e.g. the engine's 10s throttle
        giving up) can't abort a handshake the supervisor is awaiting.

        Bounded by CONNECT_TIMEOUT_S: on expiry the in-flight handshake is
        cancelled for real and we return False, so the caller's retry loop
        stays alive. Never raises on failure — a half-ready TWS must produce
        a retry, not a dead loop.
        """
        limit = self.CONNECT_TIMEOUT_S if timeout is None else timeout

        inflight = self._connect_task
        if inflight is not None and not inflight.done():
            task = inflight
        else:
            task = self._connect_task = asyncio.create_task(self._connect_inner())

        try:
            # shield() stops OUR timeout from killing a handshake another
            # caller is still awaiting; the explicit cancel() below is the
            # only thing that aborts it.
            return await asyncio.wait_for(asyncio.shield(task), limit)

        except asyncio.TimeoutError:
            print(
                f"[Gateway] Connect handshake exceeded {limit:.0f}s "
                f"(TWS likely still starting up) — aborting attempt, "
                f"caller will retry."
            )
            task.cancel()
            if self._connect_task is task:
                self._connect_task = None
            # ERROR (not DISCONNECTED): the socket may well be UP with steps
            # 2-6 unrun. This flag is what stops _reconnect_gateway's
            # adopt-guard from claiming a half-configured gateway.
            self._status = ConnectionStatus.ERROR
            return False

        except asyncio.CancelledError:
            # CancelledError is BaseException, NOT Exception — every
            # `except Exception` in the callers would let it straight through
            # and kill the retry loop, which is the exact bug being fixed here.
            # Distinguish "the shared handshake was aborted by whoever timed
            # out first" (→ plain failure) from "this bot is shutting down"
            # (→ must propagate, or Ctrl+C stops working).
            if task.cancelled():
                self._status = ConnectionStatus.ERROR
                return False
            raise

    async def _connect_inner(self) -> bool:
        """The actual handshake. Only ever entered via connect()'s gate."""
        self._status = ConnectionStatus.CONNECTING
        self._apply_async_patches()

        # Invalidate the contract cache — a new connection means we should
        # re-qualify against the (possibly different) IBKR account.
        self._contract = None

        print(f"[Gateway] Attempting connect to {self.host}:{self.port}...")

        try:
            from ib_async import IB

            # Tear down any prior handle before building a new one. A
            # half-open handle reports isConnected() == False while IBKR
            # still holds our clientId slot server-side — which is why
            # disconnect()'s `if self._ib.isConnected()` guard never
            # cleaned up the ghost, and the reconnect then collided with
            # its own stale slot.
            old_ib = self._ib
            if old_ib is not None:
                self._ib = None
                # cancelAccountSummary BEFORE disconnect: IBKR otherwise
                # keeps the summary slot reserved for the session and the
                # reconnect's reqAccountSummary fails with error 322 —
                # trading one lockup for another.
                try:
                    if self._account_summary_subscribed and old_ib.isConnected():
                        old_ib.cancelAccountSummary()
                except Exception:
                    pass
                self._account_summary_subscribed = False
                # Same for the per-contract P&L subscription — release it on
                # the old handle so the reconnect's re-subscribe starts clean.
                # (_subscribe_pnl_single re-fires after re-qualification.)
                self._cancel_pnl_single(old_ib=old_ib)
                # Unconditional — deliberately NOT gated on isConnected(),
                # since the leaking case is exactly the one that reports False.
                try:
                    old_ib.disconnect()
                except Exception:
                    pass

            self._ib = IB()
            print(f"[Gateway] IB object created, calling connectAsync...")
            # Use connectAsync, not connect. The sync wrapper internally
            # calls `loop.run_until_complete(connectAsync(...))`; calling
            # that from inside our already-running asyncio loop raises
            # "This event loop is already running". nest_asyncio used to
            # paper over this by allowing nested run_until_complete, but
            # nest_asyncio is incompatible with Python 3.14's task model
            # and has been removed. Native async APIs are the correct fix.
            #
            # Explicit 8s timeout via asyncio.wait_for so a hung TWS doesn't
            # block forever — live regression 2026-06-09 multi-bot scenario:
            # 5 of 8 bots failed to reconnect after TWS restart because
            # their connectAsync calls hung indefinitely while IBKR's
            # server-side held stale slots from the previous connection.
            # With timeout: failed attempt raises, the engine's
            # _try_active_reconnect catches it, retries in 10s on a clean
            # slate. Without: bot is permanently stuck.
            await asyncio.wait_for(
                self._ib.connectAsync(
                    host=self.host, port=self.port, clientId=self.client_id,
                ),
                timeout=8.0,
            )
            print(f"[Gateway] connectAsync returned, reqMarketDataType...")
            self._ib.reqMarketDataType(1)  # LIVE data (1=live, 2=delayed) — sync, no run_until_complete

            self._status = ConnectionStatus.CONNECTED
            self._connected_at = self._ts()
            self._last_heartbeat = self._connected_at
            print(f"[Gateway] Connected!")
            # A52 lifecycle log: connect event with wall clock — match
            # against chaos test's KILL/RESTART markers to compute the
            # disconnect-window duration this bot experienced.
            print(
                f"[BRACKET_LIFECYCLE] CONNECT  cid={self.client_id}  "
                f"at={self._connected_at.isoformat()}"
            )

            # Wait for open orders to fully populate before returning.
            # Without this, the engine's startup reconciliation might
            # falsely assume no resting orders exist (because ib_async's
            # openTrades is populated async) and aggressively fold the
            # engine to FLAT, which then cancels the user's resting orders!
            try:
                if not self.paper:
                    await self._ib.reqAllOpenOrdersAsync()
                    print(f"[Gateway] Open orders populated.")
            except Exception as e:
                print(f"[Gateway] reqAllOpenOrdersAsync failed (continuing): {e}")

            # ── EXECUTIONS BACKFILL (live regression 2026-06-09) ──────────
            # ib.fills() returns the cache of executions seen via the
            # execDetailsEvent stream during THIS session. On a fresh
            # connect, that cache is empty — IBKR doesn't push historical
            # fills automatically. The engine's _reconcile_missed_fills
            # relies on ib.fills() to detect executions that happened
            # during downtime; without backfill, fills from the prior
            # session (or during a TWS-restart window) silently slip
            # past and the engine's audit log diverges from broker truth.
            #
            # Calling reqExecutionsAsync() with a default filter pulls
            # all of TODAY's executions for our client_id. (For account-
            # wide fills across all clients use ExecutionFilter() with
            # clientId=0 — but those mostly arrive via
            # `commissionReportEvent` for orders we own anyway.)
            try:
                if not self.paper and hasattr(self._ib, 'reqExecutionsAsync'):
                    n_before = len(self._ib.fills())
                    # Capture before-set so we can identify the NEW fills
                    # added by this call (not just count).
                    before_exec_ids = {
                        getattr(f.execution, 'execId', None)
                        for f in self._ib.fills()
                    }
                    # FIX (phantom NAKED SHORT on restart): the default
                    # reqExecutions filter returns only TODAY's executions, so a
                    # fill that landed while the engine was DOWN on a prior day
                    # (e.g. an aftermarket bracket BUY the night before) is never
                    # backfilled → the ledger under-counts → the engine reports a
                    # phantom short on restart. Pull a 7-day window — IBKR's
                    # execution-retention maximum — so even a weekend + holiday
                    # down-gap (e.g. an FX bracket stop that fired Sunday while
                    # the bot was down) is recovered on the next reconcile,
                    # never carried as a phantom long that then mis-sells into a
                    # naked short. The FL3 merge floor + the FL9
                    # `since` floor still discard anything before THIS session,
                    # so the wider pull cannot re-introduce stale pre-session
                    # fills — it only adds genuine downtime fills. clientId-scoped
                    # to keep it light on a large fleet; falls back to today-only
                    # if IBKR rejects the filter.
                    try:
                        from ib_async import ExecutionFilter as _ExecFilter
                        from datetime import timedelta as _td
                        _ef = _ExecFilter()
                        try:
                            _ef.clientId = int(self.client_id)
                        except Exception:
                            pass
                        _ef.time = (datetime.utcnow() - _td(days=7)).strftime('%Y%m%d %H:%M:%S')
                        await self._ib.reqExecutionsAsync(_ef)
                    except Exception:
                        await self._ib.reqExecutionsAsync()
                    n_after = len(self._ib.fills())
                    print(f"[Gateway] Executions backfilled "
                          f"({n_after - n_before} new of {n_after} total).")
                    # A52: dump every NEW fill we just learned about.
                    # This tells us if the disconnect-window BUY actually
                    # arrived in the backfill or not.
                    for f in self._ib.fills():
                        ex = f.execution
                        if getattr(ex, 'execId', None) in before_exec_ids:
                            continue
                        try:
                            sym = _logical_symbol_from_contract(f.contract)
                        except Exception:
                            sym = '?'
                        print(
                            f"[BRACKET_LIFECYCLE] BACKFILL_NEW  cid={self.client_id}  "
                            f"sym={sym}  side={ex.side}  shares={ex.shares}  "
                            f"price={ex.price}  orderId={ex.orderId}  "
                            f"execId={ex.execId}  time={ex.time}"
                        )
                    # Also dump the FULL set on first backfill (or always)
                    # so we know what the engine could have seen but didn't.
                    # Compressed: just orderId + side + shares per fill,
                    # latest 20.
                    recent = list(self._ib.fills())[-20:]
                    for f in recent:
                        ex = f.execution
                        try:
                            sym = _logical_symbol_from_contract(f.contract)
                        except Exception:
                            sym = '?'
                        print(
                            f"[BRACKET_LIFECYCLE] BACKFILL_KNOWN  cid={self.client_id}  "
                            f"sym={sym}  side={ex.side}  shares={ex.shares}  "
                            f"orderId={ex.orderId}  execId={ex.execId}  time={ex.time}"
                        )
            except Exception as e:
                print(f"[Gateway] reqExecutionsAsync failed (continuing): {e}")

            # Subscribe ONCE to account summary updates. After this the
            # `accountSummaryEvent` handler keeps `_account_cache` in sync
            # automatically — so equity/buying_power/cash lookups never
            # trigger another IBKR request. Without this explicit single
            # subscribe, the implicit one-shot-per-call behavior of some
            # ib_async versions causes IBKR error 322 within seconds of
            # the risk module starting its 1s polls.
            await self._subscribe_account_summary()

            # Eagerly qualify the contract so the FIRST order doesn't pay
            # the ContractDetails RPC cost on the hot path. After this,
            # every place_order / place_stop_limit call returns the cached
            # Contract instance from _get_contract() in microseconds.
            try:
                if not self.paper:
                    await self._get_contract()
                    print(f"[Gateway] Contract qualified and cached for {self.symbol}")
            except Exception as e:
                print(f"[Gateway] Contract pre-qualify failed (will retry lazily): {e}")

            # Subscribe to IBKR's per-contract P&L stream now that the
            # contract (and its conId) is known. Gives the dashboards the
            # broker's own realized/unrealized numbers — correct across
            # multi-day holds where our mark-to-market drifts. Best-effort;
            # dashboards fall back to engine-computed P&L if this is absent.
            try:
                await self._subscribe_pnl_single()
            except Exception as e:
                print(f"[Gateway] reqPnLSingle subscribe failed (continuing): {e}")

            if self._on_connect:
                self._on_connect()

            return True
        except Exception as e:
            self._status = ConnectionStatus.ERROR
            print(f"[Gateway] Connect error: {e}")
            if self._on_error:
                self._on_error(e)
            return False

    async def disconnect(self):
        """Disconnect from IBKR."""
        # A52 lifecycle log: explicit disconnect (us deciding to leave).
        # Distinct from an underlying TCP drop, which arrives as
        # connection-state changes via _try_active_reconnect's polls.
        try:
            print(
                f"[BRACKET_LIFECYCLE] DISCONNECT cid={self.client_id}  "
                f"at={self._ts().isoformat()}  reason=explicit"
            )
        except Exception:
            pass
        self._running = False
        if self._poll_proc:
            self._poll_proc.terminate()
            self._poll_proc = None
        if self._ib and self._ib.isConnected():
            # Cancel the account-summary subscription BEFORE disconnecting,
            # otherwise IBKR keeps the subscription slot reserved server-side
            # for the rest of the session — a reconnect would immediately
            # hit error 322 trying to re-subscribe. Best-effort; failure
            # here doesn't block the disconnect path.
            try:
                if self._account_summary_subscribed:
                    self._ib.cancelAccountSummary()
                    self._account_summary_subscribed = False
            except Exception:
                pass
            # Same server-side-slot reasoning for the per-contract P&L
            # subscription — cancel before dropping the socket.
            self._cancel_pnl_single()
            self._ib.disconnect()
            self._ib = None
        self._status = ConnectionStatus.DISCONNECTED
        if self._on_disconnect:
            self._on_disconnect()

    # Known-good (symbol, exchange, currency) overrides. Anything not
    # listed here gets the SMART/USD default, then a fallback sweep of
    # common venues if that fails.
    _SYMBOL_OVERRIDES = {
        # Indian
        "INFY":  ("NSE",   "INR"),
        # European (SMART picks the right one in EU, but currency matters)
        "ASML":  ("SMART", "EUR"),
        "SAP":   ("SMART", "EUR"),
        "NVD":   ("SMART", "EUR"),
        "SHELL": ("SMART", "EUR"),
        "ULVR":  ("SMART", "EUR"),
    }
    # Fallback (exchange, currency) combos to try when the primary
    # qualification returns no contracts. Order matters — most common
    # first.
    _FALLBACK_VENUES = (
        ("SMART", "USD"),
        ("SMART", "EUR"),
        ("SMART", "GBP"),
        ("SMART", "CAD"),
        ("ARCA",  "USD"),
        ("ISLAND", "USD"),
    )

    async def _get_contract(self, exchange="SMART", currency="USD"):
        """Return the qualified contract for our symbol, qualifying it ONCE
        and caching the result.

        Before this cache, every order placement called
        `self._ib.qualifyContracts(contract)` — a full ContractDetails RPC
        to IBKR taking ~50–200ms — making sub-millisecond order placement
        impossible. Now the first call qualifies and stores; every
        subsequent order reuses the cached `Contract` instance.

        Async because all callers are `async def` and the underlying
        `qualifyContractsAsync` is ib_async's native non-blocking API.
        The sync `qualifyContracts` wraps it in `loop.run_until_complete`,
        which can't nest inside an already-running asyncio loop
        (it raised "This event loop is already running" until we removed
        nest_asyncio). Native async is the correct fix.

        Qualification flow:
            1. If symbol is in _SYMBOL_OVERRIDES, use that (exchange, currency).
            2. Otherwise use the caller-supplied default (SMART, USD).
            3. If qualification returns nothing, sweep _FALLBACK_VENUES
               until one resolves. This handles symbols outside the hard-
               coded list without silently breaking.
            4. Last-ditch: cache the unqualified primary so subsequent
               orders don't keep re-trying the slow path. Order
               placement will surface a clear IBKR error if it's truly
               wrong.

        The cache is invalidated on reconnect (cleared in connect()), so
        clientId / port changes will re-qualify naturally.
        """
        if self._contract is not None:
            return self._contract

        from ib_async import Stock

        # Pick primary venue/currency
        if self.symbol in self._SYMBOL_OVERRIDES:
            exch, ccy = self._SYMBOL_OVERRIDES[self.symbol]
        else:
            exch, ccy = exchange, currency

        # ── Multi-asset contract construction (D2-PM) ─────────────────
        # If an AssetSpec resolves for this symbol, let it own contract
        # construction — that's how FX gets Forex(), CFDs get CFD(),
        # futures get Future() with the right month/exchange. For US
        # equity the spec produces Stock(symbol, "SMART", "USD") which
        # is byte-identical to the legacy path (when there are no
        # symbol-overrides matching).
        primary = None
        _is_cfd = bool(getattr(self, 'cfd', False))
        try:
            from src.assets import resolve as _resolve_spec
            from src.assets.enum import AssetClass as _AssetClass
            # --cfd → GENERIC CFD: claim ANY symbol and let the broker qualify
            # the real contract + minTick. One path for index / equity / FX /
            # metal / commodity — no per-symbol routing or guessing. Broker is
            # the source of truth (the generic resolver + cross_validate).
            _hint = _AssetClass.CFD if _is_cfd else None
            spec = _resolve_spec(self.symbol, hint=_hint)
            primary = spec.contract.make(self.symbol)
        except Exception:
            spec = None  # legacy path picks up below

        # Legacy equity construction — used when no spec resolved, or when the
        # symbol-override table specifies a non-default venue. SKIPPED in CFD
        # mode: a CFD must NEVER be silently replaced by (or overridden with)
        # the equity Stock(), or we'd trade the wrong instrument.
        if not _is_cfd and (primary is None or (self.symbol in self._SYMBOL_OVERRIDES)):
            primary = Stock(self.symbol, exch, ccy)
        if primary is None:
            raise RuntimeError(
                f"--cfd set for {self.symbol} but no CFD contract resolved "
                f"(unknown CFD symbol or spec build failed). Refusing to fall "
                f"back to the equity contract."
            )
        qualified = await self._ib.qualifyContractsAsync(primary)

        # Fallback sweep if primary fails (handles symbols outside the override
        # list without silent breakage). NOT in CFD mode — equity venue
        # fallbacks would qualify the wrong (equity) instrument.
        if not qualified and not _is_cfd:
            for fb_exch, fb_ccy in self._FALLBACK_VENUES:
                if (fb_exch, fb_ccy) == (exch, ccy):
                    continue  # already tried
                fallback = Stock(self.symbol, fb_exch, fb_ccy)
                qualified = await self._ib.qualifyContractsAsync(fallback)
                if qualified:
                    print(
                        f"[Gateway] Contract qualified for {self.symbol} on "
                        f"fallback venue {fb_exch}/{fb_ccy} (primary {exch}/{ccy} failed)"
                    )
                    break

        if qualified:
            self._contract = qualified[0]
            # ── Multi-asset cross-validation (D3-PM) ───────────────────
            # If a spec resolved earlier, assert that IBKR's
            # ContractDetails agree with the spec's expected currency /
            # multiplier / tick. This is the "ES vs MES 10× catastrophe"
            # tripwire: if operator types ES but the contract is
            # actually MES, refuse to start. The check is best-effort —
            # we already have a qualified contract, so any failure
            # here is logged but doesn't roll back qualification.
            # The exception, if any, propagates up to the engine
            # startup path where it can be caught + surfaced loudly.
            if spec is not None:
                try:
                    from src.assets import SpecRegistry, SpecMismatchError
                    broker_truth = await SpecRegistry.cross_validate(
                        spec, self.symbol, self._ib
                    )
                    # Adopt the broker's reported minTick as the runtime
                    # rounding grid. Spec's hardcoded value is the safe
                    # offline default; venue's value is what we should
                    # actually round to. Real HFT systems do exactly this.
                    bmt = broker_truth.get("min_tick") if broker_truth else None
                    if bmt is not None and bmt > 0:
                        self._runtime_min_tick = float(bmt)
                        # Surface the discovered tick so the operator sees
                        # which grid is actually in effect. If it differs
                        # from the spec's hardcoded default, that's worth
                        # knowing (e.g. EURUSD spec=0.00005 but venue=0.00001).
                        try:
                            from src.assets.types import price as _to_price
                            spec_tick = float(spec.tick.tick_size(_to_price(1.0)))
                            if abs(spec_tick - self._runtime_min_tick) > 1e-12:
                                print(
                                    f"[Gateway] RUNTIME TICK ADOPTED for {self.symbol}: "
                                    f"spec default = {spec_tick}, broker reports "
                                    f"{self._runtime_min_tick}. Using BROKER value for "
                                    f"all rounding (venue is source of truth)."
                                )
                            else:
                                print(
                                    f"[Gateway] runtime tick = {self._runtime_min_tick} "
                                    f"({self.symbol}) — matches spec default."
                                )
                        except Exception:
                            pass
                except SpecMismatchError as e:
                    print(
                        f"[Gateway] SPEC/BROKER MISMATCH for {self.symbol}: "
                        f"field={e.field} expected={e.expected!r} "
                        f"actual={e.actual!r} — REFUSING to use this contract. "
                        f"Context: {e.context}"
                    )
                    raise
                except Exception as e:
                    # Don't kill the engine on a transient validation
                    # glitch (e.g. ContractDetails RPC timed out).
                    # Log loudly but proceed; the spec-dispatch + engine
                    # guards still protect.
                    print(
                        f"[Gateway] cross_validate raised non-mismatch "
                        f"error (proceeding cautiously): "
                        f"{type(e).__name__}: {e}"
                    )
        else:
            # Last-ditch: cache the unqualified primary. Subsequent
            # placeOrder calls will surface a clear IBKR error rather
            # than re-RPC'ing every time.
            print(
                f"[Gateway] WARNING: could not qualify {self.symbol} on any "
                f"venue — order placement will fail until this is resolved"
            )
            self._contract = primary

        return self._contract

    async def get_price(self) -> Optional[float]:
        """Get current market price using reqHistoricalData (no subscription needed).

        Returns:
          float — last close price from IBKR historical bars
          None  — IBKR returned no bars (genuinely no data for this contract)

        Raises ConnectionError when disconnected — caller must distinguish
        "stale" from "no data". Previously this returned None for both,
        which let gap-down market-fallback logic proceed on stale data.
        """
        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                "Gateway not connected; get_price would return stale "
                "or None — caller must not place orders on this data."
            )
        try:
            contract = await self._get_contract()
            bars = await self._ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            if bars:
                self._last_heartbeat = self._ts()
                return bars[-1].close
        except Exception as e:
            print(f"Price error: {e}")
            # Distinguish: socket died vs IBKR returned no data
            if self._ib is None or not self._ib.isConnected():
                raise ConnectionError(f"Socket died during get_price") from e
        return None

    async def get_positions(self) -> list[Position]:
        """Get positions - includes paper positions.

        ib_async's `Position` namedtuple from `IB.positions()` exposes
        (account, contract, position, avgCost) only — there is no
        `marketValue` field (that was an ib_insync extension). For an
        accurate live mark-to-market we'd have to subscribe to market
        data per symbol and multiply by LTP, but for the reset preview
        and dashboard glance, position × avgCost is plenty.

        ── Logical-ticker translation (D5 hotfix) ────────────────────
        The `symbol` field returned here is the LOGICAL ticker the
        engine uses (config.ticker), NOT the raw ib_async contract
        symbol. For US equity, CFDs, and futures the two are identical
        — but for FX, ib_async stores Forex("EURUSD") with
        `contract.symbol="EUR"` (base ccy), so a naive passthrough
        breaks every spec-aware caller (POSITION_MISMATCH false
        positives, orphan-stop cancels, the live 2026-06-05 EURUSD
        incident). We translate via `_logical_symbol_from_contract`
        which walks the registered contract policies and uses
        whichever one claims the contract via its `identify()` method.

        ── Disconnect handling (CRITICAL — added after live regression) ──
        If the gateway is NOT connected to IBKR, this raises ConnectionError
        instead of silently returning []. Empty-list-on-disconnect is
        INDISTINGUISHABLE from "I am connected and you have no positions" —
        which caused the engine's `_reconcile_position_state` to conclude
        "broker shows 0, engine thinks LONG → fold to FLAT → re-enter"
        WHILE THE BROKER WAS DISCONNECTED. The fold cancelled the protective
        stop, the re-entry attempt was sent to nothing, and on reconnect
        the engine was inconsistent. (Live 2026-06-06 incident.)

        Callers now MUST catch ConnectionError and skip whatever decision
        they'd have made on the data. Paper mode still works — paper
        positions are local, never raise.
        """
        # Paper-mode positions are local; always available regardless of
        # IB connection state.
        if self.paper:
            positions = []
            for sym, p in self._paper_positions.items():
                if p["qty"] != 0:
                    positions.append(Position(
                        symbol=sym,
                        quantity=p["qty"],
                        avg_cost=float(p["avg_cost"]),
                        market_value=float(p["qty"] * p["avg_cost"]),
                    ))
            return positions

        # Live mode: REFUSE to return positions while disconnected — the
        # engine cannot distinguish "no positions" from "I don't know"
        # without this signal. Returning [] silently is the bug class
        # that caused the 2026-06-06 false-fold + re-entry incident.
        if not self.connected:
            raise ConnectionError(
                "Gateway is not connected to IBKR; cannot report positions. "
                "Caller must skip any decision that depends on broker truth "
                "while disconnected (do NOT treat as 'no positions')."
            )
        # Additional defense: check the underlying ib_async socket too.
        # `self.connected` reads our internal _status, which is event-
        # driven; the actual socket might be down even if the event
        # hasn't fired yet.
        if self._ib is None or not self._ib.isConnected():
            raise ConnectionError(
                "Gateway._status says CONNECTED but ib_async.isConnected() "
                "is False — broker socket is dead. Refusing to report "
                "positions until socket recovers."
            )

        # Live IBKR positions — we're past the disconnect guards above.
        positions = []
        for pos in self._ib.positions():
            if pos.position != 0:
                # marketValue is not on ib_async Position; getattr with
                # default so we don't AttributeError on the access.
                mkt_val = getattr(pos, 'marketValue', None) or (pos.position * pos.avgCost)
                logical_sym = _logical_symbol_from_contract(pos.contract)
                positions.append(Position(
                    symbol=logical_sym,
                    quantity=pos.position,
                    avg_cost=pos.avgCost,
                    market_value=float(mkt_val) if mkt_val else 0.0,
                ))

        return positions

    def get_our_position_via_executions(self, symbol: str, since=None) -> Optional[int]:
        """A43 — return signed net position for `symbol` summed from
        OUR clientId's executions only. This is the BULLETPROOF truth
        source for "what this bot has placed at the broker".

        WHY THIS IS BETTER THAN accountValues OR positions():
            * `positions()` lies for spot FX (cash-ledger quirk; see A18).
            * `accountValues` cash balance is contaminated when 2+ bots
              share a base currency (EURUSD + EURJPY both move EUR; see
              A42's known limitation).
            * `executions` filtered by clientId is uniquely OURS. Across
              all sessions today, each fill is signed (+for BUY, -for SELL),
              and the sum is precisely what this bot has at the broker.
              No contamination, no quirks, no derivation.

        WHY THIS WORKS ACROSS RESTARTS:
            The A19 fix calls reqExecutionsAsync() in connect(), which
            populates self._ib.fills() with all of TODAY's executions
            for our clientId. Even after restart, we re-fetch and see
            the full history. State files can be lost; this can't.

        IMPLEMENTATION:
            * Iterate ib.fills() (already populated by A19 + live
              execDetailsEvent stream).
            * Filter to OUR clientId (other clients' fills on the same
              account are NOT ours).
            * Match symbol via the existing logical_symbol translator
              (handles FX's base/quote shenanigans).
            * Sum: BOT → +shares, SLD → −shares.

        EDGE CASES:
            * Executions older than ~24h fall out of IBKR's cache.
              Acceptable for paper / live overnight; long-term tracking
              would need persistent journaling (Layer 2 work).
            * Stale fills from a prior bot crash that didn't write to
              audit are STILL counted here — correctly, because the
              broker did execute them on our behalf.

        Returns:
            int  — signed net position (positive=long, negative=short).
            0    — no executions on record for this symbol.
            None — paper mode, disconnected, or executions API failed.

        FL9 — `since` (optional, naive-or-aware datetime): when provided, only
        executions at-or-after `since` are summed in the ib.fills() live-sum
        path; fills strictly BEFORE it are skipped. The engine passes its
        session-start floor for a bot that booted FLAT (no saved position), so
        stale pre-restart executions left on a REUSED clientId can never be
        summed into a phantom position. The durable-ledger shortcut is NOT
        gated by `since` (it is already floored at merge time and survives
        24h eviction — see the inline note below). When `since is None`
        (default — restored-with-position path, all other callers) behaviour
        is unchanged: full execution history.

        Paper mode: None (no real broker executions).
        Disconnected: None (caller MUST NOT treat as "flat").
        """
        if self.paper:
            return None
        # FL4 — prefer the durable ledger when present AND populated. It is
        # gap-free and survives IBKR's ~24h execution-cache eviction (the
        # documented limitation of the ib.fills() path below). The engine
        # has already merged today's reqExecutions into it on reconcile
        # (FL3), so the ledger is a superset of ib.fills(). We require
        # count()>0 so a disabled/empty ledger transparently falls through
        # to the proven live-sum path — never returns a false 0.
        # FL9: the ledger shortcut is intentionally NOT gated by `since`. The
        # ledger is already floored at merge time (FL7) and is wiped alongside
        # the state file on a clean restart, so it never holds stale
        # pre-session fills in practice — while remaining the only source that
        # survives IBKR's ~24h ib.fills() eviction for a long-held position.
        # The phantom we're killing here comes solely from the EMPTY-ledger
        # fall-through to the ib.fills() live-sum below, which the `since`
        # floor in that loop now blocks.
        _led = getattr(self, '_fill_ledger', None)
        if _led is not None:
            try:
                if _led.count() > 0:
                    return int(_led.net(symbol))
            except Exception:
                # Ledger fault → fall through to the live-sum truth source.
                pass
        if not self.connected or self._ib is None or not self._ib.isConnected():
            return None
        try:
            our_client_id = int(getattr(self, 'client_id', 0) or 0)
        except Exception:
            return None
        try:
            fills = self._ib.fills()
        except Exception:
            return None
        net = 0
        for f in fills:
            try:
                exec_ = getattr(f, 'execution', None)
                if exec_ is None:
                    continue
                fill_client_id = int(getattr(exec_, 'clientId', -1))
                if fill_client_id != our_client_id:
                    continue
                # Match symbol via logical-symbol translator so FX
                # (which IBKR stores with contract.symbol=base_ccy)
                # gets correctly attributed.
                contract = getattr(f, 'contract', None)
                if contract is None:
                    continue
                logical_sym = _logical_symbol_from_contract(contract)
                if logical_sym != symbol:
                    continue
                # FL9 — session-start floor: skip fills from BEFORE this bot's
                # session so a fresh-start bot on a REUSED clientId cannot adopt
                # stale pre-restart executions as a phantom position. tz: ib's
                # exec.time is UTC-aware; the engine's floor (since) is a naive
                # datetime.now() — treat it as UTC, mirroring the identical
                # coercion in engine._reconcile_missed_fills (FL3 floor).
                if since is not None:
                    _ft = getattr(exec_, 'time', None)
                    if _ft is not None:
                        try:
                            from datetime import timezone as _utz
                            _cmp = (since.replace(tzinfo=_utz.utc)
                                    if (getattr(_ft, 'tzinfo', None) is not None and since.tzinfo is None)
                                    else since)
                            if _ft < _cmp:
                                continue
                        except Exception:
                            # Unparseable timestamp → don't drop the fill;
                            # counting a real fill beats hiding one.
                            pass
                shares = int(getattr(exec_, 'shares', 0) or 0)
                side = (getattr(exec_, 'side', '') or '').upper()
                if side == 'BOT':
                    net += shares
                elif side == 'SLD':
                    net -= shares
                # else: ignore (shouldn't happen for spot products)
            except Exception:
                # Skip malformed fill records; don't let one bad
                # execution break the whole position calculation.
                continue
        return net

    def get_fx_position_via_account_values(self, symbol: str) -> Optional[float]:
        """A42 — return signed FX position size for `symbol` using the
        ACCOUNT CURRENCY CASH LEDGER instead of ib.positions().

        WHY:
            ib.positions() does NOT report spot FX as a discrete position
            on most account configurations — and after a TWS restart, even
            accounts that previously showed an FX position via positions()
            tend to consolidate it into the cash ledger. The result is
            the "cash quirk": positions() returns 0 for EURUSD even though
            the user economically owns +25k EUR.

            The actual truth source for FX is the BASE currency cash
            balance from ib.accountValues():
                EURUSD long  25k  →  EUR balance +25,000
                EURUSD short 25k  →  EUR balance −25,000
                EURUSD flat       →  EUR balance unchanged from baseline

        APPLICABILITY:
            This method is correct for FX pairs where the BASE currency
            is NOT the account's base currency (USD on most accounts).
            For non-USD-base pairs (EURUSD, GBPUSD, AUDUSD, NZDUSD,
            EURJPY), the base-ccy balance is uniquely attributable to
            that pair (assuming no other bot trades the same base ccy).
            For USD-base pairs (USDJPY, USDCHF, USDCAD), USD balance is
            contaminated by every pair's activity — caller should fall
            back to ib.positions() + A18 guard for those.

        Returns:
            float — the signed base-currency balance (positive = long,
                    negative = short). Use this as the FX position truth.
            None  — symbol isn't a 6-letter alpha pair, or the base ccy
                    balance isn't found in accountValues, or we're not
                    connected.

        Paper mode: returns None (paper sim has no real cash ledger).
        Disconnected: returns None (caller must NOT treat as "flat" — it
                      means "unknown" exactly like get_positions ConnectionError).
        """
        # Shape check: must be 6-letter uppercase alpha FX pair
        if not (len(symbol) == 6 and symbol.isalpha() and symbol.isupper()):
            return None
        base = symbol[:3]
        if self.paper:
            return None
        if not self.connected or self._ib is None or not self._ib.isConnected():
            return None
        try:
            for v in self._ib.accountValues():
                if v.tag == "CashBalance" and v.currency == base:
                    try:
                        return float(v.value)
                    except (TypeError, ValueError):
                        return None
        except Exception:
            return None
        return None

    def get_all_fills(self) -> list:
        """Return the broker's full fill history for the current session.

        Used by Engine._reconcile_missed_fills() on every reconnect (and
        startup) to detect executions that happened while the bot was
        disconnected. Without this, a fill that arrives during the
        ~10-60s disconnect window between IB Gateway daily restart and
        our supervisor's reconnect would silently slip past us — the
        engine would think it's still MONITORING while IBKR considers us
        LONG, or vice versa.

        Returns a list of ib_async `Fill` objects. Each has:
            fill.contract.symbol          : str
            fill.execution.execId         : str (unique per execution)
            fill.execution.side           : 'BOT' | 'SLD' (IBKR taxonomy)
            fill.execution.shares         : int
            fill.execution.price          : float
            fill.execution.time           : datetime
            fill.execution.orderId        : IBKR-assigned broker order id

        Paper mode: returns []. The paper-trading IBKR backend fires fills
        synchronously through our `_on_fill` callback, so there's no
        disconnect window where a fill could be missed; nothing to replay.

        CRITICAL: while disconnected, used to return [] silently — which
        meant "no missed fills" to the caller. But during a disconnect
        window REAL fills could have happened. Now raises ConnectionError
        so reconcile-on-restart can defer until connection recovers.
        """
        if self.paper:
            return []
        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                "Gateway not connected; get_all_fills cannot scan the "
                "disconnect window for missed fills. Caller must NOT "
                "assume 'no missed fills' on this return."
            )
        try:
            return list(self._ib.fills())
        except Exception as e:
            # Distinguish disconnect from genuine RPC failure.
            if self._ib is None or not self._ib.isConnected():
                raise ConnectionError(
                    f"Socket died during get_all_fills"
                ) from e
            print(f"[get_all_fills] RPC failure: {type(e).__name__}: {e}", file=sys.stderr)
            return []

    def fetch_open_orders(self) -> list[dict]:
        """Return IBKR open orders for our symbol as plain dicts.

        Used by Engine reconcile-on-startup: after the engine restarts, any
        GTC stop-limits placed in the prior session are still resting at
        IBKR but the engine's in-memory `_order_id_map` and registry are
        empty. The engine walks this list, infers each order's engine-side
        id from its properties, and calls `register_existing_order` to
        rewire fill events back into the strategy state machine.

        Only returns active/working orders (Submitted, PreSubmitted, etc.) —
        fully filled or cancelled orders are filtered out. Paper mode
        returns []: paper has no broker-side persistence to reconcile with.

        Each dict carries the raw IBKR `Trade` object under `_trade` so the
        caller can pass it back via `register_existing_order` to wire up
        fillEvent without us having to track the Trade list ourselves.

        ── Disconnect handling ──
        Paper mode: returns [] (no broker, no orders).
        Live mode while disconnected: raises ConnectionError. Same
        rationale as `get_positions` — silently returning [] is
        indistinguishable from "no resting orders" and triggers false-
        positive NAKED_POSITION + re-arm cascades. Callers must catch
        and skip whatever decision depends on broker truth.
        """
        if self.paper:
            return []
        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                "Gateway is not connected to IBKR; cannot enumerate open "
                "orders. Caller must skip any decision that depends on "
                "broker truth while disconnected (do NOT treat as 'no "
                "resting orders' — that triggers naked-position cascades)."
            )

        # Status set covers every IBKR state where the order can still
        # fire and affect position:
        #   Submitted       — live at exchange
        #   PreSubmitted    — accepted by IBKR routing but waiting on
        #                     activation conditions (e.g. bracket child
        #                     waiting for parent's first fill)
        #   PendingSubmit   — buffered client-side, transmit pending.
        #                     This is where a freshly-submitted bracket
        #                     child sits before the parent's `transmit=True`
        #                     flushes both legs. Including this status
        #                     means reconcile-on-restart SEES the child
        #                     even if the engine crashed in the 50ms
        #                     window between bracket submission and parent
        #                     activation.
        #   ApiPending      — API-level holdoff, briefly transient
        # Excluded: Cancelled / ApiCancelled / Filled / Inactive (terminal),
        # PendingCancel (cancel-in-flight; treat as gone).
        ACTIVE_STATUSES = {'Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending'}
        out: list[dict] = []
        # Track active BUY-parent ids in this snapshot so the
        # "PendingSubmit child" diagnostic only fires when the parent
        # is genuinely still pending. After the parent fills, the
        # bracket child's status stays "PreSubmitted" in some ib_async
        # versions even though the order is fully active — printing
        # "will activate when parent fills" every 30s after the parent
        # already filled is misleading noise. (Reported live 2026-06-05.)
        active_parent_ids: set = set()
        pending_children: list = []
        for trade in self._ib.openTrades():
            # Logical-ticker match (see _logical_symbol_from_contract).
            # Bare `trade.contract.symbol` is "EUR" for Forex("EURUSD")
            # — would silently drop every FX bracket child and convince
            # the health-check that we have no protective stop (2026-06-05).
            if _logical_symbol_from_contract(trade.contract) != self.symbol:
                continue
            if trade.orderStatus.status not in ACTIVE_STATUSES:
                continue
            order = trade.order
            # Surface bracket children explicitly. parentId != 0 means
            # this is a child of a bracket — useful diagnostic when
            # debugging "is the child actually visible?".
            is_bracket_child = bool(getattr(order, 'parentId', 0))
            # Track this open order's status for the diagnostic below.
            if is_bracket_child and trade.orderStatus.status in (
                'PendingSubmit', 'PreSubmitted'
            ):
                pending_children.append(int(getattr(order, 'parentId', 0)))
            else:
                # Any non-child order with parent semantics is a candidate
                # parent. Track by orderId so we can later check whether
                # a pending child's parent is still open.
                active_parent_ids.add(int(order.orderId))
            out.append({
                'broker_id': str(order.orderId),
                'action': order.action,
                'order_type': order.orderType,
                'qty': int(order.totalQuantity),
                'limit_price': float(order.lmtPrice) if order.lmtPrice else None,
                'stop_price': float(order.auxPrice) if order.auxPrice else None,
                'tif': order.tif,
                'status': trade.orderStatus.status,
                'parent_id': int(getattr(order, 'parentId', 0)) or None,
                'is_bracket_child': is_bracket_child,
                # IBKR's `orderRef` field — we stash our engine_id here
                # when placing orders, so reconcile can recover the EXACT
                # per-cycle engine_id (e.g. BR_SELL_100_TSLA_c4_n7) rather
                # than guess from convention. Empty string for orders we
                # didn't place (e.g. user's manual TWS orders).
                'order_ref': getattr(order, 'orderRef', '') or '',
                '_trade': trade,
            })
        # Only print "will activate when parent fills" for children
        # whose PARENT is ALSO still open (i.e. genuinely waiting).
        # After parent fills, the parent is no longer in openTrades, so
        # the child's PreSubmitted status is just a stale broker label
        # and the message is misleading — suppress it.
        genuinely_waiting = [
            pid for pid in pending_children if pid in active_parent_ids
        ]
        if genuinely_waiting:
            print(
                f"[Gateway] fetch_open_orders: {len(genuinely_waiting)} bracket "
                f"child(ren) in PendingSubmit/PreSubmitted state on {self.symbol} "
                f"— will activate when parent fills"
            )
        return out

    async def fetch_all_open_orders_for_symbol(self, symbol: str) -> list[dict]:
        """Return broker-side open orders for `symbol` across ALL clients.

        IBKR's default `openTrades()` accessor only contains orders submitted
        by THIS client_id. To see resting orders from other clients (or from
        manual TWS placements, client_id=0), we need to call `reqAllOpenOrders`
        first — IBKR then pushes every open order on the account into the
        local cache via the openOrder event stream, after which `openTrades()`
        contains the union.

        Used by the naked-position guard at startup to detect the case where
        another client_id placed a BUY but the order hasn't filled yet:
        broker_position is still 0 (looks safe!) but two BUYs armed at the
        same trigger would double-fill on crossing.

        Returns dicts with `owning_client_id` so the caller can filter:
        we only care about orders NOT placed by us (the current client_id).

        Brief async sleep after the request to give IBKR time to push the
        snapshot. 0.5s is a generous upper bound — measured at ~50-150 ms
        in practice on a co-located gateway.
        """
        if self.paper:
            return []
        # CRITICAL: cross-client conflict detection requires KNOWING what's
        # at the broker. While disconnected, returning [] silently means
        # "no conflict" — but two BUYs from different clients could
        # collide undetected, double-filling on the next trigger.
        # (Same bug class as 2026-06-06 get_positions.)
        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                "Gateway not connected; cannot enumerate cross-client open "
                "orders for naked-position guard. Caller must treat as "
                "'unknown', NOT 'no conflict'."
            )
        try:
            # Trigger the cross-client snapshot. Some ib_async versions
            # expose only the sync variant (which still just sends the
            # request — actual data lands via the event handler).
            if hasattr(self._ib, 'reqAllOpenOrdersAsync'):
                await self._ib.reqAllOpenOrdersAsync()
            else:
                self._ib.reqAllOpenOrders()
            # Give IBKR a moment to push the openOrder messages. Without
            # this, openTrades() may return the stale per-client list.
            await asyncio.sleep(0.5)
        except Exception as e:
            # Genuine RPC failure (not disconnect) — raise so caller can
            # decide whether to retry or refuse to start. Empty-list was
            # the silent failure we're eliminating.
            raise ConnectionError(
                f"reqAllOpenOrders failed: {type(e).__name__}: {e}. "
                f"Cross-client snapshot unavailable; caller must not "
                f"assume 'no conflict'."
            ) from e

        ACTIVE_STATUSES = {'Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending'}
        out: list[dict] = []
        try:
            for trade in self._ib.openTrades():
                if _logical_symbol_from_contract(trade.contract) != symbol:
                    continue
                if trade.orderStatus.status not in ACTIVE_STATUSES:
                    continue
                order = trade.order
                # `clientId` on the Order is the client_id that PLACED it.
                # 0 = manual TWS placement. Anything else = a specific API client.
                owning_cid = getattr(order, 'clientId', None)
                if owning_cid is None:
                    # Some ib_async versions stash it on the Trade instead.
                    owning_cid = getattr(trade, 'clientId', 0)
                out.append({
                    'broker_id': str(order.orderId),
                    'owning_client_id': int(owning_cid or 0),
                    'action': order.action,
                    'order_type': order.orderType,
                    'qty': int(order.totalQuantity),
                    'limit_price': float(order.lmtPrice) if order.lmtPrice else None,
                    'stop_price': float(order.auxPrice) if order.auxPrice else None,
                    'tif': order.tif,
                    'status': trade.orderStatus.status,
                })
        except Exception as e:
            print(f"[Gateway] openTrades enumeration failed: {type(e).__name__}: {e}")
            return []
        return out

    def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        """Cancel all working orders at IBKR for `symbol`.

        Used by the --reset CLI flow to wipe the broker side of state. Returns
        the count of cancel requests issued (not confirmations — IBKR may
        take a moment to acknowledge each cancel).

        Bracket-aware: parent and child are both Trade objects in
        `self._ib.openTrades()`, so iterating cancels both. If parent is
        cancelled before filling, IBKR may auto-cancel the child as a
        side effect — the explicit `cancelOrder(child)` then gets a
        benign "already terminal" status which ib_async swallows. Net
        result is the same: both legs gone.
        """
        if self.paper or not self._ib or not self._ib.isConnected():
            return 0
        cancelled = 0
        bracket_parents = 0
        bracket_children = 0
        for trade in self._ib.openTrades():
            if _logical_symbol_from_contract(trade.contract) != symbol:
                continue
            if trade.isDone():
                continue
            # Diagnostic counting — surfaces "we cancelled N orders, B
            # of which were bracket legs" in the reset log so the operator
            # can confirm both legs were addressed.
            o = trade.order
            is_child = bool(getattr(o, 'parentId', 0))
            is_parent_held = (not is_child) and (getattr(o, 'transmit', True) is False)
            if is_child:
                bracket_children += 1
            elif is_parent_held:
                bracket_parents += 1
            # A52 lifecycle log: bulk-cancel path. Distinguishable from
            # the single-cancel path so we can tell which path killed
            # what during chaos (--reset vs engine logic vs sweep).
            if is_child or is_parent_held:
                bid = str(o.orderId)
                eid = self._order_id_map.get(bid, '?')
                print(
                    f"[BRACKET_LIFECYCLE] CANCEL_SENT  cid={self.client_id}  "
                    f"engine_id={eid}  broker_id={bid}  "
                    f"side={o.action} type={o.orderType} "
                    f"role={'CHILD' if is_child else 'PARENT_HELD'} "
                    f"src=cancel_open_orders_for_symbol({symbol})"
                )
            self._ib.cancelOrder(o)
            cancelled += 1
        if bracket_parents or bracket_children:
            print(
                f"[Gateway] cancel_open_orders_for_symbol({symbol}): "
                f"{cancelled} cancels issued ({bracket_parents} bracket parent(s), "
                f"{bracket_children} bracket child(ren))"
            )
        return cancelled

    async def verify_symbol_flat_at_broker(self, symbol: str,
                                           timeout_s: float = 3.0) -> bool:
        """Poll IBKR until no working orders remain for `symbol`, or
        until `timeout_s` elapses. Used by --reset to confirm the
        cancels actually took effect before declaring success.

        Returns True if no working orders found within timeout, False if
        any remained (operator should investigate manually). Each poll
        is `fetch_open_orders` which is a local-cache read — cheap.

        CRITICAL: while disconnected, this used to return True silently,
        making --reset declare success when the broker state was actually
        unknown. Live orders could still be resting at IBKR while the
        operator thought everything was cancelled. Now: raises
        ConnectionError so the CLI flow refuses to declare success on
        unknown data.
        """
        if self.paper:
            return True
        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                f"Gateway not connected; cannot verify {symbol} is flat at "
                f"broker. --reset must NOT declare success on unknown state."
            )
        import time as _time
        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            try:
                remaining = self.fetch_open_orders()
            except ConnectionError:
                # Mid-poll disconnect — propagate, same rationale.
                raise
            if not remaining:
                return True
            await asyncio.sleep(0.2)
        # Last check before declaring failure
        try:
            return not self.fetch_open_orders()
        except ConnectionError:
            raise

    async def flatten_position(self, symbol: str) -> Optional[dict]:
        """Close any open position in `symbol` with a market order.

        Used by the --reset CLI flow. Long position → SELL; short → BUY.
        Returns:
          - dict describing the flatten order on success
          - None if there's already no position to close

        CRITICAL: while disconnected, used to return None silently ("no
        position to flatten") — but actually the position state was
        UNKNOWN. A live position could be left with no protective stop
        while operator thought --reset succeeded. Now: raises
        ConnectionError on disconnect.
        """
        if self.paper:
            return None
        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                f"Gateway not connected; cannot flatten {symbol}. Position "
                f"state is unknown — --reset must NOT proceed as if flat."
            )

        # Find the position for this symbol. Match via the logical-
        # ticker translator so FX positions ("EUR" → "EURUSD") resolve
        # correctly — without this `--reset` was a no-op on any FX
        # account, leaving real positions live.
        position_qty = 0.0
        for pos in self._ib.positions():
            if _logical_symbol_from_contract(pos.contract) == symbol and pos.position != 0:
                position_qty = pos.position
                break

        if position_qty == 0:
            return None

        from ib_async import MarketOrder
        side = "SELL" if position_qty > 0 else "BUY"
        qty = abs(int(position_qty))
        contract = await self._get_contract()
        # outsideRth=True so reset works during pre-market / after-hours.
        # Without this, IBKR queues the MARKET order until 09:30 ET and
        # the user keeps an unwanted position open until RTH (Warning 399).
        order = MarketOrder(
            action=side, totalQuantity=qty, tif='DAY',
            outsideRth=True,
        )
        trade = self._ib.placeOrder(contract, order)
        return {
            'side': side,
            'qty': qty,
            'broker_id': str(trade.order.orderId),
            'trade': trade,
        }

    def _attach_commission_watcher(self, trade_obj, broker_id: str) -> None:
        """Subscribe to trade.commissionReportEvent so the engine learns
        the TRUE broker-charged commission as soon as IBKR sends it.

        Why this exists separately from fillEvent: ib_async exposes
        commission via `fill.commissionReport` on each Fill object,
        BUT IBKR sends commissionReport as a SEPARATE wire message
        ~100-1000ms after the execution. At fillEvent time, the
        Fill's commissionReport is almost always None — we can't
        read the true commission yet. ib_async hydrates the same
        Fill object when commissionReport arrives later AND fires
        `trade.commissionReportEvent(trade, fill, commissionReport)`
        for any subscriber.

        Without this watcher, our path was:
          1. fillEvent fires, commissionReport=None → modeled estimate runs
          2. ...later... commissionReport arrives, NOBODY LISTENS → ignored
          3. PnL is wrong forever (uses modeled estimate, $92 on EURUSD
             where real IBKR fee is $2)

        With this watcher:
          1. fillEvent fires → modeled estimate (best guess at the time)
          2. commissionReportEvent fires → engine UPDATES the order's
             broker_commission to the true value
          3. Subsequent `order.calculate_commission()` returns truth
          4. Round-trip PnL on SELL fill picks up the true BUY commission
             (the SELL's commissionReport almost always arrives before
             the engine reads BUY's commission for round-trip math)

        Dedup: commission events from ib_async fire once per execId
        per session, but reconnect replays can re-fire. We dedup
        on execId to avoid double-applying.
        """
        if self.paper or trade_obj is None:
            return

        def on_commission_report(_trade, fill, cr):
            try:
                if cr is None:
                    return
                comm = getattr(cr, 'commission', None)
                if comm is None or comm <= 0:
                    return
                exec_id = getattr(fill.execution, 'execId', None) if fill else None
                if exec_id and exec_id in self._commission_exec_ids_seen:
                    return
                if exec_id:
                    self._commission_exec_ids_seen.add(exec_id)
                engine_id = self._order_id_map.get(broker_id, broker_id)
                if self._on_commission:
                    self._on_commission(engine_id, float(comm), exec_id)
            except Exception as e:
                # Best-effort — never let a commission-report glitch
                # crash the trading loop. Worst case we keep the
                # modeled estimate.
                print(
                    f"[Gateway] commission report handler error for "
                    f"{broker_id}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )

        trade_obj.commissionReportEvent += on_commission_report

    def _attach_bracket_lifecycle_logger(
        self, trade_obj, broker_id: str, role: str, peer_broker_id: str
    ) -> None:
        """A52 — log EVERY status transition for a bracket leg.

        Captures the full state machine: PendingSubmit → Submitted →
        PreSubmitted → Filled/Cancelled/Inactive. Tagged with role
        ('PARENT'|'CHILD') and peer broker_id so a single grep on
        BRACKET_LIFECYCLE shows the entire timeline of a bracket pair.

        Diagnostic only — no engine semantics depend on this log.
        We dedup on (broker_id, status) so noisy reconnect reposts
        don't fill the log with the same Cancelled line ten times.
        """
        if self.paper or trade_obj is None:
            return

        seen = set()
        # A57: track whether we've already fired the inverse cancel so a
        # noisy resend of "Cancelled" status doesn't try to cancel the
        # peer parent twice.
        peer_cancel_fired = [False]
        TERMINAL_REJECTS = {'Cancelled', 'Rejected', 'Inactive', 'ApiCancelled'}

        def on_status(_trade):
            try:
                status = _trade.orderStatus.status
                filled = getattr(_trade.orderStatus, 'filled', 0)
                remaining = getattr(_trade.orderStatus, 'remaining', 0)
                avg = getattr(_trade.orderStatus, 'avgFillPrice', 0.0)
            except Exception:
                return
            key = (broker_id, status, filled)
            if key in seen:
                return
            seen.add(key)

            # Pull the last broker message in case it's a reject reason.
            msg = ""
            try:
                if _trade.log:
                    msg = (_trade.log[-1].message or "")[:120]
            except Exception:
                pass

            engine_id = self._order_id_map.get(broker_id, broker_id)
            print(
                f"[BRACKET_LIFECYCLE] STATUS  cid={self.client_id}  role={role}  "
                f"broker_id={broker_id}  engine_id={engine_id}  "
                f"peer={peer_broker_id}  status={status}  "
                f"filled={filled} remaining={remaining} avg={avg}  msg={msg!r}"
            )

            # ── A57: CHILD-REJECTED → CANCEL ORPHAN PARENT ─────────────
            # Invariant the operator stated: if a bracket BUY parent
            # exists at the broker, there MUST be a matching SELL child
            # until the parent fills. A naked parent (transmit=False,
            # the TWS "blue Transmit" state) violates this.
            #
            # Live regression 2026-06-10 AUDUSD: IBKR rejected the
            # bracket CHILD with Error 135 ("Can't find order with
            # id =796") milliseconds after placement — a known race
            # where the parent isn't yet registered server-side when
            # the child references it via parentId. The child
            # transitioned PendingSubmit → Cancelled in ~400ms while
            # the parent stayed at PendingSubmit / Transmit=False
            # forever, visible in TWS as a stuck "Transmit" order.
            #
            # The symmetric defense to A45 (parent cancel → child
            # cancel): when the CHILD enters a terminal non-filled
            # state, cancel the peer PARENT immediately so no held
            # BUY can later trigger naked. Only fire when
            # filled == 0 (a child that filled is the normal SL
            # path — leave the parent alone).
            # A65 (2026-06-10): skip A57 cascade when the "Cancelled"
            # status is actually IBKR rejecting our MODIFY of a
            # stop-after-triggered, not cancelling the order itself.
            # Sequence: parent BUY fills → engine calls
            # modify_stop_trigger to update child stop from initial
            # estimate to VWAP-based stop → if LTP already touched the
            # stop, IBKR responds Error 201 "Stop price revision is
            # disallowed after order has triggered" → child status
            # transitions to Cancelled WITH filled=0 momentarily. The
            # ACTUAL order is fine and fills a few ms later. Without
            # this filter, A57 cascades, cancels the (already-filled)
            # parent, AND the engine re-arms a fresh SL_SELL_* on top
            # of the still-pending bracket-child SELL — user reports
            # this as a "random SELL appearing in TWS" (AUDUSD n2 live
            # 2026-06-10).
            #
            # The signature is in IBKR's error 201 message. Skip A57
            # entirely in that case — the original order will fill on
            # its own and the engine's normal fill-handling path takes
            # over.
            is_modify_after_triggered = (
                isinstance(msg, str)
                and ("after order has triggered" in msg.lower()
                     or "error 201" in msg.lower())
            )
            if (
                role == 'CHILD'
                and status in TERMINAL_REJECTS
                and float(filled or 0) == 0.0
                and peer_broker_id
                and not peer_cancel_fired[0]
                and not is_modify_after_triggered
            ):
                peer_cancel_fired[0] = True
                print(
                    f"[BRACKET_LIFECYCLE] CHILD_REJECTED_CANCELLING_PARENT  "
                    f"cid={self.client_id}  child_broker_id={broker_id}  "
                    f"child_engine_id={engine_id}  parent_broker_id={peer_broker_id}  "
                    f"child_status={status}  reason={msg!r}"
                )
                try:
                    parent_trade = None
                    target_id = int(peer_broker_id)
                    for t in self._ib.trades():
                        if t.order.orderId == target_id:
                            parent_trade = t
                            break
                    if parent_trade is not None and not parent_trade.isDone():
                        self._ib.cancelOrder(parent_trade.order)
                        print(
                            f"[BRACKET_LIFECYCLE] CANCEL_SENT  cid={self.client_id}  "
                            f"broker_id={peer_broker_id}  "
                            f"engine_id={self._order_id_map.get(peer_broker_id, '?')}  "
                            f"side=BUY type=? status_at_cancel=? src=A57_child_rejected"
                        )
                    else:
                        print(
                            f"[BRACKET_LIFECYCLE] A57 parent {peer_broker_id} "
                            f"not found in trades or already done — no cancel needed"
                        )
                except Exception as e:
                    # Best-effort: never let the cancel raise out of the
                    # status callback. If this fails the invariant sweep
                    # would eventually catch the orphan parent on its
                    # next 250ms tick.
                    print(
                        f"[BRACKET_LIFECYCLE] A57 parent-cancel raised "
                        f"({type(e).__name__}: {e}); orphan parent may remain "
                        f"until invariant sweep catches it",
                        file=sys.stderr,
                    )
            elif (
                role == 'CHILD'
                and status in TERMINAL_REJECTS
                and is_modify_after_triggered
            ):
                # A65 suppression: child "Cancelled" status was actually
                # IBKR rejecting our modify_stop_trigger AFTER the child
                # had already triggered. The child is mid-fill — don't
                # cancel the parent (it's filled too) and don't tear down
                # _bracket_child in the engine's status handler. The next
                # FILL event will arrive within ~50ms and the normal
                # fill-handling path takes over.
                print(
                    f"[BRACKET_LIFECYCLE] A65_SUPPRESS_PARENT_CANCEL  "
                    f"cid={self.client_id}  child_broker_id={broker_id}  "
                    f"child_engine_id={engine_id}  "
                    f"reason='modify-after-triggered: child is filling, "
                    f"not actually cancelled'"
                )

        trade_obj.statusEvent += on_status

    def _attach_status_watcher(self, trade_obj, broker_id: str) -> None:
        """Subscribe to trade.statusEvent so terminal NON-FILL outcomes
        (Cancelled, Rejected, Inactive) surface to the engine.

        Without this, an order that IBKR rejects (margin failure, halted
        symbol, invalid stop price, etc.) leaves `_pending_stop` set forever
        — the engine thinks an SL is armed and the position is unprotected.

        Status events can fire many times per order (the same Cancelled
        message can be resent on reconnect); we dedup on (engine_id, status)
        via `_terminal_statuses_seen` so the engine only sees one notification.
        """
        if self.paper or trade_obj is None:
            return

        TERMINAL_NOT_FILLED = {'Cancelled', 'Rejected', 'Inactive', 'ApiCancelled'}

        def on_status(_trade):
            try:
                status = _trade.orderStatus.status
            except Exception:
                return
            if status not in TERMINAL_NOT_FILLED:
                return
            engine_id = self._order_id_map.get(broker_id, broker_id)
            key = (engine_id, status)
            if key in self._terminal_statuses_seen:
                return
            self._terminal_statuses_seen.add(key)

            # Pull the rejection message if IBKR gave one
            msg = ""
            try:
                if _trade.log:
                    msg = _trade.log[-1].message or ""
            except Exception:
                pass

            if self._on_order_status:
                self._on_order_status(engine_id, status, msg)

        trade_obj.statusEvent += on_status

    def register_existing_order(
        self, broker_id: str, engine_id: str, trade_obj=None
    ) -> None:
        """Wire an already-resting broker order back into the fill pipeline.

        Adds the broker_id → engine_id mapping and re-attaches BOTH the
        fillEvent (for normal fills) and the statusEvent watcher (for
        Rejected/Cancelled/Inactive terminal states). Same paths used for
        freshly-placed orders.

        No-op for paper mode (no broker, no events).
        """
        if self.paper:
            return
        self._order_id_map[broker_id] = engine_id
        if trade_obj is None:
            return

        def on_fill(_trade, fill):
            if self._on_fill:
                eid = self._order_id_map.get(broker_id, broker_id)
                # fill.execution.time is the broker's authoritative fill
                # timestamp (tz-aware UTC). Forwarded so the engine stamps
                # OrderRecord.filled_at with the real fill moment instead
                # of "when our callback ran".
                # fill.commissionReport.commission is IBKR's all-in dollar
                # commission for THIS execution (base + SEC + FINRA TAF +
                # CAT + clearing + pass-throughs). Forwarded so the order
                # registry uses penny-exact broker numbers instead of the
                # modeled formula. Falls back to None when ib_async hasn't
                # populated the report yet (rare; commissionReport usually
                # arrives before fillEvent fires).
                cr = getattr(fill, 'commissionReport', None)
                ib_commission: Optional[float] = None
                if cr is not None:
                    val = getattr(cr, 'commission', None)
                    if val is not None and val > 0:
                        ib_commission = float(val)
                self._on_fill(
                    eid,
                    fill.execution.shares,
                    fill.execution.price,
                    fill.execution.execId,
                    getattr(fill.execution, 'time', None),
                    ib_commission,
                )

        trade_obj.fillEvent += on_fill
        self._attach_status_watcher(trade_obj, broker_id)
        # Late-arriving commission report — see _attach_commission_watcher.
        self._attach_commission_watcher(trade_obj, broker_id)

        # ── A58 (2026-06-10): re-attach bracket lifecycle logger ────────
        # Without this, brackets that were placed in a PRIOR session and
        # adopted via reconcile lose the A57 child-cancel→parent-cancel
        # symmetry: the lifecycle logger lives where A57's logic lives,
        # and that logger was only attached at fresh placement. So when
        # the chaos test's teardown sweep cancels an adopted child, A57
        # never fires, and the parent stays alive as an orphan BUY.
        #
        # Determine role + peer from the engine_id naming convention
        # (matches `_make_engine_id`):
        #     ENTRY_BUY_*  → bracket parent — peer is the child SELL STP
        #                    with `parentId == self.orderId`
        #     BR_SELL_*    → bracket child  — peer is `order.parentId`
        # Anything else (SL_*, ENTRY_LMT_*, etc.) is not part of a
        # bracket pair and gets no lifecycle logger attached.
        try:
            role = None
            peer_broker_id = ''
            if isinstance(engine_id, str):
                if engine_id.startswith('ENTRY_BUY_'):
                    role = 'PARENT'
                    this_id = int(trade_obj.order.orderId)
                    # Walk trades to find the child (SELL STP with
                    # parentId == this order's orderId). The child may
                    # have already been adopted in this reconcile pass
                    # or will be in a moment — either way `ib.trades()`
                    # exposes it as long as it's still resting at IBKR.
                    for t in self._ib.trades():
                        try:
                            if (int(getattr(t.order, 'parentId', 0) or 0) == this_id
                                    and (t.order.action or '').upper() == 'SELL'):
                                peer_broker_id = str(t.order.orderId)
                                break
                        except Exception:
                            continue
                elif engine_id.startswith('BR_SELL_'):
                    role = 'CHILD'
                    parent_oid = int(getattr(trade_obj.order, 'parentId', 0) or 0)
                    if parent_oid:
                        peer_broker_id = str(parent_oid)
            if role is not None:
                self._attach_bracket_lifecycle_logger(
                    trade_obj, broker_id, role, peer_broker_id
                )
                print(
                    f"[BRACKET_LIFECYCLE] LOGGER_RE_ATTACHED  cid={self.client_id}  "
                    f"role={role}  broker_id={broker_id}  engine_id={engine_id}  "
                    f"peer={peer_broker_id}  src=A58_register_existing_order"
                )
        except Exception as e:
            # Best-effort: never let logger re-attach raise during
            # reconcile. Worst case we lose A57 protection on this
            # bracket and the invariant sweep catches the orphan.
            print(
                f"[A58] lifecycle logger re-attach failed for {broker_id}/{engine_id}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    async def place_order(
        self,
        side: OrderSide,
        qty: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        order_id: str = "",
    ) -> str:
        """Place order - paper or live.

        If `order_id` is provided, fills are reported back through `_on_fill`
        with that same id. In live mode, the IBKR-assigned broker id is
        translated via `_order_id_map`.
        """
        if self.paper:
            return await self._paper_order(side, qty, order_type, limit_price, order_id)

        return await self._live_order(side, qty, order_type, limit_price, order_id)

    def _execute_fill(self, order_id: str, qty: int, price: float, side: OrderSide):
        """Execute a fill - update position and fire callback."""
        # Update paper position
        if self.symbol not in self._paper_positions:
            self._paper_positions[self.symbol] = {"qty": 0, "avg_cost": 0.0}

        p = self._paper_positions[self.symbol]
        if side == OrderSide.BUY:
            total_cost = p["qty"] * p["avg_cost"] + qty * price
            new_qty = p["qty"] + qty
            p["avg_cost"] = total_cost / new_qty if new_qty else 0.0
            p["qty"] = new_qty
        else:
            p["qty"] -= qty

        # Fire fill callback. exec_id is None in paper mode — paper doesn't
        # replay fills, so the registry's dedup-by-exec_id isn't needed.
        if self._on_fill:
            self._on_fill(order_id, qty, price, None)

    async def _paper_order(
        self,
        side: OrderSide,
        qty: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        order_id: str = "",
    ) -> str:
        """Paper order - handles MARKET and LIMIT orders."""
        oid = order_id or f"PAPER_{side.value}_{qty}_{self.symbol}"

        # Guard: only fill once a real tick has arrived. The explicit
        # _has_price flag replaces the old `_last_price != 100.0` sentinel
        # that would silently break if the symbol ever traded at $100.
        has_valid_price = self._has_price and self._last_price > 0

        if order_type == OrderType.LIMIT and limit_price:
            if has_valid_price:
                if side == OrderSide.BUY and self._last_price <= limit_price:
                    # BUY LIMIT: fill when LTP at or below limit
                    self._execute_fill(oid, qty, min(self._last_price, limit_price), side)
                    return oid
                if side == OrderSide.SELL and self._last_price >= limit_price:
                    # SELL LIMIT: fill when LTP at or above limit
                    self._execute_fill(oid, qty, max(self._last_price, limit_price), side)
                    return oid

            # Not yet triggered (or no valid price) - track for later fill
            self._pending_limits[oid] = {
                'side': side,
                'limit': limit_price,
                'qty': qty,
            }
            return oid

        # MARKET order - fill immediately with small slippage
        if not has_valid_price:
            # No real price yet; fall back to cached _last_price without jitter
            self._execute_fill(oid, qty, self._last_price, side)
            return oid

        # Paper-mode MARKET order — spec-aware slippage + snap, same
        # helper as STP fills. Was hardcoded to ±$0.03 / 2dp which
        # made paper FX trades behave like equity (3000 pips of
        # slippage simulated — meaningless).
        import random
        max_slip, snap = _paper_slippage_for_symbol(self.symbol)
        jitter = random.uniform(-max_slip * 1.5, max_slip * 1.5)  # MKT ≈ 1.5x STP slip
        price = snap(self._last_price + jitter)
        self._execute_fill(oid, qty, price, side)
        return oid

    async def _live_order(
        self,
        side: OrderSide,
        qty: int,
        order_type: OrderType,
        limit_price: Optional[float],
        order_id: str = "",
    ) -> str:
        """Live order via ib_async.

        When `order_id` is provided, the IBKR-assigned broker id is mapped to
        it via `_order_id_map` so fill callbacks report the engine's id.
        """
        from ib_async import MarketOrder, LimitOrder

        contract = await self._get_contract()

        # Set TIF explicitly to override any account-level "Order Preset" that
        # would otherwise default to DAY (IBKR warning 10349). A DAY limit
        # would auto-cancel at 4 PM ET, killing the entry order if the trigger
        # hasn't fired same-session. GTC: order rests up to 90 days.
        # MarketOrder fills instantly, so its TIF is cosmetic — keep DAY there
        # both because it's the conventional value and because some IBKR
        # account types reject GTC on MKT orders.
        if order_type == OrderType.LIMIT and limit_price:
            order = LimitOrder(
                action=side.value, totalQuantity=qty,
                lmtPrice=limit_price, tif='GTC',
                outsideRth=True,
            )
        else:
            # outsideRth=True so force_exit / square_off and any other
            # MARKET placement works during ETH (04:00-09:30 ET and
            # 16:00-20:00 ET). Without it IBKR queues the order until
            # 09:30 ET and the user is stuck with the position they
            # tried to flatten (Warning 399).
            order = MarketOrder(
                action=side.value, totalQuantity=qty, tif='DAY',
                outsideRth=True,
            )

        trade = self._ib.placeOrder(contract, order)
        broker_id = str(trade.order.orderId)
        if order_id:
            self._order_id_map[broker_id] = order_id

        def on_fill(_trade, fill):
            # Use fill.execution data, NOT trade.orderStatus: orderStatus is
            # populated by a separate ib_async event that often arrives after
            # fillEvent, leaving avgFillPrice=0.0 when we read it. Also,
            # execution.shares is this-fill qty (matches registry's
            # filled_qty += qty); orderStatus.filled is cumulative and would
            # double-count partial fills.
            # exec_id is passed through so the registry can dedup if IBKR
            # replays this fill on reconnect.
            if self._on_fill:
                engine_id = self._order_id_map.get(broker_id, broker_id)
                # Forward fill.execution.time (tz-aware UTC) so the engine
                # stamps OrderRecord.filled_at with the real broker fill
                # timestamp, not the moment ib_async happened to deliver
                # this callback. Critical for reconnect-replay accuracy.
                self._on_fill(
                    engine_id,
                    fill.execution.shares,
                    fill.execution.price,
                    fill.execution.execId,
                    getattr(fill.execution, 'time', None),
                )

        trade.fillEvent += on_fill
        # statusEvent gives terminal non-fill outcomes (Rejected, Cancelled,
        # Inactive) which fillEvent doesn't cover. Without this, a rejected
        # order leaves _pending_stop hanging and the position unprotected.
        self._attach_status_watcher(trade, broker_id)
        # Late-arriving commission report — see _attach_commission_watcher.
        self._attach_commission_watcher(trade, broker_id)
        # No artificial sleep here. ib_async's placeOrder() is non-blocking
        # and the fillEvent + statusEvent fire asynchronously when IBKR
        # responds. The previous 100ms sleep added that much latency to
        # every entry order for no functional benefit.

        return order_id or broker_id

    async def place_stop_limit(
        self,
        side: OrderSide,
        qty: int,
        stop_price: float,
        limit_price: float,
        order_id: str = "",
    ) -> Optional[float]:
        """
        Place stop-limit order.

        In paper mode: simulates stop-limit behavior
        In live mode: passes to IBKR

        Args:
            side: BUY or SELL
            qty: Order quantity
            stop_price: Trigger price
            limit_price: Limit price (worst fill price)
            order_id: Optional order ID for tracking

        Returns:
            Fill price if filled, None if not yet filled
        """
        if self.paper:
            return await self._paper_stop_limit(side, qty, stop_price, limit_price, order_id)

        return await self._live_stop_limit(side, qty, stop_price, limit_price, order_id)

    async def _paper_stop_limit(
        self,
        side: OrderSide,
        qty: int,
        stop_price: float,
        limit_price: float,
        order_id: str = "",
    ) -> Optional[float]:
        """
        Paper stop-limit simulation.

        Behavior:
        - If LTP crosses stop_price, trigger order
        - Fill at limit_price (or better if market allows)
        """
        ltp = self._last_price

        # Check if stop is already triggered
        triggered = False
        if side == OrderSide.SELL:
            triggered = ltp <= stop_price
        else:
            triggered = ltp >= stop_price

        if triggered:
            # Stop triggered - fill at limit (with small slippage simulation).
            # Spec-aware: equity ±2¢, FX ±0.0001 (half-pip × 2), futures ±half-tick.
            import random
            max_slip, snap = _paper_slippage_for_symbol(self.symbol)
            slip = random.uniform(-max_slip, max_slip)
            fill_price = snap(limit_price + slip)

            # Update position
            if self.symbol not in self._paper_positions:
                self._paper_positions[self.symbol] = {"qty": 0, "avg_cost": 0.0}

            p = self._paper_positions[self.symbol]
            if side == OrderSide.BUY:
                total_cost = p["qty"] * p["avg_cost"] + qty * fill_price
                new_qty = p["qty"] + qty
                p["avg_cost"] = total_cost / new_qty if new_qty else 0.0
                p["qty"] = new_qty
            else:
                p["qty"] -= qty

            # Fire fill callback (paper sim — no exec_id replay risk)
            if self._on_fill:
                oid = order_id or f"PAPER_SL_{side.value}_{qty}_{self.symbol}"
                self._on_fill(oid, qty, fill_price, None)

            return fill_price

        # Not triggered yet - return None (order remains pending)
        return None

    async def _live_stop_limit(
        self,
        side: OrderSide,
        qty: int,
        stop_price: float,
        limit_price: float,
        order_id: str = "",
    ) -> Optional[float]:
        """Live stop-limit via IBKR.

        The order is submitted to IBKR and rests there until the stop triggers;
        the fill arrives asynchronously through `fillEvent` → `_on_fill`. This
        function always returns `None` (no synchronous fill price) to match
        `place_stop_limit`'s contract.

        When `order_id` is provided, the IBKR-assigned broker id is mapped to
        it via `_order_id_map` so fill callbacks report the engine's id.
        """
        from ib_async import StopLimitOrder

        contract = await self._get_contract()

        # IBKR StopLimit: stopPrice = trigger, order with lmtPrice.
        # tif=GTC so the order rests at IBKR until the trigger fires (could
        # be hours/days for breakout entries, longer for protective stops
        # on positions held overnight). Without an explicit tif, IBKR's
        # account preset would apply — typically DAY — which would silently
        # cancel the order at the 4 PM ET close (IBKR warning 10349). For
        # the protective SELL stop, that means walking into next morning
        # with NO stop on an open position.
        #
        # outsideRth=True so the order can fire during pre-market
        # (04:00-09:30 ET) and after-hours (16:00-20:00 ET) as well as RTH.
        # Critical for swing-position protective stops: an overnight news
        # gap that opens NVDA -8% would be missed by an RTH-only stop until
        # 09:30 ET, by which time the limit may be irrelevant. With this
        # flag set, the stop activates from 04:00 ET when liquidity returns.
        # Tradeoff: ETH liquidity is thin and fills can be poor — the user
        # has explicitly accepted this risk in exchange for gap coverage.
        order = StopLimitOrder(
            action=side.value,
            totalQuantity=qty,
            stopPrice=stop_price,
            lmtPrice=limit_price,
            tif='GTC',
            outsideRth=True,
        )
        # Stash our engine_id in IBKR's `orderRef` field. Survives the
        # round-trip back through `ib.openTrades()` / `ib.fills()` so
        # the engine's reconcile path can recover the EXACT engine_id
        # (including the `_n{cycle_seq}` cycle disambiguator) instead
        # of guessing from convention. Guessing was the root cause of
        # the 2026-05-27 TSLA $44k phantom PnL — a leftover order's
        # broker_id resolved back to a same-named id from a new cycle.
        if order_id:
            order.orderRef = order_id

        trade = self._ib.placeOrder(contract, order)
        broker_id = str(trade.order.orderId)
        if order_id:
            self._order_id_map[broker_id] = order_id

        def on_fill(_trade, fill):
            # See _live_order for why we use fill.execution rather than
            # trade.orderStatus: avoids zero-price races and partial-fill
            # double-counting. exec_id enables registry idempotency on
            # IBKR fill replays after reconnect.
            if self._on_fill:
                engine_id = self._order_id_map.get(broker_id, broker_id)
                # Forward fill.execution.time (tz-aware UTC) so the engine
                # stamps OrderRecord.filled_at with the real broker fill
                # timestamp, not the moment ib_async happened to deliver
                # this callback. Critical for reconnect-replay accuracy.
                self._on_fill(
                    engine_id,
                    fill.execution.shares,
                    fill.execution.price,
                    fill.execution.execId,
                    getattr(fill.execution, 'time', None),
                )

        trade.fillEvent += on_fill
        self._attach_status_watcher(trade, broker_id)
        # Late-arriving commission report — see _attach_commission_watcher.
        self._attach_commission_watcher(trade, broker_id)

        return None

    async def place_stop_market(
        self,
        side: OrderSide,
        qty: int,
        stop_price: float,
        order_id: str = "",
    ) -> Optional[float]:
        """Place a standalone stop-MARKET order (no limit).

        Same shape as `place_stop_limit` but fires a MARKET order at the
        trigger instead of a stop-limit pair. Used by the protective-SL
        re-arm path (`_place_protective_stop_inner`) so the order type
        matches the bracket child placed by `place_bracket_buy_stop_market`.

        Why STP-MARKET over STP-LMT for protective stops: in fast
        markets the trigger can fire but the limit gets skipped (LTP
        gaps past both in a single tick) → STP-LMT sits unfilled,
        position bleeds. STP-MARKET guarantees execution at whatever
        the next print is — slippage in exchange for certainty of exit.
        Senior-prescribed after the 2026-05-27 NVDA incident where the
        STP-LMT trigger+limit got skipped together.

        Paper mode: simulates fill at `stop_price` when triggered.
        Live mode: passes to IBKR via `StopOrder`.

        Returns:
            Paper: fill price if already triggered, else None.
            Live:  None (fill arrives async via fillEvent → _on_fill).
        """
        if self.paper:
            return await self._paper_stop_market(side, qty, stop_price, order_id)

        return await self._live_stop_market(side, qty, stop_price, order_id)

    async def _paper_stop_market(
        self,
        side: OrderSide,
        qty: int,
        stop_price: float,
        order_id: str = "",
    ) -> Optional[float]:
        """Paper sim of a stop-market. Mirrors `_paper_stop_limit` but
        fills at `stop_price` (with small slippage) instead of a separate
        limit price — paper doesn't have spread / liquidity to model
        properly, so trigger price ≈ fill price is a reasonable proxy
        and keeps the paper P&L close to the live behaviour.
        """
        ltp = self._last_price

        triggered = (ltp <= stop_price) if side == OrderSide.SELL else (ltp >= stop_price)
        if not triggered:
            return None

        # Spec-aware slippage + snap (see _paper_slippage_for_symbol).
        import random
        max_slip, snap = _paper_slippage_for_symbol(self.symbol)
        slip = random.uniform(-max_slip, max_slip)
        fill_price = snap(stop_price + slip)

        if self.symbol not in self._paper_positions:
            self._paper_positions[self.symbol] = {"qty": 0, "avg_cost": 0.0}
        p = self._paper_positions[self.symbol]
        if side == OrderSide.BUY:
            total_cost = p["qty"] * p["avg_cost"] + qty * fill_price
            new_qty = p["qty"] + qty
            p["avg_cost"] = total_cost / new_qty if new_qty else 0.0
            p["qty"] = new_qty
        else:
            p["qty"] -= qty

        if self._on_fill:
            oid = order_id or f"PAPER_STP_{side.value}_{qty}_{self.symbol}"
            self._on_fill(oid, qty, fill_price, None)
        return fill_price

    async def _live_stop_market(
        self,
        side: OrderSide,
        qty: int,
        stop_price: float,
        order_id: str = "",
    ) -> Optional[float]:
        """Live stop-market via IBKR. Mirrors `_live_stop_limit` minus
        the limit price."""
        from ib_async import StopOrder

        contract = await self._get_contract()

        order = StopOrder(
            action=side.value,
            totalQuantity=qty,
            stopPrice=stop_price,
            tif='GTC',
            outsideRth=True,
        )
        # Stash engine_id in IBKR's `orderRef` field — same as the
        # bracket child + STP-LMT placement paths. Lets reconcile
        # recover the exact per-cycle engine_id on restart.
        if order_id:
            order.orderRef = order_id

        trade = self._ib.placeOrder(contract, order)
        broker_id = str(trade.order.orderId)
        if order_id:
            self._order_id_map[broker_id] = order_id

        def on_fill(_trade, fill):
            if self._on_fill:
                engine_id = self._order_id_map.get(broker_id, broker_id)
                cr = getattr(fill, 'commissionReport', None)
                ib_commission = None
                if cr is not None:
                    val = getattr(cr, 'commission', None)
                    if val is not None and val > 0:
                        ib_commission = float(val)
                self._on_fill(
                    engine_id,
                    fill.execution.shares,
                    fill.execution.price,
                    fill.execution.execId,
                    getattr(fill.execution, 'time', None),
                    ib_commission,
                )

        trade.fillEvent += on_fill
        self._attach_status_watcher(trade, broker_id)
        # Late-arriving commission report — see _attach_commission_watcher.
        self._attach_commission_watcher(trade, broker_id)

        return None

    async def place_bracket_buy_stop_market(
        self,
        qty: int,
        parent_stop_price: float,
        parent_limit_price: float,
        child_stop_price: float,
        parent_order_id: str,
        child_order_id: str,
        parent_market: bool = False,
    ) -> Optional[tuple]:
        """Submit a BUY STP-LMT parent + SELL STP (market on trigger) child
        as a single IBKR bracket. The child SELL rests at the broker the
        instant the bracket is accepted, even though it's marked
        "Pending Submit" until the parent fills — so a disconnect or
        crash between submit and fill leaves the position protected at
        the broker, not by the engine.

        `parent_market=True` swaps the parent for a plain BUY MARKET
        (config.entry_market / --market). Everything else about the
        bracket is unchanged: same parentId + transmit handshake, same
        child, same orphan-cancel guard. Only the parent's own trigger
        goes away — the caller is asserting that the entry condition has
        already been met upstream, so there is nothing left to wait for.
        `parent_stop_price` / `parent_limit_price` are then ignored for
        placement, though the caller still uses the trigger to size the
        child's initial stop.

        Why MARKET on the child instead of STP-LMT: in fast markets the
        trigger fires but the limit can be skipped (LTP gaps past both
        in one tick) → STP-LMT sits unfilled, position bleeds. STP-MARKET
        guarantees execution at whatever the next print is — slippage in
        exchange for certainty of exit. Senior-prescribed after the
        2026-05-27 $300 NVDA incident.

        Returns (parent_broker_id, child_broker_id) on success, None on
        paper mode (which falls back to the legacy two-step placement —
        paper has no parent-id semantics). Engine_ids are mapped via
        `_order_id_map` so subsequent fill callbacks report engine ids.

        IMPORTANT: order matters. Parent goes first with `transmit=False`
        (IBKR buffers it client-side), then child with `transmit=True`
        flushes both to the exchange atomically. If we sent child first
        the broker would reject it (orphan parentId).
        """
        if self.paper:
            # Paper sim: no parent-id semantics. The engine falls back to
            # the legacy "submit BUY first, place SL on fill" path. We
            # signal this via None so the caller knows to take the
            # legacy code path. Paper users hit the same risk as before
            # (naked window between BUY fill and SL placement), but
            # paper is for testing so it's acceptable.
            return None

        from ib_async import MarketOrder, StopLimitOrder, StopOrder

        contract = await self._get_contract()

        # NOTE on bracket atomicity (REVERTED A20 — OCA broke modify path):
        # The earlier A20 fix added ocaGroup + ocaType to both legs. That
        # made IBKR auto-cancel the child if parent cancelled, but it ALSO
        # rejected any later modify of the child with Error 10326
        # "OCA group revision is not allowed" — forcing the engine into a
        # cancel-then-replace fallback that briefly exposed two orders in
        # TWS. The atomic-cancel-on-parent-cancel behavior is ALREADY
        # provided by IBKR's native parentId semantics for brackets:
        # when the parent is cancelled (manually or via OCA elsewhere),
        # the child auto-cancels. No explicit OCA needed for this path.
        # The phantom n2 SELL in the original incident was a stale order
        # from a prior session — addressed by A19 (executions backfill)
        # and A21 (session-id in engine_id), not by OCA.

        # PARENT: BUY STP-LMT (or plain MARKET), transmit=False so IBKR
        # buffers it client-side until the child arrives.
        if parent_market:
            # tif is cosmetic on a market order — it fills on arrival — but
            # DAY matches the convention in `_live_order`. outsideRth=True so
            # an ETH entry is not silently queued until 09:30 ET (Warning 399).
            parent = MarketOrder(
                action=OrderSide.BUY.value,
                totalQuantity=qty,
                tif='DAY',
                outsideRth=True,
            )
        else:
            parent = StopLimitOrder(
                action=OrderSide.BUY.value,
                totalQuantity=qty,
                stopPrice=parent_stop_price,
                lmtPrice=parent_limit_price,
                tif='GTC',
                outsideRth=True,
            )
        parent.transmit = False
        # Reserve a broker order id so the child can reference parentId
        # before parent is actually placed. ib_async's nextValidId is
        # incremented for both placeOrder calls below.
        parent.orderId = self._ib.client.getReqId()
        # Stash engine_id in orderRef — see _live_stop_limit comment for
        # why. Lets reconcile recover the per-cycle disambiguated id.
        if parent_order_id:
            parent.orderRef = parent_order_id

        # CHILD: SELL STP (market on trigger), parentId = parent's id.
        # `transmit=True` flushes both to IBKR atomically.
        # NO ocaGroup — see big comment above.
        child = StopOrder(
            action=OrderSide.SELL.value,
            totalQuantity=qty,
            stopPrice=child_stop_price,
            tif='GTC',
            outsideRth=True,
        )
        child.parentId = parent.orderId
        child.transmit = True
        child.orderId = self._ib.client.getReqId()
        if child_order_id:
            child.orderRef = child_order_id

        # A50 REVERTED (A51): attaching ExecutionCondition to the child
        # caused both parent + child to stay in client-side "Transmit"
        # status (never sent to exchange). The bracket-with-parentId
        # and conditional-order semantics conflict in ib_async/IBKR.
        # Reverted to clean parentId-only bracket; A48 invariant sweep
        # remains the safety net for orphan detection (≤250ms exposure).

        # Place parent (buffered), then child (transmits both).
        #
        # SAFETY: if placeOrder(child) raises or is interrupted AFTER
        # placeOrder(parent) succeeded, the parent is left at IBKR with
        # transmit=False — visible in TWS as a permanent "Transmit"
        # status order that will never fire on its own. Live regression
        # 2026-06-09 USDCHF: user spotted a transmit-stuck BUY STP-LMT
        # in TWS during the restart-positions chaos test. Failure modes
        # for the child call: IBKR rejection (bad price, contract not
        # qualified, rate limit), ib_async serialization error, asyncio
        # task cancellation. Without this guard, every such failure
        # leaves dead orders accumulating at the broker.
        parent_trade = self._ib.placeOrder(contract, parent)
        # A52 lifecycle log: parent placed
        print(
            f"[BRACKET_LIFECYCLE] PLACE_PARENT  cid={self.client_id} "
            f"engine_id={parent_order_id}  broker_id={parent_trade.order.orderId}  "
            + ("type=MKT (no resting trigger)  "
               if parent_market
               else f"stop={parent_stop_price}  limit={parent_limit_price}  ")
            + "transmit=False (held until child)"
        )
        # A75 (2026-06-10): brief ingest gap between parent & child to
        # eliminate the "Error 135, Can't find order with id=N" race.
        # Both messages travel the same TCP socket so they ARRIVE in
        # order — but IBKR's order-management thread can process them
        # out-of-order under load (live regression: 3 of 32 bots hit it
        # during simultaneous reconnect-respawn). When that happens, the
        # child sees no parent and the broker rejects it with Error 135
        # → A57 cancels the orphan parent → engine retries 30s later
        # (cosmetic latency, no exposure leak, but visible as a phantom
        # "extra" BUY in TWS during the gap).
        #
        # 50ms is enough to win the race under all observed conditions
        # without meaningfully delaying normal bracket entry (a real
        # bracket entry takes 100-500ms end-to-end already from network
        # RTT + ack). Same path used by FX and equity, no behavior
        # change beyond the added micro-delay.
        await asyncio.sleep(0.05)
        try:
            child_trade = self._ib.placeOrder(contract, child)
            # A52 lifecycle log: child placed (and bracket transmitted)
            print(
                f"[BRACKET_LIFECYCLE] PLACE_CHILD   cid={self.client_id} "
                f"engine_id={child_order_id}  broker_id={child_trade.order.orderId}  "
                f"parent_broker_id={parent_trade.order.orderId}  "
                f"stop={child_stop_price}  transmit=True (flushes both)"
            )
        except Exception as e:
            # Cancel the orphan parent so it doesn't linger untransmitted.
            try:
                self._ib.cancelOrder(parent_trade.order)
            except Exception as cancel_err:
                print(
                    f"[Gateway] CRITICAL: bracket child placement failed "
                    f"({type(e).__name__}: {e}) AND parent cancel also "
                    f"failed ({type(cancel_err).__name__}: {cancel_err}). "
                    f"Parent order {parent.orderId} may be stuck "
                    f"untransmitted at IBKR — operator must cancel it "
                    f"manually in TWS.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[Gateway] Bracket child placement failed "
                    f"({type(e).__name__}: {e}); cancelled orphan parent "
                    f"{parent.orderId} to keep IBKR clean.",
                    file=sys.stderr,
                )
            # Re-raise so the engine knows the bracket placement failed
            # and can decide whether to retry / alert / fall back to legacy.
            raise

        parent_broker_id = str(parent_trade.order.orderId)
        child_broker_id = str(child_trade.order.orderId)

        # Map engine ids ↔ broker ids so fill callbacks report engine ids.
        if parent_order_id:
            self._order_id_map[parent_broker_id] = parent_order_id
        if child_order_id:
            self._order_id_map[child_broker_id] = child_order_id

        # Wire fillEvent for each leg. Both legs share the same callback
        # shape as `_live_stop_limit`. The engine's `_on_gateway_fill`
        # dispatches by `order.side` from the registry, so parent fills
        # land in the BUY branch and child fills in the SELL branch
        # without any additional discriminator here.
        def _make_on_fill(broker_id_local: str, role_local: str, peer_local: str):
            def on_fill(_trade, fill):
                # A52 lifecycle log: any fill on a bracket leg.
                eid = self._order_id_map.get(broker_id_local, broker_id_local)
                try:
                    exec_id = fill.execution.execId
                    shares = fill.execution.shares
                    price = fill.execution.price
                    ftime = getattr(fill.execution, 'time', None)
                except Exception:
                    exec_id = shares = price = ftime = None
                print(
                    f"[BRACKET_LIFECYCLE] FILL    cid={self.client_id}  "
                    f"role={role_local}  broker_id={broker_id_local}  "
                    f"engine_id={eid}  peer={peer_local}  "
                    f"shares={shares} price={price} execId={exec_id} time={ftime}"
                )
                if self._on_fill:
                    cr = getattr(fill, 'commissionReport', None)
                    ib_commission = None
                    if cr is not None:
                        val = getattr(cr, 'commission', None)
                        if val is not None and val > 0:
                            ib_commission = float(val)
                    self._on_fill(
                        eid,
                        fill.execution.shares,
                        fill.execution.price,
                        fill.execution.execId,
                        getattr(fill.execution, 'time', None),
                        ib_commission,
                    )
            return on_fill

        parent_trade.fillEvent += _make_on_fill(parent_broker_id, 'PARENT', child_broker_id)
        child_trade.fillEvent += _make_on_fill(child_broker_id, 'CHILD', parent_broker_id)
        self._attach_status_watcher(parent_trade, parent_broker_id)
        self._attach_status_watcher(child_trade, child_broker_id)
        # A52: full-fidelity bracket lifecycle trace for both legs
        self._attach_bracket_lifecycle_logger(parent_trade, parent_broker_id, 'PARENT', child_broker_id)
        self._attach_bracket_lifecycle_logger(child_trade, child_broker_id, 'CHILD', parent_broker_id)
        # Late-arriving commission reports for BOTH legs.
        self._attach_commission_watcher(parent_trade, parent_broker_id)
        self._attach_commission_watcher(child_trade, child_broker_id)

        return (parent_broker_id, child_broker_id)

    async def modify_stop_trigger(
        self,
        order_id: str,
        new_stop_price: float,
        new_qty: Optional[int] = None,
    ) -> bool:
        """Modify an existing STP order's trigger price (and optionally qty)
        IN PLACE at the broker — atomic at IBKR, NO cancel/replace gap.

        Used by the bracket flow after the parent BUY fills: the child SELL
        STP was submitted with an estimated stop_price (based on the
        BUY trigger) — once we know the actual VWAP we modify the child
        to use the real fill-price-based stop.

        Returns True on success, False if the order can't be found or
        the broker rejected the modify. Best-effort: callers should
        fall back to cancel+replace if this returns False AND the
        position is at risk.

        `order_id` accepts either a broker order_id (str int) OR an
        engine_id that's been mapped via `_order_id_map`.
        """
        if self.paper:
            # Paper: update the in-memory pending stop's trigger
            for pid, info in list(self._pending_limits.items()):
                if pid == order_id and info.get('side') == OrderSide.SELL:
                    info['stop_price'] = new_stop_price
                    if new_qty is not None:
                        info['qty'] = new_qty
                    return True
            return False

        # Live mode: refuse to "modify" while disconnected. Returning
        # False silently used to mean "order not found" — caller would
        # then escalate to cancel+replace, which could create a duplicate
        # orphan stop at the broker once the socket recovers.
        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                f"Gateway not connected; cannot modify {order_id}. Caller "
                f"must NOT escalate to cancel+replace (would create orphan)."
            )

        # Resolve engine_id → broker_id if needed
        broker_id = order_id
        for bid, eid in self._order_id_map.items():
            if eid == order_id:
                broker_id = bid
                break

        try:
            broker_order_id_int = int(broker_id)
        except (TypeError, ValueError):
            print(f"[modify_stop_trigger] non-numeric broker id: {broker_id}", file=sys.stderr)
            return False

        # Find the live Trade object whose order.orderId matches
        try:
            for trade in self._ib.trades():
                if trade.order.orderId == broker_order_id_int:
                    # Update fields in place; re-call placeOrder with
                    # the same Order object = modify semantics in IBKR.
                    trade.order.auxPrice = new_stop_price
                    # `auxPrice` is the trigger for STP orders. For STP LMT
                    # `lmtPrice` would also need updating, but the bracket
                    # child is plain STP (market on trigger).
                    if new_qty is not None and new_qty > 0:
                        trade.order.totalQuantity = new_qty
                    self._ib.placeOrder(trade.contract, trade.order)
                    return True
        except Exception as e:
            # Distinguish "I don't know" from "order missing": socket
            # disconnect during the call is "unknown", not "missing".
            # Engine's cancel+replace fallback should only fire on
            # "missing", not on transient socket errors.
            print(f"[modify_stop_trigger] {broker_id}: {type(e).__name__}: {e}", file=sys.stderr)
            if self._ib is None or not self._ib.isConnected():
                raise ConnectionError(
                    f"Socket died during modify_stop_trigger({broker_id}). "
                    f"Caller must NOT escalate to cancel+replace."
                ) from e
            return False

        # Order not found in live trades — already terminal or never
        # accepted. Caller's fallback path (cancel + place fresh SL)
        # is the right escalation.
        return False

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel order. Accepts either a broker order id (str int) or the
        engine_id we mapped to it via _order_id_map.

        Returns:
          True  — cancel request was sent to the broker successfully.
          False — order not found in our local list of working trades
                  (already terminal or never accepted).

        Raises ConnectionError if disconnected — caller must not
        interpret missing cancel as "order doesn't exist." Loud over
        silent (the prior `except: pass` swallowed real socket errors)."""
        if self.paper:
            return True

        if self._ib is None or not self._ib.isConnected() or not self.connected:
            raise ConnectionError(
                f"Gateway not connected; cancel for {order_id} not sent. "
                f"Caller must NOT assume order is cancelled."
            )

        # Translate engine_id → broker_id if needed
        broker_id = order_id
        if order_id in self._order_id_map.values():
            broker_id = next(
                (bid for bid, eid in self._order_id_map.items() if eid == order_id),
                order_id,
            )

        try:
            for trade in self._ib.trades():
                if str(trade.order.orderId) == broker_id:
                    # A52 lifecycle log: cancel request leaving us.
                    # Tag bracket legs (engine_id starts with BR_BUY/BR_SELL)
                    # so a single grep BRACKET_LIFECYCLE shows who killed what.
                    eid = self._order_id_map.get(broker_id, order_id)
                    is_bracket = isinstance(eid, str) and (
                        eid.startswith('BR_BUY_') or eid.startswith('BR_SELL_')
                    )
                    if is_bracket:
                        try:
                            status = trade.orderStatus.status
                            side = trade.order.action
                            otype = trade.order.orderType
                        except Exception:
                            status = side = otype = '?'
                        print(
                            f"[BRACKET_LIFECYCLE] CANCEL_SENT  cid={self.client_id}  "
                            f"engine_id={eid}  broker_id={broker_id}  "
                            f"side={side} type={otype} status_at_cancel={status}"
                        )
                    self._ib.cancelOrder(trade.order)
                    return True
        except Exception as e:
            # Log + distinguish disconnect from "order missing" — was
            # `except: pass`, which swallowed real socket errors and let
            # caller treat "I don't know" as "no such order".
            print(
                f"[cancel_order] {order_id}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            if self._ib is None or not self._ib.isConnected():
                raise ConnectionError(
                    f"Socket died during cancel_order({order_id})."
                ) from e
        return False

    async def modify_order(
        self,
        order_id: str,
        *,
        qty: Optional[int] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> bool:
        """Modify a resting order in place by re-issuing `placeOrder` with the
        same orderId. IBKR treats same-orderId placeOrder as atomic modify —
        the order's existing partial fills are preserved.

        Used by the partial-fill manager to:
          (a) resize a protective SELL stop as new BUY partials arrive (qty + stop/limit)
          (b) chase a stuck BUY entry by bumping its limit price upward (limit_price)

        Returns True if a matching working order was found and modified.

        Why modify, not cancel+replace: cancel+replace creates a brief
        window where the order is gone from the broker. If the SL fires
        in that window we have no protection. Modify is atomic at IBKR.
        """
        if self.paper:
            return True
        if not self.connected:
            return False

        # Translate engine_id → broker_id if our map has it
        broker_id = order_id
        if order_id in self._order_id_map.values():
            broker_id = next(
                (bid for bid, eid in self._order_id_map.items() if eid == order_id),
                order_id,
            )

        try:
            for trade in self._ib.trades():
                if str(trade.order.orderId) != broker_id:
                    continue
                if trade.isDone():
                    return False  # nothing to modify
                order = trade.order
                # Apply requested changes; leave others as-is. IBKR uses
                # auxPrice for STP/STPLMT trigger, lmtPrice for limit.
                if qty is not None:
                    order.totalQuantity = qty
                if limit_price is not None:
                    order.lmtPrice = float(limit_price)
                if stop_price is not None:
                    order.auxPrice = float(stop_price)
                self._ib.placeOrder(trade.contract, order)
                return True
        except Exception as e:
            print(f"[Gateway] modify_order({order_id}) failed: {e}")
        return False

    async def cancel_all(self) -> int:
        """Cancel all orders."""
        if self.paper:
            self._paper_positions.clear()
            return 1

        cancelled = 0
        if self.connected:
            for trade in self._ib.trades():
                if not trade.isDone():
                    self._ib.cancelOrder(trade.order)
                    cancelled += 1
        return cancelled

    def update_price(self, price: float):
        """Update cached price (call from market data subscription).

        Sets `_has_price=True` on the first real tick. Downstream paper-fill
        logic reads this flag rather than guessing from `_last_price`, so a
        symbol that genuinely trades at $100.00 fills correctly.
        """
        if price > 0:
            self._last_price = price
            self._has_price = True
            self._last_heartbeat = self._ts()
            # Check pending LIMIT orders
            self._check_pending_limits(price)

    def mark_data_alive(self) -> None:
        """Refresh the market-data heartbeat on ANY incoming tick.

        `update_price()` only advances `_last_heartbeat` when it gets a
        `last > 0` trade print. Quote-driven assets — index/FX CFDs — have
        NO trade prints (BBO-only), so `update_price` never fires for them
        and the heartbeat would freeze at connect time. The risk gate's
        `_is_price_fresh()` (60s window) would then block every entry with
        "Price stale" ~60s after startup — even while bid/ask ticks stream
        normally. Call this on every tick, before the `last > 0` gate, so
        the heartbeat tracks feed liveness for all asset classes.
        """
        self._last_heartbeat = self._ts()

    def _check_pending_limits(self, ltp: float):
        """Check if any pending LIMIT orders should fill."""
        if not self._pending_limits:
            return

        # Guard: only fill once a real tick has arrived. Pre-tick the
        # cached _last_price is 0.0 and _has_price is False — both block.
        # No magic $100 sentinel.
        if not self._has_price or ltp <= 0:
            return

        filled = []
        for order_id, info in self._pending_limits.items():
            side = info['side']
            limit_price = info['limit']
            qty = info['qty']

            should_fill = False
            fill_price = limit_price

            if side == OrderSide.BUY and ltp <= limit_price:
                # BUY LIMIT: LTP at or below limit - fill at LTP (cheaper than limit)
                should_fill = True
                fill_price = min(ltp, limit_price)
            elif side == OrderSide.SELL and ltp >= limit_price:
                # SELL LIMIT: LTP at or above limit - fill at LTP (better than limit)
                should_fill = True
                fill_price = max(ltp, limit_price)

            if should_fill:
                # Fill at LTP (better price for you)
                self._execute_fill(order_id, qty, fill_price, side)
                filled.append(order_id)

        # Remove filled orders
        for order_id in filled:
            del self._pending_limits[order_id]

    def start_polling(self, callback: Callable[[float], None], interval: int = 5):
        """Start price polling subprocess with queue-based callback."""
        if self.symbol in ("INFY",):
            exchange, currency = "NSE", "INR"
        elif self.symbol in ("ASML", "SAP", "NVD", "SHELL", "ULVR"):
            exchange, currency = "SMART", "EUR"
        else:
            exchange, currency = "SMART", "USD"

        poll_script = rf"""
import sys, json, time
from datetime import datetime
import nest_asyncio
nest_asyncio.apply()

# Note: asyncio.timeouts.timeout monkey-patch removed — ib_async supports
# Python 3.14 natively. If you re-enable the legacy ib_insync, the patch
# would go here, but globally disabling asyncio timeouts is unsafe and
# should not return.

from ib_async import IB, Stock

host = "{self.host}"
port = {self.port}
client_id = {self.client_id + 200}
symbol = "{self.symbol}"
exchange = "{exchange}"
currency = "{currency}"
poll_seconds = {interval}

ib = IB()
ib.connect(host, port, clientId=client_id)
ib.reqMarketDataType(2)

contract = Stock(symbol, exchange, currency)
qualified = ib.qualifyContracts(contract)
if not qualified:
    sys.stdout.write(json.dumps({{"type": "error", "msg": f"Contract not qualified for {{symbol}}"}}) + "\n")
    sys.stdout.flush()
    sys.exit(1)

c = qualified[0]
sys.stdout.write(json.dumps({{"type": "connected", "symbol": symbol}}) + "\n")
sys.stdout.flush()

while True:
    try:
        bars = ib.reqHistoricalData(c, endDateTime="", durationStr="1 D",
            barSizeSetting="1 min", whatToShow="TRADES", useRTH=True, formatDate=1)
        if bars:
            now = datetime.now()
            sys.stdout.write(json.dumps({{
                "type": "tick",
                "price": bars[-1].close,
                "timestamp": now.isoformat(),
            }}) + "\n")
            sys.stdout.flush()
        else:
            sys.stdout.write(json.dumps({{"type": "no_bars"}}) + "\n")
            sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(json.dumps({{"type": "error", "msg": str(e)}}) + "\n")
        sys.stdout.flush()
    time.sleep(poll_seconds)
"""
        self._poll_proc = subprocess.Popen(
            [sys.executable, "-c", poll_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )

        self._poll_callback = callback
        self._poll_queue = asyncio.Queue()

        def read_loop():
            for line in self._poll_proc.stdout:
                try:
                    msg = json.loads(line.strip())
                    msg_type = msg.get("type", "unknown")
                    if msg_type == "tick":
                        price = float(msg["price"])
                        self.update_price(price)
                        self._poll_queue.put_nowait(price)
                    elif msg_type == "error":
                        print(f"[poll error] {msg.get('msg', 'unknown')}", flush=True)
                    elif msg_type == "connected":
                        print(f"[poll] connected: {msg.get('symbol', '')}", flush=True)
                    elif msg_type == "no_bars":
                        print(f"[poll] no bars returned", flush=True)
                except Exception as e:
                    print(f"[poll read error] {e}", flush=True)

        threading.Thread(target=read_loop, daemon=True, name="poll_reader").start()

        # Start queue processor if we have an async loop running
        self._running = True
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self._process_queue())
        except RuntimeError:
            pass  # No running loop yet

    async def _process_queue(self):
        """Process prices from poll queue."""
        while self._running:
            try:
                price = await asyncio.wait_for(self._poll_queue.get(), timeout=1.0)
                if self._poll_callback:
                    await self._poll_callback(price)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[queue error] {e}")

    def stop_polling(self):
        """Stop price polling."""
        if self._poll_proc:
            self._poll_proc.terminate()
            self._poll_proc = None

    def start_streaming(self, callback: Callable[[float], None]):
        """
        Start real-time streaming market data subscription.
        Uses reqMktData for instant tick updates (not polling).
        Much faster than polling - updates on every trade.
        """
        if not self._ib or not self._ib.isConnected():
            print("[stream] not connected")
            return

        from ib_async import Stock

        # Determine exchange/currency
        if self.symbol in ("INFY",):
            exchange, currency = "NSE", "INR"
        elif self.symbol in ("ASML", "SAP", "NVD", "SHELL", "ULVR"):
            exchange, currency = "SMART", "EUR"
        else:
            exchange, currency = "SMART", "USD"

        contract = Stock(self.symbol, exchange, currency)
        qualified = self._ib.qualifyContracts(contract)
        if qualified:
            self._contract = qualified[0]
        else:
            self._contract = contract

        self._mkt_data = self._ib.reqMktData(
            self._contract,
            snapshot=False,  # No snapshot - streaming only
        )

        # Tick event handler - pass full Ticker for complete data
        def on_tick(ticker):
            self._last_heartbeat = self._ts()
            callback(ticker)  # Pass full Ticker object

        self._mkt_data.updateEvent += on_tick

    def stop_streaming(self):
        """Stop streaming market data."""
        if self._mkt_data and self._ib:
            try:
                self._ib.cancelMktData(self._mkt_data)
            except:
                pass
            self._mkt_data = None

    async def _subscribe_account_summary(self) -> None:
        """Issue ONE `reqAccountSummary` at connect time.

        ib_async pushes updates into its internal `accountSummary()` cache
        whenever IBKR sends a new value (typically every few seconds, or
        whenever something material changes — a fill, a price move that
        moves NetLiq, etc). We hook `accountSummaryEvent` to mirror those
        updates into `self._account_cache` so `get_account_value()` is
        a pure dict lookup with no IBKR calls and no failure modes.

        Idempotent — safe to call multiple times; the second + onwards
        no-op out via `_account_summary_subscribed`.
        """
        if self._account_summary_subscribed or self._ib is None:
            return
        try:
            # ib_async exposes both sync (reqAccountSummary) and async
            # (reqAccountSummaryAsync) variants depending on version. Try
            # async first since we're already in an async context.
            if hasattr(self._ib, 'reqAccountSummaryAsync'):
                await self._ib.reqAccountSummaryAsync()
            else:
                # Older sync variant — returns immediately after issuing
                # the subscription request, the actual data lands via
                # the event stream.
                self._ib.reqAccountSummary()

            # Wire the event so changes mirror into our local cache.
            # Signature in ib_async: AccountValue(account, tag, value, currency, modelCode)
            def _on_acct_update(v):
                try:
                    self._account_cache[v.tag] = float(v.value)
                except (ValueError, TypeError, AttributeError):
                    # Some tags are non-numeric (eg "AccountType"); ignore.
                    pass

            try:
                self._ib.accountSummaryEvent += _on_acct_update
            except Exception:
                # Some ib_async versions use a different event name. Fall
                # back to seeding the cache from the snapshot — fresh
                # values will still arrive on the next `accountSummary()`
                # poll path, but updates may be slower.
                pass

            # Seed the cache with whatever's already cached in ib_async
            # (the subscribe call above usually returns synchronously with
            # the first snapshot). Without this seed, the first second of
            # operation reads from an empty dict.
            try:
                for item in self._ib.accountSummary():
                    try:
                        self._account_cache[item.tag] = float(item.value)
                    except (ValueError, TypeError):
                        pass
            except Exception:
                pass

            self._account_summary_subscribed = True
            print(f"[Gateway] Account summary subscribed; {len(self._account_cache)} initial tags cached")
        except Exception as e:
            # Subscription failure is non-fatal. Account values will read
            # as 0.0 → risk module treats as "unknown" → equity-based
            # gates skip until we recover. Connection itself is fine.
            print(f"[Gateway] Account summary subscribe failed: {type(e).__name__}: {e}")

    async def _subscribe_pnl_single(self) -> None:
        """Subscribe to IBKR's per-contract P&L stream (`reqPnLSingle`) for
        the qualified contract.

        This is the AUTHORITATIVE source for realized/unrealized P&L on the
        open position — IBKR computes it against its own average cost and
        mark price, and keeps it correct across multi-day holds (overnight
        carry/financing, daily marks, cost-basis adjustments, corporate
        actions). Our engine's mark-to-market (`(last - entry) * qty`) can't
        see any of that, which is why the dashboard used to drift from TWS.

        Updates arrive on `pnlSingleEvent`; we mirror them into
        `_pnl_single_cache[conId]` so `get_ibkr_pnl()` is a pure dict read
        with no IBKR round-trip on the hot path — same pattern as the
        account-summary cache.

        Idempotent per conId. No-op in paper mode (there is no real IBKR
        position to price) and when the contract isn't qualified yet.
        """
        if self.paper or self._ib is None or not self._ib.isConnected():
            return
        contract = self._contract
        con_id = int(getattr(contract, 'conId', 0) or 0) if contract else 0
        if con_id <= 0:
            # Contract not qualified yet — connect() calls us after
            # `_get_contract()`, and reconnect re-qualifies, so this only
            # skips the pre-qualify window.
            return
        if con_id in self._pnl_single_conids:
            return

        # Discover the account reqPnLSingle must be scoped to. Single-login
        # accounts can pass "" but IBKR is stricter for some account types,
        # so use the explicit code from managedAccounts() when available.
        if not self._account_code:
            try:
                accts = self._ib.managedAccounts()
                if accts:
                    self._account_code = accts[0]
            except Exception:
                self._account_code = ""

        def _on_pnl_single(entry) -> None:
            # ib_async PnLSingle: account, modelCode, conId, position,
            # dailyPnL, unrealizedPnL, realizedPnL, value. IBKR sends NaN
            # (not 0) for a value it hasn't computed yet — coerce those to
            # None so downstream code can tell "unknown" from "flat".
            try:
                cid = int(getattr(entry, 'conId', 0) or 0)
                if cid <= 0:
                    return

                def _num(x):
                    try:
                        xf = float(x)
                    except (TypeError, ValueError):
                        return None
                    # IBKR uses a huge sentinel / NaN for "not available".
                    if xf != xf or abs(xf) > 1e17:
                        return None
                    return xf

                self._pnl_single_cache[cid] = {
                    'daily_pnl': _num(getattr(entry, 'dailyPnL', None)),
                    'unrealized_pnl': _num(getattr(entry, 'unrealizedPnL', None)),
                    'realized_pnl': _num(getattr(entry, 'realizedPnL', None)),
                    'position': _num(getattr(entry, 'position', None)),
                    'value': _num(getattr(entry, 'value', None)),
                    'ts': self._ts().isoformat(),
                }
            except Exception:
                pass

        try:
            try:
                self._ib.pnlSingleEvent += _on_pnl_single
            except Exception:
                # Some ib_async versions expose a different event name; the
                # returned object is still live-updated, so seeding below
                # keeps the cache usable even if the event hook fails.
                pass

            pnl_obj = self._ib.reqPnLSingle(self._account_code, "", con_id)
            self._pnl_single_conids.add(con_id)
            # Seed immediately from the returned object (usually still NaN
            # until IBKR's first push lands ~1s later, but harmless).
            if pnl_obj is not None:
                _on_pnl_single(pnl_obj)
            print(
                f"[Gateway] reqPnLSingle subscribed  acct={self._account_code or '(default)'}  "
                f"conId={con_id}  sym={self.symbol}"
            )
        except Exception as e:
            # Non-fatal: dashboards fall back to the engine-computed P&L when
            # no IBKR value is cached. Connection + trading are unaffected.
            print(f"[Gateway] reqPnLSingle subscribe failed: {type(e).__name__}: {e}")

    def _cancel_pnl_single(self, old_ib=None) -> None:
        """Cancel any live reqPnLSingle subscriptions. Called on disconnect
        so a stale handle doesn't linger across reconnect (which re-qualifies
        and re-subscribes). `old_ib` lets connect() cancel on the previous IB
        handle it's about to discard."""
        ib = old_ib if old_ib is not None else self._ib
        if ib is not None:
            for cid in list(self._pnl_single_conids):
                try:
                    ib.cancelPnLSingle(self._account_code, "", cid)
                except Exception:
                    pass
        self._pnl_single_conids.clear()
        # Keep the last-known values in _pnl_single_cache — the dashboard
        # shows them dimmed as "last seen" rather than blanking on a blip.

    def get_ibkr_pnl(self, con_id: int | None = None) -> dict | None:
        """IBKR's per-contract P&L for the open position, straight from
        `reqPnLSingle`. Pure cache read — ZERO IBKR calls.

        Returns a dict with `daily_pnl`, `unrealized_pnl`, `realized_pnl`,
        `position`, `value` (any may be None if IBKR hasn't computed it yet),
        plus `ts`. Returns None when:
          - paper mode (no real broker position),
          - the subscription hasn't landed yet (first ~1s after entry),
          - no conId (contract not qualified / flat).

        Callers must treat None (and None fields) as "unknown" and fall back
        to the engine-computed P&L.
        """
        if self.paper:
            return None
        if con_id is None:
            contract = self._contract
            con_id = int(getattr(contract, 'conId', 0) or 0) if contract else 0
        if not con_id or con_id <= 0:
            return None
        return self._pnl_single_cache.get(int(con_id))

    def get_account_value(self, key: str) -> float:
        """Get account value from the local cache populated by the
        `accountSummaryEvent` stream. ZERO IBKR calls per invocation.

        Returns 0.0 when:
          - not connected
          - the subscription hasn't landed yet (first ~1s after connect)
          - IBKR doesn't push this tag for the account type

        Callers must treat 0.0 as "unknown" (the risk module already does).
        """
        if not self.connected:
            return 0.0
        return self._account_cache.get(key, 0.0)

    def get_equity(self) -> float:
        # Returns 0.0 when account data is unavailable (no IBKR connection,
        # or accountSummary not yet populated). The risk module treats 0
        # as "unknown" and refuses orders — safer than the old $1M fallback
        # that would let a $950k order through against a real $100k account.
        return self.get_account_value("NetLiquidation")

    def get_buying_power(self) -> float:
        """Account buying power (`BuyingPower` from accountSummary).

        For a margin account this is roughly 2-4× equity (cash + margin
        loan capacity). For a cash account it equals available cash.
        Returns 0.0 when account data hasn't been pushed yet.

        Used by the dashboard's "BP USED" pill to show position notional
        as a fraction of buying power — complements the equity-based
        exposure % which doesn't capture margin headroom.
        """
        return self.get_account_value("BuyingPower")

    def get_cash(self) -> float:
        return self.get_account_value("TotalCashValue") or 0.0
