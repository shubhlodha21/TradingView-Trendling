━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 12 ·  src/assets/us_stock.py
  USEquitySpec — the regression baseline · 8 policies that mirror equity 1:1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  555 lines · 9 dataclasses + 1 factory + 1 resolver · the equity corner of the
  multi-asset abstraction. THE INVARIANT: an engine on US equity via this spec
  must be byte-identical to the old nabi branch — same prices, same qty, same
  routing, same audit CSVs, same .gt_state shape, same dashboard numbers. If a
  PLTR test diverges with-spec vs without-spec, the bug is HERE, not the engine.

要 Require ┊ ib_async (late-imported Stock/IB) · the policies Protocols
          ┊ a symbol string · a FeedSnapshot · a timezone-aware datetime
出 Provides┊ make_us_equity_spec() → AssetSpec[US_EQUITY] · 8 concrete policies
          ┊ a SpecRegistry resolver auto-registered at import (priority 100)

─── 部  Modules used ──────────────────────────────────────────────────────
   __future__ / dataclasses     ┊ frozen+slots dataclasses for every policy
   datetime · zoneinfo          ┊ NY_TZ session math (DST-correct, UTC out)
   decimal                      ┊ exact tick / fee / spread arithmetic
   re                           ┊ _US_EQUITY_PATTERN symbol shape match
   ib_async  (TYPE_CHECKING)    ┊ Contract · IB — real import deferred to .make/.qualify
   .enum                        ┊ AssetClass.US_EQUITY
   .policies (+ .commission/.contract/.price/.sizing/.tick)
                                ┊ the 8 Protocols + ContractNotFound · NoUsablePrice
                                ┊ SizingMismatch · round_to_grid · Side · RoundDirection
   .resolver                    ┊ SpecRegistry.register(resolver, priority)
   .spec                        ┊ AssetSpec — the composed bundle returned
   .types                       ┊ Currency · Money · Price · Quantity · QuantityUnit · shares

