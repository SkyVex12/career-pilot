import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { emit } from "@tauri-apps/api/event";
import { GuestScreenView } from "../src/screenui";
import { Watching, VoiceMute } from "../src/seats";
import { guestDeps, TOPIC } from "../src/screen";
import { VOICE_OFF } from "../src/voice";
import { initial, type SessionView } from "../src/session";

type Handler = (...args: unknown[]) => void;
function fakeRoom() {
  const handlers = new Map<string, Handler[]>();
  return {
    on(event: string, handler: Handler) { handlers.set(event, [...(handlers.get(event) ?? []), handler]); return this; },
    emit(event: string, ...args: unknown[]) { for (const handler of handlers.get(event) ?? []) handler(...args); },
    connect: vi.fn(async () => undefined), disconnect: vi.fn(async () => undefined),
    localParticipant: { publishData: vi.fn(async () => undefined) },
  };
}
function videoTrack() {
  return { kind: "video", attach: vi.fn(), detach: vi.fn(), setPlayoutDelay: vi.fn(), getRTCStatsReport: vi.fn(async () => new Map()) };
}
function session(caps = ["view", "note", "device.view", "device.control"]): SessionView {
  return { ...initial, running: true, sessionId: "s1", connection: { state: "live", session_id: "s1", resumed: false }, seat: { sessionId: "s1", seatId: "seat1", hostName: "Alex", mode: "interview", title: "Northwind", capabilities: caps as never, hostConnected: true, sharing: true } };
}
let rooms: ReturnType<typeof fakeRoom>[];
beforeEach(() => {
  rooms = [];
  vi.spyOn(guestDeps, "newRoom").mockImplementation(() => { const room = fakeRoom(); rooms.push(room); return room as never; });
  vi.spyOn(guestDeps, "token").mockResolvedValue({ url: "wss://example", token: "screen-token", control: true });
});
afterEach(() => vi.restoreAllMocks());

