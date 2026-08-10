━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 50 ·  src/strategy/risk.py
  the pre-trade gate — sub-microsecond yes/no before any BUY is armed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  777 lines · 1 free function · 4 classes (fx helper · 2 file-readers · result
  dataclass · the gate). The hot path is O(1) against cached values — no loops,
  no JSON parse, no gateway call on the tick that places an order. Every slow
  thing (equity fetch, peer-file scan, limits read) hides behind a TTL cache.

要 Require ┊ a Config (caps, ticker), a Gateway (.get_equity / .get_buying_power
          ┊ / _last_heartbeat), and — optionally — a PortfolioReader +
          ┊ PortfolioLimitsReader for cross-bot, file-shared limits
出 Provides┊ RiskCheck.check() → RiskResult(allowed, reason, circuit_break)
          ┊ record_fill / record_loss / record_win / reset_daily (cold path)
          ┊ fx_quote_pnl_to_usd() · PortfolioReader · PortfolioLimitsReader

─── 部  Modules used ─────────────────────────────────────────────────────
   json · os                       ┊ peer state/live + limits file I/O · env flags
   dataclasses (dataclass)         ┊ RiskResult (slots=True)
   datetime (datetime, date)       ┊ daily-reset boundary · TTL clocks · heartbeat age
   pathlib (Path)                  ┊ cwd globbing of .gt_state_* / .gt_live_*
   typing (Optional, TYPE_CHECKING)┊ Config only imported for type-checking
   config.models (Config)          ┊ TYPE_CHECKING — caps, ticker (no runtime import)
   assets (resolve)                ┊ LAZY, inside _maybe_refresh — multiplier-correct
   assets.types (Quantity, price)  ┊ peer notional for futures/FX (D4-PM)

─── 算  Algorithm · the hot-path gate + its caches ────────────────────────
 Require: price, qty for the order the engine wants to place.
 Ensure : return allowed=True ONLY if portfolio-wide exposure, daily loss,
          consec losses, trades-today AND price-freshness all pass.

  1: RiskCheck.check(price, qty)               ▷ THE hot path · target <1µs
  2:   if env GT_DISABLE_RISK_GATE set → return RiskResult(True)
     │                                           ▷ stress-driver bypass · NEVER live
  3:   ticker ← config.ticker
  4:   fx_usd ← _fx_to_usd_notional(ticker, price, qty)
     │      ▷ 6-alpha-upper pair? quote==USD → price×qty ; base==USD → qty
     │        cross → qty (≈, no live BASE→USD rate) ; else None (equity/fut)
  5:   order_value ← fx_usd if not None else price × qty
  6:   equity ← _get_cached_equity()           ▷ 1s TTL ; 0.0 until first good read
  7:   equity_known ← _equity_known and equity > 0
  8:   file_limits ← limits.read() if limits else {}    ▷ 1s-TTL .json read
     │      eff_max_position_usd ← min(config, file)     ▷ tightest source wins
     │      eff_max_daily_loss_usd ← min(config, file)
  9:   ── gate 1 · combined exposure ──        ▷ portfolio-wide, not just this order
     │      portfolio_notional ← portfolio.open_notional() if portfolio else 0
     │      combined ← portfolio_notional + order_value
     │      if combined > eff_max_position_usd → RiskResult(False, …)
 10:   ── gate 2a · daily loss (hard USD floor) ──
     │      daily_pnl ← portfolio.daily_pnl() if portfolio else _cached_daily_pnl
     │      if daily_pnl ≤ −eff_max_daily_loss_usd
     │          → RiskResult(False, …, circuit_break=True)   ▷ SHUT DOWN, not just block
 11:   ── gate 2b · legacy %-of-equity loss ──  ▷ skipped until equity_known
     │      if _cached_daily_pnl < equity × _daily_loss_pct → RiskResult(False)
 12:   ── gate 3 · consecutive losses ──
     │      if _consecutive_losses ≥ _max_consec → RiskResult(False)
 13:   ── gate 4 · trades per day ──
     │      today ← _ts().date() ; if today ≠ _daily_reset_date → _reset_daily(today)
     │      if _cached_trades_today ≥ _max_trades → RiskResult(False)
 14:   ── gate 5 · price staleness ──
     │      if not _is_price_fresh() → RiskResult(False, "Price stale")
     │      ▷ heartbeat age ≥ 60s ; missing heartbeat → True (paper mode)
 15:   return RiskResult(True)                  ▷ all gates clear → arm the entry

 16: ── COLD PATH · record_fill(price, qty, side) ──  on every broker fill
     │      _cached_trades_today += 1
     │      BUY  → _pending_buy ← (price, qty)              ▷ remember entry
     │      SELL → gross ← (price − entry)×min(qty,entry_qty)
     │             gross ← fx_quote_pnl_to_usd(ticker, gross, price, matched)
     │                     ▷ normalize quote-ccy P&L → USD (USDJPY, crosses)
     │             _cached_daily_pnl += gross ; _pending_buy ← None
 17: record_loss() → _consecutive_losses += 1 ; record_win() → reset to 0
 18: reset_daily() → _reset_daily(today): zero trades, daily_pnl, consec

 19: ── CACHE refresh · PortfolioReader._maybe_refresh() ──  lazy, behind TTL
     │      if within TTL AND _scan_max_mtime() ≤ last → return (nothing moved)
     │      else glob .gt_state_*_*.json across cwd:
     │        pnl += st.pnl
     │        notional += live.position_notional   (qty×LTP, FX-normalized)
     │                  else qty×entry via assets.resolve + _fx_to_usd_notional
     │      ▷ RUNNING-ONLY (2026-07-02): live.pending_notional is NOT added —
     │        a working-but-unfilled BUY does not count toward exposure/cap

