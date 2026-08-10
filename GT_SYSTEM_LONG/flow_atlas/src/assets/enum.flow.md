━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 06 ·  src/assets/enum.py
  the asset-type discriminator — the one taxonomy every spec hangs from
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  61 lines · 1 class (AssetClass, an Enum) · 1 property · 1 __repr__
  A leaf node — imports nothing of ours, calls nothing. Pure vocabulary.
  Its job is to be the single `if asset.kind == X` the engine is allowed
  to write; everywhere else dispatches through the spec's policies.

要 Require ┊ nothing — stdlib `enum` only; no project deps, no I/O
出 Provides┊ class AssetClass · 10 members (6 live, 4 deferred)
          ┊ AssetClass.is_supported_day1 — the shippability gate
          ┊ name == value (string), so members round-trip through JSON state

─── 部  Modules used ──────────────────────────────────────────────────────
   __future__                    ┊ annotations — postponed evaluation
   enum  (Enum)                  ┊ the base class; members are str-valued

─── 算  Algorithm · the taxonomy and its one gate ─────────────────────────
 Require: nothing.  This file constructs a closed vocabulary at import time.
 Ensure : every symbol the system can trade names exactly one AssetClass,
          and a spec exists for it iff is_supported_day1 is True.

  1: import enum.Enum                         ▷ no project imports — leaf node
  2: class AssetClass(Enum):                  ▷ evaluated once at module import
  3:   bind 10 members, name == value (str)   ▷ uppercase; JSON-round-trippable
     │      live      US_EQUITY · FX_CASH · FUTURE
     │                INDEX_CFD · SHARE_CFD · FX_CFD
     │      deferred  OPTION · OPTION_ON_FUT · CRYPTO · BOND
     │      ▷ deferred kept present so the discriminator stays exhaustive
  4: __repr__(self) → "AssetClass.<NAME>"     ▷ readable in logs / audit lines
  5: ── consumed downstream (not called here) ──
  6:   a spec factory tags itself        spec(asset_class=AssetClass.US_EQUITY, …)
     │      us_stock.py · forex.py · future.py · cfds.py each stamp one member
  7:   a hint guards the factory         if hint is not AssetClass.FX_CASH: reject
     │      ▷ identity compare (`is`/`is not`), never string compare
  8:   SpecRegistry asks is_supported_day1 ▷ rejects symbols whose class
     │                                       has no concrete spec yet (resolver)
  9:   engine / broker / handler / models  ▷ late `from src.assets.enum import
     │      import AssetClass for the lone   AssetClass as _AC` — the only
     │      sanctioned kind-check               `if kind == X` in the engine
 10: return — a frozen vocabulary; mutation happens only by editing this file.

─── 関  Functions / classes defined ───────────────────────────────────────
   class AssetClass(Enum)                       the top-level asset taxonomy
     US_EQUITY      = "US_EQUITY"     ┊ NYSE/NASDAQ stocks via SMART routing
     FX_CASH        = "FX_CASH"       ┊ spot Forex on IDEALPRO
     FUTURE         = "FUTURE"        ┊ CME/CBOT/NYMEX futures contracts
     INDEX_CFD      = "INDEX_CFD"     ┊ IBKR contracts-for-difference (index)
     SHARE_CFD      = "SHARE_CFD"     ┊ CFDs on individual shares
     FX_CFD         = "FX_CFD"        ┊ FX-pair CFDs (routing ≠ spot FX)
     OPTION         = "OPTION"        ┊ equity options (deferred — own sprint)
     OPTION_ON_FUT  = "OPTION_ON_FUT" ┊ options on futures (deferred)
     CRYPTO         = "CRYPTO"        ┊ IBKR crypto via Paxos (deferred)
     BOND           = "BOND"          ┊ fixed income (deferred)
     __repr__(self) → str             ┊ "AssetClass.<NAME>" for logs
     is_supported_day1 (property)→bool┊ True for the 6 live members only

─── 変  Variables / state created ─────────────────────────────────────────
   AssetClass.<NAME>    member       10 singleton instances, str-backed
   (the live set)       frozenset    inlined inside is_supported_day1 —
                                      the 6 shippable members, Day-1 deliverable
   ▷ no module-level mutable state; no instance state beyond the Enum members

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   (none of ours) — depends only on stdlib enum.Enum.  A true leaf.

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   assets.__init__            ▷ re-exports AssetClass at the package surface
   assets.spec                ▷ AssetSpec carries an asset_class field
   assets.resolver            ▷ SpecRegistry — is_supported_day1 shippability gate
   assets.us_stock            ▷ tags US_EQUITY · rejects mismatched hint
   assets.forex               ▷ tags FX_CASH · hint guard
   assets.future              ▷ tags FUTURE · hint guard
   assets.cfds                ▷ tags INDEX_CFD / SHARE_CFD / FX_CFD · hint guards
   config.models              ▷ late import `as _AC` for kind-aware config
   execution.broker           ▷ late import `as _AssetClass` for routing
   feed.handler               ▷ late import `as _AC` for feed-type dispatch
   strategy.engine            ▷ late import `as _AC` — the one sanctioned `if kind`

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Single discriminator.  This enum is the only place the engine is allowed
     to branch on asset type; all other behavior dispatches through the spec's
     policies via composition (see the file's module docstring).
   • name == value.  Members are string-valued and uppercase so a TradeState
     JSON snapshot can serialize / restore an AssetClass losslessly.
   • Identity, not equality.  Downstream guards use `is` / `is not` against
     members, never `==` on the string — enum singletons make this safe.
   • Exhaustive on purpose.  OPTION · OPTION_ON_FUT · CRYPTO · BOND are present
     but deferred; is_supported_day1 returns False for them so SpecRegistry
     refuses such symbols rather than dispatching to a missing spec.
   • Onboarding path (3 steps, zero engine edits): add a member here → add a
     concrete spec under src/assets/<name>.py → register it in [[resolver]].
   • Leaf node.  No graph node exists for this file (no calls/imports of ours);
     edges above were recovered by grep on the import + dispatch sites.
   • Links:  carried by → [[spec]] · gated by → [[resolver]] · branched on by →
     [[engine]] · stamped by → [[us_stock]] [[forex]] [[future]] [[cfds]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
