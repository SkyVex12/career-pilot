// Guest seats in the UI (ARCHITECTURE.md, "Guest seats"). The host invites another Copilot
// account to follow a live call; the guest watches it read only in their own overlay and, if
// allowed, sends notes that only the host sees.
//
// The host's side: a Guests button in the overlay's header (who is watching, at a glance) that
// opens the guest panel; the guests' notes in a lane of their own (never mixed into the answers,
// each with Ask AI). The panel invites with what the guest may do already chosen (chips, as
// last time), changes it on each guest's card, shares the screen, picks where guests' voice
// plays, and pauses what guests see. Guests' voice turns on by itself once a guest may talk.
// The guest's side: the invites (Call screen), a notice that this is someone else's private
// call, the note box when notes are allowed, and Leave.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type Dispatch } from "react";
import type { SeatInfo } from "@copilot/protocol";
import { api, inTauri, rest, type MySeat, type VoiceDevice } from "./api";
import { VOICE_OFF, onVoiceStatus, voiceCommand, windowsName, type VoiceOutput, type VoiceStatus } from "./voice";
import { MODE_LABEL } from "./calls";
import { GuestScreenView, HostScreenSection, STOP_CONTROL_KEY } from "./screenui";
import type { SentNote, SessionView } from "./session";
import { usePolling } from "./usePolling";
import { Badge, Banner, Button, Input, Switch, cx } from "./ui";

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
  accepted: "Wants to watch",
  admitted: "Invited, not joined yet",
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
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!panel.current?.contains(e.target as Node) && !button.current?.contains(e.target as Node)) setOpen(false);
    };
    // Esc closes it wherever focus is (a button that just went busy drops focus to the page).
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
      button.current?.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (!s.running || s.seat) return null;
  const text = watchingText(openSeats(s.seats));
  const paused = !s.sharing && text !== "";
  return (
    <span className="ov-menu-wrap">
      <button
        ref={button}
        type="button"
        className={cx("ov-watching", paused && "is-paused")}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={paused ? "Sharing paused: guests see nothing. Click for guests" : text ? "Guests watching this call: click to manage" : "Invite a guest to this call"}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden="true">👥 </span>
        {text ? (paused ? `${text} · paused` : text) : "Guests"}
      </button>
      {open && (
        <div ref={panel} className="ov-menu ov-guests" role="dialog" aria-label="Guests">
          <GuestPanel s={s} />
        </div>
      )}
    </span>
  );
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
    <section className="ov-notes" aria-label="Notes from guests">
      <h2 className="ov-notes-head">Notes from guests · only you see them</h2>
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
        <p className="ov-ask-error" role="alert">
          {error}
        </p>
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

/** What a guest may do: a chip each, on the invite and on each guest's card. Voice and the
 * screen show only where the server can carry them. Control goes with the screen. */
export const PERMS: { cap: string; label: string; icon: string; title: string; needs?: "voice" | "screen" }[] = [
  { cap: "note", label: "Notes", icon: "✎", title: "May send you notes only you see" },
  { cap: "voice", label: "Voice", icon: "🎙", title: "May talk to you. You hear them on your headphones, never on the call", needs: "voice" },
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
  return caps.filter((c) => {
    const p = PERMS.find((x) => x.cap === c);
    return p !== undefined && (!p.needs || media[p.needs]);
  });
}

function PermChips({ label, caps, media, disabled, onToggle }: { label: string; caps: string[]; media: Media; disabled?: boolean; onToggle: (cap: string, on: boolean) => void }) {
  return (
    <div className="seat-chips" role="group" aria-label={label}>
      {PERMS.filter((p) => !p.needs || media[p.needs]).map((p) => {
        const on = caps.includes(p.cap);
        return (
          <button key={p.cap} type="button" className="seat-chip" aria-pressed={on} title={p.title} disabled={disabled} onClick={() => onToggle(p.cap, !on)}>
            <span aria-hidden="true">{p.icon}</span> {p.label}
          </button>
        );
      })}
    </div>
  );
}

