import { mockWindows } from "@tauri-apps/api/mocks";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Shell, useWorkspaceOpenQuickSettings } from "../src/Shell";
import { initial } from "../src/session";
import type { OverlayPrefs } from "../src/api";
import { settingsView } from "./fixtures";
import { ipc } from "./ipc";

beforeEach(() => {
  vi.useFakeTimers();
  mockWindows("main");
  const prefs: OverlayPrefs = {
    opacity: 0.8, compact: false, click_through: false, on_top: true,
    hotkeys: { toggle_overlay: "", click_through: "", click_through_off: "", opacity_up: "", opacity_down: "" },
  };
  ipc.handle("window_prefs", () => prefs);
  ipc.handle("get_settings", () => settingsView());
});
afterEach(() => vi.useRealTimers());

async function advance(time: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(time); });
}
const profiles = { profiles: [{ id: "me", name: "Alex Rivera", me: true, career_os_user_id: null }, { id: "work", name: "Jordan Lee", me: false, career_os_user_id: "u2" }], current: null, here: "me" };
function QuickSettingsShortcut() {
  const open = useWorkspaceOpenQuickSettings();
  return <button onClick={() => open?.()}>Open settings from compact</button>;
}
function workspace() {
  const onTab = vi.fn(), onSwitch = vi.fn();
  render(<Shell tab="applications" onTab={onTab} session={initial} profiles={profiles}
    onSwitchProfile={onSwitch} profileError={null} version={null} showDebug={false}>
    <input aria-label="Draft note" /><QuickSettingsShortcut />
  </Shell>);
  const gear = screen.getByRole("button", { name: "Quick settings" });
  const note = screen.getByRole("textbox", { name: "Draft note" });
  const profile = screen.getByRole("combobox", { name: "Profile" });
  act(() => note.focus());
  return { gear, note, profile, onTab, onSwitch };
}

it("keeps the primary destinations visible without a second navigation inside Interview prep", () => {
  const { onTab, note } = workspace();
  const navigation = screen.getByRole("navigation", { name: "Main" });
  expect(within(navigation).getAllByRole("button").map(button => button.getAttribute("aria-label"))).toEqual(["Call", "Interview prep", "Work context", "Past calls"]);
  expect(within(navigation).getByRole("button", { name: "Interview prep" })).toHaveAttribute("aria-current", "page");
  expect(screen.queryByLabelText("App menu")).not.toBeInTheDocument();
  expect(note).toHaveFocus();
  fireEvent.click(within(navigation).getByRole("button", { name: "Work context" }));
  expect(onTab).toHaveBeenLastCalledWith("work");
  expect(screen.queryByRole("navigation", { name: "Interview preparation" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Resumes" })).not.toBeInTheDocument();
});

it("opens Quick settings on hover without stealing the draft focus, including after settings load", async () => {
  const { gear, note } = workspace();
  fireEvent.pointerEnter(gear);
  await advance(119);
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
  await advance(1);
  expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeInTheDocument();
  expect(screen.getByRole("slider", { name: "Transparency" })).toHaveValue("20");
  expect(note).toHaveFocus();
});

it("lets the pointer reach the Quick settings portal and closes after leaving it", async () => {
  const { gear, note } = workspace();
  fireEvent.pointerEnter(gear);
  await advance(120);
  const panel = screen.getByRole("dialog", { name: "Quick settings" });
  expect(panel.parentElement).toBe(document.body);
  fireEvent.pointerLeave(gear);
  await advance(180);
  fireEvent.pointerEnter(panel);
  await advance(240);
  expect(panel).toBeInTheDocument();
  expect(note).toHaveFocus();
  fireEvent.pointerLeave(panel);
  await advance(239);
  expect(panel).toBeInTheDocument();
  await advance(1);
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
});

it("pins Quick settings after a hover click, and Escape closes it with focus on its persistent gear", async () => {
  const { gear } = workspace();
  fireEvent.pointerEnter(gear);
  await advance(120);
  act(() => gear.focus());
  fireEvent.click(gear);
  fireEvent.pointerLeave(gear);
  await advance(500);
  expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeInTheDocument();
  fireEvent.click(gear);
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
  fireEvent.pointerEnter(gear);
  await advance(120);
  fireEvent.click(gear);
  fireEvent.pointerLeave(gear);
  await advance(500);
  fireEvent.keyDown(screen.getByRole("dialog", { name: "Quick settings" }), { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
  expect(gear).toHaveFocus();
});

it("dismisses Quick settings when opening Profile, and dismisses Profile on an outside click", async () => {
  const { gear, profile, note, onSwitch } = workspace();
  fireEvent.click(gear);
  await advance(1);
  expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeInTheDocument();
  fireEvent.pointerDown(profile);
  act(() => profile.focus());
  fireEvent.click(profile);
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
  expect(screen.getByRole("listbox")).toBeInTheDocument();
  fireEvent.pointerDown(note);
  act(() => note.focus());
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(onSwitch).not.toHaveBeenCalled();
  fireEvent.click(profile);
  fireEvent.click(screen.getByRole("option", { name: "Jordan Lee" }));
  expect(onSwitch).toHaveBeenCalledWith("work");
});

it("opens the same persistent gear from compact setup without adding a second trigger", async () => {
  workspace();
  fireEvent.click(screen.getByRole("button", { name: "Open settings from compact" }));
  await advance(1);
  expect(screen.getAllByRole("button", { name: "Quick settings" })).toHaveLength(1);
  expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeInTheDocument();
});

it.each(["applications", "work", "history", "settings", "debug"] as const)("returns from %s through the persistent Call tab", async tab => {
  const onTab = vi.fn();
  render(<Shell tab={tab} onTab={onTab} session={initial} profiles={profiles}
    onSwitchProfile={() => {}} profileError={null} version={null} showDebug>
    <p>Page content</p>
  </Shell>);
  fireEvent.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("button", { name: "Call" }));
  expect(onTab).toHaveBeenCalledWith("start");
});