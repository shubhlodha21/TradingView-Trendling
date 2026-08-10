#!/usr/bin/env python3
"""RTH trendline signals -- headless runner, one or many instruments.

No UI. Each instrument is defined by five inputs::

    ticker, time1 + price1 (past), time2 + price2 (future), direction

From those the engine builds a trendline through *market* seconds and prints,
every ``--interval``, the line's price at that instant next to the live price
fetched from Interactive Brokers. When the live price crosses the line in the
signalled direction, a signal is emitted.

Everything runs together: one IB connection streams every subscribed ticker on
its own thread while this loop walks the RTH clock and compares each instrument
against its own line.

Examples
--------
One instrument, straight from the command line::

    python3 live.py --ticker AAPL \
        --start "2026-08-07 14:30" --start-price 205.00 \
        --end   "2026-08-12 19:00" --end-price   212.00 \
        --direction UP

Several, one --line each::

    python3 live.py \
        --line "AAPL,2026-08-07 14:30,205.00,2026-08-12 19:00,212.00,UP" \
        --line "MSFT,2026-08-07 14:30,410.00,2026-08-12 19:00,395.00,DOWN" \
        --line "EURUSD,2026-08-07 08:00,1.0850,2026-08-12 17:00,1.0990,UP"

A basket from a file (see --example-config for the template)::

    python3 live.py --config lines.csv --format json | tee ticks.jsonl

No broker needed -- replay the whole window against a seeded synthetic feed::

    python3 live.py --config lines.csv --feed simulated --replay-speed 300
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from rth import (
    Anchor,
    CrossDetector,
    RthClock,
    SimulatedFeed,
    Trendline,
    YFinanceFeed,
    get_market,
    guess_market,
)
from rth.feeds import CsvFeed

UP, DOWN = "UP", "DOWN"

EXAMPLE_CONFIG = """\
ticker,time1,price1,time2,price2,direction
# time1/price1 = the past anchor, time2/price2 = the future one.
# Times are UTC unless they carry an offset. direction is UP or DOWN.
# Optional extra columns: market, session_mode, trigger, sec_type, exchange,
# currency, csv_file.
AAPL,2026-08-07 14:30,205.00,2026-08-12 19:00,212.00,UP
MSFT,2026-08-07 14:30,410.00,2026-08-12 19:00,395.00,DOWN
EURUSD,2026-08-07 08:00,1.0850,2026-08-12 17:00,1.0990,UP
BTC-USD,2026-08-07 00:00,58000,2026-08-12 00:00,62000,UP
"""

_stop = False


def _handle_signal(signum, frame):            # noqa: ARG001 - signal handler shape
    global _stop
    _stop = True


# --------------------------------------------------------------------------- #
# the five inputs, per instrument
# --------------------------------------------------------------------------- #

@dataclass
class LineSpec:
    """One instrument's trendline definition, before it meets a calendar."""

    ticker: str
    time1: pd.Timestamp
    price1: float
    time2: pd.Timestamp
    price2: float
    direction: str = UP
    market: str | None = None
    session_mode: str | None = None
    trigger: str | None = None
    sec_type: str = "auto"
    exchange: str | None = None
    currency: str | None = None
    primary_exchange: str | None = None
    expiry: str | None = None
    csv_file: str | None = None
    # Execution sizing, per instrument. Unset falls back to the --exec-* flags.
    qty: float | None = None
    stop: float | None = None
    offset_entry_pct: float | None = None
    exec_port: int | None = None

    def __post_init__(self):
        self.ticker = str(self.ticker).strip().upper()
        self.direction = str(self.direction).strip().upper() or UP
        if self.direction in ("LONG", "BUY", "ABOVE"):
            self.direction = UP
        if self.direction in ("SHORT", "SELL", "BELOW"):
            self.direction = DOWN
        if self.direction not in (UP, DOWN):
            raise ValueError(
                f"{self.ticker}: direction must be UP or DOWN, got {self.direction!r}"
            )
        self.time1, self.time2 = parse_time(self.time1), parse_time(self.time2)
        self.price1, self.price2 = float(self.price1), float(self.price2)
        for name, cast in (("qty", float), ("stop", float),
                           ("offset_entry_pct", float), ("exec_port", int)):
            value = getattr(self, name)
            if value is not None and not isinstance(value, (int, float)):
                setattr(self, name, cast(value))


# Config columns are named however the user thinks of them.
_ALIASES = {
    "ticker": {"ticker", "symbol", "instrument", "name"},
    "time1": {"time1", "t1", "start", "start_time", "past_time", "time_1", "from"},
    "price1": {"price1", "p1", "value1", "past_value", "start_price", "past_price",
               "price_1", "value_1"},
    "time2": {"time2", "t2", "end", "end_time", "future_time", "time_2", "to"},
    "price2": {"price2", "p2", "value2", "future_value", "end_price", "future_price",
               "price_2", "value_2"},
    "direction": {"direction", "dir", "side", "signal", "updown", "up_down"},
    "market": {"market", "market_id", "venue"},
    "session_mode": {"session_mode", "mode", "session"},
    "trigger": {"trigger", "trigger_mode"},
    "sec_type": {"sec_type", "sectype", "security_type", "type"},
    "exchange": {"exchange", "exch"},
    "currency": {"currency", "ccy"},
    "primary_exchange": {"primary_exchange", "primary"},
    "expiry": {"expiry", "expiration", "contract_month"},
    "csv_file": {"csv_file", "csv", "file"},
    "qty": {"qty", "quantity", "size", "shares"},
    "stop": {"stop", "stop_loss", "stop_pct", "sl"},
    "offset_entry_pct": {"offset_entry_pct", "entry_offset", "offset"},
    "exec_port": {"exec_port", "trade_port", "order_port"},
}
_CANONICAL = {alias: field for field, aliases in _ALIASES.items() for alias in aliases}


