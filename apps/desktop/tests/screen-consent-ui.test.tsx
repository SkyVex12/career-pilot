import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit, listen } from "@tauri-apps/api/event";
import { describe, expect, it } from "vitest";
import { HostControlRequests } from "../src/screenui";
import { SCREEN_OFF } from "../src/screen";
import { initial, type SessionView } from "../src/session";
import { ipc } from "./ipc";

const request = { sessionId: "s1", identity: "seat:p", name: "Blair", requestId: "request-123", expiresAt: Date.now() + 60_000 };
const session = { ...initial, running: true, sessionId: "s1", seats: [
  { seat_id: "p", name: "Blair", status: "admitted", capabilities: ["view", "device.view"] },
] } as SessionView;

async function prompt() {
  const commands: Record<string, unknown>[] = [];
  const off = await listen<Record<string, unknown>>("screen:cmd", e => commands.push(e.payload));
  const view = render(<HostControlRequests s={session} />);
  await waitFor(() => expect(commands.some(c => c.cmd === "status")).toBe(true));
  await act(() => emit("screen:status", { ...SCREEN_OFF, sessionId: "s1", host: "on", surface: "monitor", asking: [request] }));
  expect(await screen.findByRole("dialog", { name: "Allow remote control?" })).toBeInTheDocument();
  expect(screen.getByText(/Blair is asking to use your mouse/)).toBeInTheDocument();
  return { commands, off: async () => { view.unmount(); off(); await act(async () => {}); }, view };
}

describe("host remote control consent", () => {
  it("shows the prompt without the Assistants panel and grants only after explicit approval", async () => {
    ipc.api("PATCH", /^\/sessions\/s1\/seats\/p$/, () => ({}));
    const { commands, off } = await prompt();
    expect(ipc.apiCalls("PATCH")).toEqual([]);
    expect(commands.some(c => c.cmd === "control")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Approve control" }));
    await waitFor(() => expect(commands).toContainEqual({ cmd: "control", person: request }));
    expect(ipc.apiCalls("PATCH")[0].body).toEqual({ capabilities: ["view", "device.view", "device.control"], expected_capabilities: ["view", "device.view"] });
    await off();
  });
  it("declining preserves view-only permission and informs the sharing window", async () => {
    const { commands, off } = await prompt();
    await userEvent.click(screen.getByRole("button", { name: "Decline" }));
    await waitFor(() => expect(commands).toContainEqual({ cmd: "decline", request }));
    expect(ipc.apiCalls("PATCH")).toEqual([]);
    expect(commands.some(c => c.cmd === "control")).toBe(false);
    await off();
  });
  it("never permits native control if granting the capability fails", async () => {
    ipc.api("PATCH", /^\/sessions\/s1\/seats\/p$/, () => { throw "Assistant has left"; });
    const { commands, off } = await prompt();
    await userEvent.click(screen.getByRole("button", { name: "Approve control" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Assistant has left");
    expect(commands.some(c => c.cmd === "control")).toBe(false);
    await off();
  });
});


it("shows native attention once per request and restores it on cancellation", async () => {
  const { view, off } = await prompt();
  await waitFor(() => expect(ipc.calls.filter(c => c.cmd === "screen_control_prompt" && c.args.active === true)).toHaveLength(1));
  view.rerender(<HostControlRequests s={{ ...session }} />);
  expect(ipc.calls.filter(c => c.cmd === "screen_control_prompt" && c.args.active === true)).toHaveLength(1);
  await act(() => emit("screen:status", { ...SCREEN_OFF, sessionId: "s1", host: "on", asking: [] }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(ipc.calls).toContainEqual({ cmd: "screen_control_prompt", args: { sessionId: "s1", requestId: request.requestId, active: false, durationMs: 0 } });
  await off();
});

it("never approves an old request when its capability response arrives after cancellation", async () => {
  let finish!: (value: unknown) => void;
  ipc.api("PATCH", /^\/sessions\/s1\/seats\/p$/, () => new Promise(resolve => { finish = resolve; }));
  const { commands, off } = await prompt();
  await userEvent.click(screen.getByRole("button", { name: "Approve control" }));
  await act(() => emit("screen:status", { ...SCREEN_OFF, sessionId: "s1", host: "on", asking: [] }));
  await act(async () => { finish({}); });
  expect(commands.some(c => c.cmd === "control")).toBe(false);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await off();
});

it("an old approval error cannot overwrite the next request's pending state", async () => {
  let fail!: (error: string) => void;
  let finish!: (value: unknown) => void;
  ipc.api("PATCH", /^\/sessions\/s1\/seats\/p$/, () => new Promise((_resolve, reject) => { fail = reject; }));
  const { off } = await prompt();
  await userEvent.click(screen.getByRole("button", { name: "Approve control" }));
  const next = { ...request, requestId: "request-next" };
  await act(() => emit("screen:status", { ...SCREEN_OFF, sessionId: "s1", host: "on", asking: [next] }));
  ipc.api("PATCH", /^\/sessions\/s1\/seats\/p$/, () => new Promise(resolve => { finish = resolve; }));
  await userEvent.click(screen.getByRole("button", { name: "Approve control" }));
  await act(async () => { fail("Stale failure"); });
  expect(screen.queryByText("Stale failure")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Updating…" })).toBeDisabled();
  await act(async () => { finish({}); });
  await off();
});

it("ignores a status from a different call and a viewer whose access was revoked", async () => {
  const { view, off } = await prompt();
  view.rerender(<HostControlRequests s={{ ...session, sessionId: "new-call" }} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  view.rerender(<HostControlRequests s={{ ...session, seats: [] }} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await off();
});
