// Guest seats' voice (ARCHITECTURE.md, "Guest seats", phase 2): a guest talks to the host, and
// only the host hears it. WebRTC through LiveKit, in the main window's webview (LiveKit's
// JavaScript SDK): no native WebRTC in the app. The voice lives in the main window alone; both
// windows drive it with `voice:cmd` events and show its `voice:status`.
//
// The host hears guests on the call's selected output. Voice turns on by itself once a guest
// may talk. Each guest's <audio> element is routed before anything plays. Older WebKit can
// only use the system output, so native device identity must confirm it is the call's output.
// Voice stops when that route changes or a chosen device disappears. Web Audio mixing is off.
//
// The guest's microphone is open only while they talk (the track stops on mute), with the
// browser's echo cancellation, noise suppression and gain control.
import { Room, RoomEvent, Track, type RemoteParticipant, type RemoteTrack, type RemoteAudioTrack } from "livekit-client";
import { emit, listen } from "@tauri-apps/api/event";
import { api, rest, type VoiceDevice } from "./api";

export type VoiceCmd =
  | { cmd: "outputs" }
  | { cmd: "host_on"; sessionId: string; allowed: string[] }
  | { cmd: "host_off" }
  | { cmd: "host_mute"; on: boolean }
  | { cmd: "host_allowed"; identities: string[] }
  | { cmd: "guest_on"; seatId: string }
  | { cmd: "guest_talk"; on: boolean }
  | { cmd: "guest_off" }
  | { cmd: "status" };

export interface VoiceOutput {
  deviceId: string;
  label: string;
}

export interface VoiceStatus {
  host: "off" | "checking" | "on";
  hostMuted: boolean;
  /** Where guests play now: the browser device id, "" for system default; null when off. */
  device: string | null;
  guest: "off" | "connecting" | "on";
  talking: boolean;
  outputs: VoiceOutput[];
  error: string | null;
}

export const VOICE_OFF: VoiceStatus = { host: "off", hostMuted: false, device: null, guest: "off", talking: false, outputs: [], error: null };

const CMD = "voice:cmd";
const STATUS = "voice:status";

/** Ask the main window's voice to do something (from either window). */
/** A device's name as Windows gives it. Chromium appends a USB device's vendor:product id,
 * "Speakers (Logi USB Headset) (046d:0a64)", which Windows' own name does not have. */
export function windowsName(label: string): string {
  return label.replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, "").trim();
}

export function voiceCommand(c: VoiceCmd): Promise<void> {
  return emit(CMD, c);
}

/** Follow the voice's status (from either window). */
export function onVoiceStatus(f: (s: VoiceStatus) => void): Promise<() => void> {
  return listen<VoiceStatus>(STATUS, (e) => f(e.payload));
}

/** What the controller needs from the world; the tests fake it. */
export interface VoiceDeps {
  newRoom(): Room;
  newAudio(): HTMLAudioElement;
  devices(): Promise<MediaDeviceInfo[]>;
  callOutput(): Promise<VoiceDevice | null>;
  /** One short microphone grant, so device names are readable (WebView2 hides them before). */
  unlockLabels(): Promise<void>;
  hostToken(sessionId: string): Promise<{ url: string; token: string }>;
  guestToken(seatId: string): Promise<{ url: string; token: string }>;
  onDeviceChange(f: () => void, followDefault?: boolean): () => void;
}

export const browserDeps: VoiceDeps = {
  newRoom: () =>
    new Room({
      adaptiveStream: false,
      dynacast: false,
      webAudioMix: false,
      stopLocalTrackOnUnpublish: true,
      audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      publishDefaults: { stopMicTrackOnMute: true },
    }),
  newAudio: () => new Audio(),
  devices: () => navigator.mediaDevices.enumerateDevices(),
  callOutput: async () => (await api.voiceDevices()).find((device) => device.call) ?? null,
  unlockLabels: async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    for (const t of stream.getTracks()) t.stop();
  },
  hostToken: (sessionId) => rest.hostVoice(sessionId),
  guestToken: (seatId) => rest.guestVoice(seatId),
  onDeviceChange: (f, followDefault = false) => {
    navigator.mediaDevices.addEventListener("devicechange", f);
    // Older WebKit does not expose output devices and may not report an output-only change.
    const timer = followDefault ? setInterval(f, 1000) : undefined;
    if (followDefault) window.addEventListener("focus", f);
    return () => {
      navigator.mediaDevices.removeEventListener("devicechange", f);
      clearInterval(timer);
      window.removeEventListener("focus", f);
    };
  },
};

type Routable = HTMLAudioElement & { setSinkId?: (id: string) => Promise<void> };

/** An element routed to `deviceId` before anything plays on it; refused if it cannot be. */
export async function routed(el: HTMLAudioElement, deviceId: string): Promise<HTMLAudioElement> {
  const r = el as Routable;
  if (typeof r.setSinkId === "function") await r.setSinkId(deviceId);
  else if (deviceId !== "") throw new Error("This window cannot choose an output device.");
  return el;
}

