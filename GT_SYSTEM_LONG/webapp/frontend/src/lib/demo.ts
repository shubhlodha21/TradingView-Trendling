/**
 * Demo data engine — entirely client-side. Drives the same store the
 * real WebSocket would, so every component renders without any backend
 * running.
 *
 * Scenarios baked in:
 *   * 5 bots across different states (LONG / WAITING_REENTRY / MONITORING)
 *     so the Positions table and Bots tab are both populated.
 *   * Geometric Brownian motion price ticks at ~5 Hz with realistic
 *     drift + volatility per symbol.
 *   * Spreads stay 1-3 bps; bid_size / ask_size jitter ±25%.
 *   * Tape entries every ~700 ms, alternating direction so the trades
 *     panel scrolls.
 *   * Gateway flips offline once a minute for ~5s — shows that the
 *     top-bar badge actually changes.
 *   * Realized P&L drifts slightly so the P&L cells animate.
 *
 * Stop with `stopDemo()` (the WS driver does this when a real backend
 * connects so demo data doesn't double-write).
 */
import { setState, getState } from "./store";
import type {
  GatewayStatus, ProcessInfo, SymbolLive, SymbolSnapshot, SymbolState,
} from "./types";

type Seed = {
  symbol: string; cid: number;
  state: "MONITORING" | "WAITING_REENTRY" | "IN_POSITION";
  basePx: number;   // current mark
  vol: number;      // per-tick volatility ($)
  drift: number;    // per-tick drift bias ($)
  qty: number;      // open position size (0 if not in position)
  entry: number;    // entry price (if in position)
  pnl: number;      // realized
  trades: number;
  wins: number; losses: number;
  trigger: number;  // current breakout trigger
};

const SEEDS: Seed[] = [
  // Big move-of-the-day: long NVDA, deeply in profit.
  { symbol: "NVDA",  cid: 1, state: "IN_POSITION",     basePx: 482.50, vol: 0.18, drift: +0.005, qty: 50, entry: 472.10, pnl:  340.00, trades: 4, wins: 3, losses: 1, trigger: 480.00 },
  // Just took a loss and is waiting for the next breakout retest.
  { symbol: "AAPL",  cid: 2, state: "WAITING_REENTRY", basePx: 224.80, vol: 0.05, drift: -0.001, qty:  0, entry: 0,      pnl: -82.50,  trades: 7, wins: 4, losses: 3, trigger: 226.00 },
  // Newly launched, monitoring near the trigger.
  { symbol: "TSLA",  cid: 3, state: "MONITORING",      basePx: 252.10, vol: 0.20, drift: +0.002, qty:  0, entry: 0,      pnl: 0.0,    trades: 0, wins: 0, losses: 0, trigger: 255.00 },
  // Small win, currently flat after exit.
  { symbol: "NFLX",  cid: 6, state: "WAITING_REENTRY", basePx: 712.40, vol: 0.30, drift: +0.002, qty:  0, entry: 0,      pnl:  214.20, trades: 2, wins: 2, losses: 0, trigger: 715.00 },
  // The hot AI play — running, in profit, large position.
  { symbol: "AVGO",  cid: 4, state: "IN_POSITION",     basePx: 1632.40, vol: 0.40, drift: +0.010, qty: 15, entry: 1610.00, pnl: 0.0, trades: 1, wins: 0, losses: 0, trigger: 1620.00 },
];

const PROCS: ProcessInfo[] = SEEDS.map((s, i) => ({
  key: `${s.symbol}_${s.cid}`,
  pid: 10_000 + i * 7,
  symbol: s.symbol,
  client_id: s.cid,
  port: 7496,
  paper: false,
  started_at: new Date(Date.now() - (3600_000 + i * 1200_000)).toISOString(),
  cmd: [
    "python", "run_live.py", s.symbol,
    "--trigger", String(s.trigger),
    "--qty", String(Math.max(s.qty, 50)),
    "--stop", "0.01",
    "--port", "7496",
    "--client-id", String(s.cid),
    "--offset-fixed", "0.20",
    "--uvloop",
  ],
  status: "running",
  exit_code: null,
  log_tail: [
    `[Engine] state=${s.state} qty=${s.qty} trigger=$${s.trigger}`,
    "[Gateway] Account summary subscribed",
    "[Feed] tick stream live",
  ],
}));

