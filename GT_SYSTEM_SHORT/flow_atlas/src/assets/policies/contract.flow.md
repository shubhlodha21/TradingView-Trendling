━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 15 ·  src/assets/policies/contract.py
  the contract contract — one Protocol that says "build me · qualify me"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  84 lines · 1 Protocol (ContractPolicy) · 3 exception classes · 0 runtime imports.
  A pure interface page. The 6 hardcoded Stock("…","SMART","USD") sites that
  used to litter broker.py / handler.py all collapse to this one shape: each
  asset class supplies a make() + qualify() pair behind this Protocol.

要 Require ┊ nothing at import time — ib_async types are TYPE_CHECKING only,
          ┊ so policies stay unit-testable with no IBKR present
          ┊ at call time: qualify() needs a connected IB handle
出 Provides┊ Protocol ContractPolicy (runtime-checkable) · make / qualify shape
          ┊ exception ladder ContractError → ContractNotFound · ContractAmbiguous

─── 部  Modules used ──────────────────────────────────────────────────────
   __future__                    ┊ annotations — deferred string type eval
   typing                        ┊ TYPE_CHECKING · Protocol · runtime_checkable
   ib_async  (TYPE_CHECKING only)┊ Contract · IB — for signatures, never loaded
                                 ┊ ▷ deliberate: no import-time dependency on IBKR

─── 算  Algorithm · the two-method contract ───────────────────────────────
 Require: a human symbol str; for qualify(), a connected IB
 Ensure : make() is pure & deterministic · qualify() returns an order-ready
          Contract or raises a typed ContractError

  1: caller picks an AssetSpec.contract             ▷ a ContractPolicy impl
     │                                                 (us_stock · forex · future · cfds)
  2: raw = policy.make(symbol)                       ▷ STEP 1 — pure, no network
     │      make("PLTR")   → Stock("PLTR","SMART","USD")
     │      make("EURUSD") → Forex("EURUSD")        ▷ auto-routes to IDEALPRO
     │      make("ES")     → Future("ES", month="202503", "CME", mult="50")
     │      ▷ deterministic — fails only on bad input, never on the wire
  3: con = await policy.qualify(ib, raw)             ▷ STEP 2 — the network call
     │      round-trips raw against IBKR ContractDetails
  4:   on success → Contract with conId · localSymbol · tradingClass ·
     │              primaryExchange populated                   ▷ THE order object
  5:   on zero matches   → raise ContractNotFound   ▷ bad symbol / exchange / entitlement
  6:   on >1 match       → raise ContractAmbiguous  ▷ futures w/ many expiries; narrow by month
  7:   on socket trouble → propagate ib_async network error    ▷ not wrapped
  8: caller hands con to the engine / broker for orders         ▷ → 流 broker
     │      ▷ two methods, two failure modes — that is the whole design:
     │        make() can't touch the network, qualify() is the only place it can fail

─── 関  Functions / classes defined ───────────────────────────────────────
   class ContractPolicy(Protocol)   @runtime_checkable — the asset-class interface
     make(symbol) -> Contract       pure constructor; deterministic; no I/O
     async qualify(ib, contract)    network round-trip → populated Contract or raise
   class ContractError(Exception)   base for every contract-build failure
   class ContractNotFound(…Error)   IBKR returned zero matches
   class ContractAmbiguous(…Error)  IBKR returned >1 match, policy didn't disambiguate

─── 変  Variables / state created ─────────────────────────────────────────
   (none)                           stateless module — pure interface + exceptions.
                                    No module-level constants, no singletons.

─── 呼  Calls-out  → ──────────────────────────────────────────────────────
   (none at runtime)                only TYPE_CHECKING references to ib_async.
                                    Implementations call ib.reqContractDetailsAsync;
                                    the Protocol itself calls nothing.

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   assets.policies.__init__      ▷ re-exports ContractPolicy (the package face)
   assets.spec   (AssetSpec)     ▷ field  contract: ContractPolicy
   assets.us_stock               ▷ implements; raises ContractNotFound (us_stock.py:166)
   assets.forex                  ▷ implements; raises ContractNotFound (forex.py:158)
   assets.future                 ▷ FuturesContractPolicy; raises NotFound + Ambiguous
   assets.cfds                   ▷ implements; raises ContractNotFound (cfds.py:134)
   assets.resolver               ▷ catches ContractNotFound when a symbol won't qualify
   execution.broker              ▷ imports FuturesContractPolicy (broker.py:73)
   ▷ graph had no node for this file — edges above are grep-verified call sites

─── 注  Notes · invariants ────────────────────────────────────────────────
   • Zero import-time cost.  ib_async lives under TYPE_CHECKING only, so a policy
     can be constructed and make()-tested with no broker, no socket, no IBKR.
   • Split by failure mode.  make() is pure (bad-input only) · qualify() is the
     sole network seam (no-match / ambiguous / down). Keep that line clean.
   • Structural typing.  @runtime_checkable Protocol — impls need not inherit;
     they just need make + qualify. AssetSpec.contract holds one.
   • Exception ladder.  Catch ContractError to mean "any build failure";
     catch the leaves to act (Ambiguous → narrow the month; NotFound → give up).
   • Links:  the contract object flows to → [[broker]] for orders · selected by
     [[spec]] (AssetSpec) · resolved + caught in [[resolver]] · siblings in this
     package: [[lifecycle]] · package face: [[__init__]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
