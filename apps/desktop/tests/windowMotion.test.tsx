import { expect, it, vi } from "vitest";
import { mockWindows } from "@tauri-apps/api/mocks";
import { LogicalSize } from "@tauri-apps/api/dpi";
import { clearCompactShape, isPointerInsideWindow, resizeCompactBounds, resizeWindowBounds, withWindowResize } from "../src/windowMotion";
import { ipc } from "./ipc";

it("serializes auto-fit and mode transitions instead of interleaving writes", async () => {
  const order: string[] = [];
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const first = withWindowResize(async () => { order.push("fit starts"); await gate; order.push("fit ends"); });
  const second = withWindowResize(async () => { order.push("transition starts"); });
  await Promise.resolve();
  expect(order).toEqual(["fit starts"]);
  release();
  await Promise.all([first, second]);
  expect(order).toEqual(["fit starts", "fit ends", "transition starts"]);
});

it("changes native bounds exactly once without cloning or animating the UI", async () => {
  mockWindows("main");
  const renderTarget = vi.fn();
  await resizeWindowBounds(new LogicalSize(280, 56), renderTarget);
  const sizes = ipc.calls.filter(c => c.cmd === "plugin:window|set_size");
  expect(sizes).toHaveLength(1);
  expect(JSON.parse(JSON.stringify(sizes[0].args.value))).toEqual({ Logical: { width: 280, height: 56 } });
  expect(renderTarget).toHaveBeenCalledOnce();
  expect(document.querySelector(".window-morph")).toBeNull();
});

it("does not commit the target view when the native operation fails", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|set_size", () => { throw new Error("Window unavailable"); });
  const renderTarget = vi.fn();
  await expect(resizeWindowBounds(new LogicalSize(48, 48), renderTarget)).rejects.toThrow();
  expect(renderTarget).not.toHaveBeenCalled();
  // A rejected operation must not poison subsequent queued operations.
  await expect(withWindowResize(async () => "recovered")).resolves.toBe("recovered");
});

it.each([
  [48, 48, "dot"],
  [280, 56, "toolbar"],
] as const)("keeps the native canvas unchanged when applying the %s×%s compact region", async (width, height, shape) => {
  mockWindows("main");
  ipc.handle("set_compact_window_shape", () => true);
  const renderTarget = vi.fn();
  await resizeCompactBounds(width, height, renderTarget);
  expect(ipc.calls).toEqual([{ cmd: "set_compact_window_shape", args: { shape } }]);
  expect(renderTarget).toHaveBeenCalledOnce();
});

it("uses centered native resizing only when shaped compact windows are unsupported", async () => {
  mockWindows("main");
  ipc.handle("set_compact_window_shape", () => false);
  const renderTarget = vi.fn();
  await resizeCompactBounds(48, 48, renderTarget);
  expect(ipc.calls).toEqual([
    { cmd: "set_compact_window_shape", args: { shape: "dot" } },
    { cmd: "resize_compact_window", args: { width: 48, height: 48 } },
  ]);
  expect(renderTarget).toHaveBeenCalledOnce();
});

it("keeps the existing view when the compact region operation fails", async () => {
  mockWindows("main");
  ipc.handle("set_compact_window_shape", () => { throw new Error("Region unavailable"); });
  const renderTarget = vi.fn();
  await expect(resizeCompactBounds(48, 48, renderTarget)).rejects.toThrow("Region unavailable");
  expect(renderTarget).not.toHaveBeenCalled();
  expect(ipc.calls.some(call => call.cmd === "resize_compact_window")).toBe(false);
});

it("clears compact clipping without changing native bounds", async () => {
  mockWindows("main");
  ipc.handle("set_compact_window_shape", () => true);
  await clearCompactShape();
  expect(ipc.calls).toEqual([{ cmd: "set_compact_window_shape", args: { shape: "full" } }]);
});

it.each([
  [{ x: -1240, y: -48 }, true],
  [{ x: -1450, y: -90 }, true],
  [{ x: -1030, y: -48 }, false],
  [{ x: -1240, y: -6 }, false],
  [{ x: -1451, y: -48 }, false],
])("checks physical cursor %j against a compact window on a scaled negative-coordinate monitor", async (pointer, inside) => {
  mockWindows("main");
  ipc.handle("plugin:window|cursor_position", () => pointer);
  ipc.handle("plugin:window|inner_position", () => ({ x: -1450, y: -90 }));
  // A 280×56 toolbar at 150% scale. No extra scale conversion is needed.
  ipc.handle("plugin:window|inner_size", () => ({ width: 420, height: 84 }));
  await expect(isPointerInsideWindow()).resolves.toBe(inside);
});

it.each([0, Number.NaN, Number.POSITIVE_INFINITY])("rejects unavailable native bounds instead of treating them as a pointer exit (%s)", async width => {
  mockWindows("main");
  ipc.handle("plugin:window|cursor_position", () => ({ x: 10, y: 10 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|inner_size", () => ({ width, height: 48 }));
  await expect(isPointerInsideWindow()).rejects.toThrow("Window pointer bounds are unavailable.");
});

it.each([1.25, 1.5])("keeps the dot's two-pixel native safety margin within its hover region at %s scale", async scale => {
  mockWindows("main");
  const position = { x: -1450, y: -90 };
  const size = { width: 280 * scale, height: 56 * scale };
  const center = { x: position.x + size.width / 2, y: position.y + size.height / 2 };
  ipc.handle("plugin:window|inner_position", () => position);
  ipc.handle("plugin:window|inner_size", () => size);
  ipc.handle("plugin:window|scale_factor", () => scale);
  for (const [offsetX, offsetY, inside] of [
    [0, 0, true],
    [23, 0, true],
    [0, -23, true],
    [25, 0, true],
    [27, 0, false],
    [20, 20, false],
    [-100, 0, false],
  ] as const) {
    ipc.handle("plugin:window|cursor_position", () => ({ x: center.x + offsetX * scale, y: center.y + offsetY * scale }));
    await expect(isPointerInsideWindow("dot")).resolves.toBe(inside);
  }
});

it.each([1.25, 1.5])("limits dot hover to a resized fallback window at %s scale", async scale => {
  mockWindows("main");
  const position = { x: -1450, y: -90 };
  const diameter = 48 * scale;
  ipc.handle("plugin:window|inner_position", () => position);
  ipc.handle("plugin:window|inner_size", () => ({ width: diameter, height: diameter }));
  ipc.handle("plugin:window|scale_factor", () => scale);
  for (const [offsetX, inside] of [[23, true], [25, false]] as const) {
    ipc.handle("plugin:window|cursor_position", () => ({ x: position.x + diameter / 2 + offsetX * scale, y: position.y + diameter / 2 }));
    await expect(isPointerInsideWindow("dot")).resolves.toBe(inside);
  }
});

it.each([0, Number.NaN, Number.POSITIVE_INFINITY])("does not infer dot hover from an invalid display scale (%s)", async scale => {
  mockWindows("main");
  ipc.handle("plugin:window|cursor_position", () => ({ x: 140, y: 28 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|inner_size", () => ({ width: 280, height: 56 }));
  ipc.handle("plugin:window|scale_factor", () => scale);
  await expect(isPointerInsideWindow("dot")).rejects.toThrow();
});
