// Guest seats' voice (ARCHITECTURE.md, "Guest seats", phase 2): a guest talks to the host, and
// only the host hears it. WebRTC through LiveKit, in the main window's WebView2 (LiveKit's
// JavaScript SDK): no native WebRTC in the app. The voice lives in the main window alone; both
// windows drive it with `voice:cmd` events and show its `voice:status`.
//
// The host hears guests on the output device they pick, any of them, the call's own included
// (guests are then transcribed with the call, which the host accepts); the picker warns about a
// device Windows doesn't call headphones. Each guest's <audio> element is routed with setSinkId
// before anything plays; if routing fails, or the device goes away, voice stops: it never falls
// back to the default device. Web Audio mixing (which plays on the default device) is off.
//
// The guest's microphone is open only while they talk (the track stops on mute), with the
// browser's echo cancellation, noise suppression and gain control.
import { Room, RoomEvent, Track, type RemoteParticipant, type RemoteTrack } from "livekit-client";
import { emit, listen } from "@tauri-apps/api/event";
import { rest } from "./api";

export type VoiceCmd =
  | { cmd: "outputs" }
  | { cmd: "host_on"; deviceId: string; sessionId: string; allowed: string[] }
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
  guest: "off" | "connecting" | "on";
  talking: boolean;
  outputs: VoiceOutput[];
  error: string | null;
}

export const VOICE_OFF: VoiceStatus = { host: "off", hostMuted: false, guest: "off", talking: false, outputs: [], error: null };

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
  /** One short microphone grant, so device names are readable (WebView2 hides them before). */
  unlockLabels(): Promise<void>;
  hostToken(sessionId: string): Promise<{ url: string; token: string }>;
  guestToken(seatId: string): Promise<{ url: string; token: string }>;
  onDeviceChange(f: () => void): () => void;
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
  unlockLabels: async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    for (const t of stream.getTracks()) t.stop();
  },
  hostToken: (sessionId) => rest.hostVoice(sessionId),
  guestToken: (seatId) => rest.guestVoice(seatId),
  onDeviceChange: (f) => {
    navigator.mediaDevices.addEventListener("devicechange", f);
    return () => navigator.mediaDevices.removeEventListener("devicechange", f);
  },
};

type Routable = HTMLAudioElement & { setSinkId?: (id: string) => Promise<void> };

/** An element routed to `deviceId` before anything plays on it; refused if it cannot be. */
export async function routed(el: HTMLAudioElement, deviceId: string): Promise<HTMLAudioElement> {
  const r = el as Routable;
  if (typeof r.setSinkId !== "function") throw new Error("This window cannot choose an output device.");
  await r.setSinkId(deviceId);
  return el;
}

export class VoiceController {
  status: VoiceStatus = { ...VOICE_OFF };
  private room: Room | null = null;
  private role: "host" | "guest" | null = null;
  private deviceId = "";
  private allowed = new Set<string>();
  private elements = new Map<string, HTMLAudioElement>();
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
    try {
      switch (c.cmd) {
        case "status":
          return this.publish(this.status);
        case "outputs":
          return await this.outputs();
        case "host_on":
          return await this.hostOn(c.deviceId, c.sessionId, c.allowed);
        case "host_off":
          return await this.stop(null);
        case "host_mute":
          this.set({ hostMuted: c.on });
          return this.applyMutes();
        case "host_allowed":
          this.allowed = new Set(c.identities);
          return this.applyMutes();
        case "guest_on":
          return await this.guestOn(c.seatId);
        case "guest_talk":
          return await this.talk(c.on);
        case "guest_off":
          return await this.stop(null);
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (c.cmd === "host_on" || c.cmd === "guest_on") await this.stop(message);
      else this.set({ error: message });
    }
  }

  private async outputs(): Promise<void> {
    let list = await this.deps.devices();
    if (list.some((d) => d.kind === "audiooutput" && !d.label)) {
      await this.deps.unlockLabels();
      list = await this.deps.devices();
    }
    // The "default" and "communications" entries follow Windows' choice: never a fixed device.
    const outputs = list
      .filter((d) => d.kind === "audiooutput" && d.deviceId !== "default" && d.deviceId !== "communications" && d.label)
      .map((d) => ({ deviceId: d.deviceId, label: d.label }));
    this.set({ outputs });
  }

