━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  流 47 ·  src/ledger/graph.py
  the currency graph — a pure, file-based projection of the flat fill ledgers
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  400 lines · 0 classes · 5 functions · a COMPUTED read-model. No broker, no
  client-id slot, no socket. It reads the durable per-bot fill JSONL the engine
  writes (FL1–FL8) and projects them into the object the trader reasons about:
      currencies → NODES (net cash = A·x̂)   pairs → EDGES   clients → owners.
  A fill on AUD.JPY lights the AUD↔JPY edge and ripples into both nodes.

要 Require ┊ a directory of `.gt_fills_<SYM>_<PORT>_<CID>.jsonl` files on disk
          ┊ nothing else — the clock (`ts`) is passed in, never read here
出 Provides┊ build_ledger_graph() → the nested graph dict (nodes/edges/cycles)
          ┊ reconcile_cash()      → per-(account,ccy) cash reconciliation
          ┊ pure + deterministic — same files in, same graph out

─── 部  Modules used ──────────────────────────────────────────────────────
   glob                          ┊ enumerate `.gt_fills_*.jsonl` under data_dir
   json                          ┊ parse one fill record per JSONL line
   os                            ┊ os.path.join / basename for the ledger glob
   re                            ┊ _FILE_RE — pull sym · port · cid from filename
   collections (local import)    ┊ deque — BFS over the spanning forest (_cycle_basis)
   ── reads files written by ──   ┊ execution.fill_ledger (FL1) · engine (FL2)

─── 算  Algorithm · ledger files → currency graph ─────────────────────────
 Require: a data_dir of append-only fill JSONL; optional universe set; ts; recent_n
 Ensure : node/edge/client/cycle projection is a faithful A·x̂ of every fill;
          two owners of one symbol are NEVER silently merged.

  1: build_ledger_graph(data_dir, universe, ts, recent_n)   ▷ the projector
  2:   for fp in sorted(glob(data_dir/".gt_fills_*.jsonl")):
     │      m ← _FILE_RE.search(basename)                    ▷ skip non-matching
  3:      sym ← m.sym ; cid ← int(m.cid)
     │      if universe and sym ∉ universe → skip
  4:      fx ← _is_fx(sym)                                   ▷ 6 alpha, both legs ∈ _CCY
     │      base ← sym[:3] if fx else None ; quote ← sym[3:] if fx else "USD"
  5:      for line in open(fp):                              ▷ one fill per line
     │         r ← json.loads(line)   (skip blank / unparsable)
  6:         sg ← _sign(r.side)       ▷ BOT/BUY/B → +1 · SLD/SELL/S → −1 · else skip
  7:         n  ← float(r.shares)     ▷ skip on bad number
     │         p  ← float(r.price) or None
  8:         position  += sg·n                               ▷ net base units / shares
     │         if fx:  base_leg += sg·n ;  quote_leg += −sg·n·p
     │         else:   quote_leg += −sg·n·p                  ▷ equity spends USD
     │         last_fill_ts ← r.time ; all_fills.append( (ts,sym,sg,n,p,base,quote) )
  9:      rec ← {base,quote,position,base_leg,quote_leg,client_id,fills,asset,last_fill_ts}
 10:      key ← sym  unless sym already in pairs → f"{sym}#{cid}"
     │         ▷ A79 single-writer should prevent this; graph refuses to merge owners
 11:      ── currency nodes (net cash per ccy = A·x̂) ──
     │         fx  → currencies[base].net_cash += base_leg ; [quote] += quote_leg
     │         eq  → currencies["USD"].net_cash += quote_leg
 12:      ── client grouping ──  clients[cid]: append key, += fills, open++ if |pos|>ε
 13:      ── per-client ccy footprint ──  client_ccy[cid][ccy] += leg   ▷ attribution
 14:   finalize: each currency.degree ← len(pairs) ; net_cash rounded
 15:   ── edges (only FX pairs connect two nodes) ──
     │      per pair: open_pairs++ / gross += |pos| ; emit {pair,base,quote,
     │      position,magnitude,side(long/short/flat),client_id}
 16:   ── LV5 blind dimensions ──  open_fx ← edges with |pos|>ε
     │      cycles ← _cycle_basis(open_fx)                   ▷ E − V + C loops
 17:   ── LV7 recent ticker ──  all_fills.sort by ts desc ; take recent_n
 18:   return { ts, currencies, pairs, edges, clients(str-keyed),
     │           client_ccy(str-keyed,rounded), cycles, blind_dim,
     │           recent_fills, totals{fills,pairs,open_pairs,currencies,
     │           gross_base_notional,blind_dim} }

 19: _cycle_basis(open_edges)                                ▷ fundamental cycle basis
     │      union-find spanning forest over currencies(vertices), pairs(edges)
 20:      each edge → tree (joins two trees) or nontree (closes a loop)
 21:      for each nontree edge:  loop ← [pair] + tree_path_pairs(b,q)
     │         tree_path_pairs ← BFS over adj forest (local deque import)
 22:      return cycles  ▷ the position dimensions IBKR per-ccy cash is BLIND to

 23: reconcile_cash(graph, cash_by_account, baseline, client_account_map, tol…)
     │      ▷ the shared-pocket problem: ONE IBKR balance per ccy, moved by all
 24:      expected[ccy] ← Σ OUR clients' footprint (from graph.client_ccy)
     │         grouped per account via acct_of(cid) (sole acct or map)
 25:      per (account,ccy):  delta ← cash − baseline ; resid ← delta − expected
     │         reconciled? is_reconciled(): USD always true (financing/PnL noise),
     │         else |resid| ≤ max(tol_abs, tol_rel·scale)
 26:      aggregate across accounts → by_currency[ccy] with external_residual +
     │         by_client attribution ; external_detected if any non-USD unreconciled
 27:      return {by_currency, by_account, multi_account, external_detected}
     │         ▷ external residual = "USD moved somewhere else" — a DIFFERENT
     │           client / manual TWS / missed fill that no client of ours explains

