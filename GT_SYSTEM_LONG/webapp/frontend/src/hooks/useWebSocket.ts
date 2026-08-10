/**
 * useWS — singleton WebSocket pushed into the shared store.
 *
 * Reads/writes go through `src/lib/store.ts` so demo mode can use the
 * same store without this file knowing about it. When a real frame
 * lands we automatically `stopDemo()` so we don't double-write.
 *
 * Reconnect: exponential backoff with jitter, capped at 30s.
 * Components stay rendering the last-known snapshot during outages —
 * the page is meant to be glanceable, not crash on a hiccup.
 */
import { setState, useStore, type Store } from "../lib/store";
import { stopDemo } from "../lib/demo";
import type { WSFrame } from "../lib/types";

let ws: WebSocket | null = null;
let attempt = 0;
let stopped = false;
let started = false;

function connect(): void {
  if (stopped) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    attempt = 0;
    // The first real frame wins — kill demo so it doesn't fight us.
    stopDemo();
    setState({ connected: true, source: "ws", lastError: null });
  };

  ws.onmessage = (ev) => {
    try {
      const frame = JSON.parse(ev.data) as WSFrame;
      switch (frame.type) {
        case "snapshot":
          setState({ symbols: frame.payload.symbols });
          break;
        case "process_list":
          setState({ processes: frame.payload.processes });
          break;
        case "gateway":
          setState({ gateway: frame.payload });
          break;
        case "error":
          setState({
            lastError: `${frame.payload.where}: ${frame.payload.error}`,
          });
          break;
        default:
          break;
      }
    } catch (e) {
      setState({ lastError: `bad frame: ${e}` });
    }
  };

  ws.onclose = () => {
    setState({ connected: false });
    if (stopped) return;
    const delay = Math.min(30_000, 250 * 2 ** attempt) + Math.random() * 250;
    attempt += 1;
    setTimeout(connect, delay);
  };

  ws.onerror = () => {
    // close handler runs right after; swallow the noisy dev-tools warning
  };
}

export function startWS(): void {
  if (started || typeof window === "undefined") return;
  started = true;
  connect();
}

export function shutdownWS(): void {
  stopped = true;
  ws?.close();
}

export function useWS(): Store {
  return useStore();
}

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    stopped = true;
    ws?.close();
  });
}