const INVITE_CAPS_KEY = "seats.invite-caps";

/** What the next guest may do: as the host chose for the last one; notes only at first. */
export function savedInviteCaps(): string[] {
  try {
    const v: unknown = JSON.parse(window.localStorage.getItem(INVITE_CAPS_KEY) ?? "null");
    if (Array.isArray(v) && v.every((x) => typeof x === "string")) return v;
  } catch {
    // Nothing saved, or unreadable: the first-time choice.
  }
  return ["note"];
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
  const [showConsent] = useState(() => !consentSeen());
  // Whether the server can carry voice and the screen at all: no chip that can only fail.
  useEffect(() => {
    if (!sid || !inTauri) return;
    rest.seatsOf(sid).then(
      (r) => setMedia({ voice: r.voice === true, screen: r.screen === true }),
      () => setMedia({ voice: false, screen: false }),
    );
  }, [sid]);
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
      try {
        window.localStorage.setItem(CONSENT_KEY, "1");
      } catch {
        // Shown again next time; harmless.
      }
      setEmail("");
    }, `Invited. If ${email.trim()} has a Copilot account, they can join from their Call screen now.`);
  const seats = s.seats.filter((x) => x.status !== "removed");
  const toggle = (x: SeatInfo, cap: string, on: boolean) =>
    forSeat(x.seat_id, () => rest.setGuestCapabilities(sid, x.seat_id, withCapability(x.capabilities, cap, on)));
  return (
    <div className="seat-panel">
      <section className="seat-section" aria-label="Invite a guest">
        <form
          className="seat-invite"
          onSubmit={(e) => {
            e.preventDefault();
            if (email.trim()) void invite();
          }}
        >
          <Input type="email" aria-label="Guest's Copilot email" placeholder="Invite by Copilot email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} />
          <Button size="sm" type="submit" disabled={busy || !email.trim()}>
            Invite
          </Button>
        </form>
        <PermChips label="What a new guest may do" caps={caps} media={media} disabled={busy} onToggle={(cap, on) => setCaps((c) => withCapability(c, cap, on))} />
        {showConsent && <p className="seat-hint">Guests see this call live, including the other side's words. Invite only people the call's participants would be fine with.</p>}
        {message && <p className="seat-hint">{message}</p>}
        {error && (
          <p className="seat-error" role="alert">
            {error}
          </p>
        )}
      </section>
      {seats.length > 0 && (
        <ul className="seat-list" aria-label="Guests">
          {seats.map((x) => (
            <li key={x.seat_id} className="seat-card">
              <div className="seat-card-head">
                <span className="seat-name">{x.name}</span>
                <span className={cx("seat-status", isWatching(x) && "is-watching")}>{isWatching(x) ? "Watching" : STATUS_TEXT[x.status]}</span>
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
                <p className="seat-error" role="alert">
                  {seatErrors[x.seat_id]}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      <HostScreenSection s={s} />
      <HostVoice s={s} />
      {openSeats(s.seats).some((x) => x.status === "admitted") && (
        <section className="seat-section" aria-label="Sharing">
          <Switch
            label="Share the call with guests"
            hint={s.sharing ? "Guests see the call as it happens" : "Paused: guests see nothing of the call, now or later"}
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
  const host = s.seat.hostName;
  return (
    <div className="ov-guest" role="note">
      <span>
        You are viewing {host}'s private call. Only {host} sees your notes. Do not record or share it.
      </span>
      <button type="button" className="ov-ask-btn" onClick={() => inTauri && void api.seatLeave().catch(() => undefined)}>
        Leave
      </button>
    </div>
  );
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
        <span className="ov-ask-error" role="alert">
          {error}
        </span>
      )}
    </form>
  );
}

/** The Call screen while watching someone else's call. */
export function Watching({ s, dispatch }: { s: SessionView; dispatch: Dispatcher }) {
  if (!s.seat) return null;
  const seat = s.seat;
  return (
    <div className="ui-stack">
      <div className="ui-row-inline">
        <Badge tone="info" icon="👁">
          Watching
        </Badge>
        <strong>
          {seat.hostName}'s call · {MODE_LABEL[seat.mode] ?? seat.mode}
          {seat.title ? ` · ${seat.title}` : ""}
        </strong>
      </div>
      <Banner tone="info" title="Someone else's private call">
        You see {seat.hostName}'s overlay as it happens. Nothing you do reaches the call; only {seat.hostName} sees your notes. Do not record or share it.
      </Banner>
      {!seat.hostConnected && <Banner tone="warning">{seat.hostName}'s app is reconnecting. The call goes on.</Banner>}
      {!seat.sharing && <Banner tone="warning">{seat.hostName} paused sharing.</Banner>}
      <div className="ui-row-inline">
        <Button onClick={() => api.showOverlay(true)}>Show overlay</Button>
        <Button variant="ghost" onClick={() => api.seatLeave()}>
          Leave
        </Button>
      </div>
      <GuestScreenView s={s} />
      <GuestTalk s={s} />
      <GuestNoteBox s={s} dispatch={dispatch} />
    </div>
  );
}

/** Invites to someone else's live call, on the Call screen when no call runs here: Join opens
 * the call at once (the host's invite is the permission; there is nothing to accept). Not now
 * hides it here; the host can remove it. Polled every 5 s; the poll never wakes the server,
 * since an invite only exists during a call. */
export function GuestInvites({ running, onJoined }: { running: boolean; onJoined?: () => void }) {
  const [seats, setSeats] = useState<MySeat[]>([]);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    if (!inTauri) return;
    rest.mySeats().then(
      (r) => setSeats(r.seats),
      () => setSeats([]),
    );
  }, []);
  useEffect(() => {
    if (!running) load();
  }, [running, load]);
  usePolling(load, 5000, !running);
  const shown = seats.filter((x) => x.status === "admitted" && !hidden.has(x.seat_id));
  if (running || shown.length === 0) return null;
  const join = (x: MySeat) => {
    setError(null);
    api.seatJoin(x.session_id, x.seat_id).then(
      () => onJoined?.(),
      (e) => setError(String(e)),
    );
  };
  return (
    <section className="seat-invites" aria-label="Invites to watch a call">
      {shown.map((x) => (
        <Banner
          key={x.seat_id}
          tone="info"
          title={`${x.host_name} invited you to watch a call`}
          action={
            <span className="ui-row-inline">
              <Button size="sm" onClick={() => join(x)}>
                Join
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setHidden((h) => new Set(h).add(x.seat_id))}>
                Not now
              </Button>
            </span>
          }
        >
          {MODE_LABEL[x.mode] ?? x.mode}
          {x.title ? ` · ${x.title}` : ""}. You see the call read only; only {x.host_name} sees your notes.
        </Banner>
      ))}
      {error && <Banner tone="danger">{error}</Banner>}
    </section>
  );
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
    if (!active || !inTauri) return;
    let disposed = false;
    let off: (() => void) | undefined;
    onVoiceStatus((v) => setStatus(v)).then((f) => {
      if (disposed) f();
      else off = f;
    });
    void voiceCommand({ cmd: "status" });
    return () => {
      disposed = true;
      off?.();
    };
  }, [active]);
  return status;
}

