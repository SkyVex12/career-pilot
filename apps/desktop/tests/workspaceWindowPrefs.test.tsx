import { act, renderHook, waitFor } from "@testing-library/react";
import { emit } from "@tauri-apps/api/event";
import { expect, it } from "vitest";
import { useWorkspaceWindowOpacity } from "../src/App";
import { ipc } from "./ipc";

const opacity = () => document.documentElement.style.getPropertyValue("--workspace-opacity");

it("restores window background opacity and responds to native preference events", async () => {
  ipc.handle("window_prefs", () => ({ opacity: 0.7 }));
  const view = renderHook(useWorkspaceWindowOpacity);
  await waitFor(() => expect(opacity()).toBe("0.7"));
  await act(() => emit("copilot://workspace-window", { opacity: 0.4 }));
  expect(opacity()).toBe("0.4");
  view.unmount();
  expect(opacity()).toBe("");
  await act(() => emit("copilot://workspace-window", { opacity: 0.8 }));
  expect(opacity()).toBe("");
});

it("does not overwrite a new window preference with a delayed initial response", async () => {
  let finish!: (prefs: { opacity: number }) => void;
  ipc.handle("window_prefs", () => new Promise(resolve => { finish = resolve; }));
  renderHook(useWorkspaceWindowOpacity);
  await waitFor(() => expect(ipc.calls.some(call => call.cmd === "window_prefs")).toBe(true));
  await act(() => emit("copilot://workspace-window", { opacity: 0.5 }));
  await act(async () => finish({ opacity: 1 }));
  expect(opacity()).toBe("0.5");
});
