import { act, renderHook, waitFor } from "@testing-library/react";
import type { PointerEvent } from "react";
import { expect, it } from "vitest";
import { mockWindows } from "@tauri-apps/api/mocks";
import { useCardWindow } from "../src/cardWindow";
import { useWindowMode } from "../src/windowMode";
import { ipc } from "./ipc";

function mockResizable(initial: boolean) {
  let resizable = initial;
  ipc.handle("plugin:window|is_resizable", () => resizable);
  ipc.handle("plugin:window|set_resizable", args => { resizable = args.value as boolean; });
  return () => resizable;
}

it("page changes, streamed content, and viewport events never resize the native window", async () => {
  mockWindows("main");
  const root = document.createElement("div"); root.id = "root";
  const page = document.createElement("div"); page.className = "shell-content";
  root.append(page); document.body.append(root);
  const { unmount } = renderHook(useCardWindow);
  await act(async () => {
    page.hidden = true;
    page.textContent = "A long streamed response".repeat(100);
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new Event("copilot:window-layout"));
  });
  expect(ipc.calls.filter(c => /set_size|set_position|unmaximize/.test(c.cmd))).toEqual([]);
  unmount(); root.remove();
});

it("restores logical size after compact mode so DPI changes do not alter the intended card size", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 1296, height: 1040 }));
  ipc.handle("plugin:window|scale_factor", () => 2);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { await result.current.change("toolbar"); });
  await act(async () => { await result.current.change("full"); });
  const sizes = ipc.calls.filter(c => c.cmd === "plugin:window|set_size");
  expect(JSON.parse(JSON.stringify(sizes[sizes.length - 1].args.value))).toEqual({ Logical: { width: 648, height: 520 } });
  expect(ipc.calls.filter(c => c.cmd === "plugin:window|set_decorations")).toHaveLength(0);
});

it("changes compact bounds once in each direction without resetting native window constraints", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { await result.current.change("toolbar"); });
  const enteringCompact = ipc.calls.length;
  await act(async () => { expect(await result.current.change("dot")).toBe(true); });
  await act(async () => { expect(await result.current.change("toolbar")).toBe(true); });
  const compactCalls = ipc.calls.slice(enteringCompact);
  expect(compactCalls.filter(call => call.cmd === "resize_compact_window").map(call => call.args)).toEqual([
    { width: 64, height: 44 },
    { width: 280, height: 56 },
  ]);
  expect(compactCalls.filter(call => /set_size|set_min_size|decorat|is_maximized|unmaximize/.test(call.cmd))).toEqual([]);
});

it("protects the first compact frame before changing native bounds or decorations", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => true);
  ipc.handle("plugin:window|is_maximized", () => true);
  mockResizable(true);
  ipc.handle("set_compact_window_shape", () => true);
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { expect(await result.current.change("toolbar")).toBe(true); });
  const commands = ipc.calls.map(call => call.cmd);
  const prepared = commands.indexOf("set_compact_window_shape");
  expect(prepared).toBeGreaterThanOrEqual(0);
  for (const command of ["plugin:window|unmaximize", "plugin:window|set_resizable", "plugin:window|set_min_size", "plugin:window|set_decorations", "plugin:window|set_size"]) {
    expect(commands.indexOf(command)).toBeGreaterThan(prepared);
  }
  expect(ipc.calls.filter(call => call.cmd === "set_compact_window_shape").map(call => call.args.shape)).toEqual(["toolbar"]);
  expect(result.current.mode).toBe("toolbar");
});

it("cleans the frame guard after failed compact entry and saves fresh bounds on retry", async () => {
  mockWindows("main");
  let size = { width: 648, height: 520 };
  ipc.handle("plugin:window|inner_size", () => size);
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  const resizable = mockResizable(true);
  ipc.handle("set_compact_window_shape", () => true);
  ipc.handle("plugin:window|set_size", () => { throw new Error("Window unavailable"); });
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { expect(await result.current.change("toolbar")).toBe(false); });
  expect(result.current.mode).toBe("full");
  expect(result.current.busy).toBe(false);
  expect(result.current.error).toContain("Window unavailable");
  expect(resizable()).toBe(true);
  expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_resizable").map(call => call.args.value)).toEqual([false, true]);
  expect(ipc.calls.filter(call => call.cmd === "set_compact_window_shape").map(call => call.args.shape)).toEqual(["toolbar", "full"]);
  expect(document.documentElement).not.toHaveAttribute("data-window-transition");

  size = { width: 800, height: 600 };
  ipc.handle("plugin:window|set_size", () => undefined);
  await act(async () => { expect(await result.current.change("toolbar")).toBe(true); });
  await act(async () => { expect(await result.current.change("full")).toBe(true); });
  const sizes = ipc.calls.filter(call => call.cmd === "plugin:window|set_size");
  expect(JSON.parse(JSON.stringify(sizes[sizes.length - 1].args.value))).toEqual({ Logical: size });
});

