// Guest seats' screen in the UI (screen.ts; ARCHITECTURE.md, "Guest seats", phases 4 and 5).
//
// The host's side: "Your screen" in the guest panel (Share screen, Stop sharing, Stop control),
// and chips in the overlay's header while the screen is shared and while a guest controls it,
// so the host always knows and is one click (or Ctrl+Alt+End) from stopping it. A guest with
// Control controls as soon as the host shares a whole screen (screen.ts); the host's Stop takes
// their Control away, which the header's chip does here.
// The guest's side: the host's screen on the Call screen while watching, full screen on a
// click; with Control, their mouse and keys on the picture go to the host's computer, Stop
// controlling gives it back and Take control takes it again.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent, type RefObject } from "react";
import type { RemoteTrack } from "livekit-client";
import { inTauri, rest } from "./api";
import {
  GUEST_OFF,
  GuestScreen,
  SCREEN_OFF,
  allowControl,
  askScreenStatus,
  endControl,
  guestDeps,
  onScreenStatus,
  pictureSpot,
  readStats,
  screenControllers,
  shareScreen,
  stopScreen,
  type GuestScreenState,
  type InputEvent,
  type Person,
  type ScreenStatus,
  type StatsSample,
  type ViewStats,
} from "./screen";
import type { SessionView } from "./session";
import { Button, cx } from "./ui";
import { Icon } from "./ui/Icon";

/** The key that ends a guest's control anywhere (src-tauri hotkeys.rs, STOP_CONTROL). */
export const STOP_CONTROL_KEY = "Ctrl+Alt+End";

/** The share's status, from whichever window shares. */
export function useScreenStatus(active: boolean): ScreenStatus {
  const [status, setStatus] = useState<ScreenStatus>(SCREEN_OFF);
  useEffect(() => {
    if (!active || !inTauri) return;
    let disposed = false;
    let off: (() => void) | undefined;
    onScreenStatus(setStatus).then((f) => {
      if (disposed) f();
      else off = f;
    });
    void askScreenStatus();
    return () => {
      disposed = true;
      off?.();
    };
  }, [active]);
  return status;
}

/** The seats that may see the host's screen. */
export function screenViewers(s: SessionView): string[] {
  return s.seats.filter((x) => x.status === "admitted" && x.capabilities.includes("device.view")).map((x) => x.name);
}

/** The seats that may control it (`device.control`), as LiveKit knows them. */
export function screenControllersOf(s: SessionView): Person[] {
  return s.seats
    .filter((x) => x.status === "admitted" && x.capabilities.includes("device.control"))
    .map((x) => ({ identity: `seat:${x.seat_id}`, name: x.name }));
}

/** The guest panel's "Your screen": shown once a guest may see it. One button to share; then
 * who controls, and the one thing to do about it. */
export function HostScreenSection({ s }: { s: SessionView }) {
  const viewers = screenViewers(s);
  const controllers = screenControllersOf(s);
  const status = useScreenStatus(s.running);
  if (!s.sessionId || (viewers.length === 0 && status.host === "off")) return null;
  const sid = s.sessionId;
  const on = status.host === "on";
  const whole = status.surface === "monitor";
  const names = (people: { name: string }[]) => people.map((p) => p.name).join(" and ");
  const asking = status.asking.find((a) => a.identity !== status.controller?.identity);
  return (
    <section className="seat-section seat-screen" aria-label="Your screen">
      {on ? (
        <>
          <div className="seat-invite">
            <span className="seat-hint seat-grow">Sharing {whole ? "your screen" : "a window"}{viewers.length > 0 ? ` with ${viewers.join(" and ")}` : ""}.</span>
            <Button size="sm" variant="ghost" onClick={() => void stopScreen()}>
              Stop sharing
            </Button>
          </div>
          {status.controller ? (
            <div className="seat-invite seat-control">
              <span className="seat-hint seat-grow">
                {status.controller.name} controls it. {STOP_CONTROL_KEY} stops it anywhere.
              </span>
              <Button size="sm" variant="danger" onClick={() => void endControl()}>
                Stop control
              </Button>
            </div>
          ) : (
            controllers.length > 0 && !whole && <span className="seat-hint">A window is view only. Share your whole screen to give {names(controllers)} control.</span>
          )}
          {asking && (
            <div className="seat-invite">
              <span className="seat-hint seat-grow">{asking.name} asks to take over.</span>
              <Button size="sm" onClick={() => void allowControl(asking)}>
                Let {asking.name} control
              </Button>
            </div>
          )}
        </>
      ) : (
        <div className="seat-invite">
          <span className="seat-hint seat-grow">
            {controllers.length > 0 ? `Pick your whole screen so ${names(controllers)} can control it.` : `${viewers.join(" and ")} will see it. The overlay is never shown.`}
          </span>
          {/* The picker must open from this click (screen.ts). */}
          <Button size="sm" disabled={status.host === "starting"} onClick={() => void shareScreen(sid)}>
            {status.host === "starting" ? "Choosing…" : "Share screen"}
          </Button>
        </div>
      )}
      {status.error && (
        <p className="seat-error" role="alert">
          {status.error}
        </p>
      )}
    </section>
  );
}

