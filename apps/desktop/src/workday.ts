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
  flow: "How it works",
  constraint: "Limits",
  risk: "Open risks",
  person: "People",
  term: "Terms",
};

/// What a fact from a document carries besides its one line: a decision's reason, what was
/// rejected and the tradeoff; a component's neighbours; a flow's steps.
export function itemDetails(item: MemoryItem): string[] {
  const text = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  const list = (v: unknown) => (Array.isArray(v) ? v.map((x) => String(x).trim()).filter(Boolean) : []);
  const out: string[] = [];
  if (text(item.body.why)) out.push(`Why: ${text(item.body.why)}`);
  if (list(item.body.alternatives).length) out.push(`Rejected: ${list(item.body.alternatives).join("; ")}`);
  if (text(item.body.tradeoff)) out.push(`Tradeoff: ${text(item.body.tradeoff)}`);
  if (list(item.body.talks_to).length) out.push(`Talks to: ${list(item.body.talks_to).join(", ")}`);
  list(item.body.steps).forEach((s, n) => out.push(`${n + 1}. ${s}`));
  return out;
}

/// The type a standup gives a question, as the test box names it.
export const QUESTION_TYPE_LABELS: Record<string, string> = {
  decision: "Decision",
  how_it_works: "How it works",
  status_follow_up: "Status follow-up",
  estimate_risk: "Estimate or risk",
  standup_status: "Status",
  technical: "Technical",
  system_design: "Design",
  respond: "Reply",
  other: "Other",
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
