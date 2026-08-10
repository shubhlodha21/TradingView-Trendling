━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 22 ·  src/config/__init__.py
  the config package façade — one door, three rooms (models · persistence · loader)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  32 lines · 0 classes · 0 functions defined here · pure re-export surface.
  "Env vars only, no YAML, no heavy libs." (file docstring) — the whole config
  layer is namespaced behind this single import so the rest of the system writes
  `from src.config import Config, load` and never reaches into submodules.

要 Require ┊ the three sibling modules must import cleanly:
          ┊ config.models · config.persistence · config.loader
出 Provides┊ one flat namespace — 12 names in __all__:
          ┊ Config · ConnectionStatus · OrderType · OrderSide · OrderStatus
          ┊ TradeState · Order · Position · TradeContext   (← from models)
          ┊ StateStore · AuditLog                           (← from persistence)
          ┊ load                                            (← from loader)

─── 部  Modules used ─────────────────────────────────────────────────────
   config.models        ┊ dataclasses + enums — Config, the TradeState machine,
                        ┊ Order/Position/TradeContext, the OrderType/Side/Status
                        ┊ enums, ConnectionStatus
   config.persistence   ┊ StateStore (.gt_state JSON snapshots) · AuditLog (CSV)
   config.loader        ┊ load() — reads env vars → builds a Config

─── 算  Algorithm · what import-time does ─────────────────────────────────
 Require: the three submodules are importable
 Ensure : `src.config` exposes a single flat, stable public surface

  1: import src.config                        ▷ Python runs this __init__ once
  2:   from config.models import (…)          ▷ pull 9 type/enum names into ns
     │      Config · ConnectionStatus · OrderType · OrderSide · OrderStatus
     │      TradeState · Order · Position · TradeContext
  3:   from config.persistence import          ▷ pull 2 storage names into ns
     │      StateStore, AuditLog
  4:   from config.loader import load          ▷ pull the env→Config builder
  5:   __all__ ← [the 12 names]                ▷ declares the public API;
     │                                            bounds `from src.config import *`
  6: return — namespace is now flat            ▷ callers never touch submodules
     │                                            directly (e.g. run_live, engine,
     │                                            broker all import from here)

─── 関  Functions / classes defined ──────────────────────────────────────
   (none defined locally — this file only re-exports)
   re-exported ┊ Config · ConnectionStatus · OrderType · OrderSide
               ┊ OrderStatus · TradeState · Order · Position · TradeContext
               ┊ StateStore · AuditLog · load

─── 変  Variables / state created ────────────────────────────────────────
   __all__   list[str]   the 12 public names — the package's contract
   (no module-level mutable state; import is side-effect-free beyond binding)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   IMPORTS_FROM  config.models        (line 4)
   IMPORTS_FROM  config.persistence   (line 15)
   IMPORTS_FROM  config.loader        (line 16)
   (no function calls — re-export only)

─── 被  Called-by  ← ──────────────────────────────────────────────────────
   live_trading.py   (line 13)   ▷ graph edge — top-level entry imports here
   test_live.py      (line 9)    ▷ graph edge — test harness imports here
   ▷ in practice the whole tree reads through this door (engine, broker, risk,
     run_live import `from src.config import …`); the graph records the two
     direct façade edges above — other modules resolve the names transitively.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Façade, not logic.  Zero behaviour lives here; bugs in config belong to
     [[models]], [[persistence]], or [[loader]] — never this file.
   • Single door.  Callers MUST import from `src.config`, not its submodules,
     so the internal split (models/persistence/loader) can change freely.
   • __all__ is the contract.  Adding a public name means editing both the
     import block AND __all__ — keep them in lockstep or `import *` drifts.
   • Env-only config.  Docstring pins the policy: env vars, no YAML, no heavy
     libs — the actual env reading happens in [[loader]] via load().
   • Links:  types/enums → [[models]] · snapshots+CSV → [[persistence]]
     env→Config → [[loader]] · consumed by → [[engine]] · [[broker]] · [[risk]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
