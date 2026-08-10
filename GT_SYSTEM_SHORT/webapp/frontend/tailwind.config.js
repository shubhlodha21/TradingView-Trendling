/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Hyperliquid-inspired palette. Pure-black background + a single
      // teal accent, with magenta/emerald for sell/buy. Greys are tuned
      // so the panel borders feel like 1px hairlines under glow rather
      // than chunky frames.
      colors: {
        ink: {
          950: "#070a0c",   // page background
          900: "#0c1115",   // panel background
          850: "#101820",   // raised panel
          800: "#161f28",   // border
          700: "#1d2832",   // hover
          600: "#2a3744",   // muted text
        },
        teal: {
          DEFAULT: "#3fd0c9",
          dim: "#1b9d96",
          glow: "#3fd0c9",
        },
        bid: "#39d98a",     // buys / longs
        ask: "#ef4778",     // sells / shorts
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "Menlo", "monospace"],
      },
      boxShadow: {
        // Top-of-panel glow used on the header bar in the screenshot.
        glow: "0 -40px 80px -20px rgba(63, 208, 201, 0.18)",
        panel: "0 0 0 1px rgba(255,255,255,0.04), 0 8px 24px -16px rgba(0,0,0,0.7)",
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
    },
  },
  plugins: [],
};