def parse_time(text) -> pd.Timestamp:
    """Parse a timestamp, treating a naive one as UTC."""
    if isinstance(text, pd.Timestamp):
        ts = text
    else:
        try:
            ts = pd.Timestamp(str(text).strip())
        except ValueError as exc:
            raise ValueError(f"cannot parse time {text!r}: {exc}") from None
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def spec_from_mapping(row: dict, index: int) -> LineSpec:
    """Build a spec from a config row, whatever the columns are called."""
    clean: dict = {}
    for key, value in row.items():
        if key is None or value is None:
            continue
        canonical = _CANONICAL.get(str(key).strip().lower().replace(" ", "_"))
        text = str(value).strip()
        if canonical and text != "" and text.lower() != "nan":
            clean[canonical] = text

    missing = [f for f in ("ticker", "time1", "price1", "time2", "price2")
               if f not in clean]
    if missing:
        raise ValueError(
            f"row {index}: missing {', '.join(missing)}. Columns seen: "
            f"{sorted(k for k in row if k)}"
        )
    return LineSpec(**clean)


def spec_from_flag(text: str, index: int) -> LineSpec:
    """Parse ``TICKER,TIME1,PRICE1,TIME2,PRICE2,DIRECTION`` from --line."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 5:
        raise ValueError(
            f"--line #{index} needs at least "
            "TICKER,TIME1,PRICE1,TIME2,PRICE2[,DIRECTION[,MARKET[,SESSION_MODE]]] "
            f"-- got {text!r}"
        )
    fields = ["ticker", "time1", "price1", "time2", "price2",
              "direction", "market", "session_mode"]
    return LineSpec(**dict(zip(fields, parts)))


def load_config(path: str) -> list[LineSpec]:
    """Read instrument definitions from a .csv or .json file."""
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload["lines"] if isinstance(payload, dict) else payload
        return [spec_from_mapping(row, i + 1) for i, row in enumerate(rows)]

    with open(path, newline="", encoding="utf-8-sig") as handle:
        # Comment lines keep the shipped template self-documenting.
        body = [ln for ln in handle if ln.strip() and not ln.lstrip().startswith("#")]
    return [spec_from_mapping(row, i + 1)
            for i, row in enumerate(csv.DictReader(body))]


def load_specs(args) -> list[LineSpec]:
    specs: list[LineSpec] = []
    if args.config:
        specs.extend(load_config(args.config))
    for i, text in enumerate(args.line or [], start=1):
        specs.append(spec_from_flag(text, i))
    if args.ticker:
        specs.append(LineSpec(
            ticker=args.ticker,
            time1=args.start, price1=args.start_price,
            time2=args.end, price2=args.end_price,
            direction=args.direction,
            market=args.market, session_mode=args.session_mode,
            sec_type=args.sec_type, exchange=args.exchange,
            currency=args.currency, primary_exchange=args.primary_exchange,
            expiry=args.expiry, csv_file=args.csv_file,
        ))

    if args.only:
        wanted = {t.strip().upper() for t in args.only}
        kept = [s for s in specs if s.ticker in wanted]
        unknown = wanted - {s.ticker for s in specs}
        if unknown:
            raise ValueError(
                f"--only names {', '.join(sorted(unknown))}, which is not in this "
                f"config. Available: {', '.join(s.ticker for s in specs)}"
            )
        specs = kept
    return specs


# --------------------------------------------------------------------------- #
# arguments
# --------------------------------------------------------------------------- #

def _time_arg(text: str) -> pd.Timestamp:
    try:
        return parse_time(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live.py",
        description="RTH trendline signals against a live IBKR feed, for one "
                    "instrument or a basket. No UI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples")[-1],
    )

    many = p.add_argument_group("many instruments")
    many.add_argument("--config", default=None, metavar="FILE",
                      help="CSV or JSON file, one row per instrument: ticker, "
                           "time1, price1, time2, price2, direction.")
    many.add_argument("--line", action="append", default=None, metavar="SPEC",
                      help="TICKER,TIME1,PRICE1,TIME2,PRICE2[,DIRECTION[,MARKET"
                           "[,SESSION_MODE]]]. Repeatable.")
    many.add_argument("--only", action="append", default=None, metavar="TICKER",
                      help="Run just these instruments out of the config. "
                           "Repeatable -- this is how one basket file drives "
                           "several processes (one per tmux pane).")
    many.add_argument("--example-config", action="store_true",
                      help="Print a template config file and exit.")
    many.add_argument("--list-instruments", action="store_true",
                      help="Print the tickers the config resolves to, one per "
                           "line, and exit. Meant for scripting.")

    one = p.add_argument_group("one instrument (equivalent to a single --line)")
    one.add_argument("--ticker", help="Instrument symbol, e.g. AAPL, EURUSD.")
    one.add_argument("--start", type=_time_arg,
                     help="Past anchor time. Naive values are read as UTC.")
    one.add_argument("--start-price", type=float, help="Past anchor price.")
    one.add_argument("--end", type=_time_arg, help="Future anchor time (UTC).")
    one.add_argument("--end-price", type=float, help="Future anchor price.")
    one.add_argument("--direction", default=UP, choices=[UP, DOWN],
                     help="UP signals when price crosses above the line, DOWN "
                          "below. Default: UP.")

    market = p.add_argument_group("session calendar (defaults for every instrument)")
    market.add_argument("--market", default=None,
                        help="Market id, e.g. EQ:XNYS, FX:SPOT, CFD:METALS. "
                             "Default: guessed per ticker.")
    market.add_argument("--session-mode", default=None,
                        help="Session definition within the market. Default: the "
                             "market's first mode (regular hours).")
    market.add_argument("--list-markets", action="store_true",
                        help="Print the market ids and their session modes, then exit.")

    sig = p.add_argument_group("signal")
    sig.add_argument("--trigger", default="last", choices=["last", "touch"],
                     help="'last' needs the traded price through the line; 'touch' "
                          "accepts the wick. Default: last.")
    sig.add_argument("--signal-mode", default="cross", choices=["cross", "above"],
                     help="'cross' fires on the transition through the line (default). "
                          "'above' fires on every tick the price is beyond it.")
    sig.add_argument("--repeat", action="store_true",
                     help="Keep signalling on re-crosses instead of stopping at the first.")
    sig.add_argument("--cooldown", type=float, default=0.0,
                     help="Minimum market seconds between repeat signals.")
    sig.add_argument("--exit-on-signal", action="store_true",
                     help="Stop as soon as any instrument signals.")
    sig.add_argument("--stop-when-signalled", action="store_true",
                     help="Drop an instrument once it has signalled, and exit when "
                          "every instrument is done.")

    feed = p.add_argument_group("price feed")
    feed.add_argument("--feed", default="ibkr", choices=["ibkr", "simulated", "yfinance", "csv"],
                      help="Default: ibkr.")
    feed.add_argument("--csv-file", default=None,
                      help="For --feed csv with one instrument; per instrument, use "
                           "a csv_file column in --config.")
    feed.add_argument("--volatility", type=float, default=1.0, help="For --feed simulated.")
    feed.add_argument("--seed", type=int, default=7,
                      help="For --feed simulated. Each instrument gets seed+n.")

    ib = p.add_argument_group("interactive brokers")
    ib.add_argument("--ib-host", default=os.environ.get("IB_HOST", "127.0.0.1"))
    ib.add_argument("--ib-port", type=int, default=int(os.environ.get("IB_PORT", 7497)),
                    help="7497 TWS paper, 7496 TWS live, 4002 Gateway paper, "
                         "4001 Gateway live. Default: 7497.")
    ib.add_argument("--ib-client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", 17)),
                    help="Must be unique per API connection. One connection covers "
                         "every ticker. Default: 17.")
    ib.add_argument("--sec-type", default="auto",
                    choices=["auto", "STK", "CASH", "CRYPTO", "FUT", "CFD", "IND"],
                    help="Default: guessed per ticker.")
    ib.add_argument("--exchange", default=None, help="Override IB routing, e.g. SMART, IDEALPRO.")
    ib.add_argument("--currency", default=None, help="Override the contract currency.")
    ib.add_argument("--primary-exchange", default=None,
                    help="Disambiguates SMART-routed US stocks, e.g. NASDAQ.")
    ib.add_argument("--expiry", default=None, help="YYYYMM or YYYYMMDD, for FUT contracts.")
    ib.add_argument("--delayed", action="store_true",
                    help="Use IB's delayed data (type 3) when you hold no live subscription.")

    ex = p.add_argument_group(
        "execution -- hand a signal to GT_SYSTEM_LONG / GT_SYSTEM_SHORT")
    ex.add_argument("--on-signal", default="print", choices=["print", "tmux", "process"],
                    help="What to do when a signal fires. 'print' only shows the "
                         "command -- nothing runs. 'tmux' opens a window per trade; "
                         "'process' spawns it detached with a log file. Choosing "
                         "tmux or process ARMS live order placement. The whole "
                         "execution layer is off unless this or one of --gt-root / "
                         "--long-dir / --short-dir is given.")
    ex.add_argument("--gt-root", default=os.environ.get("GT_ROOT"), metavar="DIR",
                    help="Folder holding GT_SYSTEM_LONG and GT_SYSTEM_SHORT. "
                         "Default: $GT_ROOT, else this repo's parent directory.")
    ex.add_argument("--long-dir", default=None, metavar="DIR",
                    help="Override the UP/BUY folder outright.")
    ex.add_argument("--short-dir", default=None, metavar="DIR",
                    help="Override the DOWN/SELL folder outright.")
    ex.add_argument("--exec-entry", default="market", choices=["market", "stop-limit"],
                    help="How the launched bot enters. 'market' (default) passes "
                         "--market so it fills immediately -- the crossing has "
                         "already happened by the time we launch. 'stop-limit' "
                         "rests a STP-LMT at the trigger with --offset-entry-pct, "
                         "trading certainty of fill for control of price.")
    ex.add_argument("--exec-template", default=None, metavar="CMD",
                    help="Full command template, overriding --exec-entry. "
                         "Placeholders: {ticker} {trigger} {client_id} {port} "
                         "{qty} {stop} {offset_entry_pct} {direction} {side}.")
    ex.add_argument("--exec-trigger", default="line", choices=["line", "last"],
                    help="Which price becomes --trigger. 'line' (default) is the "
                         "trendline level that was crossed; 'last' is the traded "
                         "price that confirmed the cross.")
    ex.add_argument("--exec-client-id", type=int, default=100, metavar="N",
                    help="First IB client id for launched bots; each launch takes "
                         "the next one. Kept clear of --ib-client-id. Default: 100.")
    ex.add_argument("--exec-client-id-file", default=None, metavar="FILE",
                    help="Where the client-id counter persists, so a restart never "
                         "reissues an id a running bot holds. Default: logs/.gt_client_id.")
    ex.add_argument("--exec-paper", action="store_true",
                    help="Launch the bot against the PAPER account: sends "
                         "GT_PAPER=true and --paper, and moves the bot's port to "
                         "7497 unless --exec-port says otherwise. Use this for "
                         "every shakedown run.")
    ex.add_argument("--exec-port", type=int, default=None, metavar="PORT",
                    help="IBKR port passed to the bot. Default: 7496 live, "
                         "7497 with --exec-paper.")
    ex.add_argument("--exec-qty", type=float, default=512, metavar="N",
                    help="Quantity passed to the bot. Per instrument, use a qty "
                         "column in --config. Default: 512.")
    ex.add_argument("--exec-stop", type=float, default=0.0025, metavar="PCT",
                    help="Stop-loss fraction passed to the bot. Default: 0.0025.")
    ex.add_argument("--exec-offset-entry-pct", type=float, default=0.001, metavar="PCT",
                    help="Entry offset fraction passed to the bot. Default: 0.001.")
    ex.add_argument("--exec-price-decimals", type=int, default=None, metavar="N",
                    help="Rounding for the trigger price. Default: 2, or 5 for FX.")
    ex.add_argument("--exec-session", default="gt", metavar="NAME",
                    help="tmux session for launched bots. Default: gt.")
    ex.add_argument("--exec-log", default=None, metavar="FILE.csv",
                    help="Record every launch (command, client id, status). "
                         "Default: logs/launches.csv.")
    ex.add_argument("--exec-repeat", action="store_true",
                    help="Allow more than one launch per ticker per run. Off by "
                         "default so a re-cross cannot double your position.")
    ex.add_argument("--yes", action="store_true",
                    help="Skip the confirmation prompt when arming live execution.")

    run = p.add_argument_group("run")
    run.add_argument("--interval", type=float, default=1.0,
                     help="Seconds between comparison cycles. Default: 1.")
    run.add_argument("--replay-speed", type=float, default=None,
                     help="Drive the engine clock at N times real time from the "
                          "earliest start anchor. Testing only.")
    run.add_argument("--max-ticks", type=int, default=None,
                     help="Stop after N cycles.")
    run.add_argument("--format", default="table", choices=["table", "json"],
                     help="'json' prints one object per line. Default: table.")
    run.add_argument("--quiet", action="store_true",
                     help="Print signals only -- suppress the per-tick rows.")
    run.add_argument("--decimals", type=int, default=None,
                     help="Price precision. Default: 5 for FX, 4 otherwise, per instrument.")
    run.add_argument("--record", default=None, metavar="DIR|FILE",
                     help="Collect the live feed to disk: every quote, the "
                          "trendline price beside it and the gap. A directory "
                          "gets one file per instrument per UTC day; a name "
                          "ending .csv or .jsonl gets one combined file. CSV "
                          "recordings replay through --feed csv.")
    run.add_argument("--record-format", default=None, choices=["csv", "jsonl"],
                     help="Override the format implied by --record.")
    run.add_argument("--no-rotate", action="store_true",
                     help="One file per instrument for the whole run instead of "
                          "one per UTC day.")
    run.add_argument("--log-signals", default=None, metavar="FILE.csv",
                     help="Append every signal to a CSV file.")
    run.add_argument("--export-line", default=None, metavar="FILE.csv",
                     help="Write every trendline's datapoints (ticker, timestamp, "
                          "rth_seconds, line_price) to CSV, then continue.")
    run.add_argument("--line-step", type=float, default=60.0,
                     help="Step in market seconds for --export-line. Default: 60.")
    run.add_argument("--dump-line", type=int, default=0, metavar="N",
                     help="Print the first N datapoints of each trendline and exit.")
    return p


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

class Printer:
    """Table or JSON-lines output. Everything flushes, so piping stays live."""

    def __init__(self, fmt: str, decimals: int, quiet: bool, ticker_width: int = 8):
        self.fmt = fmt
        self.dp = decimals
        self.quiet = quiet
        self.tw = max(ticker_width, 6)
        self.pw = max(decimals + 6, 10)
        self._header_done = False

    def _emit(self, payload: dict, text: str) -> None:
        print(json.dumps(payload, default=str) if self.fmt == "json" else text, flush=True)

    def info(self, text: str, **fields) -> None:
        self._emit({"type": "info", "message": text, **fields}, text)

    def status(self, text: str, ticker: str | None = None, **fields) -> None:
        prefix = f"[{ticker}] " if ticker else ""
        self._emit(
            {"type": "status", "message": text, "ticker": ticker, **fields},
            f"[status] {prefix}{text}",
        )

    def error(self, text: str, ticker: str | None = None) -> None:
        payload = {"type": "error", "message": text, "ticker": ticker}
        if self.fmt == "json":
            print(json.dumps(payload, default=str), flush=True)
        else:
            prefix = f"[{ticker}] " if ticker else ""
            print(f"[error] {prefix}{text}", file=sys.stderr, flush=True)

    def header(self) -> None:
        if self.fmt == "json" or self.quiet or self._header_done:
            return
        w, tw = self.pw, self.tw
        print(
            f"{'time (utc)':<21}{'ticker':<{tw}}{'rth_s':>10}  {'line':>{w}}  "
            f"{'price':>{w}}  {'gap':>{w}}  {'state':<6} {'age':>5}",
            flush=True,
        )
        print("-" * (21 + tw + 10 + 3 * (w + 2) + 13), flush=True)
        self._header_done = True

    def tick(self, row: dict) -> None:
        if self.quiet:
            return
        if self.fmt == "json":
            print(json.dumps({"type": "tick", **row}, default=str), flush=True)
            return
        w, tw, dp = self.pw, self.tw, row.get("decimals", self.dp)
        age = row.get("age_seconds")
        print(
            f"{row['time']:<21}{row['ticker']:<{tw}}{row['rth_seconds']:>10.0f}  "
            f"{row['line_price']:>{w}.{dp}f}  {row['price']:>{w}.{dp}f}  "
            f"{row['gap']:>+{w}.{dp}f}  {row['state']:<6} "
            f"{('-' if age is None else f'{age:.0f}s'):>5}",
            flush=True,
        )

    def point(self, row: dict) -> None:
        """One trendline datapoint: timing and the line's price there."""
        self._emit(
            {"type": "line_point", **row},
            f"{row['time']:<21}{row['ticker']:<{self.tw}}"
            f"{row['rth_seconds']:>10.0f}  {row['line_price']:>12.{self.dp}f}",
        )

    def signal(self, row: dict) -> None:
        if self.fmt == "json":
            print(json.dumps({"type": "signal", **row}, default=str), flush=True)
            return
        dp = row.get("decimals", self.dp)
        print(
            f"\n*** SIGNAL {row['side']} {row['ticker']} *** "
            f"{row['time']}  price {row['price']:.{dp}f} "
            f"vs line {row['line_price']:.{dp}f} "
            f"(gap {row['gap']:+.{dp}f}, {row['rth_seconds']:,.0f} market seconds in)\n",
            flush=True,
        )


