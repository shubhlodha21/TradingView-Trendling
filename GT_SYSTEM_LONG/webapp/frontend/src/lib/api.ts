/**
 * Thin REST client. Every function returns a typed Promise of the
 * backend's response shape (from ./types.ts).
 *
 * No retries, no caching, no global error handler — those belong at
 * the call site where context is available (UI can show a toast,
 * background refresher can decide whether to back off).
 */
import type {
  AlertCounts,
  AlertEntry,
  AlertSeverity,
  AuditOrder,
  AuditPnl,
  AuditState,
  GatewayStatus,
  LaunchRequest,
  ManualOrderRequest,
  ManualOrderResponse,
  ProcessInfo,
  RestoreResult,
  SavedSession,
  StopAllResult,
  SymbolSnapshot,
} from "./types";

const BASE = "/api";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!resp.ok) {
    let detail: string;
    try {
      const j = await resp.json();
      detail = j.detail ?? JSON.stringify(j);
    } catch {
      detail = await resp.text();
    }
    throw new Error(`HTTP ${resp.status}: ${detail}`);
  }
  return resp.json();
}

export const api = {
  health: () => http<{ ok: boolean; cwd: string }>("/health"),
  gateway: () => http<GatewayStatus>("/gateway"),
  snapshot: () => http<SymbolSnapshot[]>("/snapshot"),
  listProcesses: () => http<ProcessInfo[]>("/processes"),
  launch: (req: LaunchRequest) =>
    http<ProcessInfo>("/processes", { method: "POST", body: JSON.stringify(req) }),
  kill: (key: string, force = false, forget = false) =>
    http<ProcessInfo>(
      `/processes/${encodeURIComponent(key)}?force=${force}&forget=${forget}`,
      { method: "DELETE" },
    ),
  placeOrder: (req: ManualOrderRequest, dryRun = false) =>
    http<ManualOrderResponse>(`/orders?dry_run=${dryRun}`, {
      method: "POST",
      body: JSON.stringify(req),
    }),
  // Session lifecycle
  savedSession: () => http<SavedSession | null>("/sessions/saved"),
  saveSession: () => http<SavedSession>("/sessions/save", { method: "POST" }),
  restoreSession: () => http<RestoreResult>("/sessions/restore", { method: "POST" }),
  restoreOne: (key: string) =>
    http<ProcessInfo>(`/sessions/restore/${encodeURIComponent(key)}`, { method: "POST" }),
  stopAll: (force = false) =>
    http<StopAllResult>(`/sessions/stop-all?force=${force}`, { method: "POST" }),
  // Audit + alerts
  auditOrders: (key: string, limit = 100) =>
    http<AuditOrder[]>(`/audit/orders/${encodeURIComponent(key)}?limit=${limit}`),
  auditOpenOrders: (key: string) =>
    http<AuditOrder[]>(`/audit/open-orders/${encodeURIComponent(key)}`),
  auditState: (key: string, limit = 50) =>
    http<AuditState[]>(`/audit/state/${encodeURIComponent(key)}?limit=${limit}`),
  auditPnl: (key: string, limit = 2000) =>
    http<AuditPnl[]>(`/audit/pnl/${encodeURIComponent(key)}?limit=${limit}`),
  alerts: (opts?: { limit?: number; severity?: AlertSeverity; symbol?: string }) => {
    const p = new URLSearchParams();
    if (opts?.limit != null) p.set("limit", String(opts.limit));
    if (opts?.severity) p.set("severity", opts.severity);
    if (opts?.symbol) p.set("symbol", opts.symbol);
    const q = p.toString();
    return http<AlertEntry[]>(`/alerts${q ? `?${q}` : ""}`);
  },
  alertCounts: () => http<AlertCounts>("/alerts/counts"),
};
