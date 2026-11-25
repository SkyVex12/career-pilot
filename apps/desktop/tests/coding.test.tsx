// Coding mode on the overlay (ARCHITECTURE.md, "Coding mode"): answers with code drawn and
// copied exactly, a screenshot's card (its tasks, its answer, its code, no "detailed answer"
// button), and the bar that takes, shows, removes and sends shots.
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit } from "@tauri-apps/api/event";
import { describe, expect, it, vi } from "vitest";
import type { Numbered, ShotView, UiEvent } from "../src/api";
import { CodeAnswer, inline, languageOf, splitBlocks } from "../src/CodeAnswer";
import { CodingBar } from "../src/CodingBar";
import Overlay from "../src/Overlay";
import { ipc } from "./ipc";

const CODE = "**Approach:** two pointers.\n```python\ndef f(a):\n\n    return a[n:]  # keep\n```\n**Complexity:** O(n)";

describe("answers with code", () => {
  it("splits text from fenced code, and an unclosed fence is a block still streaming", () => {
    expect(splitBlocks(CODE)).toEqual([
      { kind: "text", lines: ["**Approach:** two pointers."] },
      { kind: "code", lang: "python", code: "def f(a):\n\n    return a[n:]  # keep", open: false },
      { kind: "text", lines: ["**Complexity:** O(n)"] },
    ]);
    expect(splitBlocks("```js\nconst x = 1;")).toEqual([{ kind: "code", lang: "js", code: "const x = 1;", open: true }]);
  });

  it("knows the usual languages by their fence tags", () => {
    expect(languageOf("py")).toBe("python");
    expect(languageOf("C++")).toBe("cpp");
    expect(languageOf("postgresql")).toBe("sql");
    expect(languageOf("text")).toBeNull();
  });

  it("draws bold and inline code without injecting HTML", () => {
    render(<p>{inline("**Bug:** `hi = mid` <b>never</b>")}</p>);
    expect(screen.getByText("Bug:").tagName).toBe("STRONG");
    expect(screen.getByText("hi = mid").tagName).toBe("CODE");
    expect(screen.getByText(/<b>never<\/b>/)).toBeInTheDocument();
  });

  it("highlights code and copies it exactly, blank lines and indentation kept", async () => {
    const write = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText: write } });
    render(<CodeAnswer text={"### 1. Merge\n" + CODE} />);
    expect(screen.getByRole("heading", { name: "1. Merge" })).toBeInTheDocument();
    const block = document.querySelector(".code-block") as HTMLElement;
    expect(block.querySelector(".hljs-keyword")).toHaveTextContent("def");
    await userEvent.click(within(block).getByRole("button", { name: "Copy the python code" }));
    expect(write).toHaveBeenCalledWith("def f(a):\n\n    return a[n:]  # keep");
    expect(within(block).getByRole("button", { name: "Copy the python code" })).toHaveTextContent("Copied");
  });

  it("cannot copy a block that is still being written", () => {
    render(<CodeAnswer text={"```python\ndef f(a):"} streaming />);
    expect(screen.getByRole("button", { name: "Copy the python code" })).toBeDisabled();
  });
});

let n = 0;
const ev = (event: UiEvent): Numbered => ({ n: n++, event });
const srv = (m: Record<string, unknown>) => ev({ kind: "server", message: { seq: n, ...m } as never });

function overlay(events: Numbered[]) {
  ipc.handle("snapshot", () => [events, true, n]);
  ipc.handle("overlay_prefs", () => ({ opacity: 1, compact: false, click_through: false, on_top: true, hotkeys: {} }));
  return render(<Overlay />);
}

describe("a follow-up about the last problem", () => {
  it("draws the new code as code, under spoken talking points", async () => {
    n = 0;
    overlay([
      srv({ type: "question.detected", question_id: "f1", text: "Can you write it in Java?", question_type: "coding", confidence: 1, speculative: false }),
      srv({ type: "answer.delta", question_id: "f1", pass: "fast", text: "- Same two pointers\n- Arrays.sort first\n" }),
      srv({ type: "answer.delta", question_id: "f1", pass: "deep", text: "```java\nint[][] merge(int[][] w) {\n  return w;\n}\n```\n**What changed:** Java." }),
    ]);
    const card = await screen.findByRole("article");
    const points = within(card).getByRole("group", { name: "Talking points" });
    expect(within(points).getAllByRole("listitem")).toHaveLength(2);
    const deep = within(card).getByRole("group", { name: "Detailed answer" });
    expect(within(deep).getByRole("button", { name: "Copy the java code" })).toBeInTheDocument();
    expect(within(deep).getByText("What changed:").tagName).toBe("STRONG");
  });
});

