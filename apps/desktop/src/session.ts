// The session as the UI sees it, folded from the core's event stream. Both windows run the
// same reducer; a window opened or reloaded mid-call replays the snapshot first, so the whole
// conversation comes back (the core keeps the session's events, compacted; see
// src-tauri/src/history.rs).
import { useEffect, useReducer } from "react";
import type { Capability, MemoryRef, Mode, QuestionType, ScreenReading, SeatInfo, ServerMessage } from "@copilot/protocol";
import { api, inTauri, onEvent, type ConnectionState, type Numbered, type QuestionLatency, type Replay, type ReplayPass, type StopReason, type UiEvent } from "./api";

export interface PassView {
  text: string;
  done: boolean;
  error: string | null; // answer.error code; "cancelled" keeps what arrived
  source: string | null;
  refs: MemoryRef[];
  /** Chunk positions are per question/pass; old gateways omit them. */
  stream?: { next: number; pending: Record<number, { text: string; end: number }>; expected?: number };
}

export interface QuestionView {
  id: string;
  text: string;
  type: QuestionType;
  confidence: number;
  speculative: boolean;
  cancelled: boolean;
  fast: PassView;
  deep: PassView;
  deepRequested: boolean;
  answerStrategy?: "legacy_dual" | "adaptive" | "adaptive_hybrid" | "adaptive_on_demand";
  /** A screenshot's question (coding mode), once the gateway has read the screen: its tasks. */
  screen?: ScreenReading;
}

/** A screenshot's question (coding mode): its answers are code and Markdown, not talking points.
 * Its text names the screen ("Screen: 2 tasks (choice, code)") before and after the reading. */
export function isScreenQuestion(q: QuestionView): boolean {
  return q.screen !== undefined || /^Screen\b/.test(q.text);
}

export interface TranscriptLine {
  key: string;
  channel: string;
  text: string;
  final: boolean;
  /** From the call's start, once the final says (a partial has no times yet). */
  startMs?: number;
  endMs?: number;
}

export type BadgeState = "ok" | "degraded" | "reconnecting";

/** A guest's note, as the host sees it (seat.note). `asked`: the host pressed Ask AI. */
export interface NoteView {
  id: string;
  seatId: string;
  name: string;
  text: string;
  at: string;
  asked: boolean;
}

/** The call this app watches as a guest (seat.ready, seat.host, seat.update). */
export interface GuestSeat {
  sessionId: string;
  seatId: string;
  hostName: string;
  mode: Mode;
  title: string | null;
  capabilities: Capability[];
  hostConnected: boolean;
  sharing: boolean;
}

/** A note this guest sent, and what became of it (seat.command.result). */
export interface SentNote {
  id: string;
  text: string;
  result: "ok" | "denied" | "expired" | "rate_limited" | "failed" | null;
}

/// One entry of the conversation, in the order it first appeared: a transcript line (either
/// side) or a question with its answers.
export type TimelineRef = { kind: "line"; key: string } | { kind: "question"; id: string };

export interface SessionView {
  running: boolean;
  /** The session last live in this run; kept after it stops, for the post-call screen. */
  sessionId: string | null;
  connection: ConnectionState | null;
  stopped: StopReason | null;
  /** Reject queued server events until the native transport announces a live connection. */
  socketClosed: boolean;
  status: Record<string, { state: BadgeState; message: string }>;
  /** Every question of the session; none is ever removed. */
  questions: QuestionView[];
  /** Cancellation can arrive before detection; keep its tombstone for this session. */
  cancelledIds: Record<string, true>;
  /** Every transcript line of the session, both sides; none is removed (an echo line is). */
  transcript: TranscriptLine[];
  timeline: TimelineRef[];
  /** Question types whose deep answer starts on its own (session.ready), if the gateway said. */
  deepAuto: QuestionType[] | null;
  /** Only server acknowledgements change the live language; a draft is never authoritative. */
  spokenLanguage: string | null;
  languageSwitching: boolean;
  languageResult: { requestId: string | null; error: string | null; seq: number } | null;
  latency: Record<string, QuestionLatency>;
  log: string[];
  speaking: { them: boolean; me: boolean };
  /** Guest seats, the host's side: the call's seats, the guests' notes, and whether guests see
   * the call (ARCHITECTURE.md, "Guest seats"). */
  seats: SeatInfo[];
  notes: NoteView[];
  sharing: boolean;
  /** Guest seats, the guest's side: the call this app watches, or null in a call of its own. */
  seat: GuestSeat | null;
  sentNotes: SentNote[];
}

