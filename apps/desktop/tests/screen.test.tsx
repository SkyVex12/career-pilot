// Guest seats' screen (src/screen.ts): the host shares only what they picked, from the click,
// and stops cleanly; a guest watches only the host's video, and comes back by itself when
// taken out of the room while it may still watch. LiveKit and the picker are faked.
import { describe, expect, it, vi } from "vitest";
import { GuestScreen, HostScreen, type GuestDeps, type HostDeps, type ScreenStatus } from "../src/screen";
import { withCapability } from "../src/seats";

type Handler = (...args: unknown[]) => void;

function fakeRoom() {
  const handlers = new Map<string, Handler[]>();
  return {
    handlers,
    on(event: string, f: Handler) {
      handlers.set(event, [...(handlers.get(event) ?? []), f]);
      return this;
    },
    emit(event: string, ...args: unknown[]) {
      for (const f of handlers.get(event) ?? []) f(...args);
    },
    connect: vi.fn(async () => undefined),
    disconnect: vi.fn(async () => undefined),
    localParticipant: { publishTrack: vi.fn(async () => undefined) },
  };
}

function fakeScreenTrack(surface = "monitor") {
  const ended: (() => void)[] = [];
  return {
    kind: "video",
    stop: vi.fn(),
    ended: () => ended.forEach((f) => f()),
    mediaStreamTrack: {
      getSettings: () => ({ displaySurface: surface }),
      addEventListener: (_: string, f: () => void) => ended.push(f),
    },
  };
}

function host(over: Partial<HostDeps> = {}) {
  const room = fakeRoom();
  const track = fakeScreenTrack();
  const deps: HostDeps = {
    newRoom: () => room as never,
    capture: vi.fn(async () => [track as never]),
    token: vi.fn(async () => ({ url: "wss://lk", token: "host-token" })),
    ...over,
  };
  const statuses: ScreenStatus[] = [];
  const h = new HostScreen(deps, (s) => statuses.push(s));
  return { h, deps, room, track, statuses };
}

describe("the host's screen", () => {
  it("opens the picker first, then joins and shares that track, with no audio", async () => {
    const t = host();
    await t.h.share("s1");
    expect(t.deps.capture).toHaveBeenCalledBefore(t.deps.token as never);
    expect(t.deps.token).toHaveBeenCalledWith("s1");
    expect(t.room.connect).toHaveBeenCalledWith("wss://lk", "host-token", { autoSubscribe: false });
    expect(t.room.localParticipant.publishTrack).toHaveBeenCalledWith(t.track, expect.objectContaining({ source: "screen_share" }));
    expect(t.h.status).toEqual({ host: "on", surface: "monitor", error: null });
  });

  it("a closed picker is no error and joins nothing", async () => {
    const t = host({ capture: vi.fn(async () => Promise.reject(new DOMException("denied", "NotAllowedError"))) });
    await t.h.share("s1");
    expect(t.h.status).toEqual({ host: "off", surface: null, error: null });
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
    await Promise.resolve();
    expect(t.room.disconnect).toHaveBeenCalled();
    expect(t.h.status.host).toBe("off");
    const u = host();
    await u.h.share("s1");
    await u.h.stop(null);
    expect(u.track.stop).toHaveBeenCalled();
    expect(u.h.sharing).toBe(false);
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
});

describe("screen grants", () => {
  it("control needs the screen, and goes with it", () => {
    expect(withCapability(["view"], "device.control", true)).toEqual(["view", "device.view", "device.control"]);
    expect(withCapability(["view", "device.view", "device.control"], "device.view", false)).toEqual(["view"]);
    expect(withCapability(["view", "device.view", "device.control"], "device.control", false)).toEqual(["view", "device.view"]);
  });
});
