import { act, renderHook } from "@testing-library/react";
import { emit } from "@tauri-apps/api/event";
import { mockWindows } from "@tauri-apps/api/mocks";
import type { PointerEvent } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useWindowMode } from "../src/windowMode";
import * as motion from "../src/compactMotion";
import { ipc } from "./ipc";

const pointer = { pointerType: "mouse", buttons: 0 } as PointerEvent;
const tick = async (ms = 300) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const focus = async (value: boolean) => act(async () => { await emit(value ? "tauri://focus" : "tauri://blur"); });

beforeEach(() => {
  vi.useFakeTimers();
  mockWindows("main");
  ipc.handle("plugin:window|is_focused", () => true);
  ipc.handle("plugin:window|is_minimized", () => false);
  ipc.handle("plugin:window|inner_size", () => ({ width: 280, height: 56 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|cursor_position", () => ({ x: 1000, y: 1000 }));
  ipc.handle("set_compact_window_shape", () => true);
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

async function toolbar() {
  const hook = renderHook(() => useWindowMode(false, true));
  await act(async () => { await hook.result.current.change("toolbar"); });
  return hook;
}

it("keeps selected medium mode open after pointer exit, then collapses on native blur", async () => {
  const { result } = await toolbar();
  act(() => result.current.onPointerLeave(pointer));
  await tick(1000);
  expect(result.current.mode).toBe("toolbar");
  await focus(false);
  await tick();
  expect(result.current.mode).toBe("dot");
});

it("expands on hover without focus, collapses on leave, and stays open after selection", async () => {
  ipc.handle("plugin:window|is_focused", () => false);
  const { result } = await toolbar();
  await tick();
  expect(result.current.mode).toBe("dot");
  ipc.handle("plugin:window|cursor_position", () => ({ x: 140, y: 28 }));
  act(() => result.current.onPointerEnter(pointer));
  await tick();
  expect(result.current.mode).toBe("toolbar");
  ipc.handle("plugin:window|cursor_position", () => ({ x: 1000, y: 1000 }));
  act(() => result.current.onPointerLeave(pointer));
  await tick();
  expect(result.current.mode).toBe("dot");

  ipc.handle("plugin:window|cursor_position", () => ({ x: 140, y: 28 }));
  act(() => result.current.onPointerEnter(pointer));
  await tick();
  await focus(true);
  ipc.handle("plugin:window|cursor_position", () => ({ x: 1000, y: 1000 }));
  act(() => result.current.onPointerLeave(pointer));
  await tick(1000);
  expect(result.current.mode).toBe("toolbar");
  await focus(false);
  await tick();
  expect(result.current.mode).toBe("dot");
});

it("checks the cursor on blur even when the webview missed pointer leave", async () => {
  const { result } = await toolbar();
  act(() => result.current.onPointerEnter(pointer));
  await focus(false);
  await tick();
  expect(result.current.mode).toBe("dot");
});

it("does not let a late initial focus query overwrite a newer selection", async () => {
  let finish!: (value: boolean) => void;
  ipc.handle("plugin:window|is_focused", () => new Promise<boolean>(resolve => { finish = resolve; }));
  const { result } = await toolbar();
  await focus(true);
  await act(async () => { finish(false); });
  act(() => result.current.onPointerLeave(pointer));
  await tick();
  expect(result.current.mode).toBe("toolbar");
});

it("cancels collapse when focus arrives during a native cursor check", async () => {
  const { result } = await toolbar();
  let finish!: (point: { x: number; y: number }) => void;
  ipc.handle("plugin:window|cursor_position", () => new Promise(resolve => { finish = resolve; }));
  await focus(false);
  await tick();
  await focus(true);
  await act(async () => { finish({ x: 1000, y: 1000 }); });
  expect(result.current.mode).toBe("toolbar");
  expect(ipc.calls.some(call => call.cmd === "set_compact_window_shape" && call.args.shape === "dot")).toBe(false);
});

it("keeps medium mode when selected during the collapse animation", async () => {
  const { result } = await toolbar();
  let finish!: (value: boolean) => void;
  vi.spyOn(motion, "animateCompactMorph").mockReturnValue({
    finished: new Promise<boolean>(resolve => { finish = resolve; }), cancel: vi.fn(),
  });
  await focus(false);
  await tick();
  await focus(true);
  await act(async () => { finish(true); });
  expect(result.current.mode).toBe("toolbar");
  expect(result.current.error).toBeNull();
  expect(ipc.calls.some(call => call.cmd === "set_compact_window_shape" && call.args.shape === "dot")).toBe(false);
});

it("keeps full mode on blur and opens the small launcher when selected by the keyboard", async () => {
  const { result } = await toolbar();
  await focus(false);
  await tick();
  expect(result.current.mode).toBe("dot");
  await focus(true);
  await tick();
  expect(result.current.mode).toBe("toolbar");
  await act(async () => { await result.current.change("full"); });
  await focus(false);
  await tick();
  expect(result.current.mode).toBe("full");
});
