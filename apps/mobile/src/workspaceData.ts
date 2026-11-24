export type Employer = {
  id: string;
  name: string;
  title: string;
  team: string;
  status: "active" | "archived";
  started_on: string | null;
  ended_on: string | null;
};
export type WorkFile = {
  id: string;
  name: string;
  status: string;
  error: string | null;
};
export type MemoryItem = {
  id: string;
  kind: string;
  title: string;
  summary: string;
  status: string;
  day: string | null;
};
export type WorkLog = {
  id: string;
  day: string;
  text: string;
  status: string;
  revision?: number;
  attachments: WorkFile[];
  items: MemoryItem[];
};
export type Proposal = {
  id: string;
  title: string;
  summary: string;
  evidence: string;
  document_name: string | null;
};
export type WorkDocument = WorkFile & {
  headings: string[];
  facts: MemoryItem[];
};

export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error(
      "The server returned an invalid workspace response. Refresh and try again.",
    );
  return value as Record<string, unknown>;
}
export function text(value: unknown): string {
  if (typeof value !== "string")
    throw new Error(
      "The server returned an invalid workspace field. Refresh and try again.",
    );
  return value;
}
export const nullableText = (value: unknown) =>
  value == null ? null : text(value);
export function list<T>(value: unknown, parse: (item: unknown) => T): T[] {
  if (!Array.isArray(value))
    throw new Error(
      "The server returned an invalid workspace list. Refresh and try again.",
    );
  return value.map(parse);
}
export function parseEmployer(value: unknown): Employer {
  const r = record(value);
  if (r.status !== "active" && r.status !== "archived")
    throw new Error("Unrecognized company status. Update the app and retry.");
  return {
    id: text(r.id),
    name: text(r.name),
    title: text(r.title),
    team: text(r.team),
    status: r.status,
    started_on: nullableText(r.started_on),
    ended_on: nullableText(r.ended_on),
  };
}
export const parseEmployers = (value: unknown) =>
  list(record(value).employers, parseEmployer);
export function parseWorkFile(value: unknown): WorkFile {
  const r = record(value);
  return {
    id: text(r.id),
    name: text(r.name),
    status: r.status == null ? "saved" : text(r.status),
    error: nullableText(r.error),
  };
}
export function parseMemory(value: unknown): MemoryItem {
  const r = record(value);
  return {
    id: text(r.id),
    kind: text(r.kind),
    title: text(r.title),
    summary: text(r.summary),
    status: text(r.status),
    day: nullableText(r.day),
  };
}
export function parseLog(value: unknown): WorkLog {
  const r = record(value);
  if (
    r.revision !== undefined &&
    (!Number.isSafeInteger(r.revision) || Number(r.revision) < 0)
  )
    throw new Error("Invalid work log revision. Refresh before editing.");
  return {
    id: text(r.id),
    day: text(r.day),
    text: text(r.text),
    status: text(r.status),
    ...(typeof r.revision === "number" ? { revision: r.revision } : {}),
    attachments: list(r.attachments, parseWorkFile),
    items: list(r.items, parseMemory),
  };
}
export function parseLogs(value: unknown) {
  const r = record(value);
  return { logs: list(r.logs, parseLog), next: nullableText(r.next_before) };
}
export function parseProposal(value: unknown): Proposal {
  const r = record(value);
  return {
    id: text(r.id),
    title: text(r.title),
    summary: text(r.summary),
    evidence: nullableText(r.evidence) ?? "",
    document_name: nullableText(r.document_name),
  };
}
export function parseDocument(value: unknown): WorkDocument {
  const r = record(value);
  return {
    ...parseWorkFile(r),
    headings: list(r.headings, text),
    facts: list(r.facts, parseMemory),
  };
}
/** Date-only fields use the displayed local date, never a UTC day shifted by timezone. */
export function localDay(now = new Date()): string {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}
export function validDay(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000"))
    return false;
  const date = new Date(`${value}T12:00:00Z`);
  return (
    !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value
  );
}
export function employerBody(fields: {
  name: string;
  title: string;
  team: string;
  started: string;
  ended: string;
}) {
  if (!fields.name.trim() || fields.name.trim().length > 200)
    throw new Error("Enter a company name of 1–200 characters.");
  if (fields.title.length > 200 || fields.team.length > 200)
    throw new Error("Role and team must be at most 200 characters.");
  if (
    (fields.started && !validDay(fields.started)) ||
    (fields.ended && !validDay(fields.ended))
  )
    throw new Error("Enter dates as YYYY-MM-DD.");
  if (fields.started && fields.ended && fields.ended < fields.started)
    throw new Error("The end date must be on or after the start date.");
  return {
    name: fields.name.trim(),
    title: fields.title.trim(),
    team: fields.team.trim(),
    started_on: fields.started || null,
    ended_on: fields.ended || null,
  };
}