describe("assistant remote desktop workspace", () => {
  it("shows a clear permission state on the main call page before screen access is granted", () => {
    render(<Watching s={session(["view", "note"])} dispatch={() => undefined} />);
    expect(screen.getByRole("region", { name: "Alex's screen" })).toBeVisible();
    expect(screen.getByText("Screen access is off")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Note to Alex" })).toBeVisible();
    expect(guestDeps.token).not.toHaveBeenCalled();
  });

  it("connects on the main call page and presents the host video without opening another window", async () => {
    render(<Watching s={session()} dispatch={() => undefined} />);
    // The waiting heading also exists before the token request creates a receiver.
    await waitFor(() => expect(rooms).toHaveLength(1));
    expect(rooms[0].connect).toHaveBeenCalledWith("wss://example", "screen-token", { autoSubscribe: true });
    expect(await screen.findByText("Their shared display will appear here automatically.")).toBeVisible();
    expect(screen.getByText("Waiting for Alex to share")).toBeVisible();
    const track = videoTrack();
    await act(() => rooms[0].emit("trackSubscribed", track, {}, { identity: "host" }));
    expect(screen.getByText("Viewing")).toBeVisible();
    expect(track.attach).toHaveBeenCalledWith(document.querySelector("video"));
    expect(screen.getByRole("button", { name: "Request control" })).toBeEnabled();
  });

  it("moves one receiver between the main window and overlay and releases it on permission loss", async () => {
    const s = session();
    const views = (overlay: boolean, current = s) => <><div data-testid="main"><GuestScreenView s={current} /></div><div data-testid="overlay" hidden={!overlay}><GuestScreenView s={current} active={overlay} window="overlay" /></div></>;
    const view = render(views(false));
    await waitFor(() => expect(rooms).toHaveLength(1));
    // Mounting the inactive overlay does not take the default main receiver.
    expect(within(screen.getByTestId("main")).queryByRole("button", { name: "Open shared screen here" })).toBeNull();
    view.rerender(views(true));
    await waitFor(() => expect(rooms).toHaveLength(2));
    expect(rooms[0].disconnect).toHaveBeenCalled();
    await userEvent.click(within(screen.getByTestId("main")).getByRole("button", { name: "Open shared screen here" }));
    await waitFor(() => expect(rooms).toHaveLength(3));
    expect(rooms[1].disconnect).toHaveBeenCalled();
    expect(within(screen.getByTestId("overlay")).getByRole("button", { name: "Open shared screen here" })).toBeVisible();
    view.rerender(views(true, session(["view", "note"])));
    await waitFor(() => expect(rooms[2].disconnect).toHaveBeenCalled());
    expect(screen.getAllByText("Screen access is off")).toHaveLength(2);
  });

  it("keeps its receiver through control approval and ignores stale subscriptions after screen access is restored", async () => {
    const view = render(<GuestScreenView s={session(["view", "device.view"])} />);
    await waitFor(() => expect(rooms).toHaveLength(1));
    view.rerender(<GuestScreenView s={session()} />);
    expect(rooms).toHaveLength(1);
    expect(rooms[0].disconnect).not.toHaveBeenCalled();
    view.rerender(<GuestScreenView s={session(["view", "note"])} />);
    await waitFor(() => expect(rooms[0].disconnect).toHaveBeenCalled());
    view.rerender(<GuestScreenView s={session()} />);
    await waitFor(() => expect(rooms).toHaveLength(2));
    const old = videoTrack(); await act(() => rooms[0].emit("trackSubscribed", old, {}, { identity: "host" }));
    expect(old.attach).not.toHaveBeenCalled();
    const current = videoTrack(); await act(() => rooms[1].emit("trackSubscribed", current, {}, { identity: "host" }));
    expect(current.attach).toHaveBeenCalled();
  });

  it("keeps release controls inside full screen and releases held keys on blur", async () => {
    render(<GuestScreenView s={session()} />);
    await waitFor(() => expect(rooms).toHaveLength(1));
    await act(() => rooms[0].emit("trackSubscribed", videoTrack(), {}, { identity: "host" }));
    await userEvent.click(screen.getByRole("button", { name: "Request control" }));
    const requestPacket = (rooms[0].localParticipant.publishData.mock.calls as unknown as [Uint8Array, { topic: string }][]).find(([, options]) => options.topic === TOPIC.ask);
    expect(requestPacket).toBeDefined();
    const request = JSON.parse(new TextDecoder().decode(requestPacket![0]));
    expect(screen.queryByRole("button", { name: "Release control" })).toBeNull();
    await act(() => rooms[0].emit("dataReceived", new TextEncoder().encode(JSON.stringify({ on: true, requestId: request.requestId })), { identity: "host" }, 0, TOPIC.control));
    const desktop = screen.getByRole("region", { name: "Alex's screen" });
    Object.defineProperty(desktop, "requestFullscreen", { value: vi.fn(async () => undefined), configurable: true });
    await userEvent.click(screen.getByRole("button", { name: "Full screen" }));
    expect(desktop.requestFullscreen).toHaveBeenCalled();
    expect(within(desktop).getByRole("button", { name: "Release control" })).toBeVisible();
    const target = screen.getByLabelText("Alex's screen: your mouse and keys go there");
    fireEvent.keyDown(target, { key: "a", code: "KeyA" });
    fireEvent.blur(target);
    const packets = rooms[0].localParticipant.publishData.mock.calls as unknown as [Uint8Array, { topic: string }][];
    expect(packets.filter(([, options]) => options.topic === TOPIC.input).map(([payload]) => JSON.parse(new TextDecoder().decode(payload)))).toEqual([{ events: [{ t: "key_down", code: "KeyA" }] }, { events: [{ t: "key_up", code: "KeyA" }] }]);
  });

  it("shows a full-screen error without losing the video or control actions", async () => {
    render(<GuestScreenView s={session()} />); await waitFor(() => expect(rooms).toHaveLength(1));
    await act(() => rooms[0].emit("trackSubscribed", videoTrack(), {}, { identity: "host" }));
    const desktop = screen.getByRole("region", { name: "Alex's screen" });
    Object.defineProperty(desktop, "requestFullscreen", { value: vi.fn(async () => { throw new Error("Full screen unavailable"); }), configurable: true });
    await userEvent.click(screen.getByRole("button", { name: "Full screen" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Full screen unavailable");
    expect(screen.getByText("Viewing")).toBeVisible();
  });

  it("shows the same assistant mute state in both windows after the authoritative event", async () => {
    const s = { ...initial, running: true, sessionId: "host-session" };
    render(<><VoiceMute s={s} /><VoiceMute s={s} /></>);
    await act(async () => { await Promise.resolve(); await emit("voice:status", { ...VOICE_OFF, host: "on", hostMuted: true }); });
    const toggles = await screen.findAllByRole("switch", { name: "Hear assistants" });
    expect(toggles).toHaveLength(2);
    for (const toggle of toggles) expect(toggle).not.toBeChecked();
    await act(() => emit("voice:status", { ...VOICE_OFF, host: "on", hostMuted: false }));
    for (const toggle of toggles) expect(toggle).toBeChecked();
  });
});