  private async hostOn(deviceId: string, sessionId: string, allowed: string[]): Promise<void> {
    if (this.room) return;
    this.set({ host: "checking", error: null });
    await this.outputs();
    const device = this.status.outputs.find((o) => o.deviceId === deviceId);
    if (!device) throw new Error("Those headphones are not there any more.");
    await routed(this.deps.newAudio(), deviceId); // the window can play there, or no voice

    const grant = await this.deps.hostToken(sessionId);
    const room = this.deps.newRoom();
    this.room = room;
    this.role = "host";
    this.deviceId = deviceId;
    this.allowed = new Set(allowed);
    room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: unknown, who: RemoteParticipant) => {
      if (track.kind !== Track.Kind.Audio) return;
      void this.play(track, who.identity);
    });
    room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack, _pub: unknown, who: RemoteParticipant) => {
      track.detach();
      this.elements.delete(who.identity);
    });
    room.on(RoomEvent.Disconnected, () => void this.stop(this.room ? "Voice was disconnected." : null));
    this.stopWatching = this.deps.onDeviceChange(() => void this.watchDevice());
    await room.connect(grant.url, grant.token, { autoSubscribe: true });
    this.set({ host: "on", hostMuted: false, error: null });
  }

  /** A guest's track, on the chosen headphones only. */
  private async play(track: RemoteTrack, who: string): Promise<void> {
    try {
      const el = await routed(this.deps.newAudio(), this.deviceId);
      el.muted = this.status.hostMuted || !this.allowed.has(who);
      track.attach(el);
      this.elements.set(who, el);
      await el.play();
    } catch {
      track.detach();
      await this.stop("Guests' voice could not play on those headphones, so it stopped.");
    }
  }

  private applyMutes() {
    for (const [who, el] of this.elements) el.muted = this.status.hostMuted || !this.allowed.has(who);
  }

  private async watchDevice(): Promise<void> {
    const list = await this.deps.devices();
    if (!list.some((d) => d.kind === "audiooutput" && d.deviceId === this.deviceId)) {
      await this.stop("The headphones went away, so guests' voice stopped.");
    }
  }

  private async guestOn(seatId: string): Promise<void> {
    if (this.room) return;
    this.set({ guest: "connecting", error: null });
    const grant = await this.deps.guestToken(seatId);
    const room = this.deps.newRoom();
    this.room = room;
    this.role = "guest";
    room.on(RoomEvent.Disconnected, () => void this.stop(this.room ? "Voice was disconnected." : null));
    await room.connect(grant.url, grant.token, { autoSubscribe: false });
    this.set({ guest: "on", talking: false });
  }

  private async talk(on: boolean): Promise<void> {
    if (this.role !== "guest" || !this.room) {
      if (on) throw new Error("Voice is not connected.");
      return;
    }
    await this.room.localParticipant.setMicrophoneEnabled(on);
    this.set({ talking: on });
  }

  private async stop(error: string | null): Promise<void> {
    const room = this.room;
    this.room = null;
    this.role = null;
    this.stopWatching?.();
    this.stopWatching = null;
    for (const el of this.elements.values()) {
      el.pause();
      el.srcObject = null;
    }
    this.elements.clear();
    if (room) await room.disconnect();
    this.set({ host: "off", hostMuted: false, guest: "off", talking: false, error });
  }
}

/** In the main window only: the one voice controller, driven by `voice:cmd` events. */
export function startVoice(deps: VoiceDeps = browserDeps): () => void {
  const controller = new VoiceController(deps, (s) => void emit(STATUS, s));
  let queue = Promise.resolve();
  const off = listen<VoiceCmd>(CMD, (e) => {
    queue = queue.then(() => controller.handle(e.payload));
  });
  return () => {
    void off.then((f) => f());
    void controller.handle({ cmd: "host_off" });
  };
}
