// Guest seats' screen (src/screen.ts): the host shares only what they picked, from the click,
// and stops cleanly; a guest watches only the host's video, and comes back by itself when
// taken out of the room while it may still watch. Control (phase 5): only a guest who may, only
// after the host lets them, only on a whole-screen share, on the shared monitor; it ends with
// the grant, and the app lets go of what was held. LiveKit, the picker and the app are faked.
import { describe, expect, it, vi } from "vitest";
import {
  ENCODING,
  GuestScreen,
  HostScreen,
  MAX_EVENTS_PER_S,
  TOPIC,
  matchingMonitors,
  pictureSpot,
  readStats,
  type GuestDeps,
  type HostDeps,
  type InputEvent,
  type Monitor,
  type ScreenStatus,
} from "../src/screen";
import { withCapability } from "../src/seats";
import { statsLine } from "../src/screenui";

type Handler = (...args: unknown[]) => void;

function fakeRoom() {
  const handlers = new Map<string, Handler[]>();
  const sent: { topic: string; to: string[]; body: unknown }[] = [];
  return {
    handlers,
    sent,
    on(event: string, f: Handler) {
      handlers.set(event, [...(handlers.get(event) ?? []), f]);
      return this;
    },
    emit(event: string, ...args: unknown[]) {
      for (const f of handlers.get(event) ?? []) f(...args);
    },
    connect: vi.fn(async () => undefined),
    disconnect: vi.fn(async () => undefined),
    localParticipant: {
      publishTrack: vi.fn(async () => undefined),
      publishData: vi.fn(async (data: Uint8Array, o: { topic: string; destinationIdentities: string[] }) => {
        sent.push({ topic: o.topic, to: o.destinationIdentities, body: JSON.parse(new TextDecoder().decode(data)) });
      }),
    },
  };
}

function fakeScreenTrack(surface = "monitor", width = 1920, height = 1080) {
  const ended: (() => void)[] = [];
  return {
    kind: "video",
    stop: vi.fn(),
    ended: () => ended.forEach((f) => f()),
    mediaStreamTrack: {
      getSettings: () => ({ displaySurface: surface, width, height }),
      addEventListener: (_: string, f: () => void) => ended.push(f),
    },
  };
}

const MAIN: Monitor = { id: "\\\\.\\DISPLAY1", rect: { x: 0, y: 0, w: 2560, h: 1440 }, primary: true };
const SIDE: Monitor = { id: "\\\\.\\DISPLAY2", rect: { x: 2560, y: 0, w: 1280, h: 1024 }, primary: false };

function host(over: Partial<HostDeps> = {}, track = fakeScreenTrack()) {
  const room = fakeRoom();
  const log: string[] = [];
  let now = 0;
  const deps: HostDeps = {
    newRoom: () => room as never,
    capture: vi.fn(async () => [track as never]),
    token: vi.fn(async () => ({ url: "wss://lk", token: "host-token" })),
    monitors: vi.fn(async () => [MAIN, SIDE]),
    begin: vi.fn(async (r) => {
      log.push(`begin ${r.x},${r.y} ${r.w}x${r.h}`);
    }),
    input: vi.fn(async (events: InputEvent[]) => {
      log.push(`input ${events.map((e) => e.t).join(",")}`);
    }),
    end: vi.fn(async () => {
      log.push("end");
    }),
    now: () => now,
    ...over,
  };
  const statuses: ScreenStatus[] = [];
  const h = new HostScreen(deps, (s) => statuses.push(s));
  return {
    h, deps, room, track, statuses, log,
    tick: (ms: number) => {
      now += ms;
    },
  };
}

const PAVLO = { identity: "seat:p", name: "Pavlo" };
const BLAIR = { identity: "seat:b", name: "Blair" };
const data = (body: unknown) => new TextEncoder().encode(JSON.stringify(body));

