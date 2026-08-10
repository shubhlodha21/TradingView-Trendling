/**
 * Candlestick chart panel — TradingView's `lightweight-charts`.
 *
 * Data sourcing today: synthetic OHLC built from the streaming `last`
 * price. This is a *display* of live ticks bucketed into the active
 * timeframe, not a historical query — building a historical bar feed
 * needs a separate backend endpoint and is on the next-iteration list.
 *
 * Why ship without history: getting the layout, colors, and timeframe
 * controls right today means the historical-feed swap-in is a one-line
 * change (`series.setData(rows)`). The user gets the polished surface
 * immediately and we don't block on the data-engineering question of
 * which historical source (IBKR reqHistoricalData? local CSV cache?).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import {
  ColorType, CrosshairMode, LineStyle, createChart,
  type IChartApi, type ISeriesApi, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { Icon } from "./Icon";
import type { SymbolSnapshot } from "../lib/types";

type Timeframe = "1s" | "5s" | "1m" | "5m" | "15m" | "1h" | "D" | "W";
const FRAMES: Timeframe[] = ["1s", "5s", "1m", "5m", "15m", "1h", "D", "W"];

const FRAME_SECS: Record<Timeframe, number> = {
  "1s": 1, "5s": 5, "1m": 60, "5m": 300, "15m": 900,
  "1h": 3600, "D": 86_400, "W": 604_800,
};

interface Candle {
  time: UTCTimestamp;
  open: number; high: number; low: number; close: number;
}

interface Props {
  symbol: SymbolSnapshot | null;
}

export function ChartPanel({ symbol }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const candlesRef = useRef<Map<string, Candle[]>>(new Map());
  const [tf, setTf] = useState<Timeframe>("1m");

  // ── Init chart once ──────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#6b7280",
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.04)", style: LineStyle.Dotted },
        horzLines: { color: "rgba(255,255,255,0.04)", style: LineStyle.Dotted },
      },
      rightPriceScale: {
        borderColor: "#161f28",
        textColor: "#6b7280",
        scaleMargins: { top: 0.1, bottom: 0.2 },
      },
      timeScale: {
        borderColor: "#161f28",
        timeVisible: true,
        secondsVisible: true,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#3fd0c9", labelBackgroundColor: "#0c1115" },
        horzLine: { color: "#3fd0c9", labelBackgroundColor: "#0c1115" },
      },
      autoSize: true,
    });

    const series = chart.addCandlestickSeries({
      upColor: "#39d98a",
      downColor: "#ef4778",
      borderUpColor: "#39d98a",
      borderDownColor: "#ef4778",
      wickUpColor: "#39d98a",
      wickDownColor: "#ef4778",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(() => chart.timeScale().fitContent());
    ro.observe(containerRef.current);
    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // ── Feed: bucket each `last` tick into the current TF ────────────
  const cacheKey = useMemo(
    () => `${symbol?.key ?? "none"}_${tf}`,
    [symbol?.key, tf],
  );

  useEffect(() => {
    if (!seriesRef.current) return;
    const existing = candlesRef.current.get(cacheKey) ?? [];
    seriesRef.current.setData(existing);
  }, [cacheKey]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !symbol) return;
    const px = symbol.live.last;
    if (!px || px <= 0) return;

    const secs = FRAME_SECS[tf];
    const now = Math.floor(Date.now() / 1000);
    const bucket = (Math.floor(now / secs) * secs) as UTCTimestamp;

    const arr = candlesRef.current.get(cacheKey) ?? [];
    const last = arr[arr.length - 1];
    if (!last || last.time !== bucket) {
      const open = last ? last.close : px;
      const nc: Candle = { time: bucket, open, high: px, low: px, close: px };
      arr.push(nc);
      // Cap memory at 5,000 bars per cache key — at 1s bars that's
      // ~80 minutes, plenty to scroll through, no leak risk over a
      // long session.
      if (arr.length > 5000) arr.shift();
      candlesRef.current.set(cacheKey, arr);
      series.update(nc);
    } else {
      last.high = Math.max(last.high, px);
      last.low = Math.min(last.low, px);
      last.close = px;
      series.update(last);
    }
  }, [symbol?.live.last, symbol?.key, cacheKey, tf]);

  return (
    <div className="panel flex flex-1 flex-col">
      <div className="flex items-center justify-between gap-2 border-b border-ink-800 px-3 py-2">
        <div className="flex items-center gap-2">
          <button className="flex items-center gap-1.5 rounded-md border border-ink-800 px-2 py-1 text-2xs text-ink-600 hover:text-zinc-300">
            <span>ƒₓ</span> Indicators
          </button>
          <button className="rounded-md border border-ink-800 p-1.5 text-ink-600 hover:text-zinc-300">
            <Icon name="indicators" size={14} />
          </button>
        </div>
        <div className="flex items-center gap-1">
          {FRAMES.map((f) => (
            <button
              key={f}
              onClick={() => setTf(f)}
              className={clsx(
                "rounded-md px-2 py-1 text-2xs font-medium",
                tf === f
                  ? "bg-ink-800 text-teal ring-1 ring-inset ring-teal/30"
                  : "text-ink-600 hover:bg-ink-800/60 hover:text-zinc-300",
              )}
            >
              {f}
            </button>
          ))}
          <button className="ml-1 rounded-md border border-ink-800 p-1 text-ink-600 hover:text-zinc-300" title="Fit">
            <Icon name="magnifier" size={12} />
          </button>
        </div>
      </div>
      <div className="relative flex-1">
        <div ref={containerRef} className="absolute inset-0" />
        {(!symbol || symbol.live.last <= 0) && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-xs text-ink-600">
            {symbol ? "Waiting for first tick…" : "Select a symbol"}
          </div>
        )}
      </div>
    </div>
  );
}

// Suppress an unused-import warning when tree-shaking strips Time.
void (null as unknown as Time);