const VOICE_DEVICE_KEY = "seats.voice-device";

function savedDevice(): string {
  try {
    return window.localStorage.getItem(VOICE_DEVICE_KEY) ?? "";
  } catch {
    return "";
  }
}

/** What to warn about a device for guests' voice, or null. Only a warning: the host may pick
 * it anyway, and the checks on Turn on decide. */
export function deviceProblem(d: VoiceDevice): string | null {
  if (!d.private) return "not headphones";
  return null;
}

/** The window's output device, with what Windows says about it (matched by name). */
function annotate(o: VoiceOutput, known: VoiceDevice[]): string | null {
  const name = windowsName(o.label).toLowerCase();
  const d = known.find((k) => k.name.toLowerCase() === name);
  return d ? deviceProblem(d) : "not known to Windows";
}

function saveDevice(id: string) {
  try {
    window.localStorage.setItem(VOICE_DEVICE_KEY, id);
  } catch {
    // Remembered only for convenience.
  }
}

/** Guests' voice on by itself once a guest may talk (on the headphones used last, or Windows'
 * default output), off when none may, and who is heard kept in step. Tried once a call: after
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
    void voiceCommand({ cmd: "host_on", deviceId: savedDevice(), sessionId: s.sessionId, allowed: ids });
  }, [hosting, key, status.host, s.sessionId]);
}

/** Where guests' voice plays (switchable while it plays), and why it stopped. It turns on by
 * itself (useGuestVoice); the header's button mutes it. */