describe("the host's screen", () => {
  it("opens the picker first, then joins and shares that track, with no audio", async () => {
    const t = host();
    await t.h.share("s1");
    expect(t.deps.capture).toHaveBeenCalledBefore(t.deps.token as never);
    expect(t.deps.token).toHaveBeenCalledWith("s1");
    expect(t.room.connect).toHaveBeenCalledWith("wss://lk", "host-token", { autoSubscribe: false });
    expect(t.room.localParticipant.publishTrack).toHaveBeenCalledWith(t.track, expect.objectContaining({ source: "screen_share" }));
    expect(t.h.status).toMatchObject({ host: "on", surface: "monitor", controller: null, error: null });
  });

  it("a closed picker is no error and joins nothing", async () => {
    const t = host({ capture: vi.fn(async () => Promise.reject(new DOMException("denied", "NotAllowedError"))) });
    await t.h.share("s1");
    expect(t.h.status).toMatchObject({ host: "off", surface: null, error: null });
    expect(t.deps.token).not.toHaveBeenCalled();
  });

  it("a refused token stops the capture and says why", async () => {
    const t = host({ token: vi.fn(async () => Promise.reject(new Error("Screen sharing is not set up on this server."))) });
    await t.h.share("s1");
    expect(t.track.stop).toHaveBeenCalled();
    expect(t.h.status).toMatchObject({ host: "off", error: "Screen sharing is not set up on this server." });
  });

  it("stops when the system's own stop ends the capture, and on Stop", async () => {
    const t = host();
    await t.h.share("s1");
    t.track.ended();
    await vi.waitFor(() => expect(t.room.disconnect).toHaveBeenCalled());
    expect(t.h.status.host).toBe("off");
    const u = host();
    await u.h.share("s1");
    await u.h.stop(null);
    expect(u.track.stop).toHaveBeenCalled();
    expect(u.h.sharing).toBe(false);
  });
});