/// Before session.ready says otherwise (an older gateway): the three types it auto-starts.
export const AUTO_DEEP: QuestionType[] = ["behavioral", "technical", "system_design"];

const emptyPass = (): PassView => ({ text: "", done: false, error: null, source: null, refs: [] });

export const initial: SessionView = {
  running: false,
  sessionId: null,
  connection: null,
  stopped: null,
  socketClosed: false,
  status: {},
  questions: [],
  cancelledIds: {},
  transcript: [],
  timeline: [],
  deepAuto: null,
  spokenLanguage: null,
  languageSwitching: false,
  languageResult: null,
  latency: {},
  log: [],
  speaking: { them: false, me: false },
  seats: [],
  notes: [],
  sharing: true,
  seat: null,
  sentNotes: [],
};

type Action =
  | { type: "event"; event: UiEvent }
  | { type: "reset"; running: boolean }
  | { type: "deep_requested"; id: string }
  | { type: "note_sent"; id: string; text: string }
  | { type: "note_asked"; id: string };

export function deepStartsAlone(s: SessionView, type: QuestionType): boolean {
  return (s.deepAuto ?? AUTO_DEEP).includes(type);
}

function updateQuestion(s: SessionView, id: string, f: (q: QuestionView) => QuestionView): SessionView {
  let changed = false;
  const questions = s.questions.map(q => {
    if (q.id !== id) return q;
    const next = f(q);
    changed ||= next !== q;
    return next;
  });
  return changed ? { ...s, questions } : s;
}

function appendDelta(p: PassView, text: string, sequence?: number | null, sequenceEnd?: number | null): PassView {
  const repairing = p.error === "incomplete_stream" && p.stream?.expected !== undefined;
  if (p.done && !repairing) return p;
  if (sequence == null) return p.done ? p : { ...p, text: p.text + text };
  const stream = p.stream ?? { next: 0, pending: {} };
  const end = sequenceEnd ?? sequence + 1;
  if (!Number.isSafeInteger(sequence) || !Number.isSafeInteger(end) || sequence < 0 || end <= sequence) return p;
  if (sequence < stream.next || stream.pending[sequence] || (stream.expected !== undefined && end > stream.expected)) return p;
  // A malformed/lost stream must not consume unbounded memory or spin forever.
  if (sequence - stream.next > 128 || Object.keys(stream.pending).length >= 128) return { ...p, done: true, error: "stream_order", stream: undefined };
  const pending = { ...stream.pending, [sequence]: { text, end } };
  let next = stream.next;
  let content = p.text;
  while (pending[next]) {
    const chunk = pending[next];
    delete pending[next];
    content += chunk.text;
    next = chunk.end;
  }
  for (const key of Object.keys(pending)) if (Number(key) < next) delete pending[Number(key)];
  const incomplete = stream.expected !== undefined && next < stream.expected;
  return { ...p, text: content, stream: { ...stream, next, pending }, ...(repairing ? { error: incomplete ? "incomplete_stream" : null } : {}) };
}

function automaticExpert(s: SessionView, type: QuestionType, strategy: QuestionView["answerStrategy"]): boolean {
  // Candidate routing uses more than question type. Only legacy predicts a queued pass;
  // candidate experts become available when their own stream arrives or the user asks.
  return (!strategy || strategy === "legacy_dual") && deepStartsAlone(s, type);
}

function log(s: SessionView, line: string): SessionView {
  const stamp = new Date().toLocaleTimeString();
  return { ...s, log: [...s.log.slice(-199), `${stamp} ${line}`] };
}

function stopAnswers(s: SessionView, reason: StopReason): SessionView {
  const finish = (p: PassView): PassView => p.done
    ? { ...p, stream: undefined }
    : { ...p, done: true, error: "cancelled", stream: undefined };
  return {
    ...s, running: false, stopped: reason, socketClosed: true, speaking: { them: false, me: false },
    questions: s.questions.map(q => {
      const idleExpert = !q.deepRequested && !q.deep.text && !q.deep.stream && !q.deep.done && q.deep.error === null;
      return { ...q, fast: finish(q.fast), deep: idleExpert ? q.deep : finish(q.deep) };
    }),
  };
}

