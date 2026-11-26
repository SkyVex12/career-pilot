import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit, listen } from "@tauri-apps/api/event";
import { mockWindows } from "@tauri-apps/api/mocks";
import { describe, expect, it } from "vitest";
import type { Numbered, ShotView, UiEvent } from "../src/api";
import Overlay from "../src/Overlay";
import { CodingBar } from "../src/CodingBar";
import { ScreenChip } from "../src/screenui";
import { SCREEN_OFF } from "../src/screen";
import { initial } from "../src/session";
import { SETTINGS } from "./fixtures";
import { ipc } from "./ipc";

let n = 0;
const event = (value: UiEvent): Numbered => ({ n: n++, event: value });
const server = (value: Record<string, unknown>) => event({ kind: "server", message: { seq: n, ...value } as never });
const live = () => event({ kind: "connection", state: { state: "live", session_id: "s1", resumed: false } });
const shot = (id: number): ShotView => ({ id, url: "data:image/webp;base64,UklGRg==", title: `Problem ${id}`, width: 500, height: 400, bytes: 100, source: "window", capture_ms: 2, encode_ms: 2 });
function start(guest = false) {
  n = 0;
  const events = [live()];
  if (guest) events.push(server({ type: "seat.ready", session_id: "s1", seat_id: "guest1", host_name: "Alex", mode: "interview", title: "Interview", capabilities: ["view", "note"], host_connected: true, sharing: true }));
  events.push(server({ type: "question.detected", question_id: "q1", text: "How did you recover?", question_type: "other", confidence: 1, speculative: false }), server({ type: "answer.delta", question_id: "q1", pass: "fast", text: "- Rolled back" }));
  ipc.handle("snapshot", () => [events, true, n]);
  ipc.handle("overlay_prefs", () => ({ opacity: 1, compact: false, click_through: false, on_top: true, hotkeys: {} }));
  ipc.handle("get_settings", () => ({ settings: SETTINGS }));
  mockWindows("overlay");
  return render(<Overlay />);
}

