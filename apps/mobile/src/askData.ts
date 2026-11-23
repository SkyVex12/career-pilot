export const scopeKinds = [
  "auto",
  "general",
  "all",
  "company",
  "application",
  "document",
  "call",
] as const;
export type ScopeKind = (typeof scopeKinds)[number];
export type Scope = {
  kind: ScopeKind;
  id?: string;
  from_date?: string;
  to_date?: string;
};
export type Source = {
  kind: string;
  id: string;
  label: string;
  citation?: string;
  start_ms?: number;
};
export type Message = {
  id: string;
  question: string;
  answer: string;
  status: "pending" | "done" | "failed" | "cancelled";
  error?: string;
  sources: Source[];
};
export type Conversation = {
  id: string;
  title: string;
  scope: Scope;
  messages?: Message[];
  retrieval_notice?: string;
  retention_notice?: string;
};
export type SourceDetail = {
  label: string;
  content?: string;
  excerpt?: string;
  date?: string;
  start_ms?: number;
};
const invalid = () =>
  new Error(
    "The server returned an incomplete Work response. Retry, or update the server to the current CoRuby version.",
  );
function object(v: unknown): Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v)) throw invalid();
  return v as Record<string, unknown>;
}
function string(v: unknown): string {
  if (typeof v !== "string") throw invalid();
  return v;
}
function optional(v: unknown): string | undefined {
  return v == null ? undefined : string(v);
}
function list(v: unknown): unknown[] {
  if (!Array.isArray(v)) throw invalid();
  return v;
}
export function needsItem(kind: string): boolean {
  return ["company", "application", "document", "call"].includes(kind);
}
function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return (
    Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
  );
}
export function makeScope(kind: string, id = "", from = "", to = ""): Scope {
  if (!(scopeKinds as readonly string[]).includes(kind))
    throw new Error("Choose a supported context.");
  if (needsItem(kind) && !id.trim())
    throw new Error("Choose a context item first.");
  for (const value of [from, to])
    if (value && !validDate(value))
      throw new Error("Use a real calendar date in YYYY-MM-DD format.");
  if (from && to && from > to)
    throw new Error("The end date must be on or after the start date.");
  return {
    kind: kind as ScopeKind,
    ...(needsItem(kind) ? { id } : {}),
    ...(from ? { from_date: from } : {}),
    ...(to ? { to_date: to } : {}),
  };
}
function parseScope(value: unknown): Scope {
  const v = object(value);
  return makeScope(
    string(v.kind),
    optional(v.id),
    optional(v.from_date),
    optional(v.to_date),
  );
}
function parseSource(value: unknown): Source {
  const v = object(value),
    kind = string(v.kind),
    id = string(v.id);
  if (!/^[a-z_]+$/.test(kind) || !id) throw invalid();
  const start = v.start_ms;
  if (
    start != null &&
    (typeof start !== "number" || !Number.isFinite(start) || start < 0)
  )
    throw invalid();
  return {
    kind,
    id,
    label: string(v.label),
    citation: optional(v.citation),
    ...(typeof start === "number" ? { start_ms: start } : {}),
  };
}
export function parseMessage(value: unknown): Message {
  const v = object(value),
    status = string(v.status);
  if (!["pending", "done", "failed", "cancelled"].includes(status))
    throw invalid();
  return {
    id: string(v.id),
    question: string(v.question),
    answer: string(v.answer),
    status: status as Message["status"],
    error: optional(v.error),
    sources: list(v.sources).map(parseSource),
  };
}
export function withMessage(conversation: Conversation, message: Message): Conversation {
  const messages = conversation.messages ?? [];
  return {
    ...conversation,
    messages: messages.some((item) => item.id === message.id)
      ? messages.map((item) => (item.id === message.id ? message : item))
      : [...messages, message],
  };
}
export function parseConversation(
  value: unknown,
  detail = false,
): Conversation {
  const v = object(value);
  return {
    id: string(v.id),
    title: string(v.title),
    scope: parseScope(v.scope),
    ...(detail ? { messages: list(v.messages).map(parseMessage) } : {}),
    retrieval_notice: optional(v.retrieval_notice),
    retention_notice: optional(v.retention_notice),
  };
}
export function parseConversations(value: unknown): Conversation[] {
  return list(object(value).conversations).map((v) => parseConversation(v));
}
export function parseOptions(value: unknown): { id: string; label: string }[] {
  return list(object(value).options).map((v) => {
    const o = object(v);
    return { id: string(o.id), label: string(o.label) };
  });
}
export function parseSourceDetail(value: unknown): SourceDetail {
  const v = object(value),
    start = v.start_ms;
  if (
    start != null &&
    (typeof start !== "number" || !Number.isFinite(start) || start < 0)
  )
    throw invalid();
  return {
    label: string(v.label),
    content: optional(v.content),
    excerpt: optional(v.excerpt),
    date: optional(v.date),
    ...(typeof start === "number" ? { start_ms: start } : {}),
  };
}