function terminalSession(s: SessionView): boolean {
  return s.stopped !== null || s.connection?.state === "stopped";
}

export function reduce(s: SessionView, a: Action): SessionView {
  if (a.type === "reset") return { ...initial, running: a.running };
  if (a.type === "deep_requested") return terminalSession(s) ? s : updateQuestion(s, a.id, (q) => q.deepRequested ? q : ({ ...q, deepRequested: true }));
  if (a.type === "note_sent") return { ...s, sentNotes: [...s.sentNotes.slice(-49), { id: a.id, text: a.text, result: null }] };
  if (a.type === "note_asked") return { ...s, notes: s.notes.map((n) => (n.id === a.id ? { ...n, asked: true } : n)) };
  const e = a.event;
  switch (e.kind) {
    case "context_reset":
      return { ...initial };
    case "connection": {
      if (terminalSession(s) && e.state.state !== "stopped") return s;
      if (e.state.state === "stopped") {
        // End streaming immediately; the separate native stopped event makes the
        // saved session ready for review. Keep that ordering for the call workspace.
        return { ...stopAnswers(s, e.state.reason), stopped: s.stopped, connection: e.state };
      }
      const next = { ...s, connection: e.state, running: true };
      if (e.state.state === "live") {
        return log({ ...next, stopped: null, socketClosed: false, sessionId: e.state.session_id }, `live ${e.state.resumed ? "(resumed)" : "(new session)"} ${e.state.session_id}`);
      }
      if (e.state.state === "reconnecting") return log(next, `reconnecting in ${e.state.in_ms} ms (attempt ${e.state.attempt + 1})`);
      return next;
    }
    case "stopped":
      return log(stopAnswers(s, e.reason), `stopped: ${e.reason}`);
    case "closed":
      return log({ ...s, socketClosed: true }, `socket closed ${e.code ?? "(no close frame)"} ${e.reason}`);
    case "warning":
      return log(s, `warning: ${e.message}`);
    case "vad":
      if (terminalSession(s)) return s;
      return { ...s, speaking: { ...s.speaking, [e.channel]: e.speaking } };
    case "latency":
      return { ...s, latency: { ...s.latency, [e.question.question_id]: e.question } };
    case "server":
      break;
  }
  if (s.socketClosed || s.stopped !== null) return s;
  const m = e.message;
  switch (m.type) {
    case "status":
      return log({ ...s, status: { ...s.status, [m.component]: { state: m.state, message: m.message } } }, `status ${m.component}: ${m.state} ${m.message}`);
    case "transcript.partial":
    case "transcript.final": {
      const key = `${m.channel}:${m.seg_id}`;
      if (m.type === "transcript.final" && m.echo) {
        // The other side's voice in the mic: not the user's words.
        return {
          ...s,
          transcript: s.transcript.filter((t) => t.key !== key),
          timeline: s.timeline.filter((r) => !(r.kind === "line" && r.key === key)),
        };
      }
      const line: TranscriptLine =
        m.type === "transcript.final"
          ? { key, channel: m.channel, text: m.text, final: true, startMs: m.start_ms, endMs: m.end_ms }
          : { key, channel: m.channel, text: m.text, final: false };
      const i = s.transcript.findIndex((t) => t.key === key);
      if (i >= 0 && s.transcript[i].final && !line.final) return s;
      if (i >= 0) return { ...s, transcript: s.transcript.map((t, j) => (j === i ? line : t)) };
      return { ...s, transcript: [...s.transcript, line], timeline: [...s.timeline, { kind: "line", key }] };
    }
    case "question.detected": {
      if (s.cancelledIds[m.question_id]) return s;
      const exists = s.questions.some((q) => q.id === m.question_id);
      if (exists) {
        return updateQuestion(s, m.question_id, (q) => q.cancelled || (!q.speculative && m.speculative) ? q : ({
          ...q,
          text: m.text,
          type: m.question_type,
          confidence: m.confidence,
          speculative: m.speculative,
          answerStrategy: m.answer_strategy ?? q.answerStrategy,
          deepRequested: q.deepRequested || automaticExpert(s, m.question_type, m.answer_strategy ?? q.answerStrategy),
          ...(m.screen ? { screen: m.screen } : {}),
        }));
      }
      const q: QuestionView = {
        id: m.question_id,
        text: m.text,
        type: m.question_type,
        confidence: m.confidence,
        speculative: m.speculative,
        cancelled: false,
        fast: emptyPass(),
        deep: emptyPass(),
        answerStrategy: m.answer_strategy ?? "legacy_dual",
        deepRequested: automaticExpert(s, m.question_type, m.answer_strategy),
        ...(m.screen ? { screen: m.screen } : {}),
      };
      return { ...s, questions: [...s.questions, q], timeline: [...s.timeline, { kind: "question", id: q.id }] };
    }
    case "question.cancelled":
      if (s.cancelledIds[m.question_id]) return s;
      return updateQuestion({ ...s, cancelledIds: { ...s.cancelledIds, [m.question_id]: true } }, m.question_id, (q) => ({ ...q, cancelled: true }));
    case "answer.delta":
      return updateQuestion(s, m.question_id, (q) => {
        if (q.cancelled) return q;
        const pass = appendDelta(q[m.pass], m.text, m.sequence, m.sequence_end);
        return pass === q[m.pass] ? q : { ...q, [m.pass]: pass };
      });
    case "answer.done":
      return updateQuestion(s, m.question_id, (q) => {
        const p = q[m.pass];
        if (q.cancelled || p.done) return q;
        const stream = m.delta_count == null ? p.stream : { ...(p.stream ?? { next: 0, pending: {} }), expected: m.delta_count };
        return { ...q, [m.pass]: { ...p, done: true, refs: m.memory_refs, source: m.source ?? null, stream, error: stream?.expected !== undefined && stream.next < stream.expected ? "incomplete_stream" : null } };
      });
    case "answer.error":
      return updateQuestion(s, m.question_id, (q) => q.cancelled || (q[m.pass].done && !(m.code === "cancelled" && q[m.pass].error === "incomplete_stream")) ? q : ({ ...q, [m.pass]: { ...q[m.pass], done: true, error: m.code, stream: undefined } }));
    case "error":
      return log(s, `gateway error ${m.code}${m.fatal ? " (fatal)" : ""}: ${m.message}`);
    case "session.ready":
      return log({ ...s, deepAuto: m.deep_auto_types ?? null, spokenLanguage: m.spoken_language ?? s.spokenLanguage, languageSwitching: m.language_switching === true, languageResult: null }, `session ready, ${m.limits.max_minutes} min limit${m.me_audio ? ", your words live" : ""}`);
    case "session.language":
      return { ...s, spokenLanguage: m.spoken_language, languageResult: { requestId: m.request_id ?? null, error: m.error ?? null, seq: m.seq } };
    // Guest seats, the host's side.
    case "seats.update":
      return { ...s, seats: m.seats };
    case "seat.note":
      if (s.notes.some((n) => n.id === m.note_id)) return s;
      return { ...s, notes: [...s.notes, { id: m.note_id, seatId: m.seat_id, name: m.name, text: m.text, at: m.at, asked: false }] };
    case "sharing":
      return { ...s, sharing: m.on };
    // Guest seats, the guest's side.
    case "seat.ready":
      return log(
        {
          ...s,
          seat: {
            sessionId: m.session_id,
            seatId: m.seat_id,
            hostName: m.host_name,
            mode: m.mode,
            title: m.title ?? null,
            capabilities: m.capabilities,
            hostConnected: m.host_connected,
            sharing: m.sharing,
          },
        },
        `watching ${m.host_name}'s call`,
      );
    case "seat.snapshot": {
      // The call so far replaces whatever was shown; the seat itself and the notes stay.
      let next: SessionView = { ...s, questions: [], transcript: [], timeline: [], status: {} };
      for (const raw of m.messages) next = reduce(next, { type: "event", event: { kind: "server", message: raw as unknown as ServerMessage } });
      return next;
    }
    case "seat.host":
      return s.seat ? { ...s, seat: { ...s.seat, hostConnected: m.connected, sharing: m.sharing } } : s;
    case "seat.update":
      return s.seat ? { ...s, seat: { ...s.seat, capabilities: m.capabilities } } : s;
    case "seat.command.result":
      return { ...s, sentNotes: s.sentNotes.map((n) => (n.id === m.command_id ? { ...n, result: m.result } : n)) };
    default:
      return s;
  }
}

