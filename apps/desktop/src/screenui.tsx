// Guest seats' screen in the UI (screen.ts; ARCHITECTURE.md, "Guest seats", phase 4).
//
// The host's side: "Your screen" in the guest panel (Share my screen, Stop), and a chip in
// the overlay's header while the screen is shared, so the host always knows and can stop it.
// The guest's side: the host's screen on the Call screen while watching, full screen on a click.
import { useEffect, useRef, useState } from "react";
import type { RemoteTrack } from "livekit-client";
import { inTauri } from "./api";
import { GuestScreen, SCREEN_OFF, askScreenStatus, guestDeps, onScreenStatus, shareScreen, stopScreen, type GuestScreenState, type ScreenStatus } from "./screen";
import type { SessionView } from "./session";
import { Button, cx } from "./ui";

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

const SURFACE: Record<string, string> = { monitor: "your whole screen", window: "a window", browser: "a tab" };

/** The guest panel's "Your screen": shown once a guest may see it. */
export function HostScreenSection({ s }: { s: SessionView }) {
  const viewers = screenViewers(s);
  const status = useScreenStatus(s.running);
  if (!s.sessionId || (viewers.length === 0 && status.host === "off")) return null;
  const sid = s.sessionId;
  const on = status.host === "on";
  return (
    <section className="seat-section seat-screen" aria-label="Your screen">
      <h3 className="seat-section-title">Your screen</h3>
      {on ? (
        <>
          <span className="seat-hint">
            Sharing {SURFACE[status.surface ?? ""] ?? "your screen"}
            {viewers.length > 0 ? ` with ${viewers.join(" and ")}` : ""}. The overlay is never shown.
          </span>
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

/** The overlay header's chip while the screen is shared: always in sight, one click stops it.
 * The share also stops with the call. */
export function ScreenChip({ s }: { s: SessionView }) {
  const status = useScreenStatus(s.running || s.sessionId !== null);
  useEffect(() => {
    if (!s.running && status.host !== "off") void stopScreen();
  }, [s.running, status.host]);
  if (status.host !== "on") return null;
  return (
    <button type="button" className="ov-screen-chip" title="Your screen is shared with guests: click to stop" onClick={() => void stopScreen()}>
      🖥 Sharing · Stop
    </button>
  );
}

/** The host's screen, for a guest who may see it (main window, while watching). */
export function GuestScreenView({ s }: { s: SessionView }) {
  const seat = s.seat;
  const seatId = seat && s.running && seat.capabilities.includes("device.view") ? seat.seatId : null;
  // The grants it was opened with: a change reopens it (the API took it out of the room).
  const control = seat?.capabilities.includes("device.control") ?? false;
  const [state, setState] = useState<GuestScreenState>({ state: "off", control: false, error: null });
  const [track, setTrack] = useState<RemoteTrack | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (!seatId || !inTauri) return;
    const g = new GuestScreen(guestDeps, seatId, (st, t) => {
      setState(st);
      setTrack(t);
    });
    void g.open();
    return () => {
      void g.close();
      setTrack(null);
      setState({ state: "off", control: false, error: null });
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
  if (!seatId || !seat) return null;
  const host = seat.hostName;
  return (
    <section className="screen-view" aria-label={`${host}'s screen`}>
      <div className="screen-view-head">
        <strong>{host}'s screen</strong>
        {state.state === "watching" && (
          <Button size="sm" variant="ghost" onClick={() => void video.current?.requestFullscreen()}>
            Full screen
          </Button>
        )}
      </div>
      <div className={cx("screen-view-frame", state.state !== "watching" && "is-empty")}>
        <video ref={video} className="screen-view-video" autoPlay playsInline muted hidden={state.state !== "watching"} />
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