class SignalLog:
    """Append-only CSV of signals across every instrument."""

    FIELDS = ["time", "exact_time", "ticker", "side", "direction", "price",
              "line_price", "gap", "rth_seconds", "trigger", "mode"]

    def __init__(self, path: str):
        self.path = path
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self._handle = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=self.FIELDS,
                                      extrasaction="ignore")
        if fresh:
            self._writer.writeheader()
            self._handle.flush()

    def write(self, row: dict) -> None:
        self._writer.writerow(row)
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


# --------------------------------------------------------------------------- #
# one instrument, ready to run
# --------------------------------------------------------------------------- #

@dataclass
class Track:
    """A trendline, its calendar, its feed and its detector -- one instrument."""

    spec: LineSpec
    market_id: str
    session_mode: str
    clock: RthClock
    line: Trendline
    detector: CrossDetector
    decimals: int
    feed: object = None
    done: bool = False
    signals: int = 0
    was_open: bool | None = None
    waiting_reported: bool = False
    pending_reported: bool = False
    _reported_error: str | None = field(default=None, repr=False)

    @property
    def ticker(self) -> str:
        return self.spec.ticker

    @property
    def direction(self) -> str:
        return self.spec.direction

    @property
    def trigger(self) -> str:
        return self.detector.mode


def build_tracks(specs: list[LineSpec], args, out: Printer) -> list[Track]:
    """Resolve every spec into a runnable track. A bad spec is reported and
    skipped -- one unusable line does not stop the rest of the basket."""
    clocks: dict[tuple[str, str], RthClock] = {}
    tracks: list[Track] = []

    for spec in specs:
        market_id = spec.market or args.market or guess_market(spec.ticker)
        try:
            market = get_market(market_id)
        except KeyError as exc:
            out.error(f"{exc}. Try --list-markets.", ticker=spec.ticker)
            continue

        mode = spec.session_mode or args.session_mode or market.default_mode
        if mode not in market.modes:
            out.error(f"{market_id} has no session mode {mode!r}. "
                      f"Available: {list(market.modes)}", ticker=spec.ticker)
            continue

        # exchange_calendars is slow to build, so instruments on the same
        # calendar share one clock.
        key = (market_id, mode)
        if key not in clocks:
            clocks[key] = RthClock(market.provider(mode))
        clock = clocks[key]

        try:
            line = Trendline(
                clock,
                Anchor(spec.time1, spec.price1),
                Anchor(spec.time2, spec.price2),
                spec.direction,
            )
        except ValueError as exc:
            out.error(str(exc), ticker=spec.ticker)
            continue

        decimals = args.decimals if args.decimals is not None else (
            5 if market.asset_class == "Currency" else 4
        )
        tracks.append(Track(
            spec=spec,
            market_id=market_id,
            session_mode=mode,
            clock=clock,
            line=line,
            detector=CrossDetector(
                line,
                mode=spec.trigger or args.trigger,
                repeat=args.repeat,
                cooldown_seconds=args.cooldown,
            ),
            decimals=decimals,
        ))
    return tracks


