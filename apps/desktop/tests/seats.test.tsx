// Guest seats (ARCHITECTURE.md, "Guest seats"): the reducer folds the seat messages; a guest's
// overlay is read only, says whose private call it is, and sends notes when allowed; the host
// sees who is watching and the guests' notes in a chat apart from the answers, with Ask AI.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { emit, listen } from "@tauri-apps/api/event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Numbered, UiEvent } from "../src/api";
import Overlay from "../src/Overlay";
import { GuestInvites, deviceProblem, openSeats, voiceIdentities, watchingText } from "../src/seats";
import { badge, initial, postCallFor, reduce, type SessionView } from "../src/session";
import { VOICE_OFF, type VoiceCmd, type VoiceStatus } from "../src/voice";
import { ipc } from "./ipc";

let n = 0;
const ev = (event: UiEvent): Numbered => ({ n: n++, event });
const msg = (m: Record<string, unknown>) => ({ seq: 1, ...m }) as never;
const srv = (m: Record<string, unknown>) => ev({ kind: "server", message: msg(m) });
const live = () => ev({ kind: "connection", state: { state: "live", session_id: "s1", resumed: false } });
const SEAT = "0192f3d0-0000-7a00-8000-0000000000b1";

const ready = (caps = ["view", "note"]) =>
  srv({
    type: "seat.ready",
    session_id: "s1",
    seat_id: SEAT,
    host_name: "Alex",
    mode: "interview",
    title: "Acme onsite",
    capabilities: caps,
    host_connected: true,
    sharing: true,
  });

const snapshot = () =>
  srv({
    type: "seat.snapshot",
    seq: 40,
    messages: [
      { type: "transcript.final", seq: 30, channel: "them", seg_id: 1, text: "Tell me about an outage.", start_ms: 0, end_ms: 900 },
      { type: "transcript.final", seq: 31, channel: "me", seg_id: 1, text: "Sure, last spring.", start_ms: 1000, end_ms: 1900 },
      { type: "question.detected", seq: 32, question_id: "q1", text: "Tell me about an outage.", question_type: "behavioral", confidence: 0.9, speculative: false },
      { type: "answer.delta", seq: 35, question_id: "q1", pass: "fast", text: "- Froze deploys\n- Rolled back" },
      { type: "answer.done", seq: 36, question_id: "q1", pass: "fast", memory_refs: [], usage: { input_tokens: 0, cached_tokens: 0, output_tokens: 0 } },
    ],
  });

function fold(events: Numbered[]): SessionView {
  return events.reduce<SessionView>((s, e) => reduce(s, { type: "event", event: e.event }), { ...initial, running: true });
}

const HOTKEYS = { toggle_overlay: "", click_through: "", click_through_off: "", opacity_up: "", opacity_down: "" };

function start(events: Numbered[]) {
  ipc.handle("snapshot", () => [events, true, n]);
  ipc.handle("overlay_prefs", () => ({ opacity: 1, compact: false, click_through: false, on_top: true, hotkeys: HOTKEYS }));
  return render(<Overlay />);
}

