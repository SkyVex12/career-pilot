// Small pieces the screens share: a pack's state, dates, plurals, and refreshing a kept-open tab.
import { useEffect, useRef } from "react";
import type { PackInfo } from "./api";
import { Badge } from "./ui";

export function plural(n: number, word: string, many = `${word}s`): string {
  return `${n} ${n === 1 ? word : many}`;
}

/** An interview or application pack: ready (with its prepared answers), building, or expired. */
export function PackBadge({ pack }: { pack: PackInfo | null | undefined }) {
  if (!pack) return <Badge tone="warning">Pack building</Badge>;
  if (!pack.available) return <Badge tone="neutral">Pack expired</Badge>;
  return <Badge tone="success">Pack ready{pack.prepared_answers != null ? ` (${plural(pack.prepared_answers, "answer")})` : ""}</Badge>;
}

export function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** "today 11:15 PM (in 45 min)", "Sat 9:28 PM", "Oct 22, 2026". */
export function relative(iso: string, now = Date.now()): string {
  const d = new Date(iso);
  const t = d.getTime();
  if (Number.isNaN(t)) return iso;
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const days = Math.round((startOfDay(t) - startOfDay(now)) / 86_400_000);
  const mins = Math.round((t - now) / 60_000);
  const soon = mins > 0 && mins < 120 ? ` (in ${mins} min)` : mins <= 0 && mins > -120 ? ` (${-mins} min ago)` : "";
  if (days === 0) return `today ${time}${soon}`;
  if (days === 1) return `tomorrow ${time}`;
  if (days === -1) return `yesterday ${time}`;
  if (days > 1 && days < 7) return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
  return d.toLocaleDateString(undefined, { dateStyle: "medium" });
}

function startOfDay(t: number): number {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
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

/** A text file read in the app; a PDF goes up as a file instead. */
export const ACCEPT = ".pdf,.md,.markdown,.txt";