def attach_feeds(tracks: list[Track], args, out: Printer):
    """Give every track a price feed. Returns the object to shut down, if any.

    For IB that is a single session with one subscription per instrument -- IB
    counts connections, not symbols, so a basket needs exactly one socket.
    """
    if args.feed == "simulated":
        for offset, track in enumerate(tracks):
            track.feed = SimulatedFeed(
                track.line, volatility=args.volatility, seed=args.seed + offset
            )
        return None

    if args.feed == "yfinance":
        for track in tracks:
            track.feed = YFinanceFeed(track.ticker)
        return None

    if args.feed == "csv":
        for track in tracks:
            path = track.spec.csv_file or args.csv_file
            if not path:
                raise SystemExit(
                    f"--feed csv needs a file for {track.ticker}: pass --csv-file, "
                    "or add a csv_file column to the config."
                )
            # high/low are named through so a --record CSV replays with its
            # wicks intact; CsvFeed falls back to price if a file lacks them.
            track.feed = CsvFeed(path, high_col="high", low_col="low")
        return None

    from rth.ibkr import DELAYED, LIVE, IBKRSession

    def report(subscription, message):
        out.error(message, ticker=subscription.ticker if subscription else None)

    session = IBKRSession(
        host=args.ib_host,
        port=args.ib_port,
        client_id=args.ib_client_id,
        market_data_type=DELAYED if args.delayed else LIVE,
        on_error=report,
    )
    for track in tracks:
        track.feed = session.add(
            track.ticker,
            sec_type=track.spec.sec_type if track.spec.sec_type != "auto" else args.sec_type,
            exchange=track.spec.exchange or args.exchange,
            currency=track.spec.currency or args.currency,
            primary_exchange=track.spec.primary_exchange or args.primary_exchange,
            expiry=track.spec.expiry or args.expiry,
        )

    out.status(
        f"connecting to IB at {args.ib_host}:{args.ib_port} "
        f"(client {args.ib_client_id}) for {len(tracks)} instrument(s)",
        host=args.ib_host, port=args.ib_port, client_id=args.ib_client_id,
        instruments=len(tracks),
    )
    session.start()

    for track in tracks:
        if track.feed.subscribed:
            out.status(f"subscribed: {track.feed.qualified_description}"
                       + ("  [delayed data]" if args.delayed else ""),
                       ticker=track.ticker, contract=track.feed.qualified_description)
        else:
            out.error(track.feed.error or "subscription was rejected", ticker=track.ticker)
            track.done = True

    if all(track.done for track in tracks):
        session.stop()
        raise SystemExit("No instrument could be subscribed -- nothing to run.")
    return session


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #

