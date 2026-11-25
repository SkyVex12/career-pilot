import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit } from "@tauri-apps/api/event";
import { expect, it } from "vitest";
import { WorkspaceQuickSettings } from "../src/WorkspaceQuickSettings";
import type { OverlayPrefs, Settings } from "../src/api";
import { ipc } from "./ipc";
import { SETTINGS, settingsView } from "./fixtures";

function routes() {
  let prefs: OverlayPrefs = {
    opacity: 0.8, compact: false, click_through: false, on_top: true,
    hotkeys: { toggle_overlay: "Control+Shift+O", click_through: "Control+Shift+T", click_through_off: "Control+Shift+G", opacity_up: "Control+Shift+Up", opacity_down: "Control+Shift+Down" },
  };
  let settings = { ...SETTINGS };
  ipc.handle("window_prefs", () => ({ ...prefs }));
  ipc.handle("get_settings", () => settingsView({ settings }));
  ipc.handle("set_window_opacity", ({ value }) => { prefs = { ...prefs, opacity: Number(value) }; });
  ipc.handle("set_window_on_top", ({ on }) => { prefs = { ...prefs, on_top: Boolean(on) }; });
  ipc.handle("set_window_click_through", ({ on }) => { prefs = { ...prefs, click_through: Boolean(on) }; });
  ipc.handle("save_settings", ({ settings: next }) => { settings = next as Settings; });
  return { settings: () => settings, setSettings: (next: Settings) => { settings = next; }, setPrefs: (next: Partial<OverlayPrefs>) => { prefs = { ...prefs, ...next }; } };
}

async function open() {
  await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
  await screen.findByRole("slider", { name: "Transparency" });
  await screen.findByRole("switch", { name: "Stealth mode" });
}

it("edits shared window controls and refreshes after each change", async () => {
  routes();
  render(<WorkspaceQuickSettings running={false} />);
  await open();
  const dialog = screen.getByRole("dialog", { name: "Quick settings" });
  expect(within(dialog).getByRole("region", { name: "Both windows" })).toBeInTheDocument();
  expect(screen.getByRole("slider", { name: "Transparency" })).toHaveValue("80");
  expect(screen.getByRole("switch", { name: "Click-through" })).toHaveAccessibleDescription("Clicks pass through both windows. Ctrl+Shift+G turns it off.");
  await userEvent.click(screen.getByRole("switch", { name: "Keep on top" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "Keep on top" })).toHaveAttribute("aria-checked", "false"));
  await userEvent.click(screen.getByRole("switch", { name: "Click-through" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "Click-through" })).toHaveAttribute("aria-checked", "true"));
  const slider = screen.getByRole("slider", { name: "Transparency" });
  fireEvent.change(slider, { target: { value: "40" } });
  fireEvent.pointerUp(slider);
  await waitFor(() => expect(ipc.calls.find(call => call.cmd === "set_window_opacity")?.args).toEqual({ value: 0.4 }));
  expect(ipc.calls.find(call => call.cmd === "set_window_on_top")?.args).toEqual({ on: false });
  expect(ipc.calls.find(call => call.cmd === "set_window_click_through")?.args).toEqual({ on: true });
});

it("changes only stealth on the latest settings and reports a failed save", async () => {
  const state = routes();
  render(<WorkspaceQuickSettings running={false} />);
  await open();
  state.setSettings({ ...state.settings(), mic_device: "new-device", display_name: "Latest name" });
  await userEvent.click(screen.getByRole("switch", { name: "Stealth mode" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "Stealth mode" })).toHaveAttribute("aria-checked", "true"));
  expect(ipc.calls.find(call => call.cmd === "save_settings")?.args).toEqual({ settings: { ...SETTINGS, mic_device: "new-device", display_name: "Latest name", stealth: true }, devToken: null });
  ipc.handle("save_settings", () => { throw "Could not save settings"; });
  await userEvent.click(screen.getByRole("switch", { name: "Stealth mode" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not save settings");
  expect(screen.getByRole("switch", { name: "Stealth mode" })).toHaveAttribute("aria-checked", "true");
});

it("retries load failures, refreshes every open, and returns keyboard focus", async () => {
  const state = routes();
  ipc.handle("window_prefs", () => { throw "Window unavailable"; });
  render(<><WorkspaceQuickSettings running={false} /><button>Next control</button></>);
  await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Window unavailable");
  const retryPrefs: OverlayPrefs = { opacity: 1, compact: false, on_top: false, click_through: false, hotkeys: { toggle_overlay: "", click_through: "", click_through_off: "Control+Shift+G", opacity_up: "", opacity_down: "" } };
  ipc.handle("window_prefs", () => retryPrefs);
  await userEvent.click(screen.getByRole("button", { name: "Retry settings" }));
  await screen.findByRole("slider", { name: "Transparency" });
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).toBeNull();
  expect(screen.getByRole("button", { name: "Quick settings" })).toHaveFocus();
  state.setSettings({ ...state.settings(), stealth: true });
  await open();
  expect(screen.getByRole("switch", { name: "Stealth mode" })).toHaveAttribute("aria-checked", "true");
  const slider = screen.getByRole("slider", { name: "Transparency" });
  slider.focus();
  await userEvent.tab({ shift: true });
  expect(screen.getByRole("button", { name: "Quick settings" })).toHaveFocus();
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).toBeNull();
});

it("closes on an outside click and opens the existing settings screen", async () => {
  routes();
  render(<><WorkspaceQuickSettings running={false} /><button>Outside</button></>);
  await open();
  await userEvent.click(screen.getByRole("button", { name: "Outside" }));
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).toBeNull();
  await open();
  await userEvent.click(screen.getByRole("button", { name: "All settings…" }));
  await waitFor(() => expect(ipc.calls.some(call => call.cmd === "open_main_settings")).toBe(true));
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).toBeNull();
});

it("confirms quit while a call runs, without quitting on Cancel", async () => {
  routes();
  render(<WorkspaceQuickSettings running />);
  await open();
  await userEvent.click(screen.getByRole("button", { name: "Quit CoRuby" }));
  const confirmation = screen.getByRole("alertdialog", { name: "Quit during a call?" });
  expect(ipc.calls.some(call => call.cmd === "quit_app")).toBe(false);
  await userEvent.click(within(confirmation).getByRole("button", { name: "Cancel" }));
  expect(ipc.calls.some(call => call.cmd === "quit_app")).toBe(false);
  await open();
  await userEvent.click(screen.getByRole("button", { name: "Quit CoRuby" }));
  await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Quit CoRuby" }));
  await waitFor(() => expect(ipc.calls.some(call => call.cmd === "quit_app")).toBe(true));
});


it("reflects native window preference changes while the menu remains open", async () => {
  routes();
  render(<WorkspaceQuickSettings running={false} />);
  await open();
  await act(() => emit("copilot://workspace-window", {
    opacity: 0.5, compact: false, on_top: false, click_through: true,
    hotkeys: { click_through_off: "Control+Shift+G" },
  }));
  expect(screen.getByRole("slider", { name: "Transparency" })).toHaveValue("50");
  expect(screen.getByRole("switch", { name: "Keep on top" })).toHaveAttribute("aria-checked", "false");
  expect(screen.getByRole("switch", { name: "Click-through" })).toHaveAttribute("aria-checked", "true");
});
