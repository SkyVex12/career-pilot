// Guest seats' screen (ARCHITECTURE.md, "Guest seats", phases 4 and 5): the host shares their
// screen with the guests the host lets see it (`device.view`), and may let one guest who holds
// `device.control` drive it with their mouse and keys. WebRTC through LiveKit, in a room of its
// own (`screen-<session id>`), apart from voice: the host's token can only share the screen
// (and talk to guests over data), a guest's can only watch (and send data with control).
//
// The host. The browser lets a page capture the screen only right after a click in it, so the
// share runs in the window where the host clicked (the overlay or the main window), and the
// windows agree through `screen:cmd` / `screen:status` events: one window shares, the others
// show its status and drive it. The overlay is excluded from capture (stealth.rs), so a guest
// never sees it; anything else on the shared screen or window they do see.
//
// Control (phase 5). Only while the host shares a whole screen, only a guest whose seat holds
// `device.control` (the host's windows pass the list on as it changes), and only after the
// host lets that guest control: then the guest's events (data, topic `input`) are handed to
// the app (remote.rs), which places them on the shared monitor. One guest at a time. It ends
// with Stop control, the Stop control hotkey, the guest's grant going away, the share ending,
// or the guest letting go; the app then lets go of every key and button the guest held.
//
// The guest watches in the main window (Watching, on the Call screen), which joins the room
// while their seat has `device.view`, and comes back by itself when the host changes what the
// seat may do (the API takes the guest out of the room, and the next token says what holds).
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

export const HOST = "host";

export interface Person {
  identity: string;
  name: string;
}

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
}

/** One event of a controlling guest (remote.rs `Input`); x and y are 0 to 1 across the picture. */
export type InputEvent =
  | { t: "move"; x: number; y: number }
  | { t: "down" | "up"; button: number; x: number; y: number }
  | { t: "wheel"; dx: number; dy: number }
  | { t: "key_down" | "key_up"; code: string };

export interface ScreenStatus {
  host: "off" | "starting" | "on";
  /** What the host shares: "monitor" (a whole screen), "window", or null when unknown. */
  surface: string | null;
  /** The guest controlling the screen now. */
  controller: Person | null;
  /** Guests who asked to control. */
  asking: Person[];
  /** More than one monitor could be the shared one: the host says which, for this guest. */
  choose: { person: Person; monitors: Monitor[] } | null;
  /** Guests the host stopped (Stop control, the hotkey): the UI takes their Control away. */
  stopped: Person[];
  error: string | null;
}

export const SCREEN_OFF: ScreenStatus = { host: "off", surface: null, controller: null, asking: [], choose: null, stopped: [], error: null };

type ScreenCmd =
  | { cmd: "stop" }
  | { cmd: "status" }
  | { cmd: "controllers"; allowed: Person[] }
  | { cmd: "control"; person: Person; monitorId?: string }
  | { cmd: "control_off" };

/** Why control ended: the host stopped it (their Control is then taken away), the guest gave
 * it back (they take it again with Take control), or something else did (the grant went, the
 * guest left, the share stopped). */
export type EndedBy = "host" | "guest" | "other";

const CMD = "screen:cmd";
const STATUS = "screen:status";
/** Data topics: the guest's events and asks, the host's word on control, and the controlling
 * guest's ping (the host sends it straight back) to time the input path. */
export const TOPIC = { input: "input", ask: "ask", release: "release", control: "control", ping: "ping", pong: "pong" } as const;
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
  capture(): Promise<LocalTrack[]>;
  token(sessionId: string): Promise<Grant>;
  monitors(): Promise<Monitor[]>;
  begin(target: Rect): Promise<void>;
  input(events: InputEvent[]): Promise<void>;
  end(): Promise<void>;
  now(): number;
}

export const hostDeps: HostDeps = {
  newRoom: () => new Room({ adaptiveStream: false, dynacast: false }),
  capture: () =>
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
  return monitors.filter((m) => m.rect.h > 0 && Math.abs(m.rect.w / m.rect.h - ratio) < 0.02);
}

