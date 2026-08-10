/**
 * Session control panel — Stop All / Restore Session / Save Session.
 *
 * Sits at the top of the Bots tab. The flow this implements:
 *
 *   4 pm ET   → click "Stop All"        → every running bot gets
 *                                          SIGTERM. State files are
 *                                          flushed by the engine on
 *                                          its way out. GTC stop-limits
 *                                          remain at IBKR so any open
 *                                          position is protected
 *                                          overnight.
 *   9:30 am → click "Restore Session" → every bot in .gt_session.json
 *                                          that isn't already running
 *                                          gets relaunched with the
 *                                          same flags. The engine's
 *                                          state-recovery + reconcile
 *                                          flow takes over from there.
 *
 * Both buttons use an inline confirm step instead of a modal so the
 * action stays one-handed and the user can read the consequence next
 * to the button before committing.
 */
import clsx from "clsx";
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import {
  demoRestoreSession, demoSavedSession, demoSaveSession, demoStopAll,
} from "../lib/demo";
import { fmt } from "../lib/fmt";
import { getState } from "../lib/store";
import type { ProcessInfo, SavedSession } from "../lib/types";
import { Icon } from "./Icon";

type Pending = null | "stop" | "restore" | "save";

export function SessionPanel({ processes }: { processes: ProcessInfo[] }) {
  const [saved, setSaved] = useState<SavedSession | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const isDemo = getState().source === "demo";
  const running = processes.filter((p) => p.status === "running");

  // Refresh the saved-session view whenever the process list changes —
  // auto-save runs on the backend per launch, so the file may have
  // grown since the last render. In demo we read localStorage; both
  // paths land here uniformly.
  const refresh = useCallback(async () => {
    try {
      if (isDemo) {
        const s = demoSavedSession();
        setSaved(s as SavedSession | null);
      } else {
        const s = await api.savedSession();
        setSaved(s);
      }
    } catch {
      /* a missing session file is normal — leave saved=null */
    }
  }, [isDemo]);

  useEffect(() => { refresh(); }, [refresh, processes]);

  async function doStop(force: boolean) {
    setBusy(true); setMsg(null);
    try {
      const r = isDemo ? demoStopAll() : await api.stopAll(force);
      const n = r.stopped.length;
      setMsg({ kind: "ok",
        text: n === 0
          ? "No running bots to stop."
          : `Stopped ${n} bot${n === 1 ? "" : "s"}. Session preserved — Restore brings them back.`,
      });
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message ?? String(e) });
    } finally { setBusy(false); setPending(null); }
  }

  async function doRestore() {
    setBusy(true); setMsg(null);
    try {
      const r = isDemo
        ? demoRestoreSession()
        : await api.restoreSession();
      if (r.error) {
        setMsg({ kind: "err", text: r.error });
      } else {
        const launched = r.launched.length;
        const skipped = r.skipped.length;
        setMsg({
          kind: "ok",
          text: `Launched ${launched}${skipped ? `, skipped ${skipped} (already running or error)` : ""}.`,
        });
      }
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message ?? String(e) });
    } finally { setBusy(false); setPending(null); }
  }

  async function doSave() {
    setBusy(true); setMsg(null);
    try {
      const s = isDemo ? demoSaveSession() : await api.saveSession();
      setMsg({ kind: "ok", text: `Saved ${s.bots.length} bot${s.bots.length === 1 ? "" : "s"} to session.` });
      setSaved(s as SavedSession);
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message ?? String(e) });
    } finally { setBusy(false); setPending(null); }
  }

  return (
    <div className="rounded-xl border border-ink-800 bg-ink-900/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-200">
            <Icon name="layers" size={14} className="text-teal" /> Session
          </div>
          <div className="mt-1 text-2xs text-ink-600">
            Active: <span className="font-mono text-zinc-300">{running.length}</span>
            <span className="mx-1.5 text-ink-800">·</span>
            Saved: <span className="font-mono text-zinc-300">{saved?.bots.length ?? 0}</span>
            {saved && (
              <>
                <span className="mx-1.5 text-ink-800">·</span>
                Snapshot {fmt.ago(saved.saved_at)} ago
              </>
            )}
          </div>
        </div>

        {/* Action buttons — confirm-on-click pattern. First click sets
            `pending`, second click in the same area commits. */}
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            disabled={busy || running.length === 0}
            onClick={() => (pending === "stop" ? doStop(false) : setPending("stop"))}
            className={clsx(
              "flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-2xs font-medium transition-colors disabled:opacity-40",
              pending === "stop"
                ? "border-ask bg-ask text-ink-950"
                : "border-ask/30 bg-ask/5 text-ask hover:bg-ask/10",
            )}
            title={running.length === 0 ? "No running bots" : "SIGTERM every running bot"}
          >
            <Icon name="stop" size={12} />
            {pending === "stop" ? `Confirm stop ${running.length}` : "Stop All"}
          </button>

          <button
            disabled={busy || !saved || saved.bots.length === 0}
            onClick={() => (pending === "restore" ? doRestore() : setPending("restore"))}
            className={clsx(
              "flex items-center gap-1 rounded-md border px-2.5 py-1.5 text-2xs font-medium transition-colors disabled:opacity-40",
              pending === "restore"
                ? "border-bid bg-bid text-ink-950"
                : "border-bid/30 bg-bid/5 text-bid hover:bg-bid/10",
            )}
            title={!saved ? "No saved session yet" : "Relaunch saved bots that aren't already running"}
          >
            <Icon name="play" size={12} />
            {pending === "restore" ? `Confirm restore ${saved?.bots.length ?? 0}` : "Restore Session"}
          </button>

          <button
            disabled={busy || running.length === 0}
            onClick={doSave}
            className="flex items-center gap-1 rounded-md border border-ink-800 px-2.5 py-1.5 text-2xs font-medium text-ink-600 hover:text-zinc-300 disabled:opacity-40"
            title="Overwrite the saved session with the currently-running bots"
          >
            <Icon name="check" size={12} />
            Save Session
          </button>

          {pending && (
            <button
              onClick={() => setPending(null)}
              className="rounded-md p-1 text-ink-600 hover:text-zinc-300"
              title="Cancel"
            >
              <Icon name="x" size={12} />
            </button>
          )}
        </div>
      </div>

      {msg && (
        <div className={clsx(
          "mt-2 rounded-md border px-2 py-1.5 text-2xs",
          msg.kind === "ok"
            ? "border-bid/30 bg-bid/5 text-bid"
            : "border-ask/30 bg-ask/5 text-ask",
        )}>{msg.text}</div>
      )}

      {/* Preview of what Restore will do — only shown when there's
          something saved that isn't currently running. */}
      {saved && saved.bots.length > 0 && (
        <SavedDiff saved={saved} running={running} />
      )}
    </div>
  );
}

