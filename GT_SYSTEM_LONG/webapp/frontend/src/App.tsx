/**
 * Root layout.
 *
 * Data source selection on mount:
 *   * URL `?demo=1`  → demo mode, no backend touched.
 *   * URL `?live=1`  → live mode only; never falls back to demo.
 *   * Default        → try live WS for 2s; if no connection, fall back
 *                      to demo so the page is never blank.
 *
 *   ┌ TopBar ────────────────────────────────────────────────────────┐
 *   │ logo · nav · gateway · ws · settings                            │
 *   ├──┬─ Center column ─────────────────────────┬─ Right column ─────┤
 *   │L │  SymbolHeader (pills)                   │   OrderBook /      │
 *   │e │  ChartPanel                             │   Trades           │
 *   │f │  BottomTabs (Balances/Positions/Bots/…) │   OrderTicket      │
 *   │t │                                          │                    │
 *   └──┴──────────────────────────────────────────┴────────────────────┘
 */
import { useEffect, useMemo, useState } from "react";
import { TopBar } from "./components/TopBar";
import { LeftRail } from "./components/LeftRail";
import { SymbolHeader } from "./components/SymbolHeader";
import { ChartPanel } from "./components/ChartPanel";
import { BottomTabs } from "./components/BottomTabs";
import { OrderBookPanel } from "./components/OrderBookPanel";
import { OrderTicket } from "./components/OrderTicket";
import { useWS, startWS } from "./hooks/useWebSocket";
import { startDemo, stopDemo } from "./lib/demo";
import { getState } from "./lib/store";
import { Icon } from "./components/Icon";

function resolveMode(): "demo" | "live" | "auto" {
  if (typeof window === "undefined") return "auto";
  const url = new URLSearchParams(window.location.search);
  if (url.get("demo") === "1") return "demo";
  if (url.get("live") === "1") return "live";
  return "auto";
}

export default function App() {
  const { connected, source, symbols, processes, gateway, lastError } = useWS();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [mode] = useState(resolveMode);

  // ── Bootstrap the data source ─────────────────────────────────────
  useEffect(() => {
    if (mode === "demo") {
      startDemo();
      return () => stopDemo();
    }
    // live or auto: open the WS. The WS driver will call stopDemo()
    // automatically if a real frame ever arrives.
    startWS();
    if (mode === "auto") {
      // 2s grace window — if WS hasn't connected, light up demo so
      // the page renders something useful instead of an empty grid.
      // Read the live store state inside the timeout (not the closure)
      // so the check reflects whatever happened during the wait.
      const t = setTimeout(() => {
        if (getState().source !== "ws") startDemo();
      }, 2000);
      return () => clearTimeout(t);
    }
    return undefined;
    // resolveMode + startWS are stable; we run this once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-select the first live symbol once data arrives.
  useEffect(() => {
    if (selectedKey) return;
    const first = symbols.find((s) => s.is_active) ?? symbols[0];
    if (first) setSelectedKey(first.key);
  }, [symbols, selectedKey]);

  const selected = useMemo(
    () => symbols.find((s) => s.key === selectedKey) ?? null,
    [symbols, selectedKey],
  );

  return (
    <div className="bg-stage-glow flex h-screen flex-col">
      {source === "demo" && <DemoBanner />}
      <TopBar gateway={gateway} wsConnected={connected} />

      {lastError && (
        <div className="border-b border-ask/30 bg-ask/10 px-4 py-1.5 text-2xs text-ask">
          {lastError}
        </div>
      )}

      {/*
        Outer grid uses `min-h-0` so flex children can shrink below their
        intrinsic content size — without this a tall ChartPanel pushes
        BottomTabs off-screen and a tall OrderTicket clips the Deploy
        button at the bottom of the viewport. `overflow-hidden` on the
        outer keeps the page itself fixed at h-screen; scroll happens
        per-column instead.
      */}
      <div className="grid min-h-0 flex-1 grid-cols-[3rem_minmax(0,1fr)_22rem] gap-3 overflow-hidden p-3">
        <LeftRail />

        {/* Center column ─ symbol pills, chart (fixed-ish), tabbed table (own scroll) */}
        <div className="flex min-h-0 min-w-0 flex-col gap-3">
          <div className="panel">
            <SymbolHeader
              symbols={symbols}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
            />
          </div>
          {/* Chart takes a healthy share of the viewport but caps so the
              positions/bots table beneath is always visible. Without
              max-h the chart would grow to fill `flex-1` and push the
              table out of view on short windows. */}
          <div className="flex max-h-[55vh] min-h-[18rem] flex-1 flex-col">
            <ChartPanel symbol={selected} />
          </div>
          {/* BottomTabs has its own internal scroll for the table body;
              `min-h-0` lets the flex parent let it shrink when needed. */}
          <div className="flex min-h-0 flex-1">
            <BottomTabs
              symbols={symbols}
              processes={processes}
              selectedKey={selectedKey}
            />
          </div>
        </div>

        {/* Right column ─ scrolls as a whole. OrderTicket in Strategy
            mode is long; with overflow-hidden on the grid above, the
            column needs explicit overflow-y-auto + min-h-0 to scroll
            instead of clipping the bottom buttons. */}
        <div className="flex min-h-0 flex-col gap-3 overflow-y-auto pr-1">
          <OrderBookPanel symbol={selected} />
          <OrderTicket symbol={selected} />
        </div>
      </div>
    </div>
  );
}

function DemoBanner() {
  return (
    <div className="flex items-center justify-center gap-2 border-b border-teal/30 bg-teal/10 px-4 py-1.5 text-2xs font-medium text-teal">
      <Icon name="bolt" size={12} />
      <span className="uppercase tracking-wider">Demo Mode</span>
      <span className="text-teal/70">·</span>
      <span className="text-teal/80">
        synthetic data — no backend connected. Append <code className="rounded bg-ink-900 px-1 text-teal">?live=1</code> to the URL to disable.
      </span>
    </div>
  );
}