// ── Internal mutable per-symbol state for the random walk ───────────
interface RuntimeState {
  px: number; high: number; low: number; open: number; vwap_num: number; vwap_den: number;
  bidSize: number; askSize: number; volume: number;
  tape: Array<{ ts: string; price: number; size: number; direction: number; exchange: string }>;
  pnl: number;
}

const runtime = new Map<string, RuntimeState>();

function initRuntime(s: Seed): RuntimeState {
  return {
    px: s.basePx,
    high: s.basePx * 1.012,
    low:  s.basePx * 0.988,
    open: s.basePx * (1 + (Math.random() - 0.5) * 0.01),
    vwap_num: 0, vwap_den: 0,
    bidSize: 200 + Math.floor(Math.random() * 800),
    askSize: 200 + Math.floor(Math.random() * 800),
    volume: 1_200_000 + Math.floor(Math.random() * 4_000_000),
    tape: [],
    pnl: s.pnl,
  };
}

SEEDS.forEach((s) => runtime.set(s.symbol, initRuntime(s)));

// ── Runtime bot insertion (used by the Strategy deployer in demo mode) ──
// The right-panel OrderTicket's Strategy mode calls this when the user
// hits Deploy. The new bot starts in MONITORING state at a synthetic
// price near the trigger, so the chart + positions tabs animate it
// immediately like the seeded ones.
export function addDemoBot(spec: {
  symbol: string;
  trigger: number;
  qty: number;
  stop: number;
  port: number;
  client_id: number;
  paper: boolean;
  offset_fixed?: number | null;
}): { key: string; pid: number } {
  const sym = spec.symbol.toUpperCase();
  // De-dupe — if a bot already exists for this (symbol, client_id),
  // refuse, same contract as the real backend's ProcessManager.launch.
  const key = `${sym}_${spec.client_id}`;
  if (PROCS.some((p) => p.key === key && p.status === "running")) {
    throw new Error(`Bot ${key} already running (demo)`);
  }
  // Start price = trigger ± 2% so the chart line crosses the trigger
  // visibly within ~30 seconds at the default vol.
  const basePx = spec.trigger * (1 + (Math.random() - 0.5) * 0.04);
  const seed: Seed = {
    symbol: sym,
    cid: spec.client_id,
    state: "MONITORING",
    basePx,
    vol: Math.max(0.05, basePx * 0.0008),  // ~8 bps per tick
    drift: 0,
    qty: 0, entry: 0, pnl: 0,
    trades: 0, wins: 0, losses: 0,
    trigger: spec.trigger,
  };
  // Only add to SEEDS if we don't already have this symbol (different
  // client_id pointing at the same symbol shouldn't double-feed the
  // tick loop — they share the same price runtime).
  if (!SEEDS.some((s) => s.symbol === sym)) {
    SEEDS.push(seed);
    runtime.set(sym, initRuntime(seed));
  }
  const pid = 20_000 + PROCS.length;
  const argv: string[] = [
    "python", "run_live.py", sym,
    "--trigger", String(spec.trigger),
    "--qty", String(spec.qty),
    "--stop", String(spec.stop),
    "--port", String(spec.port),
    "--client-id", String(spec.client_id),
  ];
  if (spec.offset_fixed != null) argv.push("--offset-fixed", String(spec.offset_fixed));
  argv.push("--uvloop");
  PROCS.push({
    key, pid,
    symbol: sym, client_id: spec.client_id, port: spec.port, paper: spec.paper,
    started_at: new Date().toISOString(),
    cmd: argv,
    status: "running",
    exit_code: null,
    log_tail: [
      `[Engine] launched via Strategy ticket (demo)`,
      `[Engine] state=MONITORING qty=${spec.qty} trigger=$${spec.trigger}`,
      `[Gateway] (demo) account summary subscribed`,
    ],
  });
  // Flush to store immediately so the Bots tab counter updates without
  // waiting for the next 5s process tick.
  setState({ processes: PROCS.slice() });
  // Auto-persist to demo session, mirroring the backend's auto-save
  // on every successful launch.
  try { demoSaveSession(); } catch {}
  return { key, pid };
}

