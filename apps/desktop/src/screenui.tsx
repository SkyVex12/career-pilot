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
import { useScreenViewer, type ViewerWindow } from "./screenViewer";
import "./sharing.css";

/** The key that ends a guest's control anywhere (src-tauri hotkeys.rs, STOP_CONTROL). */
export const STOP_CONTROL_KEY = "Ctrl+Alt+End";

/** The share's status, from whichever window shares. */
export function useScreenStatus(active: boolean): ScreenStatus {
  const [status, setStatus] = useState<ScreenStatus>(SCREEN_OFF);
  useEffect(() => {
    if (!active || !inTauri) { setStatus(SCREEN_OFF); return; }
    let disposed = false;
    let off: (() => void) | undefined;
    onScreenStatus(value => { if (!disposed) setStatus(value); }).then((f) => {
      if (disposed) f();
      else { off = f; void askScreenStatus(); }
    });
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
  const [error, setError] = useState<string | null>(null);
  if (!s.sessionId) return null;
  const sid = s.sessionId;
  const on = status.host === "on";
  const whole = status.surface === "monitor";
  const asking = status.asking.find(a => a.identity !== status.controller?.identity);
  const action = async (run: () => Promise<void>) => {
    setError(null);
    try { await run(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  return <section className="seat-section host-screen" aria-label="Your screen">
    <div className="host-screen-heading">
      <Icon name="monitor" size={17} />
      <div><strong>{on ? "Screen sharing on" : "Your screen"}</strong><span>{on ? (whole ? "Entire display" : "One window") : "Share only when you’re ready"}</span></div>
      <Button size="sm" variant={on ? "danger" : "secondary"} disabled={status.host === "starting" || (!on && viewers.length === 0)} onClick={() => void action(on ? stopScreen : () => shareScreen(sid))}>
        {on ? "Stop sharing" : status.host === "starting" ? "Choosing…" : "Share screen"}
      </Button>
    </div>
    {!on && viewers.length === 0 && <p className="host-screen-detail">Give an assistant screen access to share.</p>}
    {viewers.length > 0 && <p className="host-screen-detail">{on ? "Visible to" : "Screen access"}: {viewers.join(", ")}</p>}
    {status.controller && <div className="host-control-status"><Icon name="mouse" size={15} /><span><strong>{status.controller.name} has control</strong><small>{STOP_CONTROL_KEY} to stop anytime</small></span><Button size="sm" variant="danger" onClick={() => void action(endControl)}>Stop control</Button></div>}
    {on && controllers.length > 0 && !whole && <p className="host-screen-detail">Remote control needs an entire display. Stop sharing to choose a display.</p>}
    {asking && <div className="host-control-status"><span>{asking.name} requests control</span><Button size="sm" onClick={() => void action(() => allowControl(asking))}>Allow control</Button></div>}
    {(status.error || error) && <p className="seat-error" role="alert">{error || status.error}</p>}
  </section>;
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
  const buttons = useRef(new Set<number>());
  const lastSpot = useRef({ x: 0, y: 0 });
  const send = useCallback((events: InputEvent[]) => {
    if (on && guest && events.length) void guest.send(events).catch(() => undefined);
  }, [on, guest]);
  const push = useCallback((e: InputEvent) => send([e]), [send]);
  const spot = (e: { clientX: number; clientY: number }) => {
    const el = video.current;
    return el ? pictureSpot(e.clientX, e.clientY, el.getBoundingClientRect(), el.videoWidth, el.videoHeight) : null;
  };
  const letGo = useCallback(() => {
    send([
      ...[...held.current].map((code): InputEvent => ({ t: "key_up", code })),
      ...[...buttons.current].map((button): InputEvent => ({ t: "up", button, ...lastSpot.current })),
    ]);
    held.current.clear(); buttons.current.clear();
  }, [send]);
  useEffect(() => {
    window.addEventListener("blur", letGo);
    const hidden = () => { if (document.hidden) letGo(); };
    document.addEventListener("visibilitychange", hidden);
    return () => { letGo(); window.removeEventListener("blur", letGo); document.removeEventListener("visibilitychange", hidden); };
  }, [letGo]);
  useEffect(() => {
    const el = video.current?.parentElement;
    if (!el || !on) return;
    const wheel = (e: WheelEvent) => {
      e.preventDefault(); push({ t: "wheel", dx: e.deltaX, dy: e.deltaY });
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => el.removeEventListener("wheel", wheel);
  }, [on, push, video]);
  if (!on) return {};
  return {
    tabIndex: 0,
    onPointerMove: (e: PointerEvent) => {
      const p = spot(e);
      if (p) { lastSpot.current = p; push({ t: "move", ...p }); }
    },
    onPointerDown: (e: PointerEvent<HTMLDivElement>) => {
      const p = spot(e);
      if (!p) return;
      e.preventDefault(); e.currentTarget.focus(); e.currentTarget.setPointerCapture(e.pointerId);
      buttons.current.add(e.button); lastSpot.current = p;
      push({ t: "down", button: e.button, ...p });
    },
    onPointerUp: (e: PointerEvent) => {
      if (!buttons.current.delete(e.button)) return;
      const p = spot(e) ?? lastSpot.current;
      push({ t: "up", button: e.button, ...p });
    },
    onPointerCancel: letGo,
    onContextMenu: (e: MouseEvent) => e.preventDefault(),
    onKeyDown: (e: KeyboardEvent) => {
      if (e.code === "End" && e.ctrlKey && e.altKey) {
        e.preventDefault(); e.stopPropagation(); letGo(); void guest?.release().catch(() => undefined); return;
      }
      // Escape must still leave full screen. Other keys are sent only while the picture is focused.
      if (e.code === "Escape" && document.fullscreenElement) { letGo(); return; }
      e.preventDefault(); e.stopPropagation(); held.current.add(e.code); push({ t: "key_down", code: e.code });
    },
    onKeyUp: (e: KeyboardEvent) => {
      if (!held.current.delete(e.code)) return;
      e.preventDefault(); e.stopPropagation(); push({ t: "key_up", code: e.code });
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

/** The remote desktop is a first-class workspace, including when access has not yet been granted. */
export function GuestScreenView({ s, active = true, window: viewerWindow = "main" }: { s: SessionView; active?: boolean; window?: ViewerWindow }) {
  const seat = s.seat;
  const allowed = seat?.capabilities.includes("device.view") ?? false;
  const seatId = seat && s.running && allowed ? seat.seatId : null;
  const control = seat?.capabilities.includes("device.control") ?? false;
  const viewer = useScreenViewer(seatId, active, viewerWindow);
  const [state, setState] = useState<GuestScreenState>(GUEST_OFF);
  const [track, setTrack] = useState<RemoteTrack | null>(null);
  const [guest, setGuest] = useState<GuestScreen | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [full, setFull] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const desktop = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!seatId || !viewer.owns || !inTauri) return;
    let live = true;
    const g = new GuestScreen(guestDeps, seatId, (st, t) => {
      if (live) { setState(st); setTrack(t); }
    });
    setGuest(g); setError(null);
    void g.open();
    return () => {
      live = false; void g.close(); setGuest(null); setTrack(null); setState(GUEST_OFF);
    };
  }, [seatId, control, viewer.owns, attempt]);
  useEffect(() => {
    const el = video.current;
    if (!track || !el) return;
    track.attach(el);
    return () => { track.detach(el); };
  }, [track]);
  useEffect(() => {
    const changed = () => setFull(document.fullscreenElement === desktop.current);
    document.addEventListener("fullscreenchange", changed);
    return () => { document.removeEventListener("fullscreenchange", changed); };
  }, []);
  const watching = !!seatId && viewer.owns && state.state === "watching";
  const controlling = watching && control && state.controlling;
  const handlers = useControl(guest, controlling, video);
  const stats = useViewStats(guest, watching ? track : null, controlling);
  if (!seat || !s.running) return null;
  const host = seat.hostName;
  const action = async (run: () => Promise<unknown> | undefined) => {
    setError(null);
    try { await run(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };
  const fullscreen = () => action(async () => {
    if (document.fullscreenElement === desktop.current) await document.exitFullscreen();
    else if (desktop.current?.requestFullscreen) await desktop.current.requestFullscreen();
    else throw new Error("Full screen is unavailable. Maximize the Ruby window to enlarge the shared screen.");
  });
  const status = !allowed ? "No screen access" : viewer.elsewhere ? "Open in another window" : controlling ? "You have control" : watching ? "Viewing" : state.state === "off" && state.error ? "Disconnected" : state.state === "connecting" || !viewer.owns ? "Connecting" : "Waiting for screen";
  return <section ref={desktop} className={cx("screen-view remote-desktop", controlling && "is-controlling")} aria-label={`${host}'s screen`}>
    <header className="remote-toolbar">
      <div className="remote-title"><Icon name="monitor" size={17} /><strong>{host}’s screen</strong><span className={cx("remote-status", watching && "is-live", controlling && "is-control")}>{status}</span></div>
      <div className="remote-actions">
        {watching && state.control && control && !controlling && <Button size="sm" disabled={state.asked} onClick={() => void action(() => guest?.ask())}><Icon name="mouse" size={14} />{state.asked ? "Control requested" : "Take control"}</Button>}
        {controlling && <Button size="sm" variant="danger" onClick={() => void action(() => guest?.release())}>Release control</Button>}
        {(watching || full) && <Button size="sm" variant="ghost" onClick={() => void fullscreen()}><Icon name={full ? "compact" : "expand"} size={14} />{full ? "Exit full screen" : "Full screen"}</Button>}
      </div>
    </header>
    <div className={cx("remote-stage", controlling && "is-control")} aria-label={controlling ? `${host}'s screen: your mouse and keys go there` : undefined} {...handlers}>
      <video ref={video} className="remote-video" autoPlay playsInline muted hidden={!watching} />
      {!watching && <div className="remote-empty" role="status"><Icon name={allowed ? "monitor" : "shield"} size={32} />
        <strong>{!allowed ? "Screen access is off" : viewer.elsewhere ? `Screen is open in the ${viewer.elsewhere === "overlay" ? "overlay" : "main window"}` : state.error ? "Screen disconnected" : state.state === "connecting" || !viewer.owns ? "Connecting to shared screen…" : `Waiting for ${host} to share`}</strong>
        <p>{!allowed ? `${host} can enable screen access in Assistants.` : viewer.elsewhere ? "Open it here to view and control from this window." : state.error || (state.state === "waiting" ? "Their shared display will appear here automatically." : "Establishing a secure screen connection.")}</p>
        {viewer.elsewhere && <Button size="sm" onClick={viewer.takeHere}>Open shared screen here</Button>}
        {viewer.owns && state.error && <Button size="sm" onClick={() => setAttempt(value => value + 1)}>Reconnect screen</Button>}
      </div>}
    </div>
    <footer className="remote-footer"><span>{controlling ? `Click the screen to use mouse and keyboard · ${STOP_CONTROL_KEY} releases control` : watching ? (control ? "Remote control is available" : "View only · the host controls screen access") : "Your notes and voice remain available below"}</span>{watching && stats && <span className="remote-quality" title={statsLine(stats)}>{stats.fps !== null ? `${stats.fps} fps` : "Connected"}{stats.networkMs !== null ? ` · ${stats.networkMs} ms` : ""}</span>}</footer>
    {error && <p className="remote-error" role="alert">{error}</p>}
  </section>;
}