it("retains compact protection and full bounds when direct dot entry fails after rendering the toolbar", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  const resizable = mockResizable(true);
  ipc.handle("set_compact_window_shape", args => {
    if (args.shape === "dot") throw new Error("Region unavailable");
    return true;
  });
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { expect(await result.current.change("dot")).toBe(false); });
  expect(result.current.mode).toBe("toolbar");
  expect(result.current.error).toContain("Region unavailable");
  expect(resizable()).toBe(false);
  expect(ipc.calls.filter(call => call.cmd === "set_compact_window_shape").map(call => call.args.shape)).toEqual(["toolbar", "dot"]);
  await act(async () => { expect(await result.current.change("full")).toBe(true); });
  const sizes = ipc.calls.filter(call => call.cmd === "plugin:window|set_size");
  expect(JSON.parse(JSON.stringify(sizes[sizes.length - 1].args.value))).toEqual({ Logical: { width: 648, height: 520 } });
  expect(result.current.mode).toBe("full");
  expect(resizable()).toBe(true);
});

it.each([true, false])("locks compact resizing and restores the original full-window option (%s)", async initiallyResizable => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  ipc.handle("set_compact_window_shape", () => true);
  const resizable = mockResizable(initiallyResizable);
  const { result } = renderHook(() => useWindowMode());
  for (const mode of ["toolbar", "dot", "toolbar"] as const) {
    await act(async () => { expect(await result.current.change(mode)).toBe(true); });
    expect(resizable()).toBe(false);
  }
  await act(async () => { expect(await result.current.change("full")).toBe(true); });
  expect(resizable()).toBe(initiallyResizable);
  expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_resizable").map(call => call.args.value)).toEqual(initiallyResizable ? [false, true] : []);
});

it.each([false, true])("restores full-window options if compact bounds fail after native preparation (overlay: %s)", async overlay => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 960, height: 720 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  let decorated = true;
  let maximized = true;
  ipc.handle("plugin:window|is_decorated", () => decorated);
  ipc.handle("plugin:window|set_decorations", args => { decorated = args.value as boolean; });
  ipc.handle("plugin:window|is_maximized", () => maximized);
  ipc.handle("plugin:window|unmaximize", () => { maximized = false; });
  ipc.handle("plugin:window|maximize", () => { maximized = true; });
  ipc.handle("set_compact_window_shape", () => true);
  ipc.handle("plugin:window|set_size", () => { throw new Error("Window unavailable"); });
  const resizable = mockResizable(true);
  const { result } = renderHook(() => useWindowMode(overlay));
  await act(async () => { expect(await result.current.change("toolbar")).toBe(false); });
  expect(result.current.mode).toBe("full");
  expect(resizable()).toBe(true);
  expect(decorated).toBe(true);
  expect(maximized).toBe(true);
  const constraints = ipc.calls.filter(call => call.cmd === "plugin:window|set_min_size");
  expect(JSON.parse(JSON.stringify(constraints[constraints.length - 1].args.value))).toEqual({ Logical: { width: overlay ? 320 : 400, height: 240 } });
  const commands = ipc.calls.map(call => call.cmd);
  const failedResize = commands.indexOf("plugin:window|set_size");
  expect(commands.lastIndexOf("plugin:window|set_resizable")).toBeGreaterThan(failedResize);
  expect(commands.lastIndexOf("plugin:window|set_decorations")).toBeGreaterThan(failedResize);
  expect(commands.lastIndexOf("plugin:window|maximize")).toBeGreaterThan(commands.lastIndexOf("set_compact_window_shape"));
});