export class VoiceController {
  status: VoiceStatus = { ...VOICE_OFF };
  private room: Room | null = null;
  private role: "host" | "guest" | null = null;
  private deviceId = "";
  private defaultOutputId: string | null = null;
  private checkingDevice = false;
  private allowed = new Set<string>();
  private elements = new Map<RemoteTrack, { identity: string; element: HTMLAudioElement | null }>();
  private hostSessionId: string | null = null;
  private epoch = 0;
  private talkQueue: Promise<void> = Promise.resolve();
  private stopWatching: (() => void) | null = null;

  constructor(
    private deps: VoiceDeps,
    private publish: (s: VoiceStatus) => void,
  ) {}

  private set(patch: Partial<VoiceStatus>) {
    this.status = { ...this.status, ...patch };
    this.publish(this.status);
  }

  async handle(c: VoiceCmd): Promise<void> {
    let operationEpoch = this.epoch;
    try {
      switch (c.cmd) {
        case "status":
          return this.publish(this.status);
        case "outputs":
          return await this.outputs();
        case "host_on": {
          const pending = this.hostOn(c.sessionId, c.allowed);
          operationEpoch = this.epoch;
          return await pending;
        }
        case "host_off":
          return await this.stop(null);
        case "host_mute":
          this.set({ hostMuted: c.on });
          return this.applyMutes();
        case "host_allowed":
          this.allowed = new Set(c.identities);
          return this.applyMutes();
        case "guest_on": {
          const pending = this.guestOn(c.seatId);
          operationEpoch = this.epoch;
          return await pending;
        }
        case "guest_talk":
          return await this.talk(c.on);
        case "guest_off":
          return await this.stop(null);
      }
    } catch (e) {
      if (operationEpoch !== this.epoch) return;
      const message = e instanceof Error ? e.message : String(e);
      if (c.cmd === "host_on" || c.cmd === "guest_on") await this.stop(message);
      else this.set({ error: message });
    }
  }

  private async outputs(): Promise<void> {
    let list = await this.deps.devices();
    if (!list.some((d) => d.kind === "audiooutput") || list.some((d) => d.kind === "audiooutput" && !d.label)) {
      await this.deps.unlockLabels();
      list = await this.deps.devices();
    }
    // The "default" and "communications" entries follow Windows' choice: never a fixed device.
    const outputs = list
      .filter((d) => d.kind === "audiooutput" && d.deviceId !== "default" && d.deviceId !== "communications" && d.label)
      .map((d) => ({ deviceId: d.deviceId, label: d.label }));
    this.set({ outputs });
  }

