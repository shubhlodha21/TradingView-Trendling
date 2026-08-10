"""
Ultra-Fast Pre-Trade Risk Check (< 0.001ms target)

Optimized for latency:
- All values cached, updated only on fills (not on every tick)
- No loops on hot path (pre-order check)
- All config values pre-cached at init
- Uses __slots__ for minimal memory footprint

Pre-order checks:
1. Position size vs equity (order_value <= equity * 0.95)
2. Daily P&L vs loss limit (cached, updated on fill)
3. Consecutive losses limit (cached, updated on fill)
4. Trades per day limit (cached, updated on fill)
5. Price staleness (gateway heartbeat)

Post-order: P&L recalculation only on fill events.
"""
import json
import os
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.models import Config


def fx_quote_pnl_to_usd(ticker: str, quote_pnl: float, ref_price: float,
                        qty: float) -> float:
    """Convert a realized P&L expressed in an FX pair's QUOTE currency to USD.

    `quote_pnl` is `(exit - entry) * qty`, denominated in the pair's QUOTE
    currency (FX quoting: ticker='BBBQQQ', qty in BBB, price is BBB→QQQ rate,
    so a price-delta × qty is in QQQ). This normalizes it to USD using the
    SAME classification as `RiskCheck._fx_to_usd_notional`, so the displayed
    P&L and the daily-loss gate can never diverge:

      • equity / futures / CFD / unrecognised ticker → already USD, unchanged.
      • EURUSD, GBPUSD (quote == USD)               → already USD, unchanged.
      • USDJPY (base == USD, quote = JPY)           → quote_pnl / ref_price
        (JPY → USD ≈ ÷ USDJPY rate, which is ref_price).
      • crosses (EURJPY, EURGBP, CADCHF …)          → quote_pnl / ref_price,
        the SAME 1/price approximation _fx_to_usd_notional uses for crosses
        (no live BASE→USD rate is plumbed; documented ~0.7–1.5x for crosses).

    Best-effort and TOTALLY side-effect free: on ANY error or a zero
    ref_price it returns `quote_pnl` unchanged, so it can never break the
    fill path. Equities are provably untouched (6-alpha-upper guard fails
    for AAPL/NVDA/etc → returns input).
    """
    try:
        if not (len(ticker) == 6 and ticker.isalpha() and ticker.isupper()):
            return quote_pnl                       # equity / futures / CFD
        quote = ticker[3:]
        if quote == "USD":
            return quote_pnl                       # EURUSD: P&L already USD
        if not ref_price:
            return quote_pnl                       # guard against ÷0
        return quote_pnl / ref_price               # USDJPY + crosses
    except Exception:
        return quote_pnl                           # never break record/display


