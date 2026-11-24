import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { SessionDetail } from "../src/SessionDetail";
import { Work } from "../src/Work";
import { ToastProvider } from "../src/ui";
import { localDay } from "../src/workday";
import { employer } from "./fixtures";
import { ipc } from "./ipc";

it("preserves a stale notes draft, reloads the conflicting version, and uses the save receipt revision", async () => {
  let revision = 10, saved = "Original notes";
  ipc.api("GET", /^\/sessions\/s1$/, () => ({ id: "s1", status: "ready", mode: "interview", store_transcript: false, questions: 0, notes: saved, revision, outputs: { summary: "Summary" } }));
  ipc.api("PUT", /^\/sessions\/s1\/notes$/, (_path, body) => {
    const edit = body as { text: string; expected_revision: number };
    if (edit.expected_revision !== revision) throw "This call changed; reload before saving. (HTTP 409)";
    saved = edit.text; revision++;
    return { notes: saved, revision };
  });
  render(<SessionDetail sessionId="s1" />);
  await userEvent.click(await screen.findByRole("tab", { name: "Notes" }));
  fireEvent.change(screen.getByLabelText("Call notes"), { target: { value: "My unsaved draft" } });
  saved = "Notes from another device"; revision = 20;
  await userEvent.click(screen.getByRole("button", { name: "Save notes" }));
  expect(ipc.apiCalls("PUT")[0].body).toEqual({ text: "My unsaved draft", expected_revision: 10 });
  expect(screen.getByLabelText("Call notes")).toHaveValue("My unsaved draft");
  expect(screen.getByRole("button", { name: "Save notes" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Load latest notes" }));
  expect(await screen.findByText("Notes from another device")).toBeVisible();
  expect(screen.getByLabelText("Call notes")).toHaveValue("My unsaved draft");
  expect(ipc.calls.filter(call => call.args.path === "/sessions/s1").slice(-1)[0]?.args.refresh).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "Save notes" }));
  expect(ipc.apiCalls("PUT").slice(-1)[0]?.body).toEqual({ text: "My unsaved draft", expected_revision: 20 });
  fireEvent.change(screen.getByLabelText("Call notes"), { target: { value: "More notes" } });
  await userEvent.click(screen.getByRole("button", { name: "Save notes" }));
  expect(ipc.apiCalls("PUT").slice(-1)[0]?.body).toEqual({ text: "More notes", expected_revision: 21 });
});

it("revalidates visible work on foreground return without rebasing a dirty draft", async () => {
  let revision = 10, saved = "Original work";
  const log = () => ({ id: "l1", day: localDay(), text: saved, revision, status: "extracted", attachments: [], items: [], items_count: 0 });
  ipc.api("GET", /^\/employers$/, () => ({ employers: [employer("e1", "Fernhill")] }));
  ipc.api("GET", /^\/employers\/e1\/logs$/, () => ({ logs: [log()], next_before: null }));
  ipc.api("GET", /^\/employers\/e1\/logs\/[^/]+$/, () => log());
  ipc.api("PUT", /^\/employers\/e1\/logs\/[^/]+$/, (_path, body) => {
    const edit = body as { text: string; expected_revision: number };
    if (edit.expected_revision !== revision) throw "This work log changed; reload before saving. (HTTP 409)";
    saved = edit.text; revision++;
    return log();
  });
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  try {
    render(<ToastProvider><Work /></ToastProvider>);
    await waitFor(() => expect(screen.getByLabelText("What you worked on")).toHaveValue("Original work"));
    fireEvent.change(screen.getByLabelText("What you worked on"), { target: { value: "My work draft" } });
    saved = "Work from another device"; revision = 20;
    await act(async () => { visibility.mockReturnValue("hidden"); document.dispatchEvent(new Event("visibilitychange")); });
    await act(async () => { visibility.mockReturnValue("visible"); document.dispatchEvent(new Event("visibilitychange")); });
    expect(ipc.apiCalls("GET").filter(call => call.path === "/employers/e1/logs")).toHaveLength(2);
    expect(screen.getByLabelText("What you worked on")).toHaveValue("My work draft");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(ipc.apiCalls("PUT")[0].body).toEqual({ text: "My work draft", expected_revision: 10 });
    expect(await screen.findByText("This work log changed")).toBeVisible();
    expect(screen.getByLabelText("What you worked on")).toHaveValue("My work draft");
    await userEvent.click(screen.getByRole("button", { name: "Load latest log" }));
    await screen.findByText("Latest saved log");
    expect(screen.getByLabelText("What you worked on")).toHaveValue("My work draft");
    expect(ipc.calls.filter(call => String(call.args.path).startsWith("/employers/e1/logs/")).slice(-1)[0]?.args.refresh).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(ipc.apiCalls("PUT").slice(-1)[0]?.body).toEqual({ text: "My work draft", expected_revision: 20 });
    fireEvent.change(screen.getByLabelText("What you worked on"), { target: { value: "More work" } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(ipc.apiCalls("PUT").slice(-1)[0]?.body).toEqual({ text: "More work", expected_revision: 21 });
  } finally { visibility.mockRestore(); }
});

it("protects a new-day draft against another device creating that day first", async () => {
  ipc.api("GET", /^\/employers$/, () => ({ employers: [employer("e1", "Fernhill")] }));
  ipc.api("GET", /^\/employers\/e1\/logs$/, () => ({ logs: [], next_before: null }));
  ipc.api("PUT", /^\/employers\/e1\/logs\/[^/]+$/, () => { throw "This work log changed; reload before saving. (HTTP 409)"; });
  render(<ToastProvider><Work /></ToastProvider>);
  fireEvent.change(await screen.findByLabelText("What you worked on"), { target: { value: "My new-day draft" } });
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(ipc.apiCalls("PUT")[0].body).toEqual({ text: "My new-day draft", expected_revision: -1 });
  expect(screen.getByLabelText("What you worked on")).toHaveValue("My new-day draft");
  expect(await screen.findByText("This work log changed")).toBeVisible();
});
