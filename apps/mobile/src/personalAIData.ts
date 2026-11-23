import { list, record, text, validDay } from "./workspaceData.ts";

export type Purpose = "call" | "practice" | "ask";
export type ContextKind = "profile" | "company" | "application";
export type Preparation = { title: string; purpose: Purpose; employer_id: string | null; job_id: string | null; questions: string[]; date_from: string; date_to: string };
export type BriefSource = { id: string; title: string; url: string | null; attribution: string; source_date: string | null };
export type BriefFact = { id: string; kind: "progress" | "decision" | "blocker" | "next_step" | "commitment" | "term" | "example"; text: string; provenance: "sourced" | "user_report" | "inference"; source_ids: string[] };
export type Brief = { title: string; prepared_at: string; items: BriefFact[]; sources: BriefSource[]; coverage_gaps: string[] };
export type BriefVersion = { id: string; brief: Brief; origin: string };
export type PreparationRequest = { id: string; request_revision: number; scope: Preparation; active_version_id: string | null; active_brief?: BriefVersion; expected_brief_format: Record<string, unknown>; instructions: string };
export type AIConnection = { id: string; client_id: string; scope: string; expires_at: number };
export type ImportAttempt = { fingerprint: string; key: string };

function bounded(value: unknown, label: string, max: number): string {
  if (typeof value !== "string" || !value.trim() || [...value.trim()].length > max) throw new Error(`${label} must contain 1–${max} characters.`);
  return value.trim();
}
function identifier(value: unknown): string {
  const id = bounded(value, "Item and source IDs", 60);
  if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error("Item and source IDs can contain only letters, numbers, underscores and hyphens.");
  return id;
}
function knownKeys(r: Record<string, unknown>, keys: string[], label: string) {
  const unexpected = Object.keys(r).find(key => !keys.includes(key));
  if (unexpected) throw new Error(`${label} has an unexpected field: ${unexpected}. Paste only the brief JSON from the AI request.`);
}
function boundedList<T>(value: unknown, parse: (item: unknown) => T, max: number, label: string): T[] {
  if (!Array.isArray(value) || value.length > max) throw new Error(`${label} must be a list of at most ${max} entries.`);
  return value.map(parse);
}
function day(value: unknown): string {
  if (typeof value !== "string" || !validDay(value)) throw new Error("Enter valid dates as YYYY-MM-DD.");
  return value;
}
function optionalId(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) throw new Error("Choose a valid company or application.");
  return value;
}
export function parsePreparation(value: unknown): Preparation {
  const r = record(value);
  const purpose = r.purpose ?? "call";
  if (purpose !== "call" && purpose !== "practice" && purpose !== "ask") throw new Error("Choose calls, interview practice, or Ask as the purpose.");
  const employer_id = optionalId(r.employer_id), job_id = optionalId(r.job_id);
  if (employer_id && job_id) throw new Error("Choose one company or application.");
  const date_from = day(r.date_from), date_to = day(r.date_to);
  if (date_to < date_from || (Date.parse(`${date_to}T00:00:00Z`) - Date.parse(`${date_from}T00:00:00Z`)) / 86400000 > 366) throw new Error("Choose an ordered date range of at most 366 days.");
  const questions = boundedList(r.questions, q => bounded(q, "Each question", 1200), 8, "Questions");
  if (!questions.length) throw new Error("Add at least one preparation question.");
  return { title: bounded(r.title, "Title", 200), purpose, employer_id, job_id, questions, date_from, date_to };
}
export function preparationBody(fields: { title: string; purpose: Purpose; kind: ContextKind; target: string; questions: string[]; from: string; to: string }): Preparation {
  if (fields.kind !== "profile" && !fields.target) throw new Error("Choose the company or application for this request.");
  return parsePreparation({ title: fields.title, purpose: fields.purpose, employer_id: fields.kind === "company" ? fields.target : null, job_id: fields.kind === "application" ? fields.target : null, questions: fields.questions, date_from: fields.from, date_to: fields.to });
}
export function safeSourceURL(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value !== "string") throw new Error("Source links must be HTTP or HTTPS URLs.");
  try { const url = new URL(value.trim()); if ((url.protocol === "http:" || url.protocol === "https:") && url.hostname) return url.href; } catch { /* Show one actionable validation message. */ }
  throw new Error("Source links must be HTTP or HTTPS URLs.");
}
export function parseBrief(value: unknown, now = Date.now()): Brief {
  const r = record(value);
  knownKeys(r, ["title", "prepared_at", "items", "sources", "coverage_gaps"], "Brief");
  const prepared_at = bounded(r.prepared_at, "Preparation time", 64);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(prepared_at) || !validDay(prepared_at.slice(0, 10)) || !Number.isFinite(Date.parse(prepared_at))) throw new Error("prepared_at must be an ISO date and time with a timezone, such as 2026-11-23T12:00:00Z.");
  if (Date.parse(prepared_at) > now + 300000) throw new Error("The brief's preparation time is in the future. Ask your AI to correct prepared_at.");
  const sources = boundedList(r.sources ?? [], value => {
    const s = record(value); knownKeys(s, ["id", "title", "url", "attribution", "source_date"], "Source");
    return { id: identifier(s.id), title: bounded(s.title, "Source title", 250), url: safeSourceURL(s.url), attribution: bounded(s.attribution, "Source attribution", 300), source_date: s.source_date == null ? null : day(s.source_date) };
  }, 30, "Sources");
  const sourceIDs = new Set(sources.map(s => s.id));
  if (sourceIDs.size !== sources.length) throw new Error("Source IDs must be unique.");
  const items = boundedList(r.items ?? [], value => {
    const item = record(value); knownKeys(item, ["id", "kind", "text", "provenance", "source_ids"], "Brief item");
    if (typeof item.kind !== "string" || !["progress", "decision", "blocker", "next_step", "commitment", "term", "example"].includes(item.kind)) throw new Error("Each item needs a supported kind: progress, decision, blocker, next_step, commitment, term, or example.");
    if (typeof item.provenance !== "string" || !["sourced", "user_report", "inference"].includes(item.provenance)) throw new Error("Each item must identify its provenance as sourced, user_report, or inference.");
    const source_ids = boundedList(item.source_ids ?? [], identifier, 8, "Item source references");
    if (item.provenance === "sourced" && !source_ids.length) throw new Error("Sourced items need at least one linked source.");
    if (source_ids.some(id => !sourceIDs.has(id))) throw new Error("A brief item refers to a missing source. Ask your AI to include that source.");
    return { id: identifier(item.id), kind: item.kind as BriefFact["kind"], text: bounded(item.text, "Brief item text", 1200), provenance: item.provenance as BriefFact["provenance"], source_ids };
  }, 40, "Brief items");
  if (new Set(items.map(i => i.id)).size !== items.length) throw new Error("Brief item IDs must be unique.");
  const coverage_gaps = boundedList(r.coverage_gaps ?? [], gap => bounded(gap, "Coverage gap", 1200), 10, "Coverage gaps");
  if (!items.length && !coverage_gaps.length) throw new Error("The brief needs at least one item or an explanation of its coverage gaps.");
  const result = { title: bounded(r.title, "Brief title", 200), prepared_at, items, sources, coverage_gaps };
  if (new TextEncoder().encode(JSON.stringify(result)).length > 32768) throw new Error("Brief must be at most 32 KiB. Ask your AI for a shorter brief.");
  return result;
}
export function importBriefText(raw: string, now = Date.now()): Brief {
  if (raw.length > 262144) throw new Error("The pasted response is too long. Paste only the brief JSON, up to 32 KiB.");
  const json = raw.trim().replace(/^```(?:json)?\s*\n?([\s\S]*?)\n?```$/i, "$1");
  let value: unknown;
  try { value = JSON.parse(json); } catch { throw new Error("This is not valid JSON. Paste the complete brief object returned by your AI."); }
  return parseBrief(value, now);
}
export function reviewedBrief(brief: Brief, excluded: ReadonlySet<string>): Brief {
  const items = brief.items.filter(item => !excluded.has(item.id));
  return { ...brief, items, coverage_gaps: items.length || brief.coverage_gaps.length ? brief.coverage_gaps : ["All imported items excluded during review."] };
}
export function parsePreparationRequest(value: unknown): PreparationRequest {
  const r = record(value);
  if (!Number.isSafeInteger(r.request_revision) || Number(r.request_revision) < 1) throw new Error("The server returned an invalid request revision. Refresh and retry.");
  const active_version_id = r.active_version_id == null ? null : text(r.active_version_id);
  let active_brief: BriefVersion | undefined;
  if (r.active_brief != null) {
    const b = record(r.active_brief);
    active_brief = { id: text(b.id), brief: parseBrief(b.brief, Number.POSITIVE_INFINITY), origin: text(b.origin) };
    if (active_brief.id !== active_version_id) throw new Error("The brief changed while loading. Refresh this request.");
  }
  return { id: text(r.id), request_revision: Number(r.request_revision), scope: parsePreparation(r.scope), active_version_id, ...(active_brief ? { active_brief } : {}), instructions: text(r.instructions), expected_brief_format: record(r.expected_brief_format) };
}
export const parsePreparationRequests = (value: unknown) => list(record(value).requests, parsePreparationRequest);
export const parseAIConnections = (value: unknown): AIConnection[] => list(record(value).connections, value => {
  const r = record(value);
  if (typeof r.expires_at !== "number" || !Number.isFinite(r.expires_at) || r.expires_at < 0 || r.expires_at > 8640000000000) throw new Error("The server returned an invalid AI connection expiry.");
  return { id: text(r.id), client_id: text(r.client_id), scope: text(r.scope), expires_at: r.expires_at };
});
export function publicationAttempt(request: PreparationRequest, brief: Brief, previous: ImportAttempt | null, makeKey: () => string): ImportAttempt {
  const fingerprint = JSON.stringify([request.id, request.request_revision, brief]);
  return previous?.fingerprint === fingerprint ? previous : { fingerprint, key: makeKey() };
}
export function parseBriefReceipt(value: unknown, requestId: string, publication = false): { version_id: string; request_id: string } {
  const r = record(value);
  if (r.request_id !== requestId || typeof r.version_id !== "string" || !r.version_id || (publication && typeof r.duplicate !== "boolean")) throw new Error("The server did not confirm this brief update. Refresh the request before retrying.");
  return { version_id: r.version_id, request_id: requestId };
}
/** An accepted version pointer is durable even while its follow-up read is offline. */
export function withBriefReceipt(request: PreparationRequest, receipt: { request_id: string; version_id: string }): PreparationRequest {
  if (receipt.request_id !== request.id) throw new Error("The brief receipt belongs to another request.");
  const { active_brief: _previousBrief, ...rest } = request;
  return { ...rest, active_version_id: receipt.version_id };
}
export function withPreparationRequest(requests: PreparationRequest[], value: PreparationRequest): PreparationRequest[] {
  return requests.some(request => request.id === value.id) ? requests.map(request => request.id === value.id ? value : request) : [value, ...requests];
}
export function handoffText(r: PreparationRequest): string {
  return `Prepare context for CoRuby from this conversation and other sources you actually have permission to access. Do not assume access to other chats or memory. Preserve stable item IDs on refresh and state coverage gaps.\n\nIf connected, call get_context_request with request_id "${r.id}" for current scope and revision, then publish_context_brief with that request_id, request_revision and a unique idempotency_key.\n\nIf the tools are unavailable, return only brief JSON matching the schema below for manual import, without the publication envelope.\n\nPreparation request:\n${JSON.stringify({ request_id: r.id, request_revision: r.request_revision, scope: r.scope, instructions: r.instructions, expected_brief_format: r.expected_brief_format }, null, 2)}`;
}
