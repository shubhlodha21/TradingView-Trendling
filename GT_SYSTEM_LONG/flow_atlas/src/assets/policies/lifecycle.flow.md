━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 16 ·  src/assets/policies/lifecycle.py
  the lifecycle contract — does this position roll · expire · settle T+N · finance?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  94 lines · 1 Protocol (LifecyclePolicy) · 4 methods · zero runtime imports.
  A pure interface — it defines what every asset class must answer about a
  position's life OUTSIDE the BUY/SELL state machine. The concrete bodies live
  in each asset module (NoLifecycle, CFDLifecycle, FutureLifecycle); this file
  is only the shape they promise to fill.

要 Require ┊ nothing at runtime — typing.Protocol + datetime only
          ┊ ib_async.Contract referenced under TYPE_CHECKING (no import cost)
出 Provides┊ class LifecyclePolicy(Protocol) — the lifecycle metadata contract
          ┊ four queries: needs_roll · expiry · settlement_days · has_overnight_financing
          ┊ @runtime_checkable — isinstance(spec.lifecycle, LifecyclePolicy) works

─── 部  Modules used ─────────────────────────────────────────────────────
   __future__.annotations        ┊ deferred (string) annotations everywhere
   datetime                      ┊ date · datetime — expiry return + ts arg
   typing                        ┊ Optional · Protocol · runtime_checkable
                                 ┊ TYPE_CHECKING — guard the Contract forward-ref
   ib_async.Contract  (typing)   ┊ structural type only; never imported at runtime

─── 算  Algorithm · the four questions a position answers ─────────────────
 Require: an AssetSpec carries one .lifecycle conforming to this Protocol.
 Ensure : a non-rolling, non-expiring asset answers safely (False / None / T+N)
          without any contract math; futures alone carry real roll/expiry logic.

  1: AssetSpec built (→ 流 us_stock · forex · cfds · future)
     │      spec.lifecycle ← NoLifecycle | CFDLifecycle | FutureLifecycle
     │      ▷ each concrete class structurally satisfies this Protocol
  2: ── health / session tick asks: "is this contract about to roll?" ──
  3:   spec.lifecycle.needs_roll(contract, ts) -> bool
     │      non-rolling assets (equity · FX cash · most CFDs) → always False
     │      futures → True when ts within roll_window_days of expiry
     │      ▷ Day-1: engine only SURFACES a ROLL_NEEDED alert — operator rolls
     │      ▷ auto-roll trade is parked work (week 2)
  4: ── SpecRegistry validation asks: "when does this expire?" ──
  5:   spec.lifecycle.expiry(contract) -> Optional[date]
     │      non-expiring → None ; futures/options → the contract's expiry date
     │      ▷ used to REFUSE starting an engine on an already-expired contract
     │      ▷ live call site: future.py:529  expiry = intent.spec.lifecycle.expiry(contract)
  6: ── risk gate asks: "how many settlement days for capital sizing?" ──
  7:   spec.lifecycle.settlement_days() -> int
     │      US equity → 1 (T+1 since May 2024) ; FX cash → 2 (T+2)
     │      futures → 0 (intraday mark) ; CFDs → 0 (continuous mark)
     │      ▷ feeds the available-capital calculation in the risk gate
  8: ── scheduler asks: "does overnight cost money?" ──
  9:   spec.lifecycle.has_overnight_financing() -> bool
     │      CFDs → True (daily financing on notional × quote-ccy rate)
     │      everything else → False
     │      ▷ flag only in Day-1; the accrual job itself is future work
 10: return — four cheap answers, no side effects, no orders, no broker calls.

─── 関  Functions / classes defined ──────────────────────────────────────
   class LifecyclePolicy(Protocol)   @runtime_checkable — the lifecycle contract
     needs_roll(contract, ts) -> bool        roll-window check (futures only)
     expiry(contract) -> Optional[date]      expiry date or None if perpetual
     settlement_days() -> int                T+N convention for capital sizing
     has_overnight_financing() -> bool       daily financing flag (CFDs True)

─── 変  Variables / state created ────────────────────────────────────────
   (none)               this file holds no module-level state and no instance
                        fields — Protocol method bodies are `...` stubs only.
                        Concrete state (settlement_days_, roll_window_days) lives
                        in the implementing dataclasses, not here.

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   (none at runtime)    Protocol bodies are ellipsis; nothing is called.
                        Only datetime/typing names are referenced as types.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   assets.policies.__init__   ▷ re-exports `from .lifecycle import LifecyclePolicy`
   assets.spec                ▷ AssetSpec.lifecycle: LifecyclePolicy (the field type)
   assets.us_stock            ▷ NoLifecycle (settlement_days_=1, no roll/expiry)
   assets.forex               ▷ NoLifecycle(settlement_days_=2) — FX cash T+2
   assets.cfds                ▷ CFDLifecycle — has_overnight_financing=True
   assets.future              ▷ FutureLifecycle — roll-window + real expiry()
   (graph note: no node yet for this file — edges grepped from src, not invented)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Interface only.  This file declares no behavior — every method is `...`.
     The four answers are honored by the concrete classes in each asset module.
   • Safe defaults.  Non-rolling / non-expiring assets MUST return False / None,
     never raise — so equity & FX flow through the lifecycle questions untouched.
   • Day-1 is passive.  needs_roll / has_overnight_financing only SURFACE state
     (alert · flag); no roll trade and no financing accrual happens automatically.
   • Catastrophe guard.  expiry() exists so SpecRegistry can refuse to start an
     engine on an expired future — failure to roll → forced settlement / delivery.
   • runtime_checkable lets duck-typed specs be isinstance-verified at wiring time.
   • Links:  shapes → [[spec]] · equity/FX impl → [[us_stock]] · [[forex]]
     financing → [[cfds]] · roll+expiry → [[future]] · alerts → [[alerts]]
     capital sizing → [[risk]] · validation refusal → [[future]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
