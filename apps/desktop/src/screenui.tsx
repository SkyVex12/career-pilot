// Guest seats' screen in the UI (screen.ts; ARCHITECTURE.md, "Guest seats", phases 4 and 5).
//
// The host's side: "Your screen" in the guest panel (Share my screen, Stop, and for each guest
// who may control: Let them control, Stop control), and chips in the overlay's header while
// the screen is shared, while a guest controls it, and when a guest asks to, so the host always
// knows and is one click (or Ctrl+Alt+End) from stopping it.
// The guest's side: the host's screen on the Call screen while watching, full screen on a
// click; with control, Ask to control, and while the host lets them, their mouse and keys on
// the picture go to the host's computer.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent, type RefObject } from "react";
import type { RemoteTrack } from "livekit-client";
import { inTauri } from "./api";
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
  screenControllers,
  shareScreen,
  stopScreen,
  type GuestScreenState,
  type InputEvent,
  type Person,
  type ScreenStatus,
} from "./screen";
import type { SessionView } from "./session";
import { Button, cx } from "./ui";

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

const SURFACE: Record<string, string> = { monitor: "your whole screen", window: "a window", browser: "a tab" };

/** The guest panel's "Your screen": shown once a guest may see it. */
export function HostScreenSection({ s }: { s: SessionView }) {
  const viewers = screenViewers(s);
  const controllers = screenControllersOf(s);
  const status = useScreenStatus(s.running);
  const [monitor, setMonitor] = useState("");
  if (!s.sessionId || (viewers.length === 0 && status.host === "off")) return null;
  const sid = s.sessionId;
  const on = status.host === "on";
  const whole = status.surface === "monitor";
  return (
    <section className="seat-section seat-screen" aria-label="Your screen">
      <h3 className="seat-section-title">Your screen</h3>
      {on ? (
        <>
          <span className="seat-hint">
            Sharing {SURFACE[status.surface ?? ""] ?? "your screen"}
            {viewers.length > 0 ? ` with ${viewers.join(" and ")}` : ""}. The overlay is never shown.
          </span>
          {controllers.length > 0 && !whole && <span className="seat-hint">Share a whole screen to let a guest control it.</span>}
          {whole &&
            controllers.map((p) => {
              const controlling = status.controller?.identity === p.identity;
              const asking = status.asking.some((a) => a.identity === p.identity);
              return (
                <div key={p.identity} className="seat-control">
                  <span className="seat-hint">
                    {controlling ? `${p.name} is controlling your screen. ${STOP_CONTROL_KEY} stops it anywhere.` : asking ? `${p.name} asks to control your screen.` : `${p.name} may control your screen if you let them.`}
                  </span>
                  {controlling ? (
                    <Button size="sm" variant="danger" onClick={() => void endControl()}>
                      Stop control
                    </Button>
                  ) : (
                    <Button size="sm" variant={asking ? "primary" : "secondary"} onClick={() => void allowControl(p)}>
                      Let {p.name} control
                    </Button>
                  )}
                </div>
              );
            })}
          {status.choose && (
            <div className="seat-invite" role="group" aria-label="Which screen you share">
              <select className="ui-input" aria-label="Which screen you share" value={monitor} onChange={(e) => setMonitor(e.target.value)}>
                <option value="">Which screen are you sharing?</option>
                {status.choose.monitors.map((m, i) => (
                  <option key={m.id} value={m.id}>
                    Screen {i + 1} ({m.rect.w}×{m.rect.h}
                    {m.primary ? ", main" : ""})
                  </option>
                ))}
              </select>
              <Button size="sm" disabled={!monitor} onClick={() => status.choose && void allowControl(status.choose.person, monitor)}>
                Let {status.choose.person.name} control
              </Button>
            </div>
          )}
          <div className="seat-invite">
            <Button size="sm" variant="ghost" onClick={() => void stopScreen()}>
              Stop sharing
            </Button>
          </div>
        </>
      ) : (
        <>
          <span className="seat-hint">
            {viewers.join(" and ")} may see your screen once you share it. You choose a screen or a window; the overlay is never shown.
          </span>
          <div className="seat-invite">
            {/* The picker must open from this click (screen.ts). */}
            <Button size="sm" disabled={status.host === "starting"} onClick={() => void shareScreen(sid)}>
              {status.host === "starting" ? "Choosing…" : "Share my screen"}
            </Button>
          </div>
        </>
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
  if (status.host !== "on") return null;
  const asking = status.asking[0];
  return (
    <>
      {status.controller ? (
        <button type="button" className="ov-screen-chip is-control" title={`${status.controller.name} controls your screen: click or ${STOP_CONTROL_KEY} to stop`} onClick={() => void endControl()}>
          🖱 {status.controller.name} controls · Stop
        </button>
      ) : (
        asking && (
          <button type="button" className="ov-screen-chip is-ask" title={`${asking.name} asks to control your screen: click to let them`} onClick={() => void allowControl(asking)}>
            ✋ Let {asking.name} control
          </button>
        )
      )}
      <button type="button" className="ov-screen-chip" title="Your screen is shared with guests: click to stop" onClick={() => void stopScreen()}>
        🖥 Sharing · Stop
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

/** The host's screen, for a guest who may see it (main window, while watching). */
export function GuestScreenView({ s }: { s: SessionView }) {
  const seat = s.seat;
  const seatId = seat && s.running && seat.capabilities.includes("device.view") ? seat.seatId : null;
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
              {state.asked ? "Asked…" : "Ask to control"}
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
    </section>
  );
}
