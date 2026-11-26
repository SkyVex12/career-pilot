import { act, render, screen, within } from "@testing-library/react";
import { emit } from "@tauri-apps/api/event";
import { describe, expect, it, vi } from "vitest";
import type { Numbered } from "../src/api";
import { AnswerText } from "../src/AnswerText";
import { CodeAnswer } from "../src/CodeAnswer";
import Overlay from "../src/Overlay";
import { ipc } from "./ipc";

vi.mock("../src/AnswerText", { spy: true });
vi.mock("../src/CodeAnswer", { spy: true });
let n = 0;
const message = (m: Record<string, unknown>): Numbered => ({ n: n++, epoch: 1, event: { kind: "server", message: { seq: n, ...m } as never } });
const detected = (id: string, text = id, speculative = false) => message({ type: "question.detected", question_id: id, text, question_type: "technical", confidence: 1, speculative, answer_strategy: "adaptive_on_demand" });
const delta = (id: string, text: string, sequence = 0) => message({ type: "answer.delta", question_id: id, pass: "fast", text, sequence });
const done = (id: string, count = 1) => message({ type: "answer.done", question_id: id, pass: "fast", delta_count: count, memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } });
function start() {
  n = 0;
  const events = [detected("old", "Earlier answer"), delta("old", "An earlier grounded answer."), done("old"), detected("code", "Earlier coding answer"), delta("code", "```python\nprint(1)\n```"), done("code"), detected("live", "Why Redis?", true), delta("live", "I chose Redis")];
  ipc.handle("snapshot", () => [events, true, n, 1]);
  ipc.handle("overlay_prefs", () => ({ opacity: 1, compact: true, click_through: false, on_top: true, hotkeys: {} }));
  return render(<Overlay />);
}

describe("stream rendering", () => {
  it("commits the next useful sentence without reparsing completed prose or historical code", async () => {
    start();
    await screen.findByRole("article", { name: "Now: Why Redis?" });
    vi.mocked(AnswerText).mockClear();
    vi.mocked(CodeAnswer).mockClear();
    await act(() => emit("copilot://event", delta("live", " for shared expiring cache entries.", 1)));
    expect(within(screen.getByRole("article", { name: "Now: Why Redis?" })).getByRole("group", { name: "Answer" })).toHaveTextContent("I chose Redis for shared expiring cache entries.");
    expect(vi.mocked(AnswerText).mock.calls.length).toBeGreaterThan(0);
    expect(vi.mocked(AnswerText).mock.calls.every(([props]) => props.text.startsWith("I chose Redis"))).toBe(true);
    expect(CodeAnswer).not.toHaveBeenCalled();
  });

  it("still applies transcript confirmation, new-question promotion, and terminal cancellation", async () => {
    start();
    const current = await screen.findByRole("article", { name: "Now: Why Redis?" });
    await act(() => emit("copilot://event", detected("live", "Why did you choose Redis?")));
    expect(current).toHaveAccessibleName("Now: Why did you choose Redis?");
    expect(current).not.toHaveClass("is-speculative");
    await act(() => emit("copilot://event", detected("next", "How do you handle cache misses?")));
    expect(current).toHaveClass("is-earlier");
    await act(() => emit("copilot://event", delta("next", "I fall back to the database.")));
    await act(() => emit("copilot://event", message({ type: "question.cancelled", question_id: "live", reason: "superseded" })));
    await act(() => emit("copilot://event", delta("live", " Stale text must not return.", 1)));
    await act(() => emit("copilot://event", done("live", 2)));
    expect(screen.queryByText(/Stale text/)).not.toBeInTheDocument();
    expect(screen.queryByRole("article", { name: /Why did you choose Redis/ })).not.toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Now: How do you handle cache misses?" })).toHaveTextContent("I fall back to the database.");
  });

  it("keeps one sentence when a late duplicate chunk follows an out-of-order delivery", async () => {
    start();
    await screen.findByRole("article", { name: "Now: Why Redis?" });
    await act(() => emit("copilot://event", delta("live", " entries.", 2)));
    expect(screen.getByRole("article", { name: "Now: Why Redis?" })).not.toHaveTextContent("entries.");
    await act(() => emit("copilot://event", delta("live", " for expiring", 1)));
    await act(() => emit("copilot://event", delta("live", " for expiring", 1)));
    await act(() => emit("copilot://event", done("live", 3)));
    const answer = within(screen.getByRole("article", { name: "Now: Why Redis?" })).getByRole("group", { name: "Answer" });
    expect(answer).toHaveTextContent("I chose Redis for expiring entries.");
    expect(answer.textContent?.match(/for expiring/g)).toHaveLength(1);
    expect(answer.querySelector(".ov-caret")).toBeNull();
  });
});


it("stopping a complete adaptive answer does not report an unrequested expert as unavailable", async () => {
  start();
  await screen.findByRole("article", { name: "Now: Why Redis?" });
  await act(() => emit("copilot://event", done("live")));
  await act(() => emit("copilot://event", { epoch: 1, n: n++, event: { kind: "stopped", reason: "ended" } } satisfies Numbered));
  const answer = within(screen.getByRole("article", { name: "Now: Why Redis?" })).getByRole("group", { name: "Answer" });
  expect(answer).toHaveTextContent("I chose Redis");
  expect(answer.querySelector(".ov-caret")).toBeNull();
  expect(screen.queryByText(/Expert response unavailable/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Preparing expert response/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Stopped for a newer question/)).not.toBeInTheDocument();
});