/// What the overlay shows, in order: the other side's lines, the user's own lines, and each
/// question with its answers. A speculative question that was cancelled is left out.
export type ConversationItem =
  | { kind: "them"; key: string; text: string; final: boolean }
  | { kind: "me"; key: string; text: string; final: boolean }
  | { kind: "question"; key: string; question: QuestionView };

export function conversation(s: SessionView): ConversationItem[] {
  const lines = new Map(s.transcript.map((t) => [t.key, t]));
  const questions = new Map(s.questions.map((q) => [q.id, q]));
  const out: ConversationItem[] = [];
  for (const ref of s.timeline) {
    if (ref.kind === "line") {
      const t = lines.get(ref.key);
      if (!t || !t.text) continue;
      out.push({ kind: t.channel === "me" ? "me" : "them", key: t.key, text: t.text, final: t.final });
    } else {
      const q = questions.get(ref.id);
      if (q && !q.cancelled) out.push({ kind: "question", key: q.id, question: q });
    }
  }
  return out;
}

/// A past call rebuilt as the overlay showed it (History): every line and question in the
/// order it appeared, by time from the start of the call. A line appeared when it started; a
/// question when it was detected, so it follows the line it answers. Nothing is live.
export function fromReplay(r: Replay): SessionView {
  // A missing deep pass was never asked for (not done, so the card says so); a missing fast
  // pass simply has nothing to show.
  const passOf = (p: ReplayPass | null, missingDone: boolean): PassView =>
    p ? { text: p.text, done: true, error: null, source: p.source, refs: p.refs } : { ...emptyPass(), done: missingDone };
  const transcript: TranscriptLine[] = r.segments
    .filter((seg) => seg.text)
    .map((seg) => ({ key: `${seg.channel}:${seg.seg_id}`, channel: seg.channel, text: seg.text, final: true, startMs: seg.start_ms, endMs: seg.end_ms }));
  const questions: QuestionView[] = r.questions.map((q) => ({
    id: q.id,
    text: q.text,
    type: q.type as QuestionType,
    confidence: 1,
    speculative: false,
    cancelled: false,
    fast: passOf(q.fast, true),
    deep: passOf(q.deep, false),
    deepRequested: Boolean(q.deep),
    answerStrategy: q.answer_strategy ?? "legacy_dual",
  }));
  const timed: { at: number; order: number; ref: TimelineRef }[] = [
    ...r.segments
      .filter((seg) => seg.text)
      .map((seg, i) => ({ at: seg.start_ms, order: i, ref: { kind: "line", key: `${seg.channel}:${seg.seg_id}` } as TimelineRef })),
    ...r.questions.map((q, i) => ({ at: q.offset_ms, order: r.segments.length + i, ref: { kind: "question", id: q.id } as TimelineRef })),
  ];
  timed.sort((a, b) => a.at - b.at || a.order - b.order);
  return { ...initial, sessionId: r.session_id, transcript, questions, timeline: timed.map((t) => t.ref) };
}

