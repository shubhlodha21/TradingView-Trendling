"""Interactive Brokers live feed.

Implements the :class:`~rth.feeds.PriceFeed` contract against TWS or IB Gateway,
so the same trendline/crossing engine that runs on simulated data runs on a real
account with no other changes.

Three pieces:

``IBKRSession``  owns the socket. One connection, one background thread, any
                 number of instruments subscribed to it. IB counts *connections*
                 against a client-id, not symbols, so watching thirty tickers
                 should not open thirty sockets.
``IBKRTicker``   one instrument's quote stream off a session. This is the object
                 that satisfies ``PriceFeed`` -- one per trendline.
``IBKRFeed``     the single-ticker convenience case: a session with exactly one
                 subscription, started and stopped as a unit.

The IB API is asyncio-based and wants to own an event loop. Rather than force
the caller into async, the connection lives in a background thread with its own
loop: it streams quotes continuously while the main thread walks the market
clock at whatever cadence it likes and reads the latest snapshot. That is what
"running together" means here -- the socket never waits on the printer, and the
printer never blocks on the socket.

Requires TWS or IB Gateway running locally with the API enabled
(Configure -> API -> Settings -> "Enable ActiveX and Socket Clients").
Default ports:

    7497  TWS paper          7496  TWS live
    4002  IB Gateway paper   4001  IB Gateway live

Install the client library::

    pip install ib_async          # maintained successor to ib_insync
"""

from __future__ import annotations

import asyncio
import math
import threading
from dataclasses import dataclass

import pandas as pd

from .feeds import PriceFeed
from .sessions import to_utc

# Market data types, per IB's reqMarketDataType.
LIVE, FROZEN, DELAYED, DELAYED_FROZEN = 1, 2, 3, 4

# A standard account gets 100 concurrent streaming lines. Past that IB rejects
# further subscriptions rather than queueing them, so warn before it happens.
MAX_STREAMING_LINES = 100

# Default routing per security type. Overridable per instrument.
_DEFAULT_EXCHANGE = {
    "STK": "SMART",
    "CASH": "IDEALPRO",
    "CRYPTO": "PAXOS",
    "FUT": "GLOBEX",
    "CFD": "SMART",
    "IND": "CBOE",
}

# TRADES needs actual prints; FX and CFDs have none, so they quote midpoint.
_DEFAULT_WHAT_TO_SHOW = {
    "CASH": "MIDPOINT",
    "CFD": "MIDPOINT",
    "IND": "TRADES",
    "CRYPTO": "AGGTRADES",     # IB rejects TRADES on the Paxos venue
}

_FX_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "DKK", "SGD", "HKD", "CNH", "MXN", "ZAR", "TRY", "PLN",
}

# Yahoo-style suffixes -> (IB exchange, currency). Handy when the same ticker
# string is being fed to both this and the Yahoo feed.
_SUFFIX_ROUTES = {
    ".L": ("LSE", "GBP"), ".DE": ("IBIS", "EUR"), ".PA": ("SBF", "EUR"),
    ".AS": ("AEB", "EUR"), ".SW": ("EBS", "CHF"), ".MI": ("BVME", "EUR"),
    ".MC": ("BM", "EUR"), ".ST": ("SFB", "SEK"), ".T": ("TSEJ", "JPY"),
    ".HK": ("SEHK", "HKD"), ".SI": ("SGX", "SGD"), ".AX": ("ASX", "AUD"),
    ".TO": ("TSE", "CAD"), ".NS": ("NSE", "INR"), ".BO": ("BSE", "INR"),
}

_BAR_SIZES = {
    1: "1 secs", 5: "5 secs", 10: "10 secs", 15: "15 secs", 30: "30 secs",
    60: "1 min", 120: "2 mins", 300: "5 mins", 600: "10 mins", 900: "15 mins",
    1800: "30 mins", 3600: "1 hour",
}

# "Market data farm connection is OK" and friends arrive on the error channel
# but are status, not failure. Anything else gets attributed to its instrument.
_INFO_ERROR_CODES = {1100, 1101, 1102, 2100, 2103, 2104, 2105, 2106, 2107,
                     2108, 2119, 2137, 2150, 2158}


