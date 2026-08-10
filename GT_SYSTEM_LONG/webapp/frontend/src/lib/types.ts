/**
 * TypeScript mirrors of webapp/backend/schemas.py.
 *
 * These are *manually* kept in sync — a 30-second discipline that
 * we accept in exchange for not adding a code-generation step. The
 * shapes are small and the Pydantic models are the source of truth.
 */

export interface SymbolState {
  state: string | null;
  cycle_id: string | null;
  position_open: boolean;
  entry_price: number | null;
  highest_price: number | null;
  stop_loss: number | null;
  previous_breakout_level: number | null;
  quantity: number;
  trades_today: number;
  wins: number;
  losses: number;
  pnl: number;
  total_commission: number;
}

export interface SymbolLive {
  ts: string | null;
  symbol: string | null;
  last: number;
  bid: number;
  ask: number;
  bid_size: number;
  ask_size: number;
  volume: number;
  open: number;
  high: number;
  low: number;
  vwap: number;
  trigger_price: number;
  quantity: number;
  equity: number;
  buying_power: number;
  position_notional: number;
  exposure_pct: number;
  bp_used_pct: number;
  rate: number;
  tick_rate: number;
  bbo_rate: number;
  trade_rate: number;
  buy_pct: number;
  sell_pct: number;
  connected: boolean;
  paused: boolean;
  heartbeat_age: number;
  latency: Record<string, unknown>;
  tape: Array<{
    ts?: string;
    price?: number;
    size?: number;
    exchange?: string;
    direction?: number;
  }>;
}

export interface SymbolSnapshot {
  key: string;
  symbol: string;
  client_id: number;
  is_active: boolean;
  last_seen: string;
  state: SymbolState;
  live: SymbolLive;
  spread: number;
  spread_bps: number;
  change_pct: number;
}

export interface ProcessInfo {
  key: string;
  pid: number;
  symbol: string;
  client_id: number;
  port: number;
  paper: boolean;
  started_at: string;
  cmd: string[];
  status: "running" | "exited" | "killed" | "error";
  exit_code: number | null;
  log_tail: string[];
}

export interface GatewayStatus {
  host: string;
  port: number;
  reachable: boolean;
  last_checked: string;
  error: string | null;
}

export interface LaunchRequest {
  symbol: string;
  trigger: number;
  qty: number;
  stop: number;
  port: number;
  client_id: number;
  paper: boolean;
  uvloop: boolean;
  sl_limit_offset?: number | null;
  offset_stop_fraction?: number | null;
  offset_entry_pct?: number | null;
  offset_fixed?: number | null;
}

export type OrderSide = "BUY" | "SELL";
export type OrderType = "MARKET" | "LIMIT" | "STOP" | "STOP_LIMIT";
export type TIF = "DAY" | "GTC" | "IOC";

export interface ManualOrderRequest {
  symbol: string;
  side: OrderSide;
  qty: number;
  order_type: OrderType;
  limit_price?: number | null;
  stop_price?: number | null;
  tif: TIF;
  acknowledged_notional: number;
}

export interface ManualOrderResponse {
  order_id: string;
  status: string;
  submitted_at: string;
}

// ── Audit + Alerts ──────────────────────────────────────────────────
// Numeric fields stay as strings (matches the bot's CSV writer
// per-column formatting). Parse with parseFloat at render time.
export interface AuditOrder {
  timestamp: string | null;
  event: string | null;          // SUBMITTED / FILLED / REJECTED / CANCELLED
  order_id: string | null;
  side: string | null;           // BUY / SELL
  qty: string | null;
  order_type: string | null;
  limit_price: string | null;
  stop_price: string | null;
  signal_price: string | null;
  fill_price: string | null;
  slippage: string | null;       // signed $ — positive = paid more than signal
  commission: string | null;
  pnl: string | null;
  reason: string | null;
  exchange: string | null;
  state_at_time: string | null;
  position_at_time: string | null;
}

export interface AuditState {
  timestamp: string | null;
  event: string | null;
  state: string | null;
  position_open: string | null;
  entry_price: string | null;
  highest_price: string | null;
  stop_loss: string | null;
  trigger_price: string | null;
  breakout_level: string | null;
  prev_ltp: string | null;
  ltp: string | null;
  pnl: string | null;
  trades_today: string | null;
  wins: string | null;
  losses: string | null;
  config_trigger: string | null;
  config_stop_pct: string | null;
  config_qty: string | null;
}

export interface AuditPnl {
  timestamp: string | null;
  state: string | null;
  position_open: string | null;
  entry_price: string | null;
  current_price: string | null;
  unrealized_pnl: string | null;
  realized_pnl: string | null;
  total_pnl: string | null;
  wins: string | null;
  losses: string | null;
  trades_today: string | null;
  comm_today: string | null;
  highest_price: string | null;
  stop_loss: string | null;
}

export type AlertSeverity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";

export interface AlertEntry {
  code: string;
  severity: AlertSeverity;
  message: string;
  timestamp: string;
  context: Record<string, unknown>;
  correlation_id?: string;
}

export interface AlertCounts {
  CRITICAL: number;
  HIGH: number;
  MEDIUM: number;
  LOW: number;
}


// ── Session save / restore ──────────────────────────────────────────
export interface SavedSession {
  saved_at: string;
  bots: LaunchRequest[];
}
export interface RestoreResult {
  launched: ProcessInfo[];
  skipped: { key: string; reason: string }[];
  error: string | null;
}
export interface StopAllResult {
  stopped: string[];
  already_done: string[];
}


// ── WS frame discriminated union ────────────────────────────────────
export type WSFrame =
  | { type: "snapshot"; payload: { symbols: SymbolSnapshot[] } }
  | { type: "process_list"; payload: { processes: ProcessInfo[] } }
  | { type: "gateway"; payload: GatewayStatus }
  | { type: "tick"; payload: unknown }
  | { type: "log"; payload: { key: string; line: string } }
  | { type: "error"; payload: { where: string; error: string } };
