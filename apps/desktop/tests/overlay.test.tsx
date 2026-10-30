// The overlay (docs/design/ui-proposal.md, "Overlay rules"): the newest question first and in
// full with the earlier ones, each pass's state in words, no empty bullet, the opacity fading
// the background only, and every behaviour kept: click-through and its way back, the menu,
// quit, the detailed answer on request, thumbs, copy, text size, compact, Ask and Answer.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit } from "@tauri-apps/api/event";
import { describe, expect, it } from "vitest";
import type { Numbered, UiEvent } from "../src/api";
import { answerLines } from "../src/AnswerText";
import Overlay, { ConversationView, passState } from "../src/Overlay";
import { fromReplay } from "../src/session";
import { ipc } from "./ipc";

let n = 0;
const ev = (event: UiEvent): Numbered => ({ n: n++, event });
const srv = (m: Record<string, unknown>) => ev({ kind: "server", message: { seq: n, ...m } as never });
const live = () => ev({ kind: "connection", state: { state: "live", session_id: "s1", resumed: false } });
const detected = (id: string, text: string, type = "behavioral") => srv({ type: "question.detected", question_id: id, text, question_type: type, confidence: 0.9, speculative: false });
const delta = (id: string, pass: "fast" | "deep", text: string) => srv({ type: "answer.delta", question_id: id, pass, text });
const done = (id: string, pass: "fast" | "deep") => srv({ type: "answer.done", question_id: id, pass, memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } });

const HOTKEYS = { toggle_overlay: "CommandOrControl+Shift+O", click_through: "CommandOrControl+Shift+T", click_through_off: "CommandOrControl+Shift+G", opacity_up: "CommandOrControl+Shift+Up", opacity_down: "CommandOrControl+Shift+Down" };

function start(events: Numbered[], running = true, prefs: Partial<{ opacity: number; compact: boolean; click_through: boolean }> = {}) {
  ipc.handle("snapshot", () => [events, running, n]);
  ipc.handle("overlay_prefs", () => ({ opacity: 1, compact: false, click_through: false, on_top: true, hotkeys: HOTKEYS, ...prefs }));
  return render(<Overlay />);
}

const twoQuestions = () => {
  n = 0;
  return [
    live(),
    detected("q1", "Tell me about an outage."),
    delta("q1", "fast", "- Froze deploys\n- Rolled back"),
    done("q1", "fast"),
    delta("q1", "deep", "- Paged at 18:05"),
    done("q1", "deep"),
    detected("q2", "How would you design alerting?", "other"),
    delta("q2", "fast", "- Alert on symptoms\n-"),
  ];
};

