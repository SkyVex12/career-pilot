import { describe, expect, it } from "vitest";
import { isSuggested } from "../src/AnswerText";

// 2026-10-28: interview answers may suggest details not in the user's data, each line ending
// in [~]; the overlay dims those lines and tags them "check".
describe("suggested details", () => {
  it("marks a line that ends in [~]", () => {
    expect(isSuggested("Batched upserts on the deal ID [~]")).toBe(true);
    expect(isSuggested("Proposed HubSpot workflows [n:5b60]")).toBe(false);
  });
});
