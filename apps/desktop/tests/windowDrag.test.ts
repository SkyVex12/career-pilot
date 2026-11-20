// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest";
import { installWindowDragging, startWindowResize } from "../src/windowDrag";
import { ipc } from "./ipc";

let dispose: (() => void) | undefined;
afterEach(() => {
  dispose?.();
  dispose = undefined;
  document.body.replaceChildren();
});

function frame(html: string) {
  document.body.innerHTML = html;
  dispose = installWindowDragging();
  return document.body.firstElementChild!;
}

function press(element: Element, button = 0) {
  const event = new MouseEvent("pointerdown", { button, bubbles: true, cancelable: true });
  element.dispatchEvent(event);
  return event;
}

const drags = () => ipc.calls.filter(call => call.cmd === "start_window_drag");

it("starts native dragging only for the primary button on marked blank space", () => {
  const region = frame('<div data-native-drag-region></div>');
  expect(press(region, 1).defaultPrevented).toBe(false);
  expect(press(region, 2).defaultPrevented).toBe(false);
  expect(drags()).toHaveLength(0);
  expect(press(region).defaultPrevented).toBe(true);
  expect(drags()).toHaveLength(1);
});

it("allows nested title text inside an explicitly marked header", () => {
  const header = frame('<header data-native-drag-region="header"><h1><span>Applications</span></h1></header>');
  expect(press(header.querySelector("span")!).defaultPrevented).toBe(true);
  expect(drags()).toHaveLength(1);
});

it("preserves body text selection even inside a blank-space drag region", () => {
  const body = frame('<div data-native-drag-region><p><span>Select this transcript</span></p></div>');
  expect(press(body.querySelector("span")!).defaultPrevented).toBe(false);
  expect(drags()).toHaveLength(0);
});

it("never drags from native controls, accessible controls, or editable content in a header", () => {
  const header = frame(`<header data-native-drag-region="header">
    <button><span>Start call</span></button><a href="#"><span>Link</span></a>
    <input><select><option>Profile</option></select><textarea></textarea>
    <details><summary><span>Menu</span></summary></details>
    <label><span>Input label</span><input></label>
    <div role="combobox"><span>Profile</span></div><div role="switch"><span>Setting</span></div>
    <div contenteditable><span>Edit me</span></div>
    <button data-native-drag-region>Explicitly marked control</button>
  </header>`);
  for (const target of header.querySelectorAll("span, input, select, textarea, button[data-native-drag-region]")) {
    expect(press(target).defaultPrevented).toBe(false);
  }
  expect(drags()).toHaveLength(0);
});

it("removes the listener before a replacement install so native dragging runs once", () => {
  const header = frame('<header data-native-drag-region="header"><span>Title</span></header>');
  dispose!();
  expect(press(header).defaultPrevented).toBe(false);
  expect(drags()).toHaveLength(0);
  dispose = installWindowDragging();
  press(header.querySelector("span")!);
  expect(drags()).toHaveLength(1);
  dispose();
  dispose = undefined;
  press(header);
  expect(drags()).toHaveLength(1);
});


it("does not turn an already-handled resize gesture into a window move", () => {
  const header = frame('<header data-native-drag-region="header"><span>Resize handle</span></header>');
  const grip = header.querySelector("span")!;
  grip.addEventListener("pointerdown", event => event.preventDefault());
  expect(press(grip).defaultPrevented).toBe(true);
  expect(drags()).toHaveLength(0);
});

it.each(["North", "NorthEast", "East", "SouthEast", "South", "SouthWest", "West", "NorthWest"] as const)("routes %s resizing through the safe native command", async direction => {
  await startWindowResize(direction);
  expect(ipc.calls).toEqual([{ cmd: "start_window_resize", args: { direction } }]);
});
