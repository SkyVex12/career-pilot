import test from "node:test";
import assert from "node:assert/strict";
import {
  employerBody,
  parseDocument,
  parseEmployers,
  parseLogs,
  validDay,
  localDay,
} from "../src/workspaceData.ts";

test("workspace parsing accepts a real work log's optional attachment status and extracted facts", () => {
  const parsed = parseLogs({
    logs: [
      {
        id: "log",
        day: "2026-11-23",
        text: "Decision",
        status: "ready",
        attachments: [
          {
            id: "f",
            name: "architecture.pdf",
            content_type: "application/pdf",
            size_bytes: 500,
          },
        ],
        items: [],
      },
    ],
    next_before: "2026-10-24",
  });
  assert.equal(parsed.logs[0].attachments[0].status, "saved");
  assert.equal(parsed.next, "2026-10-24");
  assert.deepEqual(parseLogs({ logs: [], next_before: null }), {
    logs: [],
    next: null,
  });
});
test("malformed data is rejected before rendering raw objects or replacing current context", () => {
  assert.throws(() =>
    parseEmployers({
      employers: [{ id: "a", name: {}, title: "", team: "", status: "active" }],
    }),
  );
  assert.throws(() =>
    parseDocument({
      id: "f",
      name: "file",
      status: "indexed",
      headings: {},
      facts: [],
    }),
  );
  assert.throws(() => parseLogs({ logs: [], next_before: {} }));
  assert.throws(() => parseEmployers({ employers: null }));
});
test("company forms enforce real date and name constraints before sending a mutation", () => {
  const fields = {
    name: " Acme ",
    title: " Engineer ",
    team: " Core ",
    started: "2024-02-29",
    ended: "",
  };
  assert.deepEqual(employerBody(fields), {
    name: "Acme",
    title: "Engineer",
    team: "Core",
    started_on: "2024-02-29",
    ended_on: null,
  });
  for (const change of [
    { name: " " },
    { name: "a".repeat(201) },
    { ended: "2024-02-28" },
    { started: "2025-02-29" },
    { started: "0000-01-01" },
  ])
    assert.throws(() => employerBody({ ...fields, ...change }));
});
test("day input rejects rollover dates and local defaults retain the local calendar date", () => {
  for (const day of [
    "2026-02-30",
    "2026-1-01",
    "2026-13-01",
    "2026-01-01T12:00",
    "0000-01-01",
  ])
    assert.equal(validDay(day), false);
  assert.equal(validDay("2024-02-29"), true);
  assert.equal(localDay(new Date(2026, 10, 23, 0, 1)), "2026-11-23");
});
