// The transcript as the split overlay shows it: the speech-to-text sends a line for every short
// stretch of speech, often a few words, so each side's consecutive lines are joined into one
// block until the other side speaks, a long pause, or the block gets long. Also the overlay's
// view prefs (split or one column, text size), kept per computer in localStorage.
import type { SessionView } from "./session";

/// A pause longer than this starts a new block, even from the same speaker.
export const BLOCK_GAP_MS = 8_000;
/// A block this long starts a new one with the next line, so a monologue stays readable.
export const BLOCK_MAX_CHARS = 900;

export interface BlockPart {
  key: string;
  text: string;
  final: boolean;
}

export interface TranscriptBlock {
  /** The first line's key: stable while the block grows. */
  key: string;
  who: "them" | "me";
  parts: BlockPart[];
  /** Questions detected right after this block's lines, so the block can point at its card. */
  asked: string[];
}

export function blockText(b: TranscriptBlock): string {
  return b.parts.map((p) => p.text).join(" ");
}

/// `showMe: false` leaves the user's own blocks out (the live overlay); they still end the
/// other side's block, and a question detected after them points at the block before.
export function transcriptBlocks(s: SessionView, { showMe = true }: { showMe?: boolean } = {}): TranscriptBlock[] {
  const lines = new Map(s.transcript.map((t) => [t.key, t]));
  const live = new Set(s.questions.filter((q) => !q.cancelled).map((q) => q.id));
  const out: TranscriptBlock[] = [];
  let lastEnd: number | undefined;
  for (const ref of s.timeline) {
    if (ref.kind === "question") {
      if (live.has(ref.id) && out.length) out[out.length - 1].asked.push(ref.id);
      continue;
    }
    const t = lines.get(ref.key);
    if (!t || !t.text) continue;
    const who = t.channel === "me" ? "me" : "them";
    const prev = out[out.length - 1];
    const paused = lastEnd !== undefined && t.startMs !== undefined && t.startMs - lastEnd > BLOCK_GAP_MS;
    const part = { key: t.key, text: t.text, final: t.final };
    if (prev && prev.who === who && !paused && blockText(prev).length < BLOCK_MAX_CHARS) {
      prev.parts.push(part);
    } else {
      out.push({ key: t.key, who, parts: [part], asked: [] });
    }
    if (t.endMs !== undefined) lastEnd = t.endMs;
  }
  if (showMe) return out;
  const kept: TranscriptBlock[] = [];
  for (const b of out) {
    if (b.who === "them") kept.push(b);
    else kept[kept.length - 1]?.asked.push(...b.asked);
  }
  return kept;
}

/// The text sizes the overlay steps through; 1 is the size it always had.
export const TEXT_SCALES = [0.8, 0.9, 1, 1.1, 1.25, 1.4, 1.6, 1.8, 2];

/// The size `steps` away from `v` (snapped to the nearest step first), held to the ends.
export function stepScale(v: number, steps: number): number {
  let i = 0;
  for (let j = 1; j < TEXT_SCALES.length; j++) {
    if (Math.abs(TEXT_SCALES[j] - v) < Math.abs(TEXT_SCALES[i] - v)) i = j;
  }
  return TEXT_SCALES[Math.max(0, Math.min(TEXT_SCALES.length - 1, i + steps))];
}

export type OverlayLayout = "split" | "stacked";

export const TEXT_SCALE_KEY = "overlay.textScale";
export const LAYOUT_KEY = "overlay.layout";

export function loadTextScale(store: Pick<Storage, "getItem"> | null = safeStorage()): number {
  const v = Number(read(store, TEXT_SCALE_KEY));
  return Number.isFinite(v) && v > 0 ? stepScale(v, 0) : 1;
}

/// Split by default; "stacked" is the one-column view the overlay had before.
export function loadLayout(store: Pick<Storage, "getItem"> | null = safeStorage()): OverlayLayout {
  return read(store, LAYOUT_KEY) === "stacked" ? "stacked" : "split";
}

export function savePref(key: string, value: string, store: Pick<Storage, "setItem"> | null = safeStorage()): void {
  try {
    store?.setItem(key, value);
  } catch {
    // A per-computer convenience only.
  }
}

function read(store: Pick<Storage, "getItem"> | null, key: string): string | null {
  try {
    return store?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function safeStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}
