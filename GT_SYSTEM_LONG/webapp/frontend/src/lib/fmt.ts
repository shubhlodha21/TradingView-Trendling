/**
 * Number / string formatters used everywhere prices, qty, P&L are
 * rendered. Centralized so the whole UI uses the same locale rules
 * and digit-grouping.
 */

const NF_USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});
const NF_USD_SIGNED = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  signDisplay: "exceptZero",
  maximumFractionDigits: 2,
});
const NF_PRICE = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});
const NF_INT = new Intl.NumberFormat("en-US");
const NF_PCT = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "exceptZero",
});

export const fmt = {
  usd: (n: number | null | undefined): string =>
    n == null || !isFinite(n) ? "—" : NF_USD.format(n),
  usdSigned: (n: number | null | undefined): string =>
    n == null || !isFinite(n) ? "—" : NF_USD_SIGNED.format(n),
  price: (n: number | null | undefined): string =>
    n == null || !isFinite(n) || n === 0 ? "—" : NF_PRICE.format(n),
  int: (n: number | null | undefined): string =>
    n == null || !isFinite(n) ? "—" : NF_INT.format(n),
  pct: (n: number | null | undefined): string =>
    // Backend already gives us percent units (5.0 = 5%) — divide for Intl.
    n == null || !isFinite(n) ? "—" : NF_PCT.format(n / 100),
  // Lightweight relative-time formatter. We don't need a full i18n
  // dependency for "5s / 2m / 1h" labels.
  ago: (iso: string | null | undefined): string => {
    if (!iso) return "—";
    const then = Date.parse(iso);
    if (isNaN(then)) return "—";
    const s = Math.floor((Date.now() - then) / 1000);
    if (s < 0) return "now";
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h`;
    return `${Math.floor(h / 24)}d`;
  },
};
