"""Checks for the IBKR feed and the headless runner, single and multi-ticker.

No broker required: the IB client library is replaced with a stub that speaks
the same handful of methods, so the connection thread, the quote plumbing, the
per-instrument error routing and the signal path are all exercised offline.

Run with:  python -m tests.test_ibkr
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import threading
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from rth import ibkr

PASS, FAIL = [], []


def check(name, got, want, tol=1e-9):
    numeric = isinstance(want, (int, float)) and not isinstance(want, bool)
    ok = abs(got - want) <= tol if numeric else got == want
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        got={got!r} want={want!r}")


def truthy(name, got):
    (PASS if got else FAIL).append(name)
    print(f"[{'PASS' if got else 'FAIL'}] {name}  -> {got!r}")


NAN = float("nan")

# Symbols the stub broker refuses to recognise, so rejection can be tested.
UNKNOWN_SYMBOLS = {"NOPE"}


# --------------------------------------------------------------------------- #
# stub IB client
# --------------------------------------------------------------------------- #

class _Event(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self

    def fire(self, *args):
        for fn in list(self):
            fn(*args)


class _Ticker:
    def __init__(self, contract=None):
        self.contract = contract
        self.last = self.bid = self.ask = self.close = NAN
        self.bidSize = self.askSize = self.volume = NAN
        self.time = None
        self.updateEvent = _Event()

    def push(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)
        self.time = pd.Timestamp.now(tz="UTC").to_pydatetime()
        self.updateEvent.fire(self)


class _Contract:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.conId = 0


class _IB:
    """Enough of the IB client to drive IBKRSession."""

    instances: list["_IB"] = []

    def __init__(self):
        self._connected = False
        self.cancelled: list[str] = []
        self.disconnected = False
        self.market_data_type = None
        self.streams: dict[str, _Ticker] = {}
        self.errorEvent = _Event()
        self._next_con_id = 1000
        _IB.instances.append(self)

    async def connectAsync(self, host, port, clientId=1, timeout=10):
        await asyncio.sleep(0.01)
        self._connected = True

    def isConnected(self):
        return self._connected

    def reqMarketDataType(self, value):
        self.market_data_type = value

    async def qualifyContractsAsync(self, contract):
        if contract.symbol in UNKNOWN_SYMBOLS:
            self.errorEvent.fire(1, 200, "No security definition has been found",
                                 contract)
            return []
        self._next_con_id += 1
        contract.conId = self._next_con_id
        return [contract]

    def reqMktData(self, contract, *args):
        stream = _Ticker(contract)
        self.streams[contract.symbol] = stream
        return stream

    def cancelMktData(self, contract):
        self.cancelled.append(contract.symbol)

    def disconnect(self):
        self._connected = False
        self.disconnected = True

    async def reqHistoricalDataAsync(self, contract, **kw):
        self.last_request = kw
        return [
            types.SimpleNamespace(date=pd.Timestamp("2025-01-06 14:30", tz="UTC"),
                                  close=205.5, high=205.8, low=205.1),
            types.SimpleNamespace(date=pd.Timestamp("2025-01-06 14:31", tz="UTC"),
                                  close=206.0, high=206.2, low=205.4),
        ]


ibkr._import_ib = lambda: types.SimpleNamespace(IB=_IB, Contract=_Contract)


def stream_of(ticker: str) -> _Ticker:
    """The stub's quote stream for a ticker. Keyed by IB symbol, so EURUSD -> EUR."""
    return _IB.instances[-1].streams[ibkr.ContractSpec.resolve(ticker).symbol]


# --------------------------------------------------------------------------- #
print("\n=== contract routing ===")