def build_launcher(args, tracks: list[Track], out: Printer):
    """Wire up the execution launcher, or None if signals stay informational."""
    from rth.execution import (
        LIVE_PORT,
        PAPER_PORT,
        ClientIdAllocator,
        TradeLauncher,
        pick_template,
    )

    log_dir = Path(args.exec_log).parent if args.exec_log else Path("logs")
    root = args.gt_root or Path(__file__).resolve().parent.parent
    # Per-signal rounding overrides this; it is only the fallback.
    decimals = args.exec_price_decimals if args.exec_price_decimals is not None else 2

    launcher = TradeLauncher(
        gt_root=root,
        long_dir=args.long_dir,
        short_dir=args.short_dir,
        template=args.exec_template or pick_template(args.exec_entry, args.exec_paper),
        mode=args.on_signal,
        allocator=ClientIdAllocator(
            args.exec_client_id_file or (log_dir / ".gt_client_id"),
            base=args.exec_client_id,
        ),
        session=args.exec_session,
        defaults={
            # --exec-paper moves the bot's port with it, so the account type and
            # the socket can never disagree by accident.
            "port": args.exec_port if args.exec_port is not None else (
                PAPER_PORT if args.exec_paper else LIVE_PORT
            ),
            "qty": int(args.exec_qty) if float(args.exec_qty).is_integer() else args.exec_qty,
            "stop": args.exec_stop,
            "offset_entry_pct": args.exec_offset_entry_pct,
        },
        log_path=args.exec_log or (log_dir / "launches.csv"),
        once_per_ticker=not args.exec_repeat,
        price_decimals=decimals,
    )

    if args.exec_client_id <= args.ib_client_id < args.exec_client_id + 1000:
        out.status(
            f"--exec-client-id {args.exec_client_id} is in the same range as the "
            f"feed's --ib-client-id {args.ib_client_id}; a collision would drop "
            "one of the connections", reason="client_id_range",
        )

    out.info("execution: " + launcher.describe()[0].split()[1], **{
        "execution_mode": launcher.mode,
        "long_dir": str(launcher.long_dir),
        "short_dir": str(launcher.short_dir),
    })
    for line in launcher.describe():
        out.info(f"  {line}")

    problems = launcher.check_directories()
    for problem in problems:
        out.error(problem)
    if problems and launcher.mode != "print":
        raise SystemExit(
            "Refusing to arm execution with a missing folder -- fix --gt-root, "
            "--long-dir or --short-dir first."
        )
    return launcher


