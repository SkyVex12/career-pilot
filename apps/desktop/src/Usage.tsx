import { useEffect, useRef, useState } from "react";
import { rest, type UsageTotals } from "./api";
import type { SessionView } from "./session";
import { useDataActivity, usePolling } from "./usePolling";
import "./usage.css";

export function connectedMinutes(seconds: number): string {
  return new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(Math.max(0, seconds) / 60);
}

/** These are server snapshots, never a wall-clock estimate. The lifetime total
 * already includes this call, so no local elapsed time is added to either number. */
export function useUsageTotals(sessionId: string | null = null, active = true, running = false, refreshKey = "") {
  const scope = sessionId ?? "account";
  const current = useRef(scope);
  current.current = scope;
  const [state, setState] = useState<{ scope: string; data: UsageTotals | null; error: boolean }>({ scope, data: null, error: false });
  usePolling(async () => {
    try {
      const data = await rest.usageTotals(sessionId, { wake: false });
      if (current.current !== scope) return true;
      if (data.session_id !== sessionId) throw new Error("Usage response belongs to another call");
      setState({ scope, data, error: false });
      return true;
    } catch {
      if (current.current === scope) setState(previous => ({ scope, data: previous.scope === scope ? previous.data : null, error: true }));
      return false;
    }
  }, running ? 10_000 : 30_000, active, { immediate: true, timeoutMs: Infinity, key: `${scope}/${running}/${refreshKey}` });
  return state.scope === scope ? state : { scope, data: null, error: false };
}

export function AccountUsage({ active = true, running = false }: { active?: boolean; running?: boolean }) {
  const { data, error } = useUsageTotals(null, active, running);
  return <div className="usage-summary" role="group" aria-label="Lifetime account usage">
    <span className="ui-num">{data ? <><strong>{connectedMinutes(data.total_seconds)}</strong> total minutes used</> : error ? "Usage is temporarily unavailable." : "Loading total usage…"}</span>
    <span className="usage-note">Connected live time across all your workspaces. {error && data ? "Waiting for an update." : "Updates periodically."}</span>
  </div>;
}

export function LiveUsage({ session, compact = false }: { session: SessionView; compact?: boolean }) {
  const enabled = Boolean(session.sessionId) && !session.seat;
  const { data, error } = useUsageTotals(session.sessionId, enabled, session.running, session.connection?.state ?? "");
  if (!enabled) return null;
  const minutes = data?.session_seconds == null ? null : connectedMinutes(data.session_seconds);
  const paused = session.running && session.connection?.state !== "live";
  const note = paused ? "Connection paused; reconnect time is excluded." : "Includes silence while connected. Updates periodically.";
  if (compact) return <LiveCallTimer key={session.sessionId} session={session} data={data} error={error} />;
  return <div className="usage-summary" role="group" aria-label="This call's consumed minutes">
    <span className="ui-num">{minutes !== null ? <><strong>{minutes}</strong> minutes used this call <span className="usage-total">· {connectedMinutes(data!.total_seconds)} total minutes used</span></> : error ? "Usage is temporarily unavailable." : "Loading consumed minutes…"}</span>
    <span className="usage-note">{note} {error && data ? "Waiting for an update." : "Confirmed time only."}</span>
  </div>;
}

export function connectedDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const parts = [minutes, total % 60].map(value => String(value).padStart(2, "0"));
  return (hours ? [String(hours), ...parts] : parts).join(":");
}

/** A live display between ledger checkpoints. Account totals remain server-confirmed. */
function LiveCallTimer({ session, data, error }: { session: SessionView; data: UsageTotals | null; error: boolean }) {
  const connected = session.running && session.connection?.state === "live";
  const visible = useDataActivity();
  const clock = useRef({ seconds: data?.session_seconds ?? 0, at: performance.now(), connected, snapshot: data });
  const [seconds, setSeconds] = useState(clock.current.seconds);

  useEffect(() => {
    const now = performance.now();
    const previous = clock.current;
    let elapsed = previous.seconds + (previous.connected ? Math.max(0, now - previous.at) / 1000 : 0);
    if (data !== previous.snapshot && data?.session_seconds != null) {
      // Checkpoints can lag. Keep the live timer steady; use the exact final ledger after end.
      elapsed = session.running ? Math.max(elapsed, data.session_seconds) : data.session_seconds;
    }
    clock.current = { seconds: elapsed, at: now, connected, snapshot: data };
    setSeconds(elapsed);
  }, [connected, session.running, data]);

  useEffect(() => {
    if (!connected || !visible) return;
    let timer: number;
    const update = () => {
      const elapsed = clock.current.seconds + Math.max(0, performance.now() - clock.current.at) / 1000;
      setSeconds(elapsed);
      // Align the next tick to a whole second even when a snapshot arrives between ticks.
      timer = window.setTimeout(update, Math.ceil(1000 - (elapsed * 1000) % 1000));
    };
    update();
    return () => window.clearTimeout(timer);
  }, [connected, visible, data]);

  const note = session.running
    ? connected ? "Connected call time. Updates every second." : "Timer paused while reconnecting."
    : "Final connected call time.";
  const account = data ? " Recorded account total: " + connectedMinutes(data.total_seconds) + " minutes." : "";
  return <span className="ov-usage ui-num" role="timer" aria-label="Call duration" aria-live="off" title={note + account + (error ? " Waiting for a usage update." : "")}>
    {connectedDuration(seconds)}
  </span>;
}
