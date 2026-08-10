━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 46 ·  src/ledger/__init__.py
  the package threshold — names the ledger-graph namespace, carries no code
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1 line · 0 classes · 0 functions · a pure package marker.
  A docstring and nothing else.  It declares what the `ledger` package IS —
  "aggregation + graph views over the durable per-bot fill ledgers" — and
  then steps aside.  All the work lives in the sibling module [[graph]].

要 Require ┊ nothing — imported for its side-effect of existing (package init)
出 Provides┊ the `ledger` namespace · the package-level docstring (its charter)
          ┊ no re-exports — callers reach in for `ledger.graph` explicitly

─── 部  Modules used ─────────────────────────────────────────────────────
   (none)                         ┊ no imports · no `from … import` · no __all__
                                  ┊ the file body is exactly one docstring line

─── 算  Algorithm · what running this file does ──────────────────────────
 Require: the Python import machinery touches `src/ledger/`
 Ensure : the name `ledger` resolves to a package; `ledger.graph` is reachable.

  1: import ledger                            ▷ Python locates src/ledger/__init__.py
  2:   bind module docstring → ledger.__doc__ ▷ "aggregation + graph views …"
     │                                           the package's stated charter
  3:   (no code executes)                     ▷ no imports, no globals, no work
  4: import ledger.graph                       ▷ the real surface — loaded on demand
     │                                           by the SSE server / monitor, never
     │                                           pulled in transitively from here
  5: return — the namespace exists; control passes to [[graph]].

─── 関  Functions / classes defined ──────────────────────────────────────
   (none)                         ┊ the module defines no symbols of its own

─── 変  Variables / state created ────────────────────────────────────────
   __doc__              str          the package charter line (only binding here)
   __name__ / __path__  (implicit)   package identity supplied by the import system

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   (none)                         ▷ graph file_summary / imports_of returned 0 edges
                                    — confirmed: this file calls nothing, imports
                                    nothing.  The package's outbound edges all
                                    originate in [[graph]], not here.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   (any `import ledger.graph` site)  ▷ the SSE ledger-graph server + monitor front
                                       end (LV1–LV10) reach `ledger.graph` through
                                       this package; they bind the namespace this
                                       file declares, not the docstring itself.
   ▷ graph importers_of returned 0 — no module imports the bare package for a
     symbol; the marker is consumed implicitly by sub-module imports only.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Empty by design.  Storage stays the append-only JSONL the engine writes
     (FL1–FL8); this package deliberately adds NO source of truth.  Keeping
     __init__ bare avoids import-time work and circular pulls into [[graph]].
   • No re-exports.  Callers must say `ledger.graph` explicitly — the package
     does not hoist names, so the dependency on the heavy aggregator is opt-in.
   • One charter, two readers.  The docstring is the human contract; the real
     projection (currencies→nodes, pairs→edges, clients→owners) lives next door.
   • Links:  the whole package →  [[graph]] · fill files written by [[fill_ledger]]
     · projected positions reconciled in [[engine]] · P&L scaling → [[risk]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