def _import_ib():
    """Import the client library lazily, so `import rth` works without it."""
    try:
        import ib_async as ib
        return ib
    except ImportError:
        pass
    try:
        import ib_insync as ib          # older name, same API surface
        return ib
    except ImportError:
        raise ImportError(
            "The IBKR feed needs the Interactive Brokers client library.\n"
            "    pip install ib_async\n"
            "and make sure TWS or IB Gateway is running with the API enabled."
        ) from None


def _num(value) -> float | None:
    """IB reports 'no data' as NaN or -1 depending on the field."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or out <= 0:
        return None
    return out


def guess_sec_type(ticker: str) -> str:
    """Best-effort STK / CASH / CRYPTO from the ticker string."""
    t = (ticker or "").strip().upper()
    base = t.replace("/", "").replace("_", "").replace("=X", "")
    if len(base) == 6 and base[:3] in _FX_CODES and base[3:] in _FX_CODES:
        return "CASH"
    if base[:3] in {"XAU", "XAG", "XPT", "XPD"} and len(base) == 6:
        return "CASH"
    if t.split("-")[0] in {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LTC"}:
        return "CRYPTO"
    return "STK"


# --------------------------------------------------------------------------- #
# what to subscribe to
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ContractSpec:
    """A ticker string resolved into IB's contract fields."""

    ticker: str
    sec_type: str
    symbol: str
    exchange: str
    currency: str
    primary_exchange: str | None = None
    expiry: str | None = None

    @classmethod
    def resolve(
        cls,
        ticker: str,
        sec_type: str = "auto",
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
        expiry: str | None = None,
    ) -> "ContractSpec":
        t = (ticker or "").strip().upper()
        if not t:
            raise ValueError("A ticker is required.")

        kind = (sec_type or "auto").strip().upper()
        kind = guess_sec_type(t) if kind in ("", "AUTO") else kind
        default_exchange = _DEFAULT_EXCHANGE.get(kind, "SMART")

        if kind == "CASH":
            base = t.replace("/", "").replace("_", "").replace("=X", "")
            symbol, venue, ccy = (
                (base[:3], default_exchange, base[3:]) if len(base) == 6
                else (base, default_exchange, "USD")
            )
        elif kind == "CRYPTO":
            symbol = t.split("-")[0].split("/")[0]
            venue = default_exchange
            ccy = t.split("-")[-1] if "-" in t else "USD"
        else:
            symbol, venue, ccy = t, default_exchange, "USD"
            for suffix, (suffix_venue, suffix_ccy) in _SUFFIX_ROUTES.items():
                if t.endswith(suffix):
                    symbol, venue, ccy = t.removesuffix(suffix), suffix_venue, suffix_ccy
                    break

        return cls(
            ticker=t,
            sec_type=kind,
            symbol=symbol,
            exchange=exchange or venue,
            currency=currency or ccy,
            primary_exchange=primary_exchange,
            expiry=expiry,
        )

    @property
    def what_to_show(self) -> str:
        return _DEFAULT_WHAT_TO_SHOW.get(self.sec_type, "TRADES")

    def build(self):
        """An unqualified IB Contract for this spec."""
        ib = _import_ib()
        contract = ib.Contract(
            secType=self.sec_type,
            symbol=self.symbol,
            exchange=self.exchange,
            currency=self.currency,
        )
        if self.primary_exchange:
            contract.primaryExchange = self.primary_exchange
        if self.expiry:
            contract.lastTradeDateOrContractMonth = self.expiry
        return contract

    def describe(self) -> str:
        return f"{self.symbol} {self.sec_type} {self.exchange}/{self.currency}"


# --------------------------------------------------------------------------- #
# one instrument
# --------------------------------------------------------------------------- #

