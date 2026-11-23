// Guest seats' screen (ARCHITECTURE.md, "Guest seats", phases 4 and 5): the host shares their
// screen with the guests the host lets see it (`device.view`), and may let one guest who holds
// `device.control` drive it with their mouse and keys. WebRTC through LiveKit, in a room of its
// own (`screen-<session id>`), apart from voice: the host's token can only share the screen
// (and talk to guests over data), a guest's can watch and send quality or control requests.
//
// The host. The browser lets a page capture the screen only right after a click in it, so the
// share runs in the window where the host clicked (the overlay or the main window), and the
// windows agree through `screen:cmd` / `screen:status` events: one window shares, the others
// show its status and drive it. App windows follow the explicit Stealth preference; with
// Stealth off, the shared display includes them like other visible windows.
//
// Control (phase 5). Only while the host shares a whole screen, only a guest whose seat holds
// `device.control` (the host's windows pass the list on as it changes), and only after the
// host lets that guest control: then the guest's events (data, topic `input`) are handed to
// the app (remote.rs), which places them on the shared monitor. One guest at a time. It ends
// with Stop control, the Stop control hotkey, the guest's grant going away, the share ending,
// or the guest letting go; the app then lets go of every key and button the guest held.
//
// The guest watches in the main window (Watching, on the Call screen), which joins the room
// while their seat has `device.view`. Revoking view access removes the participant;
// changing control eligibility preserves the room and still requires explicit host consent.
import {
  Room,
  RoomEvent,
  Track,
  createLocalScreenTracks,
  type LocalTrack,
  type RemoteParticipant,
  type RemoteTrack,
} from "livekit-client";
import { emit, listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { rest } from "./api";
import { isMac } from "./platform";
import { captureNativeScreen, nativeShares } from "./nativeScreen";

export const HOST = "host";

export interface Person {
  identity: string;
  name: string;
}

export interface ControlRequest extends Person { sessionId: string; requestId: string; expiresAt: number }
export const CONTROL_REQUEST_MS = 60_000;
export const SCREEN_QUALITIES = {
  "720p": { width: 1280, height: 720, bitrate: 3_000_000 },
  "1080p": { width: 1920, height: 1080, bitrate: 5_000_000 },
  "1440p": { width: 2560, height: 1440, bitrate: 10_000_000 },
  "2160p": { width: 3840, height: 2160, bitrate: 18_000_000 },
} as const;
export type ScreenQuality = keyof typeof SCREEN_QUALITIES;
const isQuality = (value: unknown): value is ScreenQuality => typeof value === "string" && Object.prototype.hasOwnProperty.call(SCREEN_QUALITIES, value);

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Monitor {
  id: string;
  rect: Rect;
  primary: boolean;
  pixels?: [number, number];
  /** Windows/WebRTC source ID, matched by native display device name (not array order). */
  capture_id?: number;
}

/** One event of a controlling guest (remote.rs `Input`); x and y are 0 to 1 across the picture. */
export type InputEvent =
  | { t: "move"; x: number; y: number }
  | { t: "down" | "up"; button: number; x: number; y: number }
  | { t: "wheel"; dx: number; dy: number }
  | { t: "key_down" | "key_up"; code: string };

export interface ScreenStatus {
  host: "off" | "starting" | "on";
  sessionId: string | null;
  /** What the host shares: "monitor" (a whole screen), "window", or null when unknown. */
  surface: string | null;
  /** The guest controlling the screen now. */
  controller: Person | null;
  /** Guests who asked to control. */
  asking: ControlRequest[];
  /** Guests the host stopped (Stop control, the hotkey): the UI takes their Control away. */
  stopped: Person[];
  error: string | null;
}

export const SCREEN_OFF: ScreenStatus = { host: "off", sessionId: null, surface: null, controller: null, asking: [], stopped: [], error: null };

type ScreenCmd =
  | { cmd: "stop"; sessionId?: string }
  | { cmd: "status" }
  | { cmd: "controllers"; sessionId: string | null; allowed: Person[]; viewers?: Person[] }
  | { cmd: "control"; person: ControlRequest }
  | { cmd: "decline"; request: ControlRequest }
  | { cmd: "control_off" };

/** Why control ended: the host stopped it (their Control is then taken away), the guest gave
 * it back (they take it again with Take control), or something else did (the grant went, the
 * guest left, the share stopped). */
export type EndedBy = "host" | "guest" | "other";

const CMD = "screen:cmd";
const STATUS = "screen:status";
/** Data topics: the guest's events and asks, the host's word on control, and the controlling
 * guest's ping (the host sends it straight back) to time the input path. */
export const TOPIC = { input: "input", ask: "ask", release: "release", control: "control", ping: "ping", pong: "pong", quality: "screen_quality", qualityStatus: "screen_quality_status" } as const;
/** At most this many of a guest's events a second; the rest are dropped. */
export const MAX_EVENTS_PER_S = 600;

/** The shared picture's encoding: sharp text while guests only watch; while a guest controls,
 * twice the frames (and the bits for them), and frames are kept over sharpness when the line
 * is short, so the pointer and what it moves keep up with the guest's hand. */
export const SCREEN_CODEC = "h264";

export const ENCODING = {
  watch: { maxBitrate: 5_000_000, maxFramerate: 30, degradation: "maintain-resolution" },
  control: { maxBitrate: 8_000_000, maxFramerate: 60, degradation: "balanced" },
} as const;

export interface Grant {
  url: string;
  token: string;
}

/** What sharing needs from the world; the tests fake it. */
export interface HostDeps {
  newRoom(): Room;
  /** The screen picker (getDisplayMedia): must run right after the host's click. */
  capture(signal?: AbortSignal): Promise<LocalTrack[]>;
  token(sessionId: string): Promise<Grant>;
  monitors(): Promise<Monitor[]>;
  begin(target: Rect): Promise<void>;
  input(events: InputEvent[]): Promise<void>;
  end(): Promise<void>;
  now(): number;
}

export const hostDeps: HostDeps = {
  newRoom: () => new Room({ adaptiveStream: false, dynacast: false }),
  capture: (signal) => (isMac() || /Win/.test(navigator.platform)) ? captureNativeScreen(signal) :
    createLocalScreenTracks({
      audio: false,
      // Captured at up to 60 frames, so control can send them all (ENCODING).
      resolution: { width: 1920, height: 1080, frameRate: 60 },
      contentHint: "detail",
      selfBrowserSurface: "exclude",
      surfaceSwitching: "exclude",
      systemAudio: "exclude",
    }),
  token: (sessionId) => rest.hostScreen(sessionId),
  monitors: () => invoke<Monitor[]>("remote_monitors"),
  begin: (target) => invoke<void>("remote_begin", { target }),
  input: (events) => invoke<void>("remote_input", { events }),
  end: () => invoke<void>("remote_end"),
  now: () => Date.now(),
};

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** The monitors whose shape matches the shared picture (a whole-screen share is one monitor,
 * maybe scaled down, never cropped). */
export function matchingMonitors(monitors: Monitor[], width: number, height: number): Monitor[] {
  if (!(width > 0 && height > 0)) return monitors;
  const ratio = width / height;
  return monitors.filter((m) => {
    const [w, h] = m.pixels ?? [m.rect.w, m.rect.h];
    return h > 0 && Math.abs(w / h - ratio) < 0.02;
  });
}

/** Bind input to the display chosen by the capture picker. Chromium exposes the Windows
 * EnumDisplayDevices source ID in the track label; native Mac capture keeps its display ID.
 * A known but missing source must never fall back to another display of the same size. */
export function sharedMonitor(monitors: Monitor[], track: MediaStreamTrack): Monitor | null {
  const native = nativeShares.get(track);
  if (native) return native.kind === "monitor" ? monitors.find((m) => m.id === (native.monitor_id ?? String(native.id))) ?? null : null;
  const settings = track.getSettings();
  if (settings.displaySurface !== "monitor") return null;
  if (track.label?.startsWith("screen:")) {
    const source = /^screen:(\d+):0$/.exec(track.label);
    if (!source) return null; // A native-picker session or whole-desktop source is not one monitor.
    const id = Number(source[1]);
    return monitors.find((m) => m.capture_id === id) ?? null;
  }
  // Other capture implementations do not expose a source identity. Only an unambiguous
  // whole-screen shape may be used; identical displays remain view-only.
  const fit = matchingMonitors(monitors, settings.width ?? 0, settings.height ?? 0);
  return fit.length === 1 ? fit[0] : null;
}

const sameMonitor = (a: Monitor, b: Monitor) => a.id === b.id && a.capture_id === b.capture_id &&
  a.rect.x === b.rect.x && a.rect.y === b.rect.y && a.rect.w === b.rect.w && a.rect.h === b.rect.h;

/** The host's share in one window. */
export class HostScreen {
  status: ScreenStatus = SCREEN_OFF;
  room: Room | null = null;
  private tracks: LocalTrack[] = [];
  private allowed: Person[] = [];
  private viewers: Person[] = [];
  private permissionsSessionId: string | null = null;
  private approved: ControlRequest | null = null;
  private requestTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private requestCooldown = new Map<string, number>();
  private quality: ScreenQuality = "1080p";
  private qualityQueue = Promise.resolve();
  private qualityLast = new Map<string, number>();
  private queue: Promise<void> = Promise.resolve();
  private pending = 0;
  /** The newest move that came while the app was busy: only where the pointer ends up counts. */
  private spare: InputEvent | null = null;
  private window = { start: 0, count: 0 };
  /** The display chosen for this share. Control never selects a different display. */
  private target: Monitor | null = null;
  private controlEpoch = 0;
  private granting = false;
  private stopping = false;
  private shareEpoch = 0;
  private captureAbort: AbortController | null = null;

  constructor(
    private deps: HostDeps,
    private publish: (s: ScreenStatus) => void,
  ) {}

  private set(patch: Partial<ScreenStatus>) {
    this.status = { ...this.status, ...patch };
    this.publish(this.status);
  }

  get sharing(): boolean {
    return this.room !== null;
  }

  /** Call from the click itself: the picker comes first, while the click still counts. */
  async share(sessionId: string): Promise<void> {
    if (this.room || this.status.host === "starting") return;
    if (this.permissionsSessionId && this.permissionsSessionId !== sessionId) {
      this.allowed = []; this.viewers = [];
    }
    const epoch = ++this.shareEpoch;
    const captureAbort = new AbortController();
    this.captureAbort = captureAbort;
    this.stopping = false;
    const current = () => !this.stopping && epoch === this.shareEpoch;
    this.set({ host: "starting", sessionId, error: null });
    let tracks: LocalTrack[] = [];
    let room: Room | null = null;
    const discard = async () => {
      for (const track of tracks) track.stop();
      if (room) await room.disconnect().catch(() => undefined);
    };
    try {
      tracks = await this.deps.capture(captureAbort.signal);
    } catch (error) {
      if (!current()) return;
      const cancelled = error instanceof DOMException && ["AbortError", "NotAllowedError"].includes(error.name);
      this.set({ host: "off", sessionId: null, surface: null, error: cancelled ? null : String(error instanceof Error ? error.message : error) });
      return;
    }
    if (!current()) { await discard(); return; }
    try {
      const video = tracks.find((t) => t.kind === Track.Kind.Video);
      if (!video) throw new Error("No screen was chosen.");
      this.tracks = tracks;
      const available = () => {
        if (!current()) return false;
        if (video.mediaStreamTrack.readyState === "ended") throw new Error(nativeShares.get(video.mediaStreamTrack)?.error || "Screen capture ended before sharing was ready. Try sharing again.");
        return true;
      };
      available();
      const monitors = await this.deps.monitors();
      if (!available()) { await discard(); return; }
      this.target = sharedMonitor(monitors, video.mediaStreamTrack);
      const grant = await this.deps.token(sessionId);
      if (!available()) { await discard(); return; }
      room = this.deps.newRoom();
      const activeRoom = room;
      this.room = room;
      const ownsRoom = () => current() && this.room === activeRoom;
      room.on(RoomEvent.Disconnected, () => { if (ownsRoom()) void this.stop("Screen sharing was disconnected."); });
      room.on(RoomEvent.DataReceived, (payload: Uint8Array, who?: RemoteParticipant, _kind?: unknown, topic?: string) => {
        if (ownsRoom() && who) void this.data(payload, who, topic);
      });
      room.on(RoomEvent.ParticipantDisconnected, (who: RemoteParticipant) => {
        if (!ownsRoom()) return;
        if (this.status.controller?.identity === who.identity) void this.endControl(null);
        const request = this.status.asking.find(p => p.identity === who.identity);
        if (request) void this.decline(request, "Assistant disconnected");
        this.qualityLast.delete(who.identity);
      });
      room.on(RoomEvent.ParticipantConnected, (who: RemoteParticipant) => {
        if (!ownsRoom()) return;
        void this.qualityStatus(who.identity);
      });
      video.mediaStreamTrack.addEventListener("ended", () => {
        if (ownsRoom()) void this.stop(nativeShares.get(video.mediaStreamTrack)?.error ?? null);
      });
      await room.connect(grant.url, grant.token, { autoSubscribe: false });
      if (!ownsRoom() || !available()) { await discard(); return; }
      await room.localParticipant.publishTrack(video, {
        source: Track.Source.ScreenShare,
        simulcast: false,
        videoCodec: SCREEN_CODEC,
        videoEncoding: { maxBitrate: ENCODING.watch.maxBitrate, maxFramerate: ENCODING.watch.maxFramerate },
        degradationPreference: ENCODING.watch.degradation,
      });
      if (!ownsRoom() || !available()) { await discard(); return; }
      const native = nativeShares.get(video.mediaStreamTrack);
      const surface = native?.kind ?? video.mediaStreamTrack.getSettings().displaySurface ?? null;
      this.set({ host: "on", surface, error: null });

    } catch (e) {
      if (current()) await this.stop(e instanceof Error ? e.message : String(e));
      else await discard();
    }
  }

  async stop(error: string | null): Promise<void> {
    this.shareEpoch += 1;
    for (const timer of this.requestTimers.values()) clearTimeout(timer);
    this.requestTimers.clear(); this.requestCooldown.clear(); this.approved = null;
    this.qualityLast.clear(); this.quality = "1080p";
    this.captureAbort?.abort();
    this.captureAbort = null;
    this.stopping = true;
    await this.endControl(null);
    const room = this.room;
    this.room = null;
    for (const t of this.tracks) t.stop();
    this.tracks = [];
    this.target = null;
    if (room) await room.disconnect();
    if (room || this.status.host !== "off" || error) this.set({ ...SCREEN_OFF, stopped: this.status.stopped, error });
  }

  // --- Control ---------------------------------------------------------------------------

  /** Capabilities authorize eligibility only. Every control session also needs a fresh host decision. */
  setAllowed(allowed: Person[], viewers: Person[] = allowed, sessionId?: string | null): void {
    if (sessionId !== undefined) {
      if (this.status.sessionId && sessionId !== this.status.sessionId) return;
      this.permissionsSessionId = sessionId;
    }
    const revoked = this.allowed.filter(p => !allowed.some(q => q.identity === p.identity));
    this.allowed = allowed;
    this.viewers = viewers;
    if (this.approved && revoked.some(p => p.identity === this.approved?.identity)) void this.decline(this.approved, "Control permission removed");
    const may = (identity: string) => allowed.some(a => a.identity === identity) && viewers.some(a => a.identity === identity);
    if (this.status.controller && !may(this.status.controller.identity)) void this.endControl(null);
    for (const request of this.status.asking) {
      if (!viewers.some(p => p.identity === request.identity)) void this.decline(request, "Screen access removed");
    }
    const stopped = this.status.stopped.filter(p => may(p.identity));
    if (stopped.length !== this.status.stopped.length) this.set({ stopped });
    if (this.approved && may(this.approved.identity)) void this.allow(this.approved);
  }

  private present(identity: string): boolean {
    const there = (this.room as { remoteParticipants?: Map<string, unknown> } | null)?.remoteParticipants;
    return there instanceof Map ? there.has(identity) : true;
  }

  private requested(request: ControlRequest): boolean {
    return request.sessionId === this.status.sessionId && this.status.asking.some(p =>
      p.identity === request.identity && p.requestId === request.requestId && p.sessionId === request.sessionId && p.expiresAt > this.deps.now()) &&
      this.viewers.some(p => p.identity === request.identity) && this.present(request.identity);
  }

  async decline(request: ControlRequest, reason = "Request declined"): Promise<void> {
    if (request.sessionId !== this.status.sessionId || !this.status.asking.some(p => p.identity === request.identity && p.requestId === request.requestId)) return;
    if (this.approved?.requestId === request.requestId && this.approved.identity === request.identity) { this.approved = null; this.controlEpoch += 1; }
    clearTimeout(this.requestTimers.get(request.identity)); this.requestTimers.delete(request.identity);
    this.requestCooldown.set(request.identity, this.deps.now() + 10_000);
    this.set({ asking: this.status.asking.filter(p => p.requestId !== request.requestId || p.identity !== request.identity) });
    await this.tell(request.identity, false, request.requestId, reason);
  }

  /** Let this guest control the display already chosen in the sharing picker. */
  async allow(person: ControlRequest): Promise<void> {
    if (!this.requested(person) || this.granting) return;
    this.approved = person;
    // The host's API grant may arrive on the session stream after its HTTP response.
    // Retain this exact approval until the capability arrives, expires, or is cancelled.
    if (!this.allowed.some(p => p.identity === person.identity)) return;
    this.granting = true;
    let epoch = this.controlEpoch;
    const stillAllowed = () => epoch === this.controlEpoch && !this.stopping && this.status.host === "on" &&
      this.allowed.some((a) => a.identity === person.identity) && this.requested(person);
    try {
      if (!this.room || this.status.host !== "on") throw new Error("Share your screen first.");
      if (this.status.surface !== "monitor") throw new Error("Share a whole screen to let an assistant control it.");
      if (!this.allowed.some((a) => a.identity === person.identity)) throw new Error(`${person.name} may not control your screen.`);
      const all = await this.deps.monitors();
      if (!stillAllowed()) return;
      const target = this.target && all.find((m) => sameMonitor(m, this.target!));
      if (!target) throw new Error("The shared display could not be identified. Stop sharing and share that display again to allow control.");
      if (this.status.controller && this.status.controller.identity !== person.identity) {
        epoch += 1; // Our endControl invalidates the old grant; another stop still cancels us.
        await this.endControl(null);
      }
      // Native input may already be executing when control ends. Finish that old
      // epoch before opening a new one, so delayed keys never enter a new grant.
      await this.queue;
      if (!stillAllowed()) return;
      await this.deps.begin(target.rect);
      if (!stillAllowed()) {
        await this.deps.end();
        return;
      }
      this.approved = null;
      clearTimeout(this.requestTimers.get(person.identity)); this.requestTimers.delete(person.identity);
      this.set({ controller: { identity: person.identity, name: person.name }, asking: this.status.asking.filter((p) => p.identity !== person.identity), error: null });
      await this.tell(person.identity, true, person.requestId);
      await this.tune("control");
    } catch (e) {
      if (epoch === this.controlEpoch) {
        const error = e instanceof Error ? e.message : String(e);
        this.set({ error }); await this.decline(person, error);
      }
    } finally {
      this.granting = false;
    }
  }

  /** Control ends: the app lets go of everything the guest held, and the guest is told. */
  async endControl(error: string | null, by: EndedBy = "other"): Promise<void> {
    this.controlEpoch += 1;
    const was = this.status.controller;
    let stopped = this.status.stopped;
    if (by === "host") {
      for (const request of this.status.asking) void this.decline(request, "The host stopped remote control");
      this.approved = null;
      // The host's stop holds for every guest who could take over, until Control is given again.
      stopped = [...stopped, ...this.allowed.filter((p) => !stopped.some((q) => q.identity === p.identity))];
    }
    if (!was && !this.granting) {
      if (stopped !== this.status.stopped) this.set({ stopped });
      return;
    }
    this.set({ controller: null, stopped, error: error ?? this.status.error });
    this.spare = null;
    try {
      await this.deps.end();
    } finally {
      if (was) await this.tell(was.identity, false);
      if (was) await this.tune("watch");
    }

  }

  /** The picture's encoding for watching or for control (ENCODING), changed on the live share. */
  private async tune(mode: keyof typeof ENCODING): Promise<void> {
    const video = this.tracks.find((t) => t.kind === Track.Kind.Video);
    const sender = video?.sender;
    if (!sender) return;
    const want = ENCODING[mode];
    try {
      const p = sender.getParameters() as RTCRtpSendParameters & { degradationPreference?: string };
      if (!p.encodings?.length) return;
      for (const e of p.encodings) {
        e.maxBitrate = SCREEN_QUALITIES[this.quality].bitrate * (mode === "control" ? 1.6 : 1);
        e.maxFramerate = want.maxFramerate;
      }
      p.degradationPreference = want.degradation;
      await sender.setParameters(p);
    } catch {
      // The share keeps its encoding; control works the same, only less smoothly.
    }
  }

  private async tell(identity: string, on: boolean, requestId?: string, reason?: string): Promise<void> {
    try {
      await this.room?.localParticipant.publishData(encoder.encode(JSON.stringify({ on, ...(requestId ? { requestId } : {}), ...(reason ? { reason } : {}) })), {
        reliable: true,
        destinationIdentities: [identity],
        topic: TOPIC.control,
      });
    } catch {
      // The guest is gone; control is over on this side either way.
    }
  }

  private async qualityStatus(identity?: string, error?: string): Promise<void> {
    const settings = this.tracks.find(t => t.kind === Track.Kind.Video)?.mediaStreamTrack.getSettings();
    await this.room?.localParticipant.publishData(encoder.encode(JSON.stringify({ quality: this.quality,
      width: settings?.width, height: settings?.height, ...(error ? { error } : {}) })), {
      reliable: true, ...(identity ? { destinationIdentities: [identity] } : {}), topic: TOPIC.qualityStatus,
    }).catch(() => undefined);
  }

  private async changeQuality(quality: ScreenQuality, identity: string, shareEpoch: number): Promise<void> {
    if (shareEpoch !== this.shareEpoch || !this.room || !this.viewers.some(p => p.identity === identity) || !this.present(identity)) return;
    try {
      const track = this.tracks.find(t => t.kind === Track.Kind.Video)?.mediaStreamTrack;
      if (!track) return;
      const { width, height } = SCREEN_QUALITIES[quality];
      const native = nativeShares.get(track);
      if (native?.setQuality) await native.setQuality(height);
      else await track.applyConstraints({ width: { ideal: width, max: width }, height: { ideal: height, max: height } });
      if (shareEpoch !== this.shareEpoch) return;
      this.quality = quality;
      await this.tune(this.status.controller ? "control" : "watch");
      await this.qualityStatus();
    } catch (e) { await this.qualityStatus(identity, e instanceof Error ? e.message : "Resolution could not be changed."); }
  }

  private async data(payload: Uint8Array, who: RemoteParticipant, topic?: string): Promise<void> {
    const viewer = this.viewers.find(p => p.identity === who.identity);
    if (!viewer || !this.present(who.identity) || payload.byteLength > 65_536) return;
    if (topic === TOPIC.quality) {
      if (payload.byteLength > 256) return;
      let quality: unknown;
      try { quality = JSON.parse(decoder.decode(payload)).quality; } catch { return; }
      if (!isQuality(quality)) return;
      const now = this.deps.now();
      if (now - (this.qualityLast.get(who.identity) ?? -Infinity) < 1000) return;
      this.qualityLast.set(who.identity, now);
      const selected = quality, epoch = this.shareEpoch;
      this.qualityQueue = this.qualityQueue.then(() => this.changeQuality(selected, who.identity, epoch));
      await this.qualityQueue;
      return;
    }
    if (topic === TOPIC.ask) {
      if (this.status.host !== "on" || payload.byteLength > 256 || this.status.controller?.identity === who.identity) return;
      let requestId: unknown;
      try { requestId = JSON.parse(decoder.decode(payload)).requestId; } catch { return; }
      if (typeof requestId !== "string" || !/^[a-zA-Z0-9_-]{8,80}$/.test(requestId)) return;
      if (this.status.asking.some(p => p.identity === who.identity)) return;
      if (this.status.surface !== "monitor" || (this.requestCooldown.get(who.identity) ?? 0) > this.deps.now()) {
        await this.tell(who.identity, false, requestId, this.status.surface !== "monitor" ? "Remote control requires sharing an entire display." : "Please wait before requesting control again.");
        return;
      }
      if (!this.status.sessionId) return;
      const request = { ...viewer, sessionId: this.status.sessionId, requestId, expiresAt: this.deps.now() + CONTROL_REQUEST_MS };
      this.set({ asking: [...this.status.asking, request] });
      this.requestTimers.set(who.identity, setTimeout(() => void this.decline(request, "Request expired"), CONTROL_REQUEST_MS));
      return;
    }
    if (topic === TOPIC.release) {
      const request = this.status.asking.find(p => p.identity === who.identity);
      if (request) await this.decline(request, "Request cancelled");
      if (this.status.controller?.identity === who.identity) await this.endControl(null, "guest");
      return;
    }
    const person = this.allowed.find(a => a.identity === who.identity);
    if (!person) return; // Metadata is allowed for viewers, input is not.
    if (topic === TOPIC.ping) {
      // Straight back, untouched: the guest times the round trip of its input.
      if (this.status.controller?.identity === who.identity && payload.length <= 64) {
        void this.room?.localParticipant
          .publishData(new Uint8Array(payload), { reliable: false, destinationIdentities: [who.identity], topic: TOPIC.pong })
          .catch(() => undefined);
      }
      return;
    }
    if (topic !== TOPIC.input || this.status.controller?.identity !== who.identity) return;
    let events: InputEvent[];
    try {
      const parsed = JSON.parse(decoder.decode(payload)) as { events?: unknown };
      if (!Array.isArray(parsed.events)) return;
      events = parsed.events as InputEvent[];
    } catch {
      return;
    }
    // At most MAX_EVENTS_PER_S a second.
    const now = this.deps.now();
    if (now - this.window.start >= 1000) this.window = { start: now, count: 0 };
    const room = MAX_EVENTS_PER_S - this.window.count;
    if (room <= 0) return;
    events = events.slice(0, room);
    if (events.length === 0) return;
    this.window.count += events.length;
    // Moves never wait in line: while the app is busy, only the newest is kept, and it goes
    // next. Anything else goes in order, after the move kept before it.
    if (this.pending > 0 && events.every((e) => e.t === "move")) {
      this.spare = events[events.length - 1];
      return;
    }
    if (this.spare) {
      events = [this.spare, ...events];
      this.spare = null;
    }
    await this.run(events);
  }

  private run(events: InputEvent[]): Promise<void> {
    const epoch = this.controlEpoch;
    this.pending += 1;
    this.queue = this.queue
      .then(() => epoch === this.controlEpoch && this.status.controller ? this.deps.input(events) : undefined)
      .catch((error) => {
        if (epoch === this.controlEpoch) return this.endControl(error instanceof Error ? error.message : String(error), "host");
      })
      .finally(() => {
        this.pending -= 1;
        const spare = this.spare;
        if (this.pending === 0 && spare && this.status.controller) {
          this.spare = null;
          void this.run([spare]);
        }
      });
    return this.queue;
  }
}

let mine: HostScreen | null = null;

/** In each window: this window's share, answering `screen:cmd` when it is the one sharing. */
export function startScreenHost(deps: HostDeps = hostDeps): () => void {
  const host = new HostScreen(deps, (s) => void emit(STATUS, s));
  mine = host;
  const off = listen<ScreenCmd>(CMD, (e) => {
    const c = e.payload;
    if (c.cmd === "controllers") return host.setAllowed(c.allowed, c.viewers, c.sessionId);
    if (!host.sharing && host.status.host === "off") return;
    if (c.cmd === "stop") { if (!c.sessionId || c.sessionId === host.status.sessionId) void host.stop(null); }
    else if (c.cmd === "control") void host.allow(c.person);
    else if (c.cmd === "decline") void host.decline(c.request);
    else if (c.cmd === "control_off") void host.endControl(null, "host");
    else void emit(STATUS, host.status);
  });
  // The Stop control hotkey (remote.rs has ended it already).
  const offHotkey = listen("remote:stopped", () => void host.endControl(null, "host"));
  return () => {
    if (mine === host) mine = null;
    void off.then((f) => f());
    void offHotkey.then((f) => f());
    void host.stop(null);
  };
}

/** Share the screen from this window: call it in the click handler. */
export function shareScreen(sessionId: string): Promise<void> {
  if (!mine) return Promise.reject(new Error("Screen sharing is not ready in this window."));
  return mine.share(sessionId);
}

/** Stop sharing, in whichever window shares. */
export function stopScreen(sessionId?: string): Promise<void> {
  if (!sessionId || sessionId === mine?.status.sessionId) void mine?.stop(null);
  return emit(CMD, { cmd: "stop", ...(sessionId ? { sessionId } : {}) } satisfies ScreenCmd);
}

/** Tell the sharing window who may control now (the seats holding `device.control`). */
export function screenControllers(sessionId: string | null, allowed: Person[], viewers: Person[] = allowed): Promise<void> {
  return emit(CMD, { cmd: "controllers", sessionId, allowed, viewers } satisfies ScreenCmd);
}

/** Let a guest control the shared screen. */
export function allowControl(person: ControlRequest): Promise<void> {
  return emit(CMD, { cmd: "control", person } satisfies ScreenCmd);
}

export function declineControl(request: ControlRequest): Promise<void> {
  return emit(CMD, { cmd: "decline", request } satisfies ScreenCmd);
}

/** End a guest's control now. */
export function endControl(): Promise<void> {
  return emit(CMD, { cmd: "control_off" } satisfies ScreenCmd);
}

/** Follow the share's status from either window. */
export function onScreenStatus(f: (s: ScreenStatus) => void): Promise<() => void> {
  return listen<ScreenStatus>(STATUS, (e) => f(e.payload));
}

export function askScreenStatus(): Promise<void> {
  return emit(CMD, { cmd: "status" } satisfies ScreenCmd);
}

// --- The guest ----------------------------------------------------------------------------

export interface GuestDeps {
  newRoom(): Room;
  token(seatId: string): Promise<Grant & { control?: boolean; can_request_control?: boolean }>;
}

export const guestDeps: GuestDeps = {
  newRoom: () => new Room({ adaptiveStream: false, dynacast: false }),
  token: (seatId) => rest.guestScreen(seatId),
};

export interface GuestScreenState {
  state: "connecting" | "waiting" | "watching" | "off";
  /** May this current screen viewer ask the host for control? */
  control: boolean;
  quality?: ScreenQuality;
  qualityError?: string | null;
  /** The host lets this guest control now. */
  controlling: boolean;
  /** This guest asked, and the host has not answered. */
  asked: boolean;
  error: string | null;
}

export const GUEST_OFF: GuestScreenState = { state: "off", control: false, controlling: false, asked: false, error: null };

/** A guest's view of the host's screen: joins, follows the host's track, comes back when the
 * API takes it out (its grants changed) while it may still watch. */
export class GuestScreen {
  state: GuestScreenState = GUEST_OFF;
  room: Room | null = null;
  track: RemoteTrack | null = null;
  inputMs: number | null = null;
  private closed = false;
  private retries = 0;
  private requestId: string | null = null;
  private requestTimer: ReturnType<typeof setTimeout> | undefined;
  private epoch = 0;

  constructor(
    private deps: GuestDeps,
    private seatId: string,
    private onChange: (s: GuestScreenState, track: RemoteTrack | null) => void,
    private wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
    private now: () => number = () => performance.now(),
  ) {}

  private set(patch: Partial<GuestScreenState>) {
    if (this.closed) return;
    this.state = { ...this.state, ...patch };
    if (!this.state.controlling) this.inputMs = null;
    this.onChange(this.state, this.track);
  }

  async ping(): Promise<void> {
    if (!this.state.controlling) return;
    await this.publish(TOPIC.ping, { t: this.now() }, false);
  }

  async open(): Promise<void> {
    if (this.closed || this.room) return;
    const epoch = ++this.epoch;
    let room: Room | null = null;
    const current = () => !this.closed && epoch === this.epoch;
    this.track = null;
    this.set({ ...GUEST_OFF, state: "connecting" });
    try {
      const grant = await this.deps.token(this.seatId);
      if (!current()) return;
      room = this.deps.newRoom();
      const connectedRoom = room;
      this.room = room;
      const ownsRoom = () => current() && this.room === connectedRoom;
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: unknown, who: RemoteParticipant) => {
        if (!ownsRoom() || who.identity !== HOST || track.kind !== Track.Kind.Video) return;
        try { track.setPlayoutDelay(0); } catch { /* Older WebViews retain their own buffer. */ }
        this.track = track;
        this.set({ state: "watching", error: null });
      });
      room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
        if (!ownsRoom() || track !== this.track) return;
        this.track = null;
        void this.release().catch(() => undefined);
        this.set({ state: "waiting", controlling: false, asked: false });
      });
      room.on(RoomEvent.Reconnecting, () => {
        if (!ownsRoom()) return;
        // A reconnect never preserves remote input consent.
        clearTimeout(this.requestTimer); this.requestId = null;
        void this.publish(TOPIC.release, {}).catch(() => undefined);
        this.set({ state: "connecting", controlling: false, asked: false });
      });
      room.on(RoomEvent.Reconnected, () => {
        if (!ownsRoom()) return;
        this.set({ state: this.track ? "watching" : "waiting" });
      });
      room.on(RoomEvent.DataReceived, (payload: Uint8Array, who?: RemoteParticipant, _kind?: unknown, topic?: string) => {
        if (!ownsRoom() || who?.identity !== HOST) return;
        if (topic === TOPIC.pong) {
          try {
            const sent = (JSON.parse(decoder.decode(payload)) as { t?: unknown }).t;
            if (typeof sent === "number" && this.state.controlling) this.inputMs = Math.max(0, this.now() - sent);
          } catch { /* Not our ping. */ }
          return;
        }
        if (topic === TOPIC.qualityStatus) {
          try {
            const body = JSON.parse(decoder.decode(payload));
            if (isQuality(body.quality)) this.set({ quality: body.quality, qualityError: typeof body.error === "string" ? body.error : null });
          } catch { /* Malformed status. */ }
          return;
        }
        if (topic !== TOPIC.control) return;
        try {
          const body = JSON.parse(decoder.decode(payload)) as { on?: unknown; requestId?: unknown; reason?: unknown };
          if (body.on === true && (!this.requestId || body.requestId !== this.requestId)) return;
          if (body.on !== true && body.requestId && body.requestId !== this.requestId) return;
          clearTimeout(this.requestTimer); this.requestId = null;
          this.set({ controlling: body.on === true, asked: false, error: typeof body.reason === "string" ? body.reason : null });
        } catch { /* Ignore malformed control messages. */ }
      });
      room.on(RoomEvent.Disconnected, () => { if (ownsRoom()) void this.reopen(connectedRoom); });
      // Set the permission before connect: host control may arrive during connect.
      this.set({ control: grant.can_request_control === true || grant.control === true });
      await room.connect(grant.url, grant.token, { autoSubscribe: true });
      if (!ownsRoom()) {
        await room.disconnect();
        return;
      }
      this.retries = 0;
      this.set({ state: this.track ? "watching" : "waiting" });
    } catch (e) {
      if (!current()) {
        if (room) await room.disconnect().catch(() => undefined);
        return;
      }
      this.room = null;
      this.track = null;
      if (room) await room.disconnect().catch(() => undefined);
      this.set({ ...GUEST_OFF, error: e instanceof Error ? e.message : String(e) });
    }
  }

  private async publish(topic: string, body: unknown, reliable = true): Promise<void> {
    if (this.closed) return;
    await this.room?.localParticipant.publishData(encoder.encode(JSON.stringify(body)), {
      reliable, destinationIdentities: [HOST], topic,
    });
  }

  async setQuality(quality: ScreenQuality): Promise<void> {
    if (!isQuality(quality) || this.state.state !== "watching") return;
    await this.publish(TOPIC.quality, { quality });
  }

  async ask(): Promise<void> {
    if (!this.state.control || this.state.controlling || this.state.asked || !this.room || this.closed) return;
    this.requestId = crypto.randomUUID();
    this.set({ asked: true, error: null });
    this.requestTimer = setTimeout(() => {
      this.requestId = null; this.set({ asked: false, error: "Control request expired. You can request again." });
      void this.publish(TOPIC.release, {}).catch(() => undefined);
    }, CONTROL_REQUEST_MS);
    try { await this.publish(TOPIC.ask, { requestId: this.requestId }); }
    catch (error) { clearTimeout(this.requestTimer); this.requestId = null; this.set({ asked: false }); throw error; }
  }

  async release(): Promise<void> {
    clearTimeout(this.requestTimer); this.requestId = null;
    // Release locally even if the connection fails while the command is in flight.
    this.set({ controlling: false, asked: false });
    await this.publish(TOPIC.release, {});
  }

  async send(events: InputEvent[]): Promise<void> {
    if (!this.state.controlling || events.length === 0 || this.closed) return;
    await this.publish(TOPIC.input, { events }, !events.every((e) => e.t === "move"));
  }

  private async reopen(room: Room): Promise<void> {
    if (this.closed || this.room !== room) return;
    const epoch = ++this.epoch;
    clearTimeout(this.requestTimer); this.requestId = null;
    this.room = null;
    this.track = null;
    if (this.retries >= 3) {
      this.set({ ...GUEST_OFF, error: "The shared screen was disconnected. Try connecting again." });
      return;
    }
    this.retries += 1;
    this.set({ state: "connecting", controlling: false, asked: false });
    await this.wait(500 * this.retries);
    if (!this.closed && epoch === this.epoch) await this.open();
  }

  async close(): Promise<void> {
    if (this.closed) return;
    // Tell the host before disconnect so held keys/buttons are released promptly.
    const room = this.room;
    const release = this.state.controlling || this.state.asked ? this.publish(TOPIC.release, {}).catch(() => undefined) : Promise.resolve();
    clearTimeout(this.requestTimer); this.requestId = null;
    this.closed = true;
    this.epoch += 1;
    this.room = null;
    this.track = null;
    this.state = GUEST_OFF;
    this.inputMs = null;
    await release;
    if (room) await room.disconnect().catch(() => undefined);
  }
}