def confirm_arming(launcher, args, out: Printer) -> bool:
    """A live-money launcher gets one explicit confirmation before it runs."""
    if launcher.mode == "print":
        return True

    money = "LIVE MONEY (GT_PAPER=false)" if launcher.is_live_money else "paper"
    out.info("")
    out.info("!" * 72)
    out.info(f"!!  EXECUTION ARMED -- every signal will start a {money} bot")
    out.info(f"!!  via {launcher.mode}, from {len(getattr(args, 'only', None) or []) or 'all'} "
             f"instrument(s) being watched")
    out.info("!" * 72)

    if args.yes or not sys.stdin.isatty():
        # Unattended (systemd, tmux send-keys, a pipe) cannot answer a prompt;
        # --yes is the deliberate way to say so.
        if not args.yes:
            out.error("stdin is not a terminal and --yes was not given -- refusing "
                      "to arm execution unattended")
            return False
        return True

    try:
        answer = input("Type ARM to continue, anything else to abort: ").strip()
    except (EOFError, KeyboardInterrupt):
        out.error("no answer on stdin -- not armed. Pass --yes to arm without "
                  "a prompt (only do that from a script you trust).")
        return False
    if answer != "ARM":
        out.status("not armed -- aborting", reason="not_confirmed")
        return False
    return True


def list_markets_and_exit() -> int:
    from rth import list_markets

    for market in list_markets():
        print(f"{market.market_id:<18} {market.label}")
        for mode in market.modes:
            print(f"{'':<18}   mode: {mode}")
    return 0


