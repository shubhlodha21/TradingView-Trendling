/**
 * Bot launcher form — the GT-specific surface that replaces typing
 * the long `python run_live.py NFLX --trigger 89.60 …` command 100x
 * a day. Every field maps to a flag in webapp/backend/schemas.py
 * `LaunchRequest`, which in turn maps to the run_live.py argparse.
 *
 * UX rules:
 *   * Default to LIVE mode (paper=false) because that's the actual
 *     production use case. A red badge above the submit button shows
 *     "LIVE TRADING" so the operator can't miss it.
 *   * Show the constructed command as a `<code>` block as the user
 *     fills the form — the same hyperlinked copy-paste they'd run
 *     manually if the webapp went down. Trust through transparency.
 *   * Refuse to submit (frontend + backend both) if (symbol, client_id)
 *     already running — saves the operator a surprise "client_id in use"
 *     error from IBKR mid-day.
 */
import clsx from "clsx";
import { useMemo, useState } from "react";
import { api } from "../lib/api";
import type { LaunchRequest } from "../lib/types";
import { Icon } from "./Icon";

interface Props {
  initialSymbol?: string;
}

export function BotLauncher({ initialSymbol = "" }: Props) {
  const [symbol, setSymbol] = useState(initialSymbol);
  const [trigger, setTrigger] = useState("");
  const [qty, setQty] = useState("100");
  const [stop, setStop] = useState("0.01");
  const [port, setPort] = useState("7496");
  const [clientId, setClientId] = useState("1");
  const [paper, setPaper] = useState(false);
  const [uvloop, setUvloop] = useState(true);
  const [offsetFixed, setOffsetFixed] = useState("0.20");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<string | null>(null);

  const req = useMemo<LaunchRequest | null>(() => {
    const t = parseFloat(trigger);
    const q = parseInt(qty, 10);
    const s = parseFloat(stop);
    const p = parseInt(port, 10);
    const cid = parseInt(clientId, 10);
    const of = parseFloat(offsetFixed);
    if (!symbol || !t || !q || !s || !p || isNaN(cid)) return null;
    return {
      symbol: symbol.toUpperCase(),
      trigger: t, qty: q, stop: s, port: p, client_id: cid,
      paper, uvloop,
      offset_fixed: !isNaN(of) ? of : null,
    };
  }, [symbol, trigger, qty, stop, port, clientId, paper, uvloop, offsetFixed]);

  // Live preview of the exact CLI the backend will spawn.
  const preview = useMemo(() => {
    if (!req) return "";
    const parts: string[] = [
      `GT_PAPER=${req.paper ? "true" : "false"}`,
      "python", "run_live.py", req.symbol,
      "--trigger", String(req.trigger),
      "--qty", String(req.qty),
      "--stop", String(req.stop),
      "--port", String(req.port),
      "--client-id", String(req.client_id),
    ];
    if (req.uvloop) parts.push("--uvloop");
    if (req.offset_fixed != null) parts.push("--offset-fixed", String(req.offset_fixed));
    return parts.join(" ");
  }, [req]);

  async function submit() {
    if (!req) return;
    setSubmitting(true);
    setError(null);
    try {
      const info = await api.launch(req);
      setLast(`Launched ${info.key} (pid ${info.pid})`);
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="rounded-xl border border-ink-800 bg-ink-900/60">
      <div className="border-b border-ink-800 px-3 py-2 text-2xs font-semibold uppercase tracking-wide text-ink-600">
        Launch bot
      </div>
      <div className="space-y-2 p-3">
        <div className="grid grid-cols-2 gap-2">
          <Field label="Symbol">
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              className="input"
              placeholder="NFLX"
              maxLength={8}
            />
          </Field>
          <Field label="Trigger price">
            <input
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              className="input"
              inputMode="decimal"
              placeholder="89.60"
            />
          </Field>
          <Field label="Quantity">
            <input
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              className="input"
              inputMode="numeric"
            />
          </Field>
          <Field label="Stop %">
            <input
              value={stop}
              onChange={(e) => setStop(e.target.value)}
              className="input"
              inputMode="decimal"
              placeholder="0.01"
            />
          </Field>
          <Field label="Port">
            <input
              value={port}
              onChange={(e) => setPort(e.target.value)}
              className="input"
              inputMode="numeric"
            />
          </Field>
          <Field label="Client ID">
            <input
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
              className="input"
              inputMode="numeric"
            />
          </Field>
          <Field label="Offset fixed ($)">
            <input
              value={offsetFixed}
              onChange={(e) => setOffsetFixed(e.target.value)}
              className="input"
              inputMode="decimal"
              placeholder="0.20"
            />
          </Field>
          <div className="flex items-end gap-3 pb-1">
            <label className="flex cursor-pointer items-center gap-1.5 text-2xs text-ink-600">
              <input type="checkbox" checked={uvloop} onChange={(e) => setUvloop(e.target.checked)} className="accent-teal" />
              uvloop
            </label>
            <label className="flex cursor-pointer items-center gap-1.5 text-2xs text-ink-600">
              <input type="checkbox" checked={paper} onChange={(e) => setPaper(e.target.checked)} className="accent-teal" />
              paper
            </label>
          </div>
        </div>

        {/* Constructed-command preview */}
        <div className="rounded-md border border-ink-800 bg-ink-950/60 p-2">
          <div className="mb-1 text-2xs uppercase tracking-wide text-ink-600">Command preview</div>
          <code className="block break-all font-mono text-2xs text-zinc-400">
            {preview || "—"}
          </code>
        </div>

        {!paper && (
          <div className="flex items-center gap-2 rounded-md border border-ask/30 bg-ask/5 px-2 py-1.5 text-2xs text-ask">
            <Icon name="warning" />
            <span>LIVE TRADING — real money will move when the trigger fires.</span>
          </div>
        )}

        {error && (
          <div className="rounded-md border border-ask/30 bg-ask/5 px-2 py-1.5 text-2xs text-ask">
            {error}
          </div>
        )}
        {last && !error && (
          <div className="rounded-md border border-bid/30 bg-bid/5 px-2 py-1.5 text-2xs text-bid">
            {last}
          </div>
        )}

        <button
          disabled={!req || submitting}
          onClick={submit}
          className={clsx(
            "flex w-full items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold transition-colors",
            paper
              ? "bg-teal/90 text-ink-950 hover:bg-teal disabled:opacity-50"
              : "bg-bid text-ink-950 hover:bg-bid/90 disabled:opacity-50",
          )}
        >
          <Icon name="play" />
          {submitting ? "Launching…" : `Launch ${paper ? "paper" : "LIVE"} bot`}
        </button>
      </div>

      <style>{`
        .input {
          width: 100%; background: rgba(7,10,12,0.7);
          border: 1px solid #161f28; border-radius: 0.375rem;
          padding: 0.375rem 0.5rem; font-family: 'JetBrains Mono', ui-monospace, monospace;
          font-size: 0.75rem; color: #e4e4e7; outline: none;
        }
        .input:focus { border-color: rgba(63,208,201,0.5); }
      `}</style>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-2xs uppercase tracking-wide text-ink-600">{label}</span>
      {children}
    </label>
  );
}