// --- How fast the guest sees the screen ---------------------------------------------------

/** What the guest's readout shows; null where the browser did not say. */
export interface ViewStats {
  /** Network round trip to the media server, ms. */
  networkMs: number | null;
  /** Frames shown a second. */
  fps: number | null;
  /** Time to decode a frame, ms (over the last interval). */
  decodeMs: number | null;
  /** Time a frame waits in the receive buffer, ms (over the last interval). */
  bufferMs: number | null;
  /** The codec, e.g. "H264". */
  codec: string | null;
  width?: number | null;
  height?: number | null;
}

/** The running totals one interval is measured against. */
export interface StatsSample {
  decodeS: number;
  decoded: number;
  bufferS: number;
  emitted: number;
}

type Stat = Record<string, unknown> & { type?: string; id?: string };
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const ms = (s: number) => Math.round(s * 10_000) / 10; // seconds to ms, one decimal

/** Read a video receiver's stats (getStats) against the previous sample. */
export function readStats(report: Iterable<Stat>, prev: StatsSample | null): { stats: ViewStats; sample: StatsSample | null } {
  const all = [...report];
  const byId = new Map(all.filter((s) => typeof s.id === "string").map((s) => [s.id as string, s]));
  const inbound = all.find((s) => s.type === "inbound-rtp" && (s.kind === "video" || s.mediaType === "video"));
  const stats: ViewStats = { networkMs: null, fps: null, decodeMs: null, bufferMs: null, codec: null };

  // The pair the connection runs on: the transport says which, else the nominated one.
  const transport = all.find((s) => s.type === "transport");
  const chosen = typeof transport?.selectedCandidatePairId === "string" ? byId.get(transport.selectedCandidatePairId) : undefined;
  const pair = chosen ?? all.find((s) => s.type === "candidate-pair" && s.nominated === true && s.state === "succeeded");
  const rtt = num(pair?.currentRoundTripTime);
  if (rtt !== null) stats.networkMs = Math.round(rtt * 1000);
  if (num(inbound?.frameWidth)) stats.width = num(inbound?.frameWidth);
  if (num(inbound?.frameHeight)) stats.height = num(inbound?.frameHeight);
  if (!inbound) return { stats, sample: null };

  const fps = num(inbound.framesPerSecond);
  if (fps !== null) stats.fps = Math.round(fps);
  const codec = typeof inbound.codecId === "string" ? byId.get(inbound.codecId) : undefined;
  if (typeof codec?.mimeType === "string") stats.codec = codec.mimeType.split("/")[1] ?? null;

  const sample: StatsSample = {
    decodeS: num(inbound.totalDecodeTime) ?? 0,
    decoded: num(inbound.framesDecoded) ?? 0,
    bufferS: num(inbound.jitterBufferDelay) ?? 0,
    emitted: num(inbound.jitterBufferEmittedCount) ?? 0,
  };
  if (prev) {
    const frames = sample.decoded - prev.decoded;
    if (frames > 0) stats.decodeMs = ms((sample.decodeS - prev.decodeS) / frames);
    const out = sample.emitted - prev.emitted;
    if (out > 0) stats.bufferMs = ms((sample.bufferS - prev.bufferS) / out);
  }
  return { stats, sample };
}

/** Where a point of the element falls in the picture shown with `object-fit: contain`, 0 to 1
 * each way; null in the bars around it. */
export function pictureSpot(
  px: number,
  py: number,
  box: { left: number; top: number; width: number; height: number },
  videoWidth: number,
  videoHeight: number,
): { x: number; y: number } | null {
  if (!(box.width > 0 && box.height > 0 && videoWidth > 0 && videoHeight > 0)) return null;
  const scale = Math.min(box.width / videoWidth, box.height / videoHeight);
  const w = videoWidth * scale;
  const h = videoHeight * scale;
  const x = (px - box.left - (box.width - w) / 2) / w;
  const y = (py - box.top - (box.height - h) / 2) / h;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x, y };
}
