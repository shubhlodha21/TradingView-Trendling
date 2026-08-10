/**
 * Bot inspector — the right-side panel of the Bots tab when a bot is
 * selected. Replaces the launcher form so the operator has a single
 * surface that's either "create" or "inspect/manage".
 *
 * Three sections:
 *   1. CONFIG  — parsed from the launch argv (trigger / qty / stop /
 *                offset / port / mode). What the bot was started with;
 *                what Recover will replay verbatim.
 *   2. STATE   — pulled from the matching SymbolSnapshot (entry_price,
 *                stop_loss, position_notional, realized P&L). What the
 *                engine currently thinks the world looks like.
 *   3. ACTIONS — status-aware:
 *                • running → [Stop] (highlighted) + Restart + Force kill
 *                • stopped → [Recover] (highlighted, big & green)
 *   4. LOG     — last N lines of stdout for quick diagnosis.
 *
 * Why this layout: the user's mental model is "select a bot → do
 * something with it". The actions are the point, so they get the most
 * visual weight; configs are above so the operator can sanity-check
 * before clicking Recover (e.g. "this is the right port, right qty").
 */
import clsx from "clsx";
import { fmt } from "../lib/fmt";
import type { ProcessInfo, SymbolSnapshot } from "../lib/types";
import { Icon } from "./Icon";

interface Props {
  proc: ProcessInfo;
  snap: SymbolSnapshot | null;
  busy: boolean;
  onClose: () => void;
  onStop: () => void;
  onForce: () => void;
  onRestart: () => void;
  onRecover: () => void;
}

/** Pull flag values out of an argv list. The bot was launched with
 *  `python run_live.py SYMBOL --trigger X --qty Y ...` so flag values
 *  always live at `argv[indexOfFlag + 1]`. */
function parseCmd(cmd: string[]) {
  const get = (flag: string): string | null => {
    const i = cmd.indexOf(flag);
    return i >= 0 ? cmd[i + 1] : null;
  };
  return {
    trigger: parseFloat(get("--trigger") ?? "0"),
    qty: parseInt(get("--qty") ?? "0", 10),
    stop: parseFloat(get("--stop") ?? "0.01"),
    port: parseInt(get("--port") ?? "0", 10),
    clientId: parseInt(get("--client-id") ?? "0", 10),
    offsetFixed: get("--offset-fixed") ? parseFloat(get("--offset-fixed")!) : null,
    uvloop: cmd.includes("--uvloop"),
  };
}