class IBKRTicker(PriceFeed):
    """One instrument's live quote stream, fed by an :class:`IBKRSession`.

    ``tick()`` is non-blocking: it returns the most recent quote the session
    thread has received, together with the high/low seen *since the previous
    call*, which is exactly the intrabar range touch-mode triggers need.
    """

    name = "ibkr"

    def __init__(self, session: "IBKRSession", spec: ContractSpec):
        self.session = session
        self.spec = spec
        self.ticker = spec.ticker
        self.contract = None
        self.stream = None
        self.subscribed = False
        self.error: str | None = None

        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._updates = 0

    def __repr__(self) -> str:
        return f"<IBKRTicker {self.ticker} {self.spec.describe()}>"

    @property
    def what_to_show(self) -> str:
        return self.spec.what_to_show

    @property
    def update_count(self) -> int:
        return self._updates

    @property
    def has_quote(self) -> bool:
        return self._snapshot is not None

    @property
    def qualified_description(self) -> str:
        if self.contract is None:
            return self.spec.describe()
        text = (f"{self.contract.symbol} {self.contract.secType} "
                f"{self.contract.exchange}/{self.contract.currency}")
        con_id = getattr(self.contract, "conId", 0)
        return f"{text} conId={con_id}" if con_id else text

    # -- quote intake (runs on the session thread) --------------------------

    def _on_update(self, stream) -> None:
        last = _num(getattr(stream, "last", None))
        bid = _num(getattr(stream, "bid", None))
        ask = _num(getattr(stream, "ask", None))
        close = _num(getattr(stream, "close", None))

        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        # Prefer a real print; fall back to the book, then to the prior close so
        # an instrument that has not traded yet still yields a usable reference.
        price = last if last is not None else (mid if mid is not None else close)
        if price is None:
            return

        quote_time = getattr(stream, "time", None)
        stamp = to_utc(quote_time) if quote_time is not None else pd.Timestamp.now(tz="UTC")

        with self._lock:
            self._updates += 1
            self._high = price if self._high is None else max(self._high, price)
            self._low = price if self._low is None else min(self._low, price)
            self._snapshot = {
                "price": float(price),
                "last": last,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "close": close,
                "bid_size": _num(getattr(stream, "bidSize", None)),
                "ask_size": _num(getattr(stream, "askSize", None)),
                "volume": _num(getattr(stream, "volume", None)),
                "quote_time": stamp,
            }

    # -- PriceFeed ----------------------------------------------------------

    def tick(self, at=None) -> dict | None:
        """Latest quote, with the high/low seen since the previous call.

        ``timestamp`` is stamped with ``at`` when given, so the engine's clock
        stays authoritative; ``quote_time`` and ``age_seconds`` report how fresh
        the underlying quote actually is.
        """
        with self._lock:
            snapshot = dict(self._snapshot) if self._snapshot else None
            high, low = self._high, self._low
            self._high = self._low = None       # start a fresh intrabar window

        if snapshot is None:
            return None

        price = snapshot["price"]
        stamp = to_utc(at) if at is not None else snapshot["quote_time"]
        snapshot.update(
            timestamp=stamp,
            high=float(high if high is not None else price),
            low=float(low if low is not None else price),
            # Clamped: `at` is usually floored to the second, so a quote that
            # arrived mid-second would otherwise read as negative age.
            age_seconds=max(0.0, float((stamp - snapshot["quote_time"]).total_seconds())),
            source="ibkr",
            ticker=self.ticker,
        )
        return snapshot

    def bars(self, start, end, step_seconds: float = 1.0) -> pd.DataFrame:
        """Historical bars over [start, end] via reqHistoricalData.

        IB's pacing rules are strict and second-resolution history is capped at
        ~30 minutes per request, so treat this as a warm-up/backfill helper
        rather than a bulk download.
        """
        return self.session.historical(self, start, end, step_seconds)


# --------------------------------------------------------------------------- #
# the connection
# --------------------------------------------------------------------------- #

