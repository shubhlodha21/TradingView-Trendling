━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 44 ·  src/infra/__init__.py
  the infrastructure façade — one import line for logging + alerting
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  43 lines · 0 classes · 0 functions · a pure re-export package __init__.
  It defines nothing of its own; it gathers the production-engineering
  surface — structured logging + the alert pipeline — behind one name so
  callers can write `from src.infra import …` instead of reaching into
  each submodule. The docstring also carries the design philosophy
  (defense in depth · event-based monitoring · symptom-based alerting).

要 Require ┊ src.strategy.logging  and  src.infra.alerts  must import cleanly
          ┊ (this file runs their top-level code at first `import src.infra`)
出 Provides┊ a flat namespace of 11 names, all listed in __all__:
          ┊   QuantLogger · LogLevel
          ┊   AlertManager · Alert · AlertSeverity · AlertChannel
          ┊   SlackChannel · PrintChannel · FileChannel
          ┊   AnomalyDetector · build_default_alert_manager

─── 部  Modules used ─────────────────────────────────────────────────────
   src.strategy.logging          ┊ QuantLogger · LogLevel  (the structured logger)
   src.infra.alerts              ┊ AlertManager · Alert · AlertSeverity
                                 ┊ AlertChannel + 3 channels (Slack/Print/File)
                                 ┊ AnomalyDetector · build_default_alert_manager

─── 算  Algorithm · what runs at import-time ─────────────────────────────
 Require: the two source modules above are importable.
 Ensure : after `import src.infra`, all 11 names resolve and __all__
          fixes the public surface (controls `from src.infra import *`).

  1: import src.infra                           ▷ first touch triggers the body
  2:   from src.strategy.logging import …       ▷ pull QuantLogger, LogLevel
     │      (executes logging.py top-level once → see 流 logging)
  3:   from src.infra.alerts import …           ▷ pull the 9 alert names
     │      (executes alerts.py top-level once → see 流 alerts)
  4:   __all__ = [ … 11 names … ]               ▷ declare the public surface;
     │                                             names not listed stay private
  5: ── done ──  module object cached in sys.modules; later imports are free.
  6: caller: build_default_alert_manager()      ▷ typical first real use —
     │                                             assembles AlertManager with
     │                                             default channels (→ 流 alerts)

─── 関  Functions / classes defined ──────────────────────────────────────
   (none)  ┊ this file declares no class and no function of its own.
           ┊ every name it exposes is re-exported from a submodule;
           ┊ see 流 logging and 流 alerts for the real definitions.

─── 変  Variables / state created ────────────────────────────────────────
   __all__              list[str]    the 11 public names; bounds `import *`
   QuantLogger          alias        ← src.strategy.logging   (class)
   LogLevel             alias        ← src.strategy.logging   (enum)
   AlertManager         alias        ← src.infra.alerts       (class)
   Alert                alias        ← src.infra.alerts       (dataclass)
   AlertSeverity        alias        ← src.infra.alerts       (enum)
   AlertChannel         alias        ← src.infra.alerts       (base / proto)
   SlackChannel         alias        ← src.infra.alerts       (channel)
   PrintChannel         alias        ← src.infra.alerts       (channel)
   FileChannel          alias        ← src.infra.alerts       (channel)
   AnomalyDetector      alias        ← src.infra.alerts       ("Trade Too Good")
   build_default_alert_manager alias ← src.infra.alerts       (factory)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   IMPORTS_FROM  src/strategy/logging.py     ▷ graph edge 10581 (line 17)
   IMPORTS_FROM  src/infra/alerts.py         ▷ graph edge 10582 (line 18)
   (no function calls — only import-time re-binding.)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   (graph: importers_of → 0 edges.)
   ▷ consumers import the submodules directly — `from src.infra.alerts
     import AlertManager` and `from src.strategy.logging import QuantLogger` —
     rather than through this façade, so the package __init__ has no
     recorded importers. It stands as the documented entry point /
     philosophy banner, not a load-bearing hop.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pure aggregation.  No logic, no state machine, no side effects beyond
     running the two submodules' top-level code at first import.
   • __all__ is the contract.  Keep it in lock-step with the import block;
     a name dropped from one but not the other silently breaks `import *`
     or leaves a stale public alias.
   • Note the reach across packages — logging lives under src.strategy,
     not src.infra, yet the docstring frames it as infrastructure; the
     façade papers over that split so callers see one cohesive surface.
   • Philosophy (docstring): every order critical · enumerate every edge
     case · alert on symptoms not causes · "Trade Too Good" detection.
   • Links:  logger → [[logging]] · alerts + channels + AnomalyDetector
     + build_default_alert_manager → [[alerts]] · consumer → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