// Box-Muller; ~normal(0,1)
function gauss(): number {
  let u = 0, v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function tick(): void {
  const snapshots: SymbolSnapshot[] = [];
  for (const s of SEEDS) {
    const r = runtime.get(s.symbol)!;
    // Random-walk price with mild drift toward trigger so the chart
    // crosses interesting levels on screen.
    const step = s.drift + s.vol * gauss() * 0.4;
    r.px = Math.max(0.01, r.px + step);
    r.high = Math.max(r.high, r.px);
    r.low  = Math.min(r.low, r.px);
    r.vwap_num += r.px * 100;
    r.vwap_den += 100;
    r.volume += 100 + Math.floor(Math.random() * 250);
    r.bidSize = Math.max(50, r.bidSize + Math.floor((Math.random() - 0.5) * 80));
    r.askSize = Math.max(50, r.askSize + Math.floor((Math.random() - 0.5) * 80));

    // Realized P&L drift only when in a position and trade closed —
    // for demo we just nudge it a hair so the cell flashes occasionally.
    if (s.state === "IN_POSITION" && Math.random() < 0.05) {
      r.pnl += (Math.random() - 0.45) * 8;
    }

    // ~1 tape entry every 3-4 ticks
    if (Math.random() < 0.30) {
      r.tape.push({
        ts: new Date().toISOString(),
        price: r.px,
        size: 25 + Math.floor(Math.random() * 400),
        direction: Math.random() < 0.5 ? 1 : -1,
        exchange: "NSDQ",
      });
      if (r.tape.length > 50) r.tape.shift();
    }

    const spread = Math.max(0.01, r.px * (0.0001 + Math.random() * 0.0002));
    const bid = +(r.px - spread / 2).toFixed(2);
    const ask = +(r.px + spread / 2).toFixed(2);
    const vwap = r.vwap_den > 0 ? r.vwap_num / r.vwap_den : r.px;

    const live: SymbolLive = {
      ts: new Date().toISOString(),
      symbol: s.symbol,
      last: +r.px.toFixed(2),
      bid, ask,
      bid_size: r.bidSize,
      ask_size: r.askSize,
      volume: r.volume,
      open: +r.open.toFixed(2),
      high: +r.high.toFixed(2),
      low:  +r.low.toFixed(2),
      vwap: +vwap.toFixed(4),
      trigger_price: s.trigger,
      quantity: s.qty,
      equity: 152_430,
      buying_power: 304_860,
      position_notional: s.qty * r.px,
      exposure_pct: 0,
      bp_used_pct: 0,
      rate: 60 + Math.random() * 30,
      tick_rate: 60 + Math.random() * 30,
      bbo_rate: 40 + Math.random() * 20,
      trade_rate: 8 + Math.random() * 6,
      buy_pct:  45 + Math.random() * 12,
      sell_pct: 40 + Math.random() * 12,
      connected: true,
      paused: false,
      heartbeat_age: 0.3,
      latency: { p50_ms: 0.18, p99_ms: 0.62 },
      tape: r.tape.slice(),
    };

    const state: SymbolState = {
      state: s.state,
      cycle_id: `${s.symbol}_${1000 + s.trades}`,
      position_open: s.state === "IN_POSITION",
      entry_price: s.state === "IN_POSITION" ? s.entry : null,
      highest_price: s.state === "IN_POSITION" ? Math.max(r.high, s.entry) : null,
      stop_loss: s.state === "IN_POSITION" ? +(s.entry * 0.99).toFixed(2) : null,
      previous_breakout_level: s.trigger,
      quantity: s.qty,
      trades_today: s.trades,
      wins: s.wins,
      losses: s.losses,
      pnl: +r.pnl.toFixed(2),
      total_commission: s.trades * 0.35 * Math.max(s.qty, 50),
    };

    const change_pct = r.open > 0 ? ((r.px - r.open) / r.open) * 100 : 0;
    snapshots.push({
      key: `${s.symbol}_${s.cid}`,
      symbol: s.symbol,
      client_id: s.cid,
      is_active: true,
      last_seen: new Date().toISOString(),
      state,
      live,
      spread: +(ask - bid).toFixed(2),
      spread_bps: bid > 0 ? ((ask - bid) / bid) * 10_000 : 0,
      change_pct,
    });
  }
  setState({ symbols: snapshots });
}

function processes(): void {
  setState({ processes: PROCS });
}

function gateway(t: number): void {
  // Flip TWS "offline" once a minute for 5 seconds so the badge animation
  // is visible in the demo. Most of the time TWS is up.
  const minute = Math.floor(t / 60_000);
  const inOutage = (t % 60_000) > 55_000;
  const gw: GatewayStatus = inOutage
    ? { host: "127.0.0.1", port: 7496, reachable: false, last_checked: new Date().toISOString(), error: "ConnectionRefused (demo)" }
    : { host: "127.0.0.1", port: 7496, reachable: true,  last_checked: new Date().toISOString(), error: null };
  void minute;  // unused; kept so the comment makes sense
  setState({ gateway: gw });
}

// ── Lifecycle ────────────────────────────────────────────────────────
let tickHandle: number | null = null;
let procHandle: number | null = null;
let gwHandle: number | null = null;

export function startDemo(): void {
  if (tickHandle != null) return;
  setState({
    connected: true,
    source: "demo",
    lastError: null,
  });
  processes();
  gateway(Date.now());
  // 5 Hz price + state ticks — same cadence the real WS uses.
  tickHandle = window.setInterval(tick, 200);
  procHandle = window.setInterval(processes, 5_000);
  gwHandle = window.setInterval(() => gateway(Date.now()), 1_000);
  // Auto-save the seeded PROCS as the initial session if no session
  // has been persisted yet. Matches the real backend's behavior where
  // every running bot is in .gt_session.json — so "Restore Session"
  // brings back the demo set even if the user never clicked Save.
  if (!demoSavedSession()) {
    try { demoSaveSession(); } catch {}
  }
}

export function stopDemo(): void {
  if (tickHandle != null) window.clearInterval(tickHandle);
  if (procHandle != null) window.clearInterval(procHandle);
  if (gwHandle != null) window.clearInterval(gwHandle);
  tickHandle = procHandle = gwHandle = null;
  if (getState().source === "demo") setState({ source: "idle", connected: false });
}

export function isDemoActive(): boolean {
  return tickHandle != null;
}

// ── Session ops (demo-mode counterparts of the /api/sessions/* routes) ──
// Stored in localStorage so a page reload survives — matches the
// real backend's .gt_session.json on disk.
const SESSION_KEY = "gt_demo_session_v1";

interface DemoSession {
  saved_at: string;
  bots: Array<{
    symbol: string; trigger: number; qty: number; stop: number;
    port: number; client_id: number; paper: boolean;
    offset_fixed: number | null;
  }>;
}

export function demoSavedSession(): DemoSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as DemoSession) : null;
  } catch {
    return null;
  }
}