def run(args) -> int:
    if args.list_markets:
        return list_markets_and_exit()
    if args.example_config:
        print(EXAMPLE_CONFIG, end="")
        return 0

    try:
        specs = load_specs(args)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    if not specs:
        print("[error] no instruments given. Use --config, --line, or the single "
              "--ticker/--start/--end flags. See --example-config.", file=sys.stderr)
        return 2

    if args.list_instruments:
        for spec in specs:
            print(spec.ticker)
        return 0

    width = max(len(s.ticker) for s in specs) + 2
    decimals = args.decimals if args.decimals is not None else 4
    out = Printer(args.format, decimals, args.quiet, ticker_width=width)

    tracks = build_tracks(specs, args, out)
    if not tracks:
        out.error("no usable instruments -- every line failed to build")
        return 2

    out.info(
        f"{len(tracks)} instrument(s): {', '.join(t.ticker for t in tracks)} "
        f"| feed={args.feed} trigger={args.trigger} signal-mode={args.signal_mode}",
        instruments=[t.ticker for t in tracks], feed=args.feed,
        trigger=args.trigger, signal_mode=args.signal_mode,
    )
    for track in tracks:
        summary = track.line.summary()
        out.info(
            f"  {track.ticker:<{width}} {track.direction:<4} "
            f"{track.line.start.time:%Y-%m-%d %H:%M}Z @ {track.line.start.price:.{track.decimals}f}"
            f"  ->  {track.line.end.time:%Y-%m-%d %H:%M}Z @ {track.line.end.price:.{track.decimals}f}"
            f"   [{summary['rth_seconds']:,.0f}s over {summary['sessions']} sessions, "
            f"{summary['closed_pct']:.1f}% excluded, {track.market_id}]",
            ticker=track.ticker, market=track.market_id, session_mode=track.session_mode,
            direction=track.direction, start_utc=track.line.start.time,
            end_utc=track.line.end.time, start_price=track.line.start.price,
            end_price=track.line.end.price, rth_seconds=summary["rth_seconds"],
            sessions=summary["sessions"], slope_per_hour=track.line.slope_per_hour,
        )
        if track.line.start_was_snapped or track.line.end_was_snapped:
            out.status("an anchor landed on a closed market and was moved to the "
                       "next open (worth zero market seconds, so the span is "
                       "unchanged)", ticker=track.ticker)
        if not track.line.direction_matches_slope:
            out.status(
                f"the line {'rises' if track.line.rises else 'falls'} but the signal "
                f"is {track.direction} -- valid as a "
                f"{'breakout' if track.direction == UP else 'breakdown'}, just check "
                "the prices are the right way round", ticker=track.ticker)

    # -- the trendlines' own datapoints -------------------------------------

    if args.dump_line:
        for track in tracks:
            series = track.line.series(step_seconds=args.line_step)
            for _, r in series.head(args.dump_line).iterrows():
                out.point({
                    "ticker": track.ticker,
                    "time": f"{r['timestamp']:%Y-%m-%d %H:%M:%S}Z",
                    "rth_seconds": float(r["rth_seconds"]),
                    "line_price": float(r["line_price"]),
                })
        return 0

    if args.export_line:
        frames = []
        for track in tracks:
            series = track.line.series(step_seconds=args.line_step)
            series.insert(0, "ticker", track.ticker)
            frames.append(series)
        combined = pd.concat(frames, ignore_index=True)
        combined.to_csv(args.export_line, index=False)
        out.status(f"wrote {len(combined):,} trendline datapoints for "
                   f"{len(frames)} instrument(s) to {args.export_line}",
                   path=args.export_line, points=int(len(combined)))

    # -- execution -----------------------------------------------------------

    # Opt-in only. A run that never mentions execution gets none of it: no
    # banner, no complaints about missing GT folders, and signals stay purely
    # informational.
    wants_execution = bool(
        args.on_signal != "print" or args.gt_root or args.long_dir or args.short_dir
    )
    launcher = build_launcher(args, tracks, out) if wants_execution else None
    if launcher is not None and not confirm_arming(launcher, args, out):
        return 4

    # -- feeds ---------------------------------------------------------------

    try:
        closable = attach_feeds(tracks, args, out)
    except SystemExit:
        raise
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        out.error(str(exc))
        return 3

    recorder = None
    if args.record:
        from rth.recording import TickRecorder

        try:
            recorder = TickRecorder(args.record, fmt=args.record_format,
                                    rotate_daily=not args.no_rotate)
        except (OSError, ValueError) as exc:
            out.error(f"cannot record to {args.record}: {exc}")
            return 2
        out.status(f"recording the live feed: {recorder.describe()}",
                   record=str(args.record), record_format=recorder.fmt)

    log = SignalLog(args.log_signals) if args.log_signals else None
    total_signals = 0

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    started_wall = pd.Timestamp.now(tz="UTC")
    replay_origin = min(t.line.start.time for t in tracks)
    cycles = 0

    out.header()

    try:
        finished = False
        while not _stop and not finished:
            if args.replay_speed:
                elapsed = (pd.Timestamp.now(tz="UTC") - started_wall).total_seconds()
                now = replay_origin + pd.Timedelta(seconds=elapsed * args.replay_speed)
            else:
                now = pd.Timestamp.now(tz="UTC").floor("s")

            for track in tracks:
                if track.done:
                    continue
                if evaluate(track, now, args, out, log, launcher, recorder):
                    total_signals += 1
                    if args.exit_on_signal:
                        finished = True
                        break
                    if args.stop_when_signalled:
                        track.done = True
                        out.status("done -- signalled", ticker=track.ticker)

            if finished:
                break
            if all(track.done for track in tracks):
                out.status("every instrument is finished", reason="all_done")
                break

            cycles += 1
            if args.max_ticks and cycles >= args.max_ticks:
                out.status(f"reached --max-ticks {args.max_ticks}", reason="max_ticks")
                break

            time.sleep(args.interval)

    except KeyboardInterrupt:
        pass
    finally:
        _finish(out, cycles, total_signals, closable, log, recorder)

    return 0


def _finish(out: Printer, cycles: int, signals: int, closable, log,
            recorder=None) -> None:
    if closable is not None:
        closable.stop()
    if log is not None:
        log.close()
    if recorder is not None:
        rows, files = recorder.rows_written, len(recorder.files)
        recorder.close()
        out.status(f"recorded {rows:,} observations across {files} file(s)",
                   rows_recorded=rows, files_recorded=files)
    out.status(f"stopped after {cycles:,} cycles, {signals} signal(s)",
               cycles=cycles, signals=signals)