describe("the session reducer", () => {
  it("knows the watched call and replaces the conversation with each snapshot", () => {
    n = 0;
    let s = fold([live(), ready(), srv({ type: "question.detected", question_id: "old", text: "Old", question_type: "other", confidence: 1, speculative: false }), snapshot()]);
    expect(s.seat).toMatchObject({ hostName: "Alex", capabilities: ["view", "note"], hostConnected: true, sharing: true });
    expect(s.questions.map((q) => q.id)).toEqual(["q1"]);
    expect(s.questions[0].fast).toMatchObject({ text: "- Froze deploys\n- Rolled back", done: true });
    expect(s.transcript.map((t) => t.text)).toEqual(["Tell me about an outage.", "Sure, last spring."]);

    s = reduce(s, { type: "event", event: { kind: "server", message: msg({ type: "seat.host", connected: false, sharing: true }) } });
    expect(badge(s)).toEqual({ state: "reconnecting", text: "Alex is reconnecting" });
    s = reduce(s, { type: "event", event: { kind: "server", message: msg({ type: "seat.host", connected: true, sharing: false }) } });
    expect(badge(s)).toEqual({ state: "degraded", text: "Alex paused sharing" });
    s = reduce(s, { type: "event", event: { kind: "server", message: msg({ type: "seat.update", capabilities: ["view"] }) } });
    expect(s.seat?.capabilities).toEqual(["view"]);

    s = reduce(s, { type: "note_sent", id: "c1", text: "Mention the rollback" });
    s = reduce(s, { type: "event", event: { kind: "server", message: msg({ type: "seat.command.result", command_id: "c1", result: "rate_limited" }) } });
    expect(s.sentNotes).toEqual([{ id: "c1", text: "Mention the rollback", result: "rate_limited" }]);
  });

  it("keeps the host's seats, notes (once each) and sharing", () => {
    n = 0;
    const note = srv({ type: "seat.note", seat_id: SEAT, note_id: "n1", name: "Pavlo", text: "Ask about on-call", at: "2026-11-17T15:00:00Z" });
    let s = fold([
      live(),
      srv({
        type: "seats.update",
        seats: [
          { seat_id: SEAT, name: "Pavlo", status: "admitted", capabilities: ["view", "note"], connected: true },
          { seat_id: "x", name: "Dana", status: "admitted", capabilities: ["view"], connected: false },
        ],
      }),
      note,
      note,
      srv({ type: "sharing", on: false }),
    ]);
    expect(s.notes.map((x) => [x.name, x.text, x.asked])).toEqual([["Pavlo", "Ask about on-call", false]]);
    expect(s.sharing).toBe(false);
    // Pavlo follows the call; Dana is invited and has not joined.
    expect(watchingText(openSeats(s.seats))).toBe("Pavlo · 1 invited");
    s = reduce(s, { type: "note_asked", id: "n1" });
    expect(s.notes[0].asked).toBe(true);
    expect(s.seat).toBeNull();
  });
});

