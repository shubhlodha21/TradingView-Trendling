━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 08 ·  src/assets/future.py
  the futures AssetSpec — multiplier · contract-month · roll · SPAN margin
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  610 lines · 8 dataclasses + 4 module functions · one composed AssetSpec per
  futures root. A leaf node: it assembles policies and registers itself with
  the resolver. The engine never imports it directly — it arrives via the
  SpecRegistry as just-another AssetSpec, so futures math (the multiplier)
  flows into the universal risk gate without the engine knowing it is futures.

要 Require ┊ FUTURE_ROOTS table (per-root multiplier/tick/exchange/commission)
          ┊ the policy protocols from assets.policies + assets.spec.AssetSpec
出 Provides┊ make_future_spec(symbol) → AssetSpec(asset_class=FUTURE)
          ┊ 8 policy dataclasses · _future_resolver auto-registered @ priority 20
          ┊ symbol parsing: "ES" · "ES202503" · "ESH5" all resolve to a spec

─── 部  Modules used ─────────────────────────────────────────────────────
   re                            ┊ three compiled regexes for symbol shapes
   dataclasses · decimal · datetime ┊ frozen slotted policies; Decimal money math
   .enum  (AssetClass)           ┊ tags the spec FUTURE
   .forex (ForexContinuousSession)┊ Day-3 stand-in for Globex 23h/5d session
   .policies                     ┊ FeedSnapshot · RoundDirection · OrderIntent
                                 ┊ PortfolioView · RiskVerdict — the gate types
   .policies.commission (Side)   ┊ BUY/SELL for the commission estimate
   .policies.contract            ┊ ContractAmbiguous · ContractNotFound (qualify)
   .policies.price (NoUsablePrice)┊ imported for the policy surface
   .policies.sizing (SizingMismatch)┊ raised when qty.unit ≠ CONTRACTS
   .policies.tick (round_to_grid)┊ the shared tick-rounding math
   .resolver (SpecRegistry)      ┊ register(_future_resolver, priority=20)
   .spec   (AssetSpec)           ┊ the composite returned by make_future_spec
   .types                        ┊ Currency · Money · Price · Quantity · contracts
   .us_stock (LastPricePolicy)   ┊ reused — futures DO have a real `last` feed
   ib_async (Future, IB, Contract)┊ TYPE_CHECKING + lazy local imports only

─── 算  Algorithm · symbol → spec → (price · size · gate) ────────────────
 Require: an operator symbol string ("ES" / "ES202503" / "ESH5")
 Ensure : an AssetSpec whose sizing carries the CORRECT multiplier — a wrong
          multiplier silently 10×'s risk (MES $5 vs ES $50) and blinds the gate.

  1: SpecRegistry.register(_future_resolver, priority=20)   ▷ at import time
     │                                          checked BEFORE forex(50)/equity(100)
     │                                          ▷ "ES" looks like equity — claim it first
  2: resolver fires: _future_resolver(symbol, hint)
  3:   if hint and hint ≠ FUTURE → return None     ▷ yield to other classes
  4:   root, _month ← _parse_futures_symbol(symbol) ▷ may raise ValueError → None
  5:   if root ∉ FUTURE_ROOTS → None ; else → make_future_spec(symbol)
  6: make_future_spec(symbol)                       ▷ the assembly point
  7:   root, _month ← _parse_futures_symbol(symbol) ▷ shape-detect:
     │      _ROOT_ONLY      "ES"        → (root, None)   ▷ caller defaults front-month
     │      _ROOT_YYYYMM    "ES202503"  → (root,"202503")▷ validate 1≤mm≤12, yyyy≥2020
     │      _ROOT_CODE_YEAR "ESH5"      → H=Mar, decade-anchor single-digit year
     │                                          ▷ good ~10y; longer needs YYYYMM
  8:   meta ← FUTURE_ROOTS[root]                    ▷ the per-product truth row
  9:   return AssetSpec(                            ▷ COMPOSE the policy bundle:
     │      asset_class = FUTURE · quote_currency = meta.currency · venue = exchange
     │      contract  = FuturesContractPolicy()           → builds ib_async.Future
     │      price     = LastPricePolicy()                 → real last-trade feed
     │      tick      = FuturesTickPolicy.for_root(root)  → grain 0.25/0.10/0.01…
     │      sizing    = MultiplierSizing.for_root(root)   → ★ the multiplier ★
     │      commission= IBKRFuturesCommission.for_root()  → flat per-contract
     │      session   = ForexContinuousSession()          → Globex approx (Day-3)
     │      lifecycle = FuturesRollLifecycle(roll_window_days=5)
     │      risk_overlay = FuturesRiskOverlay() )          → SPAN-headroom + roll
 10: ── at trade time, the engine/gate calls into the policies ──
 11:   contract.make(symbol)             ▷ root-only → _front_quarterly_month()
     │                                      next Mar/Jun/Sep/Dec; past ~15th → roll fwd
     │      → ib_async.Future(symbol=root, lastTradeDateOrContractMonth=month,
     │                        exchange, currency, multiplier=str(meta.multiplier))
 12:   await contract.qualify(ib, c)     ▷ qualifyContractsAsync → 0 ⇒ ContractNotFound
     │                                      >1 ⇒ ContractAmbiguous ; else qualified[0]
 13:   sizing.notional(qty, price)       ▷ ★ qty.value × price × multiplier (Money) ★
     │      if qty.unit ≠ CONTRACTS → SizingMismatch     ▷ this feeds the RiskGate
 14:   risk_overlay.check(intent, portfolio)            ▷ futures-only second gate:
     │      SELL → ok (exits never blocked)
     │      ① margin headroom: required = notional × buffer% (8%); if BP < required
     │         and same currency → BLOCK(margin_required, margin_available)
     │      ② roll window: expiry = lifecycle.expiry(contract.make(symbol));
     │         days_to_expiry ≤ min_days_before_roll(3) → BLOCK fresh entry
     │      else → ok ; every spec-access wrapped — a failed lookup never breaks gate
 15:   lifecycle.needs_roll(contract, ts)               ▷ Day-3 flag only (no auto-roll)
     │      parse YYYYMM(→day20) / YYYYMMDD; (expiry − ts).days ≤ roll_window_days
 16: reverse path — FuturesContractPolicy.identify(ib_contract)  ▷ position-match:
     │      secType ∈ {FUT, CONTFUT} → symbol.upper() as root, else None

