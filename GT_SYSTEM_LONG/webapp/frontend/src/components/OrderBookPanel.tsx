/**
 * Right-panel top: Order Book / Trades tabs.
 *
 * Today's data: synthetic ladder derived from the symbol's bid/ask
 * + the bid_size/ask_size. The real depth-of-book stream needs a
 * `reqMktDepth` subscription on the backend, which we haven't enabled
 * (it costs market-data permissions on the IBKR account). The layout
 * is here so dropping in real L2 data is a one-row substitution.
 *
 * Trades tab: feeds from the live snapshot's `tape` array (already
 * written by the bot's microstructure aggregator). Real, not stubbed.
 */
import clsx from "clsx";
import { useState } from "react";
import { fmt } from "../lib/fmt";
import type { SymbolSnapshot } from "../lib/types";

type Tab = "book" | "trades";

interface Props { symbol: SymbolSnapshot | null }

export function OrderBookPanel({ symbol }: Props) {
  const [tab, setTab] = useState<Tab>("book");
  return (
    <div className="panel flex flex-col">
      <div className="grid grid-cols-2 border-b border-ink-800">
        <TabBtn label="Order Book" active={tab === "book"} onClick={() => setTab("book")} />
        <TabBtn label="Trades" active={tab === "trades"} onClick={() => setTab("trades")} />
      </div>
      {tab === "book" ? <Ladder symbol={symbol} /> : <Trades symbol={symbol} />}
    </div>
  );
}

function TabBtn({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        "py-2.5 text-xs font-semibold transition-colors",
        active ? "bg-ink-900 text-teal" : "text-ink-600 hover:text-zinc-300",
      )}
    >
      {label}
    </button>
  );
}

function Ladder({ symbol }: Props) {
  if (!symbol || symbol.live.last <= 0) {
    return <Empty label={symbol ? "Waiting for ticks…" : "Select a symbol"} />;
  }

  // Synthetic ladder: walk N levels at $0.05 increments around bid/ask,
  // decaying size as we step away. Placeholder until reqMktDepth lands.
  const LEVELS = 10;
  const step = Math.max(0.01, symbol.live.last * 0.0001); // 1 bps
  const asks: { px: number; sz: number }[] = [];
  const bids: { px: number; sz: number }[] = [];
  for (let i = 0; i < LEVELS; i++) {
    asks.push({
      px: symbol.live.ask + i * step,
      sz: Math.max(1, Math.round((symbol.live.ask_size || 100) * (1 / (1 + i * 0.25)))),
    });
    bids.push({
      px: symbol.live.bid - i * step,
      sz: Math.max(1, Math.round((symbol.live.bid_size || 100) * (1 / (1 + i * 0.25)))),
    });
  }
  asks.reverse();

  const maxSize = Math.max(...asks.map((r) => r.sz), ...bids.map((r) => r.sz));

  return (
    <div className="flex flex-col">
      <div className="grid grid-cols-3 gap-2 border-b border-ink-800 px-3 py-1.5 text-2xs uppercase tracking-wide text-ink-600">
        <div>Price</div>
        <div className="text-right">Size</div>
        <div className="text-right">Total</div>
      </div>
      <div className="overflow-y-auto">
        {asks.map((r, i) => (
          <Row key={`a${i}`} px={r.px} sz={r.sz} tone="ask" max={maxSize} />
        ))}
        <div className="flex items-center justify-between border-y border-ink-800 px-3 py-1 text-2xs">
          <span className="text-ink-600">Spread</span>
          <span className="font-mono text-zinc-200">{fmt.price(symbol.spread)}</span>
          <span className="font-mono text-ink-600">{symbol.spread_bps.toFixed(2)} bps</span>
        </div>
        {bids.map((r, i) => (
          <Row key={`b${i}`} px={r.px} sz={r.sz} tone="bid" max={maxSize} />
        ))}
      </div>
    </div>
  );
}

function Row({ px, sz, tone, max }: { px: number; sz: number; tone: "bid" | "ask"; max: number }) {
  const depth = max > 0 ? Math.min(100, (sz / max) * 100) : 0;
  return (
    <div
      className={clsx(
        "grid grid-cols-3 gap-2 px-3 py-0.5 font-mono text-2xs num",
        tone === "bid" ? "depth-bid" : "depth-ask",
      )}
      style={{ ["--depth" as any]: `${depth}%` }}
    >
      <span className={tone === "bid" ? "text-bid" : "text-ask"}>{fmt.price(px)}</span>
      <span className="text-right text-zinc-300">{fmt.int(sz)}</span>
      <span className="text-right text-ink-600">{fmt.int(sz * px / 1000).replace(/\d$/, (d) => d + "k")}</span>
    </div>
  );
}

function Trades({ symbol }: Props) {
  const tape = symbol?.live.tape ?? [];
  if (tape.length === 0) return <Empty label="No tape yet." />;
  return (
    <div className="flex flex-col">
      <div className="grid grid-cols-3 gap-2 border-b border-ink-800 px-3 py-1.5 text-2xs uppercase tracking-wide text-ink-600">
        <div>Time</div>
        <div className="text-right">Price</div>
        <div className="text-right">Size</div>
      </div>
      <div className="max-h-[24rem] overflow-y-auto">
        {tape.slice().reverse().map((t, i) => {
          const ts = t.ts ? new Date(t.ts).toLocaleTimeString() : "—";
          const dir = (t.direction ?? 0) > 0 ? "bid" : (t.direction ?? 0) < 0 ? "ask" : null;
          return (
            <div key={i} className="grid grid-cols-3 gap-2 px-3 py-0.5 font-mono text-2xs num">
              <span className="text-ink-600">{ts.split(" ")[0]}</span>
              <span className={clsx("text-right", dir === "bid" && "text-bid", dir === "ask" && "text-ask", !dir && "text-zinc-300")}>
                {fmt.price(t.price)}
              </span>
              <span className="text-right text-zinc-300">{fmt.int(t.size)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return <div className="flex h-64 items-center justify-center text-2xs text-ink-600">{label}</div>;
}