function SavedDiff({
  saved, running,
}: { saved: SavedSession; running: ProcessInfo[] }) {
  const runningKeys = new Set(running.map((r) => r.key));
  const willLaunch = saved.bots.filter(
    (b) => !runningKeys.has(`${b.symbol}_${b.client_id}`),
  );
  if (willLaunch.length === 0) {
    return (
      <div className="mt-2 text-2xs text-ink-600">
        All saved bots are currently running. Restore is a no-op.
      </div>
    );
  }
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-2xs text-ink-600 hover:text-zinc-300">
        Restore would launch {willLaunch.length} bot{willLaunch.length === 1 ? "" : "s"} →
      </summary>
      <div className="mt-1.5 space-y-1">
        {willLaunch.map((b) => (
          <div key={`${b.symbol}_${b.client_id}`}
            className="flex items-center justify-between rounded-md border border-ink-800 bg-ink-950/60 px-2 py-1 font-mono text-2xs">
            <span className="text-zinc-200">{b.symbol} <span className="text-ink-600">c{b.client_id}</span></span>
            <span className="text-ink-600">
              qty {b.qty} · trigger ${b.trigger.toFixed(2)} · stop {(b.stop * 100).toFixed(2)}%
              {!b.paper && <span className="ml-1 text-ask">· LIVE</span>}
            </span>
          </div>
        ))}
      </div>
    </details>
  );
}