routes = {
    "AAPL":    ("STK", "AAPL", "SMART", "USD"),
    "EURUSD":  ("CASH", "EUR", "IDEALPRO", "USD"),
    "EUR/USD": ("CASH", "EUR", "IDEALPRO", "USD"),
    "XAUUSD":  ("CASH", "XAU", "IDEALPRO", "USD"),
    "BTC-USD": ("CRYPTO", "BTC", "PAXOS", "USD"),
    "VOD.L":   ("STK", "VOD", "LSE", "GBP"),
    "7203.T":  ("STK", "7203", "TSEJ", "JPY"),
}
for ticker, want in routes.items():
    spec = ibkr.ContractSpec.resolve(ticker)
    check(f"{ticker} routes correctly",
          (spec.sec_type, spec.symbol, spec.exchange, spec.currency), want)

override = ibkr.ContractSpec.resolve("AAPL", exchange="ISLAND", primary_exchange="NASDAQ")
check("explicit exchange wins", override.build().exchange, "ISLAND")
check("primary exchange passes through", override.build().primaryExchange, "NASDAQ")
check("futures carry their expiry",
      ibkr.ContractSpec.resolve("ES", sec_type="FUT", expiry="202609")
      .build().lastTradeDateOrContractMonth, "202609")
check("FX quotes midpoint", ibkr.ContractSpec.resolve("EURUSD").what_to_show, "MIDPOINT")
check("crypto quotes aggtrades", ibkr.ContractSpec.resolve("BTC-USD").what_to_show,
      "AGGTRADES")


# --------------------------------------------------------------------------- #
print("\n=== one instrument: quote plumbing ===")

feed = ibkr.IBKRFeed("AAPL", market_data_type=ibkr.DELAYED)
feed.start(timeout=10)
truthy("connects", feed.connected)
check("market data type is honoured", feed.ib.market_data_type, ibkr.DELAYED)
truthy("contract is qualified", feed.contract.conId > 0)
check("no quote yet means no tick", feed.tick(), None)

stream = feed.ib.streams["AAPL"]
for price in (205.10, 205.90, 205.40):
    stream.push(last=price)
    time.sleep(0.005)

now = pd.Timestamp("2025-01-06 15:00", tz="UTC")
obs = feed.tick(now)
check("price is the latest print", obs["price"], 205.40)
check("high spans the window since the last read", obs["high"], 205.90)
check("low spans the window since the last read", obs["low"], 205.10)
check("timestamp comes from the engine clock", obs["timestamp"], now)
truthy("age is never negative", obs["age_seconds"] >= 0)

stream.push(last=206.00)
obs = feed.tick(now)
check("the high/low window resets on read", (obs["high"], obs["low"]), (206.0, 206.0))

# No prints -- fall back to the book, then to the prior close.
stream.push(last=NAN, bid=205.0, ask=205.2)
check("falls back to the midpoint", feed.tick(now)["price"], 205.1)
stream.push(last=NAN, bid=NAN, ask=NAN, close=204.5)
check("falls back to the prior close", feed.tick(now)["price"], 204.5)
# IB sends -1 for "no data on this field"; that must not become a price.
stream.push(last=-1.0, bid=-1.0, ask=-1.0, close=204.5)
check("a -1 field is not treated as a price", feed.tick(now)["price"], 204.5)

bars = feed.bars("2025-01-06 14:00", "2025-01-06 15:00", 60)
check("historical bars come back shaped for the engine", list(bars.columns),
      ["timestamp", "price", "high", "low"])
check("bar count", len(bars), 2)
check("bar size is translated for IB", feed.ib.last_request["barSizeSetting"], "1 min")
truthy("RTH filtering is left to the clock", feed.ib.last_request["useRTH"] is False)

held = feed.ib
feed.stop()
truthy("subscription is cancelled on stop", held.cancelled == ["AAPL"])
truthy("socket is closed on stop", held.disconnected)
truthy("the feed thread exits",
       not any(t.name == "ibkr-session" for t in threading.enumerate()))


# --------------------------------------------------------------------------- #
print("\n=== many instruments on one connection ===")

errors: list[tuple[str | None, str]] = []
session = ibkr.IBKRSession(
    port=4002, on_error=lambda sub, msg: errors.append((sub.ticker if sub else None, msg))
)
basket = {t: session.add(t) for t in ("AAPL", "MSFT", "EURUSD", "NOPE")}
session.start(timeout=15)

