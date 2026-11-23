import test from "node:test";
import assert from "node:assert/strict";
import { handoffText, importBriefText, parseAIConnections, parseBrief, parseBriefReceipt, parsePreparationRequest, parsePreparationRequests, preparationBody, publicationAttempt, reviewedBrief, withBriefReceipt, withPreparationRequest } from "../src/personalAIData.ts";

const now = Date.parse("2026-11-23T15:00:00Z");
const company = "019adf0b-7ad0-7000-8000-000000000001";
const scope = { title: "Delivery review", purpose: "call" as const, kind: "profile" as const, target: "", questions: ["What shipped?", "What remains?"], from: "2026-11-01", to: "2026-11-23" };
const fact = { id: "delivery", kind: "progress", text: "Shipped the reviewed change.", provenance: "sourced", source_ids: ["source"] };
const source = { id: "source", title: "Release notes", attribution: "Team notes", url: "https://example.test/release", source_date: "2026-11-22" };
const brief = { title: "Delivery review", prepared_at: "2026-11-23T14:00:00Z", items: [fact], sources: [source], coverage_gaps: ["Production monitoring was unavailable."] };
const request = () => parsePreparationRequest({ id: "request1", request_revision: 1, scope: preparationBody(scope), active_version_id: null, instructions: "Use permitted sources only.", expected_brief_format: { type: "object", properties: { title: { type: "string" } } } });

test("preparation is scoped to one chosen context and rejects empty or impossible ranges", () => {
  assert.equal(preparationBody({ ...scope, target: company }).employer_id, null);
  const work = preparationBody({ ...scope, kind: "company", target: company, purpose: "ask" });
  assert.equal(work.employer_id, company);
  assert.equal(work.job_id, null);
  assert.equal(work.purpose, "ask");
  assert.deepEqual(work.questions, scope.questions);
  assert.equal(preparationBody({ ...scope, kind: "application", target: company }).job_id, company);
  assert.throws(() => preparationBody({ ...scope, kind: "company" }), /Choose/);
  assert.throws(() => preparationBody({ ...scope, from: "2026-02-30" }), /valid dates/);
  assert.throws(() => preparationBody({ ...scope, from: "2025-01-01" }), /366/);
  assert.throws(() => preparationBody({ ...scope, to: "2026-01-01" }), /ordered/);
  assert.throws(() => preparationBody({ ...scope, questions: [] }), /at least one/);
  assert.throws(() => preparationBody({ ...scope, questions: Array(9).fill("Q") }), /at most 8/);
  assert.throws(() => preparationBody({ ...scope, questions: [" "] }), /1–1200/);
  assert.doesNotThrow(() => preparationBody({ ...scope, from: "2024-01-01", to: "2025-01-01" }));
});

test("pasted brief validates real sources, provenance, dates and optional server defaults", () => {
  const parsed = importBriefText("```json\n" + JSON.stringify(brief) + "\n```", now);
  assert.equal(parsed.items[0].source_ids[0], "source");
  assert.equal(parsed.sources[0].url, source.url);
  assert.equal(parsed.sources[0].source_date, source.source_date);
  assert.deepEqual(importBriefText(JSON.stringify({ title: "No access", prepared_at: brief.prepared_at, coverage_gaps: ["No linked sources"] }), now).items, []);
  assert.throws(() => importBriefText("Here is your brief", now), /not valid JSON/);
  assert.throws(() => parseBrief({ ...brief, request_id: company }, now), /unexpected field/);
  assert.throws(() => parseBrief({ ...brief, items: [{ ...fact, privileged: true }] }, now), /unexpected field/);
  assert.throws(() => parseBrief({ ...brief, items: [{ ...fact, provenance: "verified" }] }, now), /provenance/);
  assert.throws(() => parseBrief({ ...brief, items: [{ ...fact, kind: ["progress"] }] }, now), /supported kind/);
  assert.throws(() => parseBrief({ ...brief, items: [{ ...fact, provenance: ["sourced"] }] }, now), /provenance/);
  assert.throws(() => parseBrief({ ...brief, sources: [] }, now), /missing source/);
  assert.throws(() => parseBrief({ ...brief, items: [{ ...fact, source_ids: [] }] }, now), /linked source/);
  assert.throws(() => parseBrief({ ...brief, items: [fact, fact] }, now), /IDs must be unique/);
  assert.throws(() => parseBrief({ ...brief, sources: [source, source] }, now), /IDs must be unique/);
  assert.throws(() => parseBrief({ ...brief, sources: [{ ...source, url: "file:///private" }] }, now), /HTTP or HTTPS/);
  assert.throws(() => parseBrief({ ...brief, sources: [{ ...source, source_date: "2026-02-30" }] }, now), /valid dates/);
  assert.throws(() => parseBrief({ ...brief, prepared_at: "2026-11-23T14:00:00" }, now), /timezone/);
  assert.throws(() => parseBrief({ ...brief, prepared_at: "2026-11-24T14:00:00Z" }, now), /future/);
  assert.throws(() => parseBrief({ ...brief, prepared_at: "2026-02-30T14:00:00Z" }, now), /ISO date/);
  assert.throws(() => parseBrief({ ...brief, items: [], coverage_gaps: [] }, now), /at least one/);
});