function persistDemoSession(s: DemoSession): void {
  try { localStorage.setItem(SESSION_KEY, JSON.stringify(s)); } catch {}
}

/** Snapshot current running PROCS into the localStorage session. */
export function demoSaveSession(): DemoSession {
  const bots = PROCS
    .filter((p) => p.status === "running")
    .map((p) => {
      // Recover params from the cmd argv — easier than threading the
      // original LaunchRequest through, and matches the real backend's
      // "snapshot what's running" semantics.
      const get = (flag: string) => {
        const i = p.cmd.indexOf(flag);
        return i >= 0 ? p.cmd[i + 1] : null;
      };
      return {
        symbol: p.symbol,
        trigger: parseFloat(get("--trigger") ?? "0"),
        qty: parseInt(get("--qty") ?? "0", 10),
        stop: parseFloat(get("--stop") ?? "0.01"),
        port: p.port,
        client_id: p.client_id,
        paper: p.paper,
        offset_fixed: get("--offset-fixed") ? parseFloat(get("--offset-fixed")!) : null,
      };
    });
  const sess: DemoSession = { saved_at: new Date().toISOString(), bots };
  persistDemoSession(sess);
  return sess;
}

/** SIGTERM-equivalent for every running bot in demo mode. Doesn't
    touch the saved session so demoRestoreSession() can revive them. */
