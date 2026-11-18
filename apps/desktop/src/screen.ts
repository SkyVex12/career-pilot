// Guest seats' screen (ARCHITECTURE.md, "Guest seats", phase 4): the host shares their screen
// with the guests the host lets see it (`device.view`). WebRTC through LiveKit, in a room of
// its own (`screen-<session id>`), apart from voice: the host's token can only share the
// screen (no audio), a guest's can only watch.
//
// The host. The browser lets a page capture the screen only right after a click in it, so the
// share runs in the window where the host clicked (the overlay or the main window), and the
// windows agree through `screen:cmd` / `screen:status` events: one window shares, the others
// show its status and can stop it. The overlay is excluded from capture (stealth.rs), so a
// guest never sees it; anything else on the shared screen or window they do see.
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
import { rest } from "./api";

export const HOST = "host";

export interface ScreenStatus {
  host: "off" | "starting" | "on";
  /** What the host shares: "monitor" (a whole screen), "window", or null when unknown. */
  surface: string | null;
  error: string | null;
}

export const SCREEN_OFF: ScreenStatus = { host: "off", surface: null, error: null };

type ScreenCmd = { cmd: "stop" } | { cmd: "status" };

const CMD = "screen:cmd";
const STATUS = "screen:status";

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
}

export const hostDeps: HostDeps = {
  newRoom: () => new Room({ adaptiveStream: false, dynacast: false }),
  capture: () =>
    createLocalScreenTracks({
      audio: false,
      contentHint: "detail",
      selfBrowserSurface: "exclude",
      surfaceSwitching: "exclude",
      systemAudio: "exclude",
    }),
  token: (sessionId) => rest.hostScreen(sessionId),
};

/** The host's share in one window. */
export class HostScreen {
  status: ScreenStatus = SCREEN_OFF;
  room: Room | null = null;
  private tracks: LocalTrack[] = [];

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
      // The host stops it from the system's own "stop sharing", or the screen goes away.
      video.mediaStreamTrack.addEventListener("ended", () => void this.stop(null));
      await room.connect(grant.url, grant.token, { autoSubscribe: false });
      await room.localParticipant.publishTrack(video, {
        source: Track.Source.ScreenShare,
        simulcast: false,
        videoEncoding: { maxBitrate: 2_500_000, maxFramerate: 15 },
      });
      const surface = video.mediaStreamTrack.getSettings().displaySurface ?? null;
      this.set({ host: "on", surface, error: null });
    } catch (e) {
      await this.stop(e instanceof Error ? e.message : String(e));
    }
  }

  async stop(error: string | null): Promise<void> {
    const room = this.room;
    this.room = null;
    for (const t of this.tracks) t.stop();
    this.tracks = [];
    if (room) await room.disconnect();
    if (room || this.status.host !== "off" || error) this.set({ host: "off", surface: null, error });
  }
}

let mine: HostScreen | null = null;

/** In each window: this window's share, answering `screen:cmd` when it is the one sharing. */
export function startScreenHost(deps: HostDeps = hostDeps): () => void {
  const host = new HostScreen(deps, (s) => void emit(STATUS, s));
  mine = host;
  const off = listen<ScreenCmd>(CMD, (e) => {
    if (!host.sharing && host.status.host === "off") return;
    if (e.payload.cmd === "stop") void host.stop(null);
    else void emit(STATUS, host.status);
  });
  return () => {
    if (mine === host) mine = null;
    void off.then((f) => f());
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
  /** May this guest send their mouse and keys (`device.control`)? */
  control: boolean;
  error: string | null;
}

/** A guest's view of the host's screen: joins, follows the host's track, comes back when the
 * API takes it out (its grants changed) while it may still watch. */
export class GuestScreen {
  state: GuestScreenState = { state: "off", control: false, error: null };
  room: Room | null = null;
  track: RemoteTrack | null = null;
  private closed = false;
  private retries = 0;

  constructor(
    private deps: GuestDeps,
    private seatId: string,
    private onChange: (s: GuestScreenState, track: RemoteTrack | null) => void,
    private wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  ) {}

  private set(patch: Partial<GuestScreenState>) {
    this.state = { ...this.state, ...patch };
    this.onChange(this.state, this.track);
  }

  async open(): Promise<void> {
    if (this.closed) return;
    this.set({ state: "connecting", error: null });
    try {
      const grant = await this.deps.token(this.seatId);
      if (this.closed) return;
      const room = this.deps.newRoom();
      this.room = room;
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: unknown, who: RemoteParticipant) => {
        if (who.identity !== HOST || track.kind !== Track.Kind.Video) return;
        this.track = track;
        this.set({ state: "watching" });
      });
      room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
        if (track !== this.track) return;
        this.track = null;
        this.set({ state: "waiting" });
      });
      room.on(RoomEvent.Disconnected, () => void this.reopen());
      await room.connect(grant.url, grant.token, { autoSubscribe: true });
      this.retries = 0;
      if (!this.track) this.set({ state: "waiting", control: grant.control === true });
      else this.set({ control: grant.control === true });
    } catch (e) {
      this.room = null;
      this.set({ state: "off", error: e instanceof Error ? e.message : String(e) });
    }
  }

  /** Taken out of the room (grants changed) or the connection dropped: try again a few times. */
  private async reopen(): Promise<void> {
    this.room = null;
    this.track = null;
    if (this.closed) return;
    if (this.retries >= 3) {
      this.set({ state: "off", error: "The screen was disconnected." });
      return;
    }
    this.retries += 1;
    this.set({ state: "connecting" });
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