/** The overlay header's chips while the screen is shared: always in sight, one click stops
 * it; while a guest controls it, one click (or the hotkey) ends that. The share also stops
 * with the call. This also keeps the sharing window told who may control. */
export function ScreenChip({ s }: { s: SessionView }) {
  const status = useScreenStatus(s.running || s.sessionId !== null);
  const allowed = screenControllersOf(s);
  const key = allowed.map((p) => `${p.identity}=${p.name}`).join(",");
  useEffect(() => {
    if (inTauri) void screenControllers(allowed);
    // The list, by value: `key` changes when it does.
  }, [key]);
  useEffect(() => {
    if (!s.running && status.host !== "off") void stopScreen();
  }, [s.running, status.host]);
  // The host stopped control: the guests it held back lose Control, so it stays stopped until
  // the host gives it again on their card.
  const stoppedKey = status.stopped.map((p) => p.identity).join(",");
  useEffect(() => {
    const sid = s.sessionId;
    if (!inTauri || !sid || !stoppedKey) return;
    for (const identity of stoppedKey.split(",")) {
      const seat = s.seats.find((x) => `seat:${x.seat_id}` === identity);
      if (!seat?.capabilities.includes("device.control")) continue;
      void rest
        .setGuestCapabilities(
          sid,
          seat.seat_id,
          seat.capabilities.filter((c) => c !== "device.control"),
        )
        .catch(() => undefined);
    }
    // Once per stop: `stoppedKey` changes when it does.
  }, [stoppedKey]);
  if (status.host !== "on") return null;
  const asking = status.asking.find((person) => person.identity !== status.controller?.identity);
  return (
    <>
      {status.controller ? (
        <button type="button" className="ov-screen-chip is-control" title={`${status.controller.name} controls your screen: click or ${STOP_CONTROL_KEY} to stop`} onClick={() => void endControl()}>
          <Icon name="mouse" size={14} /> {status.controller.name} controls · Stop
        </button>
      ) : null}
      {asking && (
          <button type="button" className="ov-screen-chip is-ask" title={`${asking.name} asks to control your screen: click to let them`} onClick={() => void allowControl(asking)}>
            <Icon name="users" size={14} /> Let {asking.name} control
          </button>
      )}
      <button type="button" className="ov-screen-chip" title="Your screen is shared with assistants: click to stop" onClick={() => void stopScreen()}>
        <Icon name="monitor" size={14} /> Sharing · Stop
      </button>
    </>
  );
}

/** A controlling guest's mouse and keys on the picture, each sent the moment it happens (the
 * browser already gives at most one move a frame). Keys held when the picture loses focus are
 * let go. */
