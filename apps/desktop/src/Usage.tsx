import { useRef, useState } from "react";
import { rest, type UsageTotals } from "./api";
import type { SessionView } from "./session";
import { usePolling } from "./usePolling";
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
  if (compact) return <span className="ov-usage ui-num" aria-label="This call's consumed minutes" title={`Recorded connected time for this call. ${note}${data ? ` Account total: ${connectedMinutes(data.total_seconds)} minutes.` : ""}${error ? " Waiting for an update." : ""}`}>
    {minutes !== null ? `${minutes} min used` : error ? "Usage unavailable" : "Usage…"}
  </span>;
  return <div className="usage-summary" role="group" aria-label="This call's consumed minutes">
    <span className="ui-num">{minutes !== null ? <><strong>{minutes}</strong> minutes used this call <span className="usage-total">· {connectedMinutes(data!.total_seconds)} total minutes used</span></> : error ? "Usage is temporarily unavailable." : "Loading consumed minutes…"}</span>
    <span className="usage-note">{note} {error && data ? "Waiting for an update." : "Confirmed time only."}</span>
  </div>;
}
