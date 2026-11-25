// The overlay's state: questions, streamed answers, cancellation, the status badge.
import { describe, expect, it } from "vitest";
import type { ServerMessage } from "@copilot/protocol";
import type { UiEvent } from "../src/api";
import { badge, initial, reduce, type SessionView } from "../src/session";

const QID = "0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210";
const QID2 = "0192f3c4-1d0a-7c55-b2e1-77c0a9d4e211";
let seq = 0;

function server(m: Record<string, unknown>): UiEvent {
  return { kind: "server", message: { seq: ++seq, ...m } as unknown as ServerMessage };
}

function run(events: UiEvent[], from: SessionView = { ...initial, running: true }): SessionView {
  return events.reduce((s, event) => reduce(s, { type: "event", event }), from);
}

const live: UiEvent = { kind: "connection", state: { state: "live", session_id: QID, resumed: false } };
const detected = (id: string, type = "behavioral", speculative = false) =>
  server({ type: "question.detected", question_id: id, text: "Tell me about a time?", question_type: type, confidence: 0.9, speculative });

describe("session reducer", () => {
  it("streams both passes into one question", () => {
    const s = run([
      live,
      detected(QID, "behavioral", true),
      server({ type: "answer.delta", question_id: QID, pass: "fast", text: "- Led the migration [n:3f2a]\n" }),
      detected(QID, "behavioral", false),
      server({ type: "answer.delta", question_id: QID, pass: "fast", text: "- Cut p95 40%\n" }),
      server({ type: "answer.done", question_id: QID, pass: "fast", memory_refs: [{ node_id: QID, label: "Payments" }], usage: { input_tokens: 1, cached_tokens: 0, output_tokens: 1 }, source: "model" }),
      server({ type: "answer.delta", question_id: QID, pass: "deep", text: "Situation: ..." }),
    ]);
    expect(s.questions).toHaveLength(1);
    const q = s.questions[0];
    expect(q.speculative).toBe(false);
    expect(q.fast.text).toBe("- Led the migration [n:3f2a]\n- Cut p95 40%\n");
    expect(q.fast.done).toBe(true);
    expect(q.fast.refs[0].label).toBe("Payments");
    expect(q.deep.text).toBe("Situation: ...");
    expect(q.deepRequested).toBe(true); // behavioral: the deep pass starts on its own
  });

  it("marks a cancelled speculative question and keeps the older answer", () => {
    const s = run([
      live,
      detected(QID, "other"),
      server({ type: "answer.delta", question_id: QID, pass: "fast", text: "- point" }),
      detected(QID2, "technical", true),
      server({ type: "question.cancelled", question_id: QID2 }),
      server({ type: "answer.error", question_id: QID, pass: "deep", code: "cancelled" }),
    ]);
    expect(s.questions.map((q) => q.cancelled)).toEqual([false, true]);
    expect(s.questions[0].fast.text).toBe("- point");
    expect(s.questions[0].deepRequested).toBe(false); // type other: deep only on expand
    expect(s.questions[0].deep.error).toBe("cancelled");
  });

  it("shows the worst component status, and reconnecting first", () => {
    let s = run([live, server({ type: "status", component: "llm", state: "degraded", message: "Slow answers" })]);
    expect(badge(s)).toEqual({ state: "degraded", text: "Slow answers" });
    s = run([server({ type: "status", component: "stt", state: "reconnecting", message: "Reconnecting transcription" })], s);
    expect(badge(s).text).toBe("Reconnecting transcription");
    s = run([{ kind: "connection", state: { state: "reconnecting", attempt: 1, in_ms: 500, close_code: null } }], s);
    expect(badge(s)).toEqual({ state: "reconnecting", text: "Reconnecting" });
    s = run([{ kind: "stopped", reason: "replaced" }], s);
    expect(s.running).toBe(false);
  });

  it("replaces partial transcripts by segment", () => {
    const s = run([
      server({ type: "transcript.partial", channel: "them", seg_id: 0, text: "Tell me" }),
      server({ type: "transcript.partial", channel: "them", seg_id: 0, text: "Tell me about" }),
      server({ type: "transcript.final", channel: "them", seg_id: 0, text: "Tell me about a time.", start_ms: 0, end_ms: 900 }),
    ]);
    expect(s.transcript).toEqual([{ key: "them:0", channel: "them", text: "Tell me about a time.", final: true, startMs: 0, endMs: 900 }]);
  });

  it("uses acknowledged live languages, keeps prior text, and recovers the current language on resume", () => {
    const ready = { type: "session.ready", session_id: QID, limits: { max_minutes: 60 }, spoken_language: "en", language_switching: true };
    let s = run([live, server(ready), server({ type: "transcript.final", channel: "them", seg_id: 1, text: "Tell me about a time.", start_ms: 0, end_ms: 900 }), detected(QID)]);
    expect(s.spokenLanguage).toBe("en"); expect(s.languageSwitching).toBe(true);
    s = run([server({ type: "session.language", spoken_language: "ar", request_id: QID2 })], s);
    expect(s.spokenLanguage).toBe("ar");
    expect(s.languageResult).toMatchObject({ requestId: QID2, error: null });
    expect(s.transcript[0].text).toBe("Tell me about a time."); expect(s.questions).toHaveLength(1);
    s = run([server({ type: "session.language", spoken_language: "ar", request_id: QID, error: "Recognition could not connect." })], s);
    expect(s.spokenLanguage).toBe("ar"); expect(s.languageResult?.error).toBe("Recognition could not connect.");
    s = run([server({ ...ready, spoken_language: "vi" })], s);
    expect(s.spokenLanguage).toBe("vi"); expect(s.languageResult).toBeNull();
    expect(reduce(s, { type: "event", event: { kind: "context_reset" } }).spokenLanguage).toBeNull();
  });

  it("does not offer live switching when an older gateway omits the capability", () => {
    const s = run([live, server({ type: "session.ready", session_id: QID, limits: { max_minutes: 60 }, spoken_language: "en" })]);
    expect(s.languageSwitching).toBe(false);
  });
});