/// Apply the core's compacted snapshot, then live events. Native epochs distinguish a
/// new session from a duplicate event numbered zero; older cores use session identity.
export function snapshotApplier(dispatch: (a: Action) => void) {
  let next = -1;
  let epoch: number | undefined;
  let sessionId: string | null = null;
  const retired = new Set<string>();
  const early: Numbered[] = [];
  const identity = (e: UiEvent): string | null => {
    if (e.kind === "connection" && e.state.state === "live") return e.state.session_id;
    if (e.kind === "server" && (e.message.type === "session.ready" || e.message.type === "seat.ready")) return e.message.session_id;
    return null;
  };
  const apply = (e: Numbered) => {
    const id = identity(e.event);
    if (e.epoch !== undefined) {
      if (epoch !== undefined && e.epoch < epoch) return;
      if (epoch !== undefined && e.epoch > epoch) {
        dispatch({ type: "reset", running: true });
        sessionId = null;
        next = 0;
      }
      epoch = e.epoch;
    } else if (epoch !== undefined) {
      // Never let an older core's queued, untagged event cross a tagged boundary.
      return;
    } else if (id && sessionId && id !== sessionId) {
      if (retired.has(id)) return;
      retired.add(sessionId);
      dispatch({ type: "reset", running: true });
      sessionId = null;
      next = 0;
    }
    if (id) sessionId = id;
    if (e.n < next && e.event.kind === "server") {
      const m = e.event.message;
      // Per-pass positions let a reordered chunk repair a gap after a newer event.
      // Cancellation is terminal even when it arrives before detection or completion.
      if ((m.type === "answer.delta" && m.sequence != null) || (m.type === "answer.done" && m.delta_count != null)
          || m.type === "question.cancelled" || (m.type === "answer.error" && m.code === "cancelled")) {
        dispatch({ type: "event", event: e.event });
      }
      return;
    }
    if (e.n < next) return;
    dispatch({ type: "event", event: e.event });
    next = e.n + 1;
  };
  return {
    live(e: Numbered) {
      if (next < 0) early.push(e);
      else apply(e);
    },
    snapshot(events: Numbered[], running: boolean, nextN?: number, epochN?: number) {
      dispatch({ type: "reset", running });
      epoch = epochN ?? events.find(e => e.epoch !== undefined)?.epoch;
      sessionId = null;
      retired.clear();
      // Compacted entries retain their original place, but their n may be newer.
      for (const e of events) {
        dispatch({ type: "event", event: e.event });
        sessionId = identity(e.event) ?? sessionId;
      }
      next = nextN ?? (events.length ? Math.max(...events.map(e => e.n)) + 1 : 0);
      for (const e of early) apply(e);
      early.length = 0;
    },
  };
}