function useControl(guest: GuestScreen | null, on: boolean, video: RefObject<HTMLVideoElement | null>) {
  const held = useRef(new Set<string>());
  const send = useCallback(
    (events: InputEvent[]) => {
      if (on && guest && events.length > 0) void guest.send(events).catch(() => undefined);
    },
    [on, guest],
  );
  const push = useCallback((e: InputEvent) => send([e]), [send]);
  const spot = (e: { clientX: number; clientY: number }) => {
    const el = video.current;
    if (!el) return null;
    return pictureSpot(e.clientX, e.clientY, el.getBoundingClientRect(), el.videoWidth, el.videoHeight);
  };
  const letGo = useCallback(() => {
    send([...held.current].map((code): InputEvent => ({ t: "key_up", code })));
    held.current.clear();
  }, [send]);
  useEffect(() => {
    if (!on) held.current.clear();
  }, [on]);
  // The wheel needs a listener that may stop the page from scrolling (React's is passive).
  useEffect(() => {
    const el = video.current?.parentElement;
    if (!el || !on) return;
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      push({ t: "wheel", dx: e.deltaX, dy: e.deltaY });
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [on, push, video]);
  if (!on) return {};
  return {
    tabIndex: 0,
    onPointerMove: (e: PointerEvent) => {
      const p = spot(e);
      if (p) push({ t: "move", ...p });
    },
    onPointerDown: (e: PointerEvent<HTMLDivElement>) => {
      const p = spot(e);
      if (!p) return;
      e.preventDefault();
      e.currentTarget.focus();
      e.currentTarget.setPointerCapture(e.pointerId);
      push({ t: "down", button: e.button, ...p });
    },
    onPointerUp: (e: PointerEvent) => {
      const p = spot(e) ?? { x: 0, y: 0 };
      push({ t: "up", button: e.button, x: Math.min(1, Math.max(0, p.x)), y: Math.min(1, Math.max(0, p.y)) });
    },
    onContextMenu: (e: MouseEvent) => e.preventDefault(),
    onKeyDown: (e: KeyboardEvent) => {
      e.preventDefault();
      held.current.add(e.code);
      push({ t: "key_down", code: e.code });
    },
    onKeyUp: (e: KeyboardEvent) => {
      e.preventDefault();
      held.current.delete(e.code);
      push({ t: "key_up", code: e.code });
    },
    onBlur: letGo,
  };
}

/** How fast the guest sees the screen, read once a second while watching; while controlling,
 * the input path is timed too (a ping the host sends straight back). */
function useViewStats(guest: GuestScreen | null, track: RemoteTrack | null, controlling: boolean) {
  const [stats, setStats] = useState<(ViewStats & { inputMs: number | null }) | null>(null);
  useEffect(() => {
    if (!track) {
      setStats(null);
      return;
    }
    let prev: StatsSample | null = null;
    let live = true;
    const tick = async () => {
      if (controlling) void guest?.ping().catch(() => undefined);
      const report = await track.getRTCStatsReport().catch(() => undefined);
      if (!live || !report) return;
      const read = readStats(report.values() as Iterable<Record<string, unknown>>, prev);
      prev = read.sample;
      setStats({ ...read.stats, inputMs: controlling ? (guest?.inputMs ?? null) : null });
    };
    void tick();
    const timer = setInterval(() => void tick(), 1000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [guest, track, controlling]);
  return stats;
}

/** The readout's parts, in plain words, leaving out what the browser did not say. */
export function statsLine(s: ViewStats & { inputMs: number | null }): string {
  const parts: string[] = [];
  if (s.inputMs !== null) parts.push(`Your input ${Math.round(s.inputMs)} ms there and back`);
  if (s.networkMs !== null) parts.push(`Network ${s.networkMs} ms`);
  if (s.fps !== null) parts.push(`${s.fps} fps`);
  if (s.decodeMs !== null) parts.push(`Decode ${s.decodeMs} ms`);
  if (s.bufferMs !== null) parts.push(`Buffer ${s.bufferMs} ms`);
  if (s.codec) parts.push(s.codec);
  return parts.join(" · ");
}

/** The host's screen, for a guest who may see it (main window, while watching). */
export function GuestScreenView({ s, active = true }: { s: SessionView; active?: boolean }) {
  const seat = s.seat;
  const seatId = active && seat && s.running && seat.capabilities.includes("device.view") ? seat.seatId : null;
  // The grants it was opened with: a change reopens it (the API took it out of the room).
  const control = seat?.capabilities.includes("device.control") ?? false;
  const [state, setState] = useState<GuestScreenState>(GUEST_OFF);
  const [track, setTrack] = useState<RemoteTrack | null>(null);
  const [guest, setGuest] = useState<GuestScreen | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!seatId || !inTauri) return;
    const g = new GuestScreen(guestDeps, seatId, (st, t) => {
      setState(st);
      setTrack(t);
    });
    setGuest(g);
    void g.open();
    return () => {
      void g.close();
      setGuest(null);
      setTrack(null);
      setState(GUEST_OFF);
    };
  }, [seatId, control]);
  useEffect(() => {
    const el = video.current;
    if (!track || !el) return;
    track.attach(el);
    return () => {
      track.detach(el);
    };
  }, [track]);
  const controlling = state.controlling && state.state === "watching";
  const handlers = useControl(guest, controlling, video);
  const stats = useViewStats(guest, state.state === "watching" ? track : null, controlling);
  if (!seatId || !seat) return null;
  const host = seat.hostName;
  const watching = state.state === "watching";
  return (
    <section className="screen-view" aria-label={`${host}'s screen`}>
      <div className="screen-view-head">
        <strong>{host}'s screen</strong>
        <span className="screen-view-actions">
          {watching && state.control && !state.controlling && (
            <Button size="sm" disabled={state.asked} onClick={() => void guest?.ask()}>
              {state.asked ? `Waiting for ${host}…` : "Take control"}
            </Button>
          )}
          {controlling && (
            <Button size="sm" variant="ghost" onClick={() => void guest?.release()}>
              Stop controlling
            </Button>
          )}
          {watching && (
            <Button size="sm" variant="ghost" onClick={() => void frame.current?.requestFullscreen()}>
              Full screen
            </Button>
          )}
        </span>
      </div>
      {controlling && <p className="screen-view-hint">You control {host}'s screen. Click the picture to type there; click outside it to stop typing.</p>}
      <div
        ref={frame}
        className={cx("screen-view-frame", !watching && "is-empty", controlling && "is-control")}
        aria-label={controlling ? `${host}'s screen: your mouse and keys go there` : undefined}
        {...handlers}
      >
        <video ref={video} className="screen-view-video" autoPlay playsInline muted hidden={!watching} />
        {state.state === "connecting" && <span className="screen-view-note">Connecting…</span>}
        {state.state === "waiting" && <span className="screen-view-note">{host} isn't sharing their screen right now.</span>}
        {state.state === "off" && state.error && (
          <span className="screen-view-note" role="alert">
            {state.error}
          </span>
        )}
      </div>
      {state.state === "watching" && stats && statsLine(stats) && (
        <p
          className="screen-view-stats"
          title="Your input: a ping to the host's computer and back. Network: to the media server and back. Decode and Buffer: per frame on this computer."
        >
          {statsLine(stats)}
        </p>
      )}
    </section>
  );
}
