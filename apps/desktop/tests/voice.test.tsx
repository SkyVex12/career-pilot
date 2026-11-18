// Guest seats' voice in the window (src/voice.ts): the host hears guests on the device they
// picked, routed before anything plays, never on another device; a guest's microphone opens
// only while they talk. LiveKit and the audio elements are faked.
import { describe, expect, it, vi } from "vitest";
import { VoiceController, windowsName, type VoiceCmd, type VoiceDeps, type VoiceStatus } from "../src/voice";

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
    localParticipant: { setMicrophoneEnabled: vi.fn(async () => undefined) },
  };
}

function fakeAudio(log: string[], routable = true) {
  const el = {
    muted: false,
    src: "",
    srcObject: null as unknown,
    play: vi.fn(async () => {
      log.push(`play ${el.sink}`);
    }),
    pause: vi.fn(),
    sink: "",
  } as Record<string, unknown> & { sink: string; muted: boolean };
  if (routable) {
    el.setSinkId = vi.fn(async (id: string) => {
      el.sink = id;
      log.push(`route ${id}`);
    });
  }
  return el;
}

const HP = { kind: "audiooutput", deviceId: "d-hp", label: "Headphones (USB)", groupId: "" } as MediaDeviceInfo;
const DEFAULT = { kind: "audiooutput", deviceId: "default", label: "Default - Speakers", groupId: "" } as MediaDeviceInfo;

function setup(over: Partial<VoiceDeps> = {}) {
  const log: string[] = [];
  const room = fakeRoom();
  const audios: ReturnType<typeof fakeAudio>[] = [];
  let devices: MediaDeviceInfo[] = [DEFAULT, HP];
  let changed: (() => void) | null = null;
  const deps: VoiceDeps = {
    newRoom: () => room as never,
    newAudio: () => {
      const a = fakeAudio(log);
      audios.push(a);
      return a as never;
    },
    devices: async () => devices,
    unlockLabels: vi.fn(async () => undefined),
    hostToken: vi.fn(async () => ({ url: "wss://lk", token: "host-token" })),
    guestToken: vi.fn(async () => ({ url: "wss://lk", token: "guest-token" })),
    onDeviceChange: (f) => {
      changed = f;
      return () => {
        changed = null;
      };
    },
    ...over,
  };
  const statuses: VoiceStatus[] = [];
  const c = new VoiceController(deps, (s) => statuses.push(s));
  return {
    c, deps, room, log, audios, statuses,
    setDevices: (d: MediaDeviceInfo[]) => {
      devices = d;
    },
    deviceChange: () => changed?.(),
  };
}

