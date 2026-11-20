import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { Ask } from "../src/Ask";
import { ipc } from "./ipc";

it("keeps the latest conversation when earlier loads and sources arrive late", async () => {
  const first = { id: "a", title: "First", scope: { kind: "auto" }, messages: [{ id: "m", status: "done", question: "Earlier question", answer: "Earlier answer", sources: [{ id: "s", kind: "transcript", label: "Earlier source" }] }] };
  const second = { id: "b", title: "Second", scope: { kind: "auto" }, messages: [] };
  let resolveFirst!: (v: unknown) => void, resolveSource!: (v: unknown) => void;
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [first, second] }));
  ipc.api("GET", /^\/ask\/conversations\/a$/, () => new Promise(resolve => { resolveFirst = resolve; }));
  ipc.api("GET", /^\/ask\/conversations\/b$/, () => second);
  ipc.api("GET", /^\/ask\/sources\/transcript\/s$/, () => new Promise(resolve => { resolveSource = resolve; }));
  render(<Ask />);
  await screen.findByText("First", { selector: "option" });
  fireEvent.change(screen.getByLabelText("Saved conversations", { selector: "select" }), { target: { value: "a" } });
  fireEvent.change(screen.getByLabelText("Saved conversations", { selector: "select" }), { target: { value: "b" } });
  await waitFor(() => expect(screen.getByLabelText("Saved conversations", { selector: "select" })).toHaveValue("b"));
  await act(async () => resolveFirst(first));
  expect(screen.getByLabelText("Saved conversations", { selector: "select" })).toHaveValue("b");
  fireEvent.change(screen.getByLabelText("Saved conversations", { selector: "select" }), { target: { value: "a" } });
  await act(async () => resolveFirst(first));
  fireEvent.click(await screen.findByRole("button", { name: "Earlier source" }));
  fireEvent.change(screen.getByLabelText("Saved conversations", { selector: "select" }), { target: { value: "b" } });
  await waitFor(() => expect(screen.getByLabelText("Saved conversations", { selector: "select" })).toHaveValue("b"));
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "New draft" } });
  await act(async () => resolveSource({ content: "Stale private excerpt" }));
  expect(screen.queryByText("Stale private excerpt")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Ask a question")).toHaveValue("New draft");
});

it("does not erase a newer draft when a send finishes", async () => {
  const conversation = { id: "c", title: "Current", scope: { kind: "auto" }, messages: [] };
  let resolveSend!: (v: unknown) => void;
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [] }));
  ipc.api("POST", /^\/ask\/conversations$/, () => conversation);
  ipc.api("GET", /^\/ask\/conversations\/c$/, () => conversation);
  ipc.api("POST", /^\/ask\/conversations\/c\/messages$/, () => new Promise(resolve => { resolveSend = resolve; }));
  render(<Ask />);
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "First draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await waitFor(() => expect(resolveSend).toBeDefined());
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "Next question" } });
  await act(async () => resolveSend({ id: "m", status: "done", sources: [] }));
  expect(screen.getByLabelText("Ask a question")).toHaveValue("Next question");
});

it("asks without a call and retains a draft while inspecting a cited transcript", async () => {
  let messages: unknown[] = [];
  const conversation = { id: "c1", title: "Preparation", scope: { kind: "auto" } };
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [conversation] }));
  ipc.api("POST", /^\/ask\/conversations$/, () => conversation);
  ipc.api("GET", /^\/ask\/conversations\/c1$/, () => ({ ...conversation, messages }));
  ipc.api("POST", /^\/ask\/conversations\/c1\/messages$/, (_p, body) => {
    const b = body as { question: string; request_id: string };
    const message = { id: "m1", request_id: b.request_id, question: b.question, answer: "You chose a queue [1].", status: "done", sources: [{ id: "s1:them:1", kind: "transcript", label: "Design meeting", start_ms: 3200 }] };
    messages = [message]; return message;
  });
  ipc.api("GET", /^\/ask\/sources\/transcript\/s1%3Athem%3A1$/, () => ({ content: "We chose a queue.", start_ms: 3200 }));
  ipc.api("GET", /^\/sessions\/s1$/, () => ({id:"s1",status:"ready",mode:"interview",store_transcript:true,questions:0,notes:"",outputs:{summary:"Saved summary"}}));
  ipc.api("GET", /^\/sessions\/s1\/replay$/, () => ({session_id:"s1",stored:true,started_at:null,questions:[],segments:[{channel:"them",seg_id:1,start_ms:3200,end_ms:4200,text:"The original supporting passage"}]}));
  render(<Ask />);
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "What did we decide?" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(await screen.findByText("You chose a queue [1].")).toBeVisible();
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "What were the tradeoffs?" } });
  fireEvent.click(screen.getByRole("button", { name: "Design meeting" }));
  expect(await screen.findByText("We chose a queue.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Open saved call at this passage" }));
  expect((await screen.findByText("The original supporting passage")).tagName).toBe("MARK");
  fireEvent.click(screen.getByRole("button", { name: "Back to conversation" }));
  expect(screen.getByLabelText("Ask a question")).toHaveValue("What were the tradeoffs?");
  expect(ipc.calls.some(call => call.cmd === "start_session")).toBe(false);
});
it("preserves a failed send's request ID and draft for an idempotent retry", async () => {
  const ids: string[] = [];
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [] }));
  ipc.api("POST", /^\/ask\/conversations$/, () => ({ id: "c1", title: "Draft", scope: { kind: "general" } }));
  ipc.api("POST", /^\/ask\/conversations\/c1\/messages$/, (_p, body) => { ids.push((body as { request_id: string }).request_id); throw new Error("Network unavailable"); });
  render(<Ask />);
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "Explain queues" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await waitFor(() => expect(ids).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole("button", { name: "Ask" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await waitFor(() => expect(ids).toHaveLength(2)); expect(ids[0]).toBe(ids[1]);
  expect(screen.getByLabelText("Ask a question")).toHaveValue("Explain queues");
});
