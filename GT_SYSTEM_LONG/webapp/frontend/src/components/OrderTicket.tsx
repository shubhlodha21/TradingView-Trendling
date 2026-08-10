/**
 * Right-panel trade ticket.
 *
 * Top toggle: [Manual]  [Strategy]
 *   * Manual   → one-shot order ticket (Market/Limit/Pro · Buy/Sell · ...).
 *                Routes through ibkr_proxy (501 today; dry-run works).
 *   * Strategy → deploy a bot (currently just "Breakout / run_live.py")
 *                pre-filled with the selected symbol. Posts to
 *                /api/processes in live mode, or to demo.addDemoBot()
 *                in demo mode so the new bot ticks alongside the
 *                seeded ones.
 *
 * Strategy mode is the answer to "where do I run our breakout on a
 * ticker I'm looking at?" — it lives one click from the chart and
 * defaults to the currently-selected symbol so there's no copy-paste.
 */
import clsx from "clsx";
import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { addDemoBot } from "../lib/demo";
import { fmt } from "../lib/fmt";
import { getState } from "../lib/store";
import type {
  LaunchRequest, ManualOrderRequest, OrderSide, OrderType,
  SymbolSnapshot, TIF,
} from "../lib/types";
import { Icon } from "./Icon";

type Mode = "manual" | "strategy";
type ManualSub = "MARKET" | "LIMIT" | "PRO";

const STRATEGIES = [
  { id: "breakout", label: "Breakout (run_live.py)", available: true,
    blurb: "Stop-limit entry at trigger; protective stop trails the high." },
  { id: "mean_revert", label: "Mean-revert — coming soon", available: false,
    blurb: "Bollinger-band fade with size-on-stretch. Backtest in progress." },
];

const TIFS: TIF[] = ["DAY", "GTC", "IOC"];

interface Props { symbol: SymbolSnapshot | null }