describe("control of the host's screen", () => {
  // Pavlo has Control, and controls as soon as a whole screen is shared: no ask, no let.
  async function controlling() {
    const t = host({}, fakeScreenTrack("monitor", 1920, 1080)); // 16:9: the main monitor
    t.h.setAllowed([PAVLO, BLAIR]);
    await t.h.share("s1");
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    return t;
  }

  it("begins on the shared monitor and tells the guest", async () => {
    const t = await controlling();
    expect(t.log).toEqual(["begin 0,0 2560x1440"]);
    expect(t.h.status.controller).toEqual(PAVLO);
    expect(t.room.sent).toEqual([{ topic: TOPIC.control, to: ["seat:p"], body: { on: true } }]);
  });

  it("carries out only the controlling guest's events", async () => {
    const t = await controlling();
    const move = { events: [{ t: "move", x: 0.5, y: 0.5 }] };
    t.room.emit("dataReceived", data(move), { identity: "seat:b" }, 0, TOPIC.input); // may, but does not control
    t.room.emit("dataReceived", data(move), { identity: "seat:x" }, 0, TOPIC.input); // may not
    t.room.emit("dataReceived", data(move), { identity: "seat:p" }, 0, "chat"); // another topic
    t.room.emit("dataReceived", data(move), { identity: "seat:p" }, 0, TOPIC.input);
    await vi.waitFor(() => expect(t.log).toContain("input move"));
    expect(t.deps.input).toHaveBeenCalledTimes(1);
  });

  it("takes at most so many events a second", async () => {
    const t = await controlling();
    const many = { events: Array.from({ length: MAX_EVENTS_PER_S + 50 }, () => ({ t: "key_down", code: "KeyA" })) };
    t.room.emit("dataReceived", data(many), { identity: "seat:p" }, 0, TOPIC.input);
    t.room.emit("dataReceived", data(many), { identity: "seat:p" }, 0, TOPIC.input);
    await vi.waitFor(() => expect(t.deps.input).toHaveBeenCalledTimes(1));
    expect(vi.mocked(t.deps.input).mock.calls[0][0]).toHaveLength(MAX_EVENTS_PER_S);
    t.tick(1000);
    t.room.emit("dataReceived", data({ events: [{ t: "key_up", code: "KeyA" }] }), { identity: "seat:p" }, 0, TOPIC.input);
    await vi.waitFor(() => expect(t.deps.input).toHaveBeenCalledTimes(2));
  });

  it("moves never wait in line: while the app is busy only the newest is kept, ahead of what follows", async () => {
    const t = await controlling();
    let free: () => void = () => undefined;
    vi.mocked(t.deps.input).mockImplementationOnce(async (events) => {
      t.log.push(`input ${events.map((e) => e.t).join(",")}`);
      await new Promise<void>((r) => (free = r));
    });
    const move = (x: number) => data({ events: [{ t: "move", x, y: 0.5 }] });
    t.room.emit("dataReceived", move(0.1), { identity: "seat:p" }, 0, TOPIC.input);
    await vi.waitFor(() => expect(t.deps.input).toHaveBeenCalledTimes(1));
    t.room.emit("dataReceived", move(0.2), { identity: "seat:p" }, 0, TOPIC.input);
    t.room.emit("dataReceived", move(0.3), { identity: "seat:p" }, 0, TOPIC.input);
    t.room.emit("dataReceived", data({ events: [{ t: "down", button: 0, x: 0.3, y: 0.5 }] }), { identity: "seat:p" }, 0, TOPIC.input);
    free();
    await vi.waitFor(() => expect(t.deps.input).toHaveBeenCalledTimes(2));
    expect(vi.mocked(t.deps.input).mock.calls[1][0]).toEqual([
      { t: "move", x: 0.3, y: 0.5 },
      { t: "down", button: 0, x: 0.3, y: 0.5 },
    ]);
    // Moves alone: the newest goes as soon as the app is free.
    vi.mocked(t.deps.input).mockImplementationOnce(async () => {
      await new Promise<void>((r) => (free = r));
    });
    t.room.emit("dataReceived", move(0.4), { identity: "seat:p" }, 0, TOPIC.input);
    await vi.waitFor(() => expect(t.deps.input).toHaveBeenCalledTimes(3));
    t.room.emit("dataReceived", move(0.5), { identity: "seat:p" }, 0, TOPIC.input);
    t.room.emit("dataReceived", move(0.6), { identity: "seat:p" }, 0, TOPIC.input);
    free();
    await vi.waitFor(() => expect(t.deps.input).toHaveBeenCalledTimes(4));
    expect(vi.mocked(t.deps.input).mock.calls[3][0]).toEqual([{ t: "move", x: 0.6, y: 0.5 }]);
  });

  it("shares in H.264, sends twice the frames while a guest controls, and goes back after", async () => {
    const params = { encodings: [{ maxBitrate: 0, maxFramerate: 0 }], degradationPreference: "" };
    const sender = {
      getParameters: () => structuredClone(params),
      setParameters: vi.fn(async (p: typeof params) => {
        Object.assign(params, p);
      }),
    };
    const track = Object.assign(fakeScreenTrack("monitor", 1920, 1080), { sender });
    const t = host({}, track);
    await t.h.share("s1");
    expect(t.room.localParticipant.publishTrack).toHaveBeenCalledWith(
      track,
      expect.objectContaining({
        videoCodec: "h264",
        videoEncoding: { maxBitrate: ENCODING.watch.maxBitrate, maxFramerate: ENCODING.watch.maxFramerate },
      }),
    );
    t.h.setAllowed([PAVLO]);
    await vi.waitFor(() => expect(params).toMatchObject({ encodings: [{ maxFramerate: 60, maxBitrate: ENCODING.control.maxBitrate }], degradationPreference: "balanced" }));
    await t.h.endControl(null, "host");
    expect(params).toMatchObject({ encodings: [{ maxFramerate: 30, maxBitrate: ENCODING.watch.maxBitrate }], degradationPreference: "maintain-resolution" });
  });

  it("when the grant goes, control ends with everything let go, and passes to the next guest who may", async () => {
    const t = await controlling();
    t.h.setAllowed([BLAIR]);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(BLAIR));
    expect(t.log).toEqual(["begin 0,0 2560x1440", "end", "begin 0,0 2560x1440"]);
    expect(t.room.sent.slice(-2)).toEqual([
      { topic: TOPIC.control, to: ["seat:p"], body: { on: false } },
      { topic: TOPIC.control, to: ["seat:b"], body: { on: true } },
    ]);
    // The share stopping ends it.
    await t.h.stop(null);
    expect(t.log[t.log.length - 1]).toBe("end");
  });

  it("the host's Stop holds for everyone until their Control is given again, and names them to take it away", async () => {
    const t = await controlling();
    await t.h.endControl(null, "host");
    expect(t.h.status.controller).toBeNull();
    expect(t.h.status.stopped).toEqual([PAVLO, BLAIR]);
    expect(t.log[t.log.length - 1]).toBe("end"); // no one took over
    // Asking does not get it back.
    t.room.emit("dataReceived", data({}), { identity: "seat:p" }, 0, TOPIC.ask);
    await new Promise((r) => setTimeout(r, 20));
    expect(t.h.status.controller).toBeNull();
    // The UI takes Control away (the list empties), then the host gives it again: it works again.
    t.h.setAllowed([]);
    expect(t.h.status.stopped).toEqual([]);
    t.h.setAllowed([PAVLO]);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
  });

  it("a guest who gives control back takes it again with Take control; meanwhile another guest may", async () => {
    const t = await controlling();
    t.room.emit("dataReceived", data({}), { identity: "seat:p" }, 0, TOPIC.release);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(BLAIR)); // Pavlo held back, Blair next
    t.room.emit("dataReceived", data({}), { identity: "seat:b" }, 0, TOPIC.release);
    await vi.waitFor(() => expect(t.h.status.controller).toBeNull()); // both held back now
    t.room.emit("dataReceived", data({}), { identity: "seat:p" }, 0, TOPIC.ask);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
  });

  it("while someone controls, another guest's Take control shows to the host, from a guest who may only", async () => {
    const t = await controlling();
    t.room.emit("dataReceived", data({}), { identity: "seat:x" }, 0, TOPIC.ask);
    t.room.emit("dataReceived", data({}), { identity: "seat:b" }, 0, TOPIC.ask);
    await vi.waitFor(() => expect(t.h.status.asking).toEqual([BLAIR]));
    await t.h.allow(BLAIR);
    expect(t.h.status.controller).toEqual(BLAIR);
    expect(t.h.status.asking).toEqual([]);
  });

  it("waits for a guest to be in the room, and tells one who joins late", async () => {
    const t = host({}, fakeScreenTrack("monitor", 1920, 1080));
    const there = new Map<string, unknown>();
    Object.assign(t.room, { remoteParticipants: there });
    t.h.setAllowed([PAVLO]);
    await t.h.share("s1");
    await new Promise((r) => setTimeout(r, 20));
    expect(t.h.status.controller).toBeNull();
    there.set("seat:p", {});
    t.room.emit("participantConnected", { identity: "seat:p" });
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    expect(t.room.sent).toEqual([{ topic: TOPIC.control, to: ["seat:p"], body: { on: true } }]);
  });

  it("is refused for a window share, for a guest without the grant, and before sharing", async () => {
    const t = host({}, fakeScreenTrack("window"));
    await t.h.allow(PAVLO);
    expect(t.h.status.error).toBe("Share your screen first.");
    await t.h.share("s1");
    t.h.setAllowed([PAVLO]);
    await t.h.allow(PAVLO);
    expect(t.h.status.error).toBe("Share a whole screen to let a guest control it.");
    const u = host();
    await u.h.share("s1");
    await u.h.allow(PAVLO);
    expect(u.h.status.error).toBe("Pavlo may not control your screen.");
    expect(u.deps.begin).not.toHaveBeenCalled();
  });

  it("asks the host which monitor when several fit, once per share", async () => {
    const twin: Monitor = { id: "\\\\.\\DISPLAY3", rect: { x: -2560, y: 0, w: 2560, h: 1440 }, primary: false };
    const t = host({ monitors: vi.fn(async () => [MAIN, twin]) });
    await t.h.share("s1");
    t.h.setAllowed([PAVLO]);
    await vi.waitFor(() => expect(t.h.status.choose?.monitors.map((m) => m.id)).toEqual([MAIN.id, twin.id]));
    expect(t.deps.begin).not.toHaveBeenCalled();
    await t.h.allow(PAVLO, twin.id);
    expect(t.log).toEqual(["begin -2560,0 2560x1440"]);
    expect(t.h.status.choose).toBeNull();
    // Given back and taken again: the same screen, without asking.
    t.room.emit("dataReceived", data({}), { identity: "seat:p" }, 0, TOPIC.release);
    await vi.waitFor(() => expect(t.h.status.controller).toBeNull());
    t.room.emit("dataReceived", data({}), { identity: "seat:p" }, 0, TOPIC.ask);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    expect(t.log[t.log.length - 1]).toBe("begin -2560,0 2560x1440");
  });
});

