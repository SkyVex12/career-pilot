import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit } from "@tauri-apps/api/event";
import { expect, it, vi } from "vitest";
import { TransparencyControl, ReadingSettings } from "../src/WindowSettingControls";
import { useViewPrefs, useAnswerStyle } from "../src/viewPreferences";
import { useWorkspaceWindowOpacity } from "../src/App";
import { TEXT_SCALE_KEY } from "../src/transcript";
import { ipc } from "./ipc";

it("moves left to ninety percent transparent and right to solid without numeric labels", () => {
  const change = vi.fn();
  const view = render(<TransparencyControl opacity={1} onChange={change} />);
  const slider = screen.getByRole("slider", { name: "Transparency" });
  expect(slider).toHaveValue("100");
  fireEvent.change(slider, { target: { value: "10" } });
  expect(change).toHaveBeenLastCalledWith(0.1);
  view.rerender(<TransparencyControl opacity={0.1} onChange={change} />);
  expect(slider).toHaveAttribute("aria-valuetext", "90% transparent");
  expect(view.container.textContent).not.toMatch(/%|\d/);
  fireEvent.change(slider, { target: { value: "100" } });
  expect(change).toHaveBeenLastCalledWith(1);
});

it("main accepts the full shared opacity range, including events from a hidden overlay", async () => {
  ipc.handle("window_prefs", () => ({ opacity: 1 }));
  renderHook(useWorkspaceWindowOpacity);
  await waitFor(() => expect(document.documentElement.style.getPropertyValue("--workspace-opacity")).toBe("1"));
  await act(() => emit("copilot://workspace-window", { opacity: 0.1 }));
  expect(document.documentElement.style.getPropertyValue("--workspace-opacity")).toBe("0.1");
});

it("updates every mounted reading control and restores saved values when reopened", async () => {
  function Control({ name }: { name: string }) {
    return <section aria-label={name}><ReadingSettings running={false} busy={false} run={action => { void action(); }} /></section>;
  }
  const view = render(<><Control name="Main" /><Control name="Overlay" /></>);
  const liveView = renderHook(useViewPrefs);
  const main = within(screen.getByRole("region", { name: "Main" }));
  const overlay = within(screen.getByRole("region", { name: "Overlay" }));
  await userEvent.click(main.getByRole("radio", { name: "Paragraphs" }));
  expect(overlay.getByRole("radio", { name: "Paragraphs" })).toBeChecked();
  await userEvent.click(overlay.getByRole("button", { name: "Larger text (Ctrl+plus)" }));
  expect(liveView.result.current.textScale).toBe(1.1);
  expect(localStorage.getItem(TEXT_SCALE_KEY)).toBe("1.1");
  view.unmount();
  render(<Control name="Reopened" />);
  expect(screen.getByRole("radio", { name: "Paragraphs" })).toBeChecked();
  await userEvent.click(screen.getByRole("button", { name: "Reset" }));
  expect(liveView.result.current.textScale).toBe(1);
});

it("receives changes from another webview through storage events", () => {
  const view = renderHook(() => ({ view: useViewPrefs(), answer: useAnswerStyle(false)[0] }));
  act(() => {
    localStorage.setItem(TEXT_SCALE_KEY, "1.4");
    localStorage.setItem("copilot.answerStyle", "paragraph");
    window.dispatchEvent(new StorageEvent("storage", { key: "copilot.answerStyle", newValue: "paragraph" }));
  });
  expect(view.result.current.view.textScale).toBe(1.4);
  expect(view.result.current.answer).toBe("paragraph");
});

it("only shares a live answer-format change after the session accepts it", async () => {
  const host = renderHook(() => useAnswerStyle(true));
  const other = renderHook(() => useAnswerStyle(false));
  ipc.handle("set_answer_style", () => { throw "Disconnected"; });
  await act(async () => { await expect(host.result.current[1]("paragraph")).rejects.toBe("Disconnected"); });
  expect(other.result.current[0]).toBe("bullets");
  ipc.handle("set_answer_style", () => null);
  await act(async () => { await host.result.current[1]("paragraph"); });
  expect(other.result.current[0]).toBe("paragraph");
  expect(ipc.calls.filter(call => call.cmd === "set_answer_style")).toHaveLength(2);
});