def evaluate(track: Track, now: pd.Timestamp, args, out: Printer, log,
             launcher=None, recorder=None) -> bool:
    """Compare one instrument against its line. Returns True if it signalled."""
    line, clock = track.line, track.clock

    if now >= line.end.time:
        out.status("the trendline has reached its future anchor -- finished",
                   ticker=track.ticker, reason="line_expired")
        track.done = True
        return False

    # Instruments in a basket need not start together; a line is dormant, not
    # broken, before its past anchor.
    if now < line.start.time:
        if not track.pending_reported:
            out.status(
                f"the trendline starts at {line.start.time:%Y-%m-%d %H:%M:%S}Z "
                "-- not comparing yet",
                ticker=track.ticker, reason="not_started",
                starts_at=line.start.time,
            )
            track.pending_reported = True
        return False

    # Each instrument keeps its own calendar, so one can be trading while
    # another is shut. Report the transition once, not every cycle.
    open_now = clock.is_open(now)
    if open_now != track.was_open:
        if open_now:
            out.status(f"market open at {now:%Y-%m-%d %H:%M:%S}Z",
                       ticker=track.ticker, market_open=True)
        else:
            nxt = clock.snap_forward(now)
            out.status(
                f"market closed at {now:%Y-%m-%d %H:%M:%S}Z"
                + (f" -- next open {nxt:%Y-%m-%d %H:%M:%S}Z" if nxt > now else ""),
                ticker=track.ticker, market_open=False, next_open=nxt,
            )
        track.was_open = open_now
    if not open_now:
        return False

    observation = track.feed.tick(now)
    if observation is None:
        if not track.waiting_reported:
            out.status("waiting for the first quote from the feed",
                       ticker=track.ticker, reason="no_quote_yet")
            track.waiting_reported = True
        return False
    track.waiting_reported = False

    price = float(observation["price"])
    offset = clock.elapsed(line.start.time, now)
    line_price = line.price_at_offset(offset)
    gap = price - line_price
    beyond = gap > 0 if track.direction == UP else gap < 0
    state = "above" if gap > 0 else ("below" if gap < 0 else "level")

    row = {
        "ticker": track.ticker,
        "time": f"{now:%Y-%m-%d %H:%M:%S}Z",
        "epoch": float(now.timestamp()),
        "rth_seconds": float(offset),
        "line_price": float(line_price),
        "price": price,
        "gap": float(gap),
        "state": state,
        "beyond_line": bool(beyond),
        "direction": track.direction,
        "market": track.market_id,
        "age_seconds": observation.get("age_seconds"),
        "quote_time": observation.get("quote_time"),
        "bid": observation.get("bid"),
        "ask": observation.get("ask"),
        "last": observation.get("last"),
        "high": observation.get("high"),
        "low": observation.get("low"),
        "volume": observation.get("volume"),
        "source": observation.get("source", track.feed.name),
        "decimals": track.decimals,
    }
    out.tick(row)
    if recorder is not None:
        # Recorded before the detector runs, so the file holds every quote the
        # decision was made from -- not just the ones that signalled. --quiet
        # suppresses the printed row; it must not suppress the collection.
        try:
            recorder.write(now, row)
        except OSError as exc:
            out.error(f"recording failed: {exc}", ticker=track.ticker)

    event = track.detector.update(
        now, price, high=observation.get("high"), low=observation.get("low")
    )
    fire = event is not None
    if args.signal_mode == "above" and beyond:
        # Level mode: the plain "live price is past the line" reading,
        # re-asserted every cycle rather than only on the transition.
        fire = True
    if not fire:
        return False

    track.signals += 1
    payload = {
        **row,
        "side": "BUY" if track.direction == UP else "SELL",
        "trigger": track.trigger,
        "mode": args.signal_mode,
        "exact_time": (f"{event.exact_time:%Y-%m-%d %H:%M:%S}Z" if event else row["time"]),
        "signal_number": track.signals,
    }
    out.signal(payload)
    if log is not None:
        log.write(payload)
    if launcher is not None:
        hand_off(launcher, track, payload, args, out)
    return True


def hand_off(launcher, track: Track, payload: dict, args, out: Printer) -> None:
    """Give a fired signal to the execution system."""
    spec = track.spec
    request = {
        "ticker": track.ticker,
        "direction": track.direction,
        "side": payload["side"],
        # The crossing price: by default the trendline level that was crossed,
        # which is the level the order is meant to act on. --exec-trigger last
        # uses the traded price that confirmed it instead.
        "trigger": (payload["line_price"] if args.exec_trigger == "line"
                    else payload["price"]),
        "line_price": payload["line_price"],
        "last_price": payload["price"],
        # Per-instrument sizing from the config wins over the --exec-* defaults.
        "qty": int(spec.qty) if spec.qty is not None and float(spec.qty).is_integer()
               else spec.qty,
        "stop": spec.stop,
        "offset_entry_pct": spec.offset_entry_pct,
        "port": spec.exec_port,
        "price_decimals": (args.exec_price_decimals if args.exec_price_decimals
                           is not None else (5 if track.decimals >= 5 else 2)),
    }
    launch = launcher.fire(request)

    verb = {"launched": "launched", "built": "would run", "skipped": "skipped",
            "failed": "FAILED to launch"}[launch.status]
    out.status(
        f"{verb}: (cd {launch.cwd} && {launch.command})"
        + (f"   [{launch.detail}]" if launch.detail else ""),
        ticker=track.ticker, execution_status=launch.status,
        command=launch.command, cwd=launch.cwd, client_id=launch.client_id,
        trigger=launch.trigger, window=launch.window, detail=launch.detail,
    )


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.ticker and not args.list_markets and not args.example_config:
        missing = [
            flag for flag, value in (
                ("--start", args.start), ("--start-price", args.start_price),
                ("--end", args.end), ("--end-price", args.end_price),
            ) if value is None
        ]
        if missing:
            parser.error(
                f"--ticker also needs: {', '.join(missing)} "
                "(or define the instrument with --line / --config instead)"
            )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())