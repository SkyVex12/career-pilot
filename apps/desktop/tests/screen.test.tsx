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
  sharedMonitor,
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
import { nativeShares, NATIVE_SCREEN_SIZE } from "../src/nativeScreen";

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
    connect: vi.fn(async (): Promise<void> => undefined),
    disconnect: vi.fn(async () => undefined),
    localParticipant: {
      publishTrack: vi.fn(async () => undefined),
      publishData: vi.fn(async (data: Uint8Array, o: { topic: string; destinationIdentities: string[] }) => {
        sent.push({ topic: o.topic, to: o.destinationIdentities, body: JSON.parse(new TextDecoder().decode(data)) });
      }),
    },
  };
}

function fakeScreenTrack(surface = "monitor", width = 1920, height = 1080, label = "") {
  const handlers = new Map<string, (() => void)[]>();
  const dispatch = (name: string) => handlers.get(name)?.forEach(f => f());
  return {
    kind: "video",
    stop: vi.fn(),
    ended: () => dispatch("ended"),
    dispatch,
    mediaStreamTrack: {
      label,
      getSettings: () => ({ displaySurface: surface, width, height }),
      addEventListener: (name: string, f: () => void) => handlers.set(name, [...(handlers.get(name) ?? []), f]),
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
let requestSequence = 0;
function requestControl(t: ReturnType<typeof host>, person = PAVLO) {
  const requestId = `request-${++requestSequence}`;
  t.room.emit("dataReceived", data({ requestId }), { identity: person.identity }, 0, TOPIC.ask);
  return t.h.status.asking.find(p => p.requestId === requestId)!;
}
async function approve(t: ReturnType<typeof host>, person = PAVLO) {
  const request = requestControl(t, person);
  expect(request).toBeDefined();
  await t.h.allow(request);
}
async function permitGuest(g: GuestScreen, room: ReturnType<typeof fakeRoom>) {
  await g.ask();
  const body = [...room.sent].reverse().find(p => p.topic === TOPIC.ask)?.body as { requestId: string };
  room.emit("dataReceived", data({ on: true, requestId: body.requestId }), { identity: "host" }, 0, TOPIC.control);
}


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
  // Eligibility alone never starts remote input: a matching host decision is required.
  async function controlling() {
    const t = host({}, fakeScreenTrack("monitor", 1920, 1080)); // 16:9: the main monitor
    t.h.setAllowed([PAVLO, BLAIR]);
    await t.h.share("s1");
    await approve(t);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    return t;
  }

  it("begins on the shared monitor and tells the guest", async () => {
    const t = await controlling();
    expect(t.log).toEqual(["begin 0,0 2560x1440"]);
    expect(t.h.status.controller).toEqual(PAVLO);
    expect(t.room.sent).toContainEqual({ topic: TOPIC.control, to: ["seat:p"], body: { on: true, requestId: expect.any(String) } });
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
    await approve(t);
    await vi.waitFor(() => expect(params).toMatchObject({ encodings: [{ maxFramerate: 60, maxBitrate: ENCODING.control.maxBitrate }], degradationPreference: "balanced" }));
    await t.h.endControl(null, "host");
    expect(params).toMatchObject({ encodings: [{ maxFramerate: 30, maxBitrate: ENCODING.watch.maxBitrate }], degradationPreference: "maintain-resolution" });
  });

  it("revocation releases held input without passing control to another eligible guest", async () => {
    const t = await controlling();
    t.h.setAllowed([BLAIR]);
    await vi.waitFor(() => expect(t.h.status.controller).toBeNull());
    expect(t.log).toEqual(["begin 0,0 2560x1440", "end"]);
    await approve(t, BLAIR);
    expect(t.h.status.controller).toEqual(BLAIR);
    await t.h.stop(null);
    expect(t.log[t.log.length - 1]).toBe("end");
  });

  it("does not auto-control on capability changes, release, or a new participant", async () => {
    const t = await controlling();
    await t.h.endControl(null, "host");
    t.h.setAllowed([]); t.h.setAllowed([PAVLO]);
    t.room.emit("participantConnected", { identity: PAVLO.identity });
    expect(t.h.status.controller).toBeNull();
    await approve(t);
    t.room.emit("dataReceived", data({}), { identity: PAVLO.identity }, 0, TOPIC.release);
    await vi.waitFor(() => expect(t.h.status.controller).toBeNull());
    expect(t.deps.begin).toHaveBeenCalledTimes(2);
  });

  it("a view-only guest requests once; no input runs before the host grants and approves", async () => {
    const t = host(); t.h.setAllowed([], [PAVLO]); await t.h.share("s1");
    const request = requestControl(t);
    t.room.emit("dataReceived", data({ requestId: "flood-attempt" }), { identity: PAVLO.identity }, 0, TOPIC.ask);
    t.room.emit("dataReceived", data({ events: [{ t: "key_down", code: "KeyA" }] }), { identity: PAVLO.identity }, 0, TOPIC.input);
    expect(t.h.status.asking).toEqual([request]); expect(t.deps.input).not.toHaveBeenCalled();
    await t.h.allow(request); expect(t.deps.begin).not.toHaveBeenCalled();
    t.h.setAllowed([PAVLO], [PAVLO]);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    await t.h.stop(null);
  });

  it("declines and expires requests and rejects stale approval after disconnection", async () => {
    const t = host(); t.h.setAllowed([PAVLO]); await t.h.share("s1");
    const first = requestControl(t); await t.h.decline(first); await t.h.allow(first);
    expect(t.deps.begin).not.toHaveBeenCalled(); expect(t.h.status.asking).toEqual([]);
    t.tick(10_001); const expired = requestControl(t); t.tick(60_001); await t.h.allow(expired);
    expect(t.deps.begin).not.toHaveBeenCalled();
    t.room.emit("participantDisconnected", { identity: PAVLO.identity });
    expect(t.h.status.asking).toEqual([]); await t.h.allow(expired);
    expect(t.deps.begin).not.toHaveBeenCalled();
  });

  it("only authenticated current viewers can ask and window shares reject control", async () => {
    const t = host({}, fakeScreenTrack("window")); t.h.setAllowed([PAVLO]); await t.h.share("s1");
    requestControl(t); requestControl(t, BLAIR);
    expect(t.h.status.asking).toEqual([]); expect(t.deps.begin).not.toHaveBeenCalled();
    expect(t.room.sent).toContainEqual(expect.objectContaining({ to: [PAVLO.identity], body: expect.objectContaining({ on: false, reason: expect.stringMatching(/entire display/) }) }));
  });

  it("uses the shared display identity even when two monitors have identical dimensions", async () => {
    const twin: Monitor = { id: "\\\\.\\DISPLAY3", rect: { x: -2560, y: 0, w: 2560, h: 1440 }, primary: false, capture_id: 7 };
    const t = host({ monitors: vi.fn(async () => [{ ...MAIN, capture_id: 2 }, twin]) }, fakeScreenTrack("monitor", 1920, 1080, "screen:7:0"));
    await t.h.share("s1");
    t.h.setAllowed([PAVLO]);
    await approve(t);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    expect(t.log).toEqual(["begin -2560,0 2560x1440"]);
    // Given back and taken again: the same screen, without asking.
    t.room.emit("dataReceived", data({}), { identity: "seat:p" }, 0, TOPIC.release);
    await vi.waitFor(() => expect(t.h.status.controller).toBeNull());
    await approve(t);
    await vi.waitFor(() => expect(t.h.status.controller).toEqual(PAVLO));
    expect(t.log[t.log.length - 1]).toBe("begin -2560,0 2560x1440");
  });

  it("keeps an unidentified display view-only without offering a second picker", async () => {
    const twin = { ...MAIN, id: "another-display", primary: false };
    const t = host({ monitors: vi.fn(async () => [MAIN, twin]) });
    await t.h.share("s1");
    t.h.setAllowed([PAVLO]);
    await approve(t);
    await vi.waitFor(() => expect(t.h.status.error).toMatch(/shared display could not be identified/));
    expect(t.h.status.host).toBe("on");
    expect(t.deps.begin).not.toHaveBeenCalled();
  });

  it("does not move control to a changed display after capture starts", async () => {
    const t = host();
    await t.h.share("s1");
    vi.mocked(t.deps.monitors).mockResolvedValue([{ ...MAIN, rect: { ...MAIN.rect, x: -2560 } }, SIDE]);
    t.h.setAllowed([PAVLO]);
    await approve(t);
    await vi.waitFor(() => expect(t.h.status.error).toMatch(/shared display could not be identified/));
    expect(t.deps.begin).not.toHaveBeenCalled();
  });

  it("cannot grant control after sharing stops while native begin is pending", async () => {
    let finish!: () => void;
    const t = host({ begin: vi.fn(() => new Promise<void>((resolve) => { finish = resolve; })) });
    await t.h.share("s1");
    t.h.setAllowed([PAVLO]);
    void approve(t);
    await vi.waitFor(() => expect(t.deps.begin).toHaveBeenCalled());
    await t.h.stop(null);
    finish();
    await vi.waitFor(() => expect(t.deps.end).toHaveBeenCalledTimes(2));
    expect(t.h.status.host).toBe("off");
    expect(t.h.status.controller).toBeNull();
    expect(t.room.sent).not.toContainEqual(expect.objectContaining({ body: { on: true } }));
  });
});

describe("screen resolution and consent races", () => {
  it("binds approvals and permission updates to the exact shared call", async () => {
    const t = host(); t.h.setAllowed([PAVLO], [PAVLO], "s1"); await t.h.share("s1");
    const request = requestControl(t);
    await t.h.allow({ ...request, sessionId: "old-call" });
    expect(t.deps.begin).not.toHaveBeenCalled();
    t.h.setAllowed([], [], "old-call");
    await t.h.allow(request);
    expect(t.h.status.controller).toEqual(PAVLO);
    t.h.setAllowed([], [], "s1");
    expect(t.h.status.controller).toBeNull();
    await t.h.stop(null);
  });

  it("never trusts an extended deadline supplied by an old approval UI", async () => {
    const t = host(); t.h.setAllowed([PAVLO]); await t.h.share("s1"); const request = requestControl(t);
    t.tick(60_001);
    await t.h.allow({ ...request, expiresAt: 9_999_999 });
    expect(t.deps.begin).not.toHaveBeenCalled();
    await t.h.stop(null);
  });

  it("an old controller's late input rejection cannot stop a newly approved controller", async () => {
    let rejectInput!: (error: Error) => void;
    const t = host({ input: vi.fn(() => new Promise<void>((_resolve, reject) => { rejectInput = reject; })) });
    t.h.setAllowed([PAVLO, BLAIR]); await t.h.share("s1"); await approve(t, PAVLO);
    t.room.emit("dataReceived", data({ events: [{ t: "key_down", code: "KeyA" }] }), { identity: PAVLO.identity }, 0, TOPIC.input);
    await vi.waitFor(() => expect(rejectInput).toBeDefined());
    const handoff = approve(t, BLAIR);
    await vi.waitFor(() => expect(t.h.status.controller).toBeNull());
    expect(t.deps.begin).toHaveBeenCalledTimes(1);
    rejectInput(new Error("The previous input grant ended"));
    await handoff;
    expect(t.h.status.controller).toEqual(BLAIR);
    expect(t.h.status.stopped).toEqual([]);
    await t.h.stop(null);
  });

  it("updates native capture and encoder bitrate for a viewer, but never executes their input", async () => {
    const track = fakeScreenTrack();
    const setQuality = vi.fn(async () => undefined);
    nativeShares.set(track.mediaStreamTrack as unknown as MediaStreamTrack, { id: 7, kind: "monitor", title: "Display", monitor_id: MAIN.id, setQuality });
    const sender = { getParameters: () => ({ encodings: [{}] }), setParameters: vi.fn(async () => undefined) };
    const t = host({}, Object.assign(track, { sender }));
    t.h.setAllowed([], [PAVLO]); await t.h.share("s1");
    t.room.emit("dataReceived", data({ quality: "2160p" }), { identity: "seat:other" }, 0, TOPIC.quality);
    expect(setQuality).not.toHaveBeenCalled();
    t.room.emit("dataReceived", data({ quality: "2160p" }), { identity: PAVLO.identity }, 0, TOPIC.quality);
    await vi.waitFor(() => expect(setQuality).toHaveBeenCalledWith(2160));
    await vi.waitFor(() => expect(sender.setParameters).toHaveBeenCalledWith(expect.objectContaining({ encodings: [expect.objectContaining({ maxBitrate: 18_000_000 })] })));
    expect(t.room.sent).toContainEqual(expect.objectContaining({ topic: TOPIC.qualityStatus, body: expect.objectContaining({ quality: "2160p" }) }));
    expect(t.deps.begin).not.toHaveBeenCalled(); expect(t.deps.input).not.toHaveBeenCalled();
    t.tick(1001);
    t.room.emit("dataReceived", data({ quality: "720p" }), { identity: PAVLO.identity }, 0, TOPIC.quality);
    await vi.waitFor(() => expect(setQuality).toHaveBeenLastCalledWith(720));
    expect(sender.setParameters).toHaveBeenLastCalledWith(expect.objectContaining({ encodings: [expect.objectContaining({ maxBitrate: 3_000_000 })] }));
    await t.h.stop(null);
  });

  it("reports actual native frame dimensions after a deferred resize and ignores a retired capture", async () => {
    const track = fakeScreenTrack();
    const native = { id: 7, kind: "monitor" as const, title: "Display", monitor_id: MAIN.id,
      dimensions: { width: 1920, height: 1080 }, setQuality: vi.fn(async () => undefined) };
    nativeShares.set(track.mediaStreamTrack as unknown as MediaStreamTrack, native);
    const t = host({}, track); t.h.setAllowed([], [PAVLO]); await t.h.share("s1");
    t.room.emit("dataReceived", data({ quality: "2160p" }), { identity: PAVLO.identity }, 0, TOPIC.quality);
    await vi.waitFor(() => expect(t.room.sent).toContainEqual(expect.objectContaining({ topic: TOPIC.qualityStatus,
      body: { quality: "2160p", width: 1920, height: 1080 } })));
    // The first resized frame can be source-limited or portrait: report its
    // exact size, never a preset and never stale canvas getSettings().
    native.dimensions = { width: 1440, height: 2160 };
    track.dispatch(NATIVE_SCREEN_SIZE);
    await vi.waitFor(() => expect(t.room.sent).toContainEqual(expect.objectContaining({ topic: TOPIC.qualityStatus,
      body: { quality: "2160p", width: 1440, height: 2160 } })));
    await t.h.stop(null);
    const count = t.room.sent.length;
    native.dimensions = { width: 3840, height: 2160 };
    track.dispatch(NATIVE_SCREEN_SIZE);
    await Promise.resolve();
    expect(t.room.sent).toHaveLength(count);
  });

  it("reports capture quality failures without claiming the requested resolution succeeded", async () => {
    const track = fakeScreenTrack();
    nativeShares.set(track.mediaStreamTrack as unknown as MediaStreamTrack, { id: 7, kind: "monitor", title: "Display", monitor_id: MAIN.id,
      setQuality: async () => { throw new Error("Capture unavailable"); } });
    const t = host({}, track); t.h.setAllowed([], [PAVLO]); await t.h.share("s1");
    t.room.emit("dataReceived", data({ quality: "2160p" }), { identity: PAVLO.identity }, 0, TOPIC.quality);
    await vi.waitFor(() => expect(t.room.sent).toContainEqual(expect.objectContaining({ topic: TOPIC.qualityStatus,
      to: [PAVLO.identity], body: expect.objectContaining({ quality: "1080p", error: "Capture unavailable" }) })));
    await t.h.stop(null);
  });

  it("declining during native begin prevents control and releases native input", async () => {
    let finish!: () => void;
    const t = host({ begin: vi.fn(() => new Promise<void>(done => { finish = done; })) });
    t.h.setAllowed([PAVLO]); await t.h.share("s1"); const request = requestControl(t);
    const allowing = t.h.allow(request); await vi.waitFor(() => expect(finish).toBeDefined());
    await t.h.decline(request); finish(); await allowing;
    expect(t.h.status.controller).toBeNull(); expect(t.deps.end).toHaveBeenCalled();
    expect(t.room.sent.some(item => (item.body as { on?: boolean }).on === true)).toBe(false);
    await t.h.stop(null);
  });

  it("a revoked and re-added capability cannot revive an approval in flight", async () => {
    let finish!: () => void;
    const t = host({ begin: vi.fn(() => new Promise<void>(done => { finish = done; })) });
    t.h.setAllowed([PAVLO]); await t.h.share("s1"); const request = requestControl(t);
    const allowing = t.h.allow(request); await vi.waitFor(() => expect(finish).toBeDefined());
    t.h.setAllowed([], [PAVLO]); t.h.setAllowed([PAVLO]); finish(); await allowing;
    expect(t.h.status.controller).toBeNull(); expect(t.deps.end).toHaveBeenCalled();
    await t.h.stop(null);
  });

  it("expires unanswered requests and ignores approval after the deadline", async () => {
    vi.useFakeTimers();
    try {
      const t = host(); t.h.setAllowed([PAVLO]); await t.h.share("s1"); const request = requestControl(t);
      t.tick(60_001); await vi.advanceTimersByTimeAsync(60_001); await t.h.allow(request);
      expect(t.h.status.asking).toEqual([]); expect(t.deps.begin).not.toHaveBeenCalled();
      expect(t.room.sent).toContainEqual(expect.objectContaining({ body: expect.objectContaining({ on: false, reason: "Request expired" }) }));
      await t.h.stop(null);
    } finally { vi.useRealTimers(); }
  });
});

describe("placing the guest's pointer", () => {
  it("never guesses another display when a captured source disappears or is a native-picker session", () => {
    const monitors = [{ ...MAIN, capture_id: 2 }];
    for (const label of ["screen:7:0", "screen:2:0:s", "screen:-1:0"]) {
      expect(sharedMonitor(monitors, fakeScreenTrack("monitor", 1920, 1080, label).mediaStreamTrack as never)).toBeNull();
    }
  });

  it("keeps the native Mac display identity instead of choosing an identical monitor", () => {
    const track = fakeScreenTrack().mediaStreamTrack as unknown as MediaStreamTrack;
    nativeShares.set(track, { id: 7, kind: "monitor", title: "External display" });
    const native = { ...MAIN, id: "7", primary: false };
    expect(sharedMonitor([MAIN, native], track)).toEqual(native);
    expect(sharedMonitor([MAIN], track)).toBeNull();
  });

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
    expect(room.sent).toEqual([{ topic: TOPIC.ask, to: ["host"], body: { requestId: expect.any(String) } }]);
    expect(t.g.state.asked).toBe(true);
    room.emit("dataReceived", data({ on: true }), { identity: "seat:other" }, 0, TOPIC.control);
    expect(t.g.state.controlling).toBe(false); // only the host's word counts
    await permitGuest(t.g, room);
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
    await permitGuest(t.g, room);
    await t.g.send([{ t: "move", x: 0.1, y: 0.1 }]);
    await t.g.send([{ t: "down", button: 0, x: 0.1, y: 0.1 }]);
    await t.g.send([{ t: "key_down", code: "KeyA" }]);
    const reliable = vi.mocked(room.localParticipant.publishData).mock.calls.map((c) => (c[1] as unknown as { reliable: boolean }).reliable);
    expect(reliable.slice(-3)).toEqual([false, true, true]);
  });

  it("without control, cannot even ask", async () => {
    const t = guest();
    await t.g.open();
    await t.g.ask();
    expect(t.rooms[0].sent).toEqual([]);
  });
});