class IBKRSession:
    """One TWS/IB Gateway connection, shared by any number of instruments.

    ::

        session = IBKRSession(port=7497)
        aapl = session.add("AAPL")
        eur  = session.add("EURUSD")
        session.start()
        ...
        aapl.tick(now), eur.tick(now)
        session.stop()

    A subscription that IB rejects (bad symbol, no data permission) records the
    reason on ``IBKRTicker.error`` and is skipped -- one bad ticker in a basket
    does not take the rest down.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 17,
        market_data_type: int = LIVE,
        connect_timeout: float = 15.0,
        on_error=None,
    ):
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.market_data_type = int(market_data_type)
        self.connect_timeout = float(connect_timeout)
        self.on_error = on_error            # callable(IBKRTicker | None, str)

        self.ib = None
        self.subscriptions: list[IBKRTicker] = []
        self._by_con_id: dict[int, IBKRTicker] = {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._error: BaseException | None = None

    # -- subscriptions ------------------------------------------------------

    def add(self, ticker: str, **routing) -> IBKRTicker:
        """Register an instrument. Works before or after :meth:`start`."""
        subscription = IBKRTicker(self, ContractSpec.resolve(ticker, **routing))
        self._attach(subscription)
        return subscription

    def _attach(self, subscription: IBKRTicker) -> None:
        self.subscriptions.append(subscription)
        if len(self.subscriptions) > MAX_STREAMING_LINES:
            self._report(
                subscription,
                f"this is streaming line {len(self.subscriptions)}; a standard "
                f"account allows {MAX_STREAMING_LINES} at once and IB will "
                f"reject the excess",
            )
        if self._ready.is_set() and self._loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._subscribe(subscription), self._loop
            ).result(timeout=30)

    @property
    def live_subscriptions(self) -> list[IBKRTicker]:
        return [s for s in self.subscriptions if s.subscribed]

    @property
    def failures(self) -> list[IBKRTicker]:
        return [s for s in self.subscriptions if s.error and not s.subscribed]

    def _report(self, subscription: IBKRTicker | None, message: str) -> None:
        if subscription is not None:
            subscription.error = message
        if self.on_error is not None:
            self.on_error(subscription, message)

    # -- lifecycle ----------------------------------------------------------

    def start(self, timeout: float | None = None) -> "IBKRSession":
        """Connect and subscribe. Blocks until every registered instrument has
        been acknowledged (not until the first quote -- a shut market sends
        none)."""
        if self._thread is not None:
            return self

        _import_ib()                       # fail fast, on the caller's thread
        self._thread = threading.Thread(target=self._run, name="ibkr-session", daemon=True)
        self._thread.start()

        wait = self.connect_timeout + 10.0 + 2.0 * len(self.subscriptions) \
            if timeout is None else timeout
        if not self._ready.wait(wait):
            self.stop()
            raise TimeoutError(
                f"No response from IB at {self.host}:{self.port} after {wait:g}s. "
                "Is TWS/IB Gateway running with the API enabled, and is the port right?"
            )
        if self._error is not None:
            raise ConnectionError(f"IB connection failed: {self._error}") from self._error
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

    def __enter__(self) -> "IBKRSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def connected(self) -> bool:
        return bool(self.ib is not None and self.ib.isConnected())

    # -- background thread --------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._session())
        except BaseException as exc:                  # noqa: BLE001 - surfaced to caller
            self._error = exc
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _session(self) -> None:
        ib_mod = _import_ib()
        self.ib = ib_mod.IB()
        self.ib.errorEvent += self._on_ib_error
        await self.ib.connectAsync(
            self.host, self.port, clientId=self.client_id, timeout=self.connect_timeout
        )
        self.ib.reqMarketDataType(self.market_data_type)

        for subscription in list(self.subscriptions):
            await self._subscribe(subscription)
        self._ready.set()

        try:
            while not self._stopping.is_set():
                await asyncio.sleep(0.05)
        finally:
            for subscription in self.live_subscriptions:
                try:
                    self.ib.cancelMktData(subscription.contract)
                except Exception:
                    pass
            self.ib.disconnect()

    async def _subscribe(self, subscription: IBKRTicker) -> None:
        """Qualify one contract and open its quote stream. Never raises: a
        rejection is recorded against the instrument and the rest carry on."""
        if subscription.subscribed:
            return
        try:
            contract = subscription.spec.build()
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                self._report(
                    subscription,
                    f"IB could not identify {subscription.spec.describe()} -- check "
                    "the symbol, exchange and currency",
                )
                return
            subscription.contract = qualified[0]
            con_id = getattr(subscription.contract, "conId", 0)
            if con_id:
                self._by_con_id[con_id] = subscription

            stream = self.ib.reqMktData(subscription.contract, "", False, False)
            stream.updateEvent += subscription._on_update
            subscription.stream = stream
            subscription.subscribed = True
            subscription.error = None
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            self._report(subscription, f"subscription failed: {exc}")

    def _on_ib_error(self, req_id, code, message, contract=None) -> None:
        """IB's error channel carries status messages too; filter and route."""
        if code in _INFO_ERROR_CODES:
            return
        subscription = None
        con_id = getattr(contract, "conId", 0) if contract is not None else 0
        if con_id:
            subscription = self._by_con_id.get(con_id)
        if subscription is None and contract is not None:
            symbol = getattr(contract, "symbol", None)
            subscription = next(
                (s for s in self.subscriptions if s.spec.symbol == symbol), None
            )
        label = subscription.ticker if subscription else "session"
        text = f"IB error {code} on {label}: {message}"
        if self.on_error is not None:
            self.on_error(subscription, text)
        elif subscription is not None:
            subscription.error = text

    # -- historical ---------------------------------------------------------

    def historical(self, subscription: IBKRTicker, start, end,
                   step_seconds: float = 1.0) -> pd.DataFrame:
        if self._loop is None or not self.connected:
            raise RuntimeError("Call start() before requesting bars.")
        if not subscription.subscribed:
            raise RuntimeError(
                f"{subscription.ticker} is not subscribed: {subscription.error}"
            )

        a, b = to_utc(start), to_utc(end)
        if b <= a:
            return PriceFeed._empty()

        bar_size = _BAR_SIZES.get(int(step_seconds))
        if bar_size is None:
            raise ValueError(
                f"IB has no {step_seconds:g}s bar size; choose from {sorted(_BAR_SIZES)}"
            )

        span = int((b - a).total_seconds()) + int(step_seconds)
        duration = f"{span} S" if span <= 86400 else f"{max(1, span // 86400) + 1} D"

        future = asyncio.run_coroutine_threadsafe(
            self.ib.reqHistoricalDataAsync(
                subscription.contract,
                endDateTime=b.to_pydatetime(),
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=subscription.what_to_show,
                useRTH=False,               # the RthClock does the filtering
                formatDate=2,               # UTC epoch seconds
            ),
            self._loop,
        )
        rows = future.result(timeout=120)
        if not rows:
            return PriceFeed._empty()

        frame = pd.DataFrame([
            {
                "timestamp": to_utc(bar.date),
                "price": float(bar.close),
                "high": float(bar.high),
                "low": float(bar.low),
            }
            for bar in rows
        ])
        return frame[
            (frame["timestamp"] >= a) & (frame["timestamp"] <= b)
        ].sort_values("timestamp").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# single-instrument convenience