const shot = (id: number, title: string): ShotView => ({
  id, url: "data:image/webp;base64,UklGRg==", width: 1568, height: 882, title, bytes: 61_440,
  source: "window", capture_ms: 120, encode_ms: 90,
});  // prettier-ignore

describe("a screenshot's card", () => {
  const read = (tasks: { n: number; kind: string; language?: string }[]) =>
    srv({ type: "question.detected", question_id: "s1", text: "Screen: 2 tasks (choice, code), python", question_type: "coding", confidence: 1, speculative: false, screen: { tasks, language: "python" } });

  it("shows the screen's tasks, the answer and the code, and no detailed-answer button", async () => {
    n = 0;
    overlay([
      ev({ kind: "connection", state: { state: "live", session_id: "x", resumed: false } }),
      srv({ type: "question.detected", question_id: "s1", text: "Screen", question_type: "coding", confidence: 1, speculative: false }),
      read([{ n: 1, kind: "choice" }, { n: 2, kind: "code", language: "python" }]),
      srv({ type: "answer.delta", question_id: "s1", pass: "fast", text: "### 1. Heaps\n**Answer:** B) O(log n)\n" }),
    ]);
    const card = await screen.findByRole("article");
    const tasks = within(card).getByRole("list", { name: "Tasks on the screen" });
    expect(tasks).toHaveTextContent("1Choice");
    expect(tasks).toHaveTextContent("2Code · python");
    const answer = within(card).getByRole("group", { name: "Answer" });
    expect(within(answer).getByText("Answer:").tagName).toBe("STRONG");
    // The reading says code is coming: its place is held, and nothing offers a detailed answer.
    expect(within(card).getByRole("group", { name: "Code" })).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /detailed answer/ })).not.toBeInTheDocument();
    await act(() => emit("copilot://event", srv({ type: "answer.delta", question_id: "s1", pass: "deep", text: "```python\ndef two_sum(a, t):\n    pass\n```\n" })));
    await waitFor(() => expect(card.querySelector(".pass-deep .code-block")).not.toBeNull());
  });

  it("shows the shots it was asked with, and one opens large on a click", async () => {
    n = 0;
    ipc.handle("coding_sent_shots", () => [{ question_id: "s7", shots: [shot(7, "Problem - Chrome"), shot(8, "main.c")] }]);
    overlay([srv({ type: "question.detected", question_id: "s7", text: "Screen", question_type: "coding", confidence: 1, speculative: false })]);
    const card = await screen.findByRole("article");
    const strip = await within(card).findByRole("list", { name: "Shots sent" });
    const second = within(strip).getByRole("button", { name: "Shot 2: main.c" });
    await userEvent.click(second);
    expect(second).toHaveAttribute("aria-expanded", "true");
    expect(within(card).getByRole("img", { name: "Shot 2: main.c" })).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Close the shot" }));
    expect(within(card).queryByRole("button", { name: "Close the shot" })).not.toBeInTheDocument();
  });

  it("a screen with nothing to answer says so, and has no code section", async () => {
    n = 0;
    overlay([
      srv({ type: "question.detected", question_id: "s1", text: "Screen: nothing readable", question_type: "coding", confidence: 1, speculative: false, screen: { tasks: [], unreadable: "a blank desktop" } }),
      srv({ type: "answer.delta", question_id: "s1", pass: "fast", text: "Please take another shot." }),
      srv({ type: "answer.done", question_id: "s1", pass: "fast", memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } }),
    ]);
    const card = await screen.findByRole("article");
    expect(card).toHaveTextContent("Nothing to answer on this shot: a blank desktop. Take another.");
    expect(within(card).queryByRole("group", { name: "Code" })).not.toBeInTheDocument();
  });
});



describe("the screenshot tray", () => {
  it("shows only thumbnails and removes exactly the selected screenshot", async () => {
    render(<CodingBar shots={[shot(1, "Problem - Chrome"), shot(2, "solution.py")]} />);
    const shots = screen.getByRole("list", { name:"Screenshots ready to answer" });
    expect(within(shots).getByRole("img", { name:"Screenshot 1: Problem - Chrome" })).toBeInTheDocument();
    expect(within(shots).getAllByRole("listitem")[0]).toHaveAttribute("title", "Problem - Chrome · 1568×882 · 60 KB · 210 ms");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name:"Remove screenshot 2" }));
    expect(ipc.calls.find(c => c.cmd === "coding_remove_shot")?.args).toEqual({id:2});
  });
  it("occupies no space when there are no pending screenshots", () => {
    const {container} = render(<CodingBar shots={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