describe("placing the guest's pointer", () => {
  it("matches monitors by shape", () => {
    expect(matchingMonitors([MAIN, SIDE], 1280, 720)).toEqual([MAIN]);
    expect(matchingMonitors([MAIN, SIDE], 1280, 1024)).toEqual([SIDE]);
  });

  it("finds the spot in a letterboxed picture, and nothing in the bars", () => {
    // A 1000x500 box showing a 16:9 picture: 1000x562.5 would not fit, so 888.9x500, centred.
    const box = { left: 0, top: 0, width: 1000, height: 500 };
    expect(pictureSpot(500, 250, box, 1920, 1080)).toEqual({ x: 0.5, y: 0.5 });
    expect(pictureSpot(20, 250, box, 1920, 1080)).toBeNull();
    expect(pictureSpot(500, 250, box, 0, 0)).toBeNull();
  });
});

function guest(over: Partial<GuestDeps> = {}) {
  const rooms: ReturnType<typeof fakeRoom>[] = [];
  const deps: GuestDeps = {
    newRoom: () => {
      const r = fakeRoom();
      rooms.push(r);
      return r as never;
    },
    token: vi.fn(async () => ({ url: "wss://lk", token: "guest-token", control: false })),
    ...over,
  };
  const states: string[] = [];
  const g = new GuestScreen(deps, "seat-1", (s) => states.push(s.state), async () => undefined);
  return { g, deps, rooms, states };
}

