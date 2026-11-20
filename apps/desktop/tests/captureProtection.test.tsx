import { act, render, screen, waitFor } from "@testing-library/react";
import { emit } from "@tauri-apps/api/event";
import { expect, it } from "vitest";
import { CaptureProtectionStatus, type CaptureExclusionStatus } from "../src/captureProtection";
import { ipc } from "./ipc";

const status: CaptureExclusionStatus = { window: "overlay", requested: true, effective: false, windows_build: 22000, error: "Windows rejected capture exclusion (error 8).", reason: "stealth" };
it("reports rejected native exclusion even when the preference is enabled, then clears it after off succeeds", async () => {
  ipc.handle("capture_exclusion_status", () => [status]);
  render(<CaptureProtectionStatus active enabled />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Windows could not hide this app");
  await act(async () => { await emit("capture-exclusion-status", { ...status, requested: false, effective: false, error: null, reason: "off" }); });
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});
it("keeps Stealth off without an assistant exception once native visibility is restored", async () => {
  const off = { ...status, requested: false, effective: false, error: null, reason: "off" };
  ipc.handle("capture_exclusion_status", () => [off]);
  const { container } = render(<CaptureProtectionStatus active enabled={false} />);
  await waitFor(() => expect(ipc.calls.some(call => call.cmd === "capture_exclusion_status")).toBe(true));
  expect(container).toBeEmptyDOMElement();
  await act(async () => { await emit("capture-exclusion-status", { ...off, effective: true, error: "Windows retained exclusion." }); });
  expect(await screen.findByRole("alert")).toHaveTextContent("Windows could not restore capture visibility");
  await act(async () => { await emit("capture-exclusion-status", off); });
  await waitFor(() => expect(container).toBeEmptyDOMElement());
});
