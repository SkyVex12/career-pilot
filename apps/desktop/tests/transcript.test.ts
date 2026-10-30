// The split overlay's transcript: each side's short lines joined into blocks, and the view
// prefs (layout, text size) it remembers.
import { describe, expect, it } from "vitest";
import type { ServerMessage } from "@copilot/protocol";
import type { UiEvent } from "../src/api";
import { textKeyStep } from "../src/Overlay";
import { initial, reduce, type SessionView } from "../src/session";
import { BLOCK_GAP_MS, blockText, loadLayout, loadTextScale, savePref, stepScale, TEXT_SCALES, transcriptBlocks } from "../src/transcript";

let seq = 0;
const server = (m: Record<string, unknown>): UiEvent => ({ kind: "server", message: { seq: ++seq, ...m } as unknown as ServerMessage });
const run = (events: UiEvent[], from: SessionView = { ...initial, running: true }) => events.reduce((s, event) => reduce(s, { type: "event", event }), from);
const final = (channel: string, seg: number, text: string, start: number, end = start + 900, extra: Record<string, unknown> = {}) =>
  server({ type: "transcript.final", channel, seg_id: seg, text, start_ms: start, end_ms: end, ...extra });
const partial = (channel: string, seg: number, text: string) => server({ type: "transcript.partial", channel, seg_id: seg, text });
const detected = (id: string) => server({ type: "question.detected", question_id: id, text: `Question ${id}?`, question_type: "technical", confidence: 0.9, speculative: true });

const shape = (s: SessionView) => transcriptBlocks(s).map((b) => `${b.who}: ${blockText(b)}`);

describe("transcript blocks", () => {
  it("joins one side's short lines until the other side speaks", () => {
    const s = run([
      final("them", 0, "So,", 0, 400),
      final("them", 1, "tell me about", 900, 1800),
      final("them", 2, "the ledger migration.", 2200, 3500),
      final("me", 0, "Sure.", 4000, 4500),
      final("me", 1, "We moved it in three steps.", 4800, 7000),
      final("them", 3, "Why three?", 7600, 8400),
    ]);
    expect(shape(s)).toEqual(["them: So, tell me about the ledger migration.", "me: Sure. We moved it in three steps.", "them: Why three?"]);
    // The block keeps its first line's key, so it does not remount as it grows.
    expect(transcriptBlocks(s)[0].key).toBe("them:0");
  });

  it("starts a new block after a long pause from the same speaker", () => {
    const s = run([final("them", 0, "First thought.", 0, 1000), final("them", 1, "Much later.", 1000 + BLOCK_GAP_MS + 1, 1000 + BLOCK_GAP_MS + 900)]);
    expect(shape(s)).toEqual(["them: First thought.", "them: Much later."]);
  });

  it("starts a new block when one gets long", () => {
    const long = "word ".repeat(200).trim();
    const s = run([final("them", 0, long, 0, 1000), final("them", 1, "more", 1100, 1500)]);
    expect(transcriptBlocks(s)).toHaveLength(2);
  });

  it("shows a live partial at the end of its block, marked as not final", () => {
    const s = run([final("them", 0, "Tell me", 0, 500), partial("them", 1, "about your")]);
    const [b] = transcriptBlocks(s);
    expect(b.parts).toEqual([
      { key: "them:0", text: "Tell me", final: true },
      { key: "them:1", text: "about your", final: false },
    ]);
  });

  it("drops an echo line and joins what surrounds it", () => {
    const s = run([
      final("them", 0, "One.", 0, 500),
      partial("me", 0, "One"),
      final("me", 0, "", 0, 500, { echo: true }),
      final("them", 1, "Two.", 800, 1200),
    ]);
    expect(shape(s)).toEqual(["them: One. Two."]);
  });

  it("points a block at the questions detected after it, but not a cancelled one", () => {
    const s = run([
      final("them", 0, "How did you scale it?", 0, 1500),
      detected("q1"),
      final("me", 0, "By account.", 2000, 2600),
      detected("q2"),
      server({ type: "question.cancelled", question_id: "q2" }),
    ]);
    const blocks = transcriptBlocks(s);
    expect(blocks[0].asked).toEqual(["q1"]);
    expect(blocks[1].asked).toEqual([]);
  });
});

describe("view prefs", () => {
  it("steps the text size along the scale and holds at the ends", () => {
    expect(stepScale(1, 1)).toBe(1.1);
    expect(stepScale(1, -1)).toBe(0.9);
    expect(stepScale(1.07, 0)).toBe(1.1); // snapped first
    expect(stepScale(TEXT_SCALES[TEXT_SCALES.length - 1], 1)).toBe(TEXT_SCALES[TEXT_SCALES.length - 1]);
    expect(stepScale(TEXT_SCALES[0], -3)).toBe(TEXT_SCALES[0]);
  });

  it("reads Ctrl plus, minus and zero", () => {
    const k = (key: string, ctrlKey = true, altKey = false) => textKeyStep({ key, ctrlKey, metaKey: false, altKey });
    expect([k("="), k("+"), k("-"), k("0"), k("a"), k("=", false), k("=", true, true)]).toEqual([1, 1, -1, 0, null, null, null]);
  });

  it("remembers the layout and text size, with safe defaults", () => {
    const data = new Map<string, string>();
    const store = { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) };
    expect(loadLayout(store)).toBe("split");
    expect(loadTextScale(store)).toBe(1);
    savePref("overlay.layout", "stacked", store);
    savePref("overlay.textScale", "1.25", store);
    expect(loadLayout(store)).toBe("stacked");
    expect(loadTextScale(store)).toBe(1.25);
    data.set("overlay.textScale", "nonsense");
    expect(loadTextScale(store)).toBe(1);
    const broken = { getItem: () => { throw new Error("blocked"); } }; // prettier-ignore
    expect(loadLayout(broken)).toBe("split");
  });
});
