/**
 * The tabbed pane below the chart:
 *   Balances · Positions · Open Orders · Order History · Bots
 *
 * Tab content is pure data-mapping over the SymbolSnapshot[] +
 * ProcessInfo[] we already get over WS — no per-tab network call.
 *
 * "Bots" tab is the GT-specific addition: it's the launch + manage
 * surface for `run_live.py` subprocesses. Lives here (vs a modal)
 * so the operator's eye doesn't context-switch between "what's
 * running" and "what positions does it have".
 */
import clsx from "clsx";
import { useEffect, useState } from "react";
import { fmt } from "../lib/fmt";
import type { ProcessInfo, SymbolSnapshot } from "../lib/types";
import { BotInspector } from "./BotInspector";
import { BotLauncher } from "./BotLauncher";
import { Icon } from "./Icon";
import { SessionPanel } from "./SessionPanel";
import { api } from "../lib/api";
import { demoRestoreOne } from "../lib/demo";
import { getState } from "../lib/store";

type TabId = "balances" | "positions" | "open" | "history" | "bots";

const TABS: { id: TabId; label: string }[] = [
  { id: "balances", label: "Balances" },
  { id: "positions", label: "Positions" },
  { id: "open", label: "Open Orders" },
  { id: "history", label: "Order History" },
  { id: "bots", label: "Bots" },
];

export function BottomTabs({
  symbols,
  processes,
  selectedKey,
}: {
  symbols: SymbolSnapshot[];
  processes: ProcessInfo[];
  selectedKey: string | null;
}) {
  const [tab, setTab] = useState<TabId>("positions");
  // `min-h-0` + `flex-1` lets us inherit the height the parent column
  // allocates, instead of growing to fit content — the body's own
  // `overflow-auto` then handles tall tables and the Bots view.
  return (
    <div className="panel flex min-h-0 w-full flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b border-ink-800 px-3 pt-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={clsx("tab", tab === t.id && "tab-active")}
          >
            {t.label}
            {t.id === "bots" && (
              <span className="ml-1.5 inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-ink-800 px-1 text-2xs text-ink-600">
                {processes.length}
              </span>
            )}
            {t.id === "positions" && (
              <span className="ml-1.5 inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-ink-800 px-1 text-2xs text-ink-600">
                {symbols.filter((s) => s.state.position_open).length}
              </span>
            )}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {tab === "positions" && <PositionsTable symbols={symbols} />}
        {tab === "balances" && <BalancesTable symbols={symbols} />}
        {tab === "open" && <OpenOrdersStub />}
        {tab === "history" && <OrderHistoryStub />}
        {tab === "bots" && <BotsPanel symbols={symbols} processes={processes} selectedKey={selectedKey} />}
      </div>
    </div>
  );
}

