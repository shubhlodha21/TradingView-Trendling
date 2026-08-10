/// <reference types="vite/client" />

/*
 * Declares `import.meta.hot`, `import.meta.env`, and the asset-import
 * module types (e.g. `import logo from "./logo.svg"`) for TypeScript.
 *
 * Without this file, `useWebSocket.ts` errors on `import.meta.hot` —
 * the HMR API exists at runtime (Vite injects it in dev) but the type
 * isn't on the default `ImportMeta` interface.
 */