describe("the overlay's answers", () => {
  it("puts the newest question first, marked Now, with the earlier ones in full", async () => {
    start(twoQuestions());
    const cards = await screen.findAllByRole("article");
    expect(cards[0]).toHaveAccessibleName("Now: How would you design alerting?");
    expect(cards[1]).toHaveAccessibleName("Tell me about an outage.");
    expect(within(cards[1]).getByText("Froze deploys")).toBeInTheDocument();
    expect(within(cards[1]).getByText("Paged at 18:05")).toBeInTheDocument();
  });

  it("shows the text only: no Now badge, no question-type chip, no Done label", async () => {
    start([...twoQuestions(), done("q2", "fast")]);
    const now = (await screen.findAllByRole("article"))[0];
    expect(within(now).queryByText("Now")).not.toBeInTheDocument();
    expect(within(now).queryByText(/System design|Behavioral|Question/)).not.toBeInTheDocument();
    const status = now.querySelector(".pass-fast .pass-status") as HTMLElement;
    expect(status).toHaveTextContent("Done");
    expect(status).toHaveClass("ui-visually-hidden");
  });

  it("says each pass's state, with a caret while writing, and draws no empty bullet", async () => {
    start(twoQuestions());
    const now = (await screen.findAllByRole("article"))[0];
    const points = within(now).getByRole("group", { name: "Talking points" });
    expect(points).toHaveTextContent("Writing");
    expect(within(points).getAllByRole("listitem")).toHaveLength(1);
    expect(points.querySelector(".ov-caret")).not.toBeNull();
    await act(() => emit("copilot://event", done("q2", "fast")));
    await waitFor(() => expect(points).toHaveTextContent("Done"));
    expect(points.querySelector(".ov-caret")).toBeNull();
  });

  it("offers the detailed answer on request for a kind that does not start it", async () => {
    start(twoQuestions());
    const now = (await screen.findAllByRole("article"))[0];
    await userEvent.click(within(now).getByRole("button", { name: "Get the detailed answer ▾" }));
    expect(ipc.calls.find((c) => c.cmd === "expand_answer")?.args).toEqual({ questionId: "q2" });
    expect(within(now).getByRole("group", { name: "Detailed answer" })).toHaveTextContent("Queued");
  });

  it("thumbs are named, pressed, and sent", async () => {
    start(twoQuestions());
    const now = (await screen.findAllByRole("article"))[0];
    const up = within(now).getByRole("button", { name: "Helpful" });
    await userEvent.click(up);
    expect(up).toHaveAttribute("aria-pressed", "true");
    expect(ipc.calls.find((c) => c.cmd === "send_feedback")?.args).toMatchObject({ questionId: "q2", up: true });
  });

  it("scrolled down, a newer question offers New answer ↑ and does not move the view", async () => {
    start(twoQuestions());
    await screen.findAllByRole("article");
    const pane = screen.getByRole("region", { name: "Suggestions" }).querySelector(".ov-pane-body") as HTMLElement;
    pane.scrollTop = 200;
    await act(() => emit("copilot://event", detected("q3", "And on-call?")));
    expect(await screen.findByRole("button", { name: "New answer ↑" })).toBeInTheDocument();
    expect(pane.scrollTop).toBe(200);
  });
});