// ── Positions table ─────────────────────────────────────────────────
function PositionsTable({ symbols }: { symbols: SymbolSnapshot[] }) {
  const open = symbols.filter((s) => s.state.position_open);
  if (open.length === 0) return <Empty label="No open positions." />;
  return (
    <table className="w-full text-xs">
      <thead className="sticky top-0 bg-ink-900/95 text-2xs uppercase tracking-wide text-ink-600">
        <tr>
          <Th>Symbol</Th>
          <Th right>Qty</Th>
          <Th right>Entry</Th>
          <Th right>Last</Th>
          <Th right>Stop</Th>
          <Th right>Notional</Th>
          <Th right>Unreal P&L</Th>
          <Th right>Realized</Th>
          <Th right>W/L</Th>
          <Th right>Trades</Th>
          <Th>Bot</Th>
        </tr>
      </thead>
      <tbody className="font-mono num">
        {open.map((s) => {
          const entry = s.state.entry_price ?? 0;
          const last = s.live.last;
          const qty = s.state.quantity;
          const unreal = last > 0 && entry > 0 ? (last - entry) * qty : 0;
          return (
            <tr key={s.key} className="border-t border-ink-800 hover:bg-ink-800/40">
              <Td><span className="text-zinc-100">{s.symbol}</span></Td>
              <Td right>{fmt.int(qty)}</Td>
              <Td right>{fmt.price(entry)}</Td>
              <Td right>{fmt.price(last)}</Td>
              <Td right className="text-ask">{fmt.price(s.state.stop_loss)}</Td>
              <Td right>{fmt.usd(s.live.position_notional)}</Td>
              <Td right className={unreal >= 0 ? "text-bid" : "text-ask"}>{fmt.usdSigned(unreal)}</Td>
              <Td right className={s.state.pnl >= 0 ? "text-bid" : "text-ask"}>{fmt.usdSigned(s.state.pnl)}</Td>
              <Td right>
                <span className="text-bid">{s.state.wins}</span>/<span className="text-ask">{s.state.losses}</span>
              </Td>
              <Td right>{s.state.trades_today}</Td>
              <Td><span className="text-2xs text-ink-600">c{s.client_id}</span></Td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function BalancesTable({ symbols }: { symbols: SymbolSnapshot[] }) {
  const first = symbols.find((s) => s.live.equity > 0);
  if (!first) return <Empty label="Equity not yet reported by any running bot." />;
  const grossNotional = symbols.reduce((acc, s) => acc + s.live.position_notional, 0);
  const realized = symbols.reduce((acc, s) => acc + s.state.pnl, 0);
  return (
    <div className="grid grid-cols-2 gap-x-8 gap-y-4 p-4 sm:grid-cols-3 lg:grid-cols-4">
      <Stat label="Equity" value={fmt.usd(first.live.equity)} />
      <Stat label="Buying Power" value={fmt.usd(first.live.buying_power)} />
      <Stat label="Gross Notional" value={fmt.usd(grossNotional)} />
      <Stat label="Exposure" value={`${((grossNotional / first.live.equity) * 100).toFixed(2)}%`} />
      <Stat label="Realized P&L (today)" value={fmt.usdSigned(realized)} tone={realized >= 0 ? "bid" : "ask"} />
      <Stat label="Running bots" value={symbols.filter((s) => s.is_active).length.toString()} />
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "bid" | "ask" }) {
  return (
    <div>
      <div className="text-2xs uppercase tracking-wide text-ink-600">{label}</div>
      <div className={clsx(
        "mt-0.5 text-base font-mono num",
        tone === "bid" && "text-bid",
        tone === "ask" && "text-ask",
        !tone && "text-zinc-100",
      )}>{value}</div>
    </div>
  );
}

function OpenOrdersStub() {
  return <Empty label="Live working-order stream lands when the IBKR proxy is wired (see ibkr_proxy.py)." />;
}

function OrderHistoryStub() {
  return <Empty label="Reads data/audit/order_<SYM>_<DATE>.csv — pending the next iteration's audit reader." />;
}

function Empty({ label }: { label: string }) {
  return (
    <div className="flex h-full min-h-[14rem] items-center justify-center px-6 text-center text-xs text-ink-600">
      {label}
    </div>
  );
}

function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return <th className={clsx("px-3 py-1.5 font-medium", right ? "text-right" : "text-left")}>{children}</th>;
}
function Td({ children, right, className }: { children: React.ReactNode; right?: boolean; className?: string }) {
  return <td className={clsx("px-3 py-1.5", right ? "text-right" : "text-left", className)}>{children}</td>;
}

// ── Bots tab (launcher + process list + inspector) ──────────────────
function BotsPanel({
  symbols, processes, selectedKey,
}: {
  symbols: SymbolSnapshot[]; processes: ProcessInfo[]; selectedKey: string | null;
}) {
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Which bot row is being inspected on the right side. null = show the
  // launcher (new-bot form). Selecting a row swaps the right pane to
  // the inspector view; clicking the inspector's × returns to launcher.
  const [inspectKey, setInspectKey] = useState<string | null>(null);
  const selectedSymbol = symbols.find((s) => s.key === selectedKey)?.symbol ?? "";

  // Keep the inspected bot reactive — when a new processes[] frame
  // arrives via WS/demo, we pluck the latest ProcessInfo by key so
  // the inspector updates in real time (status flips, log_tail grows).
  const inspected = inspectKey
    ? processes.find((p) => p.key === inspectKey) ?? null
    : null;
  // If the inspected row vanishes (e.g. forgot-on-kill), drop selection
  // and fall back to the launcher rather than show an empty inspector.
  useEffect(() => {
    if (inspectKey && !processes.some((p) => p.key === inspectKey)) {
      setInspectKey(null);
    }
  }, [processes, inspectKey]);

  // Matching live snapshot for the inspected bot — same symbol +
  // client_id. The inspector uses this for the State section
  // (position, entry, stop_loss, P&L).
  const inspectedSnap = inspected
    ? symbols.find((s) => s.key === inspected.key) ?? null
    : null;

  async function kill(key: string, force = false) {
    setBusyKey(key);
    setError(null);
    try {
      await api.kill(key, force);
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setBusyKey(null);
    }
  }

  /** Per-bot recover: relaunches a single bot from its saved session
      entry. Works on `killed` / `exited` rows. In demo mode we mirror
      the same contract via demoRestoreOne so the UX is identical. */
  async function recover(key: string) {
    setBusyKey(key);
    setError(null);
    try {
      if (getState().source === "demo") demoRestoreOne(key);
      else await api.restoreOne(key);
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setBusyKey(null);
    }
  }

  /** Restart in place: graceful stop, then recover from the saved
      session entry. Convenience for "the bot is misbehaving, bounce
      it" without losing its session row. The engine's signal handler
      flushes state on SIGTERM so the relaunch's reconcile pass picks
      up exactly where the previous instance left off. */
  async function restart(key: string) {
    setBusyKey(key);
    setError(null);
    try {
      if (getState().source === "demo") {
        // Demo: flip to killed and immediately resurrect via the
        // same recover path. demoRestoreOne handles either case.
        demoRestoreOne(key);
      } else {
        await api.kill(key, false);
        // 250ms grace so the engine's signal handler can flush its
        // state file before we relaunch — that's how recovery works.
        await new Promise((r) => setTimeout(r, 250));
        await api.restoreOne(key);
      }
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <div className="space-y-3 p-3">
      {/* Session controls — Stop All / Restore Session / Save Session.
          Lives at the top of the Bots tab because it's the operator's
          most frequent action: kill at market close, restart at open. */}
      <SessionPanel processes={processes} />

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_28rem]">
      <div>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-600">Running bots</h3>
          <div className="flex items-center gap-2">
            {/* Quick toggle: when inspecting a bot, clicking + New goes
                back to the launcher form. Visible at all times so the
                operator can always reach "create new bot" in one click. */}
            <button
              onClick={() => setInspectKey(null)}
              className={clsx(
                "flex items-center gap-1 rounded-md border px-2 py-1 text-2xs font-medium transition-colors",
                inspectKey == null
                  ? "border-teal/40 bg-teal/10 text-teal"
                  : "border-ink-800 text-ink-600 hover:text-zinc-300",
              )}
              title="Show the new-bot launcher in the right pane"
            >
              <Icon name="plus" size={11} /> New
            </button>
            <span className="text-2xs text-ink-600">{processes.length} total</span>
          </div>
        </div>
        {processes.length === 0 ? (
          <Empty label="No bots launched yet. Use the launcher on the right →" />
        ) : (
          <div className="space-y-2">
            {processes.map((p) => (
              // Rows are clickable to inspect. The kill/recover buttons
              // below use stopPropagation so the click on them doesn't
              // double as a row-select. Selected row gets a teal ring
              // so the visual link to the right pane is obvious.
              <div
                key={p.key}
                onClick={() => setInspectKey(p.key)}
                className={clsx(
                  "flex cursor-pointer items-center justify-between gap-3 rounded-lg border bg-ink-900/60 px-3 py-2 transition-colors",
                  inspectKey === p.key
                    ? "border-teal/50 ring-1 ring-inset ring-teal/40"
                    : "border-ink-800 hover:border-ink-700 hover:bg-ink-900",
                )}
              >
                <div className="flex items-center gap-3">
                  <span className={clsx(
                    "h-2 w-2 rounded-full",
                    p.status === "running" ? "bg-bid animate-pulse" :
                    p.status === "killed" ? "bg-ink-600" :
                    p.status === "exited" ? "bg-ink-600" : "bg-ask",
                  )} />
                  <div>
                    <div className="font-mono text-sm text-zinc-100">{p.symbol} <span className="text-2xs text-ink-600">c{p.client_id}</span></div>
                    <div className="text-2xs text-ink-600">pid {p.pid} · port {p.port} · {p.paper ? "PAPER" : "LIVE"} · {fmt.ago(p.started_at)} ago</div>
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <span className={clsx(
                    "rounded-md px-1.5 py-0.5 text-2xs uppercase",
                    p.status === "running" ? "bg-bid/10 text-bid" :
                    p.status === "killed"  ? "bg-ink-800 text-ink-600" :
                    p.status === "exited"  ? "bg-ink-800 text-ink-600" :
                                              "bg-ask/10 text-ask",
                  )}>{p.status}</span>

                  {p.status === "running" ? (
                    <>
                      {/* Stop = SIGTERM, leaves the bot in the saved
                          session so Recover/Restore will bring it back. */}
                      <button
                        disabled={busyKey === p.key}
                        onClick={(e) => { e.stopPropagation(); kill(p.key, false); }}
                        className="rounded-md border border-ink-800 p-1.5 text-ink-600 hover:border-ask/40 hover:text-ask disabled:opacity-50"
                        title="Stop (SIGTERM) — session entry preserved"
                      >
                        <Icon name="stop" />
                      </button>
                      {/* Restart = stop + recover. Use when the engine's
                          state has drifted and you want a clean reconcile
                          pass without re-typing the launch command. */}
                      <button
                        disabled={busyKey === p.key}
                        onClick={(e) => { e.stopPropagation(); restart(p.key); }}
                        className="rounded-md border border-ink-800 p-1.5 text-ink-600 hover:border-teal/40 hover:text-teal disabled:opacity-50"
                        title="Restart (stop then relaunch with saved flags)"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M3 12a9 9 0 0 1 15-6.7M21 12a9 9 0 0 1-15 6.7M21 5v5h-5M3 19v-5h5" />
                        </svg>
                      </button>
                      <button
                        disabled={busyKey === p.key}
                        onClick={(e) => { e.stopPropagation(); kill(p.key, true); }}
                        className="rounded-md border border-ink-800 p-1.5 text-ink-600 hover:border-ask/40 hover:text-ask disabled:opacity-50"
                        title="Force kill (SIGKILL)"
                      >
                        <Icon name="x" />
                      </button>
                    </>
                  ) : (
                    /* Recover = relaunch this one bot from .gt_session.json.
                       Does NOT touch any peer bots — single-row equivalent
                       of "Restore Session". */
                    <button
                      disabled={busyKey === p.key}
                      onClick={(e) => { e.stopPropagation(); recover(p.key); }}
                      className="flex items-center gap-1 rounded-md border border-bid/30 bg-bid/5 px-2 py-1 text-2xs font-medium text-bid hover:bg-bid/10 disabled:opacity-50"
                      title="Recover this bot — relaunches with its saved flags, reconciles existing state file + GTC orders"
                    >
                      <Icon name="play" size={11} /> Recover
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        {error && (
          <div className="mt-2 rounded-md border border-ask/30 bg-ask/5 p-2 text-2xs text-ask">{error}</div>
        )}
      </div>
      {/* Right pane: switches based on selection.
          • No selection → BotLauncher (create new bot)
          • Row selected → BotInspector (configs / state / actions
            for THAT bot specifically). */}
      {inspected ? (
        <BotInspector
          proc={inspected}
          snap={inspectedSnap}
          busy={busyKey === inspected.key}
          onClose={() => setInspectKey(null)}
          onStop={() => kill(inspected.key, false)}
          onForce={() => kill(inspected.key, true)}
          onRestart={() => restart(inspected.key)}
          onRecover={() => recover(inspected.key)}
        />
      ) : (
        <BotLauncher initialSymbol={selectedSymbol} />
      )}
      </div>
    </div>
  );
}
