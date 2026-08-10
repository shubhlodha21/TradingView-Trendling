/**
 * Left vertical rail — chart-tool icons.
 *
 * In the inspiration screenshot these are TradingView-style chart
 * tools (indicators, trendline, fib, etc). We surface the same visual
 * but most are non-functional placeholders today — clicking just
 * shows a tooltip explaining the tool is coming with v1.1.
 *
 * The TWO active items are:
 *   * `candle`  — the chart panel (always visible, just shows it's
 *                 active state).
 *   * `layers`  — opens the multi-symbol overlay (planned, click-only
 *                 noop for now but Click handler emits a toast).
 *
 * Future-friendly: add to TOOLS[] with `active: true` to enable.
 */
import clsx from "clsx";
import { Icon, type IconName } from "./Icon";

const TOOLS: { id: string; icon: IconName; label: string; active?: boolean }[] = [
  { id: "candle", icon: "candle", label: "Chart", active: true },
  { id: "indicators", icon: "indicators", label: "Indicators" },
  { id: "pencil", icon: "pencil", label: "Draw" },
  { id: "text", icon: "text", label: "Text" },
  { id: "share", icon: "share", label: "Connections" },
  { id: "smile", icon: "smile", label: "Emoji" },
  { id: "ruler", icon: "ruler", label: "Measure" },
  { id: "magnifier", icon: "magnifier", label: "Zoom" },
  { id: "layers", icon: "layers", label: "Overlays" },
  { id: "trash", icon: "trash", label: "Clear" },
];

export function LeftRail() {
  return (
    <aside className="flex w-12 flex-col items-center gap-1 border-r border-ink-800 bg-ink-950/40 py-2">
      <button className="mb-1 rounded-md border border-ink-800 p-1.5 text-teal hover:bg-ink-800/50" title="New chart">
        <Icon name="plus" />
      </button>
      <button className="mb-2 rounded-md border border-ink-800 p-1.5 text-ink-600 hover:bg-ink-800/50" title="Add overlay">
        <Icon name="plus" />
      </button>
      {TOOLS.map((t) => (
        <button
          key={t.id}
          className={clsx(
            "rounded-md p-1.5 transition-colors",
            t.active
              ? "bg-ink-800/60 text-teal ring-1 ring-inset ring-teal/30"
              : "text-ink-600 hover:bg-ink-800/40 hover:text-zinc-300",
          )}
          title={t.label}
        >
          <Icon name={t.icon} />
        </button>
      ))}
    </aside>
  );
}