describe("when a guest stops watching", () => {
  const stop = (reason: "left" | "ended") => ev({ kind: "stopped", reason });

  it("never opens a post-call screen: the call was someone else's", () => {
    n = 0;
    expect(postCallFor(fold([live(), ready(), snapshot(), stop("left")]))).toBeNull();
    expect(postCallFor(fold([live(), ready(), snapshot(), stop("ended")]))).toBeNull();
    // The host's own call that ended still gets one.
    n = 0;
    expect(postCallFor(fold([live(), stop("ended")]))).toBe("s1");
  });

  it("the overlay drops the guest's notice and note box", async () => {
    n = 0;
    start([live(), ready(), snapshot()]);
    await screen.findByText(/You are viewing Alex's private call/);
    await act(() => emit("copilot://event", stop("left")));
    expect(screen.queryByText(/You are viewing Alex's private call/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Note to Alex" })).not.toBeInTheDocument();
  });
});

describe("a guest's overlay", () => {
  it("says whose private call it is and acts on nothing", async () => {
    n = 0;
    start([live(), ready(), snapshot()]);
    expect(await screen.findByText(/You are viewing Alex's private call/)).toBeInTheDocument();
    const card = (await screen.findAllByRole("article"))[0];
    expect(within(card).getByText("Froze deploys")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Get the detailed answer/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Helpful" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ask" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Answer" })).not.toBeInTheDocument();
    // The host's own words are labelled as the host's, not "You".
    expect(screen.getAllByText("Alex").length).toBeGreaterThan(0);
    expect(screen.queryByText("You")).not.toBeInTheDocument();
  });

  it("sends a note to the host and shows what became of it", async () => {
    n = 0;
    ipc.handle("seat_note", () => "c1");
    start([live(), ready(), snapshot()]);
    const box = await screen.findByRole("textbox", { name: "Note to Alex" });
    await userEvent.type(box, "Mention the rollback{Enter}");
    expect(ipc.calls.find((c) => c.cmd === "seat_note")?.args).toEqual({ text: "Mention the rollback" });
    expect(await screen.findByText("Sending")).toBeInTheDocument();
    await act(() => emit("copilot://event", srv({ type: "seat.command.result", command_id: "c1", result: "ok" })));
    expect(await screen.findByText("Delivered")).toBeInTheDocument();
  });

  it("has no note box without the grant, and Leave leaves", async () => {
    n = 0;
    start([live(), ready(["view"]), snapshot()]);
    await screen.findByText(/You are viewing Alex's private call/);
    expect(screen.queryByRole("textbox", { name: "Note to Alex" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Leave" }));
    expect(ipc.calls.some((c) => c.cmd === "seat_leave")).toBe(true);
  });
});

describe("the host's overlay", () => {
  const hostEvents = () => {
    n = 0;
    return [
      live(),
      srv({ type: "seats.update", seats: [{ seat_id: SEAT, name: "Pavlo", status: "admitted", capabilities: ["view", "note"] }] }),
      srv({ type: "question.detected", question_id: "q1", text: "Tell me about an outage.", question_type: "behavioral", confidence: 0.9, speculative: false }),
      srv({ type: "seat.note", seat_id: SEAT, note_id: "n1", name: "Pavlo", text: "Ask about on-call", at: "2026-11-17T15:00:00Z" }),
    ];
  };

  it("shows who is watching, and notes as a chat apart from the answers, with Ask AI", async () => {
    start(hostEvents());
    expect(await screen.findByText(/Pavlo/, { selector: ".ov-watching" })).toHaveAttribute("role", "status");
    const lane = await screen.findByRole("region", { name: "Notes from guests" });
    expect(within(lane).getByRole("heading", { name: "Notes from guests · only you see them" })).toBeInTheDocument();
    expect(within(lane).getByText("Ask about on-call")).toBeInTheDocument();
    expect(lane.querySelector(".ov-notes-body")).not.toBeNull(); // the part that scrolls
    const card = (await screen.findAllByRole("article")).find((a) => a.classList.contains("card"));
    expect(card && within(card).queryByText("Ask about on-call")).toBeNull(); // not in the answers
    // One button per note: Ask AI.
    expect(within(lane).getAllByRole("button").map((b) => b.textContent)).toEqual(["Ask AI"]);
    await userEvent.click(within(lane).getByRole("button", { name: "Ask AI" }));
    expect(ipc.calls.find((c) => c.cmd === "ask_note")?.args).toEqual({ noteId: "n1", text: "Ask about on-call" });
    expect(await within(lane).findByRole("button", { name: "Asked" })).toBeDisabled();
  });

  it("invites from the gear", async () => {
    ipc.api("POST", /^\/sessions\/s1\/seats$/, () => ({ status: "sent" }));
    start(hostEvents());
    await userEvent.click(await screen.findByRole("button", { name: "Overlay settings" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Guest's Copilot email" }), "pavlo@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Invite" }));
    expect(ipc.apiCalls("POST")).toEqual([
      { method: "POST", path: "/sessions/s1/seats", body: { email: "pavlo@example.com", capabilities: ["view", "note"] } },
    ]);
    expect(await screen.findByText(/they can join from their Call screen now/)).toBeInTheDocument();
  });
});

describe("a guest's invites", () => {
  const invited = () =>
    ipc.api("GET", /^\/seats$/, () => ({
      seats: [{ seat_id: SEAT, session_id: "s1", host_name: "Alex", mode: "interview", title: null, status: "admitted", capabilities: ["view"], expires_at: null }],
    }));

  it("shows Join at once: nothing to accept, no one to wait for", async () => {
    invited();
    render(<GuestInvites running={false} />);
    expect(await screen.findByText("Alex invited you to watch a call")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Join" }));
    expect(ipc.calls.find((c) => c.cmd === "seat_join")?.args).toEqual({ request: { session_id: "s1", seat_id: SEAT } });
  });

  it("Not now hides the invite here", async () => {
    invited();
    render(<GuestInvites running={false} />);
    await userEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(screen.queryByText("Alex invited you to watch a call")).not.toBeInTheDocument();
    expect(ipc.calls.some((c) => c.cmd === "seat_join")).toBe(false);
  });
});

describe("guests' voice", () => {
  let cmds: VoiceCmd[] = [];
  let unlisten: (() => void) | null = null;
  beforeEach(async () => {
    cmds = [];
    unlisten = await listen<VoiceCmd>("voice:cmd", (e) => cmds.push(e.payload));
  });
  afterEach(() => unlisten?.());
  const voiceStatus = (over: Partial<VoiceStatus>) => act(() => emit("voice:status", { ...VOICE_OFF, ...over }));

  it("plays only admitted seats that hold voice, on headphones that are not the call's", () => {
    expect(
      voiceIdentities([
        { seat_id: "a", name: "A", status: "admitted", capabilities: ["view", "voice"] },
        { seat_id: "b", name: "B", status: "admitted", capabilities: ["view", "note"] },
        { seat_id: "c", name: "C", status: "removed", capabilities: ["view", "voice"] },
      ]),
    ).toEqual(["seat:a"]);
    const d = { id: "x", name: "X", form_factor: "headphones" as const, private: true, call: false };
    expect(deviceProblem(d)).toBeNull();
    expect(deviceProblem({ ...d, call: true })).toBe("the call plays here");
    expect(deviceProblem({ ...d, form_factor: "speakers", private: false })).toBe("not headphones");
  });

  it("a guest with voice connects, then holds Talk", async () => {
    n = 0;
    start([live(), ready(["view", "voice"]), snapshot()]);
    const talk = await screen.findByRole("button", { name: "Hold to talk to Alex" });
    await waitFor(() => expect(cmds).toContainEqual({ cmd: "guest_on", seatId: SEAT }));
    expect(talk).toBeDisabled(); // not connected yet
    await voiceStatus({ guest: "on" });
    await waitFor(() => expect(talk).toBeEnabled());
    fireEvent.pointerDown(talk);
    fireEvent.pointerUp(talk);
    await waitFor(() => expect(cmds.filter((c) => c.cmd === "guest_talk")).toEqual([{ cmd: "guest_talk", on: true }, { cmd: "guest_talk", on: false }]));
  });

  it("no voice granted: no Talk", async () => {
    n = 0;
    start([live(), ready(["view", "note"]), snapshot()]);
    await screen.findByText(/You are viewing Alex's private call/);
    expect(screen.queryByRole("button", { name: /Hold to talk/ })).not.toBeInTheDocument();
  });

  it("the host turns voice on with headphones; a refusal says why", async () => {
    n = 0;
    ipc.handle("voice_devices", () => [
      { id: "wasapi:spk", name: "Speakers (Realtek)", form_factor: "speakers", private: false, call: true },
      { id: "wasapi:hp", name: "Headphones (USB)", form_factor: "headphones", private: true, call: false },
    ]);
    start([
      live(),
      srv({ type: "seats.update", seats: [{ seat_id: SEAT, name: "Pavlo", status: "admitted", capabilities: ["view", "voice"], connected: true }] }),
    ]);
    await userEvent.click(await screen.findByRole("button", { name: "Overlay settings" }));
    await waitFor(() => expect(cmds).toContainEqual({ cmd: "outputs" }));
    await voiceStatus({ outputs: [{ deviceId: "d-spk", label: "Speakers (Realtek)" }, { deviceId: "d-hp", label: "Headphones (USB)" }] });
    const pick = await screen.findByRole("combobox", { name: "Headphones for guests' voice" });
    expect(await within(pick).findByRole("option", { name: "Speakers (Realtek) (the call plays here)" })).toBeDisabled();
    await userEvent.selectOptions(pick, "d-hp");
    await userEvent.click(screen.getByRole("button", { name: "Turn on" }));
    expect(cmds).toContainEqual({ cmd: "host_on", deviceId: "d-hp", sessionId: "s1", allowed: [`seat:${SEAT}`] });
    await voiceStatus({ error: "The test tone on those headphones reached the call or the microphone, so guests' voice stays off." });
    expect(await screen.findByText(/reached the call or the microphone/)).toBeInTheDocument();
  });
});
