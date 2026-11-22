import test from "node:test";
import assert from "node:assert/strict";
import {
  parseCalls,
  parseTranscript,
  parsePreparation,
  parseSources,
  meetingContext,
  meetingKey,
} from "../src/historyData.ts";
const item = {
  meeting: {
    account_id: "account",
    calendar_id: "calendar",
    instance_id: "event",
    title: "Interview",
    start: "2026-11-20T10:00:00Z",
    end: "2026-11-20T11:00:00Z",
  },
  candidates: [],
  selected_application_id: "app",
  linked_session_id: "call",
  requires_confirmation: false,
  overlaps: false,
  unfinished_actions: ["Send notes"],
  previous: [{ notes: "Prior call", unfinished_actions: [] }],
};
const response = {
  items: [item],
  connections: [
    { connection_id: "account", provider: "google", status: "active" },
  ],
  applications: [
    { application_id: "app", company: "Fictional", job_title: "Engineer" },
  ],
  saved_sessions: [
    { id: "call", title: "Prior call", started_at: "2026-11-19T10:00:00Z" },
  ],
};
test("preparation parses the authorized correction lists and retains explicit cleared actions", () => {
  const data = parsePreparation(response);
  assert.equal(data.applications[0].application_id, "app");
  assert.equal(data.saved_sessions[0].id, "call");
  assert.deepEqual(meetingContext(data.items[0], "app", "call", ""), {
    calendar_id: "calendar",
    instance_id: "event",
    application_id: "app",
    session_id: "call",
    unfinished_actions: [],
  });
  assert.deepEqual(
    meetingContext(data.items[0], "", "", " one \none\n two ")
      .unfinished_actions,
    ["one", "two"],
  );
});
test("malformed lists, source text, and dates fail instead of appearing empty or crashing rendering", () => {
  assert.throws(() => parseCalls({ sessions: null }));
  assert.throws(() =>
    parseCalls({
      sessions: [
        {
          id: "a",
          title: "a",
          mode: "interview",
          status: "ready",
          started_at: "invalid",
        },
      ],
    }),
  );
  assert.throws(() =>
    parsePreparation({ ...response, applications: undefined }),
  );
  assert.throws(() =>
    parsePreparation({
      ...response,
      items: [{ ...item, unfinished_actions: {} }],
    }),
  );
  assert.throws(() =>
    parseSources({ sources: [{ label: "source", excerpt: { nested: true } }] }),
  );
  assert.throws(() =>
    parseTranscript({
      stored: true,
      segments: [{ seg_id: 1, channel: "them", start_ms: -1, text: "test" }],
    }),
  );
});
test("meeting identity includes account and calendar to prevent cross-calendar selection collisions", () => {
  const original = parsePreparation(response).items[0];
  assert.notEqual(
    meetingKey(original),
    meetingKey({
      ...original,
      meeting: { ...original.meeting, calendar_id: "another" },
    }),
  );
  assert.throws(() => meetingContext(original, "", "", "x".repeat(2001)));
});
test("saved transcript retains exact segment identity used by practice imports", () => {
  const segment = {
    seg_id: 12,
    channel: "them",
    start_ms: 12345,
    text: "Tell me about a launch",
  };
  assert.deepEqual(
    parseTranscript({ stored: true, segments: [segment] }).segments[0],
    segment,
  );
  assert.deepEqual(parseTranscript({ stored: false, segments: [] }), {
    stored: false,
    segments: [],
  });
});
