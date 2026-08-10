/**
 * Top navigation bar.
 *
 * Left:   logo + the primary nav (Trade / Vault / Portfolio / etc.) —
 *         icons match the inspiration screenshot. Trade is highlighted
 *         because that's the only route we ship today; the rest are
 *         placeholders with `disabled` state so the next iteration can
 *         wire them without re-laying out.
 *
 * Center: the gateway-health badge — green dot when TWS is reachable,
 *         red with the error message otherwise. This is the single most
 *         important "is the system working" signal, so it lives in the
 *         top bar where every screen surfaces it.
 *
 * Right:  WS connection state + settings stub.
 */
import clsx from "clsx";
import { Icon, type IconName } from "./Icon";
import type { GatewayStatus } from "../lib/types";
import { fmt } from "../lib/fmt";

const NAV: { id: string; label: string; icon: IconName; active?: boolean }[] = [
  { id: "trade", label: "Trade", icon: "trade", active: true },
  { id: "vault", label: "Vault", icon: "vault" },
  { id: "portfolio", label: "Portfolio", icon: "portfolio" },
  { id: "referral", label: "Referrals", icon: "referral" },
  { id: "leaderboard", label: "Leaderboard", icon: "leaderboard" },
];

export function TopBar({
  gateway,
  wsConnected,
}: {
  gateway: GatewayStatus | null;
  wsConnected: boolean;
}) {
  return (
    <header className="relative flex h-14 items-center justify-between border-b border-ink-800 bg-ink-950/60 px-4 backdrop-blur">
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-teal/40 to-transparent" />

      <div className="flex items-center gap-2">
        <Logo />
        <nav className="ml-2 flex items-center gap-1">
          {NAV.map((n) => (
            <button
              key={n.id}
              disabled={!n.active}
              className={clsx(
                "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors",
                n.active
                  ? "bg-ink-800/80 text-zinc-100 ring-1 ring-inset ring-teal/30"
                  : "text-ink-600 hover:text-zinc-300 disabled:cursor-not-allowed",
              )}
              title={n.active ? n.label : `${n.label} (coming soon)`}
            >
              <Icon name={n.icon} className={n.active ? "text-teal" : ""} />
              {n.active && <span>{n.label}</span>}
            </button>
          ))}
        </nav>
      </div>

      <div className="flex items-center gap-2">
        <GatewayBadge gw={gateway} />
        <WSBadge connected={wsConnected} />
        <button className="rounded-lg border border-ink-800 p-2 text-ink-600 transition-colors hover:text-zinc-300">
          <Icon name="globe" />
        </button>
        <button className="rounded-lg border border-ink-800 p-2 text-ink-600 transition-colors hover:text-zinc-300">
          <Icon name="settings" />
        </button>
      </div>
    </header>
  );
}

function Logo() {
  return (
    <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-ink-800 bg-ink-900">
      <svg viewBox="0 0 24 24" width={18} height={18} fill="none" stroke="#3fd0c9" strokeWidth={2}>
        <path d="M4 18L12 4l8 14M7 14h10" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </div>
  );
}

function GatewayBadge({ gw }: { gw: GatewayStatus | null }) {
  const ok = !!gw?.reachable;
  return (
    <div
      className={clsx(
        "flex items-center gap-2 rounded-lg border px-3 py-1.5 text-2xs font-medium",
        ok
          ? "border-bid/30 bg-bid/5 text-bid"
          : "border-ask/30 bg-ask/5 text-ask",
      )}
      title={
        gw
          ? `${gw.host}:${gw.port} · last checked ${fmt.ago(gw.last_checked)} ago${
              gw.error ? ` · ${gw.error}` : ""
            }`
          : "Probing TWS gateway…"
      }
    >
      <span
        className={clsx(
          "h-2 w-2 rounded-full",
          ok ? "bg-bid" : "bg-ask animate-pulse",
        )}
      />
      <span className="uppercase tracking-wide">
        {ok ? "TWS Online" : "TWS Offline"}
      </span>
      {gw && (
        <span className="hidden text-ink-600 sm:inline">
          {gw.host}:{gw.port}
        </span>
      )}
    </div>
  );
}

function WSBadge({ connected }: { connected: boolean }) {
  return (
    <div
      className={clsx(
        "flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-2xs",
        connected
          ? "border-ink-800 text-ink-600"
          : "border-ask/30 bg-ask/5 text-ask",
      )}
      title={connected ? "WebSocket connected" : "WebSocket disconnected — retrying"}
    >
      <span
        className={clsx(
          "h-1.5 w-1.5 rounded-full",
          connected ? "bg-teal" : "bg-ask animate-pulse",
        )}
      />
      <span>{connected ? "Live" : "Reconnecting"}</span>
    </div>
  );
}