# --------------------------------------------------------------------------- #

class IBKRFeed(IBKRTicker):
    """One session, one instrument -- the simple case.

    Equivalent to building an :class:`IBKRSession` and calling ``add()`` once,
    but starts and stops as a single object.
    """

    def __init__(
        self,
        ticker: str,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 17,
        sec_type: str = "auto",
        exchange: str | None = None,
        currency: str | None = None,
        primary_exchange: str | None = None,
        expiry: str | None = None,
        market_data_type: int = LIVE,
        connect_timeout: float = 15.0,
        on_error=None,
    ):
        session = IBKRSession(
            host=host, port=port, client_id=client_id,
            market_data_type=market_data_type, connect_timeout=connect_timeout,
            on_error=on_error,
        )
        spec = ContractSpec.resolve(
            ticker, sec_type=sec_type, exchange=exchange, currency=currency,
            primary_exchange=primary_exchange, expiry=expiry,
        )
        super().__init__(session, spec)
        session.subscriptions.append(self)

    # Routing fields, kept flat for convenience.
    @property
    def sec_type(self) -> str:
        return self.spec.sec_type

    @property
    def symbol(self) -> str:
        return self.spec.symbol

    @property
    def exchange(self) -> str:
        return self.spec.exchange

    @property
    def currency(self) -> str:
        return self.spec.currency

    @property
    def ib(self):
        return self.session.ib

    @property
    def connected(self) -> bool:
        return self.session.connected

    def start(self, timeout: float | None = None) -> "IBKRFeed":
        self.session.start(timeout)
        return self

    def stop(self) -> None:
        self.session.stop()

    def __enter__(self) -> "IBKRFeed":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _build_contract(self):
        """Kept for callers that want the unqualified contract."""
        return self.spec.build()