test("brief limits count Unicode characters and the actual UTF-8 payload", () => {
  assert.doesNotThrow(() => parseBrief({ ...brief, items: [{ ...fact, text: "🚀".repeat(1200) }] }, now));
  assert.throws(() => parseBrief({ ...brief, items: [{ ...fact, text: "界".repeat(1201) }] }, now), /1–1200/);
  assert.throws(() => parseBrief({ ...brief, items: Array.from({ length: 10 }, (_, i) => ({ ...fact, id: `fact${i}`, text: "界".repeat(1200) })) }, now), /32 KiB/);
  assert.throws(() => parseBrief({ ...brief, items: Array.from({ length: 41 }, (_, i) => ({ ...fact, id: `fact${i}` })) }, now), /at most 40/);
  assert.throws(() => importBriefText(" ".repeat(262145), now), /too long/);
});

test("review excludes only selected facts, preserves source references and never mutates original", () => {
  const parsed = parseBrief({ ...brief, items: [fact, { ...fact, id: "next", kind: "next_step" }] }, now);
  const reviewed = reviewedBrief(parsed, new Set(["delivery"]));
  assert.deepEqual(reviewed.items.map(item => item.id), ["next"]);
  assert.deepEqual(reviewed.items[0].source_ids, ["source"]);
  assert.equal(parsed.items.length, 2);
  const empty = reviewedBrief({ ...parsed, coverage_gaps: [] }, new Set(["delivery", "next"]));
  assert.equal(empty.items.length, 0);
  assert.match(empty.coverage_gaps[0], /excluded during review/);
  assert.doesNotThrow(() => parseBrief(empty, now));
});

test("network retries reuse the publication key only for the same request, revision and reviewed facts", () => {
  let sequence = 0;
  const makeKey = () => `key-${++sequence}`;
  const r = request(), b = parseBrief(brief, now);
  const first = publicationAttempt(r, b, null, makeKey);
  const retry = publicationAttempt(r, parseBrief(JSON.parse(JSON.stringify(brief)), now), first, makeKey);
  assert.equal(retry, first);
  assert.equal(sequence, 1);
  assert.notEqual(publicationAttempt({ ...r, request_revision: 2 }, b, retry, makeKey).key, first.key);
  assert.notEqual(publicationAttempt({ ...r, id: "other" }, b, retry, makeKey).key, first.key);
  assert.notEqual(publicationAttempt(r, reviewedBrief(b, new Set(["delivery"])), retry, makeKey).key, first.key);
});

test("validated server responses bind displayed brief to active version and preserve handoff scope", () => {
  const r = request();
  const ready = parsePreparationRequest({ ...r, active_version_id: "version1", active_brief: { id: "version1", brief, origin: "mcp" } });
  assert.equal(ready.active_brief?.brief.items[0].text, fact.text);
  assert.deepEqual(parsePreparationRequests({ requests: [ready] }), [ready]);
  assert.throws(() => parsePreparationRequest({ ...ready, active_version_id: "newer" }), /brief changed/);
  assert.throws(() => parsePreparationRequest({ ...r, request_revision: 0 }), /invalid request revision/);
  assert.throws(() => parsePreparationRequest({ ...r, expected_brief_format: [] }));
  assert.throws(() => parsePreparationRequests({ requests: null }));
  const handoff = handoffText({ ...ready, request_revision: 7 });
  assert.match(handoff, /"request_revision": 7/);
  assert.match(handoff, /"date_from": "2026-11-01"/);
  assert.match(handoff, /other sources you actually have permission to access/);
  assert.match(handoff, /without the publication envelope/);
});

test("AI connections cannot render malformed expiry or client data", () => {
  const connection = { id: "connection1", client_id: "Desktop AI", scope: "context:read context:publish", expires_at: now / 1000 };
  assert.deepEqual(parseAIConnections({ connections: [connection] }), [connection]);
  assert.throws(() => parseAIConnections({ connections: [{ ...connection, expires_at: "tomorrow" }] }), /expiry/);
  assert.throws(() => parseAIConnections({ connections: [{ ...connection, expires_at: 1e20 }] }), /expiry/);
  assert.throws(() => parseAIConnections({ connections: [{ ...connection, client_id: {} }] }));
});

test("publish and correction receipts must acknowledge this exact request before success is shown", () => {
  const receipt = { request_id: "request1", version_id: "version1", duplicate: false };
  assert.deepEqual(parseBriefReceipt(receipt, "request1", true), { request_id: "request1", version_id: "version1" });
  assert.doesNotThrow(() => parseBriefReceipt({ ...receipt, duplicate: true }, "request1", true));
  assert.doesNotThrow(() => parseBriefReceipt({ request_id: "request1", version_id: "version2" }, "request1"));
  assert.throws(() => parseBriefReceipt({ ...receipt, request_id: "other" }, "request1", true), /did not confirm/);
  assert.throws(() => parseBriefReceipt({ ...receipt, version_id: null }, "request1", true), /did not confirm/);
  assert.throws(() => parseBriefReceipt({ request_id: "request1", version_id: "version2" }, "request1", true), /did not confirm/);
});


test("accepted writes remain in the request list without trusting stale brief content when follow-up reads fail", () => {
  const original = request();
  const created = withPreparationRequest([], original);
  assert.equal(created[0].id, original.id);
  const oldBrief = { ...original, active_version_id: "old", active_brief: { id: "old", brief: parseBrief(brief, now), origin: "mcp" } };
  const receipt = { request_id: original.id, version_id: "accepted" };
  const pendingRead = withBriefReceipt(oldBrief, receipt);
  assert.equal(pendingRead.active_version_id, "accepted");
  assert.equal(pendingRead.active_brief, undefined);
  const returned = withPreparationRequest(created, pendingRead);
  assert.equal(returned.length, 1);
  assert.equal(returned[0].active_version_id, "accepted");
  assert.equal(created[0].active_version_id, null);
  assert.throws(() => withBriefReceipt(original, { ...receipt, request_id: "other" }), /another request/);
});