─── 関  Functions / classes defined ──────────────────────────────────────
   fx_quote_pnl_to_usd(ticker, quote_pnl, ref_price, qty)
                                 ┊ free fn · realized P&L quote-ccy → USD, never raises
   class PortfolioReader         ┊ Σ open_notional + daily_pnl across ALL peer bots
     open_notional               ┊ refresh + return cached USD exposure total
     daily_pnl                   ┊ refresh + return cached Σ realized P&L
     invalidate                  ┊ force next read to re-parse (wired on every fill)
     _scan_max_mtime             ┊ cheapest "did anything move?" probe (scandir, no parse)
     _maybe_refresh              ┊ TTL-OR-mtime gated glob+parse of peer state/live files
   class PortfolioLimitsReader   ┊ shared .gt_portfolio_limits.json caps (operator-set)
     path (property)             ┊ resolved file path
     read                        ┊ mtime-gated parse → override dict (last-good on error)
     write(**fields, updated_by) ┊ atomic temp+rename merge ; v≤0 clears a key
   @dataclass RiskResult         ┊ allowed · reason · circuit_break (slots)
   class RiskCheck               ┊ the gate itself (slots)
     __init__                    ┊ pre-cache all config caps + wire portfolio/limits
     _fx_to_usd_notional (static)┊ FX order price×qty → USD notional (or None)
     check                       ┊ THE hot path · 5 gates · RiskResult
     record_fill                 ┊ cold · trade count + P&L on fill
     record_loss / record_win    ┊ cold · consec-loss counter ±
     reset_daily / _reset_daily   ┊ cold · zero daily counters at open
     _get_cached_equity          ┊ 1s-TTL equity ; 0.0 until first good read (no fake $1M)
     _get_cached_buying_power    ┊ 1s-TTL BP · display-only · silent on failure
     _get_equity                 ┊ raw gateway.get_equity ; 0 on fail ; 30s-throttled warn
     _is_price_fresh             ┊ heartbeat age < 60s (True if no heartbeat)
     status (property)           ┊ dashboard dict: pnl, trades, consec, equity, BP

