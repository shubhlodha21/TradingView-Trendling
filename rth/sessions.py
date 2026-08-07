"""Session calendars: what counts as "market open" for a given instrument.

Everything in this module speaks UTC on the outside. Session templates are
declared in *local exchange time* so that DST is handled correctly -- a US
equity session is 09:30-16:00 New York, which is 14:30-21:00 UTC in winter and
13:30-20:00 UTC in summer.

Two provider families:

* ``ExchangeCalendarProvider`` -- backed by the ``exchange_calendars`` package.
  Real holiday calendars for 100+ venues, including half-days (e.g. the 13:00 ET
  close on the Friday after Thanksgiving) and lunch breaks (XTKS, XHKG, XSHG).
  This is the authority for anything listed on an exchange.

* ``WeeklyTemplateProvider`` -- for OTC / round-the-clock products (FX, metals,
  energy, index CFDs, crypto) that have no exchange calendar. Declared as a
  weekday -> intervals template plus a holiday set.

Both expose the same contract::

    provider.sessions(start_utc, end_utc) -> [(open_utc, close_utc), ...]

sorted, non-overlapping, half-open ``[open, close)``, and clipped generously
around the requested window (never *inside* it).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Protocol, Sequence

import pandas as pd
import exchange_calendars as xcals

Interval = tuple[pd.Timestamp, pd.Timestamp]

# Minutes-from-local-midnight. 1440 == end of day, and merges cleanly into the
# next day's 0 so that overnight products come out as one continuous session.
DAY_END = 1440


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def to_utc(ts) -> pd.Timestamp:
    """Coerce anything timestamp-like to a tz-aware UTC Timestamp."""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def merge_intervals(intervals: Sequence[Interval]) -> list[Interval]:
    """Sort and coalesce touching/overlapping intervals."""
    clean = [(a, b) for a, b in intervals if pd.notna(a) and pd.notna(b) and b > a]
    if not clean:
        return []
    clean.sort(key=lambda iv: iv[0])
    out: list[Interval] = [clean[0]]
    for start, end in clean[1:]:
        last_start, last_end = out[-1]
        if start <= last_end:                      # touching counts as contiguous
            if end > last_end:
                out[-1] = (last_start, end)
        else:
            out.append((start, end))
    return out


def subtract_intervals(base: Sequence[Interval], cuts: Sequence[Interval]) -> list[Interval]:
    """Remove ``cuts`` from ``base``. Both are assumed already merged."""
    out = list(base)
    for cut_start, cut_end in cuts:
        nxt: list[Interval] = []
        for start, end in out:
            if cut_end <= start or cut_start >= end:
                nxt.append((start, end))
                continue
            if start < cut_start:
                nxt.append((start, cut_start))
            if cut_end < end:
                nxt.append((cut_end, end))
        out = nxt
    return out


def easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm -- used only to derive Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def good_friday(year: int) -> dt.date:
    return easter(year) - dt.timedelta(days=2)


def otc_holidays(years: range, include_good_friday: bool = True) -> set[dt.date]:
    """Baseline closures for OTC products.

    FX and CFD venues do not publish a machine-readable calendar and brokers
    differ at the margins. New Year's Day and Christmas Day are universal;
    Good Friday closes metals, energy and index CFDs. Override per-market if
    your broker's schedule differs.
    """
    days: set[dt.date] = set()
    for y in years:
        days.add(dt.date(y, 1, 1))
        days.add(dt.date(y, 12, 25))
        if include_good_friday:
            days.add(good_friday(y))
    return days


class SessionProvider(Protocol):
    key: str
    tz: str

    def sessions(self, start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> list[Interval]:
        ...


# --------------------------------------------------------------------------- #
# exchange-backed provider
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=64)
def _calendar(code: str):
    """Build (and cache) an exchange calendar with a wide date range.

    Some venues have hard bounds well inside our default window, so fall back to
    the package default range if the explicit one is rejected.
    """
    try:
        return xcals.get_calendar(code, start="1998-01-01", end="2035-12-31")
    except Exception:
        return xcals.get_calendar(code)


@dataclass(frozen=True)
class ExchangeCalendarProvider:
    """Sessions straight from ``exchange_calendars``.

    ``include_breaks`` splits the day around an intraday break (Tokyo's lunch,
    for instance) so break minutes are not counted as market time.

    ``pre_minutes`` / ``post_minutes`` widen each session for venues with an
    extended-hours book. They are applied in *exchange local time* against the
    regular open/close, so DST stays correct, and they suppress breaks since
    extended books trade through.

    ``daily_break`` carves a recurring local-time window out of every session.
    CME's calendar models a trading day as one continuous 23:00-to-23:00 block,
    so consecutive days touch and merge into an unbroken week -- which silently
    credits the 16:00-17:00 CT maintenance halt as tradable. Passing
    ``daily_break=(16*60, 17*60)`` removes it.
    """

    code: str
    include_breaks: bool = True
    pre_minutes: int = 0
    post_minutes: int = 0
    daily_break: tuple[int, int] | None = None

    @property
    def key(self) -> str:
        suffix = ""
        if self.pre_minutes or self.post_minutes:
            suffix = f"+{self.pre_minutes}/{self.post_minutes}"
        if self.daily_break:
            suffix += f"-brk{self.daily_break[0]}"
        return f"xcal:{self.code}{suffix}"

    @property
    def tz(self) -> str:
        return str(_calendar(self.code).tz)

    def sessions(self, start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> list[Interval]:
        cal = _calendar(self.code)
        sched = cal.schedule
        lo = pd.Timestamp(to_utc(start_utc).date()) - pd.Timedelta(days=5)
        hi = pd.Timestamp(to_utc(end_utc).date()) + pd.Timedelta(days=5)
        window = sched.loc[lo:hi]
        if window.empty:
            return []

        opens = window["open"].dt.tz_convert("UTC")
        closes = window["close"].dt.tz_convert("UTC")

        extended = self.pre_minutes or self.post_minutes
        if extended:
            opens = opens - pd.Timedelta(minutes=self.pre_minutes)
            closes = closes + pd.Timedelta(minutes=self.post_minutes)

        out: list[Interval] = []
        has_breaks = (
            self.include_breaks
            and not extended
            and "break_start" in window.columns
            and window["break_start"].notna().any()
        )
        if has_breaks:
            b_start = window["break_start"].dt.tz_convert("UTC")
            b_end = window["break_end"].dt.tz_convert("UTC")
            for o, c, bs, be in zip(opens, closes, b_start, b_end):
                if pd.notna(bs) and pd.notna(be) and bs > o and be < c:
                    out.append((o, bs))
                    out.append((be, c))
                else:
                    out.append((o, c))
        else:
            out.extend(zip(opens, closes))

        merged = merge_intervals(out)
        if self.daily_break:
            merged = subtract_intervals(merged, self._break_windows(lo, hi))
        return merged

    def _break_windows(self, lo: pd.Timestamp, hi: pd.Timestamp) -> list[Interval]:
        """The recurring maintenance halt, one per local calendar day."""
        start_min, end_min = self.daily_break
        tz = self.tz
        cuts: list[Interval] = []
        day = (pd.Timestamp(lo).tz_localize("UTC") if lo.tzinfo is None else lo).tz_convert(tz).date()
        last = (pd.Timestamp(hi).tz_localize("UTC") if hi.tzinfo is None else hi).tz_convert(tz).date()
        while day <= last:
            base = pd.Timestamp(day)
            cuts.append((
                (base + pd.Timedelta(minutes=start_min)).tz_localize(
                    tz, nonexistent="shift_forward", ambiguous=True).tz_convert("UTC"),
                (base + pd.Timedelta(minutes=end_min)).tz_localize(
                    tz, nonexistent="shift_forward", ambiguous=True).tz_convert("UTC"),
            ))
            day += dt.timedelta(days=1)
        return cuts


# --------------------------------------------------------------------------- #
# weekly-template provider (OTC / 24h products)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class WeeklyTemplateProvider:
    """Sessions from a weekday -> [(start_min, end_min)] template in local time.

    Minutes are measured from local midnight; ``DAY_END`` (1440) means "to the
    end of the day" and merges with the next day's 0, which is how a Sunday
    17:00 open flows unbroken into Monday for FX.

    ``holidays`` is a callable so the set can be derived per-year lazily.
    """

    key: str
    tz: str
    template: dict[int, tuple[tuple[int, int], ...]]
    holiday_fn: Callable[[range], set[dt.date]] | None = None
    label: str = ""

    def _holidays(self, years: range) -> set[dt.date]:
        return self.holiday_fn(years) if self.holiday_fn else set()

    def sessions(self, start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> list[Interval]:
        start_utc, end_utc = to_utc(start_utc), to_utc(end_utc)
        # Pad by a couple of days each side: a local day can straddle the UTC
        # boundary in either direction depending on the offset.
        local_start = (start_utc.tz_convert(self.tz) - pd.Timedelta(days=3)).date()
        local_end = (end_utc.tz_convert(self.tz) + pd.Timedelta(days=3)).date()
        holidays = self._holidays(range(local_start.year, local_end.year + 1))

        out: list[Interval] = []
        day = local_start
        one_day = dt.timedelta(days=1)
        while day <= local_end:
            if day in holidays:
                day += one_day
                continue
            for start_min, end_min in self.template.get(day.weekday(), ()):
                o = self._localize(day, start_min)
                c = self._localize(day, end_min)
                if c > o:
                    out.append((o, c))
            day += one_day
        return merge_intervals(out)

    def _localize(self, day: dt.date, minutes: int) -> pd.Timestamp:
        naive = pd.Timestamp(day) + pd.Timedelta(minutes=minutes)
        local = naive.tz_localize(
            self.tz, nonexistent="shift_forward", ambiguous=True
        )
        return local.tz_convert("UTC")


def _template(spans: dict[int, tuple[tuple[int, int], ...]]) -> dict[int, tuple[tuple[int, int], ...]]:
    return spans


def _hm(h: int, m: int = 0) -> int:
    return h * 60 + m


MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

# FX interbank week: opens Sunday 17:00 New York, runs unbroken to Friday 17:00.
_FX_24X5 = {
    SUN: ((_hm(17), DAY_END),),
    MON: ((0, DAY_END),),
    TUE: ((0, DAY_END),),
    WED: ((0, DAY_END),),
    THU: ((0, DAY_END),),
    FRI: ((0, _hm(17)),),
}

# Spot-metals style: 18:00 New York open, 17:00-18:00 daily halt. (CME-dated
# products use the real CMES calendar instead of a template.)
_NY_COMMODITY = {
    SUN: ((_hm(18), DAY_END),),
    MON: ((0, _hm(17)), (_hm(18), DAY_END)),
    TUE: ((0, _hm(17)), (_hm(18), DAY_END)),
    WED: ((0, _hm(17)), (_hm(18), DAY_END)),
    THU: ((0, _hm(17)), (_hm(18), DAY_END)),
    FRI: ((0, _hm(17)),),
}

_ALWAYS_OPEN = {d: ((0, DAY_END),) for d in range(7)}

# CME/CBOT/COMEX/NYMEX, with the 16:00-17:00 CT maintenance halt carved back out.
_CMES = ExchangeCalendarProvider("CMES", daily_break=(_hm(16), _hm(17)))


def _weekday_window(start: int, end: int) -> dict[int, tuple[tuple[int, int], ...]]:
    return {d: ((start, end),) for d in (MON, TUE, WED, THU, FRI)}


# --------------------------------------------------------------------------- #
# market registry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MarketDef:
    """An instrument's tradable-hours definition, with selectable session modes.

    ``modes`` maps a UI-facing name ("RTH", "Extended", "London") to the provider
    that realises it. The first key is the default.
    """

    market_id: str
    asset_class: str
    label: str
    modes: dict[str, SessionProvider]

    @property
    def default_mode(self) -> str:
        return next(iter(self.modes))

    def provider(self, mode: str | None = None) -> SessionProvider:
        if mode and mode in self.modes:
            return self.modes[mode]
        return self.modes[self.default_mode]


ASSET_CLASSES = ["Equity", "CFD", "Currency", "Crypto"]

# Venues worth surfacing at the top of a long dropdown, with readable names.
_EQUITY_LABELS = {
    "XNYS": "New York Stock Exchange / Nasdaq (US)",
    "XLON": "London Stock Exchange (UK)",
    "XETR": "Xetra / Deutsche Boerse (DE)",
    "XPAR": "Euronext Paris (FR)",
    "XAMS": "Euronext Amsterdam (NL)",
    "XSWX": "SIX Swiss Exchange (CH)",
    "XMIL": "Borsa Italiana (IT)",
    "XMAD": "Bolsa de Madrid (ES)",
    "XSTO": "Nasdaq Stockholm (SE)",
    "XTKS": "Tokyo Stock Exchange (JP)",
    "XHKG": "Hong Kong Stock Exchange (HK)",
    "XSHG": "Shanghai Stock Exchange (CN)",
    "XSES": "Singapore Exchange (SG)",
    "XKRX": "Korea Exchange (KR)",
    "XBOM": "BSE / NSE India (IN)",
    "XASX": "Australian Securities Exchange (AU)",
    "XTSE": "Toronto Stock Exchange (CA)",
    "BVMF": "B3 Brazil (BR)",
    "XJSE": "Johannesburg Stock Exchange (ZA)",
    "XTAE": "Tel Aviv Stock Exchange (IL)",
    "XIST": "Borsa Istanbul (TR)",
    "XWAR": "Warsaw Stock Exchange (PL)",
    "XNZE": "NZX (NZ)",
}

# Derivative/synthetic calendars that ship with exchange_calendars. They back the
# CFD definitions below rather than appearing in the equity venue list.
_NON_EQUITY_CALENDARS = {"CMES", "XEUR", "IEPA", "us_futures", "24/5", "24/7"}

# US extended book: 04:00 pre-market to 20:00 post-market local.
_US_PRE_MIN = 5 * 60 + 30      # 09:30 regular open minus 04:00
_US_POST_MIN = 4 * 60          # 20:00 minus 16:00 regular close


def _equity_market(code: str) -> MarketDef:
    label = _EQUITY_LABELS.get(code, code)
    modes: dict[str, SessionProvider] = {
        "RTH (regular session)": ExchangeCalendarProvider(code),
    }
    if code == "XNYS":
        modes["Extended (04:00-20:00 ET)"] = ExchangeCalendarProvider(
            code, pre_minutes=_US_PRE_MIN, post_minutes=_US_POST_MIN
        )
    return MarketDef(f"EQ:{code}", "Equity", label, modes)


def _fx_modes() -> dict[str, SessionProvider]:
    """FX has no exchange, so "RTH" is a choice. 24x5 is the honest default;
    the regional windows are there if you only want to trade liquid hours."""
    hol = lambda years: otc_holidays(years, include_good_friday=False)
    return {
        "24x5 (Sun 17:00 - Fri 17:00 ET)": WeeklyTemplateProvider(
            "fx:24x5", "America/New_York", _FX_24X5, hol
        ),
        "London session (07:00-16:00 UK)": WeeklyTemplateProvider(
            "fx:london", "Europe/London", _weekday_window(_hm(7), _hm(16)), hol
        ),
        "New York session (08:00-17:00 ET)": WeeklyTemplateProvider(
            "fx:ny", "America/New_York", _weekday_window(_hm(8), _hm(17)), hol
        ),
        "Tokyo session (09:00-18:00 JST)": WeeklyTemplateProvider(
            "fx:tokyo", "Asia/Tokyo", _weekday_window(_hm(9), _hm(18)), hol
        ),
        "London/NY overlap (13:00-16:00 UTC)": WeeklyTemplateProvider(
            "fx:overlap", "UTC", _weekday_window(_hm(13), _hm(16)), hol
        ),
    }


def _cfd_markets() -> list[MarketDef]:
    """CFD hours.

    Where a genuine derivatives calendar exists, use it -- ``CMES`` (CME/CBOT/
    COMEX/NYMEX), ``XEUR`` (Eurex) and ``IEPA`` (ICE) are maintained upstream and
    get the awkward cases right, such as CME's 12:00 CT close on Christmas Eve.
    Hand-rolled templates are the fallback only where no calendar exists, and
    those are approximations -- brokers vary, so check yours.
    """
    hol = otc_holidays
    return [
        MarketDef(
            "CFD:US_INDEX", "CFD", "US index CFD (SPX500, US30, NAS100)",
            {
                "Full (CME futures hours)": _CMES,
                "RTH (09:30-16:00 ET cash session)": ExchangeCalendarProvider("XNYS"),
            },
        ),
        MarketDef(
            "CFD:EU_INDEX", "CFD", "European index CFD (GER40, EU50)",
            {
                "Full (Eurex futures hours)": ExchangeCalendarProvider("XEUR"),
                "RTH (09:00-17:30 CET cash session)": WeeklyTemplateProvider(
                    "cfd:eu_index_rth", "Europe/Berlin",
                    _weekday_window(_hm(9), _hm(17, 30)), hol
                ),
            },
        ),
        MarketDef(
            "CFD:UK_INDEX", "CFD", "UK index CFD (UK100)",
            {
                "Full (01:00-21:00 UK, approximate)": WeeklyTemplateProvider(
                    "cfd:uk_index_full", "Europe/London",
                    _weekday_window(_hm(1), _hm(21)), hol
                ),
                "RTH (08:00-16:30 UK cash session)": ExchangeCalendarProvider("XLON"),
            },
        ),
        MarketDef(
            "CFD:ASIA_INDEX", "CFD", "Asian index CFD (JP225, HK50, AUS200)",
            {
                "RTH (Tokyo cash session)": ExchangeCalendarProvider("XTKS"),
                "RTH (Hong Kong cash session)": ExchangeCalendarProvider("XHKG"),
                "RTH (Sydney cash session)": ExchangeCalendarProvider("XASX"),
                "Full (CME futures hours)": _CMES,
            },
        ),
        MarketDef(
            "CFD:METALS", "CFD", "Metals CFD (XAUUSD, XAGUSD)",
            {
                "Full (COMEX futures hours)": _CMES,
                "RTH (08:20-13:30 ET COMEX floor)": WeeklyTemplateProvider(
                    "cfd:metals_rth", "America/New_York",
                    _weekday_window(_hm(8, 20), _hm(13, 30)), hol
                ),
                "Spot (Sun 18:00 - Fri 17:00 ET)": WeeklyTemplateProvider(
                    "cfd:metals", "America/New_York", _NY_COMMODITY, hol
                ),
            },
        ),
        MarketDef(
            "CFD:ENERGY", "CFD", "Energy CFD (WTI, Brent, NatGas)",
            {
                "Full (NYMEX futures hours)": _CMES,
                "Full (ICE futures hours, for Brent)": ExchangeCalendarProvider("IEPA"),
                "RTH (09:00-14:30 ET NYMEX floor)": WeeklyTemplateProvider(
                    "cfd:energy_rth", "America/New_York",
                    _weekday_window(_hm(9), _hm(14, 30)), hol
                ),
            },
        ),
        MarketDef(
            "CFD:FX", "CFD", "FX CFD (follows interbank hours)", _fx_modes()
        ),
        MarketDef(
            "CFD:CRYPTO", "CFD", "Crypto CFD (24/7)",
            {"24/7": WeeklyTemplateProvider("crypto:247", "UTC", _ALWAYS_OPEN)},
        ),
    ]


@lru_cache(maxsize=1)
def _registry() -> dict[str, MarketDef]:
    markets: list[MarketDef] = []

    for code in sorted(xcals.get_calendar_names(include_aliases=False)):
        if code in _NON_EQUITY_CALENDARS:
            continue
        markets.append(_equity_market(code))

    # Single-stock CFDs track the underlying venue, so mirror the equity list.
    for code in ("XNYS", "XNAS", "XLON", "XETR", "XTKS", "XHKG", "XASX"):
        eq = _equity_market(code)
        markets.append(
            MarketDef(f"CFD:STOCK:{code}", "CFD",
                      f"Single-stock CFD - {eq.label}", eq.modes)
        )

    markets.extend(_cfd_markets())
    markets.append(MarketDef("FX:SPOT", "Currency", "Spot FX (all pairs)", _fx_modes()))
    markets.append(
        MarketDef("CRYPTO:SPOT", "Crypto", "Crypto spot (24/7)",
                  {"24/7": WeeklyTemplateProvider("crypto:247", "UTC", _ALWAYS_OPEN)})
    )
    return {m.market_id: m for m in markets}


def list_markets(asset_class: str | None = None) -> list[MarketDef]:
    markets = list(_registry().values())
    if asset_class:
        markets = [m for m in markets if m.asset_class == asset_class]
    # Keep the well-known venues at the top; the long tail stays alphabetical.
    priority = list(_EQUITY_LABELS)

    def sort_key(m: MarketDef):
        code = m.market_id.rsplit(":", 1)[-1]
        rank = priority.index(code) if code in priority else len(priority)
        return (rank, m.label)

    return sorted(markets, key=sort_key)


def get_market(market_id: str) -> MarketDef:
    registry = _registry()
    if market_id in registry:
        return registry[market_id]

    # Many venue codes are aliases of a shared calendar -- XNAS, NASDAQ and NYSE
    # all resolve to XNYS, which is correct: they keep identical hours. Only
    # canonical names are registered, so resolve before giving up.
    if market_id.startswith("EQ:"):
        try:
            canonical = xcals.calendar_utils.global_calendar_dispatcher.resolve_alias(
                market_id.removeprefix("EQ:")
            )
        except Exception:
            canonical = None
        if canonical and f"EQ:{canonical}" in registry:
            return registry[f"EQ:{canonical}"]

    raise KeyError(f"Unknown market id: {market_id!r}")


_SUFFIX_HINTS = {
    ".L": "EQ:XLON", ".DE": "EQ:XETR", ".PA": "EQ:XPAR", ".AS": "EQ:XAMS",
    ".SW": "EQ:XSWX", ".MI": "EQ:XMIL", ".MC": "EQ:XMAD", ".ST": "EQ:XSTO",
    ".T": "EQ:XTKS", ".HK": "EQ:XHKG", ".SS": "EQ:XSHG", ".SI": "EQ:XSES",
    ".KS": "EQ:XKRX", ".NS": "EQ:XBOM", ".BO": "EQ:XBOM", ".AX": "EQ:XASX",
    ".TO": "EQ:XTSE", ".SA": "EQ:BVMF", ".JO": "EQ:XJSE", ".TA": "EQ:XTAE",
    ".NZ": "EQ:XNZE", ".WA": "EQ:XWAR",
}

_FX_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "DKK", "SGD", "HKD", "CNH", "MXN", "ZAR", "TRY", "PLN",
}


def guess_market(ticker: str) -> str:
    """Best-effort ticker -> market_id. A convenience, not an authority --
    the UI always lets the user override it."""
    t = (ticker or "").strip().upper()
    if not t:
        return "EQ:XNYS"

    if t.startswith(("XAU", "XAG", "XPT", "XPD")):
        return "CFD:METALS"
    if t.split("-")[0].split("/")[0] in {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA"}:
        return "CRYPTO:SPOT"
    if any(k in t for k in ("WTI", "BRENT", "USOIL", "UKOIL", "NATGAS")):
        return "CFD:ENERGY"
    if any(k in t for k in ("SPX", "US500", "US30", "NAS100", "US100", "DJI")):
        return "CFD:US_INDEX"
    if any(k in t for k in ("GER40", "DAX", "EU50", "STOXX")):
        return "CFD:EU_INDEX"
    if any(k in t for k in ("UK100", "FTSE")):
        return "CFD:UK_INDEX"
    if any(k in t for k in ("JP225", "NIKKEI", "HK50", "AUS200")):
        return "CFD:ASIA_INDEX"

    base = t.replace("=X", "").replace("/", "").replace("_", "")
    if len(base) == 6 and base[:3] in _FX_CODES and base[3:] in _FX_CODES:
        return "FX:SPOT"

    for suffix, market_id in _SUFFIX_HINTS.items():
        if t.endswith(suffix):
            return market_id
    return "EQ:XNYS"          # US listings share one calendar (XNAS aliases here)
