import test from "node:test";
import assert from "node:assert/strict";
import {
  makeScope,
  parseConversation,
  parseMessage,
  withMessage,
  parseConversations,
  parseOptions,
  parseSourceDetail,
} from "../src/askData.ts";
test("scope rejects impossible dates and reversed windows, omits stale item on broad context", () => {
  assert.throws(() => makeScope("auto", "", "2026-02-30"));
  assert.throws(() => makeScope("call", ""));
  assert.throws(() => makeScope("all", "", "2026-12-31", "2026-01-01"));
  assert.deepEqual(makeScope("general", "stale", "2024-02-29"), {
    kind: "general",
    from_date: "2024-02-29",
  });
});
test("malformed server lists and nested private content fail before rendering", () => {
  assert.throws(() => parseConversations({ conversations: null }));
  assert.throws(() => parseOptions({ options: [{ id: "1", label: {} }] }));
  assert.throws(() =>
    parseSourceDetail({ label: "Source", content: { private: true } }),
  );
  assert.throws(() =>
    parseConversation(
      {
        id: "1",
        title: "Hi",
        scope: { kind: "auto" },
        messages: [
          { id: "m", question: "Hi", answer: [], status: "done", sources: [] },
        ],
      },
      true,
    ),
  );
});
test("completed conversation retains real citations and Unicode content; pending data cannot invent labels", () => {
  const c = parseConversation(
    {
      id: "c",
      title: "会話",
      scope: { kind: "document", id: "doc" },
      messages: [
        {
          id: "m",
          question: "経験は？",
          answer: "答え [S1]",
          status: "done",
          error: null,
          sources: [
            { kind: "resume", id: "r", label: "履歴書", citation: "[S1]" },
          ],
        },
      ],
    },
    true,
  );
  assert.equal(c.messages?.[0].sources[0].label, "履歴書");
  assert.equal(c.messages?.[0].answer, "答え [S1]");
  assert.throws(() =>
    parseConversation(
      {
        ...c,
        messages: [
          {
            ...c.messages![0],
            sources: [{ kind: "../private", id: "r", label: "bad" }],
          },
        ],
      },
      true,
    ),
  );
});


test("accepted message receipts remain visible without a subsequent detail read", () => {
  const conversation = parseConversation({ id: "c", title: "DNS", scope: { kind: "general" } });
  const receipt = parseMessage({ id: "m", question: "Explain DNS", answer: "", status: "pending", sources: [] });
  const pending = withMessage(conversation, receipt);
  assert.equal(pending.messages?.[0].status, "pending");
  assert.equal(pending.messages?.[0].question, "Explain DNS");
  const complete = withMessage(pending, { ...receipt, status: "done", answer: "Names map to addresses." });
  assert.equal(complete.messages?.length, 1);
  assert.equal(complete.messages?.[0].answer, "Names map to addresses.");
  assert.equal(pending.messages?.[0].status, "pending");
  assert.throws(() => parseMessage({ ...receipt, status: "unknown" }));
});
