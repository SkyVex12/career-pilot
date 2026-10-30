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
});

describe("session id", () => {
  it("is kept after the session stops, for the post-call screen", () => {
    const s = run([live, { kind: "connection", state: { state: "stopped", reason: "ended" } }, { kind: "stopped", reason: "ended" }]);
    expect(s.running).toBe(false);
    expect(s.sessionId).toBe(QID);
  });
});
