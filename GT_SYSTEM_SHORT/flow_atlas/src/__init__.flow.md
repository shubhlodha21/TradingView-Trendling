━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 02 ·  src/__init__.py
  the package seal — marks src/ as importable · holds nothing, names everything
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  0 bytes · 0 classes · 0 functions · 0 import edges · 0 importer edges
  An empty file. Its existence — not its contents — is the whole point: it
  tells Python "src is a package," so every dotted path below it resolves.
  The quietest node in the graph; it does no work and is never called.

要 Require ┊ nothing — no imports, no runtime, no side effects
出 Provides┊ the `src` package namespace itself (the dotted-path root)
          ┊ under which every working module is reached:
          ┊   src.config · src.strategy · src.execution · src.feed
          ┊   src.infra · src.assets · src.ledger

─── 部  Modules used ─────────────────────────────────────────────────────
   (none)                         ┊ the file is empty; it imports nothing
                                  ┊ and re-exports nothing — a bare marker

─── 算  Algorithm · what happens when Python touches this file ────────────
 Require: an `import src` (or any `from src.… import …`) somewhere upstream
 Ensure : the name `src` exists as a package object; submodules resolvable.

  1: interpreter resolves `src` on the import path     ▷ finds this __init__.py
  2: finds src/__init__.py present                     ▷ → src is a package,
     │                                                    not a stray directory
  3: executes the file's body                          ▷ body is empty — a no-op;
     │                                                    no code runs, nothing binds
  4: binds module object `src` in sys.modules          ▷ namespace now live
  5: dotted submodule access proceeds                  ▷ src.strategy.engine,
     │                                                    src.execution.broker, … all
     │                                                    resolve through this root
  6: return — control passes on; the seal is set once, reused thereafter.

─── 関  Functions / classes defined ──────────────────────────────────────
   (none)                         ┊ no classes, no functions, no constants

─── 変  Variables / state created ────────────────────────────────────────
   __name__   str   = "src"       ┊ the only binding — set by the import
                                  ┊ machinery, not by any line in the file
   (no module-level globals authored here)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   (none)                         ┊ graph: imports_of → 0 edges (confirmed)

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   (none direct)                  ┊ graph: importers_of → 0 edges (confirmed)
   implicit ←                     ┊ every `from src.… import …` in the tree
                                  ┊ traverses this package root to reach a leaf

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Presence is the contract.  Deleting this file would break every
     `import src.…` in the repo — even though it contains not one byte.
   • Pure namespace.  It declares no public API of its own; the real surface
     lives one level down in the submodules it parents.
   • No re-exports.  Unlike a "flat-facade" __init__, this one stays silent —
     callers must reach modules by their full dotted path.
   • Links:  the engine spine → [[engine]] · orders → [[broker]] ·
     fills → [[fill_ledger]] · ticks → [[handler]] · config → [[models]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
