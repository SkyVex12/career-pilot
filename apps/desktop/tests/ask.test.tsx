import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { Work } from "../src/Work";
import { ToastProvider } from "../src/ui";
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

it("keeps a draft safe while a saved conversation is loading", async () => {
  let finish!: (value: unknown) => void;
  const conversation = { id: "c", title: "Saved conversation", scope: { kind: "auto" }, messages: [] };
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [conversation] }));
  ipc.api("GET", /^\/ask\/conversations\/c$/, () => new Promise(resolve => { finish = resolve; }));
  render(<Ask />);
  await screen.findByText("Saved conversation", { selector: "option" });
  fireEvent.change(screen.getByLabelText("Saved conversations", { selector: "select" }), { target: { value: "c" } });
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "Keep this draft" } });
  expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  fireEvent.keyDown(screen.getByLabelText("Ask a question"), { key: "Enter", ctrlKey: true });
  expect(ipc.apiCalls("POST")).toHaveLength(0);
  await act(async () => finish(conversation));
  expect(screen.getByLabelText("Ask a question")).toHaveValue("Keep this draft");
  expect(screen.getByRole("button", { name: "Ask" })).toBeEnabled();
});


it("shows an accepted question and reconnects after the first answer read fails", async () => {
  const conversation = { id: "receipt", title: "DNS", scope: { kind: "general" } };
  const message = { id: "m", request_id: "r", status: "pending", question: "Explain DNS", answer: "", sources: [] };
  let reads = 0;
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [] }));
  ipc.api("POST", /^\/ask\/conversations$/, () => conversation);
  ipc.api("POST", /^\/ask\/conversations\/receipt\/messages$/, () => message);
  ipc.api("GET", /^\/ask\/conversations\/receipt$/, () => {
    if (++reads === 1) throw new Error("Temporary outage");
    return { ...conversation, messages: [{ ...message, status: "done", answer: "DNS maps names to addresses." }] };
  });
  render(<Ask />);
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: message.question } });
  fireEvent.keyDown(screen.getByLabelText("Ask a question"), { key: "Enter", ctrlKey: true });
  expect(await screen.findByText(message.question, { selector: "h3" })).toBeVisible();
  expect(await screen.findByText("Answer connection interrupted")).toBeVisible();
  expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  expect(await screen.findByText("DNS maps names to addresses.", {}, { timeout: 3000 })).toBeVisible();
  expect(screen.queryByText("Answer connection interrupted")).not.toBeInTheDocument();
  expect(ipc.apiCalls("POST").filter(call => call.path.endsWith("/messages"))).toHaveLength(1);
});

it("never overlaps pending answer reads when the server is slow", async () => {
  vi.useFakeTimers();
  try {
    const conversation = { id: "slow", title: "Slow", scope: { kind: "auto" } };
    const message = { id: "m", request_id: "r", status: "pending", question: "Explain DNS", answer: "", sources: [] };
    let resolveRead!: (value: unknown) => void;
    ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [] }));
    ipc.api("POST", /^\/ask\/conversations$/, () => conversation);
    ipc.api("POST", /^\/ask\/conversations\/slow\/messages$/, () => message);
    ipc.api("GET", /^\/ask\/conversations\/slow$/, () => new Promise(resolve => { resolveRead = resolve; }));
    const view = render(<Ask />);
    fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: message.question } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Ask" })));
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(ipc.apiCalls("GET").filter(call => call.path === "/ask/conversations/slow")).toHaveLength(1);
    await act(async () => resolveRead({ ...conversation, messages: [{ ...message, status: "done", answer: "Complete answer" }] }));
    expect(screen.getByText("Complete answer")).toBeVisible();
    view.unmount();
  } finally { vi.useRealTimers(); }
});

it("starts a distinct request when the retry context changes", async () => {
  const ids: string[] = [];
  const conversation = { id: "scoped", title: "Draft", scope: { kind: "auto" } };
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [] }));
  ipc.api("POST", /^\/ask\/conversations$/, () => conversation);
  ipc.api("POST", /^\/ask\/conversations\/scoped\/messages$/, (_path, value) => {
    const body = value as { request_id: string; scope: { kind: string } };
    ids.push(body.request_id);
    if (ids.length === 1) throw new Error("Context temporarily unavailable");
    expect(body.scope.kind).toBe("general");
    return { id: "done", request_id: body.request_id, question: "Explain DNS", answer: "A general answer", status: "done", sources: [] };
  });
  render(<Ask />);
  fireEvent.change(screen.getByLabelText("Ask a question"), { target: { value: "Explain DNS" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await screen.findByText(/Context temporarily unavailable/);
  fireEvent.click(screen.getByText("Context", { selector: "summary span" }));
  fireEvent.click(screen.getByRole("combobox", { name: "Context" }));
  fireEvent.click(screen.getByRole("option", { name: "General knowledge" }));
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(await screen.findByText("A general answer")).toBeVisible();
  expect(ids).toHaveLength(2);
  expect(ids[0]).not.toBe(ids[1]);
});

it("opens general Ask when loading company records fails", async () => {
  ipc.api("GET", /^\/employers$/, () => { throw new Error("Company lookup unavailable"); });
  ipc.api("GET", /^\/ask\/conversations$/, () => ({ conversations: [] }));
  render(<ToastProvider><Work /></ToastProvider>);
  await screen.findByText("Could not load your companies");
  fireEvent.click(screen.getByRole("button", { name: "Ask a question" }));
  expect(await screen.findByLabelText("Ask a question", { selector: "textarea" })).toBeVisible();
});
