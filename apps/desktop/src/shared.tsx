// Small pieces the screens share: status chips, a pack's state, dates, loading and load errors.
import { useEffect, useRef } from "react";
import type { PackInfo } from "./api";

export function Chip({ state, children }: { state: "ok" | "busy" | "bad" | "off"; children: React.ReactNode }) {
  return <span className={`chip chip-${state}`}>{children}</span>;
}

export function PackStatus({ pack }: { pack: PackInfo | null | undefined }) {
  if (!pack) return <Chip state="busy">Pack building</Chip>;
  if (!pack.available) return <Chip state="off">Pack expired</Chip>;
  return <Chip state="ok">Pack ready{pack.prepared_answers != null ? ` (${pack.prepared_answers} answers)` : ""}</Chip>;
}

export function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** A list still on its way, so it does not read as empty. */
export function Loading({ what }: { what: string }) {
  return (
    <p className="loading" role="status">
      <span className="spinner" />
      Loading {what}…
    </p>
  );
}

/** A load that failed, with a way to try again. */
export function LoadError({ error, onRetry }: { error: string; onRetry: () => unknown }) {
  return (
    <p className="note warn load-error">
      Could not load: {error}{" "}
      <button className="link" onClick={() => onRetry()}>
        Retry
      </button>
    </p>
  );
}

/** Calls `f` each time a kept-open tab is shown again (not on the first show: the tab loads
 * on mount), so what it lists is fresh without starting from nothing. */
export function useOnShow(active: boolean, f: () => unknown) {
  const seen = useRef(active);
  const latest = useRef(f);
  latest.current = f;
  useEffect(() => {
    if (active && !seen.current) latest.current();
    seen.current = active;
  }, [active]);
}
