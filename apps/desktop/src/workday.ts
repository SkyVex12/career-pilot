// Company workspaces in the UI: dates for the work log, which company the start screen offers
// first, and how a company's pack and facts read.
import type { Employer, MemoryItem, PackState, WorkplaceKind } from "./api";

/// Today as YYYY-MM-DD in this computer's time zone (the log's default day).
export function localDay(now: Date = new Date()): string {
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/// The company the start screen picks first: the one used last if it is still active, else
/// the first current one, else any active one.
export function defaultEmployer(employers: Employer[], lastUsed: string | null): string {
  const active = employers.filter((e) => e.status === "active");
  if (lastUsed && active.some((e) => e.id === lastUsed)) return lastUsed;
  return (active.find((e) => e.current) ?? active[0])?.id ?? "";
}

export function packLabel(state: PackState | null | undefined): { text: string; tone: "ok" | "busy" | "off" } {
  switch (state?.status) {
    case "ready":
      return { text: "Pack ready", tone: "ok" };
    case "building":
      return { text: "Pack building (the call can start now)", tone: "busy" };
    case "stale":
      return { text: "Pack from older notes (the call can start now)", tone: "busy" };
    default:
      return { text: "No pack yet (built when the call starts)", tone: "off" };
  }
}

export const KIND_LABELS: Record<WorkplaceKind, string> = {
  work_item: "Work",
  commitment: "Commitments",
  decision: "Decisions",
  project: "Projects",
  system: "Systems",
  person: "People",
  term: "Terms",
};

/// A fact as one line: its title, its summary when it says more, and the state of work.
export function itemLine(item: MemoryItem): string {
  const state = typeof item.body.state === "string" ? item.body.state : "";
  const due = typeof item.body.due_date === "string" && item.body.due_date ? ` · due ${item.body.due_date}` : "";
  const summary = item.summary && item.summary !== item.title ? `: ${item.summary}` : "";
  const tag = state && state !== "open" ? ` [${state.replace("_", " ")}]` : "";
  return `${item.title}${summary}${tag}${due}`;
}

/// Facts grouped by kind, in the order the Facts screen shows them.
export function byKind(items: MemoryItem[]): [WorkplaceKind, MemoryItem[]][] {
  const order = Object.keys(KIND_LABELS) as WorkplaceKind[];
  return order
    .map((k) => [k, items.filter((i) => i.kind === k)] as [WorkplaceKind, MemoryItem[]])
    .filter(([, list]) => list.length > 0);
}
