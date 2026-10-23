// The overlay's conversation view: order, both speakers, echo, the automatic deep pass, and
// the rebuild from the core's (compacted) snapshot after a reload or reconnect.
import { describe, expect, it } from "vitest";
import type { ServerMessage } from "@copilot/protocol";
import type { Numbered, UiEvent } from "../src/api";
import { conversation, initial, reduce, snapshotApplier, type SessionView } from "../src/session";

let seq = 0;
const server = (m: Record<string, unknown>): UiEvent => ({ kind: "server", message: { seq: ++seq, ...m } as unknown as ServerMessage });
const run = (events: UiEvent[], from: SessionView = { ...initial, running: true }) => events.reduce((s, event) => reduce(s, { type: "event", event }), from);

const ready = (deep: string[] | undefined) =>
  server({ type: "session.ready", session_id: "s", server_time: "2026-10-22T10:00:00Z", limits: { max_minutes: 240 }, me_audio: true, deep_auto_types: deep });
const partial = (channel: string, seg: number, text: string) => server({ type: "transcript.partial", channel, seg_id: seg, text });
const final = (channel: string, seg: number, text: string, extra: Record<string, unknown> = {}) =>
  server({ type: "transcript.final", channel, seg_id: seg, text, start_ms: seg * 1000, end_ms: seg * 1000 + 900, ...extra });
const detected = (id: string, type = "technical", speculative = false) =>
  server({ type: "question.detected", question_id: id, text: `Question ${id}?`, question_type: type, confidence: 0.9, speculative });
const delta = (id: string, pass: string, text: string) => server({ type: "answer.delta", question_id: id, pass, text });
const done = (id: string, pass: string) =>
  server({ type: "answer.done", question_id: id, pass, memory_refs: [], usage: { input_tokens: 1, cached_tokens: 0, output_tokens: 1 }, source: "model" });

/// A short call: the other side asks, the user answers, a follow-up, an echo.
function call(): UiEvent[] {
  return [
    ready(["behavioral", "technical", "system_design", "coding", "standup_status", "other"]),
    partial("them", 0, "How did"),
    partial("them", 0, "How did you scale"),
    final("them", 0, "How did you scale the ledger?"),
    detected("q1", "technical", true),
    delta("q1", "fast", "- Sharded by account [n:3f2a]\n"),
    detected("q1", "technical", false),
    delta("q1", "fast", "- Reads to replicas\n"),
    done("q1", "fast"),
    partial("me", 0, "We sharded"),
    delta("q1", "deep", "Direct answer: shard by account\n"),
    final("me", 0, "We sharded it by account."),
    delta("q1", "deep", "Tradeoff: cross-shard reports\n"),
    done("q1", "deep"),
    partial("me", 1, "Why"), // the mic heard the other side...
    final("me", 1, "", { echo: true }), // ...and the gateway dropped it
    final("them", 1, "Why by account?"),
    detected("q2", "other"),
    detected("q3", "technical", true),
    server({ type: "question.cancelled", question_id: "q3" }),
    delta("q2", "fast", "- Even load\n"),
  ];
}

