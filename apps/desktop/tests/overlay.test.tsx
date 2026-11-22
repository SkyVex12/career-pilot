// The overlay (docs/design/ui-proposal.md, "Overlay rules"): the newest question first and in
// full with the earlier ones, each pass's state in words, no empty bullet, the opacity fading
// the background only, and every behaviour kept: click-through and its way back, the menu,
// quit, the detailed answer on request, thumbs, copy, text size, compact, Ask and Answer.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit } from "@tauri-apps/api/event";
import { mockWindows } from "@tauri-apps/api/mocks";
import { describe, expect, it, vi } from "vitest";
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
  it("keeps the live transcript and answers visible together, with screen tools on demand", async () => {
    start([...twoQuestions(), srv({ type: "transcript.partial", channel: "them", seg_id: 0, text: "What would you monitor?" })]);
    const answers = await screen.findByRole("region", { name: "Suggestions" });
    const transcript = screen.getByRole("region", { name: "Transcript" });
    expect(within(transcript).getByText("What would you monitor?")).toBeVisible();
    expect(within(answers).getAllByRole("article")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /^(Answers|Transcript)$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Solve" })).not.toBeInTheDocument();
    await act(() => emit("copilot://event", srv({ type: "transcript.final", channel: "them", seg_id: 0, text: "What would you monitor first?", start_ms: 0, end_ms: 1500 })));
    await act(() => emit("copilot://event", delta("q2", "fast", " Track latency")));
    expect(within(transcript).getByText("What would you monitor first?")).toBeVisible();
    expect(within(answers).getByText("Track latency")).toBeVisible();
    await userEvent.click(screen.getByRole("tab", { name: "Screen tools" }));
    expect(screen.getByRole("button", { name: "Solve" })).toBeVisible();
  });

  it("links a transcript question to its visible answer without changing the layout", async () => {
    n = 0;
    start([
      live(),
      srv({ type: "transcript.final", channel: "them", seg_id: 0, text: "How did you recover?", start_ms: 0, end_ms: 1500 }),
      detected("recovery", "How did you recover?"),
      delta("recovery", "fast", "- Rolled back"),
    ]);
    const answer = await screen.findByRole("article");
    const scroll = vi.spyOn(answer, "scrollIntoView");
    await userEvent.click(within(screen.getByRole("region", { name: "Transcript" })).getByRole("button", { name: "Suggestion →" }));
    expect(scroll).toHaveBeenCalledWith({ block: "start", behavior: "smooth" });
    expect(screen.getByRole("region", { name: "Transcript" })).toBeVisible();
    expect(answer).toBeVisible();
    scroll.mockRestore();
  });

  it("remembers a combined one-column layout and can return to simultaneous split panes", async () => {
    const first = start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    await userEvent.click(screen.getByRole("combobox", { name: "Layout" }));
    await userEvent.click(screen.getByRole("option", { name: "Stacked" }));
    expect(localStorage.getItem("overlay.layout")).toBe("stacked");
    expect(screen.getByRole("region", { name: "Conversation" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Current suggestion" })).toBeVisible();
    first.unmount();
    start(twoQuestions());
    await screen.findByRole("region", { name: "Current suggestion" });
    expect(screen.getByRole("region", { name: "Conversation" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    await userEvent.click(screen.getByRole("combobox", { name: "Layout" }));
    await userEvent.click(screen.getByRole("option", { name: "Split" }));
    expect(screen.getByRole("region", { name: "Transcript" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Suggestions" })).toBeVisible();
    expect(screen.getAllByRole("article")).toHaveLength(2);
  });

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
  it("asks before ending a host call and keeps the call intact when cancelled", async () => {
    start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "End call" }));
    const dialog = screen.getByRole("alertdialog", { name: "End this call?" });
    expect(ipc.calls.some((c) => c.cmd === "end_session")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Keep talking" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(ipc.calls.some((c) => c.cmd === "end_session" || c.cmd === "plugin:window|hide")).toBe(false);
  });

  it.each([true, false])("ends a host call with summarize=%s only after confirmation", async (summarize) => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ id: "s1", mode: "interview", store_transcript: true }));
    let finish!: () => void;
    ipc.handle("end_session", () => new Promise<void>((resolve) => { finish = resolve; }));
    start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "End call" }));
    const dialog = screen.getByRole("alertdialog");
    const summary = within(dialog).getByRole("checkbox", { name: "Create a summary" });
    expect(summary).toBeChecked();
    if (!summarize) await userEvent.click(summary);
    await userEvent.click(within(dialog).getByRole("button", { name: "End call" }));
    expect(ipc.calls.filter((c) => c.cmd === "end_session")).toEqual([{ cmd: "end_session", args: { summarize } }]);
    for (const button of within(dialog).getAllByRole("button")) expect(button).toBeDisabled();
    expect(dialog).toBeVisible();
    await act(async () => finish());
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(ipc.calls.some((c) => c.cmd === "plugin:window|hide" || c.cmd === "open_main_call")).toBe(false);
  });

  it.each([true, false])("keeps a failed end attempt visible and retryable with summarize=%s", async (summarize) => {
    ipc.handle("end_session", () => { throw "Could not end the call. Try again."; });
    start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "End call" }));
    const dialog = screen.getByRole("alertdialog");
    if (!summarize) await userEvent.click(within(dialog).getByRole("checkbox", { name: "Create a summary" }));
    const action = within(dialog).getByRole("button", { name: "End call" });
    await userEvent.click(action);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Could not end the call. Try again.");
    expect(action).toBeEnabled();
    expect(ipc.calls.some((c) => c.cmd === "plugin:window|hide" || c.cmd === "open_main_call")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Keep talking" }));
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Answer" })).toBeEnabled();
  });

  it("uses the standup and transcript choices saved for the active call", async () => {
    ipc.api("GET", /^\/sessions\/s1$/, () => ({ id: "s1", mode: "standup", store_transcript: false }));
    start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "End call" }));
    const dialog = screen.getByRole("alertdialog");
    await within(dialog).findByText(/The summary keeps your standup report and commitments/);
    expect(dialog).toHaveTextContent("The transcript is not stored");
    expect(within(dialog).getByRole("button", { name: "End call" })).toBeEnabled();
  });

  it("returns to setup from an inactive overlay without ending a call", async () => {
    n = 0;
    start([], false);
    await userEvent.click(screen.getByRole("button", { name: "Return to call setup" }));
    expect(ipc.calls.some((c) => c.cmd === "open_main_call")).toBe(true);
    expect(ipc.calls.some((c) => c.cmd === "end_session")).toBe(false);
  });

  it("dismisses stale end confirmations when the call stops or changes", async () => {
    start(twoQuestions());
    await screen.findAllByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "End call" }));
    await act(() => emit("copilot://event", ev({ kind: "stopped", reason: "ended" })));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await act(() => emit("copilot://event", ev({ kind: "connection", state: { state: "live", session_id: "s2", resumed: false } })));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "End call" }));
    await act(() => emit("copilot://event", ev({ kind: "connection", state: { state: "live", session_id: "s3", resumed: false } })));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(ipc.calls.some((c) => c.cmd === "end_session")).toBe(false);
  });

  it("lets guests hide their overlay without offering to end the host's call", async () => {
    mockWindows("overlay");
    n = 0;
    start([srv({ type: "seat.ready", session_id: "s1", seat_id: "guest-1", host_name: "Alex", mode: "interview", title: "Acme onsite", capabilities: ["view", "note"], host_connected: true, sharing: true })]);
    const hide = await screen.findByRole("button", { name: "Hide overlay" });
    expect(screen.queryByRole("button", { name: "End call" })).not.toBeInTheDocument();
    await userEvent.click(hide);
    expect(ipc.calls.some((c) => c.cmd === "plugin:window|hide")).toBe(true);
    expect(ipc.calls.some((c) => c.cmd === "end_session")).toBe(false);
  });

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
    const gear = screen.getByRole("button", { name: "Quick settings" });
    await userEvent.click(gear);
    const menu = screen.getByRole("dialog", { name: "Quick settings" });
    expect(within(menu).getByRole("slider", { name: /Transparency/ })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
    expect(gear).toHaveFocus();
    await userEvent.click(gear);
    await userEvent.click(screen.getByRole("button", { name: "Quit CoRuby" }));
    expect(ipc.calls.some((c) => c.cmd === "quit_app")).toBe(false);
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Quit CoRuby" }));
    expect(ipc.calls.some((c) => c.cmd === "quit_app")).toBe(true);
  });

  it("not in a call, Quit quits at once", async () => {
    n = 0;
    start([], false);
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    await userEvent.click(screen.getByRole("button", { name: "Quit CoRuby" }));
    expect(ipc.calls.some((c) => c.cmd === "quit_app")).toBe(true);
    expect(screen.getByText("No call running. Start one in the main window.")).toBeInTheDocument();
  });

  it("keep on top and click-through are switches; click-through turns on only", async () => {
    start(twoQuestions());
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
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