─── 関  Functions / classes defined ──────────────────────────────────────
   _is_fx(sym)          → bool   6 alpha-upper, base & quote both ∈ _CCY
   _sign(side)          → int    BOT/BUY/B → +1 · SLD/SELL/S → −1 · else 0
   _cycle_basis(open_edges) → list   union-find forest → fundamental cycles
       find(x)               nested  path-compressing union-find root
       tree_path_pairs(s,t)  nested  BFS in forest → pair symbols on s→t path
   build_ledger_graph(data_dir, universe, ts, recent_n) → dict   the projector
   reconcile_cash(graph, cash_by_account, baseline…, client_account_map, tol…) → dict

─── 変  Variables / state created ────────────────────────────────────────
   _FILE_RE        re.Pattern   `.gt_fills_(sym)_(port)_(cid).jsonl$`
   _CCY            set[str]     10 FX legs (EUR USD JPY GBP AUD CHF CAD NZD SEK NOK)
   ── inside build_ledger_graph (per call, no module state) ──
   currencies      dict   CCY → {net_cash, degree, pairs[]}      (the NODES)
   pairs           dict   key → {base,quote,position,…,asset}    (per-symbol)
   clients         dict   cid → {pairs[], fills, open}           (owners)
   client_ccy      dict   cid → {ccy: net cash}                  (attribution)
   all_fills       list   (time,sym,sign,shares,price,base,quote) → sorted ticker
   edges           list   FX pairs only → render edges with side/magnitude
   cycles          list   _cycle_basis output → blind_dim = len(cycles)

─── 呼  Calls-out  → ─────────────────────────────────────────────────────
   stdlib only:  glob.glob · json.loads · os.path.join/basename · re.search
                 collections.deque
   build_ledger_graph → _FILE_RE.search · _is_fx · _sign · _cycle_basis
   reconcile_cash     → reads graph["client_ccy"] (build_ledger_graph output)
   ▷ NO calls into broker / engine / feed — strictly downstream of the ledgers.

─── 被  Called-by  ← ─────────────────────────────────────────────────────
   scripts/ledger_server.py   ▷ LV2 SSE server — build_ledger_graph @10 FPS,
                                 reconcile_cash with _BASELINE per frame
   scripts/fx_positions.py    ▷ one-shot CLI snapshot of open FX positions
   tests/test_ledger_graph.py ▷ LV1 suite — universe filter, FX legs, recon,
                                 single + sub-account reconciliation cases
   (graph index empty for this file — edges above are from grep call-sites)

─── 注  Notes · invariants ───────────────────────────────────────────────
   • Pure read-model.  No broker connection, no client-id slot consumed, engine
     untouched.  Storage truth stays the append-only JSONL ([[fill_ledger]]).
   • Never read the clock.  `ts` is stamped in by the caller so the projection
     is deterministic — same files in, same graph out.
   • Never merge owners.  If two cids ever write one symbol the key becomes
     `SYM#cid` — A79 single-writer should make this impossible, but the graph
     refuses to silently fold two owners into one edge.
   • A·x̂ sign rule.  BUY a pair lifts BASE (+base_leg), spends QUOTE (−price·n);
     equity always spends USD.  Net cash per currency IS the node weight.
   • Blind dimensions (LV5).  open-FX cycles (E−V+C) are exactly the position
     space IBKR's per-currency cash and positions() cannot see → blind_dim.
   • External residual (LV9).  delta − our-expected per ccy = activity NO client
     of ours explains (other client / manual TWS / missed fill); USD exempted as
     financing/PnL noise.  Sub-account map → exact per-(account,ccy) segregation.
   • Links:  ledger files → [[fill_ledger]] · order/cash truth → [[broker]]
     P&L scaling → [[risk]] · who writes fills → [[engine]]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