describe("conversation view", () => {
  it("shows both sides and the answers in the order they came", () => {
    const items = conversation(run(call()));
    expect(items.map((i) => (i.kind === "question" ? `Q:${i.question.id}` : `${i.kind}:${i.text}`))).toEqual([
      "them:How did you scale the ledger?",
      "Q:q1",
      "me:We sharded it by account.",
      "them:Why by account?",
      "Q:q2", // q3 was a cancelled speculative question: not shown
    ]);
    const q1 = items[1].kind === "question" ? items[1].question : null;
    expect(q1?.fast.text).toBe("- Sharded by account [n:3f2a]\n- Reads to replicas\n");
    expect(q1?.deep.text).toBe("Direct answer: shard by account\nTradeoff: cross-shard reports\n");
  });

  it("shows live partial text that settles into the final", () => {
    const s = run([partial("them", 0, "Tell me")]);
    expect(conversation(s)).toEqual([{ kind: "them", key: "them:0", text: "Tell me", final: false }]);
    const s2 = run([final("them", 0, "Tell me about it.")], s);
    expect(conversation(s2)).toEqual([{ kind: "them", key: "them:0", text: "Tell me about it.", final: true }]);
  });

  it("never drops earlier messages in a long call", () => {
    const events: UiEvent[] = [];
    for (let i = 0; i < 300; i++) {
      events.push(final("them", i, `line ${i}`), detected(`q${i}`), delta(`q${i}`, "fast", "- x\n"), final("me", i, `mine ${i}`));
    }
    const items = conversation(run(events));
    expect(items).toHaveLength(900);
    expect(items[0]).toMatchObject({ kind: "them", text: "line 0" });
  });

  it("expects the deep answer for the types session.ready names", () => {
    const s = run([ready(["behavioral", "technical", "system_design", "coding", "standup_status", "other"]), detected("q1", "other")]);
    expect(s.questions[0].deepRequested).toBe(true);
    const old = run([detected("q1", "other")]); // an older gateway: the three types
    expect(old.questions[0].deepRequested).toBe(false);
    const off = run([ready([]), detected("q1", "behavioral")]);
    expect(off.questions[0].deepRequested).toBe(false);
  });
});

/// What src-tauri/src/history.rs keeps: partials and finals in their segment's first place,
/// one answer pass's deltas merged into its first, numbered by the newest event folded in.
function compact(events: UiEvent[]): { entries: Numbered[]; next: number } {
  const entries: Numbered[] = [];
  const slots = new Map<string, number>();
  events.forEach((event, n) => {
    let key: string | null = null;
    let merge = false;
    if (event.kind === "server") {
      const m = event.message;
      if (m.type === "transcript.partial" || m.type === "transcript.final") key = `seg:${m.channel}:${m.seg_id}`;
      if (m.type === "answer.delta") [key, merge] = [`delta:${m.question_id}:${m.pass}`, true];
    }
    const at = key === null ? undefined : slots.get(key);
    if (at !== undefined) {
      const old = entries[at];
      if (merge && old.event.kind === "server" && old.event.message.type === "answer.delta" && event.kind === "server" && event.message.type === "answer.delta") {
        entries[at] = { n, event: { kind: "server", message: { ...old.event.message, text: old.event.message.text + event.message.text } } };
      } else {
        entries[at] = { n, event };
      }
      return;
    }
    if (key !== null) slots.set(key, entries.length);
    entries.push({ n, event });
  });
  return { entries, next: events.length };
}

function rebuild(entries: Numbered[], next: number, live: Numbered[] = []): SessionView {
  let s: SessionView = initial;
  const applier = snapshotApplier((a) => {
    s = reduce(s, a);
  });
  applier.snapshot(entries, true, next);
  for (const e of live) applier.live(e);
  return s;
}

describe("rebuild after a reload or reconnect", () => {
  it("gives the same conversation from the compacted snapshot", () => {
    const events = call();
    const whole = run(events);
    const { entries, next } = compact(events);
    expect(entries.length).toBeLessThan(events.length);
    const again = rebuild(entries, next);
    expect(conversation(again)).toEqual(conversation(whole));
    expect(again.deepAuto).toEqual(whole.deepAuto);
  });

  it("applies live events from the snapshot's next number on, once", () => {
    const events = call();
    const { entries, next } = compact(events);
    const later = delta("q2", "fast", "- Hot accounts split\n");
    // An event the snapshot already holds comes in live as well (it was in flight): skipped.
    const s = rebuild(entries, next, [
      { n: next - 1, event: events[events.length - 1] },
      { n: next, event: later },
    ]);
    const q2 = s.questions.find((q) => q.id === "q2");
    expect(q2?.fast.text).toBe("- Even load\n- Hot accounts split\n");
  });

  it("starts over when a new session's numbering starts again", () => {
    const { entries, next } = compact(call());
    const s = rebuild(entries, next, [{ n: 0, event: final("them", 0, "A new call") }]);
    expect(conversation(s)).toEqual([{ kind: "them", key: "them:0", text: "A new call", final: true }]);
  });
});
