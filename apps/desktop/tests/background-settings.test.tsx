import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BackgroundSettings } from "../src/BackgroundSettings";
import { ipc } from "./ipc";
const preferences = { launch_at_login: false, keep_running: true, suggestions_paused: false, call_suggestions: true };
function setup(state: string, extra = {}) {
  ipc.handle("background_status", () => ({ preferences, call_detection_available: true, launch_at_login_supported: true, limitation: "" }));
  ipc.handle("background_detection_status", () => ({ state, diagnostics: null, pending: null, error: null, presentation_error: null, ...extra }));
}
afterEach(() => vi.useRealTimers());
it.each([
  ["disabled", "Call suggestions are off"], ["paused", "Call suggestions are paused"],
  ["waiting_microphone", "Waiting for microphone activity"], ["no_call_controls", "No supported call controls found"],
  ["notifications_suppressed", "System notification settings or Focus"], ["active_session", "already running a session"],
  ["suggestion_pending", "suggestion is waiting"], ["app_disabled", "application's call suggestions are disabled"],
  ["error", "native check failed"], ["unavailable", "unavailable on this system"],
  ["watching_hushed", "Watching for Hushed call activity. Microphone activity is not required."],
])("explains %s without starting capture", async (state, message) => {
  setup(state); render(<BackgroundSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "Check detection" }));
  expect(await screen.findByRole("status")).toHaveTextContent(message);
  expect(ipc.apiCalls()).toHaveLength(0);
  expect(ipc.calls.every(c => ["background_status", "background_detection_status"].includes(c.cmd))).toBe(true);
});
it("shows native transport failure and allows retry", async () => {
  setup("watching"); ipc.handle("background_detection_status", () => { throw new Error("native unavailable"); });
  render(<BackgroundSettings />); fireEvent.click(await screen.findByRole("button", { name: "Check detection" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("native unavailable");
  ipc.handle("background_detection_status", () => ({ state: "watching" }));
  fireEvent.click(screen.getByRole("button", { name: "Check detection" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Watching for a supported call");
});
it("explains presentation failures and limited inspection", async () => {
  setup("suggestion_pending", { presentation_error: "window unavailable", diagnostics: { inspection_limited: true } });
  render(<BackgroundSettings />); fireEvent.click(await screen.findByRole("button", { name: "Check detection" }));
  expect(await screen.findByRole("status")).toHaveTextContent("suggestion could not open: window unavailable");
  expect(screen.getByRole("status")).toHaveTextContent("The call controls could not all be checked");
});
it("does not schedule delayed checks after unmount while native response is pending", async () => {
  setup("checking"); let finish!: (value: unknown) => void;
  ipc.handle("background_detection_status", () => new Promise(resolve => { finish = resolve; }));
  const view = render(<BackgroundSettings />); fireEvent.click(await screen.findByRole("button", { name: "Check detection" }));
  view.unmount(); vi.useFakeTimers();
  await act(async () => { finish({ state: "checking" }); await Promise.resolve(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(ipc.calls.filter(c => c.cmd === "background_detection_status")).toHaveLength(1);
});
it("reads cached checking status only once more after the native poll interval", async () => {
  setup("checking"); const view = render(<BackgroundSettings />);
  const button = await screen.findByRole("button", { name: "Check detection" }); vi.useFakeTimers();
  await act(async () => { fireEvent.click(button); });
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(ipc.calls.filter(c => c.cmd === "background_detection_status")).toHaveLength(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(ipc.calls.filter(c => c.cmd === "background_detection_status")).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
  expect(ipc.calls.filter(c => c.cmd === "background_detection_status")).toHaveLength(2);
  view.unmount();
});

it.each(["disabled", "paused", "active_session"])("does not show a stale delivery error over %s", async state => {
  setup(state, { presentation_error: "old window failure" });
  render(<BackgroundSettings />); fireEvent.click(await screen.findByRole("button", { name: "Check detection" }));
  expect(await screen.findByRole("status")).not.toHaveTextContent("old window failure");
});

it("discards a pending detection read after call suggestion policy changes", async () => {
  setup("watching_hushed");
  let finish!: (value: unknown) => void;
  ipc.handle("background_detection_status", () => new Promise(resolve => { finish = resolve; }));
  ipc.handle("background_set_preferences", args => ({ preferences: args.preferences, call_detection_available: true, launch_at_login_supported: true, limitation: "" }));
  render(<BackgroundSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "Check detection" }));
  fireEvent.click(screen.getByRole("switch", { name: "Pause call suggestions" }));
  await act(async () => { await Promise.resolve(); });
  expect(screen.getByRole("switch", { name: "Pause call suggestions" })).toBeChecked();
  await act(async () => { finish({ state: "suggestion_pending", pending: { platform: "Hushed", kind: "start" }, hushed: { state: "incoming", message: "Old incoming call", connected: 1, monitored: 1 } }); });
  expect(screen.queryByText(/Old incoming call|A suggestion is waiting/)).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