it("a view-only viewer can request, but stale, unsolicited, and post-reconnect approvals never enable input", async () => {
  const t = guest({ token: vi.fn(async () => ({ url: "wss://lk", token: "t", control: false, can_request_control: true })) });
  await t.g.open(); const room = t.rooms[0];
  room.emit("dataReceived", data({ on: true, requestId: "unsolicited" }), { identity: "host" }, 0, TOPIC.control);
  expect(t.g.state.controlling).toBe(false);
  await t.g.ask(); const pending = room.sent[0].body as { requestId: string };
  room.emit("dataReceived", data({ on: true, requestId: "different-id" }), { identity: "host" }, 0, TOPIC.control);
  expect(t.g.state.controlling).toBe(false);
  room.emit("reconnecting");
  room.emit("dataReceived", data({ on: true, requestId: pending.requestId }), { identity: "host" }, 0, TOPIC.control);
  expect(t.g.state.controlling).toBe(false);
  await t.g.close();
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
    await approve(t);
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
    await permitGuest(g, room); room.sent.length = 0;
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


describe("native Mac capture failures", () => {
  it("shows permission guidance instead of treating it as picker cancellation", async () => {
    const t = host({ capture: vi.fn(async () => { throw new Error("Allow Screen Recording in System Settings."); }) });
    await t.h.share("session");
    expect(t.h.status.host).toBe("off");
    expect(t.h.status.error).toBe("Allow Screen Recording in System Settings.");
  });
});


describe("sharing and viewing cancellation", () => {
  it("stops a native capture that arrives after the call ended", async () => {
    let resolve!: (tracks: never[]) => void;
    const capture = vi.fn(() => new Promise<never[]>(done => { resolve = done; }));
    const t = host({ capture });
    const opening = t.h.share("s1"); await t.h.stop(null);
    const late = fakeScreenTrack(); resolve([late as never]); await opening;
    expect(late.stop).toHaveBeenCalled(); expect(t.deps.token).not.toHaveBeenCalled();
    expect(t.h.status.host).toBe("off");
  });

  it("does not publish after stop during room connection", async () => {
    const t = host(); let resolve!: () => void;
    t.room.connect.mockImplementation(() => new Promise<void>(done => { resolve = done; }));
    const opening = t.h.share("s1"); await vi.waitFor(() => expect(t.room.connect).toHaveBeenCalled());
    await t.h.stop(null); resolve(); await opening;
    expect(t.room.localParticipant.publishTrack).not.toHaveBeenCalled();
    expect(t.track.stop).toHaveBeenCalled(); expect(t.h.status.host).toBe("off");
  });

  it("ignores stale subscriptions and control after a receiver is closed", async () => {
    const t = guest({ token: vi.fn(async () => ({ url: "wss://lk", token: "t", control: true })) });
    await t.g.open(); const room = t.rooms[0]; await t.g.close();
    room.emit("trackSubscribed", { kind: "video" }, {}, { identity: "host" });
    room.emit("dataReceived", data({ on: true, requestId: "stale-request" }), { identity: "host" }, 0, TOPIC.control);
    expect(t.g.track).toBeNull(); expect(t.g.state.controlling).toBe(false);
  });

  it("never connects when a revoked seat token arrives late", async () => {
    let resolve!: (grant: { url: string; token: string }) => void;
    const t = guest({ token: vi.fn(() => new Promise<{ url: string; token: string }>(done => { resolve = done; })) });
    const opening = t.g.open(); await t.g.close(); resolve({ url: "wss://lk", token: "late" }); await opening;
    expect(t.rooms).toHaveLength(0);
  });

  it("keeps the retained video after LiveKit reconnects without a new track event", async () => {
    const t = guest(); await t.g.open(); const video = { kind: "video" };
    t.rooms[0].emit("trackSubscribed", video, {}, { identity: "host" });
    t.rooms[0].emit("reconnecting"); expect(t.g.state.state).toBe("connecting");
    t.rooms[0].emit("reconnected"); expect(t.g.state.state).toBe("watching"); expect(t.g.track).toBe(video);
  });

  it("maps a Windows native capture by monitor device name instead of equal display dimensions", () => {
    const video = fakeScreenTrack().mediaStreamTrack as unknown as MediaStreamTrack;
    const twin = { ...MAIN, id: "display-twin", rect: { ...MAIN.rect, x: -2560 } };
    nativeShares.set(video, { id: 7, kind: "monitor", title: "Laptop", monitor_id: twin.id });
    expect(sharedMonitor([MAIN, twin], video)).toEqual(twin);
    expect(sharedMonitor([MAIN], video)).toBeNull();
  });
});
