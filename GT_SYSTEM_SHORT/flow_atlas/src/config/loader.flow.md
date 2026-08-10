━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 24 ·  src/config/loader.py
  the single entry-point that turns the environment into a Config
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  8 lines · 1 function (load) · 0 classes · the thinnest node in the graph.
  A one-line delegate: it owns no logic of its own — it simply hands the
  reading of the world (env vars) to Config.from_env and returns the result.
  All the real parsing, defaulting, and validation lives in [[models]].

要 Require ┊ the process environment (os.environ) already populated —
          ┊ GT_* variables set by the shell / systemd / docker before launch
          ┊ config.models.Config with a working .from_env() classmethod
出 Provides┊ load() -> Config   — one fully-built, validated Config object
          ┊ the canonical "how the system reads its settings" seam

─── 部  Modules used ─────────────────────────────────────────────────────
   os                            ┊ imported for environment access (the source
                                 ┊ of truth) — note: read indirectly, see 注
   config.models  (Config)       ┊ Config.from_env() — the actual parser /
                                 ┊ defaulter / validator of every GT_* knob

─── 算  Algorithm · read the world, build the Config ─────────────────────
 Require: the environment is set (GT_* vars) before the process starts
 Ensure : returns one Config — or propagates from_env's error if a
          required var is missing / malformed (no silent defaults here)

  1: load()                                  ▷ no arguments — the env IS the input
  2:   return Config.from_env()              ▷ delegate everything to [[models]]
     │      Config.from_env() reads os.environ▷ parses GT_* knobs, applies
     │                                          defaults, validates, constructs
     │      ▷ any raise inside from_env bubbles straight out of load()
  3: ── caller receives the Config ──        ▷ passed down to Engine, Gateway,
     │                                          RiskGate, AssetSpec at boot
  4: return — a single immutable settings object for the whole run.

─── 関  Functions / classes defined ──────────────────────────────────────
   load() -> Config        module-level free function · the only definition.
                           Docstring: "Load config from environment variables."
                           Pure delegation — body is one return statement.

─── 変  Variables / state created ────────────────────────────────────────
   (none)               no module-level state · no globals · no caching.
                        Each call re-reads the environment via from_env, so
                        load() is a fresh snapshot every time it is invoked.

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   Config.from_env()       ▷ config.models — the one and only call made.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   Engine._load_state      ▷ src/strategy/engine.py:665 — pulls a fresh Config
                             during state restore at boot (graph CALLS edge).
   config/__init__.py      ▷ re-exports loader at package level (imports edge),
                             so callers may `from src.config import load`.

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Thin by design.  This file is a seam, not a worker. Keeping it a one-line
     delegate means there is exactly one place the system reads its settings,
     and exactly one place (from_env) that knows how to parse them.
   • os is imported but not used directly here — the actual os.environ reads
     happen inside Config.from_env. The import is vestigial / forward-looking;
     harmless, but the live dependency on the environment is via [[models]].
   • No defaults, no caching, no validation in this file. Do not add them here —
     they belong in Config.from_env so every loader stays trivially correct.
   • Fresh each call.  load() is not memoized; a second call re-snapshots the
     environment. Callers that want a single shared Config call once at boot.
   • Links:  parsing + defaults + validation → [[models]] · consumed at
     restore by → [[engine]] · persisted run-state (separate concern) → [[persistence]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