export function demoStopAll(): { stopped: string[]; already_done: string[] } {
  const stopped: string[] = [];
  const already_done: string[] = [];
  for (const p of PROCS) {
    if (p.status === "running") {
      p.status = "killed";
      p.exit_code = 0;
      p.log_tail.push("[Engine] SIGTERM received, flushing state");
      p.log_tail.push("[Engine] state file saved");
      p.log_tail.push("[Engine] disconnected from gateway");
      stopped.push(p.key);
    } else {
      already_done.push(p.key);
    }
  }
  // Also stop the tick loop for symbols that no longer have a live bot,
  // visually so positions/last freeze.
  setState({ processes: PROCS.slice() });
  return { stopped, already_done };
}

/** Relaunch every bot in the saved session that isn't currently running. */
export function demoRestoreSession(): {
  launched: Array<{ key: string; pid: number }>;
  skipped: Array<{ key: string; reason: string }>;
  error: string | null;
} {
  const sess = demoSavedSession();
  if (!sess || sess.bots.length === 0) {
    return { launched: [], skipped: [], error: "No saved session." };
  }
  const runningKeys = new Set(
    PROCS.filter((p) => p.status === "running").map((p) => p.key),
  );
  const launched: Array<{ key: string; pid: number }> = [];
  const skipped: Array<{ key: string; reason: string }> = [];
  for (const b of sess.bots) {
    const key = `${b.symbol}_${b.client_id}`;
    if (runningKeys.has(key)) {
      skipped.push({ key, reason: "already running" });
      continue;
    }
    // If a killed entry exists for this key, flip it back to running
    // rather than appending — preserves pid + start time for log continuity.
    const existing = PROCS.find((p) => p.key === key);
    if (existing) {
      existing.status = "running";
      existing.started_at = new Date().toISOString();
      existing.log_tail.push("[Engine] restarted via Restore Session");
      existing.log_tail.push(`[Engine] state=MONITORING qty=${b.qty} trigger=$${b.trigger}`);
      launched.push({ key, pid: existing.pid });
    } else {
      try {
        const r = addDemoBot(b);
        launched.push(r);
      } catch (e: any) {
        skipped.push({ key, reason: e.message ?? String(e) });
      }
    }
  }
  setState({ processes: PROCS.slice() });
  return { launched, skipped, error: null };
}

/** Relaunch a single bot from the saved demo session.
 *  Throws if the key isn't in the saved session, or is already running. */
export function demoRestoreOne(key: string): { key: string; pid: number } {
  const sess = demoSavedSession();
  if (!sess) throw new Error("No saved session.");
  const target = sess.bots.find((b) => `${b.symbol}_${b.client_id}` === key);
  if (!target) throw new Error(`${key} not in saved session.`);
  const existing = PROCS.find((p) => p.key === key);
  if (existing && existing.status === "running") {
    throw new Error(`${key} already running.`);
  }
  if (existing) {
    // Resurrect the same record so pid + log history are preserved.
    existing.status = "running";
    existing.started_at = new Date().toISOString();
    existing.log_tail.push("[Engine] resurrected via per-bot Recover");
    existing.log_tail.push(`[Engine] state=MONITORING qty=${target.qty} trigger=$${target.trigger}`);
    setState({ processes: PROCS.slice() });
    return { key: existing.key, pid: existing.pid };
  }
  // Otherwise add fresh.
  return addDemoBot(target);
}