─── 算  Algorithm · compose-once, then serve every tick ───────────────────
 Require: a symbol string (caller resolves it through SpecRegistry)
 Ensure : every equity symbol gets the SAME spec; behavior matches old equity.

  1: import-time  SpecRegistry.register(_us_equity_resolver, priority=100)
     │                                           ▷ runs once when assets.__init__
     │                                             imports this module last (so FX/
     │                                             futures win the shared "ES" shape)
  2: resolve  _us_equity_resolver(symbol, hint)  ▷ the registry's entry point
     │      if hint and hint ≠ US_EQUITY → None   ▷ operator can force another class
     │      if not _US_EQUITY_PATTERN.match → None ▷ 1-5 caps, opt .X suffix (BRK.B)
     │      else → make_us_equity_spec(symbol)
  3: make_us_equity_spec(_symbol)                 ▷ composes the 8 policies into one
     │      AssetSpec(asset_class=US_EQUITY, quote=USD, venue="SMART", …)
     │      _symbol arg ignored — API symmetry only (FX branches on pair; equity doesn't)
  4: ── per-symbol use ── SMARTStockContract.make(symbol)
     │      late-import Stock → Stock(SYM.upper(), "SMART", "USD")
     │      optional primaryExchange override (ETFs / ambiguous listings)
  5: SMARTStockContract.qualify(ib, contract)     ▷ await ib.qualifyContractsAsync
     │      empty result → raise ContractNotFound (typo / not on account)
  6: SMARTStockContract.identify(ib_contract)     ▷ reverse map broker→ticker
     │      secType ≠ "STK" → None  (don't claim FX/futures);  else symbol.upper()
  7: ── per-tick price ── LastPricePolicy.{reference,buy_compare,sell_compare}
     │      all three → _best_available(feed, prefer="last")  ▷ equity spread ≈ 1¢,
     │      `last` is the historical canonical price for trigger/stop/track
  8:   _best_available(feed, prefer)              ▷ fallback chain
     │      last → (bid+ask)/2 mid → else raise NoUsablePrice
  9:   is_actionable(feed)                         ▷ feed-quality gate
     │      need bid AND ask AND last; reject crossed; reject spread > 50 bps of mid;
     │      reject tick age > 30 s (stale / extended-hours junk)
 10: ── sizing/round/fee at order time ──
     │      DecimalTickPolicy(2).round_to_tick(price) → round_to_grid(p, 0.01, dir)
     │      SimpleSizing.notional(qty, price) → Money(qty.value × price, USD)
     │         qty.unit ≠ SHARES → raise SizingMismatch
     │      IBKRTieredEquityCommission.estimate(qty, price, side)
     │         fee = clamp(min $0.35 ≤ qty×$0.0035 ≤ 1% of trade)  (Tier-1 est.)
 11: ── session gating (UTC in, UTC out) ── USEquitySession
     │      windows_for_date: ET-localize → skip weekend / NYSE holiday →
     │      build [09:30..16:00 ET] → emit as UTC SessionWindow
     │      is_open_at · next_open (walk ≤14 d) · next_close · time_to_close ·
     │      is_within_n_minutes_of_close   ▷ feeds the engine's entry-cutoff logic
 12: ── lifecycle / risk (no-ops) ──
     │      NoLifecycle: needs_roll→False · expiry→None · settlement_days→T+1 ·
     │         has_overnight_financing→False
     │      USEquityRiskOverlay.check → RiskVerdict.ok  ▷ universal RiskGate suffices;
     │         a pass-through so the engine never needs an `if EQUITY: skip` branch

─── 関  Functions / classes defined ───────────────────────────────────────
   class SMARTStockContract          Stock(sym,"SMART","USD"); make·identify·qualify
   class LastPricePolicy             last-first price; reference/buy/sell_compare;
                                     is_actionable (spread+stale gates); _best_available
   class DecimalTickPolicy           power-of-10 tick by decimals; tick_size·
                                     round_to_tick·decimals_for_display; __post_init__ guard
   class SimpleSizing                notional=qty×price USD; for_us_equity classmethod;
                                     min_qty·qty_increment·is_valid_qty
   class IBKRTieredEquityCommission  Tier-1 fee estimate; estimate(qty,price,side,venue)
   class USEquitySession             NYSE RTH+holidays; is_open_at·windows_for_date·
                                     next_open·next_close·is_within_n_minutes_of_close·
                                     time_to_close
   class NoLifecycle                 non-rolling default; needs_roll·expiry·
                                     settlement_days·has_overnight_financing
   class USEquityRiskOverlay         pass-through check → RiskVerdict.ok
   def   make_us_equity_spec         factory → AssetSpec[US_EQUITY] (8 policies bundled)
   def   _us_equity_resolver         registry resolver: hint + pattern → spec | None

─── 変  Variables / state created ─────────────────────────────────────────
   NY_TZ                ZoneInfo     "America/New_York" — all session math anchor
   UTC                  timezone     output zone (downstream never sees ET)
   EQUITY_TICK_SIZE     Decimal      0.01 — penny tick (SEC 2001)
   TIERED_PER_SHARE     Decimal      0.0035 — IBKR Tier-1 per-share
   TIERED_MIN_PER_ORDER Decimal      0.35 — fee floor per order
   TIERED_MAX_PCT_OF_TRADE Decimal   0.01 — 1% of trade fee cap
   NYSE_HOLIDAYS_2024_2026 frozenset hardcoded NYSE closures 2024–2026 (add years fwd)
   NYSE_OPEN_LOCAL / NYSE_CLOSE_LOCAL time  09:30 / 16:00 ET RTH bounds
   _US_EQUITY_PATTERN   re.Pattern   ^[A-Z]{1,5}(\.[A-Z])?$  — equity symbol shape
   ▷ all policy instances are frozen+slots dataclasses — immutable, no per-symbol state

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   ib.qualifyContractsAsync · Stock(…)            (ib_async, late-imported)
   round_to_grid (.policies.tick)                 RiskVerdict.ok (.policies)
   SpecRegistry.register (.resolver)              AssetSpec(…) (.spec)
   raise ContractNotFound · NoUsablePrice · SizingMismatch · ValueError

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   assets/__init__.py     ▷ imports this module LAST → triggers resolver registration
   assets/resolver.py     ▷ SpecRegistry routes equity symbols here at runtime
   execution/broker.py:71 ▷ imports SMARTStockContract (contract build / identify)
   assets/forex.py:54     ▷ reuses NoLifecycle (FX cash also doesn't roll)
   assets/future.py:72    ▷ reuses LastPricePolicy
   assets/cfds.py:67      ▷ reuses DecimalTickPolicy·LastPricePolicy·NoLifecycle·
                            SimpleSizing·USEquitySession  (share-CFD mirrors equity)
   ▷ graph had no node for this file (post-build); edges grepped, not invented

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Regression baseline.  This file is the equity contract: any divergence from
     the old nabi-branch equity behavior is a bug to fix HERE, not in the engine.
   • last is canonical.  All three compare points return `last` — preserving the
     historical trigger/stop/track semantics; mid is only a fallback.
   • Actionable gate.  spread > 50 bps OR tick age > 30 s OR missing leg ⟶ not
     actionable — keeps extended-hours / halted junk out of the entry path.
   • UTC out.  Sessions emit UTC SessionWindows; downstream never touches ET/DST.
   • Half-days unhandled.  Christmas-Eve / post-Thanksgiving 13:00 closes treated
     as full sessions for Day-1 — operator halts manually (full calendar = wk 2).
   • Resolver priority.  registers at 100 (default); FX/futures register lower
     (higher priority) to claim shared shapes like "ES".  Reused widely — change a
     policy here and you move FX·futures·CFDs with it.
   • Links:  policies → [[policies]] · bundle → [[spec]] · routing → [[resolver]] ·
     contract build → [[broker]] · ticks → [[engine]] · siblings → [[forex]] [[cfds]] [[future]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