─── 変  Variables / state created ────────────────────────────────────────
   _cached_daily_pnl     float   realized P&L this session (cold-path only)
   _cached_trades_today  int     trade counter, reset at daily boundary
   _consecutive_losses   int     gate 3 counter ; record_win zeroes it
   _daily_reset_date     date    boundary — check() rolls counters when it moves
   _pending_buy          tuple?  (entry_price, qty) held BUY→SELL for P&L match
   _equity_cache / _time / _known   1s-TTL equity ; _known gates equity-based checks
   _bp_cache / _time / _known       1s-TTL buying power (display only)
   _equity_errors · _last_equity_warn  staleness diagnostics (30s throttle)
   _max_consec · _max_trades · _daily_loss_pct           pre-cached config caps
   _max_position_usd · _max_daily_loss_usd               pre-cached USD caps
   PortfolioReader._last_max_mtime  float  high-water mtime — mtime-invalidation
   PortfolioReader._cache_ttl       0.1s  (lowered from 1.0s — false re-entry reject fix)
   PortfolioLimitsReader.FILENAME   ".gt_portfolio_limits.json"  single source of caps

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   gateway.get_equity · .get_buying_power · ._last_heartbeat   (read-only)
   assets.resolve · assets.types.Quantity / price   (lazy, peer notional math)
   RiskCheck._fx_to_usd_notional   (self, also called from PortfolioReader)
   fx_quote_pnl_to_usd   (record_fill, and engine SELL path imports it)
   json · os.scandir · Path.glob   (peer/limits file I/O — off hot path)
   ▷ note: NEVER calls into engine/broker — a pure, side-effect-light gate

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   run_live.py:41,200-202   ▷ constructs RiskCheck + PortfolioReader + PortfolioLimitsReader
   run_live.py:783,852      ▷ imports RiskCheck for FX-normalized snapshot notional
   strategy.engine.py:6700  ▷ risk.check(trigger_price, qty) before arming entry
   strategy.engine.py:1575  ▷ risk.record_fill(BUY) on parent fill
   strategy.engine.py:2027-2031 ▷ record_fill(SELL) · record_loss / record_win on exit
   strategy.engine.py:5089  ▷ risk.reset_daily() at session open
   strategy.engine.py:2006,3970 ▷ imports fx_quote_pnl_to_usd for P&L display
   dashboard.py:1566        ▷ reads RiskCheck.status for the RISK panel
   dashboard_agg.py:460,1468-1469 ▷ PortfolioLimitsReader.write — operator sets caps
   tests/unit/test_risk.py  ▷ position-size · zero-equity · trades · consec · staleness
   ▷ graph note: knowledge-graph indexed the sibling `kinshasa` worktree (195 lines,
     pre-multi-asset); edges above re-verified by grep against this repo.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • No fake equity.  _get_cached_equity returns 0.0 until the first good read;
     equity-based gates (1, 2b) are SKIPPED, never satisfied by a phantom $1M.
   • Most-conservative wins.  Effective cap = min(per-bot config, shared file).
     A per-bot --risk is never silently relaxed by the portfolio file, and vice versa.
   • circuit_break ⇒ shut down.  Only the portfolio daily-loss floor (gate 2a) sets it;
     the engine stops the bot, not just the next entry.
   • Running-only exposure (2026-07-02).  PortfolioReader counts filled positions only;
     pending_notional is NOT booked, so a working unfilled BUY doesn't reserve capital.
     Tradeoff: two bots' simultaneous pendings no longer pre-reserve — the combined-fill
     race is guarded only by each bot's own new-order check (order_value) in the gate.
   • FX P&L is USD.  Both _fx_to_usd_notional (notional) and fx_quote_pnl_to_usd
     (realized P&L) keep the gate and the display in USD; crosses are a documented
     ≈0.7–1.5× approximation (no live BASE→USD rate plumbed yet).  [follow-up]
   • Hot path stays hot.  scandir-mtime + TTL keep peer-file parsing off the tick;
     invalidate() is wired on every fill so a BUY right after a SELL sees fresh state.
   • Links:  caps & ticker → [[models]] · armed by → [[engine]] · equity/heartbeat
     → [[broker]] · peer snapshots → [[persistence]] · asset notional → [[spec]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
