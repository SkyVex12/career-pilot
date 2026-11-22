// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { animateCompactMorph } from "../src/compactMotion";

type Playing = { element: HTMLElement; frames: Keyframe[]; options: KeyframeAnimationOptions; animation: Animation; finish: () => void };
let playing: Playing[];
let animateDescriptor: PropertyDescriptor | undefined;
beforeEach(() => {
  playing = [];
  animateDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
  vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: function (this: HTMLElement, frames: Keyframe[], options: KeyframeAnimationOptions) {
    let finish!: () => void;
    let reject!: (error: Error) => void;
    const finished = new Promise<void>((resolve, rejectPromise) => { finish = resolve; reject = rejectPromise; });
    const animation = { finished, cancel: vi.fn(() => reject(new Error("Animation cancelled"))), finish: vi.fn(() => finish()) } as unknown as Animation;
    playing.push({ element: this, frames, options, animation, finish });
    return animation;
  } });
});
afterEach(() => {
  if (animateDescriptor) Object.defineProperty(HTMLElement.prototype, "animate", animateDescriptor);
  else delete (HTMLElement.prototype as { animate?: unknown }).animate;
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

function surface() {
  const host = document.createElement("header");
  host.innerHTML = '<span class="session-compact-outline"></span><button class="session-compact-ruby">CoRuby</button><button>Start call</button>';
  document.body.append(host);
  const outline = host.children[0] as HTMLElement;
  const ruby = host.children[1] as HTMLElement;
  vi.spyOn(host, "getBoundingClientRect").mockReturnValue({ x: 100, y: 100, left: 100, top: 100, right: 380, bottom: 156, width: 280, height: 56, toJSON() {} });
  vi.spyOn(ruby, "getBoundingClientRect").mockReturnValue({ x: 148, y: 112, left: 148, top: 112, right: 180, bottom: 144, width: 32, height: 32, toJSON() {} });
  return { host, outline, ruby };
}

it("skips the morph when reduced motion is preferred", async () => {
  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: true } as MediaQueryList);
  const { host } = surface();
  await expect(animateCompactMorph(host, true).finished).resolves.toBe(true);
  expect(playing).toHaveLength(0);
});

it.each([true, false])("synchronizes the existing outline and ruby when collapse is %s", async collapse => {
  const { host, outline, ruby } = surface();
  const children = [...host.children];
  const clone = vi.spyOn(Node.prototype, "cloneNode");
  const morph = animateCompactMorph(host, collapse);
  const frameMotion = playing.find(item => item.element === outline)!;
  const rubyMotion = playing.find(item => item.element === ruby)!;
  const contracted = collapse ? 1 : 0;
  const expanded = collapse ? 0 : 1;
  expect(frameMotion.frames[contracted]).toMatchObject({ left: "108px", top: "6px", width: "64px", height: "44px", borderRadius: "8px" });
  expect(frameMotion.frames[expanded]).toMatchObject({ left: "0px", top: "0px", width: "280px", height: "56px" });
  expect(rubyMotion.frames[contracted]).toEqual({ transform: "translate(76px, 0px)" });
  expect(rubyMotion.frames[expanded]).toEqual({ transform: "translate(0px, 0px)" });
  expect(Number(frameMotion.options.duration)).toBeGreaterThan(0);
  expect(playing.every(item => item.options.duration === frameMotion.options.duration && item.options.easing === frameMotion.options.easing)).toBe(true);
  expect([...host.children]).toEqual(children);
  expect(clone).not.toHaveBeenCalled();
  playing.forEach(item => item.finish());
  await expect(morph.finished).resolves.toBe(true);
});

it.each([true, false])("preserves a disabled control's opacity when collapse is %s", async collapse => {
  const { host } = surface();
  const start = host.querySelectorAll("button")[1];
  const style = document.createElement("style");
  style.textContent = "button:disabled { opacity: .55; }";
  document.body.append(style);
  start.disabled = true;
  const morph = animateCompactMorph(host, collapse);
  const controlMotion = playing.find(item => item.element === start)!;
  expect(controlMotion.frames.map(frame => Number(frame.opacity))).toEqual(collapse ? [0.55, 0] : [0, 0.55]);
  playing.forEach(item => item.finish());
  await expect(morph.finished).resolves.toBe(true);
  morph.cancel();
  expect(start).toBeDisabled();
  expect(getComputedStyle(start).opacity).toBe("0.55");
});

it("cancels every active part and reports an interrupted transition", async () => {
  const { host } = surface();
  const morph = animateCompactMorph(host, true);
  morph.cancel();
  await expect(morph.finished).resolves.toBe(false);
  expect(playing.every(item => vi.mocked(item.animation.cancel).mock.calls.length === 1)).toBe(true);
});

it("finishes a pending morph when the document becomes hidden", async () => {
  const { host } = surface();
  const morph = animateCompactMorph(host, false);
  vi.spyOn(document, "hidden", "get").mockReturnValue(true);
  document.dispatchEvent(new Event("visibilitychange"));
  await expect(morph.finished).resolves.toBe(true);
  expect(playing.every(item => vi.mocked(item.animation.finish).mock.calls.length === 1)).toBe(true);
});