check("one socket for the whole basket", len(_IB.instances[-1].streams), 3)
truthy("every good ticker is subscribed",
       all(basket[t].subscribed for t in ("AAPL", "MSFT", "EURUSD")))
truthy("the bad ticker is not", not basket["NOPE"].subscribed)
truthy("...and says why", "could not identify" in (basket["NOPE"].error or ""))
check("failures are listed", [s.ticker for s in session.failures], ["NOPE"])
check("live subscriptions are listed",
      [s.ticker for s in session.live_subscriptions], ["AAPL", "MSFT", "EURUSD"])
truthy("the rejection was reported to the caller",
       any(t == "NOPE" for t, _ in errors))
truthy("IB's own error for the bad symbol is routed to it",
       any("No security definition" in m for _, m in errors))

ib = _IB.instances[-1]
stream_of("AAPL").push(last=205.0)
stream_of("MSFT").push(last=410.0)
stream_of("EURUSD").push(last=1.0850)
time.sleep(0.02)

check("AAPL quote is its own", basket["AAPL"].tick(now)["price"], 205.0)
check("MSFT quote is its own", basket["MSFT"].tick(now)["price"], 410.0)
check("EURUSD quote is its own", basket["EURUSD"].tick(now)["price"], 1.0850)
check("a rejected ticker yields no quote", basket["NOPE"].tick(now), None)

# Quotes stay separated when they interleave.
stream_of("AAPL").push(last=206.0)
stream_of("MSFT").push(last=409.0)
stream_of("AAPL").push(last=207.0)
time.sleep(0.02)
check("interleaved updates do not cross instruments",
      (basket["AAPL"].tick(now)["price"], basket["MSFT"].tick(now)["price"]),
      (207.0, 409.0))

late = session.add("TSLA")
truthy("an instrument can join after start", late.subscribed)
stream_of("TSLA").push(last=250.0)
time.sleep(0.02)
check("...and quotes immediately", late.tick(now)["price"], 250.0)

session.stop()
check("every stream is cancelled on stop", sorted(ib.cancelled),
      ["AAPL", "EUR", "MSFT", "TSLA"])
truthy("the session thread exits",
       not any(t.name == "ibkr-session" for t in threading.enumerate()))


# --------------------------------------------------------------------------- #
print("\n=== config parsing ===")

import live  # noqa: E402 - imported after the stub is installed

spec = live.spec_from_flag("AAPL,2026-08-07 14:30,205,2026-08-12 19:00,212,UP", 1)
check("--line parses", (spec.ticker, spec.price1, spec.price2, spec.direction),
      ("AAPL", 205.0, 212.0, "UP"))
check("naive times are UTC", str(spec.time1), "2026-08-07 14:30:00+00:00")
check("DOWN survives", live.spec_from_flag("X,2026-01-01,1,2026-01-02,2,down", 1).direction,
      "DOWN")
check("SELL is DOWN", live.spec_from_flag("X,2026-01-01,1,2026-01-02,2,sell", 1).direction,
      "DOWN")

# The five inputs, whatever the column headers happen to be called.
aliased = live.spec_from_mapping({
    "Symbol": "msft", "Past Time": "2026-08-07 14:30", "Past Value": "410",
    "Future Time": "2026-08-12 19:00", "Future Value": "395", "Side": "DOWN",
}, 1)
check("alias columns resolve",
      (aliased.ticker, aliased.price1, aliased.price2, aliased.direction),
      ("MSFT", 410.0, 395.0, "DOWN"))

config = pathlib.Path(__file__).with_name("_lines_test.csv")
config.write_text(live.EXAMPLE_CONFIG, encoding="utf-8")
specs = live.load_config(str(config))
check("the shipped template loads", [s.ticker for s in specs],
      ["AAPL", "MSFT", "EURUSD", "BTC-USD"])
check("comments are ignored", len(specs), 4)
config.unlink()

try:
    live.spec_from_mapping({"ticker": "AAPL", "time1": "2026-01-01"}, 3)
    truthy("a short row is rejected", False)