describe("session id", () => {
  it("is kept after the session stops, for the post-call screen", () => {
    const s = run([live, { kind: "connection", state: { state: "stopped", reason: "ended" } }, { kind: "stopped", reason: "ended" }]);
    expect(s.running).toBe(false);
    expect(s.sessionId).toBe(QID);
  });
});


describe("answer stream identity and ordering", () => {
  const chunk = (sequence: number, text: string, id = QID) => server({ type: "answer.delta", question_id: id, pass: "fast", sequence, text });
  const finish = (deltaCount: number) => server({ type: "answer.done", question_id: QID, pass: "fast", delta_count: deltaCount, memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } });

  it("orders chunk gaps, ignores duplicates, and does not mix consecutive questions", () => {
    const s = run([detected(QID), detected(QID2), chunk(1, "world."), chunk(1, "duplicate"), chunk(0, "Hello "), chunk(0, "Second answer.", QID2), finish(2), chunk(2, "late"), chunk(0, "unknown", "missing")]);
    expect(s.questions[0].fast).toMatchObject({ text: "Hello world.", done: true, error: null });
    expect(s.questions[1].fast.text).toBe("Second answer.");
    expect(s.questions).toHaveLength(2);
  });

  it("reports a missing chunk at completion and repairs only the expected gap", () => {
    let s = run([detected(QID), chunk(1, "sentence."), finish(2)]);
    expect(s.questions[0].fast).toMatchObject({ text: "", done: true, error: "incomplete_stream" });
    s = run([chunk(2, "outside final range"), chunk(0, "First ")], s);
    expect(s.questions[0].fast).toMatchObject({ text: "First sentence.", done: true, error: null });
  });

  it("resumes a compacted range at the next live sequence", () => {
    const s = run([detected(QID), server({ type: "answer.delta", question_id: QID, pass: "fast", sequence: 0, sequence_end: 3, text: "Three chunks. " }), chunk(3, "Another."), finish(4)]);
    expect(s.questions[0].fast).toMatchObject({ text: "Three chunks. Another.", done: true, error: null });
  });

  it("never revives a cancelled question or a failed pass with late events", () => {
    const s = run([detected(QID, "technical", true), chunk(0, "Old"), server({ type: "question.cancelled", question_id: QID }), detected(QID, "technical", false), chunk(1, "stale"), detected(QID2), server({ type: "answer.error", question_id: QID2, pass: "fast", code: "timeout" }), chunk(0, "late", QID2)]);
    expect(s.questions[0]).toMatchObject({ cancelled: true, fast: { text: "Old" } });
    expect(s.questions[1].fast).toMatchObject({ text: "", done: true, error: "timeout" });
  });

  it("bounds an unrecoverable chunk gap without an endless spinner", () => {
    const s = run([detected(QID), chunk(129, "far ahead")]);
    expect(s.questions[0].fast).toMatchObject({ text: "", done: true, error: "stream_order" });
  });

  it("does not replace a final transcript with an old partial", () => {
    const s = run([server({ type: "transcript.final", channel: "them", seg_id: 0, text: "Final.", start_ms: 0, end_ms: 500 }), server({ type: "transcript.partial", channel: "them", seg_id: 0, text: "Earlier" })]);
    expect(s.transcript[0]).toMatchObject({ text: "Final.", final: true });
  });

  it("does not predict a second answer for a single adaptive stream", () => {
    const s = run([server({ type: "question.detected", question_id: QID, text: "Design a notification system.", question_type: "system_design", confidence: 0.9, speculative: false, answer_strategy: "adaptive" })]);
    expect(s.questions[0]).toMatchObject({ answerStrategy: "adaptive", deepRequested: false });
  });
});


it("keeps a cancellation tombstone when detection arrives late", () => {
  const s = run([server({ type: "question.cancelled", question_id: QID }), detected(QID), server({ type: "answer.delta", question_id: QID, pass: "fast", sequence: 0, text: "Stale" })]);
  expect(s.questions).toEqual([]);
});

it("does not repair an incomplete answer after the pass was cancelled", () => {
  const s = run([
    detected(QID),
    server({ type: "answer.done", question_id: QID, pass: "fast", delta_count: 1, memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } }),
    server({ type: "answer.error", question_id: QID, pass: "fast", code: "cancelled" }),
    server({ type: "answer.delta", question_id: QID, pass: "fast", sequence: 0, text: "Late repair" }),
  ]);
  expect(s.questions[0].fast).toMatchObject({ text: "", done: true, error: "cancelled" });
});


it("does not schedule another render for duplicate or unknown answer chunks", () => {
  const event = server({ type: "answer.delta", question_id: QID, pass: "fast", sequence: 0, text: "One sentence." });
  const s = run([detected(QID), event]);
  expect(run([event], s)).toBe(s);
  expect(run([server({ type: "answer.delta", question_id: "unknown", pass: "fast", sequence: 0, text: "Old" })], s)).toBe(s);
});
