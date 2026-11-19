import { mockWindows } from "@tauri-apps/api/mocks";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Shell } from "../src/Shell";
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

function workspace() {
  render(<Shell tab="applications" onTab={() => {}} session={initial} profiles={{ profiles: [], current: null, here: null }}
    onSwitchProfile={() => {}} profileError={null} version={null} showDebug={false}>
    <input aria-label="Draft note" />
  </Shell>);
  const app = screen.getByLabelText("App menu");
  const details = app.closest("details")!;
  const gear = screen.getByRole("button", { name: "Quick settings" });
  const note = screen.getByRole("textbox", { name: "Draft note" });
  act(() => note.focus());
  return { app, details, gear, note };
}

it("opens each menu on hover without stealing focus, including after settings load", async () => {
  const { app, details, gear, note } = workspace();
  fireEvent.pointerEnter(app);
  await advance(119);
  expect(details).not.toHaveAttribute("open");
  await advance(1);
  expect(details).toHaveAttribute("open");
  expect(note).toHaveFocus();

  fireEvent.pointerEnter(gear);
  await advance(120);
  expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeInTheDocument();
  expect(screen.getByRole("slider", { name: "Transparency" })).toHaveValue("20");
  expect(note).toHaveFocus();
});

it("keeps only one workspace menu open when moving between its triggers", async () => {
  const { app, details, gear } = workspace();
  fireEvent.pointerEnter(app);
  await advance(120);
  expect(details).toHaveAttribute("open");
  fireEvent.pointerLeave(app);
  fireEvent.pointerEnter(gear);
  await advance(120);
  expect(details).not.toHaveAttribute("open");
  expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeInTheDocument();

  fireEvent.pointerLeave(gear);
  fireEvent.pointerEnter(app);
  await advance(120);
  expect(details).toHaveAttribute("open");
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).toBeNull();
});

it("lets the pointer cross the app-menu gap and closes after leaving the panel", async () => {
  const { app, details, note } = workspace();
  fireEvent.pointerEnter(app);
  await advance(120);
  fireEvent.pointerLeave(app);
  await advance(180);
  const panel = details.querySelector<HTMLElement>(".workspace-menu-panel")!;
  fireEvent.pointerEnter(panel);
  await advance(240);
  expect(details).toHaveAttribute("open");
  expect(note).toHaveFocus();

  fireEvent.pointerLeave(panel);
  await advance(239);
  expect(details).toHaveAttribute("open");
  await advance(1);
  expect(details).not.toHaveAttribute("open");
});

it("lets the pointer reach the quick-settings portal and closes after leaving it", async () => {
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
  expect(screen.queryByRole("dialog", { name: "Quick settings" })).toBeNull();
});

it.each(["app", "settings"] as const)("pins the %s menu on click after hover, until another click or Escape", async menu => {
  const { app, details, gear } = workspace();
  const trigger = menu === "app" ? app : gear;
  const isOpen = () => menu === "app" ? details.open : !!screen.queryByRole("dialog", { name: "Quick settings" });
  fireEvent.pointerEnter(trigger);
  await advance(120);
  expect(isOpen()).toBe(true);
  act(() => trigger.focus());
  fireEvent.click(trigger);
  fireEvent.pointerLeave(trigger);
  await advance(500);
  expect(isOpen()).toBe(true);
  fireEvent.click(trigger);
  expect(isOpen()).toBe(false);

  fireEvent.pointerEnter(trigger);
  await advance(120);
  fireEvent.click(trigger);
  fireEvent.pointerLeave(trigger);
  await advance(500);
  expect(isOpen()).toBe(true);
  fireEvent.keyDown(trigger, { key: "Escape" });
  expect(isOpen()).toBe(false);
  expect(trigger).toHaveFocus();
});


it.each(["applications", "resumes", "work", "history", "settings", "debug"] as const)("returns from %s to call setup", async tab => {
  const onTab = vi.fn();
  render(<Shell tab={tab} onTab={onTab} session={initial} profiles={{ profiles: [], current: null, here: null }}
    onSwitchProfile={() => {}} profileError={null} version={null} showDebug>
    <p>Page content</p>
  </Shell>);
  fireEvent.click(screen.getByRole("button", { name: "Go to call" }));
  expect(onTab).toHaveBeenCalledWith("start");
});