it("changes only the native region across repeated compact transitions on Windows", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  mockResizable(true);
  ipc.handle("set_compact_window_shape", () => true);
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { await result.current.change("toolbar"); });
  const enteringCompact = ipc.calls.length;
  for (let cycle = 0; cycle < 3; cycle++) {
    await act(async () => { expect(await result.current.change("dot")).toBe(true); });
    await act(async () => { expect(await result.current.change("toolbar")).toBe(true); });
  }
  const compactCalls = ipc.calls.slice(enteringCompact);
  expect(compactCalls.filter(call => call.cmd === "set_compact_window_shape").map(call => call.args.shape)).toEqual([
    "dot", "toolbar", "dot", "toolbar", "dot", "toolbar",
  ]);
  expect(compactCalls.filter(call => /resize_compact_window|set_size|set_position|set_min_size|resizable|decorat|is_maximized|unmaximize/.test(call.cmd))).toEqual([]);
  expect(result.current.mode).toBe("toolbar");
});

it("removes the compact window region before restoring full-size window bounds", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  ipc.handle("set_compact_window_shape", () => true);
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { await result.current.change("toolbar"); });
  await act(async () => { await result.current.change("dot"); });
  const beforeRestore = ipc.calls.length;
  await act(async () => { expect(await result.current.change("full")).toBe(true); });
  const restoreCalls = ipc.calls.slice(beforeRestore);
  const clear = restoreCalls.findIndex(call => call.cmd === "set_compact_window_shape" && call.args.shape === "full");
  const resize = restoreCalls.findIndex(call => call.cmd === "plugin:window|set_size");
  expect(clear).toBeGreaterThanOrEqual(0);
  expect(resize).toBeGreaterThan(clear);
  expect(JSON.parse(JSON.stringify(restoreCalls[resize].args.value))).toEqual({ Logical: { width: 648, height: 520 } });
  expect(result.current.mode).toBe("full");
});

it("does not expand a dot when a stale hover event points into the transparent canvas", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 280, height: 56 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  ipc.handle("set_compact_window_shape", () => true);
  ipc.handle("plugin:window|cursor_position", () => ({ x: 60, y: 28 }));
  const { result } = renderHook(() => useWindowMode(false, true));
  await act(async () => { await result.current.change("toolbar"); });
  await act(async () => { await result.current.change("dot"); });
  const beforeHover = ipc.calls.length;
  act(() => result.current.onPointerEnter({ pointerType: "mouse", buttons: 0 } as PointerEvent));
  await act(() => new Promise(resolve => setTimeout(resolve, 220)));
  expect(result.current.mode).toBe("dot");
  expect(ipc.calls.slice(beforeHover).filter(call => call.cmd === "set_compact_window_shape")).toEqual([]);
  ipc.handle("plugin:window|cursor_position", () => ({ x: 140, y: 28 }));
  act(() => result.current.onPointerEnter({ pointerType: "mouse", buttons: 0 } as PointerEvent));
  await waitFor(() => expect(result.current.mode).toBe("toolbar"));
  expect(ipc.calls.slice(beforeHover).filter(call => call.cmd === "set_compact_window_shape").map(call => call.args.shape)).toEqual(["toolbar"]);
});

it.each([true, false])("checks the actual cursor after an expansion sends a pointer exit (cursor inside: %s)", async inside => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 280, height: 56 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|cursor_position", () => inside ? { x: 140, y: 28 } : { x: 400, y: 28 });
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  const { result } = renderHook(() => useWindowMode(false, true));
  await act(async () => { await result.current.change("toolbar"); });
  await act(async () => { await result.current.change("dot"); });
  let release!: () => void;
  const resized = new Promise<void>(resolve => { release = resolve; });
  ipc.handle("resize_compact_window", args => args.width === 280 ? resized : undefined);
  const beforeExpansion = ipc.calls.length;
  let expanded!: Promise<boolean>;
  act(() => { expanded = result.current.change("toolbar"); });
  await waitFor(() => expect(ipc.calls.slice(beforeExpansion).some(call => call.cmd === "resize_compact_window")).toBe(true));
  act(() => result.current.onPointerLeave({ pointerType: "mouse", buttons: 0 } as PointerEvent));
  await act(async () => { release(); await expanded; });
  await act(() => new Promise(resolve => setTimeout(resolve, 320)));
  expect(result.current.mode).toBe(inside ? "toolbar" : "dot");
  expect(ipc.calls.slice(beforeExpansion).filter(call => call.cmd === "resize_compact_window").map(call => call.args.width)).toEqual(inside ? [280] : [280, 64]);
});