export function OrderTicket({ symbol }: Props) {
  const [mode, setMode] = useState<Mode>("manual");
  return (
    <div className="panel flex flex-col">
      <div className="grid grid-cols-2 border-b border-ink-800">
        {(["manual", "strategy"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setMode(m)}
            className={clsx(
              "flex items-center justify-center gap-1.5 py-2.5 text-xs font-semibold transition-colors",
              mode === m ? "bg-ink-900 text-teal" : "text-ink-600 hover:text-zinc-300",
            )}
          >
            <Icon name={m === "manual" ? "bolt" : "candle"} size={12} />
            {m === "manual" ? "Manual" : "Strategy"}
          </button>
        ))}
      </div>
      {mode === "manual" ? <ManualForm symbol={symbol} /> : <StrategyForm symbol={symbol} />}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// MANUAL — Market / Limit / Pro
// ─────────────────────────────────────────────────────────────────────
function ManualForm({ symbol }: Props) {
  const [sub, setSub] = useState<ManualSub>("MARKET");
  const [side, setSide] = useState<OrderSide>("BUY");
  const [size, setSize] = useState("");
  const [limit, setLimit] = useState("");
  const [stop, setStop] = useState("");
  const [tif, setTif] = useState<TIF>("DAY");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const orderType: OrderType = useMemo(() => {
    if (sub === "MARKET") return "MARKET";
    if (sub === "LIMIT") return "LIMIT";
    if (stop && limit) return "STOP_LIMIT";
    if (stop) return "STOP";
    if (limit) return "LIMIT";
    return "MARKET";
  }, [sub, limit, stop]);

  const refPrice = symbol?.live.last ?? 0;
  const sz = parseInt(size, 10) || 0;
  const lim = parseFloat(limit);
  const stp = parseFloat(stop);
  const notional =
    sz * (orderType === "LIMIT" || orderType === "STOP_LIMIT" ? (lim || refPrice) : refPrice);

  const req = useMemo<ManualOrderRequest | null>(() => {
    if (!symbol || !sz) return null;
    return {
      symbol: symbol.symbol, side, qty: sz, order_type: orderType,
      limit_price: orderType === "LIMIT" || orderType === "STOP_LIMIT" ? (!isNaN(lim) ? lim : null) : null,
      stop_price: orderType === "STOP" || orderType === "STOP_LIMIT" ? (!isNaN(stp) ? stp : null) : null,
      tif, acknowledged_notional: notional,
    };
  }, [symbol, sz, side, orderType, lim, stp, tif, notional]);

  async function submit(dryRun: boolean) {
    if (!req) return;
    setBusy(true); setMsg(null);
    try {
      // In demo mode the backend isn't reachable — fake a dry-run response.
      if (getState().source === "demo") {
        setMsg({ kind: "ok", text: `DEMO · DRY_RUN · ${req.side} ${req.qty} ${req.symbol} ${req.order_type}` });
      } else {
        const resp = await api.placeOrder(req, dryRun);
        setMsg({ kind: "ok", text: `${dryRun ? "DRY " : ""}${resp.status} · ${resp.order_id}` });
      }
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message ?? String(e) });
    } finally { setBusy(false); }
  }

  return (
    <div className="space-y-3 p-3">
      <div className="grid grid-cols-3 gap-1 rounded-lg border border-ink-800 bg-ink-950/60 p-0.5">
        {(["MARKET", "LIMIT", "PRO"] as const).map((m) => (
          <button
            key={m}
            onClick={() => setSub(m)}
            className={clsx(
              "rounded-md py-1.5 text-2xs font-semibold transition-colors",
              sub === m ? "bg-ink-800 text-teal" : "text-ink-600 hover:text-zinc-300",
            )}
          >
            {m === "PRO" ? "Pro" : m.charAt(0) + m.slice(1).toLowerCase()}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-0 rounded-lg border border-ink-800 bg-ink-950/60 p-0.5">
        <button
          onClick={() => setSide("BUY")}
          className={clsx(
            "rounded-md py-2 text-xs font-semibold transition-colors",
            side === "BUY" ? "bg-bid text-ink-950" : "text-ink-600 hover:text-zinc-300",
          )}
        >Buy / Long</button>
        <button
          onClick={() => setSide("SELL")}
          className={clsx(
            "rounded-md py-2 text-xs font-semibold transition-colors",
            side === "SELL" ? "bg-ask text-ink-950" : "text-ink-600 hover:text-zinc-300",
          )}
        >Sell / Short</button>
      </div>

      <KV label="Available to Trade" value={symbol ? fmt.usd(symbol.live.buying_power) : "—"} />
      <KV label="Current Position" value={symbol ? `${fmt.int(symbol.state.quantity)} ${symbol.symbol}` : "—"} />

      <Field label="Size">
        <div className="flex">
          <input
            value={size}
            onChange={(e) => setSize(e.target.value.replace(/[^\d]/g, ""))}
            inputMode="numeric"
            placeholder="0"
            className="input flex-1"
          />
          <span className="flex items-center rounded-r-md border border-l-0 border-ink-800 bg-ink-950 px-3 text-2xs text-ink-600">
            {symbol ? symbol.symbol : "—"}
          </span>
        </div>
      </Field>

      {(sub === "LIMIT" || sub === "PRO") && (
        <Field label={sub === "PRO" ? "Limit price" : "Limit"}>
          <input value={limit} onChange={(e) => setLimit(e.target.value)} inputMode="decimal"
            placeholder={refPrice ? refPrice.toFixed(2) : ""} className="input" />
        </Field>
      )}
      {sub === "PRO" && (
        <Field label="Stop trigger (Stop-Limit)">
          <input value={stop} onChange={(e) => setStop(e.target.value)} inputMode="decimal"
            placeholder="—" className="input" />
        </Field>
      )}
      {sub === "PRO" && (
        <Field label="Time in force">
          <div className="grid grid-cols-3 gap-1">
            {TIFS.map((t) => (
              <button key={t} onClick={() => setTif(t)}
                className={clsx(
                  "rounded-md border py-1.5 text-2xs font-medium transition-colors",
                  tif === t ? "border-teal/40 bg-teal/10 text-teal"
                    : "border-ink-800 text-ink-600 hover:text-zinc-300",
                )}>{t}</button>
            ))}
          </div>
        </Field>
      )}

      <div className="rounded-md border border-ink-800 bg-ink-950/60 p-2">
        <div className="flex items-center justify-between text-2xs">
          <span className="uppercase tracking-wide text-ink-600">Order type</span>
          <span className="font-mono text-zinc-300">{orderType}</span>
        </div>
        <div className="mt-1 flex items-center justify-between text-2xs">
          <span className="uppercase tracking-wide text-ink-600">Notional</span>
          <span className="font-mono num text-zinc-100">{fmt.usd(notional)}</span>
        </div>
      </div>

      {msg && (
        <div className={clsx("rounded-md border px-2 py-1.5 text-2xs",
          msg.kind === "ok" ? "border-bid/30 bg-bid/5 text-bid" : "border-ask/30 bg-ask/5 text-ask",
        )}>{msg.text}</div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <button
          disabled={!req || busy}
          onClick={() => submit(true)}
          className="rounded-lg border border-teal/30 bg-teal/10 py-2 text-xs font-semibold text-teal hover:bg-teal/20 disabled:opacity-50"
        >
          <Icon name="check" className="mr-1 inline align-text-bottom" /> Dry-run
        </button>
        <button
          disabled
          title="Live manual order routing is disabled until ibkr_proxy.py is wired. See the file's docstring."
          className="flex items-center justify-center gap-1 rounded-lg bg-bid py-2 text-xs font-semibold text-ink-950 opacity-40 disabled:cursor-not-allowed"
        >
          <Icon name="bolt" /> Submit
        </button>
      </div>

      <style>{inputStyles}</style>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// STRATEGY — deploy run_live.py on the selected symbol
// ─────────────────────────────────────────────────────────────────────
function StrategyForm({ symbol }: Props) {
  const [strategyId, setStrategyId] = useState("breakout");
  const strat = STRATEGIES.find((s) => s.id === strategyId)!;
  const lastPx = symbol?.live.last ?? 0;
  const refTrigger = symbol?.live.trigger_price && symbol.live.trigger_price > 0
    ? symbol.live.trigger_price
    : lastPx ? +(lastPx * 1.001).toFixed(2) : 0;

  // Auto-suggest: trigger = 0.1% above last (typical breakout retest);
  // qty = floor($5k / price); stop = 1%. User can override anything.
  const [trigger, setTrigger] = useState("");
  const [qty, setQty] = useState("");
  const [stop, setStop] = useState("0.01");
  const [port, setPort] = useState("7496");
  const [clientId, setClientId] = useState("1");
  const [offsetFixed, setOffsetFixed] = useState("0.20");
  const [paper, setPaper] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Re-prefill defaults whenever the user switches symbol or refTrigger
  // first arrives. Don't overwrite values the user has already typed.
  useEffect(() => {
    if (!trigger && refTrigger > 0) setTrigger(refTrigger.toFixed(2));
    if (!qty && lastPx > 0) setQty(String(Math.max(1, Math.floor(5000 / lastPx))));
    // Default client_id rotates so a 2nd deploy on a different symbol
    // doesn't collide with the 1st. We bump it to (running count + 1).
    if (clientId === "1") {
      const running = getState().processes.filter((p) => p.status === "running").length;
      if (running > 0) setClientId(String(running + 1));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol?.key]);

  const req = useMemo<LaunchRequest | null>(() => {
    if (!symbol) return null;
    const t = parseFloat(trigger), q = parseInt(qty, 10), s = parseFloat(stop);
    const p = parseInt(port, 10), cid = parseInt(clientId, 10), of = parseFloat(offsetFixed);
    if (!t || !q || !s || !p || isNaN(cid)) return null;
    return {
      symbol: symbol.symbol, trigger: t, qty: q, stop: s, port: p, client_id: cid,
      paper, uvloop: true,
      offset_fixed: !isNaN(of) ? of : null,
    };
  }, [symbol, trigger, qty, stop, port, clientId, offsetFixed, paper]);

  const preview = useMemo(() => {
    if (!req) return "";
    const parts = [
      `GT_PAPER=${req.paper ? "true" : "false"}`,
      "python", "run_live.py", req.symbol,
      "--trigger", String(req.trigger),
      "--qty", String(req.qty),
      "--stop", String(req.stop),
      "--port", String(req.port),
      "--client-id", String(req.client_id),
      "--uvloop",
    ];
    if (req.offset_fixed != null) parts.push("--offset-fixed", String(req.offset_fixed));
    return parts.join(" ");
  }, [req]);

  async function deploy() {
    if (!req || !strat.available) return;
    setBusy(true); setMsg(null);
    try {
      if (getState().source === "demo") {
        const r = addDemoBot({
          symbol: req.symbol, trigger: req.trigger, qty: req.qty, stop: req.stop,
          port: req.port, client_id: req.client_id, paper: req.paper,
          offset_fixed: req.offset_fixed,
        });
        setMsg({ kind: "ok", text: `DEMO · launched ${r.key} (pid ${r.pid}) — watch the Bots tab.` });
      } else {
        const info = await api.launch(req);
        setMsg({ kind: "ok", text: `Launched ${info.key} (pid ${info.pid})` });
      }
    } catch (e: any) {
      setMsg({ kind: "err", text: e.message ?? String(e) });
    } finally { setBusy(false); }
  }

  if (!symbol) {
    return (
      <div className="flex h-64 items-center justify-center px-4 text-center text-2xs text-ink-600">
        Select a symbol above to deploy a strategy on it.
      </div>
    );
  }

  return (
    <div className="space-y-3 p-3">
      <Field label="Strategy">
        <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)} className="input">
          {STRATEGIES.map((s) => (
            <option key={s.id} value={s.id} disabled={!s.available}>{s.label}</option>
          ))}
        </select>
      </Field>
      <div className="rounded-md border border-teal/20 bg-teal/5 px-2 py-1.5 text-2xs text-teal/90">
        <Icon name="bolt" size={12} className="mr-1 inline align-text-bottom" />
        {strat.blurb}
      </div>

      <KV label="Symbol" value={symbol.symbol} mono />
      <KV label="Last price" value={fmt.price(lastPx)} mono />
      <KV label="Day high / low" value={
        symbol.live.high > 0 ? `${fmt.price(symbol.live.high)} / ${fmt.price(symbol.live.low)}` : "—"
      } mono />

      <div className="grid grid-cols-2 gap-2">
        <Field label="Trigger price">
          <input value={trigger} onChange={(e) => setTrigger(e.target.value)} inputMode="decimal" className="input" />
        </Field>
        <Field label="Quantity">
          <input value={qty} onChange={(e) => setQty(e.target.value.replace(/[^\d]/g, ""))} inputMode="numeric" className="input" />
        </Field>
        <Field label="Stop %">
          <input value={stop} onChange={(e) => setStop(e.target.value)} inputMode="decimal" className="input" />
        </Field>
        <Field label="Offset ($)">
          <input value={offsetFixed} onChange={(e) => setOffsetFixed(e.target.value)} inputMode="decimal" className="input" />
        </Field>
        <Field label="Port">
          <input value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" className="input" />
        </Field>
        <Field label="Client ID">
          <input value={clientId} onChange={(e) => setClientId(e.target.value)} inputMode="numeric" className="input" />
        </Field>
      </div>

      <label className="flex cursor-pointer items-center gap-2 text-2xs text-ink-600">
        <input type="checkbox" checked={paper} onChange={(e) => setPaper(e.target.checked)} className="accent-teal" />
        Paper trading (GT_PAPER=true)
      </label>

      {/* Estimated notional */}
      {req && (
        <div className="rounded-md border border-ink-800 bg-ink-950/60 p-2">
          <div className="flex items-center justify-between text-2xs">
            <span className="uppercase tracking-wide text-ink-600">Trade size</span>
            <span className="font-mono num text-zinc-100">{fmt.usd(req.qty * req.trigger)}</span>
          </div>
          <div className="mt-1 flex items-center justify-between text-2xs">
            <span className="uppercase tracking-wide text-ink-600">Stop loss at</span>
            <span className="font-mono num text-ask">{fmt.price(req.trigger * (1 - req.stop))}</span>
          </div>
          <div className="mt-1 flex items-center justify-between text-2xs">
            <span className="uppercase tracking-wide text-ink-600">Max loss</span>
            <span className="font-mono num text-ask">{fmt.usdSigned(-req.qty * req.trigger * req.stop)}</span>
          </div>
        </div>
      )}

      {/* CLI preview — exactly what the backend will spawn */}
      <div className="rounded-md border border-ink-800 bg-ink-950/60 p-2">
        <div className="mb-1 text-2xs uppercase tracking-wide text-ink-600">Command preview</div>
        <code className="block break-all font-mono text-2xs text-zinc-400">{preview || "—"}</code>
      </div>

      {!paper && req && (
        <div className="flex items-center gap-2 rounded-md border border-ask/30 bg-ask/5 px-2 py-1.5 text-2xs text-ask">
          <Icon name="warning" />
          <span>LIVE TRADING — real orders will fire when LTP crosses trigger.</span>
        </div>
      )}

      {msg && (
        <div className={clsx("rounded-md border px-2 py-1.5 text-2xs",
          msg.kind === "ok" ? "border-bid/30 bg-bid/5 text-bid" : "border-ask/30 bg-ask/5 text-ask",
        )}>{msg.text}</div>
      )}

      <button
        disabled={!req || busy || !strat.available}
        onClick={deploy}
        className={clsx(
          "flex w-full items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-semibold transition-colors",
          paper
            ? "bg-teal/90 text-ink-950 hover:bg-teal disabled:opacity-50"
            : "bg-bid text-ink-950 hover:bg-bid/90 disabled:opacity-50",
        )}
      >
        <Icon name="play" />
        {busy ? "Deploying…" : `Deploy ${paper ? "paper" : "LIVE"} bot on ${symbol.symbol}`}
      </button>

      <style>{inputStyles}</style>
    </div>
  );
}

// ── Shared bits ─────────────────────────────────────────────────────
function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-2xs text-ink-600">{label}</span>
      <span className={clsx("text-xs text-zinc-200 num", mono && "font-mono")}>{value}</span>
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

const inputStyles = `
  .input {
    width: 100%; background: rgba(7,10,12,0.7);
    border: 1px solid #161f28; border-radius: 0.375rem;
    padding: 0.5rem 0.5rem; font-family: 'JetBrains Mono', ui-monospace, monospace;
    font-size: 0.75rem; color: #e4e4e7; outline: none;
  }
  .input:focus { border-color: rgba(63,208,201,0.5); }
`;
