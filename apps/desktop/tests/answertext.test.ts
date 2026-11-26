import { describe, expect, it } from "vitest";
import { answerLines, readableLine } from "../src/AnswerText";

// 2026-11-17: citation ids ([n:w179]) showed on the overlay as stray letters; they are left out
// of what is read, and a half-written one is held back while the answer streams.
describe("citation ids", () => {
  it("are left out of the line, with the space before them", () => {
    expect(readableLine("AP2-10179 and AP2-10180 are Ready for Release [n:w179] [n:w21f]")).toBe("AP2-10179 and AP2-10180 are Ready for Release");
    expect(readableLine("the ledger was slow [N:1A].")).toBe("the ledger was slow.");
    expect(readableLine("Form reports are on qa [n:wbf6] and merged")).toBe("Form reports are on qa and merged");
  });
  it("a half-written one at the end is held back only while streaming", () => {
    for (const tail of [" [", " [n", " [n:", " [n:w1"]) expect(readableLine(`Form logs first${tail}`, true)).toBe("Form logs first");
    expect(readableLine("Use arrays [", false)).toBe("Use arrays [");
    expect(readableLine("Then [n:w179] more", true)).toBe("Then more");
  });
});

describe("legacy suggestion notation", () => {
  it("leaves normal answer text without the old marker", () => {
    expect(readableLine("Batched upserts on the deal ID [~]")).toBe("Batched upserts on the deal ID");
    expect(readableLine("[~] I would compare both options.").trim()).toBe("I would compare both options.");
    expect(readableLine("Proposed HubSpot workflows [n:5b60] [~]")).toBe("Proposed HubSpot workflows");
  });

  it("holds only a reserved partial marker and omits empty notation rows", () => {
    expect(readableLine("I would compare both options. [~", true)).toBe("I would compare both options.");
    expect(readableLine("Use [array] syntax.", true)).toBe("Use [array] syntax.");
    expect(readableLine("Use arrays [", false)).toBe("Use arrays [");
    expect(answerLines("- [~]", true)).toEqual([]);
    expect(answerLines("[~", true)).toEqual([]);
    expect(answerLines("- First answer. [~]\n- Next answer.")).toEqual(["- First answer.", "- Next answer."]);
  });
});