it("ignores a stale pointer check when the user returns before it resolves", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 280, height: 56 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  let resolvePointer!: (point: { x: number; y: number }) => void;
  const pointer = new Promise<{ x: number; y: number }>(resolve => { resolvePointer = resolve; });
  ipc.handle("plugin:window|cursor_position", () => pointer);
  const { result } = renderHook(() => useWindowMode(false, true));
  await act(async () => { await result.current.change("toolbar"); });
  act(() => result.current.onPointerLeave({ pointerType: "mouse", buttons: 0 } as PointerEvent));
  await waitFor(() => expect(ipc.calls.some(call => call.cmd === "plugin:window|cursor_position")).toBe(true));
  act(() => result.current.onPointerEnter({ pointerType: "mouse", buttons: 0 } as PointerEvent));
  await act(async () => { resolvePointer({ x: 400, y: 28 }); await pointer; });
  expect(result.current.mode).toBe("toolbar");
  expect(ipc.calls.filter(call => call.cmd === "resize_compact_window")).toEqual([]);
});


it("keeps the toolbar and allows a retry when the native compact resize fails", async () => {
  mockWindows("main");
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { await result.current.change("toolbar"); });
  ipc.handle("resize_compact_window", () => { throw new Error("Window unavailable"); });
  await act(async () => { expect(await result.current.change("dot")).toBe(false); });
  expect(result.current.mode).toBe("toolbar");
  expect(result.current.busy).toBe(false);
  expect(result.current.error).toContain("Window unavailable");
  expect(document.documentElement).not.toHaveAttribute("data-window-transition");
  ipc.handle("resize_compact_window", () => undefined);
  await act(async () => { expect(await result.current.change("dot")).toBe(true); });
  expect(result.current.mode).toBe("dot");
  expect(result.current.error).toBeNull();
});


it.each(["maximized", "fullscreen"] as const)("preserves normal restore bounds after a compact round trip from %s", async presentation => {
  mockWindows("main");
  let maximized = presentation === "maximized";
  let fullscreen = presentation === "fullscreen";
  const normal = { width: 960, height: 700 };
  ipc.handle("plugin:window|inner_size", () => maximized || fullscreen ? { width: 2560, height: 1440 } : normal);
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => maximized);
  ipc.handle("plugin:window|is_fullscreen", () => fullscreen);
  ipc.handle("plugin:window|unmaximize", () => { maximized = false; });
  ipc.handle("plugin:window|maximize", () => { maximized = true; });
  ipc.handle("plugin:window|set_fullscreen", args => { fullscreen = args.value as boolean; });
  mockResizable(true);
  ipc.handle("set_compact_window_shape", () => true);
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { expect(await result.current.change("toolbar")).toBe(true); });
  expect(maximized).toBe(false);
  expect(fullscreen).toBe(false);
  await act(async () => { expect(await result.current.change("full")).toBe(true); });
  const sizes = ipc.calls.filter(call => call.cmd === "plugin:window|set_size");
  expect(JSON.parse(JSON.stringify(sizes[sizes.length - 1].args.value))).toEqual({ Logical: normal });
  expect(maximized).toBe(presentation === "maximized");
  expect(fullscreen).toBe(presentation === "fullscreen");
  expect(result.current.mode).toBe("full");
});

it("restores fullscreen after a compact entry failure", async () => {
  mockWindows("main");
  let fullscreen = true;
  ipc.handle("plugin:window|inner_size", () => ({ width: 960, height: 700 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  ipc.handle("plugin:window|is_fullscreen", () => fullscreen);
  ipc.handle("plugin:window|set_fullscreen", args => { fullscreen = args.value as boolean; });
  mockResizable(true);
  ipc.handle("set_compact_window_shape", () => true);
  ipc.handle("plugin:window|set_size", () => { throw new Error("Resize unavailable"); });
  const { result } = renderHook(() => useWindowMode());
  await act(async () => { expect(await result.current.change("toolbar")).toBe(false); });
  expect(fullscreen).toBe(true);
  expect(result.current.mode).toBe("full");
  expect(result.current.error).toContain("Resize unavailable");
});
