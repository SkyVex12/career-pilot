// A past call in History, rebuilt as the overlay showed it: lines and questions in the order
// they appeared, the answers whole, a deep answer never asked for left unstarted.
import { describe, expect, it } from "vitest";
import type { Replay } from "../src/api";
import { conversation, fromReplay } from "../src/session";

const replay: Replay = {
  session_id: "s1",
  stored: true,
  started_at: "2026-10-29T15:58:15Z",
  segments: [
    { channel: "them", seg_id: 0, start_ms: 0, end_ms: 1400, text: "Tell me about a hard project." },
    { channel: "me", seg_id: 0, start_ms: 3000, end_ms: 6000, text: "Sure, the payments migration." },
    { channel: "them", seg_id: 1, start_ms: 9000, end_ms: 9800, text: "Why Globex?" },
    { channel: "them", seg_id: 2, start_ms: 12000, end_ms: 12100, text: "" },
  ],
  questions: [
    {
      id: "q1", text: "Tell me about a hard project.", type: "behavioral", forced: false, offset_ms: 1600, feedback: "up",
      fast: { text: "- Payments migration", source: "model", refs: [{ node_id: "n1", label: "Payments" }], expanded: false },
      deep: { text: "In 2024 I led…", source: "model", refs: [], expanded: true },
    },
    { id: "q2", text: "Why Globex?", type: "motivation", forced: false, offset_ms: 10000, feedback: null,
      fast: { text: "- Mission", source: "prepared", refs: [], expanded: false }, deep: null },
  ],
};

describe("fromReplay", () => {
  it("orders lines and questions by when they appeared", () => {
    const items = conversation(fromReplay(replay));
    expect(items.map((i) => i.key)).toEqual(["them:0", "q1", "me:0", "them:1", "q2"]);
    expect(items.every((i) => i.kind === "question" || i.final)).toBe(true);
  });

  it("keeps the answers whole and a deep answer never asked for unstarted", () => {
    const s = fromReplay(replay);
    const [q1, q2] = s.questions;
    expect(q1.fast).toMatchObject({ text: "- Payments migration", done: true, refs: [{ label: "Payments" }] });
    expect(q1.deep).toMatchObject({ text: "In 2024 I led…", done: true });
    expect(q2.fast.source).toBe("prepared");
    expect(q2.deep).toMatchObject({ text: "", done: false });
    expect(q2.deepRequested).toBe(false);
    expect(s.running).toBe(false);
  });
});