describe("the guest's view", () => {
  it("waits for the host, then shows only the host's video", async () => {
    const t = guest();
    await t.g.open();
    expect(t.g.state.state).toBe("waiting");
    const room = t.rooms[0];
    room.emit("trackSubscribed", { kind: "video" }, {}, { identity: "seat:other" });
    expect(t.g.track).toBeNull();
    const video = { kind: "video" };
    room.emit("trackSubscribed", video, {}, { identity: "host" });
    expect(t.g.track).toBe(video);
    expect(t.g.state.state).toBe("watching");
    room.emit("trackUnsubscribed", video);
    expect(t.g.state.state).toBe("waiting");
  });

  it("comes back with a new token when taken out of the room, and stops when refused", async () => {
    const token = vi
      .fn<GuestDeps["token"]>()
      .mockResolvedValueOnce({ url: "wss://lk", token: "t1", control: false })
      .mockResolvedValueOnce({ url: "wss://lk", token: "t2", control: true })
      .mockRejectedValueOnce(new Error("The host has not let you see their screen."));
    const t = guest({ token });
    await t.g.open();
    t.rooms[0].emit("disconnected");
    await vi.waitFor(() => expect(t.g.state).toMatchObject({ state: "waiting", control: true }));
    t.rooms[1].emit("disconnected");
    await vi.waitFor(() => expect(t.g.state).toMatchObject({ state: "off", error: "The host has not let you see their screen." }));
  });

  it("leaving closes the room and does not come back", async () => {
    const t = guest();
    await t.g.open();
    await t.g.close();
    t.rooms[0].emit("disconnected");
    expect(t.rooms[0].disconnect).toHaveBeenCalled();
    expect(t.deps.token).toHaveBeenCalledTimes(1);
  });

  it("asks, controls only on the host's word, and sends its events to the host alone", async () => {
    const t = guest({ token: vi.fn(async () => ({ url: "wss://lk", token: "t", control: true })) });
    await t.g.open();
    const room = t.rooms[0];
    await t.g.send([{ t: "move", x: 0.1, y: 0.1 }]);
    expect(room.sent).toEqual([]); // not controlling: nothing goes out
    await t.g.ask();
    expect(room.sent).toEqual([{ topic: TOPIC.ask, to: ["host"], body: {} }]);
    expect(t.g.state.asked).toBe(true);
    room.emit("dataReceived", data({ on: true }), { identity: "seat:other" }, 0, TOPIC.control);
    expect(t.g.state.controlling).toBe(false); // only the host's word counts
    room.emit("dataReceived", data({ on: true }), { identity: "host" }, 0, TOPIC.control);
    expect(t.g.state).toMatchObject({ controlling: true, asked: false });
    await t.g.send([{ t: "key_down", code: "KeyA" }]);
    expect(room.sent[room.sent.length - 1]).toEqual({ topic: TOPIC.input, to: ["host"], body: { events: [{ t: "key_down", code: "KeyA" }] } });
    room.emit("dataReceived", data({ on: false }), { identity: "host" }, 0, TOPIC.control);
    expect(t.g.state.controlling).toBe(false);
  });

  it("shows frames at once, and sends moves lossy but clicks and keys reliably", async () => {
    const t = guest({ token: vi.fn(async () => ({ url: "wss://lk", token: "t", control: true })) });
    await t.g.open();
    const room = t.rooms[0];
    const video = { kind: "video", setPlayoutDelay: vi.fn() };
    room.emit("trackSubscribed", video, {}, { identity: "host" });
    expect(video.setPlayoutDelay).toHaveBeenCalledWith(0);
    room.emit("dataReceived", data({ on: true }), { identity: "host" }, 0, TOPIC.control);
    await t.g.send([{ t: "move", x: 0.1, y: 0.1 }]);
    await t.g.send([{ t: "down", button: 0, x: 0.1, y: 0.1 }]);
    await t.g.send([{ t: "key_down", code: "KeyA" }]);
    const reliable = vi.mocked(room.localParticipant.publishData).mock.calls.map((c) => (c[1] as unknown as { reliable: boolean }).reliable);
    expect(reliable).toEqual([false, true, true]);
  });

  it("without control, cannot even ask", async () => {
    const t = guest();
    await t.g.open();
    await t.g.ask();
    expect(t.rooms[0].sent).toEqual([]);
  });
});

