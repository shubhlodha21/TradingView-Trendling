/**
 * Module-scoped observable store shared by the live-WS driver and the
 * demo data engine. Whichever one is active calls `setState`; React
 * components subscribe via `useStore`.
 *
 * Why split this out: `useWebSocket.ts` originally owned both the
 * store and the WS connection. Demo mode needs to push into the same
 * store so the components don't care which source they're rendering.
 */
import { useSyncExternalStore } from "react";
import type {
  GatewayStatus,
  ProcessInfo,
  SymbolSnapshot,
} from "./types";

export interface Store {
  connected: boolean;       // true = data source is live (WS or demo)
  source: "ws" | "demo" | "idle";
  symbols: SymbolSnapshot[];
  processes: ProcessInfo[];
  gateway: GatewayStatus | null;
  lastError: string | null;
}

const initial: Store = {
  connected: false,
  source: "idle",
  symbols: [],
  processes: [],
  gateway: null,
  lastError: null,
};

let state: Store = initial;
const listeners = new Set<() => void>();

export function getState(): Store {
  return state;
}

export function setState(patch: Partial<Store>): void {
  state = { ...state, ...patch };
  for (const l of listeners) l();
}

export function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function useStore(): Store {
  return useSyncExternalStore(subscribe, getState, () => initial);
}
