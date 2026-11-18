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
import { api, inTauri, rest, type MySeat, type VoiceDevice } from "./api";
import { VOICE_OFF, onVoiceStatus, voiceCommand, type VoiceOutput, type VoiceStatus } from "./voice";
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
  const [seatErrors, setSeatErrors] = useState<Record<string, string>>({});
  const [voiceAvailable, setVoiceAvailable] = useState(false);
  const [showConsent] = useState(() => !consentSeen());
  // Whether the server can carry voice at all: no switch that can only fail.
  useEffect(() => {
    if (!sid || !inTauri) return;
    rest.seatsOf(sid).then(
      (r) => setVoiceAvailable(r.voice === true),
      () => setVoiceAvailable(false),
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
      await rest.inviteGuest(sid, email.trim(), notes);
      try {
        window.localStorage.setItem(CONSENT_KEY, "1");
      } catch {
        // Shown again next time; harmless.
      }
      setEmail("");
    }, `If ${email.trim()} has a Copilot account, they can join from their Call screen now.`);
  const seats = s.seats.filter((x) => x.status !== "removed");
  const toggle = (x: SeatInfo, cap: string, on: boolean) =>
    forSeat(x.seat_id, () => rest.setGuestCapabilities(sid, x.seat_id, withCapability(x.capabilities, cap, on)));
  return (
    <div className="seat-panel">
      <section className="seat-section" aria-label="Invite a guest">
        <h3 className="seat-section-title">Invite a guest</h3>
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
      </section>
      {seats.length > 0 && (
        <section className="seat-section" aria-label="Guests">
          <h3 className="seat-section-title">Guests</h3>
          <ul className="seat-list">
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
                {x.status === "admitted" && (
                  <div className="seat-card-perms" role="group" aria-label={`What ${x.name} may do`}>
                    <Switch label="Notes" checked={x.capabilities.includes("note")} disabled={busy} onChange={(on) => toggle(x, "note", on)} />
                    {voiceAvailable && <Switch label="Voice" checked={x.capabilities.includes("voice")} disabled={busy} onChange={(on) => toggle(x, "voice", on)} />}
                  </div>
                )}
                {seatErrors[x.seat_id] && (
                  <p className="seat-error" role="alert">
                    {seatErrors[x.seat_id]}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
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
      <HostVoice s={s} />
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

function withCapability(caps: string[], cap: string, on: boolean): string[] {
  const rest = caps.filter((c) => c !== cap);
  return on ? [...rest, cap] : rest;
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

/** Why a device cannot carry guests' voice, or null when it may (the checks still run). */
export function deviceProblem(d: VoiceDevice): string | null {
  if (d.call) return "the call plays here";
  if (!d.private) return "not headphones";
  return null;
}

/** The window's output device, with what Windows says about it (matched by name). */
function annotate(o: VoiceOutput, known: VoiceDevice[]): string | null {
  const d = known.find((k) => k.name.toLowerCase() === o.label.trim().toLowerCase());
  return d ? deviceProblem(d) : "not known to Windows";
}

/** The host's side of voice: headphones for it, on and off, mute. */
export function HostVoice({ s }: { s: SessionView }) {
  const wanted = voiceIdentities(s.seats);
  const status = useVoice(s.running);
  const [known, setKnown] = useState<VoiceDevice[]>([]);
  const [device, setDevice] = useState(savedDevice);
  const allowed = wanted.join(",");
  const on = status.host === "on";
  useEffect(() => {
    if (on && inTauri) void voiceCommand({ cmd: "host_allowed", identities: allowed ? allowed.split(",") : [] });
  }, [allowed, on]);
  const listed = wanted.length > 0 && status.host === "off";
  useEffect(() => {
    if (!listed || !inTauri) return;
    void voiceCommand({ cmd: "outputs" });
    api.voiceDevices().then(setKnown, () => setKnown([]));
  }, [listed]);
  if (wanted.length === 0 && status.host === "off") return null;
  const start = () => {
    if (!s.sessionId) return;
    try {
      window.localStorage.setItem(VOICE_DEVICE_KEY, device);
    } catch {
      // Remembered only for convenience.
    }
    void voiceCommand({ cmd: "host_on", deviceId: device, sessionId: s.sessionId, allowed: wanted });
  };
  const checking = status.host === "checking";
  return (
    <section className="seat-section seat-voice" aria-label="Guests' voice">
      <h3 className="seat-section-title">Guests' voice</h3>
      {on ? (
        <>
          <span className="seat-hint">On your headphones only; never on the call.</span>
          <Switch label="Mute guests" checked={status.hostMuted} onChange={(m) => void voiceCommand({ cmd: "host_mute", on: m })} />
          <Button size="sm" variant="ghost" onClick={() => void voiceCommand({ cmd: "host_off" })}>
            Turn off
          </Button>
        </>
      ) : (
        <>
          <div className="seat-invite">
            <select className="ui-input" aria-label="Headphones for guests' voice" value={device} onChange={(e) => setDevice(e.target.value)} disabled={checking}>
              <option value="">Choose headphones…</option>
              {status.outputs.map((o) => {
                const problem = annotate(o, known);
                return (
                  <option key={o.deviceId} value={o.deviceId} disabled={problem !== null}>
                    {o.label}
                    {problem ? ` (${problem})` : ""}
                  </option>
                );
              })}
            </select>
            <Button size="sm" disabled={checking || !device} onClick={start}>
              {checking ? "Checking…" : "Turn on"}
            </Button>
          </div>
          <span className="seat-hint">A short tone plays on them to check that nothing reaches the call or your microphone.</span>
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
