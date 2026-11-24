import { createContext, useContext, useEffect, useRef, useState } from "react";

/** Kept-mounted screens must not continue their background reads while hidden. */
export const DataActivity = createContext(true);

export function useDataActivity(active = true): boolean {
  const screenActive = useContext(DataActivity);
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return active && screenActive && visible;
}

type PollOptions = {
  timeoutMs?: number;
  immediate?: boolean;
  /** A different operation gets its own deadline. */
  key?: string;
  onTimeout?: () => void;
};

/** Sequential polling with bounded lifetime and error backoff. Return false on a caught
 * failure to back off. Callback identity never resets the deadline, and a request still
 * settling cannot overlap one after hide/show. */
export function usePolling(f: () => unknown, ms: number, active: boolean, options: PollOptions = {}) {
  const enabled = useDataActivity(active);
  const latest = useRef(f); latest.current = f;
  const timeout = useRef(options.onTimeout); timeout.current = options.onTimeout;
  const inFlight = useRef(false);
  const { timeoutMs = 5 * 60_000, immediate = false, key = "" } = options;
  useEffect(() => {
    if (!enabled) return;
    const deadline = Date.now() + timeoutMs;
    let stopped = false, failures = 0, timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (stopped) return;
      if (Date.now() >= deadline) { timeout.current?.(); return; }
      if (!inFlight.current) {
        inFlight.current = true;
        try { failures = await latest.current() === false ? failures + 1 : 0; }
        catch { failures++; }
        finally { inFlight.current = false; }
      }
      if (!stopped) timer = setTimeout(() => void tick(), Math.min(ms * 2 ** Math.min(failures, 4), 30_000, Math.max(0, deadline - Date.now())));
    };
    timer = setTimeout(() => void tick(), immediate ? 0 : ms);
    return () => { stopped = true; clearTimeout(timer); };
  }, [enabled, ms, timeoutMs, immediate, key]);
}