/** The host's share in one window. */
export class HostScreen {
  status: ScreenStatus = SCREEN_OFF;
  room: Room | null = null;
  private tracks: LocalTrack[] = [];
  private allowed: Person[] = [];
  private queue: Promise<void> = Promise.resolve();
  private pending = 0;
  /** The newest move that came while the app was busy: only where the pointer ends up counts. */
  private spare: InputEvent | null = null;
  private window = { start: 0, count: 0 };
  /** Guests who don't get control by themselves now: they gave it back ("guest", until they
   * take it again) or the host stopped them ("host", until their Control is given again). */
  private blocked = new Map<string, EndedBy>();
  /** The monitor the host picked for this share, when several fit: asked once per share. */
  private target: Monitor | null = null;
  private granting = false;
  private stopping = false;

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
    this.stopping = false;
    this.set({ host: "starting", error: null });
    let tracks: LocalTrack[] = [];
    try {
      tracks = await this.deps.capture();
    } catch {
      // Closing the picker is no error: nothing was chosen.
      this.set({ host: "off", surface: null, error: null });
      return;
    }
    try {
      const video = tracks.find((t) => t.kind === Track.Kind.Video);
      if (!video) throw new Error("No screen was chosen.");
      this.tracks = tracks;
      const grant = await this.deps.token(sessionId);
      const room = this.deps.newRoom();
      this.room = room;
      room.on(RoomEvent.Disconnected, () => void this.stop(this.room ? "Screen sharing was disconnected." : null));
      room.on(RoomEvent.DataReceived, (payload: Uint8Array, who?: RemoteParticipant, _kind?: unknown, topic?: string) => {
        if (who) void this.data(payload, who, topic);
      });
      room.on(RoomEvent.ParticipantDisconnected, (who: RemoteParticipant) => {
        if (this.status.controller?.identity === who.identity) void this.endControl(null);
        this.set({ asking: this.status.asking.filter((p) => p.identity !== who.identity) });
      });
      // A guest who (re)joins: told if they control already, or given control if they may.
      room.on(RoomEvent.ParticipantConnected, (who: RemoteParticipant) => {
        if (this.status.controller?.identity === who.identity) void this.tell(who.identity, true);
        else void this.auto();
      });
      // The host stops it from the system's own "stop sharing", or the screen goes away.
      video.mediaStreamTrack.addEventListener("ended", () => void this.stop(null));
      await room.connect(grant.url, grant.token, { autoSubscribe: false });
      await room.localParticipant.publishTrack(video, {
        source: Track.Source.ScreenShare,
        simulcast: false,
        // H.264: Windows encodes it on the graphics card where it can, which is faster than
        // VP8 in software and leaves the CPU to the call. Every guest is this app (WebView2),
        // which plays it.
        videoCodec: SCREEN_CODEC,
        videoEncoding: { maxBitrate: ENCODING.watch.maxBitrate, maxFramerate: ENCODING.watch.maxFramerate },
        degradationPreference: ENCODING.watch.degradation,
      });
      const surface = video.mediaStreamTrack.getSettings().displaySurface ?? null;
      this.set({ host: "on", surface, error: null });
      await this.auto();
    } catch (e) {
      await this.stop(e instanceof Error ? e.message : String(e));
    }
  }

  async stop(error: string | null): Promise<void> {
    this.stopping = true;
    await this.endControl(null);
    const room = this.room;
    this.room = null;
    for (const t of this.tracks) t.stop();
    this.tracks = [];
    this.target = null;
    // A new share starts afresh for guests who gave control back; the host's stops hold.
    for (const [id, by] of this.blocked) if (by === "guest") this.blocked.delete(id);
    if (room) await room.disconnect();
    if (room || this.status.host !== "off" || error) this.set({ ...SCREEN_OFF, stopped: this.status.stopped, error });
  }

  // --- Control ---------------------------------------------------------------------------

  /** The seats that hold `device.control` now; anyone else loses control and their ask. */
  setAllowed(allowed: Person[]): void {
    this.allowed = allowed;
    const may = (p: Person | null) => p !== null && allowed.some((a) => a.identity === p.identity);
    // Control taken away clears a stop: given again, it works again.
    for (const id of [...this.blocked.keys()]) if (!allowed.some((a) => a.identity === id)) this.blocked.delete(id);
    if (this.status.controller && !may(this.status.controller)) void this.endControl(null);
    const asking = this.status.asking.filter((p) => may(p));
    const stopped = this.status.stopped.filter((p) => may(p));
    if (asking.length !== this.status.asking.length || stopped.length !== this.status.stopped.length) this.set({ asking, stopped });
    void this.auto();
  }

  /** Is this guest in the screen's room now? (Unknown counts as yes.) */
  private present(identity: string): boolean {
    const there = (this.room as { remoteParticipants?: Map<string, unknown> } | null)?.remoteParticipants;
    return there instanceof Map ? there.has(identity) : true;
  }

  /** A guest with Control controls as soon as the host shares a whole screen: the first one
   * who is in the room and isn't held back. One at a time. */
  private async auto(): Promise<void> {
    if (this.granting || this.stopping || !this.room || this.status.host !== "on" || this.status.surface !== "monitor") return;
    if (this.status.controller || this.status.choose) return;
    const next = this.allowed.find((p) => !this.blocked.has(p.identity) && this.present(p.identity));
    if (next) await this.allow(next);
  }

  private shared(): { width: number; height: number } | null {
    const video = this.tracks.find((t) => t.kind === Track.Kind.Video);
    if (!video) return null;
    const s = video.mediaStreamTrack.getSettings();
    return { width: s.width ?? 0, height: s.height ?? 0 };
  }

  /** Let this guest control the shared screen (`monitorId` when the host chose among several). */
  async allow(person: Person, monitorId?: string): Promise<void> {
    if (this.granting) return;
    this.granting = true;
    try {
      if (!this.room || this.status.host !== "on") throw new Error("Share your screen first.");
      if (this.status.surface !== "monitor") throw new Error("Share a whole screen to let a guest control it.");
      if (!this.allowed.some((a) => a.identity === person.identity)) throw new Error(`${person.name} may not control your screen.`);
      this.blocked.delete(person.identity);
      const all = await this.deps.monitors();
      let target: Monitor | undefined;
      if (monitorId) target = all.find((m) => m.id === monitorId);
      else if (this.target) target = all.find((m) => m.id === this.target?.id);
      if (target) this.target = target;
      else {
        const size = this.shared();
        const fit = size ? matchingMonitors(all, size.width, size.height) : all;
        const candidates = fit.length > 0 ? fit : all;
        if (candidates.length === 1) target = candidates[0];
        else if (candidates.length > 1) {
          this.set({ choose: { person, monitors: candidates }, error: null });
          return;
        }
      }
      if (!target) throw new Error("The shared screen was not found.");
      if (this.status.controller && this.status.controller.identity !== person.identity) await this.endControl(null);
      await this.deps.begin(target.rect);
      this.set({ controller: person, choose: null, asking: this.status.asking.filter((p) => p.identity !== person.identity), error: null });
      await this.tell(person.identity, true);
      await this.tune("control");
    } catch (e) {
      this.set({ choose: null, error: e instanceof Error ? e.message : String(e) });
    } finally {
      this.granting = false;
    }
  }

  /** Control ends: the app lets go of everything the guest held, and the guest is told. */
  async endControl(error: string | null, by: EndedBy = "other"): Promise<void> {
    const was = this.status.controller;
    let stopped = this.status.stopped;
    if (by === "host") {
      // The host's stop holds for every guest who could take over, until Control is given again.
      for (const p of this.allowed) this.blocked.set(p.identity, "host");
      stopped = [...stopped, ...this.allowed.filter((p) => !stopped.some((q) => q.identity === p.identity))];
    } else if (was && by === "guest") this.blocked.set(was.identity, "guest");
    if (!was && !this.status.choose) {
      if (stopped !== this.status.stopped) this.set({ stopped });
      return;
    }
    this.set({ controller: null, choose: null, stopped, error: error ?? this.status.error });
    this.spare = null;
    try {
      await this.deps.end();
    } finally {
      if (was) await this.tell(was.identity, false);
      if (was) await this.tune("watch");
    }
    if (by !== "host") await this.auto();
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
        e.maxBitrate = want.maxBitrate;
        e.maxFramerate = want.maxFramerate;
      }
      p.degradationPreference = want.degradation;
      await sender.setParameters(p);
    } catch {
      // The share keeps its encoding; control works the same, only less smoothly.
    }
  }

  private async tell(identity: string, on: boolean): Promise<void> {
    try {
      await this.room?.localParticipant.publishData(encoder.encode(JSON.stringify({ on })), {
        reliable: true,
        destinationIdentities: [identity],
        topic: TOPIC.control,
      });
    } catch {
      // The guest is gone; control is over on this side either way.
    }
  }

  private async data(payload: Uint8Array, who: RemoteParticipant, topic?: string): Promise<void> {
    const person = this.allowed.find((a) => a.identity === who.identity);
    if (!person) return; // no device.control: nothing a guest sends counts
    if (topic === TOPIC.ping) {
      // Straight back, untouched: the guest times the round trip of its input.
      if (this.status.controller?.identity === who.identity && payload.length <= 64) {
        void this.room?.localParticipant
          .publishData(new Uint8Array(payload), { reliable: false, destinationIdentities: [who.identity], topic: TOPIC.pong })
          .catch(() => undefined);
      }
      return;
    }
    if (topic === TOPIC.ask) {
      // Take control: a guest who gave it back takes it again at once if no one has it;
      // otherwise the host sees the ask. A guest the host stopped is losing Control: ignored.
      if (this.blocked.get(who.identity) === "host" || this.status.controller?.identity === who.identity) return;
      if (this.blocked.get(who.identity) === "guest") this.blocked.delete(who.identity);
      if (!this.status.controller && this.status.surface === "monitor" && this.room) {
        await this.allow(person);
        if ((this.status.controller as Person | null)?.identity === who.identity) return;
      }
      if (!this.status.asking.some((p) => p.identity === who.identity)) this.set({ asking: [...this.status.asking, person] });
      return;
    }
    if (topic === TOPIC.release) {
      if (this.status.controller?.identity === who.identity) await this.endControl(null, "guest");
      else this.blocked.set(who.identity, "guest");
      this.set({ asking: this.status.asking.filter((p) => p.identity !== who.identity) });
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
    this.pending += 1;
    this.queue = this.queue
      .then(() => this.deps.input(events))
      .catch(() => undefined)
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
    if (c.cmd === "controllers") return host.setAllowed(c.allowed); // every window keeps the list
    if (!host.sharing && host.status.host === "off") return;
    if (c.cmd === "stop") void host.stop(null);
    else if (c.cmd === "control") void host.allow(c.person, c.monitorId);
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
export function stopScreen(): Promise<void> {
  void mine?.stop(null);
  return emit(CMD, { cmd: "stop" } satisfies ScreenCmd);
}

/** Tell the sharing window who may control now (the seats holding `device.control`). */
export function screenControllers(allowed: Person[]): Promise<void> {
  return emit(CMD, { cmd: "controllers", allowed } satisfies ScreenCmd);
}

/** Let a guest control the shared screen. */
export function allowControl(person: Person, monitorId?: string): Promise<void> {
  return emit(CMD, { cmd: "control", person, monitorId } satisfies ScreenCmd);
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
  token(seatId: string): Promise<Grant & { control?: boolean }>;
}

export const guestDeps: GuestDeps = {
  newRoom: () => new Room({ adaptiveStream: true, dynacast: false }),
  token: (seatId) => rest.guestScreen(seatId),
};

export interface GuestScreenState {
  state: "connecting" | "waiting" | "watching" | "off";
  /** May this guest ask to control (`device.control`)? */
  control: boolean;
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
  /** The last round trip of a ping over the input path (guest → host → guest), in ms. */
  inputMs: number | null = null;
  private closed = false;
  private retries = 0;

  constructor(
    private deps: GuestDeps,
    private seatId: string,
    private onChange: (s: GuestScreenState, track: RemoteTrack | null) => void,
    private wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
    private now: () => number = () => performance.now(),
  ) {}

  private set(patch: Partial<GuestScreenState>) {
    this.state = { ...this.state, ...patch };
    if (!this.state.controlling) this.inputMs = null;
    this.onChange(this.state, this.track);
  }

  /** Time the input path once (while controlling); the answer lands in `inputMs`. */
  async ping(): Promise<void> {
    if (!this.state.controlling) return;
    await this.publish(TOPIC.ping, { t: this.now() }, false);
  }

  async open(): Promise<void> {
    if (this.closed) return;
    this.set({ state: "connecting", controlling: false, asked: false, error: null });
    try {
      const grant = await this.deps.token(this.seatId);
      if (this.closed) return;
      const room = this.deps.newRoom();
      this.room = room;
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: unknown, who: RemoteParticipant) => {
        if (who.identity !== HOST || track.kind !== Track.Kind.Video) return;
        // Show each frame as soon as it can be: no smoothing buffer, which would cost the
        // guest's hand its feel of the screen.
        try {
          track.setPlayoutDelay(0);
        } catch {
          // Not supported here: the browser's own buffer stays.
        }
        this.track = track;
        this.set({ state: "watching" });
      });
      room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
        if (track !== this.track) return;
        this.track = null;
        this.set({ state: "waiting", controlling: false, asked: false });
      });
      room.on(RoomEvent.DataReceived, (payload: Uint8Array, who?: RemoteParticipant, _kind?: unknown, topic?: string) => {
        if (who?.identity !== HOST) return;
        if (topic === TOPIC.pong) {
          try {
            const sent = (JSON.parse(decoder.decode(payload)) as { t?: unknown }).t;
            if (typeof sent === "number" && this.state.controlling) this.inputMs = Math.max(0, this.now() - sent);
          } catch {
            // Not our ping: ignored.
          }
          return;
        }
        if (topic !== TOPIC.control) return;
        try {
          const on = (JSON.parse(decoder.decode(payload)) as { on?: unknown }).on === true;
          this.set({ controlling: on, asked: false });
        } catch {
          // Not the host's word: ignored.
        }
      });
      room.on(RoomEvent.Disconnected, () => void this.reopen());
      await room.connect(grant.url, grant.token, { autoSubscribe: true });
      this.retries = 0;
      this.set({ state: this.track ? "watching" : "waiting", control: grant.control === true });
    } catch (e) {
      this.room = null;
      this.set({ state: "off", controlling: false, error: e instanceof Error ? e.message : String(e) });
    }
  }

  private async publish(topic: string, body: unknown, reliable = true): Promise<void> {
    await this.room?.localParticipant.publishData(encoder.encode(JSON.stringify(body)), {
      reliable,
      destinationIdentities: [HOST],
      topic,
    });
  }

  /** Ask the host to let this guest control. */
  async ask(): Promise<void> {
    if (!this.state.control || this.state.controlling) return;
    await this.publish(TOPIC.ask, {});
    this.set({ asked: true });
  }

  /** Give control back. */
  async release(): Promise<void> {
    await this.publish(TOPIC.release, {});
    this.set({ controlling: false, asked: false });
  }

  /** The guest's mouse and keys, while the host lets them control. Moves alone go lossy: one
   * that is lost is overtaken by the next, where a resent one would hold up all after it.
   * Clicks, keys and the wheel always arrive (each carries its own spot). */
  async send(events: InputEvent[]): Promise<void> {
    if (!this.state.controlling || events.length === 0) return;
    await this.publish(TOPIC.input, { events }, !events.every((e) => e.t === "move"));
  }

  /** Taken out of the room (grants changed) or the connection dropped: try again a few times. */
  private async reopen(): Promise<void> {
    this.room = null;
    this.track = null;
    if (this.closed) return;
    if (this.retries >= 3) {
      this.set({ state: "off", controlling: false, error: "The screen was disconnected." });
      return;
    }
    this.retries += 1;
    this.set({ state: "connecting", controlling: false });
    await this.wait(500 * this.retries);
    await this.open();
  }

  async close(): Promise<void> {
    this.closed = true;
    const room = this.room;
    this.room = null;
    this.track = null;
    if (room) await room.disconnect();
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
