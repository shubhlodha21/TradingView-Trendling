━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 48 ·  src/strategy/__init__.py
  the package door — one symbol re-exported, the engine made reachable
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  5 lines · 0 classes · 0 functions · a pure namespace shim.
  It defines nothing of its own; it only lifts Engine up one level so the
  rest of the system can write  from src.strategy import Engine  instead of
  reaching into the 8210-line engine module by path.

要 Require ┊ src/strategy/engine.py must import cleanly (its own deps cascade:
          ┊ config · execution · feed · infra · assets · strategy.risk)
出 Provides┊ name  Engine  at package scope  ·  __all__ = ["Engine"]
          ┊ the public surface of the whole strategy package

─── 部  Modules used ─────────────────────────────────────────────────────
   src.strategy.engine            ┊ from … import Engine   (the one edge)
                                  ┊ everything else this package can do lives
                                  ┊ behind that single name → see 流 engine

─── 算  Algorithm · what happens at import ────────────────────────────────
 Require: the interpreter is importing the package  src.strategy
 Ensure : the name  Engine  is bound at package scope, nothing else leaks

  1: import src.strategy                       ▷ Python executes this file once
  2:   from src.strategy.engine import Engine  ▷ triggers full load of 流 engine
     │                                            (and its transitive deps) now,
     │                                            not lazily — import cost is paid here
  3:   __all__ = ["Engine"]                    ▷ declares the star-export surface;
     │                                            `from src.strategy import *` yields
     │                                            only Engine, nothing private
  4: return — the package object now carries  .Engine
     │        consumers bind it and construct one bot per (symbol, client_id)

─── 関  Functions / classes defined ──────────────────────────────────────
   (none)                         ┊ no class, no function, no logic — re-export only
                                  ┊ the symbol it exposes, Engine, is defined in
                                  ┊ src/strategy/engine.py  → 流 engine

─── 変  Variables / state created ────────────────────────────────────────
   Engine               class      re-bound here from .engine (the trading
                                   state-machine; not instantiated at import)
   __all__              list[str]  ["Engine"] — public export allow-list

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   src.strategy.engine            ▷ IMPORTS_FROM (graph edge, confidence 1.0)
                                  ▷ the only outbound edge this file has

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   run_live.py            ▷ constructs one Engine per (symbol, client_id)
   dashboard.py · dashboard_agg.py        ▷ read / render engine state
   webapp/backend/audit_reader.py         ▷ audit surface
   test_live.py · test_paper_simulation.py
   tests/test_rs1_resume_from_stopped.py · tests/test_fx_pnl_usd.py
   tests/test_fl9_floor_selection.py · tests/test_fill_ledger_fl8_selfheal.py
   tests/test_fill_ledger_engine_integration.py
   tests/harness/* · tests/assets/* · tests/unit/*
   ▷ all reach Engine through this door (graph importers_of = 0 because the
     edge resolves to the package, not the file; call sites verified by grep)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Façade only.  Adding behaviour here would hide it from readers who expect
     a __init__ to be a re-export. Keep it a one-line door; logic lives in engine.
   • Eager import.  Step 2 loads the entire engine graph at package import time.
     A broken import anywhere downstream surfaces the instant src.strategy loads.
   • Single name.  __all__ pins the contract to exactly Engine — widen it only
     when a new public type is genuinely meant to be package-level.
   • Links:  the engine itself → [[engine]] · orders → [[broker]] · ticks →
     [[handler]] · P&L → [[risk]] · durable fills → [[fill_ledger]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
