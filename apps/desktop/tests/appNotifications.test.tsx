import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { emit } from "@tauri-apps/api/event";
import { expect, it } from "vitest";
import { ServerNotification, UpdateNotification } from "../src/AppNotifications";
import { ToastProvider } from "../src/ui";
import { ipc } from "./ipc";

it("keeps a dismissed startup quiet until a new state or startup cycle", async () => {
  render(<ToastProvider persistent={<ServerNotification />}><h1>Call setup</h1></ToastProvider>);
  await act(async () => { await emit("copilot://server", { state: "starting", detail: "Starting services" }); });
  expect(await screen.findByRole("region", { name: "Starting the server" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Dismiss starting the server notification" }));
  await act(async () => { await emit("copilot://server", { state: "starting", detail: "Almost ready" }); });
  expect(screen.queryByRole("region", { name: "Starting the server" })).not.toBeInTheDocument();
  await act(async () => { await emit("copilot://server", { state: "failed", detail: "Connection timed out" }); });
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection timed out");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(ipc.calls.some(c => c.cmd === "wake_server")).toBe(true));
  expect(screen.getByRole("progressbar", { name: "Starting server" })).toBeVisible();
  await act(async () => { await emit("copilot://server", { state: "ready", detail: null }); });
  expect(screen.queryByRole("region", { name: "Starting the server" })).not.toBeInTheDocument();
  await act(async () => { await emit("copilot://server", { state: "starting", detail: null }); });
  expect(await screen.findByRole("region", { name: "Starting the server" })).toBeVisible();
});

it("reports a failed server retry and permits retrying again", async () => {
  ipc.handle("wake_server", () => { throw "Network unavailable"; });
  render(<ServerNotification />);
  await act(async () => { await emit("copilot://server", { state: "failed", detail: "Timed out" }); });
  fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Network unavailable");
  expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
});

it("ignores a retry rejection after a newer ready event", async () => {
  let rejectRetry!: (error: string) => void;
  ipc.handle("wake_server", () => new Promise((_resolve, reject) => { rejectRetry = reject; }));
  render(<ServerNotification />);
  await act(async () => { await emit("copilot://server", { state: "failed", detail: "Timed out" }); });
  fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
  await waitFor(() => expect(rejectRetry).toBeDefined());
  await act(async () => { await emit("copilot://server", { state: "ready", detail: null }); });
  await act(async () => { rejectRetry("Older request failed"); });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Server unavailable" })).not.toBeInTheDocument();
});

it("does not reopen dismissed download notices for each progress tick, and blocks restart during calls", async () => {
  const { rerender } = render(<UpdateNotification update={{ state: "downloading", version: "1.2.3", percent: 5 }} running={false} />);
  fireEvent.click(screen.getByRole("button", { name: "Dismiss app update notification" }));
  rerender(<UpdateNotification update={{ state: "downloading", version: "1.2.3", percent: 60 }} running={false} />);
  expect(screen.queryByRole("region", { name: "App update" })).not.toBeInTheDocument();
  const ready = { state: "ready", version: "1.2.3", notes: null, auto: false, install_in_s: null } as const;
  rerender(<UpdateNotification update={ready} running />);
  expect(await screen.findByRole("button", { name: "Install and restart" })).toBeDisabled();
  rerender(<UpdateNotification update={ready} running={false} />);
  ipc.handle("install_update", () => { throw "Installer unavailable"; });
  fireEvent.click(screen.getByRole("button", { name: "Install and restart" }));
  expect(await screen.findByText("Installer unavailable")).toBeVisible();
  expect(ipc.calls.filter(c => c.cmd === "install_update")).toHaveLength(1);
});