  private async hostOn(sessionId: string, allowed: string[]): Promise<void> {
    if (this.room || this.status.host === "checking") return;
    const epoch = ++this.epoch;
    const current = () => epoch === this.epoch;
    const muted = this.hostSessionId === sessionId && this.status.hostMuted;
    this.hostSessionId = sessionId;
    this.set({ host: "checking", hostMuted: muted, error: null });
    const call = await this.deps.callOutput();
    if (!current()) return;
    const probe = this.deps.newAudio();
    const canRoute = typeof (probe as Routable).setSinkId === "function";
    let deviceId = "";
    if (canRoute) {
      await this.outputs();
      if (!current()) return;
      const matches = call ? this.status.outputs.filter((output) => windowsName(output.label).toLowerCase() === windowsName(call.name).toLowerCase()) : [];
      if (matches.length !== 1) throw new Error("Assistant audio could not find the call's output. Check Call audio and try again.");
      deviceId = matches[0].deviceId;
    } else if (!call?.system_default) {
      throw new Error("Set the system sound output to the selected playback device, then try assistant audio again.");
    }
    await routed(probe, deviceId);
    if (!current()) return;
    const grant = await this.deps.hostToken(sessionId);
    if (!current()) return;
    const room = this.deps.newRoom();
    this.room = room;
    this.role = "host";
    this.deviceId = deviceId;
    this.defaultOutputId = canRoute ? null : call!.id;
    this.allowed = new Set(allowed);
    room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: unknown, who: RemoteParticipant) => {
      if (this.room !== room || !current() || track.kind !== Track.Kind.Audio) return;
      void this.play(track, who.identity);
    });
    room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
      if (this.room === room && current()) this.drop(track);
    });
    room.on(RoomEvent.Disconnected, () => { if (this.room === room && current()) void this.stop("Voice was disconnected."); });
    this.stopWatching = this.deps.onDeviceChange(() => void this.watchDevice(), !canRoute);
    await room.connect(grant.url, grant.token, { autoSubscribe: true });
    if (this.room !== room || !current()) { await room.disconnect(); return; }
    const outputReady = await this.hasDefaultOutput();
    if (this.room !== room) return;
    if (!outputReady) {
      await this.stop("The call's sound output changed. Check Call audio and try assistant audio again.");
      return;
    }
    this.set({ host: "on", device: deviceId, error: null });
    this.applyMutes();
  }

  private async hasDefaultOutput(): Promise<boolean> {
    if (this.defaultOutputId === null) return true;
    const id = this.defaultOutputId;
    const current = await this.deps.callOutput();
    return current?.system_default === true && current.id === id;
  }

  /** A binding is tracked before routing starts, so unsubscribe/stop can cancel it. */
  private async play(track: RemoteTrack, who: string): Promise<void> {
    if (this.elements.has(track)) return;
    const room = this.room;
    const binding = { identity: who, element: null as HTMLAudioElement | null };
    this.elements.set(track, binding);
    const current = () => !!room && this.room === room && this.elements.get(track) === binding;
    try {
      if (!(await this.hasDefaultOutput())) throw new Error("Call output changed");
      if (!current()) return;
      const el = this.deps.newAudio();
      binding.element = el;
      // LiveKit attach resets element.muted. Its persistent volume must be zero before
      // attaching, then both controls are reapplied after attach and on preference changes.
      el.muted = true; el.volume = 0;
      await routed(el, this.deviceId);
      if (!current()) { el.pause(); el.srcObject = null; return; }
      (track as RemoteAudioTrack).setVolume?.(0);
      track.attach(el);
      this.muteTrack(track, binding);
      await el.play();
      if (!current()) { el.pause(); el.srcObject = null; }
    } catch {
      const stillCurrent = current();
      if (this.elements.get(track) === binding) this.drop(track);
      if (stillCurrent) await this.stop("Assistant audio could not use the call's output. Check Call audio and try again.");
    }
  }

  private muteTrack(track: RemoteTrack, binding: { identity: string; element: HTMLAudioElement | null }) {
    const muted = this.status.hostMuted || !this.allowed.has(binding.identity);
    (track as RemoteAudioTrack).setVolume?.(muted ? 0 : 1);
    if (binding.element) { binding.element.volume = muted ? 0 : 1; binding.element.muted = muted; }
  }

  private applyMutes() {
    for (const [track, binding] of this.elements) this.muteTrack(track, binding);
  }

  private drop(track: RemoteTrack) {
    const binding = this.elements.get(track);
    this.elements.delete(track);
    if (binding?.element) {
      binding.element.muted = true; binding.element.volume = 0;
      binding.element.pause(); binding.element.srcObject = null;
      track.detach(binding.element);
    } else track.detach();
  }

  private async watchDevice(): Promise<void> {
    if (this.checkingDevice || !this.room) return;
    this.checkingDevice = true;
    const room = this.room;
    try {
      const present = this.defaultOutputId !== null ? await this.hasDefaultOutput() :
        (await this.deps.devices()).some((d) => d.kind === "audiooutput" && d.deviceId === this.deviceId);
      if (!present && this.room === room) await this.stop("The call's audio output changed or disconnected, so assistant audio stopped.");
    } catch {
      if (this.room === room) await this.stop("Assistant audio could not check the call's output, so it stopped. Try again.");
    } finally { this.checkingDevice = false; }
  }

  private async guestOn(seatId: string): Promise<void> {
    if (this.room || this.status.guest === "connecting") return;
    const epoch = ++this.epoch;
    this.set({ guest: "connecting", error: null });
    const grant = await this.deps.guestToken(seatId);
    if (epoch !== this.epoch) return;
    const room = this.deps.newRoom();
    this.room = room;
    this.role = "guest";
    room.on(RoomEvent.Disconnected, () => { if (this.room === room && epoch === this.epoch) void this.stop("Voice was disconnected."); });
    await room.connect(grant.url, grant.token, { autoSubscribe: false });
    if (this.room !== room || epoch !== this.epoch) { await room.disconnect(); return; }
    this.set({ guest: "on", talking: false });
  }

  private async talk(on: boolean): Promise<void> {
    const room = this.room;
    const epoch = this.epoch;
    if (this.role !== "guest" || !room) {
      if (on) throw new Error("Voice is not connected.");
      return;
    }
    const update = this.talkQueue.catch(() => undefined).then(async () => {
      if (this.room !== room || epoch !== this.epoch) return;
      await room.localParticipant.setMicrophoneEnabled(on);
      if (this.room === room && epoch === this.epoch) this.set({ talking: on });
      else if (on) await room.localParticipant.setMicrophoneEnabled(false).catch(() => undefined);
    });
    this.talkQueue = update;
    await update;
  }

  private async stop(error: string | null): Promise<void> {
    const room = this.room;
    this.epoch += 1;
    this.room = null;
    this.role = null;
    this.defaultOutputId = null;
    this.stopWatching?.();
    this.stopWatching = null;
    for (const track of [...this.elements.keys()]) this.drop(track);
    this.set({ host: "off", device: null, guest: "off", talking: false, error });
    if (room) await room.disconnect().catch(() => undefined);
  }
}

/** In the main window only: the one voice controller, driven by `voice:cmd` events. */
export function startVoice(deps: VoiceDeps = browserDeps): () => void {
  const controller = new VoiceController(deps, (s) => void emit(STATUS, s));
  let disposed = false;
  // Stop and mute must not wait behind a token request or room connection. The controller
  // guards async setup by generation and serializes only microphone enable/disable calls.
  const off = listen<VoiceCmd>(CMD, (e) => {
    if (!disposed) void controller.handle(e.payload);
  });
  return () => {
    disposed = true;
    void off.then((f) => f());
    void controller.handle({ cmd: "host_off" });
  };
}