export function useSession(): [SessionView, (a: Action) => void] {
  const [state, dispatch] = useReducer(reduce, initial);
  useEffect(() => {
    if (!inTauri) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;
    const applier = snapshotApplier(dispatch);
    onEvent((e) => applier.live(e)).then((u) => {
      if (disposed) u();
      else unlisten = u;
    });
    api.snapshot().then(([events, running, next, epoch]) => {
      if (!disposed) applier.snapshot(events, running, next, epoch);
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);
  return [state, dispatch];
}

/// Why a call stopped, in words.
export const STOPPED: Record<StopReason, string> = {
  ended: "Call ended",
  retention_failed: "Call stopped; saving failed",
  end_unconfirmed: "Call stopped; saving not confirmed",
  no_minutes: "No minutes left",
  session_active: "Another call is active",
  replaced: "Continued in another app",
  unauthorized: "Sign-in rejected",
  outdated: "Updating the app",
  seat_removed: "The host removed you from the call",
  seat_closed: "This seat is closed",
  left: "You left the call",
};

/** A stopped call may have saved only part of its history; do not imply data was lost. */
export function stopMessage(reason: StopReason): string | undefined {
  if (reason === "retention_failed") return "The gateway could not save all call data. Check History for what is available.";
  if (reason === "end_unconfirmed") return "The connection closed before the gateway confirmed completion. Check History for what is available.";
  return undefined;
}

/** The call whose post-call screen to show, if any: one of this app's own calls that ended.
 * A guest never gets one: the call was someone else's, and it may still be going on. */
export function postCallFor(s: SessionView): string | null {
  if (s.running || s.seat) return null;
  return s.stopped === "ended" ? s.sessionId : null;
}

/// One badge for the overlay: the connection first, then the worst component status.
export function badge(s: SessionView): { state: BadgeState | "off"; text: string } {
  const c = s.connection;
  if (!s.running || !c) return { state: "off", text: s.stopped ? STOPPED[s.stopped] : "Not in a call" };
  if (c.state === "connecting") return { state: "reconnecting", text: "Connecting" };
  if (c.state === "reconnecting") return { state: "reconnecting", text: "Reconnecting" };
  if (c.state === "stopped") return { state: "off", text: STOPPED[c.reason] };
  if (s.seat && !s.seat.hostConnected) return { state: "reconnecting", text: `${s.seat.hostName} is reconnecting` };
  if (s.seat && !s.seat.sharing) return { state: "degraded", text: `${s.seat.hostName} paused sharing` };
  const statuses = Object.entries(s.status).filter(([, v]) => v.state !== "ok");
  const worst = statuses.find(([, v]) => v.state === "reconnecting") ?? statuses[0];
  if (worst) return { state: worst[1].state, text: worst[1].message || `${worst[0]} ${worst[1].state}` };
  return { state: "ok", text: "Listening" };
}