class PortfolioReader:
    """Aggregates risk metrics across every running bot instance.

    Each `run_live.py` writes `.gt_state_<SYM>_<CID>.json` (event-driven)
    and `.gt_live_<SYM>_<CID>.json` (5Hz) to the working directory. The
    multi-symbol aggregator (`dashboard_agg.py`) already sums these for
    its header pills — we reuse the same data so the risk gate sees the
    same portfolio-wide picture an operator does.

    Aggregates produced:
      * `open_notional()`  — Σ across every peer:
                              position_notional (FILLED position × LTP)
                            + qty × entry for OFFLINE state files
                              (killed bots whose positions are still at
                              the broker still tie up real capital).
                            A working-but-unfilled entry does NOT count —
                            exposure reflects FILLED positions only, so a
                            fresh entry counts toward the cap from fill,
                            not from submission.
      * `daily_pnl()`      — Σ realized `pnl` from every state file.

    Caching: 1s TTL so the hot-path risk gate stays sub-millisecond.
    Refresh happens lazily on `open_notional()` / `daily_pnl()` calls
    — bounded by the file glob (one directory, ~handful of files).
    """
    __slots__ = (
        '_cwd', '_cache_ttl', '_cache_time',
        '_cached_open_notional', '_cached_daily_pnl',
        '_ts', '_errors', '_last_error',
        # High-water mtime across all peer state/live files seen on the
        # last refresh. Compared to a fresh scandir on each check to
        # detect "any peer wrote a new state file" without re-parsing.
        '_last_max_mtime',
    )

    def __init__(self, cwd: Optional[Path] = None, ttl_s: float = 0.1):
        """ttl_s lowered from 1.0s → 0.1s as a belt to mtime-invalidation's
        suspenders. The combination means:
          * Cache invalidates within 100ms of wall clock regardless of files
          * AND invalidates immediately when any peer state/live file mtime
            advances (mtime-based, so a fill that triggers a state-file
            write is visible to the next risk check within microseconds,
            not seconds). Production failure mode 2026-05-26: false re-entry
            rejects right after a fill because the cache hadn't refreshed
            in the 1ms window between SELL fill and BUY attempt.
        """
        self._cwd = Path(cwd) if cwd is not None else Path('.')
        self._cache_ttl = ttl_s
        self._cache_time = 0.0
        self._cached_open_notional = 0.0
        self._cached_daily_pnl = 0.0
        self._ts = datetime.now
        self._errors = 0
        self._last_error = ""
        # Max mtime across all peer state/live files we read last time.
        # Compared to a fresh os.scandir() pass on each refresh check;
        # any newer mtime forces a re-parse regardless of TTL.
        self._last_max_mtime: float = 0.0

    def open_notional(self) -> float:
        self._maybe_refresh()
        return self._cached_open_notional

    def daily_pnl(self) -> float:
        self._maybe_refresh()
        return self._cached_daily_pnl

    def invalidate(self) -> None:
        """Force the next read to re-parse. Cheap to call on every fill
        event — the next risk-gate check picks up fresh state without
        waiting for TTL. Engine wires this from `_on_gateway_fill` on
        every fill so a BUY entry right after a SELL fill doesn't see
        stale 'open notional' from the just-closed position."""
        self._cache_time = 0.0
        self._last_max_mtime = 0.0

    def _scan_max_mtime(self) -> float:
        """Cheapest possible 'has anything changed?' probe: scandir the
        peer state + live files and return the largest mtime seen.
        Avoids the JSON parse cost when nothing has moved.

        Costs ~one stat() per peer file (~5µs on hot SSD). Total for a
        portfolio of 10 bots: ~50µs. Beats parsing all 10 files on
        every check, which is what TTL-only would do."""
        import os as _os
        hi = 0.0
        try:
            with _os.scandir(self._cwd) as it:
                for entry in it:
                    name = entry.name
                    if not (name.startswith('.gt_state_') and name.endswith('.json')) \
                       and not (name.startswith('.gt_live_') and name.endswith('.json')):
                        continue
                    try:
                        mt = entry.stat().st_mtime
                    except OSError:
                        continue
                    if mt > hi:
                        hi = mt
        except FileNotFoundError:
            return 0.0
        return hi

    def _maybe_refresh(self) -> None:
        now = self._ts().timestamp()
        # TTL-OR-mtime invalidation: either timer expired OR any peer
        # file has been touched since our last parse → re-read. mtime
        # check is much cheaper than the actual parse pass below.
        if now - self._cache_time < self._cache_ttl:
            current_max = self._scan_max_mtime()
            if current_max <= self._last_max_mtime:
                return  # nothing changed AND TTL hasn't expired
            # Fall through to re-parse — a peer wrote new state.
        notional = 0.0
        pnl = 0.0
        try:
            for state_path in self._cwd.glob('.gt_state_*_*.json'):
                try:
                    with open(state_path) as f:
                        st = json.load(f)
                except Exception as e:
                    self._errors += 1
                    self._last_error = f"{state_path.name}: {type(e).__name__}"
                    continue
                pnl += float(st.get('pnl', 0) or 0)
                # Prefer the live snapshot's pre-computed `position_notional`
                # (qty × LTP). Falls back to qty × entry_price from the
                # state file when the live snapshot is missing or zero
                # (offline / pre-tick). Mirrors dashboard_agg.py:395-404.
                live_path = state_path.parent / state_path.name.replace(
                    '.gt_state_', '.gt_live_'
                )
                live_notional = 0.0
                # Exposure counts FILLED position only. A working-but-
                # unfilled entry (`pending_notional`) is deliberately NOT
                # added to the portfolio total — the risk gate sees a
                # bot's exposure from the moment its entry FILLS, not the
                # moment it's submitted.
                if live_path.exists():
                    try:
                        with open(live_path) as f:
                            live = json.load(f)
                        live_notional = float(live.get('position_notional', 0) or 0)
                    except Exception as e:
                        self._errors += 1
                        self._last_error = f"{live_path.name}: {type(e).__name__}"
                if live_notional > 0:
                    # live_notional is now USD-equivalent (run_live.py writes
                    # FX-normalized snapshots since 2026-06-09). Older
                    # snapshots may still be in quote ccy — defensively
                    # re-normalize using ticker+LTP if we can derive them.
                    notional += live_notional
                elif st.get('position_open'):
                    qty = float(st.get('quantity', 0) or 0)
                    entry = float(st.get('entry_price', 0) or 0)
                    # ── Multi-asset notional (D4-PM) ─────────────────
                    # Resolve the peer's spec from the ticker so we
                    # get multiplier-correct math for futures and
                    # unit-correct math for FX. Equity is byte-
                    # identical via SimpleSizing. Fallback to raw
                    # qty*entry on any lookup failure.
                    peer_ticker = st.get('ticker') or st.get('symbol') or ""
                    fallback = qty * entry
                    if not peer_ticker:
                        notional += fallback
                    else:
                        try:
                            from src.assets import resolve as _resolve_spec
                            from src.assets.types import (
                                Quantity as _Q, price as _P,
                            )
                            from decimal import Decimal as _D
                            _spec = _resolve_spec(peer_ticker)
                            _q = _Q(_D(str(qty)), _spec.sizing.expected_unit)
                            _n = _spec.sizing.notional(_q, _P(str(entry)))
                            peer_n = float(_n.amount)
                            # FX → USD normalization (same as run_live.py).
                            # Without this, USDJPY's JPY-quoted notional
                            # gets summed as USD (4M JPY → $4M phantom).
                            fx_usd = RiskCheck._fx_to_usd_notional(
                                peer_ticker, entry, qty,
                            )
                            if fx_usd is not None:
                                peer_n = fx_usd
                            notional += peer_n
                        except Exception:
                            notional += fallback
        except Exception as e:
            # Glob failure shouldn't crash the risk gate. Surface via
            # errors counter, keep last cached values.
            self._errors += 1
            self._last_error = f"glob: {type(e).__name__}"
        self._cached_open_notional = notional
        self._cached_daily_pnl = pnl
        self._cache_time = now
        # Snapshot the high-water mtime so the next refresh check can
        # decide "nothing changed" without re-parsing.
        self._last_max_mtime = self._scan_max_mtime()


