// The overlay's scrolling (follow the newest message at the bottom, stay put when scrolled up,
// "jump to latest") and copying (plain text, citations out, a brief "Copied").
import { describe, expect, it, vi } from "vitest";
import { atBottom, COPIED_MS, copier, copyText, Follower, plainText, type ScrollBox } from "../src/follow";

/// A scrolling element: `grow` adds content below.
class Box implements ScrollBox {
  scrollTop = 0;
  scrollHeight: number;
  constructor(
    public clientHeight = 400,
    height = 400,
  ) {
    this.scrollHeight = height;
  }
  grow(px: number) {
    this.scrollHeight += px;
  }
  scrollTo(top: number) {
    this.scrollTop = Math.max(0, Math.min(top, this.scrollHeight - this.clientHeight));
  }
}

describe("following the conversation", () => {
  it("scrolls to new content while at the bottom", () => {
    const box = new Box();
    const f = new Follower();
    box.grow(300);
    expect(f.onContent(box)).toBe(true);
    expect(atBottom(box)).toBe(true);
    expect(f.showJump).toBe(false);
  });

  it("stays put after scrolling up, and offers to jump", () => {
    const box = new Box(400, 2000);
    const f = new Follower();
    f.onContent(box);
    box.scrollTo(500); // the reader goes back to an earlier answer
    f.onScroll(box);
    expect(f.showJump).toBe(true);
    box.grow(250);
    expect(f.onContent(box)).toBe(false);
    expect(box.scrollTop).toBe(500); // not moved
    expect(f.unseen).toBe(1);
    f.jump(box);
    expect(atBottom(box)).toBe(true);
    expect(f.showJump).toBe(false);
    expect(f.unseen).toBe(0);
    box.grow(100);
    expect(f.onContent(box)).toBe(true); // following again
  });

  it("scrolling back down to the bottom follows again without the button", () => {
    const box = new Box(400, 2000);
    const f = new Follower();
    box.scrollTo(100);
    f.onScroll(box);
    expect(f.showJump).toBe(true);
    box.scrollTo(box.scrollHeight - box.clientHeight - 10); // within the slack
    f.onScroll(box);
    expect(f.showJump).toBe(false);
  });
});

describe("copying an answer", () => {
  it("copies plain text without citation ids", () => {
    const answer = "- Led the payments migration [n:3f2a]\n• Cut p95 latency 40% [N:7C1E] [n:aa01]  \n\n* I'd shard by account\n";
    expect(plainText(answer)).toBe("- Led the payments migration\n- Cut p95 latency 40%\n- I'd shard by account");
    expect(plainText("Situation: the ledger was slow [n:1].\nResult: 40% faster")).toBe("Situation: the ledger was slow.\nResult: 40% faster");
  });

  it("shows Copied briefly, and nothing when the clipboard refuses", async () => {
    vi.useFakeTimers();
    try {
      const written: string[] = [];
      const states: boolean[] = [];
      const c = copier((on) => states.push(on), async (t) => void written.push(t));
      expect(await c.copy("- One [n:3f2a]\n- Two\n")).toBe(true);
      expect(written).toEqual(["- One\n- Two"]);
      expect(states).toEqual([true]);
      vi.advanceTimersByTime(COPIED_MS - 1);
      expect(states).toEqual([true]);
      vi.advanceTimersByTime(1);
      expect(states).toEqual([true, false]);

      const refused = copier((on) => states.push(on), async () => {
        throw new Error("denied");
      });
      expect(await refused.copy("x")).toBe(false);
      expect(states).toEqual([true, false]);
      expect(await copyText("x", async () => undefined)).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
