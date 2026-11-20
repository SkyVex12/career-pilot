import test from "node:test";
import assert from "node:assert/strict";
import {
  cleanupPracticeMedia,
  parsePracticeChoices,
  parsePracticeContext,
  parsePracticeHistory,
  parsePracticeSession,
  parsePracticeSpeech,
  parsePracticeTranscript,
  pollPractice,
  PracticeBusy,
  PracticeMediaQueue,
  PracticeScope,
  practiceOperation,
} from "../src/practiceState.ts";
const question = {
  id: "q1",
  text: "Tell me about a challenge.",
  kind: "experience",
  attempts: [],
};
const session = {
  id: "s1",
  status: "answering",
  setup: { topic: "Leadership", duration_minutes: 15 },
  questions: [question],
  current_question_id: "q1",
  spoken_language: "vi",
  voice_available: true,
  speech_available: false,
};

test("practice session parser preserves language capabilities and safely normalizes partial feedback", () => {
  const parsed = parsePracticeSession({
    ...session,
    questions: [
      {
        ...question,
        attempts: [
          {
            id: "a1",
            answer: "My answer",
            assistance: null,
            feedback: {
              dimensions: {
                clarity: { score: 3 },
                broken: {},
                out_of_range: 5,
              },
              strengths: ["Clear action", null],
              uncertainty: {},
            },
            comparison: {
              dimensions: {
                clarity: { previous: 2, current: 3 },
                malformed: null,
              },
              evidence: { current: null },
            },
          },
        ],
      },
    ],
  });
  assert.equal(parsed.voice_available, true);
  assert.equal(parsed.speech_available, false);
  assert.equal(parsed.spoken_language, "vi");
  const attempt = parsed.questions[0].attempts[0];
  assert.deepEqual(attempt.feedback?.dimensions, { clarity: 3 });
  assert.deepEqual(attempt.feedback?.strengths, ["Clear action"]);
  assert.deepEqual(attempt.feedback?.uncertainty, []);
  assert.deepEqual(attempt.comparison?.evidence.current, []);
  assert.deepEqual(attempt.comparison?.dimensions, {
    clarity: { previous: 2, current: 3 },
  });
});
test("malformed practice responses fail before reaching rendering", () => {
  for (const value of [
    null,
    {},
    { ...session, questions: {} },
    { ...session, status: "surprise" },
    { ...session, current_question_id: "absent" },
    { ...session, questions: [question, question] },
    { ...session, questions: [{ ...question, text: { html: "bad" } }] },
    { ...session, questions: Array(201).fill(question) },
  ])
    assert.throws(() => parsePracticeSession(value), /Update the server/);
  assert.throws(() => parsePracticeHistory({ sessions: null }));
  assert.throws(() =>
    parsePracticeChoices({ items: [{ id: "en", name: {} }] }, "languages"),
  );
  assert.throws(() => parsePracticeContext({ sources: {} }));
  assert.throws(() => parsePracticeTranscript({ text: {}, silence: false }));
  assert.throws(() =>
    parsePracticeTranscript({ text: "x".repeat(16001), silence: false }),
  );
  assert.throws(() => parsePracticeSpeech({ audio_base64: "<not audio>" }));
});
test("preparing sessions and empty approved source previews are valid", () => {
  const parsed = parsePracticeSession({
    ...session,
    status: "preparing",
    questions: [],
    current_question_id: null,
    voice_available: undefined,
  });
  assert.equal(parsed.voice_available, false);
  assert.deepEqual(parsePracticeContext({ sources: [] }), []);
  assert.deepEqual(
    parsePracticeChoices(
      { items: [{ id: "vi", name: "Vietnamese" }] },
      "languages",
    ),
    [{ value: "vi", label: "Vietnamese" }],
  );
});
test("late transcription cannot publish after a question change or navigation", async () => {
  const scope = new PracticeScope(),
    old = scope.begin();
  let complete!: (value: string) => void;
  const response = new Promise<string>((resolve) => {
    complete = resolve;
  });
  let draft = "new question draft";
  const apply = response.then((value) => {
    if (scope.current(old)) draft = value;
  });
  scope.invalidate();
  const latest = scope.begin();
  complete("old microphone transcript");
  await apply;
  assert.equal(old.signal.aborted, true);
  assert.equal(draft, "new question draft");
  assert.equal(scope.current(latest), true);
  scope.finish(old);
  assert.equal(scope.current(latest), true);
});
test("action retry reuses identity only for the same action and unchanged answer", () => {
  let count = 0;
  const id = () => `request-${++count}`;
  const first = practiceOperation(
    null,
    { sessionId: "s1", action: "answer", question_id: "q1", answer: "First" },
    id,
  );
  assert.equal(practiceOperation(first, { ...first.input }, id), first);
  for (const input of [
    { ...first.input, action: "pause" },
    { ...first.input, answer: "Corrected" },
    { ...first.input, question_id: "q2" },
    { ...first.input, sessionId: "s2" },
  ])
    assert.notEqual(
      practiceOperation(first, input, id).body.request_id,
      first.body.request_id,
    );
});
test("disposed player and recorder failures cannot prevent file deletion or audio release", async () => {
  const events: string[] = [];
  await assert.doesNotReject(() =>
    cleanupPracticeMedia({
      pause: () => {
        events.push("pause");
        throw new Error("disposed");
      },
      stop: async () => {
        events.push("stop");
        throw new Error("disposed recorder");
      },
      discard: async () => {
        events.push("delete");
        throw new Error("missing file");
      },
      release: async () => {
        events.push("release");
      },
    }),
  );
  assert.deepEqual(events, ["pause", "stop", "delete", "release"]);
});
test("late native preparation finishes before interruption cleanup and a newer setup", async () => {
  const queue = new PracticeMediaQueue();
  let complete!: () => void;
  const events: string[] = [];
  const setup = queue.run(async () => {
    events.push("prepare");
    await new Promise<void>((resolve) => {
      complete = resolve;
    });
    events.push("ready");
  });
  const cleanup = queue.run(async () => {
    events.push("stop");
  });
  const next = queue.run(async () => {
    events.push("next");
  });
  await Promise.resolve();
  assert.deepEqual(events, ["prepare"]);
  complete();
  await Promise.all([setup, cleanup, next]);
  assert.deepEqual(events, ["prepare", "ready", "stop", "next"]);
  await assert.rejects(() =>
    queue.run(async () => {
      throw new Error("disposed");
    }),
  );
  await queue.run(async () => {
    events.push("recovered");
  });
  assert.equal(events.at(-1), "recovered");
});
test("a stale async finally cannot release a new operation, and failed work can unlock", () => {
  const busy = new PracticeBusy(),
    old = busy.begin()!;
  assert.equal(busy.begin(), null);
  busy.invalidate();
  const current = busy.begin()!;
  assert.equal(busy.finish(old), false);
  assert.equal(busy.active, true);
  assert.equal(busy.finish(current), true);
  assert.equal(busy.active, false);
  assert.notEqual(busy.begin(), null);
});
test("practice polling never overlaps and cancellation stops late rescheduling", async () => {
  let calls = 0,
    release!: () => void,
    cleanup!: () => void;
  const started = new Promise<void>((resolve) => {
    const stop = pollPractice(async () => {
      calls++;
      resolve();
      await new Promise<void>((r) => {
        release = r;
      });
    }, 1);
    cleanup = stop;
  });
  await started;
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(calls, 1);
  cleanup();
  release();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls, 1);
});