class PortfolioLimitsReader:
    """Shared portfolio-level risk-limit overrides.

    Reads (and optionally writes) `.gt_portfolio_limits.json` in cwd. The
    file is a single source of truth for portfolio-wide caps, set ONCE
    by an operator (typically via `python dashboard_agg.py --exposure
    50000 --risk 2000`) and consumed by every bot's risk gate.

    File format:
        {
          "max_position_value_usd": 50000,
          "max_daily_loss_usd": 2000,
          "updated_at": "2026-05-26T12:34:56.789012",
          "updated_by": "dashboard_agg.py --exposure 50000"
        }

    Caching: 1s TTL on the read side so the hot-path risk gate stays
    sub-millisecond. Picks up file edits within ~1s.

    Semantics: when both the file AND a bot's per-instance config carry
    a limit, RiskCheck uses `min(file, config)` — most conservative
    wins. That way a per-bot `--risk 500` is never silently relaxed by
    a file that says $2000, AND a portfolio-wide $1000 cap from the
    file is honored by bots launched without their own `--risk` flag.

    Missing file = no override. Bots fall back to their config values
    (themselves overridable by their own `--risk` / `--exposure` CLI
    flags). Same semantics as before this layer existed.
    """
    __slots__ = ("_path", "_cache", "_mtime", "_cache_time",
                 "_cache_ttl", "_ts")

    FILENAME = ".gt_portfolio_limits.json"

    def __init__(self, cwd: Optional[Path] = None, ttl_s: float = 1.0):
        import os as _os
        base = Path(cwd) if cwd else Path(_os.environ.get("GT_WEBAPP_CWD", "."))
        self._path = base / self.FILENAME
        self._cache: dict = {}
        self._mtime: float = 0.0
        self._cache_time: float = 0.0
        self._cache_ttl = ttl_s
        self._ts = datetime.now

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict:
        """Return the current override dict (possibly empty).

        Cached for `ttl_s`; re-parses only when on-disk mtime moves.
        Defensive against partial writes (returns last good cache on
        JSON error)."""
        now = self._ts().timestamp()
        if now - self._cache_time < self._cache_ttl:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            self._cache_time = now
            return self._cache
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._cache_time = now
            return self._cache
        if mtime != self._mtime:
            try:
                with open(self._path, "rb") as f:
                    self._cache = json.loads(f.read() or b"{}")
                self._mtime = mtime
            except Exception:
                # Keep prior cache; transient bad-read doesn't reset to {}.
                pass
        self._cache_time = now
        return self._cache

    def write(self, *, updated_by: str = "", **fields) -> dict:
        """Merge `fields` into the persisted limits dict.

        Atomic via temp+rename. Other readers see either the prior
        complete file or the new complete file, never a partial.
        Numeric values <= 0 mean "clear this key" so an operator
        can disable a previously-set override:
            limits.write(max_daily_loss_usd=0, updated_by="cleared")
        """
        current: dict = {}
        if self._path.exists():
            try:
                with open(self._path, "rb") as f:
                    current = json.loads(f.read() or b"{}")
            except Exception:
                current = {}
        for k, v in fields.items():
            if isinstance(v, (int, float)) and v <= 0:
                current.pop(k, None)
            else:
                current[k] = v
        current["updated_at"] = self._ts().isoformat(timespec="seconds")
        if updated_by:
            current["updated_by"] = updated_by
        # Atomic write
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(current, f, indent=2)
            tmp.replace(self._path)
        except Exception:
            # Cleanup best-effort
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        # Refresh cache so next read() returns the new values immediately.
        self._cache = current
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = 0.0
        self._cache_time = self._ts().timestamp()
        return current


