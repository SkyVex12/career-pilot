import { emit } from "@tauri-apps/api/event";
import { mockWindows } from "@tauri-apps/api/mocks";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import App from "../src/App";
import { settingsView } from "./fixtures";
import { ipc } from "./ipc";

function setup() {
  mockWindows("main");
  ipc.handle("get_settings", () => settingsView());
  ipc.handle("snapshot", () => [[], false, 0]);
  ipc.handle("update_status", () => ({ state: "none" }));
  ipc.handle("list_devices", () => ({ inputs: [], outputs: [] }));
  ipc.handle("plugin:window|inner_size", () => ({ width: 648, height: 520 }));
  ipc.handle("plugin:window|inner_position", () => ({ x: 0, y: 0 }));
  ipc.handle("plugin:window|cursor_position", () => ({ x: 1000, y: 1000 }));
  ipc.handle("plugin:window|scale_factor", () => 1);
  ipc.handle("plugin:window|is_decorated", () => false);
  ipc.handle("plugin:window|is_maximized", () => false);
  ipc.handle("plugin:window|is_minimized", () => false);
  ipc.handle("plugin:window|is_resizable", () => true);
  ipc.handle("set_compact_window_shape", () => true);
  ipc.api("GET", /^\/profiles$/, () => ({ items: [{ id: "account", name: "Dana", me: true, career_os_user_id: null }] }));
  ipc.api("GET", /^\/career-os\/connection$/, () => ({ connected: true, available: true }));
  ipc.api("GET", /^\/applications$/, () => ({ applications: [] }));
  ipc.api("GET", /^\/interviews$/, () => ({ interviews: [] }));
  ipc.api("GET", /^\/employers$/, () => ({ employers: [] }));
  ipc.api("GET", /^\/sessions\/live$/, () => ({ sessions: [] }));
  ipc.api("GET", /^\/resumes$/, () => ({ base: null, versions: [] }));
  return render(<App />);
}

async function compact(mode: "toolbar" | "dot") {
  await waitFor(() => expect(screen.getByRole("combobox", { name: "Profile" })).toHaveTextContent("Me (Dana)"));
  await userEvent.click(screen.getByRole("button", { name: "Collapse to toolbar" }));
  const ruby = await screen.findByRole("button", { name: "Expand full window" });
  await waitFor(() => expect(ruby.closest("header")).not.toHaveAttribute("inert"));
  if (mode === "dot") {
    fireEvent.pointerLeave(ruby.closest(".session-window")!);
    await screen.findByRole("button", { name: "Expand compact toolbar" });
    await waitFor(() => expect(ruby.closest("header")).not.toHaveAttribute("inert"));
  }
  return ruby.closest(".session-window")!;
}

function holdRestore() {
  let complete!: () => void;
  let started = false;
  ipc.handle("plugin:window|set_size", () => {
    started = true;
    return new Promise<void>(resolve => { complete = resolve; });
  });
  return {
    wait: () => waitFor(() => expect(started).toBe(true)),
    finish: () => act(async () => complete()),
  };
}

it.each(["toolbar", "dot"] as const)("restores %s bounds before opening native Settings", async mode => {
  setup();
  const surface = await compact(mode);
  const restore = holdRestore();
  await act(() => emit("copilot://open-settings"));
  await restore.wait();
  expect(surface).toHaveAttribute("data-mini", mode);
  expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
  await restore.finish();
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
  expect(surface).toHaveAttribute("data-mini", "full");
  expect(ipc.calls.filter(call => call.cmd === "set_compact_window_shape").slice(-1)[0]?.args).toEqual({ shape: "full" });
  const bounds = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").slice(-1)[0]?.args.value;
  expect(JSON.parse(JSON.stringify(bounds))).toEqual({ Logical: { width: 648, height: 520 } });
  expect(ipc.calls.some(call => ["start_session", "open_session", "end_session"].includes(call.cmd))).toBe(false);
});

it("restores compact bounds for the idle overlay's return-to-call event", async () => {
  setup();
  const surface = await compact("dot");
  await act(() => emit("copilot://open-call"));
  await waitFor(() => expect(surface).toHaveAttribute("data-mini", "full"));
  expect(screen.getByRole("button", { name: "Collapse to toolbar" })).toBeVisible();
  expect(screen.getByRole("button", { name: /^Start call/ })).toBeVisible();
  expect(ipc.calls.some(call => ["start_session", "open_session", "end_session"].includes(call.cmd))).toBe(false);
});

it("keeps compact controls reachable when expansion fails and allows a later retry", async () => {
  setup();
  const surface = await compact("toolbar");
  ipc.handle("set_compact_window_shape", ({ shape }) => {
    if (shape === "full") throw "Could not restore the window";
    return true;
  });
  await act(() => emit("copilot://open-settings"));
  await waitFor(() => expect(document.documentElement).not.toHaveAttribute("data-window-transition"));
  expect(surface).toHaveAttribute("data-mini", "toolbar");
  expect(screen.getByRole("button", { name: "Expand full window" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
  ipc.handle("set_compact_window_shape", () => true);
  await act(() => emit("copilot://open-settings"));
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
  expect(surface).toHaveAttribute("data-mini", "full");
});

it("uses the latest native destination during a single pending expansion", async () => {
  setup();
  const surface = await compact("toolbar");
  const before = ipc.calls.filter(call => call.cmd === "plugin:window|set_size").length;
  const restore = holdRestore();
  await act(() => emit("copilot://open-settings"));
  await restore.wait();
  await act(() => emit("copilot://open-call"));
  await restore.finish();
  await waitFor(() => expect(surface).toHaveAttribute("data-mini", "full"));
  expect(screen.queryByRole("heading", { name: "Settings" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Start call/ })).toBeVisible();
  expect(ipc.calls.filter(call => call.cmd === "plugin:window|set_size")).toHaveLength(before + 1);
});
