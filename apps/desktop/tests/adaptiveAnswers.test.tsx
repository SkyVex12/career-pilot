import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit } from "@tauri-apps/api/event";
import { describe, expect, it } from "vitest";
import type { Numbered } from "../src/api";
import Overlay from "../src/Overlay";
import { ipc } from "./ipc";

let n = 0;
const message = (m: Record<string, unknown>): Numbered => ({ n: n++, event: { kind: "server", message: { seq: n, ...m } as never } });
const delta = (pass: "fast" | "deep", text: string, sequence = 0) => message({ type: "answer.delta", question_id: "q", pass, text, sequence });
const done = (pass: "fast" | "deep", count = 1) => message({ type: "answer.done", question_id: "q", pass, delta_count: count, memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } });
function start(strategy: string, chunks: (() => Numbered[]) = () => []) {
  n = 0;
  const events = [message({ type: "question.detected", question_id: "q", text: "Where are you based?", question_type: "other", confidence: 1, speculative: false, answer_strategy: strategy }), ...chunks()];
  ipc.handle("snapshot", () => [events, true, n]);
  ipc.handle("overlay_prefs", () => ({ opacity: 1, compact: false, click_through: false, on_top: true, hotkeys: { toggle_overlay: "", click_through: "", click_through_off: "", opacity_up: "", opacity_down: "" } }));
  return render(<Overlay />);
}

describe("adaptive answers", () => {
  it("streams one reading surface and keeps a short question short", async () => {
    start("adaptive", () => [delta("fast", "I am based in Austin.")]);
    const answer = await screen.findByRole("group", { name: "Answer" });
    expect(answer).toHaveTextContent("I am based in Austin.");
    expect(screen.queryByRole("group", { name: "Talking points" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Detailed answer" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /expert response/ })).not.toBeInTheDocument();
    await act(() => emit("copilot://event", done("fast")));
    expect(answer.querySelector(".ov-caret")).toBeNull();
  });

  it("reviews a completed independent response only on request and preserves the original", async () => {
    start("adaptive_on_demand", () => [delta("fast", "I am based in Austin."), done("fast")]);
    await userEvent.click(await screen.findByRole("button", { name: "Get an expert response" }));
    expect(ipc.calls.filter(c => c.cmd === "expand_answer")).toEqual([{ cmd: "expand_answer", args: { questionId: "q" } }]);
    await act(() => emit("copilot://event", delta("deep", "My profile does not list a location.")));
    expect(screen.getByRole("group", { name: "Answer" })).toHaveTextContent("Austin");
    expect(screen.queryByText("My profile does not list a location.")).not.toBeInTheDocument();
    await act(() => emit("copilot://event", done("deep")));
    await userEvent.click(screen.getByRole("button", { name: "Review expert response" }));
    expect(within(screen.getByRole("group", { name: "Expert response" })).getByText("My profile does not list a location.")).toBeVisible();
    expect(screen.queryByText("I am based in Austin.")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Return to original answer" }));
    expect(screen.getByRole("group", { name: "Answer" })).toHaveTextContent("Austin");
  });

  it("does not offer a literal duplicate as additional content", async () => {
    start("adaptive_hybrid", () => [delta("fast", "I am based in Austin."), done("fast"), delta("deep", "I am based in Austin."), done("deep")]);
    expect(await screen.findByRole("group", { name: "Answer" })).toHaveTextContent("Austin");
    expect(screen.queryByRole("button", { name: "Review expert response" })).not.toBeInTheDocument();
    expect(screen.getAllByText("I am based in Austin.")).toHaveLength(1);
  });

  it("preserves the useful primary answer when the expert fails", async () => {
    start("adaptive_hybrid", () => [delta("fast", "I am based in Austin."), done("fast"), message({ type: "answer.error", question_id: "q", pass: "deep", code: "timeout" })]);
    expect(await screen.findByRole("group", { name: "Answer" })).toHaveTextContent("Austin");
    expect(screen.getByText("Expert response unavailable. The original answer is preserved.")).toBeVisible();
  });
  it.each([
    ["Python indentation", "if ready:\n    send()\nlog()", "if ready:\n    send()\n    log()"],
    ["string literal spacing", 'separator = "a b"', 'separator = "a  b"'],
  ])("keeps expert changes to %s available for explicit review", async (_name, primary, expert) => {
    const fence = "`".repeat(3);
    const fenced = (body: string) => `${fence}python\n${body}\n${fence}`;
    start("adaptive_hybrid", () => [delta("fast", fenced(primary)), done("fast"), delta("deep", fenced(expert)), done("deep")]);
    const original = await screen.findByRole("group", { name: "Answer" });
    expect(original.querySelector("code")?.textContent).toBe(primary);
    await userEvent.click(screen.getByRole("button", { name: "Review expert response" }));
    expect(screen.getByRole("group", { name: "Expert response" }).querySelector("code")?.textContent).toBe(expert);
    await userEvent.click(screen.getByRole("button", { name: "Return to original answer" }));
    expect(screen.getByRole("group", { name: "Answer" }).querySelector("code")?.textContent).toBe(primary);
  });

});