describe("how fast the guest sees the screen", () => {
  const report = (decoded: number, decodeS: number, emitted: number, bufferS: number) => [
    { type: "transport", id: "T1", selectedCandidatePairId: "CP2" },
    { type: "candidate-pair", id: "CP1", nominated: true, state: "succeeded", currentRoundTripTime: 0.5 },
    { type: "candidate-pair", id: "CP2", nominated: true, state: "succeeded", currentRoundTripTime: 0.042 },
    { type: "codec", id: "C1", mimeType: "video/H264" },
    {
      type: "inbound-rtp", id: "IV", kind: "video", codecId: "C1", framesPerSecond: 59.6,
      framesDecoded: decoded, totalDecodeTime: decodeS, jitterBufferEmittedCount: emitted, jitterBufferDelay: bufferS,
    },
  ];

  it("reads the network, frames, codec, and per-frame decode and buffer time over the last second", () => {
    const first = readStats(report(100, 0.2, 100, 1), null);
    expect(first.stats).toEqual({ networkMs: 42, fps: 60, decodeMs: null, bufferMs: null, codec: "H264" });
    const next = readStats(report(160, 0.32, 160, 1.6), first.sample);
    expect(next.stats).toMatchObject({ decodeMs: 2, bufferMs: 10 });
  });

  it("falls back to the nominated pair, and says nothing it does not know", () => {
    const r = readStats([{ type: "candidate-pair", id: "CP", nominated: true, state: "succeeded", currentRoundTripTime: 0.03 }], null);
    expect(r.stats).toEqual({ networkMs: 30, fps: null, decodeMs: null, bufferMs: null, codec: null });
    expect(r.sample).toBeNull();
  });

  it("puts it in plain words, leaving out the unknown", () => {
    expect(statsLine({ inputMs: 61.4, networkMs: 42, fps: 60, decodeMs: 2, bufferMs: null, codec: "H264" })).toBe(
      "Your input 61 ms there and back · Network 42 ms · 60 fps · Decode 2 ms · H264",
    );
    expect(statsLine({ inputMs: null, networkMs: null, fps: null, decodeMs: null, bufferMs: null, codec: null })).toBe("");
  });

  it("the host sends the controlling guest's ping straight back, and no one else's", async () => {
    const t = host({}, fakeScreenTrack("monitor", 1920, 1080));
    t.h.setAllowed([PAVLO, BLAIR]);
    await t.h.share("s1");
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    t.room.sent.length = 0;
    t.room.emit("dataReceived", data({ t: 5 }), { identity: "seat:b" }, 0, TOPIC.ping); // may, but does not control
    t.room.emit("dataReceived", data({ t: 7 }), { identity: "seat:p" }, 0, TOPIC.ping);
    await vi.waitFor(() => expect(t.room.sent).toEqual([{ topic: TOPIC.pong, to: ["seat:p"], body: { t: 7 } }]));
    expect(t.deps.input).not.toHaveBeenCalled();
  });

  it("the guest pings only while controlling, and times the host's answer", async () => {
    const rooms: ReturnType<typeof fakeRoom>[] = [];
    let now = 1000;
    const g = new GuestScreen(
      { newRoom: () => (rooms.push(fakeRoom()), rooms[0] as never), token: async () => ({ url: "wss://lk", token: "t", control: true }) },
      "seat-1",
      () => undefined,
      async () => undefined,
      () => now,
    );
    await g.open();
    const room = rooms[0];
    await g.ping();
    expect(room.sent).toEqual([]);
    room.emit("dataReceived", data({ on: true }), { identity: "host" }, 0, TOPIC.control);
    await g.ping();
    expect(room.sent).toEqual([{ topic: TOPIC.ping, to: ["host"], body: { t: 1000 } }]);
    now = 1055;
    room.emit("dataReceived", data({ t: 1000 }), { identity: "seat:other" }, 0, TOPIC.pong);
    expect(g.inputMs).toBeNull(); // only the host's answer counts
    room.emit("dataReceived", data({ t: 1000 }), { identity: "host" }, 0, TOPIC.pong);
    expect(g.inputMs).toBe(55);
    room.emit("dataReceived", data({ on: false }), { identity: "host" }, 0, TOPIC.control);
    expect(g.inputMs).toBeNull();
  });
});

describe("screen grants", () => {
  it("control needs the screen, and goes with it", () => {
    expect(withCapability(["view"], "device.control", true)).toEqual(["view", "device.view", "device.control"]);
    expect(withCapability(["view", "device.view", "device.control"], "device.view", false)).toEqual(["view"]);
    expect(withCapability(["view", "device.view", "device.control"], "device.control", false)).toEqual(["view", "device.view"]);
  });
});
