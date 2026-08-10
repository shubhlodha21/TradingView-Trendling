━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                         流 谱   ·   FLOW ATLAS
              a quiet, ever-growing map of the whole system
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

One page per source file. Each page renders that file the way the
Self-Harness paper renders its algorithm on page 4 — a single ruled
block: what it *requires*, what it *provides*, the numbered *flow* of
calls with ▷ margin-notes, and separate ledgers of the *modules* it
uses and the *variables* it creates.

The atlas mirrors `src/` exactly:  `src/strategy/engine.py`  →
`flow_atlas/src/strategy/engine.flow.md`.

—— 哲   philosophy ———————————————————————————————————————————————————————

  • 加えるのみ、削らず.   We only ADD. Nothing is ever removed. A flow
    that grows to a thousand steps is welcome — depth is the point.

  • 間 (ma).   It may grow vast, yet stays serene: generous negative
    space, one idea per line, monochrome, no ornament. Big ≠ noisy.

  • 真実は graph から.   Call/import edges come from the code
    knowledge-graph (`.code-review-graph`), not from memory — so the
    map matches the territory.

  • 記録せよ.   Log everything. Every file gets a page; the index
    below tracks what is mapped and what is still 未 (not yet).

—— 印   legend  (kanji markers keep each page scannable) ————————————————

   流  flow / file title          要  Require   (what must exist first)
   算  algorithm — the call flow   出  Provides  (what this file exports)
   部  modules used               関  functions & classes defined
   変  variables / state created  呼  calls-out  →   被  called-by  ←
   注  notes / invariants         ▷  margin-note on a flow step

—— 型   the page template ———————————————————————————————————————————————

   每 page is `<file>.flow.md` and contains, in order:
     1. ruled title banner  (流 NN · path — one-line role)
     2. 要 Require / 出 Provides
     3. 部 Modules used        (imports, grouped)
     4. 算 Algorithm           (numbered flow, indented for nested calls,
                                ▷ notes; this is the heart — it grows)
     5. 関 Functions / classes (full inventory, one line each)
     6. 変 Variables / state   (module-level + key instance state)
     7. 呼 Calls-out  /  被 Called-by   (graph edges)
     8. 注 Notes               (invariants, gotchas, links to other 流)

—— 索   living index   (☑ mapped · ◐ partial · ☐ 未 / not yet) ——————————

  strategy/                                                 lines
    ☑ src/strategy/engine.flow.md          engine.py         8210   ← started (lifecycle spine)
    ☑ src/strategy/risk.flow.md            risk.py            776
    ☑ src/strategy/logging.flow.md         logging.py         240
    ☑ src/strategy/__init__.flow.md        __init__.py          4

  execution/
    ☑ src/execution/broker.flow.md         broker.py         3060
    ☑ src/execution/order_manager.flow.md  order_manager.py   338
    ☑ src/execution/fill_ledger.flow.md    fill_ledger.py     260

  feed/
    ☑ src/feed/handler.flow.md             handler.py         822
    ☑ src/feed/candles/builder.flow.md     builder.py         585
    ☑ src/feed/connection.flow.md          connection.py      407
    ☑ src/feed/cache.flow.md               cache.py           374
    ☑ src/feed/pipeline/sequence.flow.md   sequence.py        327
    ☑ src/feed/pipeline/validator.flow.md  validator.py       304
    ☑ src/feed/pipeline/base.flow.md       base.py            269
    ☑ src/feed/pipeline/deduplicator.flow.md deduplicator.py  235
    ☑ src/feed/production.flow.md          production.py      168
    ☑ src/feed/pipeline/normalizer.flow.md normalizer.py      118
    ☑ src/feed/__init__.flow.md            __init__.py        100
    ☑ src/feed/pipeline/fast_validator.flow.md fast_validator.py 44
    ☑ src/feed/candles/__init__.flow.md    __init__.py         40

  config/
    ☑ src/config/audit.flow.md             audit.py          1339
    ☑ src/config/models.flow.md            models.py          806
    ☑ src/config/persistence.flow.md       persistence.py     198
    ☑ src/config/__init__.flow.md          __init__.py         31
    ☑ src/config/loader.flow.md            loader.py            7

  assets/
    ☑ src/assets/future.flow.md            future.py          609
    ☑ src/assets/forex.flow.md             forex.py           567
    ☑ src/assets/us_stock.flow.md          us_stock.py        554
    ☑ src/assets/types.flow.md             types.py           512
    ☑ src/assets/cfds.flow.md              cfds.py            505
    ☑ src/assets/resolver.flow.md          resolver.py        365
    ☑ src/assets/currency_service.flow.md  currency_service.py 171
    ☑ src/assets/spec.flow.md              spec.py            116
    ☑ src/assets/enum.flow.md              enum.py             60
    ☑ src/assets/__init__.flow.md          __init__.py         55
    ☑ src/assets/policies/risk_overlay.flow.md risk_overlay.py 179
    ☑ src/assets/policies/price.flow.md    price.py           148
    ☑ src/assets/policies/tick.flow.md     tick.py            124
    ☑ src/assets/policies/session.flow.md  session.py         115
    ☑ src/assets/policies/sizing.flow.md   sizing.py          105
    ☑ src/assets/policies/lifecycle.flow.md lifecycle.py       93
    ☑ src/assets/policies/contract.flow.md contract.py         83
    ☑ src/assets/policies/commission.flow.md commission.py     57
    ☑ src/assets/policies/__init__.flow.md __init__.py         46

  infra/
    ☑ src/infra/alerts.flow.md             alerts.py         1091
    ☑ src/infra/__init__.flow.md           __init__.py         42

  ledger/
    ☑ src/ledger/graph.flow.md             graph.py           399
    ☑ src/ledger/__init__.flow.md          __init__.py          1

  hero flows (cross-file, FigJam — monochrome, left→right)
    ☑ ① ENTRY 入      place bracket → fill → arm SL
    ☑ ② EXIT  出      SL fires → record P&L → re-arm   (dashed re-entry loop)
    ☑ ③ RECONCILE 復  restart → load-state → replay → adopt → three-truths → resume
    board:  https://www.figma.com/board/brnEcJrg8PLhnHt8RR7Ee5

  50 source files · ~25,000 lines.  The atlas grows one 流 at a time.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