const track = () => ({ kind: "audio", attach: vi.fn(), detach: vi.fn(() => []) });
const host: VoiceCmd = { cmd: "host_on", deviceId: "d-hp", sessionId: "s1", allowed: ["seat:a"] };
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("the host's voice", () => {
  it("makes sure the window can play on the device, then listens to guests", async () => {
    const t = setup();
    await t.c.handle(host);
    expect(t.log).toEqual(["route d-hp"]);
    expect(t.deps.hostToken).toHaveBeenCalledWith("s1");
    expect(t.room.connect).toHaveBeenCalledWith("wss://lk", "host-token", { autoSubscribe: true });
    expect(t.c.status.host).toBe("on");
  });

  it("a voice token the server refuses keeps voice off and says why", async () => {
    const t = setup({ hostToken: vi.fn(async () => Promise.reject("Voice is not set up on this server.")) });
    await t.c.handle(host);
    expect(t.c.status).toMatchObject({ host: "off", error: "Voice is not set up on this server." });
    expect(t.room.connect).not.toHaveBeenCalled();
  });

  it("a window that cannot route sound to a device gets no voice", async () => {
    const t = setup({ newAudio: () => fakeAudio([], false) as never });
    await t.c.handle(host);
    expect(t.c.status).toMatchObject({ host: "off", error: "This window cannot choose an output device." });
  });

  it("plays each guest routed to the headphones first, and only seats with voice", async () => {
    const t = setup();
    await t.c.handle(host);
    t.log.length = 0;
    const a = track();
    const b = track();
    t.room.emit("trackSubscribed", a, {}, { identity: "seat:a" });
    t.room.emit("trackSubscribed", b, {}, { identity: "seat:b" });
    await flush();
    expect(t.log).toEqual(["route d-hp", "route d-hp", "play d-hp", "play d-hp"]);
    const [elA, elB] = t.audios.slice(-2);
    expect([elA.muted, elB.muted]).toEqual([false, true]); // b's seat has no voice
    await t.c.handle({ cmd: "host_allowed", identities: ["seat:a", "seat:b"] });
    expect(elB.muted).toBe(false);
    await t.c.handle({ cmd: "host_mute", on: true });
    expect([elA.muted, elB.muted]).toEqual([true, true]);
  });

  it("stops, never falls back, when the headphones go away", async () => {
    const t = setup();
    await t.c.handle(host);
    t.setDevices([DEFAULT]);
    t.deviceChange();
    await flush();
    await flush();
    expect(t.c.status).toMatchObject({ host: "off", error: "The headphones went away, so guests' voice stopped." });
    expect(t.room.disconnect).toHaveBeenCalled();
  });

  it("uses Windows' default output when the headphones used last aren't there", async () => {
    const t = setup();
    await t.c.handle({ ...host, deviceId: "d-gone" } as VoiceCmd);
    expect(t.log).toEqual(["route "]);
    expect(t.c.status).toMatchObject({ host: "on", device: "" });
    // The default output is always there: a device change never stops it.
    t.setDevices([HP]);
    t.deviceChange();
    await flush();
    expect(t.c.status.host).toBe("on");
  });

  it("moves every guest to another device while it plays", async () => {
    const t = setup();
    await t.c.handle(host);
    t.room.emit("trackSubscribed", track(), {}, { identity: "seat:a" });
    await flush();
    t.log.length = 0;
    await t.c.handle({ cmd: "host_device", deviceId: "" });
    expect(t.log).toEqual(["route "]);
    expect(t.c.status).toMatchObject({ host: "on", device: "" });
  });

  it("lists fixed devices only, reading their names first if WebView2 hides them", async () => {
    const t = setup();
    t.setDevices([DEFAULT, { ...HP, label: "" } as MediaDeviceInfo]);
    t.deps.unlockLabels = vi.fn(async () => t.setDevices([DEFAULT, HP]));
    await t.c.handle({ cmd: "outputs" });
    expect(t.deps.unlockLabels).toHaveBeenCalled();
    expect(t.c.status.outputs).toEqual([{ deviceId: "d-hp", label: "Headphones (USB)" }]);
  });
});

describe("device names", () => {
  it("drops the USB id Chromium adds, so the name is Windows' own", () => {
    expect(windowsName("Speakers (Logi USB Headset) (046d:0a64)")).toBe("Speakers (Logi USB Headset)");
    expect(windowsName("Speakers (Realtek(R) Audio)")).toBe("Speakers (Realtek(R) Audio)");
  });
});

describe("the guest's voice", () => {
  it("joins without listening to anyone, and opens the microphone only while talking", async () => {
    const t = setup();
    await t.c.handle({ cmd: "guest_on", seatId: "seat-1" });
    expect(t.deps.guestToken).toHaveBeenCalledWith("seat-1");
    expect(t.room.connect).toHaveBeenCalledWith("wss://lk", "guest-token", { autoSubscribe: false });
    expect(t.room.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled();
    await t.c.handle({ cmd: "guest_talk", on: true });
    await t.c.handle({ cmd: "guest_talk", on: false });
    expect(t.room.localParticipant.setMicrophoneEnabled.mock.calls).toEqual([[true], [false]]);
    await t.c.handle({ cmd: "guest_off" });
    expect(t.c.status).toMatchObject({ guest: "off", talking: false });
  });

  it("cannot talk before it is connected", async () => {
    const t = setup();
    await t.c.handle({ cmd: "guest_talk", on: true });
    expect(t.c.status.error).toBe("Voice is not connected.");
  });
});
