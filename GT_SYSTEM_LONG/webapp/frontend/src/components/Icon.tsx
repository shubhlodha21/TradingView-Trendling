/**
 * Inline SVG icon set. Self-hosted (no icon-font CDN) for offline use
 * on EC2 + zero layout shift. Every icon is 16×16 on the 0 0 24 24
 * viewBox with stroke-width 1.6 to match the screenshot's hairline feel.
 *
 * Adding a new icon: drop another <path> into iconMap and reference
 * by name. No build step.
 */
import clsx from "clsx";

type Name =
  | "trade" | "vault" | "portfolio" | "leaderboard" | "referral" | "rewards"
  | "plus" | "search" | "settings" | "globe" | "candle" | "indicators"
  | "ruler" | "pencil" | "text" | "share" | "smile" | "magnifier"
  | "layers" | "trash" | "play" | "stop" | "dot" | "chevron-down"
  | "warning" | "check" | "x" | "bolt";

const paths: Record<Name, JSX.Element> = {
  trade: <path d="M3 17l5-5 4 4 8-9" />,
  vault: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="M3 9h18" /></>,
  portfolio: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 9h6M9 13h6M9 17h3" /></>,
  leaderboard: <><path d="M4 21h16" /><path d="M8 21V11M12 21V5M16 21v-7" /></>,
  referral: <><circle cx="9" cy="8" r="3" /><circle cx="17" cy="16" r="3" /><path d="M11 10l4 4" /></>,
  rewards: <><circle cx="12" cy="9" r="5" /><path d="M9 13v8l3-2 3 2v-8" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  search: <><circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4.9a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-.9-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-.9a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4.9 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z" /></>,
  globe: <><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></>,
  candle: <><path d="M6 8v8M10 5v14M14 9v6M18 6v12" /><path d="M6 4v2M6 18v2M10 3v2M10 19v2M14 7v2M14 15v2M18 4v2M18 18v2" /></>,
  indicators: <path d="M3 7h6m3 0h9M3 12h2m3 0h13M3 17h10m3 0h5" />,
  ruler: <><path d="M3 17l14-14 4 4-14 14z" /><path d="M7 13l2 2M10 10l2 2M13 7l2 2" /></>,
  pencil: <><path d="M4 20l4-1 11-11-3-3L5 16l-1 4z" /><path d="M14 6l3 3" /></>,
  text: <path d="M5 5h14M12 5v14" />,
  share: <><circle cx="6" cy="12" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="18" cy="18" r="2" /><path d="M8 11l8-4M8 13l8 4" /></>,
  smile: <><circle cx="12" cy="12" r="9" /><circle cx="9" cy="10" r="0.5" /><circle cx="15" cy="10" r="0.5" /><path d="M9 14c1 1 2 2 3 2s2-1 3-2" /></>,
  magnifier: <><circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4M11 8v6M8 11h6" /></>,
  layers: <><path d="M12 3l9 5-9 5-9-5 9-5z" /><path d="M3 13l9 5 9-5M3 18l9 5 9-5" /></>,
  trash: <><path d="M5 7h14M10 11v6M14 11v6" /><path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13M9 7V4h6v3" /></>,
  play: <path d="M7 5l12 7-12 7V5z" />,
  stop: <rect x="6" y="6" width="12" height="12" rx="1" />,
  dot: <circle cx="12" cy="12" r="4" />,
  "chevron-down": <path d="M6 9l6 6 6-6" />,
  warning: <><path d="M12 3l10 18H2L12 3z" /><path d="M12 10v5M12 18v.5" /></>,
  check: <path d="M5 12l5 5L20 7" />,
  x: <path d="M6 6l12 12M18 6L6 18" />,
  bolt: <path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" />,
};

export function Icon({
  name,
  className,
  size = 16,
  fill = "none",
  strokeWidth = 1.6,
}: {
  name: Name;
  className?: string;
  size?: number;
  fill?: string;
  strokeWidth?: number;
}) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={fill}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={clsx("shrink-0", className)}
    >
      {paths[name]}
    </svg>
  );
}

export type IconName = Name;