─── 関  Functions / classes defined ──────────────────────────────────────
   FutureRootMeta              dataclass — per-root row (root·multiplier·tick·
                               exchange·currency·commission_per_contract·desc)
   _parse_futures_symbol       str → (root, yyyymm|None); raises ValueError
   _front_quarterly_month      next Mar/Jun/Sep/Dec heuristic for root-only
   FuturesContractPolicy       .make → ib Future · .identify (staticmethod) ·
                               async .qualify (ContractNotFound/Ambiguous)
   FuturesTickPolicy           .for_root · tick_size · round_to_tick · decimals_for_display
   MultiplierSizing            .for_root · notional ★ · min_qty · qty_increment · is_valid_qty
   IBKRFuturesCommission       .for_root · estimate (abs(qty)×per_contract)
   FuturesRollLifecycle        needs_roll · expiry · settlement_days ·
                               has_overnight_financing (→ False, mark-to-market)
   FuturesRiskOverlay          check — margin-headroom + roll-window block
   make_future_spec            symbol → composed AssetSpec(FUTURE)
   _future_resolver            registry callback (symbol, hint) → AssetSpec|None

─── 変  Variables / state created ────────────────────────────────────────
   UTC                  timezone     module-level alias for timezone.utc
   FUTURE_ROOTS         dict[str,FutureRootMeta]  12 roots: ES/MES/NQ/MNQ/RTY/
                                     M2K/GC/MGC/SI/CL/MCL/NG — the single source
                                     of per-product truth (multiplier is critical)
   _MONTH_CODE_TO_NUM   dict         F=1…Z=12 (CME month letters)
   _ROOT_ONLY           Pattern      ^[A-Z][A-Z0-9]{0,2}$
   _ROOT_YYYYMM         Pattern      ^(root)(\d{6})$
   _ROOT_CODE_YEAR      Pattern      ^(root)([FGHJKMNQUVXZ])(\d)$
   FuturesRiskOverlay   margin_buffer_pct=8 · min_days_before_roll=3  (overridable)
   FuturesRollLifecycle roll_window_days=5 · settlement_days_=0
   (all policy dataclasses are frozen + slots — immutable, one per spec)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   SpecRegistry.register                ▷ at import → priority 20
   AssetSpec(...)                       ▷ the composite assembled in make_future_spec
   ib_async.Future                      ▷ lazy local import in .make / overlay
   ib.qualifyContractsAsync             ▷ async network round-trip in .qualify
   round_to_grid (policies.tick)        ▷ shared tick math
   Money · Quantity · Currency (types)  ▷ notional / commission / min_qty values
   RiskVerdict.ok / .block              ▷ the overlay's two outcomes
   _parse_futures_symbol · _front_quarterly_month  ▷ internal helpers

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   assets/__init__.py        ▷ `from . import future` — import registers the resolver
   execution/broker.py:73    ▷ imports FuturesContractPolicy (contract build/identify)
   resolver.py               ▷ SpecRegistry holds _future_resolver; engine reaches the
                                spec only through resolve(symbol) — never imports future.py
   tests/assets/test_future.py            ▷ 24-case railguard: per-root multiplier math
   tests/assets/test_symbology_lockdown.py▷ FuturesContractPolicy.identify lockdown
   ▷ graph had no node for this file (index lag); edges confirmed by grep on call sites

─── 注  Notes · invariants ───────────────────────────────────────────────
   • The multiplier is the load-bearing number.  notional = qty × price × mult;
     a wrong row in FUTURE_ROOTS silently mis-sizes EVERY order for that root and
     blinds the universal gate. test_future.py spells the math out per root for CI.
   • Composition over inheritance.  Each policy is ~30 lines, frozen+slotted; the
     engine sees only the AssetSpec interface — futures-ness lives entirely here.
   • Front-month default.  Root-only "ES" → next quarterly via a HEURISTIC (3rd-Fri
     ≈ 20th); production should read IBKR ContractDetails for the exact roll date.
   • Day-3 stand-ins.  session = ForexContinuousSession (no 17:00–18:00 ET halt yet);
     roll = detection flag only (no auto-roll); commission = flat estimate (fill report
     has the truth); SPAN margin = notional-fraction heuristic (no real SPAN loaded).
   • Overlay fails-open by design.  Every spec/expiry access in check() is wrapped —
     a synthetic-test or lookup failure must never crash the gate, only skip a check.
   • Physical-delivery risk.  CL/NG settle physically; retail cannot accept — the
     roll-window block exists to force a manual roll before forced settlement.
   • Resolver priority 20 < forex(50) < equity(100) — futures claims "ES" first;
     forex's 6-char pattern is disjoint from the 1–3 char roots, so no collision.
   • Links:  composed spec → [[spec]] · resolver order → [[resolver]] · session
     reuse → [[forex]] · last feed → [[us_stock]] · contract build → [[broker]]
     · gate consumer → [[risk]] · tick math → [[tick]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
