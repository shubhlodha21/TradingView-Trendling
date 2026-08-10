"""Price feeds.

Everything downstream consumes the same shape, so the source is swappable:

    bars(start, end, step)  -> DataFrame[timestamp, price, high, low]
    tick(at)                -> {"timestamp", "price", "high", "low"} | None

Three implementations ship here:

``SimulatedFeed``  a seeded mean-reverting walk around the trendline. Runs with
                   no credentials and actually produces crossings, so the whole
                   pipeline is demonstrable out of the box.
``CsvFeed``        replay your own tick/second export. This is the one to use
                   for honest backtests.
``YFinanceFeed``   real market data, but note the caveat on its docstring --
                   1-minute bars are the finest Yahoo serves, and they are
                   delayed. Fine for a sanity check, not for execution.

To wire your broker, implement :class:`PriceFeed` -- see ``BrokerFeed`` at the
bottom for the shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .clock import RthClock
from .sessions import to_utc

BAR_COLUMNS = ["timestamp", "price", "high", "low"]


class PriceFeed(ABC):
    name: str = "feed"

    @abstractmethod
    def bars(self, start, end, step_seconds: float = 1.0) -> pd.DataFrame:
        """Historical observations over [start, end] at ``step_seconds``."""

    @abstractmethod
    def tick(self, at) -> dict | None:
        """The single most recent observation at or before ``at``."""

    @staticmethod
    def _empty() -> pd.DataFrame:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}).astype(
            {"timestamp": "datetime64[ns, UTC]"}
        )


# --------------------------------------------------------------------------- #
# simulated
# --------------------------------------------------------------------------- #

class SimulatedFeed(PriceFeed):
    """Deterministic synthetic prices in RTH-second space.

    The path is an Ornstein-Uhlenbeck wobble added to the trendline, started
    deliberately on the wrong side of the line so that a cross is reachable: for
    an UP signal it opens below the line and works its way up.

    Indexed by *market-second offset from the start anchor*, never by wall
    clock, so it stays consistent across Streamlit reruns and never invents
    prices during a weekend.
    """

    name = "simulated"

    def __init__(
        self,
        trendline,
        volatility: float = 1.0,
        seed: int = 7,
        start_offset_atr: float = -1.2,
    ):
        self.trendline = trendline
        self.seed = int(seed)
        line = trendline

        scale = max(abs(line.end.price - line.start.price), abs(line.start.price) * 0.004)
        self.amplitude = scale * 0.45 * max(volatility, 0.05)
        # Pull-back strength and step noise, tuned so the walk wanders across
        # the line a handful of times over a typical span rather than once.
        self.theta = 0.0015
        self.sigma = self.amplitude * 0.055
        if line.direction == "DOWN":
            start_offset_atr = -start_offset_atr
        self._path: list[float] = [self.amplitude * start_offset_atr]
        self._rng = np.random.default_rng(self.seed)

    # -- path generation ----------------------------------------------------

    def _extend_to(self, k: int, chunk_limit: int = 500_000) -> None:
        need = k + 1 - len(self._path)
        if need <= 0:
            return
        need = min(need, chunk_limit)
        draws = self._rng.standard_normal(need) * self.sigma
        x = self._path[-1]
        out = []
        for d in draws:
            x = x * (1.0 - self.theta) + d
            out.append(x)
        self._path.extend(out)

    def _residual(self, k: int) -> float:
        k = max(int(k), 0)
        self._extend_to(k)
        return self._path[min(k, len(self._path) - 1)]

    def price_at_offset(self, rth_seconds: float) -> float:
        return self.trendline.price_at_offset(rth_seconds) + self._residual(rth_seconds)

    # -- PriceFeed ----------------------------------------------------------

    def bars(self, start, end, step_seconds: float = 1.0) -> pd.DataFrame:
        line = self.trendline
        stamps, offsets = line.clock.grid(start, end, step_seconds)
        if len(stamps) == 0:
            return self._empty()

        base_offset = line.clock.elapsed(line.start.time, to_utc(start))
        absolute = offsets + base_offset
        self._extend_to(int(absolute.max()) + 1)
        residuals = np.array([self._residual(o) for o in absolute])
        prices = np.clip(absolute, 0, line.span_seconds) * line.slope + line.start.price
        prices = prices + residuals

        # Intrabar range: the wick a resting order would have been filled on.
        wick = self.amplitude * 0.06
        return pd.DataFrame({
            "timestamp": stamps,
            "price": prices,
            "high": prices + wick,
            "low": prices - wick,
        })

    def tick(self, at) -> dict | None:
        ts = to_utc(at)
        line = self.trendline
        if not line.contains(ts) or not line.clock.is_open(ts):
            return None
        offset = line.clock.elapsed(line.start.time, ts)
        price = self.price_at_offset(offset)
        wick = self.amplitude * 0.06
        return {
            "timestamp": ts,
            "price": float(price),
            "high": float(price + wick),
            "low": float(price - wick),
        }


# --------------------------------------------------------------------------- #
# csv replay
# --------------------------------------------------------------------------- #

class CsvFeed(PriceFeed):
    """Replay an exported tick or second-bar file.

    Expects a timestamp column plus a price column; ``high``/``low`` are picked
    up when present so touch-mode triggers work. Timestamps are read as UTC --
    if your export is in exchange local time, convert before loading, because
    everything else in this system assumes UTC.
    """

    name = "csv"

    def __init__(
        self,
        source,
        timestamp_col: str = "timestamp",
        price_col: str = "price",
        high_col: str | None = None,
        low_col: str | None = None,
    ):
        frame = source if isinstance(source, pd.DataFrame) else pd.read_csv(source)
        if timestamp_col not in frame.columns:
            raise ValueError(f"CSV has no {timestamp_col!r} column (found: {list(frame.columns)})")
        if price_col not in frame.columns:
            raise ValueError(f"CSV has no {price_col!r} column (found: {list(frame.columns)})")

        out = pd.DataFrame({
            "timestamp": pd.to_datetime(frame[timestamp_col], utc=True),
            "price": pd.to_numeric(frame[price_col], errors="coerce"),
        })
        out["high"] = pd.to_numeric(frame[high_col], errors="coerce") if high_col in frame else out["price"]
        out["low"] = pd.to_numeric(frame[low_col], errors="coerce") if low_col in frame else out["price"]
        self.data = out.dropna(subset=["price"]).sort_values("timestamp").reset_index(drop=True)

    def bars(self, start, end, step_seconds: float = 1.0) -> pd.DataFrame:
        a, b = to_utc(start), to_utc(end)
        window = self.data[(self.data["timestamp"] >= a) & (self.data["timestamp"] <= b)]
        return window.reset_index(drop=True)

    def tick(self, at) -> dict | None:
        ts = to_utc(at)
        window = self.data[self.data["timestamp"] <= ts]
        if window.empty:
            return None
        row = window.iloc[-1]
        return {
            "timestamp": row["timestamp"],
            "price": float(row["price"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
        }


# --------------------------------------------------------------------------- #
# yfinance
# --------------------------------------------------------------------------- #

class YFinanceFeed(PriceFeed):
    """Real quotes from Yahoo Finance.

    Caveat worth being explicit about: Yahoo's finest interval is 1 minute, only
    for the last ~7 days, and quotes are delayed for most venues. Second-level
    bars here are a forward-fill of minute bars -- adequate for eyeballing the
    line against real prices, not a substitute for a tick feed when you are
    actually sizing trades off the crossing timestamp.
    """

    name = "yfinance"

    def __init__(self, ticker: str, interval: str = "1m"):
        self.ticker = ticker
        self.interval = interval
        self._cache: pd.DataFrame | None = None

    def _download(self, start, end) -> pd.DataFrame:
        import yfinance as yf

        raw = yf.download(
            self.ticker,
            start=to_utc(start).tz_convert("UTC").tz_localize(None),
            end=(to_utc(end) + pd.Timedelta(minutes=5)).tz_convert("UTC").tz_localize(None),
            interval=self.interval,
            auto_adjust=False,
            progress=False,
        )
        if raw is None or raw.empty:
            return self._empty()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        idx = pd.DatetimeIndex(raw.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        return pd.DataFrame({
            "timestamp": idx,
            "price": raw["Close"].to_numpy(dtype=float),
            "high": raw["High"].to_numpy(dtype=float),
            "low": raw["Low"].to_numpy(dtype=float),
        }).dropna(subset=["price"]).reset_index(drop=True)

    def bars(self, start, end, step_seconds: float = 1.0) -> pd.DataFrame:
        if self._cache is None or self._cache.empty:
            self._cache = self._download(start, end)
        return self._cache[
            (self._cache["timestamp"] >= to_utc(start))
            & (self._cache["timestamp"] <= to_utc(end))
        ].reset_index(drop=True)

    def tick(self, at) -> dict | None:
        self._cache = None                          # always re-pull for live use
        frame = self.bars(to_utc(at) - pd.Timedelta(days=1), to_utc(at))
        if frame.empty:
            return None
        row = frame.iloc[-1]
        return {
            "timestamp": to_utc(at),
            "price": float(row["price"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
        }


class BrokerFeed(PriceFeed):
    """Template for a live broker connection.

    Implement the two methods against your API (IB, OANDA, MT5, Polygon, ...)
    and pass an instance wherever ``SimulatedFeed`` is used today. The contract:
    UTC timestamps, ``price`` is the last traded/mid price, ``high``/``low`` are
    the extremes since the previous observation.
    """

    name = "broker"

    def __init__(self, ticker: str, **credentials):
        self.ticker = ticker
        self.credentials = credentials

    def bars(self, start, end, step_seconds: float = 1.0) -> pd.DataFrame:
        raise NotImplementedError("Connect your broker's historical endpoint here.")

    def tick(self, at) -> dict | None:
        raise NotImplementedError("Connect your broker's streaming quote here.")


def get_feed(kind: str, **kwargs) -> PriceFeed:
    feeds = {
        "simulated": SimulatedFeed,
        "csv": CsvFeed,
        "yfinance": YFinanceFeed,
        "broker": BrokerFeed,
    }
    if kind == "ibkr":
        # Imported here so the package stays usable without the IB client
        # library installed.
        from .ibkr import IBKRFeed

        return IBKRFeed(**kwargs)
    try:
        return feeds[kind](**kwargs)
    except KeyError:
        raise ValueError(
            f"Unknown feed {kind!r}; choose from {sorted([*feeds, 'ibkr'])}"
        ) from None