@dataclass(slots=True)
class RiskResult:
    """Result of pre-trade risk check.

    `circuit_break=True` signals a terminal condition — the engine should
    not just block this single entry, it should SHUT DOWN. Used by the
    portfolio daily-loss gate: once the day is past -$max_daily_loss_usd
    we want no more attempts at all, not just rejection of the next one.
    """
    allowed: bool
    reason: str = ""
    circuit_break: bool = False


class RiskCheck:
    """
    Ultra-fast pre-trade risk validation.

    Hot path (pre-order check): O(1) checks against cached values.
    No loops, no registry iteration on tick path.

    Cache update only happens on:
    - record_fill() - post-order, after a fill
    - reset_daily() - called at market open
    """
    __slots__ = (
        'config', 'gateway', 'portfolio', 'limits',
        '_cached_daily_pnl', '_cached_trades_today',
        '_consecutive_losses', '_daily_reset_date',
        '_ts',
        # Pre-cached config (avoid attribute access in hot path)
        '_max_consec', '_max_trades', '_daily_loss_pct',
        '_max_position_usd', '_max_daily_loss_usd',
        '_equity_cache', '_equity_cache_time', '_equity_cache_ttl',
        # Pending buy tracking for P&L calculation on SELL
        '_pending_buy',
        # Equity-lookup failure diagnostics — surface the staleness instead
        # of silently falling back to a fake $1M.
        '_equity_known', '_equity_errors', '_last_equity_warn',
        # Buying-power cache — same TTL semantics as equity. Used by the
        # dashboard "BP USED" pill, not by the hot-path order gate (which
        # already uses equity). Treated as best-effort: a missing or 0
        # value just makes the pill render as "--" instead of blocking trading.
        '_bp_cache', '_bp_cache_time', '_bp_known',
    )

    def __init__(self, config: "Config", gateway: "Gateway",
                 portfolio: Optional[PortfolioReader] = None,
                 limits: Optional[PortfolioLimitsReader] = None):
        self.config = config
        self.gateway = gateway
        # PortfolioReader sees ALL running bot instances via their state
        # files. None = single-instance / test mode → gates fall back to
        # this instance's own counters (same as before this change).
        self.portfolio = portfolio
        # PortfolioLimitsReader picks up portfolio-wide cap overrides
        # set by `dashboard_agg.py --exposure NN --risk NN` (or any
        # writer of `.gt_portfolio_limits.json`). When present, the
        # effective limit per gate = min(file_value, config_value) so
        # the most conservative source always wins.
        self.limits = limits

        # Cached risk values (updated on fill, not on every tick)
        self._cached_daily_pnl: float = 0.0
        self._cached_trades_today: int = 0
        self._consecutive_losses: int = 0

        # Daily reset tracking (reset counters at market open)
        self._ts = datetime.now
        self._daily_reset_date: date = self._ts().date()

        # Pre-cache config values (avoid repeated attribute access in hot path)
        self._max_consec = config.max_consecutive_losses
        self._max_trades = config.max_trades_per_day
        self._daily_loss_pct = config.daily_loss_limit_pct
        self._max_position_usd = config.max_position_value_usd
        self._max_daily_loss_usd = config.max_daily_loss_usd

        # Equity cache with TTL (avoid calling gateway.get_equity on every tick).
        # `_equity_known` flips to True only after the first successful gateway
        # fetch — until then we MUST refuse trades, not pretend we have $1M.
        self._equity_cache: float = 0.0
        self._equity_cache_time: float = 0.0
        self._equity_cache_ttl: float = 1.0  # Refresh equity every 1 second
        self._equity_known: bool = False
        self._equity_errors: int = 0
        self._last_equity_warn: float = 0.0

        # Buying-power cache (display-only; not in the order gate). Shares
        # the equity TTL so both refresh on the same 1s cadence.
        self._bp_cache: float = 0.0
        self._bp_cache_time: float = 0.0
        self._bp_known: bool = False

        # Pending buy price/qty for P&L calculation on SELL fill
        self._pending_buy: Optional[tuple] = None

    # === HOT PATH: Pre-order check (< 0.001ms) ===
    @staticmethod
    def _fx_to_usd_notional(ticker: str, price: float, qty: int) -> Optional[float]:
        """Convert an FX order's price*qty into USD notional.

        FX quoting convention: ticker = 'BBBQQQ', qty is in BBB (base),
        price is BBB→QQQ rate. So price*qty is denominated in QQQ.

          EURUSD 25000 @ 1.16  →  price*qty = 29,000 USD ✓
          USDJPY 25000 @ 160   →  price*qty = 4,000,000 JPY  (NOT USD!)
          EURJPY 25000 @ 185   →  price*qty = 4,625,000 JPY
          GBPUSD 25000 @ 1.34  →  price*qty = 33,500 USD ✓

        This staticly classifies via the 6-letter pair shape and returns
        a USD-equivalent estimate. Returns None when the ticker isn't a
        recognizable FX pair (caller falls back to price*qty for
        equities/futures/CFDs where price is already USD-per-unit).

        Live regression 2026-06-09: USDJPY/EURJPY entries got blocked at
        the risk gate because `combined exposure $4M > $50k cap` — JPY
        notional was treated as USD.
        """
        if not (len(ticker) == 6 and ticker.isalpha() and ticker.isupper()):
            return None
        base, quote = ticker[:3], ticker[3:]
        if quote == "USD":
            return float(price * qty)     # EURUSD: 1.16 × 25000 = 29000 USD
        if base == "USD":
            return float(qty)              # USDJPY: qty IS in USD
        # Cross pair (EURJPY, GBPJPY, EURGBP...) — qty is in BASE.
        # Without a live BASE→USD rate we approximate as qty USD
        # (off by ~0.7–1.5x depending on the base ccy). Acceptable
        # for risk gating; if precise sizing matters, plumb the
        # gateway's accountValues rates through here later.
        return float(qty)

    def check(self, price: float, qty: int) -> RiskResult:
        """
        Pre-trade risk check. All O(1) cached lookups.

        Target: < 0.001ms (1 microsecond)

        Equity-based gates (position size %, daily loss %) only apply when
        equity is actually known. IBKR pushes accountSummary 0.5–2s after
        connect; the engine fires its first order within milliseconds of
        connect. We do NOT block trading during that window, and we do
        NOT lie with a fake $1M fallback (that would let a $950k order
        through against a $100k account). Instead: skip the equity-based
        checks when equity is unknown, keep the non-equity gates active
        (consec losses, max trades, price staleness).
        """
        # ── STRESS-MODE BYPASS (GT_DISABLE_RISK_GATE=1) ──────────────────
        # Skip EVERY check (combined-exposure, daily-loss, consec-losses,
        # max-trades, price-staleness). Operator flag for stress drivers
        # that explicitly need the gate out of the way. NEVER set in live
        # trading. Set by tests/paper/stress_churn.py.
        try:
            if os.environ.get("GT_DISABLE_RISK_GATE", "").strip():
                return RiskResult(True)
        except Exception:
            pass

        # USD-equivalent notional. For FX, raw price*qty is in QUOTE ccy
        # (which may not be USD); use the asset-aware helper to normalize.
        ticker = getattr(self.config, 'ticker', '') or ''
        fx_usd = self._fx_to_usd_notional(ticker, price, qty)
        order_value = fx_usd if fx_usd is not None else price * qty
        equity = self._get_cached_equity()
        equity_known = self._equity_known and equity > 0

        # Compute EFFECTIVE per-gate caps. When the shared limits file
        # carries an override AND it's tighter than this bot's config,
        # the file wins (most conservative source binds). A missing file
        # or missing key falls back to the per-bot config. Result: an
        # operator can pass `--exposure 30000` to dashboard_agg ONCE
        # and every running bot picks it up within ~1s, no relaunch
        # needed; per-bot `--exposure 20000` still tightens for that bot.
        file_limits = self.limits.read() if self.limits is not None else {}
        eff_max_position_usd = self._max_position_usd
        eff_max_daily_loss_usd = self._max_daily_loss_usd
        file_exposure = file_limits.get("max_position_value_usd")
        if isinstance(file_exposure, (int, float)) and file_exposure > 0:
            eff_max_position_usd = min(eff_max_position_usd, float(file_exposure))
        file_loss = file_limits.get("max_daily_loss_usd")
        if isinstance(file_loss, (int, float)) and file_loss > 0:
            eff_max_daily_loss_usd = min(eff_max_daily_loss_usd, float(file_loss))

        # 1. Combined exposure — portfolio-wide cap on (existing positions
        # across every running bot + this new order). PortfolioReader sums
        # `position_notional` from `.gt_live_*_*.json` plus qty×entry from
        # offline state files (killed bots' positions still tie up capital).
        # When PortfolioReader is absent (test / paper one-off) we degrade
        # to the single-order check — still useful, just not portfolio-aware.
        portfolio_notional = self.portfolio.open_notional() if self.portfolio else 0.0
        combined = portfolio_notional + order_value
        if combined > eff_max_position_usd:
            return RiskResult(
                False,
                f"Combined exposure ${combined:.0f} > ${eff_max_position_usd:.0f} "
                f"cap (open=${portfolio_notional:.0f}, new=${order_value:.0f})"
            )

        # 2a. Daily loss — hard dollar floor.
        # Operator's chosen basis: IBKR `realizedPnL` from the account reqPnL
        # feed — today's REALIZED P&L (closed trades), account-wide, auto-
        # reset each session. This matches TWS's "Realized" line; we
        # deliberately do NOT use `dailyPnL`, which on this FX-heavy account
        # reports a gross, unstable number that doesn't net to TWS's DAILY.
        # Falls back to the state-file realized sum (or this instance's
        # cache) until IBKR's value has landed.
        ibkr_daily = None
        try:
            ibkr_daily = self.gateway.get_realized_pnl()
        except Exception:
            ibkr_daily = None
        if ibkr_daily is not None:
            daily_pnl = ibkr_daily
            daily_src = "IBKR"
        elif self.portfolio is not None:
            daily_pnl = self.portfolio.daily_pnl()
            daily_src = "portfolio"
        else:
            daily_pnl = self._cached_daily_pnl
            daily_src = "local"
        if daily_pnl <= -eff_max_daily_loss_usd:
            return RiskResult(
                False,
                f"Daily loss ${daily_pnl:.0f} <= -${eff_max_daily_loss_usd:.0f} "
                f"({daily_src})",
                circuit_break=True,
            )

        # 2b. Daily loss (legacy %-of-equity gate) — kept alongside 2a so
        # whichever trips first wins. Skipped until equity is known.
        if equity_known and self._cached_daily_pnl < equity * self._daily_loss_pct:
            return RiskResult(False, "Daily loss limit")

        # 3. Consecutive losses (cached counter)
        if self._consecutive_losses >= self._max_consec:
            return RiskResult(False, f"Consec loss limit ({self._max_consec})")

        # 4. Trades per day (cached counter)
        # Check daily reset first
        today = self._ts().date()
        if today != self._daily_reset_date:
            self._reset_daily(today)

        if self._cached_trades_today >= self._max_trades:
            return RiskResult(False, f"Max trades ({self._max_trades})")

        # 5. Price staleness (gateway heartbeat check)
        if not self._is_price_fresh():
            return RiskResult(False, "Price stale")

        return RiskResult(True)

    # === COLD PATH: Updated only on fill events ===

    def record_fill(self, fill_price: float, fill_qty: int, side: str):
        """
        Update cached risk values after a fill.
        Called by engine on fill event - not on every tick.
        """
        # Increment trade counter
        self._cached_trades_today += 1

        # Update P&L cache based on side.
        # SHORT INVERSION (P11): entry is the SELL (open), cover is the BUY
        # (close). The long form stashed on BUY / realized on SELL — so for a
        # short the SELL entry hit the realize branch with nothing pending
        # (no-op) and the BUY cover only stashed → the round-trip was never
        # booked and _cached_daily_pnl never moved (dead legacy daily-loss
        # gate). `_pending_buy` keeps its name but now holds the open SHORT entry.
        if side == "SELL":
            self._pending_buy = (fill_price, fill_qty)
        elif side == "BUY" and self._pending_buy is not None:
            entry_price, entry_qty = self._pending_buy
            matched = min(fill_qty, entry_qty)
            # Short round-trip gross: (entry_sell − cover_buy) * matched.
            gross = (entry_price - fill_price) * matched
            # Normalize quote-ccy P&L → USD for non-USD-quoted FX (USDJPY→JPY,
            # crosses) so the daily-loss gate is in USD. Equity / USD-quoted
            # FX pass through unchanged. Never raises (returns raw on error).
            gross = fx_quote_pnl_to_usd(
                getattr(self.config, 'ticker', '') or '', gross, fill_price, matched)
            self._cached_daily_pnl += gross
            self._pending_buy = None

    def record_loss(self):
        """Call after losing trade."""
        self._consecutive_losses += 1

    def record_win(self):
        """Call after winning trade."""
        self._consecutive_losses = 0

    def reset_daily(self):
        """Reset daily counters. Call at market open."""
        self._reset_daily(self._ts().date())

    def _reset_daily(self, today: date):
        """Reset all daily-cached values."""
        self._daily_reset_date = today
        self._cached_trades_today = 0
        self._cached_daily_pnl = 0.0
        self._consecutive_losses = 0

    # === HELPER: Cached equity (< 1ms TTL) ===

    def _get_cached_equity(self) -> float:
        """Get equity from cache, refresh if stale.

        Returns 0.0 when equity has never been successfully read. The check()
        gate refuses orders in that case — far safer than the old behavior
        of returning a hardcoded $1,000,000 fallback that would let oversized
        orders through against a real (smaller) account.
        """
        now = self._ts().timestamp()
        if now - self._equity_cache_time > self._equity_cache_ttl:
            fresh = self._get_equity()
            if fresh > 0:
                self._equity_cache = fresh
                self._equity_known = True
            # On failure: keep the last good value (if any) so a transient
            # gateway hiccup doesn't immediately tank trading, but never
            # flip _equity_known back to False — staleness is bounded by
            # the heartbeat-age gate already in check().
            self._equity_cache_time = now
        return self._equity_cache if self._equity_known else 0.0

    def _get_cached_buying_power(self) -> float:
        """Get buying power from cache, refresh if stale (1s TTL).

        Best-effort — failures are silent (no warn log) because this is
        display-only. Returns 0.0 until the first successful fetch lands,
        which the dashboard renders as "--".
        """
        now = self._ts().timestamp()
        if now - self._bp_cache_time > self._equity_cache_ttl:
            try:
                fresh = self.gateway.get_buying_power()
                if fresh and fresh > 0:
                    self._bp_cache = float(fresh)
                    self._bp_known = True
            except Exception:
                # Stay quiet — equity warnings already cover IBKR-side outage.
                pass
            self._bp_cache_time = now
        return self._bp_cache if self._bp_known else 0.0

    def _get_equity(self) -> float:
        """Get current equity from gateway.

        Returns 0.0 on failure so the caller can keep the last known good
        value or refuse trades. Throttled warning every 30s to surface a
        stuck gateway without spamming the dashboard.
        """
        try:
            equity = self.gateway.get_equity()
            if equity and equity > 0:
                return float(equity)
            # `None` or 0 from gateway = unknown. Count + (rate-limited) warn.
            self._equity_errors += 1
        except Exception as e:
            self._equity_errors += 1
            now = self._ts().timestamp()
            if now - self._last_equity_warn > 30.0:
                print(
                    f"[risk] gateway.get_equity() failed ({type(e).__name__}: {e}); "
                    f"errors={self._equity_errors}",
                    file=__import__('sys').stderr,
                )
                self._last_equity_warn = now
        return 0.0

    def _is_price_fresh(self) -> bool:
        """Check if price data is recent (< 60s)."""
        try:
            hb = getattr(self.gateway, '_last_heartbeat', None)
            if not hb:
                return True  # Allow if no heartbeat (paper mode)
            age = (self._ts() - hb).total_seconds()
            return age < 60
        except:
            return True

    # === STATUS ===

    @property
    def status(self) -> dict:
        """Get current risk status for dashboard."""
        return {
            'daily_pnl': round(self._cached_daily_pnl, 2),
            'trades_today': self._cached_trades_today,
            'consec_losses': self._consecutive_losses,
            'equity': round(self._get_cached_equity(), 2),
            # Buying power surfaces in the System panel's "BP USED" row so
            # operators see margin headroom alongside the equity-based
            # exposure %. 0.0 = not yet known (renders blank, no row).
            'buying_power': round(self._get_cached_buying_power(), 2),
        }