━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 19 ·  src/assets/policies/session.py
  the trading-clock contract — "is this asset tradable RIGHT NOW?"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  116 lines · 1 frozen dataclass (SessionWindow) · 1 Protocol (SessionPolicy)
  · 0 concrete logic — pure contract. The 156 RTH/market-hours assumptions
  buried in engine.py + health_check_loop all assumed US equity; this file
  is the abstraction that lets every asset carry its own clock.

要 Require ┊ stdlib only — dataclasses · datetime · typing · zoneinfo (impl)
          ┊ all timestamps must be UTC, timezone-aware (naive = refused)
出 Provides┊ SessionWindow — one contiguous, validated, UTC trading window
          ┊ SessionPolicy — the per-asset-class session-awareness Protocol
          ┊ (re-exported through assets.policies.__init__ → 流 policies)

─── 部  Modules used ─────────────────────────────────────────────────────
   __future__                    ┊ annotations (lazy hints; list[…] on 3.9)
   dataclasses                   ┊ @dataclass(frozen=True, slots=True)
   datetime                      ┊ datetime — the one type every method speaks
   typing                        ┊ Optional · Protocol · runtime_checkable
   ┊                             ┊ NO runtime deps · NO imports of project code
   ┊                             ┊ (concrete clocks live in us_stock / forex …)

─── 算  Algorithm · the contract, not the computation ─────────────────────
 Require: every datetime that crosses this boundary is UTC & tz-aware.
 Ensure : a caller can ask one asset "open? next open? near close?" without
          knowing whether it is NYSE, FX, CME, LSE or TSE.

  1: SessionWindow(open_utc, close_utc)        ▷ frozen + slots value object
  2:   __post_init__()                         ▷ the two guards, at birth:
     │      if open_utc.tzinfo is None
     │         or close_utc.tzinfo is None → ValueError  ▷ refuse naive ts
     │                                          (silent off-by-hour bug source)
     │      if close_utc <= open_utc      → ValueError  ▷ refuse zero/inverted
  3:   w.contains(ts)                          ▷ open_utc <= ts < close_utc
     │                                            (inclusive open, exclusive close)
  4:   w.duration_seconds()                    ▷ int((close − open).total_seconds())
  5: ── the Protocol surface (structural; no body) ──
     │  SessionPolicy is @runtime_checkable → isinstance() works on duck-typed
     │  concrete specs (us_stock.USEquitySession, forex.ForexSession, …)
  6:   is_open_at(ts) -> bool                  ▷ ts inside ANY of today's windows?
     │      ▷ the hot path — engine tick-processing gate
  7:   windows_for_date(ts) -> [SessionWindow] ▷ all windows overlapping ts's
     │      local-tz calendar date · 0 on weekend/holiday · 1 equity · 2 TSE
     │      ▷ this is the primitive; 6/8/9/10/11 all derive from it
  8:   next_open(ts) -> datetime               ▷ next open AFTER ts; if already
     │                                            in session → the NEXT one, not now
  9:   next_close(ts) -> datetime              ▷ next close after ts; if in
     │                                            session → THIS session's close
 10:   is_within_n_minutes_of_close(ts, n)     ▷ don't-enter-near-close gate;
     │                                            False if not in a session
 11:   time_to_close(ts) -> Optional[int]      ▷ seconds left, or None when flat
     │                                            of any session  ▷ EOD logic
 12: return — no state mutated; callers compose these into entry/exit gates.

─── 関  Functions / classes defined ──────────────────────────────────────
   class SessionWindow            @dataclass(frozen=True, slots=True) value obj
     open_utc · close_utc         the two UTC datetimes (fields)
     __post_init__                tz-aware + ordering guards at construction
     contains(ts)                 inclusive-open / exclusive-close membership
     duration_seconds()           window length in whole seconds
   class SessionPolicy            @runtime_checkable Protocol — the contract
     is_open_at(ts)               market open at ts?  (hot path)
     windows_for_date(ts)         windows overlapping ts's local date (primitive)
     next_open(ts)                next session open after ts
     next_close(ts)               next session close after ts
     is_within_n_minutes_of_close near-close risk gate
     time_to_close(ts)            seconds to current close, or None

─── 変  Variables / state created ────────────────────────────────────────
   open_utc             datetime   SessionWindow field · UTC tz-aware (frozen)
   close_utc            datetime   SessionWindow field · UTC tz-aware (frozen)
   ┊                              no module-level state · no caches here
   ┊                              (concrete impls may cache holiday calendars)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   stdlib only — datetime.tzinfo · (close − open).total_seconds() · ValueError
   no project-internal calls · this file sits at the bottom of the import DAG

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   assets.policies.__init__   ▷ re-exports SessionPolicy · SessionWindow → 流 policies
   assets.spec                ▷ AssetSpec.session: SessionPolicy  (the slot)
   assets.us_stock            ▷ USEquitySession implements it · builds SessionWindow
   assets.forex               ▷ ForexSession implements it · weekly-halt window
   ┊ (graph node absent for this file; edges verified by grep of import sites)
   ┊ engine / health_check     ▷ indirectly, through spec.session.* (→ 流 engine)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • UTC or nothing.  SessionWindow refuses naive datetimes at construction —
     the whole point is to kill silent off-by-hour bugs before they ship.
   • Half-open windows.  contains() is [open, close): a tick exactly at close
     is already OUT.  next_close while in-session returns THIS close.
   • windows_for_date is the primitive.  is_open_at / next_* / time_to_close
     are all derivable from it; an implementer that gets that one right is done.
   • Stateless by intent.  Implementations are pure over ts; any holiday-calendar
     cache is a private speed detail, never observable state.
   • Multi-window days are real.  TSE returns 2 windows (morning + afternoon);
     callers must never assume a single window per date.
   • Asset clocks differ: equity 09:30-16:00 ET M-F (NYSE cal) · FX continuous
     Sun 22:00 → Fri 22:00 UTC · CME 23h w/ 17:00-18:00 ET halt · LSE · TSE.
   • Links:  the slot → [[spec]] · re-export → [[policies]] · concrete equity
     clock → [[us_stock]] · FX weekly halt → [[forex]] · consumers → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
