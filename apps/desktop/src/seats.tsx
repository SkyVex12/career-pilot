// Guest seats in the UI (ARCHITECTURE.md, "Guest seats"). The host invites another Copilot
// account to follow a live call; the guest watches it read only in their own overlay and, if
// allowed, sends notes that only the host sees.
//
// The host's side: a Guests button in the overlay's header (who is watching, at a glance) that
// opens the guest panel; the guests' notes in a lane of their own (never mixed into the answers,
// each with Ask AI). Invitations include notes and voice; screen access stays explicit.
// Guests' voice turns on by itself on the call's audio output once a guest may talk.
// The guest's side: the invites (Call screen), a notice that this is someone else's private
// call, the note box when notes are allowed, and Leave.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type Dispatch } from "react";
import { createPortal } from "react-dom";
import { useOverlayPopover } from "./overlayPopover";
import { useHoverMenu } from "./ui/useHoverMenu";
import { GUESTS_CHANGED, guestScope, invitePlannedGuests, inviteProgress, learnGuestNames, recentGuests, rememberGuest, validGuestEmail, type InviteProgress } from "./guestStorage";
import { Icon } from "./ui/Icon";
import "./callDesign.css";
import type { SeatInfo } from "@copilot/protocol";
import { api, inTauri, rest, type MySeat } from "./api";
import { VOICE_OFF, onVoiceStatus, voiceCommand, type VoiceStatus } from "./voice";
import { MODE_LABEL } from "./calls";
import { GuestScreenView, HostScreenSection, STOP_CONTROL_KEY } from "./screenui";
import type { SentNote, SessionView } from "./session";
import { usePolling } from "./usePolling";
import { Banner, Button, Input, Select, Switch, cx } from "./ui";

type Dispatcher = Dispatch<{ type: "note_sent"; id: string; text: string } | { type: "note_asked"; id: string }>;

const OPEN = new Set(["invited", "accepted", "admitted"]);

/** The seats still in play: invited, accepted (waiting to be let in), admitted. */
export function openSeats(seats: SeatInfo[]): SeatInfo[] {
  return seats.filter((x) => OPEN.has(x.status));
}

/** An admitted guest whose app follows the call now. A gateway that does not say counts as
 * connected, as before. */
export function isWatching(x: SeatInfo): boolean {
  return x.status === "admitted" && x.connected !== false;
}

/** "Pavlo, Dana", or "Pavlo · 1 invited": who watches, then who is invited and has not joined. */
export function watchingText(seats: SeatInfo[]): string {
  const watching = seats.filter(isWatching).map((x) => x.name);
  const invited = seats.filter((x) => OPEN.has(x.status) && !isWatching(x)).length;
  const parts = [watching.join(", "), invited ? `${invited} invited` : ""].filter(Boolean);
  return parts.join(" · ");
}

export const NOTE_RESULT: Record<NonNullable<SentNote["result"]>, string> = {
  ok: "Delivered",
  denied: "Notes are off for you",
  expired: "Not delivered: the host was reconnecting",
  rate_limited: "Too fast; wait a moment",
  failed: "Not delivered",
};

const STATUS_TEXT: Record<SeatInfo["status"], string> = {
  invited: "Invited",
  accepted: "Ready to assist",
  admitted: "Not connected",
  declined: "Declined",
  removed: "Removed",
  expired: "Invite expired",
  ended: "Ended",
};

// --- The host ------------------------------------------------------------------------------

/** The overlay header's Guests button: who is watching at a glance (or "Guests"), and the guest
 * panel in a popover. It also keeps guests' voice on while a guest may talk, so it runs whether
 * or not the panel is open. */
