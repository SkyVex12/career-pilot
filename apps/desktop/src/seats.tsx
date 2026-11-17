// Guest seats in the UI (ARCHITECTURE.md, "Guest seats"). The host invites another Copilot
// account to follow a live call; the guest watches it read only in their own overlay and, if
// allowed, sends notes that only the host sees.
//
// The host's side: who is watching (a chip in the overlay's header), the guests' notes in a
// lane of their own (never mixed into the answers, each with Ask AI), and a panel to invite,
// admit, allow notes, remove, and pause what guests see.
// The guest's side: the invites (Call screen), a notice that this is someone else's private
// call, the note box when notes are allowed, and Leave.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type Dispatch } from "react";
import type { SeatInfo } from "@copilot/protocol";
import { api, inTauri, rest, type MySeat } from "./api";
import { MODE_LABEL } from "./calls";
import type { SentNote, SessionView } from "./session";
import { usePolling } from "./usePolling";
import { Badge, Banner, Button, Checkbox, Input, Switch, cx } from "./ui";

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

/** The overlay header's chip: who is watching, and whether sharing is paused. */
export function WatchingChip({ s }: { s: SessionView }) {
  const seats = openSeats(s.seats);
  const text = watchingText(seats);
  if (!text) return null;
  return (
    <span className={cx("ov-watching", !s.sharing && "is-paused")} role="status" title={s.sharing ? "Guests watching this call" : "Sharing paused: guests see nothing"} data-tauri-drag-region>
      <span aria-hidden="true">👁 </span>
      {s.sharing ? text : `${text} · paused`}
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

/** Invite, admit, allow notes, remove; and pause what guests see. In the overlay's gear and on
 * the Call screen while a call runs. */
export function GuestPanel({ s }: { s: SessionView }) {
  const sid = s.sessionId;
  const [email, setEmail] = useState("");
  const [notes, setNotes] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showConsent] = useState(() => !consentSeen());
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
  if (!sid || !s.running || s.seat) return null;
  const invite = () =>
    guarded(async () => {
      await rest.inviteGuest(sid, email.trim(), notes);
      try {
        window.localStorage.setItem(CONSENT_KEY, "1");
      } catch {
        // Shown again next time; harmless.
      }
      setEmail("");
    }, `If ${email.trim()} has a Copilot account, they can join from their Call screen now.`);
  const seats = s.seats.filter((x) => x.status !== "removed");
  return (
    <div className="seat-panel">
      {showConsent && <p className="seat-hint">Guests see this call live, including the other side's words. Invite only people the call's participants would be fine with.</p>}
      <form
        className="seat-invite"
        onSubmit={(e) => {
          e.preventDefault();
          if (email.trim()) void invite();
        }}
      >
        <Input type="email" aria-label="Guest's Copilot email" placeholder="Guest's Copilot email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} />
        <Button size="sm" type="submit" disabled={busy || !email.trim()}>
          Invite
        </Button>
      </form>
      <Checkbox checked={notes} onChange={setNotes} label="Allow notes to me" />
      {message && <p className="seat-hint">{message}</p>}
      {error && (
        <p className="seat-error" role="alert">
          {error}
        </p>
      )}
      {seats.length > 0 && (
        <ul className="seat-list" aria-label="Guests">
          {seats.map((x) => (
            <li key={x.seat_id} className="seat-row">
              <span className="seat-name">{x.name}</span>
              <span className="seat-status">{isWatching(x) ? "Watching" : STATUS_TEXT[x.status]}</span>
              <span className="seat-actions">
                {x.status === "accepted" && (
                  <Button size="sm" disabled={busy} onClick={() => guarded(() => rest.admitGuest(sid, x.seat_id))}>
                    Let in
                  </Button>
                )}
                {x.status === "admitted" && (
                  <Switch
                    label="Notes"
                    checked={x.capabilities.includes("note")}
                    disabled={busy}
                    onChange={(on) => guarded(() => rest.setGuestNotes(sid, x.seat_id, on))}
                  />
                )}
                {OPEN.has(x.status) && (
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => guarded(() => rest.removeGuest(sid, x.seat_id))}>
                    {isWatching(x) ? "Remove" : "Cancel invite"}
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {openSeats(s.seats).some((x) => x.status === "admitted") && (
        <Switch
          label="Share the call with guests"
          hint={s.sharing ? "Guests see the call as it happens" : "Paused: guests see nothing of the call, now or later"}
          checked={s.sharing}
          onChange={(on) => guarded(() => api.setSharing(on))}
        />
      )}
    </div>
  );
}

// --- The guest -----------------------------------------------------------------------------

/** Always shown while watching: whose call this is, and that it is private. */
export function GuestNotice({ s }: { s: SessionView }) {
  if (!s.seat) return null;
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
  if (!s.seat || !s.seat.capabilities.includes("note")) return null;
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