except ValueError as exc:
    truthy("a short row is rejected", "missing" in str(exc))


# --only is what lets one config file drive several processes (one per tmux
# pane), so it has to select exactly and complain about anything it cannot find.
def _args(**overrides):
    parsed = live.build_parser().parse_args(
        ["--config", str(config)] + overrides.pop("argv", [])
    )
    for key, value in overrides.items():
        setattr(parsed, key, value)
    return parsed


config.write_text(live.EXAMPLE_CONFIG, encoding="utf-8")
check("--only selects a subset",
      [s.ticker for s in live.load_specs(_args(argv=["--only", "MSFT",
                                                     "--only", "eurusd"]))],
      ["MSFT", "EURUSD"])
check("without --only everything loads",
      len(live.load_specs(_args())), 4)
try:
    live.load_specs(_args(argv=["--only", "GOOGL"]))
    truthy("--only rejects an unknown ticker", False)
except ValueError as exc:
    truthy("--only rejects an unknown ticker", "not in this config" in str(exc))
config.unlink()


# --------------------------------------------------------------------------- #
print("\n=== end to end: three instruments through the runner ===")

sessions: list[ibkr.IBKRSession] = []
_original_start = ibkr.IBKRSession.start


def _record(self, timeout=None):
    sessions.append(self)
    return _original_start(self, timeout)


ibkr.IBKRSession.start = _record


def _pump():
    """AAPL climbs through its line, MSFT drops through its, EURUSD stays put."""
    while not sessions or not sessions[-1].connected:
        time.sleep(0.05)
    time.sleep(0.6)
    for aapl, msft, eur in ((99.0, 101.0, 1.0850),
                            (99.5, 100.5, 1.0851),
                            (101.0, 99.0, 1.0849)):
        stream_of("AAPL").push(last=aapl)
        stream_of("MSFT").push(last=msft)
        stream_of("EURUSD").push(last=eur)
        time.sleep(1.0)


threading.Thread(target=_pump, daemon=True).start()

log = pathlib.Path(__file__).with_name("_signals_test.csv")
log.unlink(missing_ok=True)
start = pd.Timestamp.now(tz="UTC").floor("s") - pd.Timedelta(hours=1)
end = start + pd.Timedelta(hours=2)

code = live.main([
    # CRYPTO:SPOT is open 24/7, so the loop runs whenever the tests do.
    "--line", f"AAPL,{start},100,{end},100,UP,CRYPTO:SPOT",
    "--line", f"MSFT,{start},100,{end},100,DOWN,CRYPTO:SPOT",
    "--line", f"EURUSD,{start},1.0900,{end},1.0900,UP,CRYPTO:SPOT",
    "--interval", "1", "--max-ticks", "5", "--quiet",
    "--log-signals", str(log),
])
ibkr.IBKRSession.start = _original_start

check("runner exits cleanly", code, 0)
truthy("signals were logged", log.exists())
rows = pd.read_csv(log)
check("one signal per crossing instrument", sorted(rows["ticker"]), ["AAPL", "MSFT"])
check("the UP instrument bought", rows.loc[rows.ticker == "AAPL", "side"].iloc[0], "BUY")
check("the DOWN instrument sold", rows.loc[rows.ticker == "MSFT", "side"].iloc[0], "SELL")
truthy("the BUY price is above its line",
       rows.loc[rows.ticker == "AAPL", "price"].iloc[0]
       > rows.loc[rows.ticker == "AAPL", "line_price"].iloc[0])
truthy("the SELL price is below its line",
       rows.loc[rows.ticker == "MSFT", "price"].iloc[0]
       < rows.loc[rows.ticker == "MSFT", "line_price"].iloc[0])
truthy("the instrument that never crossed stayed silent",
       "EURUSD" not in set(rows["ticker"]))
log.unlink(missing_ok=True)

truthy("no IB thread is left running",
       not any(t.name == "ibkr-session" for t in threading.enumerate()))


# --------------------------------------------------------------------------- #
print("\n" + "=" * 60)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  FAILED: {name}")
sys.exit(1 if FAIL else 0)