export function HostVoice({ s }: { s: SessionView }) {
  const wanted = voiceIdentities(s.seats);
  const status = useVoice(s.running);
  const [known, setKnown] = useState<VoiceDevice[]>([]);
  const [chosen, setChosen] = useState(savedDevice);
  const shown = wanted.length > 0 || status.host !== "off";
  useEffect(() => {
    if (!shown || !inTauri) return;
    void voiceCommand({ cmd: "outputs" });
    api.voiceDevices().then(setKnown, () => setKnown([]));
  }, [shown]);
  if (!shown || !s.sessionId) return null;
  const sid = s.sessionId;
  const on = (deviceId: string) => void voiceCommand({ cmd: "host_on", deviceId, sessionId: sid, allowed: wanted });
  const choose = (deviceId: string) => {
    setChosen(deviceId);
    saveDevice(deviceId);
    if (status.host === "on") void voiceCommand({ cmd: "host_device", deviceId });
    else if (status.host === "off") on(deviceId);
  };
  const value = status.device ?? chosen;
  return (
    <section className="seat-section seat-voice" aria-label="Guests' voice">
      <label className="seat-voice-row">
        <span className="seat-hint">Guests' voice on</span>
        <select className="ui-input" aria-label="Headphones for guests' voice" value={value} onChange={(e) => choose(e.target.value)} disabled={status.host === "checking"}>
          <option value="">Windows default output</option>
          {status.outputs.map((o) => {
            const problem = annotate(o, known);
            return (
              <option key={o.deviceId} value={o.deviceId}>
                {o.label}
                {problem ? ` ⚠ ${problem}` : ""}
              </option>
            );
          })}
        </select>
      </label>
      {status.host === "checking" && <span className="seat-hint">Turning on…</span>}
      {status.host === "off" && status.error && (
        <p className="seat-error" role="alert">
          {status.error}{" "}
          <Button size="sm" variant="ghost" onClick={() => on(value)}>
            Try again
          </Button>
        </p>
      )}
    </section>
  );
}

/** The overlay header's mute for guests' voice, while it is on. */
export function VoiceMute({ s }: { s: SessionView }) {
  const status = useVoice(s.running && !s.seat);
  if (status.host !== "on") return null;
  const muted = status.hostMuted;
  return (
    <button
      type="button"
      className={cx("ov-voice-mute", muted && "is-muted")}
      aria-pressed={muted}
      title={muted ? "Guests are muted: click to hear them" : "Mute guests' voice"}
      onClick={() => void voiceCommand({ cmd: "host_mute", on: !muted })}
    >
      {muted ? "🔇 Guests" : "🔊 Guests"}
    </button>
  );
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
        <span className="ov-ask-error" role="alert">
          {status.error}
        </span>
      )}
    </div>
  );
}
