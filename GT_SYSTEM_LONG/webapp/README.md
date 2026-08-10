# GT Trading Console (webapp)

Web UI for managing the GT trading bot. **Strictly additive** — does
not modify any file under `src/`, `run_live.py`, or the dashboards.
The bot keeps running exactly as before; the webapp reads its
`.gt_state_*_*.json` / `.gt_live_*_*.json` artifacts and spawns more
`run_live.py` instances as subprocesses.

## Architecture

```
              ┌───────── Browser ──────────┐
              │   React + Vite SPA         │
              └────────────┬───────────────┘
                           │ HTTPS / WS
                           ▼
              ┌───── FastAPI (EC2) ────────┐
              │  /api/* REST               │
              │  /ws live frames           │
              │                            │
              │  process_manager  ─ spawn ▶│ run_live.py (× N)
              │  state_reader     ─ read  ◀│ .gt_state_*.json
              │  gateway_probe    ─ TCP   ▶│ TWS / IB Gateway
              │  ibkr_proxy       ─ TODO  ─│ (manual orders)
              └────────────────────────────┘
```

The webapp talks to the running bots **only through their files** —
the same contract `dashboard_agg.py` uses. Nothing imports from
`src/strategy/` or `src/execution/`.

## What's wired in v0.1

- ✅ Top bar with TWS gateway health badge (TCP probe to API port)
- ✅ Ticker dropdown + search, live header pills
  (Last / Bid / Ask / Spread / 24h / Vol / VWAP / H-L / Trigger)
- ✅ Candlestick chart driven by streaming `last` prices, with
  1s / 5s / 1m / 5m / 15m / 1h / D / W timeframes
- ✅ Positions tab — every open position across every running bot
- ✅ Balances tab — equity, BP, exposure, realized P&L roll-up
- ✅ Bots tab — list running bots, kill (SIGTERM/SIGKILL), launch form
  with live "command preview" mirroring the exact `run_live.py` argv
- ✅ Order book ladder (synthetic for now; layout is real)
- ✅ Trades tab from the bot's microstructure tape (real data)
- ✅ Manual order ticket (Market / Limit / Pro) — **dry-run only**
- ✅ WebSocket auto-reconnect with exponential backoff
- ✅ Hyperliquid-inspired dark theme, hairline borders, top-bar glow

## What's deliberately stubbed

| Feature | Why | Lives at |
|---|---|---|
| Manual order submit (real) | Need audit destination + manual-order risk gates + confirmation UX agreed before this routes real money. Returns HTTP 501. | `backend/ibkr_proxy.py` |
| Open orders tab | Needs an IBKR `openOrders` subscription. Adds a session slot. | `BottomTabs.tsx:OpenOrdersStub` |
| Order History tab | Reads `data/audit/order_<SYM>_<DATE>.csv` — pending the audit reader. | `BottomTabs.tsx:OrderHistoryStub` |
| Real L2 order book | Needs IBKR `reqMktDepth` (market-data permission). Ladder shape is correct, depth values are synthetic from bid_size/ask_size. | `OrderBookPanel.tsx:Ladder` |
| Historical chart bars | Chart shows live ticks bucketed into the active timeframe; historical bars come from a `reqHistoricalData` endpoint not yet built. Layout is final. | `ChartPanel.tsx` |

## Running it

### Backend (on EC2)

```bash
cd webapp/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# from the repo root so the state files are in cwd:
cd ../..
GT_WEBAPP_CWD="$(pwd)" \
GT_WEBAPP_TWS_HOST=127.0.0.1 \
GT_WEBAPP_TWS_PORT=7496 \
uvicorn webapp.backend.main:app --host 0.0.0.0 --port 8000
```

### Frontend (dev)

```bash
cd webapp/frontend
npm install
npm run dev    # http://localhost:5173
```

### Demo mode (no backend, no EC2 — just the browser)

The frontend runs entirely client-side in demo mode. Open it without
anything else running and you'll see a synthetic-data version with
5 fake bots across NVDA / AAPL / TSLA / NFLX / AVGO, prices ticking
at 5 Hz, populated positions, a moving order book, gateway flipping
status — the full UI under realistic load.

