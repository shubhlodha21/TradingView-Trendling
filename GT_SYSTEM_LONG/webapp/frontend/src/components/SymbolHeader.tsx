/**
 * Symbol header strip:
 *   [TICKER ▼]   Last   Bid/Ask   Spread   24h Chg   Day Vol   VWAP   H/L
 *
 * The dropdown lists every symbol the backend currently knows about
 * (i.e. has a `.gt_state_*.json` file for) plus a hard-coded list of
 * common large-caps the user can launch a fresh bot for. Typing in
 * the input filters both lists.
 *
 * Numbers update via the same WS stream — no per-pill subscription.
 * We pull from the same SymbolSnapshot the table downstairs reads, so
 * if the table says LAST $89.60, this strip says LAST $89.60.
 */
import clsx from "clsx";
import { useEffect, useMemo, useRef, useState } from "react";
import { fmt } from "../lib/fmt";
import type { SymbolSnapshot } from "../lib/types";
import { Icon } from "./Icon";

const POPULAR = ["NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA", "GOOGL",
  "NFLX", "AMD", "INTC", "BRK.B", "JPM", "V", "WMT", "COST", "AVGO",
  "ORCL", "ADBE", "CRM", "PYPL", "DIS", "PFE", "KO", "PEP", "T"];

interface Props {
  symbols: SymbolSnapshot[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
}

export function SymbolHeader({ symbols, selectedKey, onSelect }: Props) {
  const selected = symbols.find((s) => s.key === selectedKey) ?? null;
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
      <SymbolPicker
        symbols={symbols}
        selectedKey={selectedKey}
        onSelect={onSelect}
      />
      <Pill label="Last" value={fmt.price(selected?.live.last)} mono accent />
      <Pill label="Bid" value={fmt.price(selected?.live.bid)} mono tone="bid" />
      <Pill label="Ask" value={fmt.price(selected?.live.ask)} mono tone="ask" />
      <Pill label="Spread" value={
        selected && selected.spread > 0
          ? `${fmt.price(selected.spread)} · ${selected.spread_bps.toFixed(1)} bps`
          : "—"
      } mono />
      <Pill
        label="24h Chg"
        value={selected ? `${selected.change_pct >= 0 ? "+" : ""}${selected.change_pct.toFixed(2)}%` : "—"}
        mono
        tone={selected ? (selected.change_pct >= 0 ? "bid" : "ask") : undefined}
      />
      <Pill label="Day Vol" value={fmt.int(selected?.live.volume)} mono />
      <Pill label="VWAP" value={fmt.price(selected?.live.vwap)} mono />
      <Pill
        label="H/L"
        value={selected && selected.live.high > 0
          ? `${fmt.price(selected.live.high)} · ${fmt.price(selected.live.low)}`
          : "—"}
        mono
      />
      <Pill label="Trigger" value={fmt.price(selected?.live.trigger_price)} mono tone="teal" />
    </div>
  );
}

function Pill({
  label, value, mono, tone, accent,
}: {
  label: string; value: string; mono?: boolean;
  tone?: "bid" | "ask" | "teal"; accent?: boolean;
}) {
  return (
    <div className="flex flex-col">
      <span className="text-2xs uppercase tracking-wide text-ink-600">{label}</span>
      <span
        className={clsx(
          "leading-tight num",
          mono && "font-mono",
          accent ? "text-base font-semibold text-zinc-100" : "text-sm text-zinc-200",
          tone === "bid" && "text-bid",
          tone === "ask" && "text-ask",
          tone === "teal" && "text-teal",
        )}
      >
        {value}
      </span>
    </div>
  );
}

// ── Symbol picker with search + dropdown ────────────────────────────
function SymbolPicker({ symbols, selectedKey, onSelect }: Props) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const selected = symbols.find((s) => s.key === selectedKey) ?? null;

  // Merge live symbols (from running bots) with the popular static
  // list, dedupe. Live ones surface first because they have data.
  const rows = useMemo(() => {
    const live = symbols.map((s) => ({ kind: "live" as const, key: s.key, label: s.symbol, sub: `c${s.client_id}`, snap: s }));
    const seen = new Set(live.map((r) => r.label));
    const stat = POPULAR.filter((p) => !seen.has(p)).map((p) => ({ kind: "static" as const, key: `${p}_new`, label: p, sub: "—", snap: null }));
    const all = [...live, ...stat];
    const needle = q.trim().toUpperCase();
    return needle ? all.filter((r) => r.label.includes(needle)) : all;
  }, [symbols, q]);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-lg border border-ink-800 bg-ink-900/80 px-3 py-2 text-sm font-semibold text-zinc-100 hover:border-teal/40"
      >
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-teal/20 text-teal">
          <Icon name="bolt" size={12} />
        </span>
        <span className="font-mono tracking-wide">
          {selected ? `${selected.symbol} · USD` : "Pick a symbol"}
        </span>
        <Icon name="chevron-down" className="text-ink-600" size={14} />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-80 rounded-xl border border-ink-800 bg-ink-900 shadow-panel">
          <div className="flex items-center gap-2 border-b border-ink-800 px-3 py-2">
            <Icon name="search" className="text-ink-600" />
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search symbol"
              className="flex-1 bg-transparent text-sm text-zinc-200 placeholder:text-ink-600 focus:outline-none"
            />
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {rows.length === 0 ? (
              <div className="px-3 py-4 text-center text-xs text-ink-600">
                No symbols match "{q}".
              </div>
            ) : rows.map((r) => (
              <button
                key={r.key}
                onClick={() => {
                  onSelect(r.key);
                  setOpen(false);
                  setQ("");
                }}
                className={clsx(
                  "flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-sm transition-colors",
                  r.kind === "live"
                    ? "text-zinc-200 hover:bg-ink-800"
                    : "text-ink-600 hover:bg-ink-800 hover:text-zinc-300",
                  selectedKey === r.key && "bg-ink-800",
                )}
              >
                <span className="flex items-center gap-2 font-mono">
                  <span className="text-zinc-200">{r.label}</span>
                  <span className="text-2xs text-ink-600">{r.sub}</span>
                </span>
                {r.snap ? (
                  <span className="font-mono text-2xs text-ink-600">
                    {fmt.price(r.snap.live.last)}
                  </span>
                ) : (
                  <span className="text-2xs text-ink-600">launch</span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
