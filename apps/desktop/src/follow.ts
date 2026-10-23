// The overlay's scrolling and copying, kept apart from React so they are tested on their own.

/// What scrolling needs of the conversation's element.
export interface ScrollBox {
  scrollTop: number;
  readonly scrollHeight: number;
  readonly clientHeight: number;
}

/// Within this many pixels of the end counts as "at the bottom".
export const NEAR_BOTTOM_PX = 32;

export function atBottom(b: ScrollBox, slack = NEAR_BOTTOM_PX): boolean {
  return b.scrollHeight - b.scrollTop - b.clientHeight <= slack;
}

/// Auto-scroll while the reader is at the bottom; once they scroll up, stay where they are
/// and offer "jump to latest" until they come back down.
export class Follower {
  following = true;
  /// Content that arrived while not following.
  unseen = 0;

  /// The user scrolled (or the element did, after our own scroll).
  onScroll(b: ScrollBox): void {
    this.following = atBottom(b);
    if (this.following) this.unseen = 0;
  }

  /// New or longer content was rendered. Returns true if it scrolled to it.
  onContent(b: ScrollBox): boolean {
    if (this.following) {
      b.scrollTop = b.scrollHeight;
      return true;
    }
    this.unseen += 1;
    return false;
  }

  jump(b: ScrollBox): void {
    b.scrollTop = b.scrollHeight;
    this.following = true;
    this.unseen = 0;
  }

  get showJump(): boolean {
    return !this.following;
  }
}

const CITATION = /[ \t]*\[n:[0-9a-z]+\]/gi;
const BULLET = /^\s*[-*•]\s+/;

/// An answer as plain text for the clipboard: citation ids like [n:3f2a] out, bullets as "- ",
/// no trailing spaces or empty lines.
export function plainText(answer: string): string {
  return answer
    .replace(CITATION, "")
    .split("\n")
    .map((l) => l.replace(BULLET, "- ").replace(/\s+$/, ""))
    .filter((l) => l.trim())
    .join("\n");
}

/// Copy to the clipboard; false if it could not.
export async function copyText(text: string, write: (t: string) => Promise<void> = (t) => navigator.clipboard.writeText(t)): Promise<boolean> {
  try {
    await write(text);
    return true;
  } catch {
    return false;
  }
}

/// How long the "Copied" confirmation stays.
export const COPIED_MS = 1500;

/// A copy button's state: `copy` writes the plain text and shows "Copied" for COPIED_MS.
export function copier(
  onChange: (copied: boolean) => void,
  write?: (t: string) => Promise<void>,
  timers: { set: typeof setTimeout; clear: typeof clearTimeout } = { set: setTimeout, clear: clearTimeout },
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return {
    async copy(answer: string): Promise<boolean> {
      const ok = await copyText(plainText(answer), write);
      if (!ok) return false;
      if (timer !== undefined) timers.clear(timer);
      onChange(true);
      timer = timers.set(() => {
        timer = undefined;
        onChange(false);
      }, COPIED_MS);
      return true;
    },
    dispose() {
      if (timer !== undefined) timers.clear(timer);
    },
  };
}