export function BotInspector({
  proc, snap, busy, onClose,
  onStop, onForce, onRestart, onRecover,
}: Props) {
  const cfg = parseCmd(proc.cmd);
  const running = proc.status === "running";
  const tradeSize = cfg.qty * cfg.trigger;
  const stopPx = cfg.trigger * (1 - cfg.stop);
  const maxLoss = -cfg.qty * cfg.trigger * cfg.stop;

  // Replay-able command — the exact CLI a Recover would spawn.
  const preview = proc.cmd
    .filter((s, i, a) => !(i === 0 && /python/.test(s) === false && a[0].includes("python")))
    .join(" ");

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-ink-800 bg-ink-900/60">
      {/* Header strip — identity + dismiss */}
      <div className="flex items-center justify-between gap-3 border-b border-ink-800 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className={clsx(
            "h-2 w-2 rounded-full",
            running ? "bg-bid animate-pulse" :
            proc.status === "killed" ? "bg-ink-600" :
            proc.status === "exited" ? "bg-ink-600" : "bg-ask",
          )} />
          <div>
            <div className="font-mono text-sm font-semibold text-zinc-100">
              {proc.symbol} <span className="text-2xs font-normal text-ink-600">c{proc.client_id}</span>
            </div>
            <div className="text-2xs text-ink-600">
              pid {proc.pid} · {proc.paper ? "PAPER" : <span className="text-ask">LIVE</span>} · {fmt.ago(proc.started_at)} ago
            </div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="rounded-md p-1.5 text-ink-600 hover:bg-ink-800 hover:text-zinc-300"
          title="Back to launcher"
        >
          <Icon name="x" />
        </button>
      </div>

      {/* ACTIONS — at the top so they're the first thing the eye
          hits. Stop/Recover get the dominant color; supporting
          actions are de-emphasized borders. */}
      <div className="space-y-2 px-3">
        <Heading>Actions</Heading>
        {running ? (
          <>
            <button
              disabled={busy}
              onClick={onStop}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-ask px-3 py-2.5 text-sm font-semibold text-ink-950 hover:bg-ask/90 disabled:opacity-50"
              title="SIGTERM the bot. Session entry preserved — Recover brings it back."
            >
              <Icon name="stop" />
              Close bot · SIGTERM
            </button>
            <div className="grid grid-cols-2 gap-2">
              <button
                disabled={busy}
                onClick={onRestart}
                className="flex items-center justify-center gap-1.5 rounded-md border border-teal/30 bg-teal/5 px-2 py-1.5 text-2xs font-medium text-teal hover:bg-teal/10 disabled:opacity-50"
                title="Stop, wait for state flush, relaunch with same flags"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 12a9 9 0 0 1 15-6.7M21 12a9 9 0 0 1-15 6.7M21 5v5h-5M3 19v-5h5" />
                </svg>
                Restart
              </button>
              <button
                disabled={busy}
                onClick={onForce}
                className="flex items-center justify-center gap-1.5 rounded-md border border-ask/30 bg-ask/5 px-2 py-1.5 text-2xs font-medium text-ask hover:bg-ask/10 disabled:opacity-50"
                title="SIGKILL — only when SIGTERM is hung. State file may be stale."
              >
                <Icon name="x" size={12} />
                Force kill
              </button>
            </div>
          </>
        ) : (
          // Recover button — green + large because it's the *only*
          // meaningful action on a stopped bot.
          <button
            disabled={busy}
            onClick={onRecover}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-bid px-3 py-2.5 text-sm font-semibold text-ink-950 shadow-lg shadow-bid/20 hover:bg-bid/90 disabled:opacity-50"
            title="Relaunch with the same flags. Engine reconciles existing state file + GTC orders."
          >
            <Icon name="play" />
            Recover bot
          </button>
        )}
      </div>

      {/* CONFIG — read-only echo of the launch flags. This is exactly
          what Recover will replay, so showing it here doubles as a
          sanity preview. */}
      <div className="space-y-1 px-3">
        <Heading>Config</Heading>
        <Kv k="Trigger" v={fmt.usd(cfg.trigger)} mono />
        <Kv k="Quantity" v={fmt.int(cfg.qty)} mono />
        <Kv k="Stop %" v={`${(cfg.stop * 100).toFixed(2)}%`} mono />
        <Kv k="Offset $"
          v={cfg.offsetFixed != null ? fmt.usd(cfg.offsetFixed) : <span className="text-ink-600">scaled</span>}
          mono />
        <Kv k="Port" v={String(cfg.port)} mono />
        <Kv k="uvloop" v={cfg.uvloop ? "on" : "off"} mono />
        <Kv k="Mode" v={proc.paper ? "PAPER" : <span className="text-ask">LIVE</span>} mono />
      </div>

      {/* Computed risk preview — nice to see how much you're risking
          if Recover fires a fresh cycle. */}
      <div className="space-y-1 px-3">
        <Heading>Risk preview</Heading>
        <Kv k="Trade size" v={fmt.usd(tradeSize)} mono />
        <Kv k="Stop loss at" v={<span className="text-ask">{fmt.price(stopPx)}</span>} mono />
        <Kv k="Max loss" v={<span className="text-ask">{fmt.usdSigned(maxLoss)}</span>} mono />
      </div>

      {/* STATE — pulled from the live snapshot. Only meaningful if the
          bot is running OR was running recently (the file persists). */}
      <div className="space-y-1 px-3">
        <Heading>State</Heading>
        {snap ? (
          <>
            <Kv
              k="Position"
              v={snap.state.position_open
                ? <span className="text-bid">LONG {fmt.int(snap.state.quantity)} @ {fmt.price(snap.state.entry_price)}</span>
                : <span className="text-ink-600">FLAT</span>}
              mono
            />
            <Kv k="Highest seen" v={fmt.price(snap.state.highest_price)} mono />
            <Kv k="Protective stop"
              v={snap.state.stop_loss
                ? <span className="text-ask">{fmt.price(snap.state.stop_loss)}</span>
                : "—"}
              mono />
            <Kv k="Realized P&L"
              v={<span className={snap.state.pnl >= 0 ? "text-bid" : "text-ask"}>{fmt.usdSigned(snap.state.pnl)}</span>}
              mono />
            <Kv k="Trades today" v={fmt.int(snap.state.trades_today)} mono />
            <Kv k="W / L"
              v={<><span className="text-bid">{snap.state.wins}</span> / <span className="text-ask">{snap.state.losses}</span></>}
              mono />
            <Kv k="Last price" v={fmt.price(snap.live.last)} mono />
          </>
        ) : (
          <div className="text-2xs text-ink-600">
            No live snapshot yet — the bot may have died before its first state flush.
          </div>
        )}
      </div>

      {/* LOG TAIL — last 8 lines, monospace. Bot's stdout is captured
          by process_manager.py and ring-buffered at LOG_TAIL_LINES. */}
      <div className="space-y-1 px-3 pb-3">
        <Heading>Log tail</Heading>
        <div className="max-h-32 overflow-y-auto rounded-md border border-ink-800 bg-ink-950/60 p-2 font-mono text-2xs leading-relaxed text-zinc-400">
          {proc.log_tail.length === 0 ? (
            <span className="text-ink-600">No log output yet.</span>
          ) : proc.log_tail.slice(-8).map((line, i) => (
            <div key={i} className="whitespace-pre-wrap break-words">{line}</div>
          ))}
        </div>
        <details className="pt-1 text-2xs text-ink-600">
          <summary className="cursor-pointer hover:text-zinc-300">Replay command</summary>
          <code className="mt-1 block break-all rounded border border-ink-800 bg-ink-950/60 p-2 font-mono text-2xs text-zinc-400">
            {preview}
          </code>
        </details>
      </div>
    </div>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-2xs font-semibold uppercase tracking-wider text-ink-600">
      {children}
    </div>
  );
}

function Kv({ k, v, mono }: { k: string; v: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2 text-2xs">
      <span className="text-ink-600">{k}</span>
      <span className={clsx("text-zinc-200 num", mono && "font-mono")}>{v}</span>
    </div>
  );
}