export function GuestsButton({ s }: { s: SessionView }) {
  useGuestVoice(s);
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null);
  const hoverOpened = useRef(false);
  const cancelHover = useRef(() => {});
  const close = useCallback(() => { cancelHover.current(); hoverOpened.current = false; setOpen(false); }, []);
  const hover = useHoverMenu({
    onOpen: () => { if (!open) { hoverOpened.current = true; setOpen(true); } },
    onClose: () => { if (hoverOpened.current) close(); },
    keepOpen: () => Boolean(panel.current?.contains(document.activeElement)),
  });
  cancelHover.current = hover.cancel;
  const position = useOverlayPopover(open, close, button, panel);
  if (!s.running || s.seat) return null;
  const count = openSeats(s.seats).length;
  const paused = !s.sharing && count > 0;
  return <span className="ov-menu-wrap">
    <button ref={button} onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} type="button" className={cx("ov-watching", paused && "is-paused")} aria-expanded={open} aria-haspopup="dialog" title={paused ? "Sharing paused · Manage assistants" : "Manage assistants"} onClick={() => { hover.cancel(); hoverOpened.current = false; setOpen(value => !value); }}><Icon name="users" size={15} /> Assistants{count > 0 && <span className="call-count">{count}</span>}</button>
    {open && createPortal(<div ref={panel} onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} className="ov-menu ov-guests call-popover" role="dialog" aria-label="Assistants" style={position}><GuestPanel s={s} /></div>, document.body)}
  </span>;
}
/** The guests' notes, apart from the answers: a small chat that scrolls, newest at the bottom
 * and followed while the host reads there. Ask AI asks a note on the call, as a typed question;
 * nothing else of a note ever reaches the call or a model. */