describe("the overlay's frame", () => {
  it("fades the background with the opacity setting, never the whole window", async () => {
    const { container } = start(twoQuestions(), true, { opacity: 0.35 });
    const root = container.querySelector(".ov") as HTMLElement;
    await waitFor(() => expect(root.style.getPropertyValue("--ov-alpha")).toBe("0.35"));
    expect(root.style.opacity).toBe("");
  });

  it("click-through says the way back, in readable keys", async () => {
    start(twoQuestions(), true, { click_through: true });
    expect(await screen.findByText(/Click-through on/)).toHaveTextContent("Click-through on · Ctrl+Shift+G turns it off");
  });

  it("the menu closes with Escape, back on the gear; quitting mid-call asks first", async () => {
    start(twoQuestions());
    await screen.findAllByRole("article");
    const gear = screen.getByRole("button", { name: "Overlay settings" });
    await userEvent.click(gear);
    const menu = screen.getByRole("dialog", { name: "Overlay settings" });
    expect(within(menu).getByRole("slider", { name: /Opacity/ })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Overlay settings" })).not.toBeInTheDocument();
    expect(gear).toHaveFocus();
    await userEvent.click(gear);
    await userEvent.click(screen.getByRole("button", { name: "Quit Copilot…" }));
    expect(ipc.calls.some((c) => c.cmd === "quit_app")).toBe(false);
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Quit Copilot" }));
    expect(ipc.calls.some((c) => c.cmd === "quit_app")).toBe(true);
  });

  it("not in a call, Quit quits at once", async () => {
    n = 0;
    start([], false);
    await userEvent.click(screen.getByRole("button", { name: "Overlay settings" }));
    await userEvent.click(screen.getByRole("button", { name: "Quit Copilot" }));
    expect(ipc.calls.some((c) => c.cmd === "quit_app")).toBe(true);
    expect(screen.getByText("No call running. Start one in the Copilot window.")).toBeInTheDocument();
  });

  it("keep on top and click-through are switches; click-through turns on only", async () => {
    start(twoQuestions());
    await userEvent.click(screen.getByRole("button", { name: "Overlay settings" }));
    await userEvent.click(screen.getByRole("switch", { name: "Keep on top" }));
    expect(ipc.calls.find((c) => c.cmd === "set_overlay_on_top")?.args).toEqual({ on: false });
    await userEvent.click(screen.getByRole("switch", { name: "Click-through" }));
    expect(ipc.calls.find((c) => c.cmd === "set_click_through")?.args).toEqual({ on: true });
  });

  it("text size steps with Ctrl+plus and A+, compact is remembered", async () => {
    start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.keyboard("{Control>}={/Control}");
    expect(localStorage.getItem("overlay.textScale")).toBe("1.1");
    await userEvent.click(screen.getByRole("button", { name: /Larger text/ }));
    expect(localStorage.getItem("overlay.textScale")).toBe("1.25");
    await userEvent.click(screen.getByRole("button", { name: "Compact: answers only" }));
    expect(ipc.calls.find((c) => c.cmd === "set_overlay_view")?.args).toEqual({ compact: true });
    expect(screen.queryByRole("region", { name: "Transcript" })).not.toBeInTheDocument();
  });

  it("asks a typed question with Enter, and Answer asks for a reply", async () => {
    start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.type(screen.getByRole("textbox", { name: "Question" }), "What about cost?{Enter}");
    expect(ipc.calls.find((c) => c.cmd === "force_question")?.args).toEqual({ text: "What about cost?" });
    await userEvent.click(screen.getByRole("button", { name: "Answer" }));
    expect(ipc.calls.find((c) => c.cmd === "respond")?.args).toEqual({ text: null });
  });

  it("has no axe violations", async () => {
    start(twoQuestions());
    await screen.findAllByRole("article");
    const axe = (await import("axe-core")).default;
    const r = await axe.run(document.body, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } });
    expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((x) => x.target.join(" ")).join(", ")}`)).toEqual([]);
  });
});

describe("a replay (History)", () => {
  it("reads in the order the call went, with no Now", () => {
    const s = fromReplay({
      session_id: "s1",
      stored: true,
      started_at: null,
      segments: [],
      questions: [
        { id: "a", text: "First?", type: "technical", forced: false, offset_ms: 1000, feedback: "up", fast: { text: "- one", source: "model", refs: [], expanded: false }, deep: null },
        { id: "b", text: "Second?", type: "technical", forced: false, offset_ms: 2000, feedback: null, fast: { text: "- two", source: "model", refs: [], expanded: false }, deep: null },
      ],
    });
    render(<ConversationView s={s} compact={false} layout="split" textScale={1} live={false} ratings={{ a: "up", b: null }} emptyAnswers="" emptyTranscript="" showMe />);
    const cards = screen.getAllByRole("article");
    expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["First?", "Second?"]);
    expect(within(cards[0]).getByRole("button", { name: "Helpful" })).toHaveAttribute("aria-pressed", "true");
    expect(within(cards[0]).getByRole("button", { name: "Helpful" })).toBeDisabled();
  });
});

describe("helpers", () => {
  it("states in words", () => {
    const p = { text: "", done: false, error: null, source: null, refs: [] };
    expect(passState(p, "fast").text).toBe("Thinking");
    expect(passState(p, "deep").text).toBe("Queued");
    expect(passState({ ...p, text: "x" }, "fast").text).toBe("Writing");
    expect(passState({ ...p, done: true, source: "prepared" }, "fast").text).toBe("Done · prepared");
    expect(passState({ ...p, error: "cancelled" }, "deep").text).toBe("Stopped for a newer question");
    expect(passState({ ...p, error: "timeout" }, "deep").text).toBe("Failed (timeout)");
  });
  it("a bullet waits for its first word", () => {
    expect(answerLines("- one\n-")).toEqual(["- one"]);
    expect(answerLines("- one\n- ")).toEqual(["- one"]);
    expect(answerLines("- one\n- t")).toEqual(["- one", "- t"]);
  });
});