describe("the approved overlay interactions", () => {
  it("opens settings on hover and keeps the panel usable until the pointer leaves", async () => {
    start();
    const gear = screen.getByRole("button", { name: "Quick settings" });
    gear.focus();
    fireEvent.pointerEnter(gear, { pointerType: "mouse" });
    const panel = await screen.findByRole("dialog", { name: "Quick settings" });
    expect(gear).toHaveFocus();
    fireEvent.pointerLeave(gear, { pointerType: "mouse" });
    fireEvent.pointerEnter(panel, { pointerType: "mouse" });
    expect(panel).toBeVisible();
    fireEvent.pointerLeave(panel, { pointerType: "mouse" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument());
  });

  it("keeps the shared on-top value when a native window rejects the change", async () => {
    start();
    ipc.handle("set_overlay_on_top", () => { throw "Window update failed"; });
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    await userEvent.click(screen.getByRole("switch", { name: "Keep on top" }));
    expect((await screen.findByText("Window update failed")).closest('[role="alert"]')).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Keep on top" })).toBeChecked();
  });

  it("saves Stealth with freshly read settings, preserving unrelated changes", async () => {
    start();
    let current = { ...SETTINGS, stealth: false, mic_device: "first" };
    ipc.handle("get_settings", () => ({ settings: current }));
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    const toggle = await screen.findByRole("switch", { name: "Stealth mode" });
    await waitFor(() => expect(toggle).toBeEnabled());
    current = { ...current, mic_device: "changed-in-main" };
    await userEvent.click(toggle);
    await waitFor(() => expect(ipc.calls.find(c => c.cmd === "save_settings")?.args).toMatchObject({ settings: { stealth: true, mic_device: "changed-in-main" } }));
    expect(toggle).toBeChecked();
  });

  it("closes quick settings with Escape and lets an outside End click work immediately", async () => {
    start();
    await screen.findByRole("article");
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    expect(screen.queryByRole("combobox", { name: "Layout" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Call mode" })).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Paragraphs" })).toBeVisible();
    expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    await userEvent.click(screen.getByRole("button", { name: "End call" }));
    expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
    expect(screen.getByRole("alertdialog", { name: "End this call?" })).toBeVisible();
    expect(ipc.calls.some(c => c.cmd === "end_session")).toBe(false);
  });

  it("keeps internal dropdown scrolling usable and dismisses on background scroll or window blur", async () => {
    start();
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    fireEvent.scroll(screen.getByRole("dialog", { name: "Quick settings" }));
    expect(screen.getByRole("dialog", { name: "Quick settings" })).toBeVisible();
    fireEvent.scroll(document);
    expect(screen.queryByRole("dialog", { name: "Quick settings" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Assistants" }));
    expect(screen.getByRole("dialog", { name: "Assistants" })).toBeVisible();
    fireEvent.blur(window);
    expect(screen.queryByRole("dialog", { name: "Assistants" })).not.toBeInTheDocument();
  });

  it("floats hotkey screenshots above the composer without switching away from the conversation", async () => {
    start();
    const answer = await screen.findByRole("article");
    expect(screen.queryByRole("tab", {name:"Screen tools"})).not.toBeInTheDocument();
    expect(screen.queryByRole("button", {name:"Live audio"})).not.toBeInTheDocument();
    await act(() => emit("copilot://coding-shots", [shot(7)]));
    const tray = await screen.findByRole("list", {name:"Screenshots ready to answer"});
    expect(tray).toBeVisible();
    expect(answer).toBeVisible();
    expect(tray.closest(".ov-composer")).toContainElement(screen.getByRole("textbox", {name:"Question"}));
    await userEvent.click(screen.getByRole("button", {name:"Answer"}));
    expect(ipc.calls.some(c => c.cmd === "coding_solve")).toBe(true);
    expect(ipc.calls.some(c => c.cmd === "respond")).toBe(false);
  });

  it("opens Assistants on hover, bridges to its panel, and closes after leaving", async () => {
    start();
    const button = await screen.findByRole("button", {name:/Assistants/});
    button.focus();
    fireEvent.pointerEnter(button, {pointerType:"mouse"});
    const panel = await screen.findByRole("dialog", {name:"Assistants"});
    expect(button).toHaveFocus();
    fireEvent.pointerLeave(button, {pointerType:"mouse"});
    fireEvent.pointerEnter(panel, {pointerType:"mouse"});
    expect(panel).toBeVisible();
    fireEvent.pointerLeave(panel, {pointerType:"mouse"});
    await waitFor(() => expect(screen.queryByRole("dialog", {name:"Assistants"})).not.toBeInTheDocument());
  });

  it("keeps assistant display controls local and delegates quitting to native cleanup without ending the host call", async () => {
    start(true);
    await screen.findByText(/Alex.s call/);
    expect(screen.getByText("Assisting")).toBeVisible();
    expect(screen.queryByRole("button", { name: "End call" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Shared screen" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Quick settings" }));
    const panel = screen.getByRole("dialog", { name: "Quick settings" });
    expect(within(panel).queryByRole("combobox", { name: "Layout" })).not.toBeInTheDocument();
    expect(within(panel).queryByText("Call options")).not.toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "All settings…" })).not.toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Quit CoRuby" }));
    expect(screen.getByRole("alertdialog", { name: "Stop assisting and quit CoRuby?" })).toBeVisible();
    expect(screen.getByText("You’ll stop assisting. The other person’s call will continue.")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Stop assisting & quit" }));
    await waitFor(() => expect(ipc.calls.some(c => c.cmd === "quit_app")).toBe(true));
    const actions = ipc.calls.filter(c => ["seat_leave", "quit_app", "end_session"].includes(c.cmd)).map(c => c.cmd);
    expect(actions).toEqual(["quit_app"]);
  });

  it("keeps the active controller visible while a second guest requests control", async () => {
    const commands: unknown[] = [];
    const off = await listen("screen:cmd", e => commands.push(e.payload));
    render(<ScreenChip s={{ ...initial, running: true, sessionId: "s1" }} />);
    await act(() => emit("screen:status", { ...SCREEN_OFF, host: "on", surface: "monitor", controller: { identity: "seat:1", name: "Dana" }, asking: [{ identity: "seat:2", name: "Jordan" }] }));
    expect(await screen.findByRole("button", { name: "Dana controls · Stop" })).toBeVisible();
    expect(screen.getByText("Jordan requests control")).toBeVisible();
    // Consent is presented by the main workspace, never a second competing overlay action.
    expect(screen.queryByRole("button", { name: /Let Jordan control|Approve control/ })).not.toBeInTheDocument();
    expect(commands).not.toContainEqual(expect.objectContaining({ cmd: "control" }));
    expect(screen.getByRole("button", { name: "Sharing · Stop" })).toBeVisible();
    off();
  });

  it("clears each pending shot through the native tray API", async () => {
    render(<CodingBar shots={[shot(1), shot(2)]} />);
    await userEvent.click(screen.getByRole("button", { name: "Remove screenshot 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove screenshot 2" }));
    expect(ipc.calls.filter(c => c.cmd === "coding_remove_shot").map(c => c.args)).toEqual([{ id: 1 }, { id: 2 }]);
  });
});
