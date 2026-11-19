// Guest seats' voice in the window (src/voice.ts): the host hears guests on the device they
// selected for the call, routed before anything plays; a guest's microphone opens
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
    callOutput: vi.fn(async () => ({ id: "wasapi:hp", name: HP.label, form_factor: "headphones" as const, private: true, call: true })),
    unlockLabels: vi.fn(async () => undefined),
    hostToken: vi.fn(async () => ({ url: "wss://lk", token: "host-token" })),
    guestToken: vi.fn(async () => ({ url: "wss://lk", token: "guest-token" })),
    onDeviceChange: vi.fn((f) => {
      changed = f;
      return () => {
        changed = null;
      };
    }),
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
const host: VoiceCmd = { cmd: "host_on", sessionId: "s1", allowed: ["seat:a"] };
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

  it("requires native confirmation of the system output when routing is unsupported", async () => {
    const t = setup({ newAudio: () => fakeAudio([], false) as never });
    await t.c.handle(host);
    expect(t.c.status).toMatchObject({ host: "off", error: expect.stringContaining("sound output") });
    expect(t.room.connect).not.toHaveBeenCalled();
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
    expect(t.c.status).toMatchObject({ host: "off", error: expect.stringContaining("disconnected") });
    expect(t.room.disconnect).toHaveBeenCalled();
  });

  it("routes to the call's USB headset even when Windows calls it speakers", async () => {
    const t = setup({ callOutput: async () => ({ id: "wasapi:logi", name: "Speakers (Logi USB Headset)", form_factor: "speakers", private: false, call: true }) });
    t.setDevices([DEFAULT, HP, { ...HP, deviceId: "d-logi", label: "Speakers (Logi USB Headset) (046d:0a64)" } as MediaDeviceInfo]);
    await t.c.handle(host);
    expect(t.log).toEqual(["route d-logi"]);
    expect(t.c.status).toMatchObject({ host: "on", device: "d-logi" });
  });

  it.each(["missing", "unavailable", "ambiguous"])("does not guess another output when the call device is %s", async (problem) => {
    const t = setup(problem === "missing" ? { callOutput: async () => null } : {});
    if (problem === "unavailable") t.setDevices([DEFAULT]);
    if (problem === "ambiguous") t.setDevices([DEFAULT, HP, { ...HP, deviceId: "duplicate" } as MediaDeviceInfo]);
    await t.c.handle(host);
    expect(t.c.status).toMatchObject({ host: "off", error: expect.stringContaining("Check Call audio") });
    expect(t.room.connect).not.toHaveBeenCalled();
    expect(t.log).toEqual([]);
  });

  it("lists fixed devices only, reading their names first if WebView2 hides them", async () => {
    const t = setup();
    t.setDevices([DEFAULT, { ...HP, label: "" } as MediaDeviceInfo]);
    t.deps.unlockLabels = vi.fn(async () => t.setDevices([DEFAULT, HP]));
    await t.c.handle({ cmd: "outputs" });
    expect(t.deps.unlockLabels).toHaveBeenCalled();
    expect(t.c.status.outputs).toEqual([{ deviceId: "d-hp", label: "Headphones (USB)" }]);
  });

  it("requests labels when WebKit hides every output until microphone permission", async () => {
    const t = setup();
    t.setDevices([]);
    t.deps.unlockLabels = vi.fn(async () => t.setDevices([HP]));
    await t.c.handle(host);
    expect(t.deps.unlockLabels).toHaveBeenCalledOnce();
    expect(t.c.status.host).toBe("on");
  });

  it("does not fall back to system playback after an explicit routing failure", async () => {
    const audio = fakeAudio([]);
    audio.setSinkId = vi.fn(async () => { throw new Error("Output access denied"); });
    const t = setup({ newAudio: () => audio as never });
    await t.c.handle(host);
    expect(t.c.status).toMatchObject({ host: "off", error: "Output access denied" });
    expect(t.room.connect).not.toHaveBeenCalled();
    expect(audio.play).not.toHaveBeenCalled();
  });
});

describe("older WebKit system output", () => {
  const output = { id: "coreaudio:headset", name: "Headset", form_factor: "headphones" as const, private: true, call: true, system_default: true };
  function systemVoice() {
    const audio = fakeAudio([], false);
    const t = setup({
      newAudio: () => audio as never,
      devices: vi.fn(async () => []),
      callOutput: vi.fn(async () => output),
    });
    return { ...t, audio };
  }

  it("uses the confirmed call output without output enumeration or microphone access", async () => {
    const t = systemVoice();
    await t.c.handle(host);
    expect(t.c.status).toMatchObject({ host: "on", device: "", error: null });
    expect(t.deps.devices).not.toHaveBeenCalled();
    expect(t.deps.unlockLabels).not.toHaveBeenCalled();
    expect(t.deps.onDeviceChange).toHaveBeenCalledWith(expect.any(Function), true);
    const a = track();
    t.room.emit("trackSubscribed", a, {}, { identity: "seat:a" });
    await flush();
    expect(a.attach).toHaveBeenCalledWith(t.audio);
    expect(t.audio.play).toHaveBeenCalledOnce();
    expect(t.audio.muted).toBe(false);
    await t.c.handle({ cmd: "host_off" });
    const checks = vi.mocked(t.deps.callOutput).mock.calls.length;
    t.deviceChange();
    await flush();
    expect(t.deps.callOutput).toHaveBeenCalledTimes(checks);
  });

  it("refuses a selected device that is not the system output", async () => {
    const t = systemVoice();
    vi.mocked(t.deps.callOutput).mockResolvedValue({ ...output, system_default: false });
    await t.c.handle(host);
    expect(t.c.status.host).toBe("off");
    expect(t.deps.hostToken).not.toHaveBeenCalled();
    expect(t.audio.play).not.toHaveBeenCalled();
  });

  it("checks the default again after connection before reporting voice ready", async () => {
    const t = systemVoice();
    vi.mocked(t.deps.callOutput).mockResolvedValueOnce(output).mockResolvedValue({ ...output, system_default: false });
    await t.c.handle(host);
    expect(t.c.status).toMatchObject({ host: "off", error: expect.stringContaining("output changed") });
    expect(t.room.disconnect).toHaveBeenCalledOnce();
  });

  it("rechecks native identity before a newly subscribed track can play", async () => {
    const t = systemVoice();
    await t.c.handle(host);
    vi.mocked(t.deps.callOutput).mockResolvedValue({ ...output, id: "coreaudio:speakers" });
    const a = track();
    t.room.emit("trackSubscribed", a, {}, { identity: "seat:a" });
    await flush();
    expect(a.attach).not.toHaveBeenCalled();
    expect(t.audio.play).not.toHaveBeenCalled();
    expect(t.c.status.host).toBe("off");
  });

  it.each(["changed", "unavailable"])("stops playback when native output is %s", async problem => {
    const t = systemVoice();
    await t.c.handle(host);
    const a = track();
    t.room.emit("trackSubscribed", a, {}, { identity: "seat:a" });
    await flush();
    if (problem === "changed") vi.mocked(t.deps.callOutput).mockResolvedValue({ ...output, id: "coreaudio:speakers" });
    else vi.mocked(t.deps.callOutput).mockRejectedValue(new Error("Device lookup failed"));
    t.deviceChange();
    await flush();
    expect(t.c.status.host).toBe("off");
    expect(t.audio.pause).toHaveBeenCalledOnce();
    expect(t.room.disconnect).toHaveBeenCalledOnce();
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
