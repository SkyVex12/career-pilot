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
  const events = [message({ type: "question.detected", question_id: "q", text: "How would you implement the retry?", question_type: "other", confidence: 1, speculative: false, answer_strategy: strategy }), ...chunks()];
  ipc.handle("snapshot", () => [events, true, n]);
  ipc.handle("overlay_prefs", () => ({ opacity: 1, compact: false, click_through: false, on_top: true, hotkeys: { toggle_overlay: "", click_through: "", click_through_off: "", opacity_up: "", opacity_down: "" } }));
  return render(<Overlay />);
}

const code = "if ready:\n    send()\nlog()";
const text = "~~~python\n" + code + "\n~~~";

describe("tilde fences in spoken answers", () => {
  it.each(["legacy_dual", "adaptive", "adaptive_on_demand", "adaptive_hybrid"])("renders a complete %s primary code block", async strategy => {
    start(strategy, () => [delta("fast", text), done("fast")]);
    const answer = await screen.findByRole("group", { name: strategy === "legacy_dual" ? "Talking points" : "Answer" });
    expect(answer.querySelector("pre code")?.textContent).toBe(code);
    expect(within(answer).getByRole("button", { name: "Copy the python code" })).toBeEnabled();
  });
  it("renders legacy detail code without altering indentation", async () => {
    start("legacy_dual", () => [delta("fast", "I would retry only transient failures."), done("fast"), delta("deep", text), done("deep")]);
    const answer = await screen.findByRole("group", { name: "Detailed answer" });
    expect(answer.querySelector("pre code")?.textContent).toBe(code);
  });
  it("keeps an unfinished adaptive block uncopyable until the closing fence arrives", async () => {
    start("adaptive", () => [delta("fast", "~~~python\n" + code)]);
    const copy = await screen.findByRole("button", { name: "Copy the python code" });
    expect(copy).toBeDisabled();
    await act(() => emit("copilot://event", delta("fast", "\n~~~", 1)));
    expect(copy).toBeEnabled();
    expect(screen.getByRole("group", { name: "Answer" }).querySelector("pre code")?.textContent).toBe(code);
  });
  it("shows a list-indented expert block only after explicit review", async () => {
    const expert = "1. Example\n   ~~~python\n   " + code.split("\n").join("\n   ") + "\n   ~~~";
    start("adaptive_hybrid", () => [delta("fast", "I would retry only transient failures."), done("fast"), delta("deep", expert), done("deep")]);
    expect(await screen.findByRole("group", { name: "Answer" })).toHaveTextContent("transient failures");
    expect(screen.queryByRole("button", { name: "Copy the python code" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review expert response" }));
    expect(screen.getByRole("group", { name: "Expert response" }).querySelector("pre code")?.textContent).toBe(code);
  });
});
