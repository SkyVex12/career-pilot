// Small pieces the screens share: a pack's state, dates, plurals, and refreshing a kept-open tab.
import { useEffect, useRef, useState } from "react";
import type { PackInfo } from "./api";
import { Badge } from "./ui";
import { useDataActivity } from "./usePolling";

export function plural(n: number, word: string, many = `${word}s`): string {
  return `${n} ${n === 1 ? word : many}`;
}

/** An interview or application pack: ready (with its prepared answers), building, or expired. */
export function PackBadge({ pack }: { pack: PackInfo | null | undefined }) {
  if (["queued", "running"].includes(pack?.operation_state ?? "")) return <Badge tone="warning">Pack building</Badge>;
  if (pack?.operation_state === "failed") return <Badge tone="danger">Pack failed</Badge>;
  if (!pack?.available) return <Badge tone="neutral">{pack?.availability === "expired" ? "Pack expired" : "Pack unavailable"}</Badge>;
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

/** Revalidate a kept-open tab once on tab or window return. The repository still decides
 * whether its freshness window permits reuse; this never starts an idle refresh timer. */
export function useOnShow(active: boolean, f: () => unknown) {
  const visible = useDataActivity(active);
  const seen = useRef(visible);
  const latest = useRef(f);
  latest.current = f;
  useEffect(() => {
    if (visible && !seen.current) latest.current();
    seen.current = visible;
  }, [visible]);
}

/** Counts the times a kept-mounted screen has been shown again, for effects that load data:
 * put it in their dependencies so they fetch afresh each time the screen comes back. */
export function useShowCount(active: boolean): number {
  const [n, setN] = useState(0);
  useOnShow(active, () => setN((v) => v + 1));
  return n;
}

/** A text file read in the app; a PDF goes up as a file instead. */
export const ACCEPT = ".pdf,.md,.markdown,.txt";
/// Engineering context reads Markdown and text for now.
export const TEXT_ACCEPT = ".md,.markdown,.txt";