export function NotesLane({ s, dispatch }: { s: SessionView; dispatch: Dispatcher }) {
  const [error, setError] = useState<string | null>(null);
  const body = useRef<HTMLOListElement>(null);
  const atBottom = useRef(true);
  const count = s.notes.length;
  useLayoutEffect(() => {
    const el = body.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [count]);
  if (count === 0) return null;
  const ask = (id: string, text: string) => {
    setError(null);
    api.askNote(id, text).then(
      () => dispatch({ type: "note_asked", id }),
      (e) => setError(String(e)),
    );
  };
  return (
    <section className="ov-notes" aria-label="Notes from assistants">
      <h2 className="ov-notes-head">Notes from assistants · only you see them</h2>
      <ol
        className="ov-notes-body"
        ref={body}
        onScroll={(e) => {
          const el = e.currentTarget;
          atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
      >
        {s.notes.map((n) => (
          <li key={n.id} className="ov-note">
            <p className="ov-note-from">
              {n.name} <span className="ov-note-time">{noteTime(n.at)}</span>
            </p>
            <p className="ov-note-text">{n.text}</p>
            <button type="button" className="ov-note-ask" disabled={n.asked || !s.running} onClick={() => ask(n.id, n.text)}>
              {n.asked ? "Asked" : "Ask AI"}
            </button>
          </li>
        ))}
      </ol>
      {error && (
        <Banner tone="danger">
          {error}
        </Banner>
      )}
    </section>
  );
}

/** "15:04", in the reader's time. */
function noteTime(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const CONSENT_KEY = "seats.consent-seen";

function consentSeen(): boolean {
  try {
    return window.localStorage.getItem(CONSENT_KEY) === "1";
  } catch {
    return false;
  }
}

/** Optional screen permissions. Notes and voice are included with the invitation. */
export const PERMS: { cap: string; label: string; icon: string; title: string; needs?: "voice" | "screen" }[] = [
  { cap: "device.view", label: "Screen", icon: "🖥", title: "Sees your screen while you share it", needs: "screen" },
  {
    cap: "device.control",
    label: "Control",
    icon: "🖱",
    title: `Uses your mouse and keys while you share a whole screen. Stop it any time, or press ${STOP_CONTROL_KEY}`,
    needs: "screen",
  },
];

type Media = { voice: boolean; screen: boolean };

/** The capabilities among `caps` this server can grant. */
function grantable(caps: string[], media: Media): string[] {
  return ["note", ...(media.voice ? ["voice"] : []), ...caps.filter((c) => {
    const p = PERMS.find((x) => x.cap === c);
    return p !== undefined && (!p.needs || media[p.needs]);
  })];
}

function PermChips({ label, caps, media, disabled, onToggle }: { label: string; caps: string[]; media: Media; disabled?: boolean; onToggle: (cap: string, on: boolean) => void }) {
  if (!media.screen) return null;
  const value = caps.includes("device.control") ? "control" : caps.includes("device.view") ? "view" : "off";
  return <div className="assistant-access" role="group" aria-label={label}><span>Screen access</span>
    <Select aria-label={label} value={value} disabled={disabled} onChange={event => {
      const next = event.target.value;
      if (next === "control") onToggle("device.control", true);
      else if (next === "off") onToggle("device.view", false);
      else if (caps.includes("device.control")) onToggle("device.control", false);
      else onToggle("device.view", true);
    }}><option value="off">No screen access</option><option value="view">View screen</option><option value="control">Remote control</option></Select>
  </div>;
}
const INVITE_CAPS_KEY = "seats.invite-caps";

/** Keep optional screen permissions from the last invite; always include notes and voice. */
export function savedInviteCaps(): string[] {
  try {
    const v: unknown = JSON.parse(window.localStorage.getItem(INVITE_CAPS_KEY) ?? "null");
    if (Array.isArray(v) && v.every((x) => typeof x === "string")) {
      return ["note", "voice", ...v.filter((cap) => PERMS.some((p) => p.cap === cap))];
    }
  } catch {
    // Nothing saved, or unreadable: the first-time choice.
  }
  return ["note", "voice"];
}

function saveInviteCaps(caps: string[]) {
  try {
    window.localStorage.setItem(INVITE_CAPS_KEY, JSON.stringify(caps));
  } catch {
    // Remembered only for convenience.
  }
}

/** Invite with what the guest may do, change it per guest, remove; the screen, guests' voice,
 * and pausing what guests see. In the overlay's Guests popover and on the Call screen. */
export function GuestPanel({ s }: { s: SessionView }) {
  const sid = s.sessionId;
  const [email, setEmail] = useState("");
  const [caps, setCaps] = useState(savedInviteCaps);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [seatErrors, setSeatErrors] = useState<Record<string, string>>({});
  const [media, setMedia] = useState<Media>({ voice: false, screen: false });
  const [mediaReady, setMediaReady] = useState(false);
  const [mediaError, setMediaError] = useState(false);
  const [mediaAttempt, setMediaAttempt] = useState(0);
  const [showConsent] = useState(() => !consentSeen());
  const [scope, setScope] = useState<string | null>(null);
  const [progress, setProgress] = useState<InviteProgress | null>(() => sid ? inviteProgress(sid) : null);
  useEffect(() => {
    api.getSettings().then(value => { if (value?.settings) setScope(guestScope(value.settings, value.signed_in_as)); }, () => undefined);
  }, [sid]);
  useEffect(() => {
    const update = () => setProgress(sid ? inviteProgress(sid) : null);
    update(); window.addEventListener("storage", update); window.addEventListener(GUESTS_CHANGED, update);
    return () => { window.removeEventListener("storage", update); window.removeEventListener(GUESTS_CHANGED, update); };
  }, [sid]);
  // Whether the server can carry voice and the screen at all: no chip that can only fail.
  useEffect(() => {
    if (!sid || !inTauri) return;
    let disposed = false;
    setMediaReady(false);
    setMediaError(false);
    rest.seatsOf(sid).then(
      (r) => {
        if (disposed) return;
        setMedia({ voice: r.voice === true, screen: r.screen === true });
        setMediaReady(true);
        if (scope) learnGuestNames(scope, r.seats);
      },
      () => { if (!disposed) setMediaError(true); },
    );
    return () => { disposed = true; };
  }, [sid, mediaAttempt, scope]);
  const guarded = async (f: () => Promise<unknown>, done?: string) => {
    setError(null);
    setMessage(null);
    setBusy(true);
    try {
      await f();
      if (done) setMessage(done);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const forSeat = async (seatId: string, f: () => Promise<unknown>) => {
    setSeatErrors(({ [seatId]: _gone, ...rest }) => rest);
    setBusy(true);
    try {
      await f();
    } catch (e) {
      setSeatErrors((m) => ({ ...m, [seatId]: String(e) }));
    } finally {
      setBusy(false);
    }
  };
  if (!sid || !s.running || s.seat) return null;
  const invite = () =>
    guarded(async () => {
      await rest.inviteGuest(sid, email.trim(), grantable(caps, media));
      saveInviteCaps(caps);
      if (scope) {
        rememberGuest(scope, { email: email.trim() });
        void rest.seatsOf(sid).then(value => learnGuestNames(scope, value.seats), () => undefined);
      }
      try {
        window.localStorage.setItem(CONSENT_KEY, "1");
      } catch {
        // Shown again next time; harmless.
      }
      setEmail("");
    }, "Invitation sent. They can assist from their Call screen.");
  const seats = s.seats.filter((x) => x.status !== "removed");
  const toggle = (x: SeatInfo, cap: string, on: boolean) =>
    forSeat(x.seat_id, () => rest.setGuestCapabilities(sid, x.seat_id, withCapability(x.capabilities, cap, on)));
  return (
    <div className="seat-panel assistant-panel">
      <header className="assistant-panel-head"><strong>Assistants</strong><span>{openSeats(s.seats).filter(isWatching).length} connected</span></header>
      <HostScreenSection s={s} />
      {progress && progress.pending > 0 && <p className="seat-hint" role="status">Sending invitations…</p>}
      {progress && progress.failures.length > 0 && <Banner tone="danger"><p>{progress.failures.map(f => f.guest.name || f.guest.email).join(", ")}: invitation could not be sent.</p><p>{progress.failures[0].error}</p><Button size="sm" disabled={busy} onClick={() => guarded(() => invitePlannedGuests(sid, progress.scope, progress.failures.map(f => f.guest)))}>Retry invitations</Button></Banner>}
      <section className="seat-section" aria-label="Invite an assistant">
        <form
          className="seat-invite"
          onSubmit={(e) => {
            e.preventDefault();
            if (validGuestEmail(email) && mediaReady && !busy && openSeats(s.seats).length < 2) void invite();
          }}
        >
          <Input type="email" aria-label="Assistant email" placeholder="Invite by email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} />
          <Button size="sm" type="submit" disabled={busy || !mediaReady || !validGuestEmail(email) || openSeats(s.seats).length >= 2}>
            Invite
          </Button>
        </form>
        {mediaError && (
          <Banner tone="danger">
            Assistant options could not be loaded. {" "}
            <Button size="sm" variant="ghost" onClick={() => setMediaAttempt((attempt) => attempt + 1)}>Retry</Button>
          </Banner>
        )}
        <PermChips label="What a new assistant may do" caps={caps} media={media} disabled={busy} onToggle={(cap, on) => setCaps((c) => withCapability(c, cap, on))} />
        {scope && recentGuests(scope).find(p => p.email === email.trim().toLowerCase())?.name && <p className="seat-hint">{recentGuests(scope).find(p => p.email === email.trim().toLowerCase())?.name}</p>}
        <p className="seat-hint">Assistants can send notes and talk to you.</p>
        {openSeats(s.seats).length >= 2 && <p className="seat-hint">Two assistants maximum. Remove an assistant to invite someone else.</p>}
        {showConsent && <p className="seat-hint">Invite people with the call participants' agreement.</p>}
        {message && <p className="seat-hint">{message}</p>}
        {error && (
          <Banner tone="danger">
            {error}
          </Banner>
        )}
      </section>
      {seats.length > 0 && (
        <ul className="seat-list" aria-label="Assistants">
          {seats.map((x) => (
            <li key={x.seat_id} className="seat-card">
              <div className="seat-card-head">
                <span className="assistant-avatar" aria-hidden="true">{x.name.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join("").toUpperCase()}</span><span className="seat-name">{x.name}</span>
                <span className={cx("seat-status", isWatching(x) && "is-watching")}>{isWatching(x) ? "Assisting" : STATUS_TEXT[x.status]}</span>
                <span className="seat-card-actions">
                  {x.status === "accepted" && (
                    <Button size="sm" disabled={busy} onClick={() => forSeat(x.seat_id, () => rest.admitGuest(sid, x.seat_id))}>
                      Let in
                    </Button>
                  )}
                  {OPEN.has(x.status) && (
                    <Button size="sm" variant="ghost" disabled={busy} onClick={() => forSeat(x.seat_id, () => rest.removeGuest(sid, x.seat_id))}>
                      {isWatching(x) ? "Remove" : "Cancel invite"}
                    </Button>
                  )}
                </span>
              </div>
              {x.status === "admitted" && <PermChips label={`What ${x.name} may do`} caps={x.capabilities} media={media} disabled={busy} onToggle={(cap, on) => toggle(x, cap, on)} />}
              {seatErrors[x.seat_id] && (
                <Banner tone="danger" title={"Could not update access for " + x.name}>
                  {seatErrors[x.seat_id]}
                </Banner>
              )}
            </li>
          ))}
        </ul>
      )}
      <HostVoice s={s} />
      {openSeats(s.seats).some((x) => x.status === "admitted") && (
        <section className="seat-section" aria-label="Sharing">
          <Switch
            label="Share the call with assistants"
            hint={s.sharing ? "Assistants see the call as it happens" : "Paused: no new answers or transcript are shared"}
            checked={s.sharing}
            onChange={(on) => guarded(() => api.setSharing(on))}
          />
        </section>
      )}
    </div>
  );
}

// --- The guest -----------------------------------------------------------------------------

/** Always shown while watching: whose call this is, and that it is private. */
export function GuestNotice({ s }: { s: SessionView }) {
  if (!s.seat || !s.running) return null;
  return <div className="ov-guest" role="note">You are assisting {s.seat.hostName} with their call. Only {s.seat.hostName} sees your notes.</div>;
}

export function GuestLeaveButton({ s }: { s: SessionView }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!s.seat || !s.running) return null;
  const leave = async () => {
    setBusy(true); setError(null);
    try { await api.seatLeave(); }
    catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  return <span className="guest-leave"><button type="button" className="ov-ask-btn" disabled={busy} onClick={leave}>{busy ? "Stopping…" : "Stop assisting"}</button>{error && <Banner tone="danger">{error}</Banner>}</span>;
}
/** The guest's notes to the host, when the host allows them. */
export function GuestNoteBox({ s, dispatch }: { s: SessionView; dispatch: Dispatcher }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (!s.seat || !s.running || !s.seat.capabilities.includes("note")) return null;
  const live = s.connection?.state === "live";
  const last = s.sentNotes[s.sentNotes.length - 1];
  const send = () => {
    const value = text.trim();
    if (!value) return;
    setError(null);
    api.seatNote(value).then(
      (id) => {
        dispatch({ type: "note_sent", id, text: value });
        setText("");
      },
      (e) => setError(String(e)),
    );
  };
  return (
    <form
      className="ov-ask"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <input
        className="ov-ask-input"
        aria-label={`Note to ${s.seat.hostName}`}
        value={text}
        maxLength={1000}
        disabled={!live}
        onChange={(e) => setText(e.target.value)}
        placeholder={live ? `A note only ${s.seat.hostName} sees` : "Reconnecting"}
      />
      <button type="submit" className="ov-ask-btn" disabled={!live || !text.trim()}>
        Send note
      </button>
      {last && (
        <span className="ov-note-result" role="status">
          {last.result ? NOTE_RESULT[last.result] : "Sending"}
        </span>
      )}
      {error && (
        <Banner tone="danger">
          {error}
        </Banner>
      )}
    </form>
  );
}

/** The Call screen while watching someone else's call. */
export function Watching({ s, dispatch }: { s: SessionView; dispatch: Dispatcher }) {
  if (!s.seat) return null;
  const seat = s.seat;
  return <div className="assistant-workspace">
    <header className="assistant-workspace-head"><div><div className="assistant-eyebrow"><span className="assistant-live-dot" />Assisting live</div><h1>{seat.hostName}’s call</h1><p>{MODE_LABEL[seat.mode] ?? seat.mode}{seat.title ? ` · ${seat.title}` : ""}</p></div><div className="assistant-workspace-actions"><Button size="sm" onClick={() => api.showOverlay(true)}>Conversation overlay</Button><GuestLeaveButton s={s} /></div></header>
    {!seat.hostConnected && <Banner tone="warning">{seat.hostName} is reconnecting.</Banner>}
    {!seat.sharing && <Banner tone="warning">{seat.hostName} paused the call transcript and answers.</Banner>}
    <GuestScreenView s={s} window="main" />
    <section className="assistant-communication" aria-label={`Private communication with ${seat.hostName}`}><div className="assistant-communication-head"><span><Icon name="shield" size={14} />Only {seat.hostName} hears you and sees your notes</span><GuestTalk s={s} /></div><GuestNoteBox s={s} dispatch={dispatch} /></section>
  </div>;
}

/** Invites to someone else's live call, on the Call screen when no call runs here: Join opens
 * the call at once (the host's invite is the permission; there is nothing to accept). Not now
 * hides it here; the host can remove it. Polled every 5 s; the poll never wakes the server,
 * since an invite only exists during a call. */
export function GuestInvites({ running, disabled, onJoined, open: controlledOpen, onOpenChange, onCountChange, onBusyChange }: {
  running: boolean; disabled?: boolean; onJoined?: () => void; open?: boolean; onOpenChange?: (open: boolean) => void; onCountChange?: (count: number) => void; onBusyChange?: (busy: boolean) => void;
}) {
  const [seats, setSeats] = useState<MySeat[]>([]);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<string | null>(null);
  const [joining, setJoining] = useState<string | null>(null);
  const [ownOpen, setOwnOpen] = useState(false);
  const open = controlledOpen ?? ownOpen;
  const setOpen = useCallback((value: boolean) => { setOwnOpen(value); onOpenChange?.(value); }, [onOpenChange]);
  const close = useCallback(() => setOpen(false), [setOpen]);
  const button = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null);
  const position = useOverlayPopover(open, close, button, panel);
  const load = useCallback(() => {
    if (!inTauri) return;
    return rest.mySeats().then(value => setSeats(value.seats), () => { setSeats([]); return false; });
  }, []);
  useEffect(() => { if (!running) load(); }, [running, load]);
  usePolling(load, 5000, !running, { timeoutMs: Infinity });
  const shown = seats.filter(seat => seat.status === "admitted" && !hidden.has(seat.seat_id) && (!seat.expires_at || new Date(seat.expires_at).getTime() > Date.now()));
  useEffect(() => { onCountChange?.(running ? 0 : shown.length); if (running || !shown.length) setOpen(false); }, [shown.length, running, onCountChange, setOpen]);
  useEffect(() => { onBusyChange?.(joining !== null); }, [joining, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  if (running || shown.length === 0) return null;
  const join = async (seat: MySeat) => {
    if (joining || disabled) return;
    setJoining(seat.seat_id); setError(null);
    try { await api.seatJoin(seat.session_id, seat.seat_id); setOpen(false); onJoined?.(); }
    catch (err) { setError(String(err)); load(); }
    finally { setJoining(null); }
  };
  return <span className="call-join">
    <Button ref={button} size="sm" disabled={disabled || joining !== null} aria-haspopup="dialog" aria-expanded={open} title="Invitations to assist" onClick={() => setOpen(!open)}>Assist a call<span className="call-count">{shown.length}</span><Icon name="chevronDown" size={14} /></Button>
    {open && createPortal(<div ref={panel} className="call-popover call-join-menu" role="dialog" aria-label="Invitations to assist" style={position}>
      {shown.map(seat => <section key={seat.seat_id} className="call-invitation"><strong>{seat.host_name}</strong><p>{seat.title || MODE_LABEL[seat.mode] || seat.mode}</p><div className="ui-row-inline"><Button size="sm" variant="primary" loading={joining === seat.seat_id} disabled={joining !== null} onClick={() => void join(seat)}>Assist</Button><Button size="sm" variant="ghost" disabled={joining !== null} onClick={() => setHidden(old => new Set(old).add(seat.seat_id))}>Not now</Button></div></section>)}
      {error && <Banner tone="danger">{error}</Banner>}
    </div>, document.body)}
  </span>;
}
// --- Voice ---------------------------------------------------------------------------------

/** The seats whose voice may play: admitted, holding `voice` (their LiveKit identities). */
export function voiceIdentities(seats: SeatInfo[]): string[] {
  return seats.filter((x) => x.status === "admitted" && x.capabilities.includes("voice")).map((x) => `seat:${x.seat_id}`);
}

/** A seat's capabilities with one turned on or off. Control goes with the screen: it needs
 * `device.view`, and goes when that does. */
export function withCapability(caps: string[], cap: string, on: boolean): string[] {
  const gone = on ? [cap] : cap === "device.view" ? [cap, "device.control"] : [cap];
  const rest = caps.filter((c) => !gone.includes(c));
  return on ? [...rest, ...(cap === "device.control" && !rest.includes("device.view") ? ["device.view"] : []), cap] : rest;
}

/** The main window's voice (voice.ts), as it reports it. */
export function useVoice(active: boolean): VoiceStatus {
  const [status, setStatus] = useState<VoiceStatus>(VOICE_OFF);
  useEffect(() => {
    if (!active || !inTauri) { setStatus(VOICE_OFF); return; }
    let disposed = false;
    let off: (() => void) | undefined;
    onVoiceStatus((v) => { if (!disposed) setStatus(v); }).then((f) => {
      if (disposed) f();
      else { off = f; void voiceCommand({ cmd: "status" }); }
    });
    return () => {
      disposed = true;
      off?.();
    };
  }, [active]);
  return status;
}

/** Guests' voice on by itself using the call's output, off when none may talk,
 * and who is heard kept in step. Tried once a call: after
 * an error, Try again in the panel. */
export function useGuestVoice(s: SessionView) {
  const hosting = s.running && !s.seat && s.sessionId !== null;
  const status = useVoice(hosting);
  const key = hosting ? voiceIdentities(s.seats).join(",") : "";
  const tried = useRef<string | null>(null);
  useEffect(() => {
    if (!inTauri || !hosting || !s.sessionId) return;
    const ids = key ? key.split(",") : [];
    if (status.host === "on") {
      void voiceCommand(ids.length > 0 ? { cmd: "host_allowed", identities: ids } : { cmd: "host_off" });
      return;
    }
    if (ids.length === 0) {
      tried.current = null;
      return;
    }
    if (status.host !== "off" || tried.current === s.sessionId) return;
    tried.current = s.sessionId;
    void voiceCommand({ cmd: "host_on", sessionId: s.sessionId, allowed: ids });
  }, [hosting, key, status.host, s.sessionId]);
}

/** Only show connection progress or an actionable error. Output follows the call. */
export function HostVoice({ s }: { s: SessionView }) {
  const wanted = voiceIdentities(s.seats);
  const status = useVoice(s.running);
  const shown = wanted.length > 0 || status.host !== "off";
  if (!shown || !s.sessionId) return null;
  if (status.host !== "checking" && !(status.host === "off" && status.error)) return null;
  const sid = s.sessionId;
  return (
    <section className="seat-section seat-voice" aria-label="Assistants' voice">
      {status.host === "checking" && <span className="seat-hint">Connecting assistant audio…</span>}
      {status.host === "off" && status.error && (
        <Banner tone="danger">
          {status.error}{" "}
          <Button size="sm" variant="ghost" onClick={() => void voiceCommand({ cmd: "host_on", sessionId: sid, allowed: wanted })}>
            Try again
          </Button>
        </Banner>
      )}
    </section>
  );
}

/** The overlay header's mute for guests' voice, while it is on. */
export function VoiceMute({ s }: { s: SessionView }) {
  const status = useVoice(s.running && !s.seat);
  if (status.host !== "on") return null;
  const muted = status.hostMuted;
  return <div className="assistant-audio-toggle"><Switch label="Hear assistants" hint={muted ? "Muted on this device" : "Plays through your call audio output"} checked={!muted} onChange={on => void voiceCommand({ cmd: "host_mute", on: !on })} /></div>;
}

/** The guest's Talk: held (or kept on), their microphone goes to the host alone. */
export function GuestTalk({ s }: { s: SessionView }) {
  const seatId = s.seat && s.running && s.seat.capabilities.includes("voice") ? s.seat.seatId : null;
  const status = useVoice(seatId !== null);
  const [keep, setKeep] = useState(false);
  useEffect(() => {
    if (!inTauri) return;
    if (seatId) void voiceCommand({ cmd: "guest_on", seatId });
    else void voiceCommand({ cmd: "guest_off" });
  }, [seatId]);
  if (!seatId || !s.seat) return null;
  const talk = (on: boolean) => void voiceCommand({ cmd: "guest_talk", on });
  const host = s.seat.hostName;
  const ready = status.guest === "on";
  return (
    <div className="ov-talk">
      <button
        type="button"
        className={cx("ov-talk-btn", status.talking && "is-talking")}
        disabled={!ready || keep}
        onPointerDown={() => talk(true)}
        onPointerUp={() => talk(false)}
        onPointerLeave={() => !keep && status.talking && talk(false)}
      >
        {status.talking ? `Talking to ${host}…` : `Hold to talk to ${host}`}
      </button>
      <label className="ov-talk-keep">
        <input
          type="checkbox"
          checked={keep}
          disabled={!ready}
          onChange={(e) => {
            setKeep(e.target.checked);
            talk(e.target.checked);
          }}
        />
        Keep talking
      </label>
      {status.guest === "connecting" && <span className="ov-note-result">Connecting voice…</span>}
      {status.error && (
        <Banner tone="danger">
          {status.error}
        </Banner>
      )}
    </div>
  );
}
