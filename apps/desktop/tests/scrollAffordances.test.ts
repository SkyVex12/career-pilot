import { describe, expect, it } from "vitest";
import { scrollEdges } from "../src/ui/scrollAffordances";

const viewport = { scrollTop: 0, scrollLeft: 0, scrollHeight: 600, scrollWidth: 300, clientHeight: 200, clientWidth: 300 };

describe("scroll edge cues", () => {
  it("shows only the directions with more content, including the middle and final position", () => {
    expect(scrollEdges(viewport)).toEqual(["bottom"]);
    expect(scrollEdges({ ...viewport, scrollTop: 150 })).toEqual(["top", "bottom"]);
    expect(scrollEdges({ ...viewport, scrollTop: 400 })).toEqual(["top"]);
    expect(scrollEdges({ ...viewport, scrollHeight: 200 })).toEqual([]);
  });
  it("handles horizontal overflow independently of vertical overflow", () => {
    expect(scrollEdges({ ...viewport, scrollHeight: 200, scrollWidth: 900 })).toEqual(["right"]);
    expect(scrollEdges({ ...viewport, scrollWidth: 900, scrollLeft: 100 })).toEqual(["bottom", "left", "right"]);
    expect(scrollEdges({ ...viewport, scrollHeight: 200, scrollWidth: 900, scrollLeft: 600 })).toEqual(["left"]);
  });
  it("maps RTL negative scroll offsets to the physical edges that contain hidden content", () => {
    const rtl = { ...viewport, scrollHeight: 200, scrollWidth: 900 };
    expect(scrollEdges(rtl, true)).toEqual(["left"]);
    expect(scrollEdges({ ...rtl, scrollLeft: -100 }, true)).toEqual(["left", "right"]);
    expect(scrollEdges({ ...rtl, scrollLeft: -600 }, true)).toEqual(["right"]);
  });
  it("ignores fractional end pixels and clamps elastic overscroll", () => {
    expect(scrollEdges({ ...viewport, scrollTop: 399.5 })).toEqual(["top"]);
    expect(scrollEdges({ ...viewport, scrollTop: -30 })).toEqual(["bottom"]);
    expect(scrollEdges({ ...viewport, scrollTop: 450 })).toEqual(["top"]);
    expect(scrollEdges({ ...viewport, scrollHeight: 200.5 })).toEqual([]);
  });
});