```bash
cd webapp/frontend
npm install
npm run dev
# open http://localhost:5173            ← auto: tries live WS, falls
#                                         back to demo after 2s
# open http://localhost:5173/?demo=1    ← force demo, no WS attempts
# open http://localhost:5173/?live=1    ← force live, never demo
```

Demo mode shows a teal banner at the top of the screen. If a real
backend connects mid-session, the WS frame takes over automatically
and the banner disappears.

### Frontend (prod build, served by FastAPI)

```bash
cd webapp/frontend
npm install && npm run build
# Then start uvicorn as above — it auto-mounts dist/ at "/"
```

## Environment knobs

| Var | Default | Purpose |
|---|---|---|
| `GT_WEBAPP_CWD` | `.` | Where `.gt_state_*.json` files live and where `run_live.py` is spawned. |
| `GT_WEBAPP_TWS_HOST` | `127.0.0.1` | TWS host for the gateway probe. |
| `GT_WEBAPP_TWS_PORT` | `7496` | TWS port (4001 = IB Gateway live, 7497 = TWS paper, etc). |
| `GT_WEBAPP_ALLOWED_ORIGINS` | `localhost:5173` | Comma-separated CORS allowlist. |
| `GT_WEBAPP_STALENESS_S` | `30` | Mark a bot offline after this many seconds of state-file inactivity. |
| `GT_WEBAPP_FRAME_HZ` | `5` | WebSocket broadcast rate. |
| `GT_WEBAPP_GATEWAY_EVERY` | `10` | Probe TWS every Nth frame (default: every 2s at 5Hz). |
| `GT_WEBAPP_MANUAL_CLIENT_ID` | `99` | IBKR client_id for manual orders (must not collide with bot client_ids). |
| `GT_WEBAPP_MAX_MANUAL_NOTIONAL` | `25000` | Hard cap on manual order notional (same as the portfolio risk gate). |

## Security checklist before you expose this

The original prompt mentioned SSH'ing in with `ubuntu:1234`. **Do not
deploy this with that password.** Bare minimum hardening:

1. Disable SSH password auth on the EC2 box; use key-based only.
2. Move SSH off port 22.
3. EC2 security group: port 8000 restricted to your IP only, or only
   reachable via VPN / tailscale.
4. Front the FastAPI app with nginx + a real TLS cert (Let's Encrypt
   or AWS Certificate Manager).
5. Add HTTP basic auth (or session auth) — *anyone* who hits
   `/api/processes` can launch a `run_live.py` with `--paper=false`.
6. Don't commit any `.env` file with TWS or EC2 credentials.

## File map

```
webapp/
├── backend/
│   ├── main.py              # FastAPI app + routes
│   ├── schemas.py           # Pydantic models (source of truth)
│   ├── state_reader.py      # polls .gt_state_*.json / .gt_live_*.json
│   ├── process_manager.py   # spawn/kill run_live.py
│   ├── gateway_probe.py     # TCP probe for TWS
│   ├── ibkr_proxy.py        # manual order routing (501 today)
│   ├── ws.py                # WebSocket broadcaster
│   └── requirements.txt
└── frontend/
    ├── index.html
    ├── package.json
    ├── tailwind.config.js / postcss.config.js
    ├── tsconfig.json
    ├── vite.config.ts
    └── src/
        ├── App.tsx                       # root layout
        ├── main.tsx                      # entry
        ├── styles/index.css
        ├── lib/
        │   ├── api.ts                    # REST client
        │   ├── fmt.ts                    # USD/price/pct formatters
        │   └── types.ts                  # TS mirror of schemas.py
        ├── hooks/
        │   └── useWebSocket.ts           # singleton WS + store
        └── components/
            ├── Icon.tsx                  # inline SVG set
            ├── TopBar.tsx                # logo, nav, TWS badge
            ├── LeftRail.tsx              # vertical chart-tool icons
            ├── SymbolHeader.tsx          # ticker picker + pills
            ├── ChartPanel.tsx            # lightweight-charts wrapper
            ├── BottomTabs.tsx            # Balances/Positions/Open/History/Bots
            ├── BotLauncher.tsx           # run_live.py launch form
            ├── OrderBookPanel.tsx        # ladder + trades tape
            └── OrderTicket.tsx           # Market/Limit/Pro ticket
```
