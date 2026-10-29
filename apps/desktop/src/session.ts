// The session as the UI sees it, folded from the core's event stream. Both windows run the
// same reducer; a window opened or reloaded mid-call replays the snapshot first, so the whole
// conversation comes back (the core keeps the session's events, compacted; see
// src-tauri/src/history.rs).
import { useEffect, useReducer } from "react";
import type { MemoryRef, QuestionType } from "@copilot/protocol";
import { api, inTauri, onEvent, type ConnectionState, type Numbered, type QuestionLatency, type Replay, type ReplayPass, type StopReason, type UiEvent } from "./api";

export interface PassView {
  text: string;
  done: boolean;
  error: string | null; // answer.error code; "cancelled" keeps what arrived
  source: string | null;
  refs: MemoryRef[];
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
}

export interface TranscriptLine {
  key: string;
  channel: string;
  text: string;
  final: boolean;
}

export type BadgeState = "ok" | "degraded" | "reconnecting";

/// One entry of the conversation, in the order it first appeared: a transcript line (either
/// side) or a question with its answers.
export type TimelineRef = { kind: "line"; key: string } | { kind: "question"; id: string };

export interface SessionView {
  running: boolean;
  /** The session last live in this run; kept after it stops, for the post-call screen. */
  sessionId: string | null;
  connection: ConnectionState | null;
  stopped: StopReason | null;
  status: Record<string, { state: BadgeState; message: string }>;
  /** Every question of the session; none is ever removed. */
  questions: QuestionView[];
  /** Every transcript line of the session, both sides; none is removed (an echo line is). */
  transcript: TranscriptLine[];
  timeline: TimelineRef[];
  /** Question types whose deep answer starts on its own (session.ready), if the gateway said. */
  deepAuto: QuestionType[] | null;
  latency: Record<string, QuestionLatency>;
  log: string[];
  speaking: { them: boolean; me: boolean };
}

/// Before session.ready says otherwise (an older gateway): the three types it auto-starts.
export const AUTO_DEEP: QuestionType[] = ["behavioral", "technical", "system_design"];

const emptyPass = (): PassView => ({ text: "", done: false, error: null, source: null, refs: [] });

export const initial: SessionView = {
  running: false,
  sessionId: null,
  connection: null,
  stopped: null,
  status: {},
  questions: [],
  transcript: [],
  timeline: [],
  deepAuto: null,
  latency: {},
  log: [],
  speaking: { them: false, me: false },
};

type Action = { type: "event"; event: UiEvent } | { type: "reset"; running: boolean } | { type: "deep_requested"; id: string };

export function deepStartsAlone(s: SessionView, type: QuestionType): boolean {
  return (s.deepAuto ?? AUTO_DEEP).includes(type);
}

function updateQuestion(s: SessionView, id: string, f: (q: QuestionView) => QuestionView): SessionView {
  return { ...s, questions: s.questions.map((q) => (q.id === id ? f(q) : q)) };
}

function log(s: SessionView, line: string): SessionView {
  const stamp = new Date().toLocaleTimeString();
  return { ...s, log: [...s.log.slice(-199), `${stamp} ${line}`] };
}

export function reduce(s: SessionView, a: Action): SessionView {
  if (a.type === "reset") return { ...initial, running: a.running };
  if (a.type === "deep_requested") return updateQuestion(s, a.id, (q) => ({ ...q, deepRequested: true }));
  const e = a.event;
  switch (e.kind) {
    case "connection": {
      const next = { ...s, connection: e.state, running: e.state.state !== "stopped" };
      if (e.state.state === "live") {
        return log({ ...next, stopped: null, sessionId: e.state.session_id }, `live ${e.state.resumed ? "(resumed)" : "(new session)"} ${e.state.session_id}`);
      }
      if (e.state.state === "reconnecting") return log(next, `reconnecting in ${e.state.in_ms} ms (attempt ${e.state.attempt + 1})`);
      return next;
    }
    case "stopped":
      return log({ ...s, stopped: e.reason, running: false }, `stopped: ${e.reason}`);
    case "closed":
      return log(s, `socket closed ${e.code ?? "(no close frame)"} ${e.reason}`);
    case "warning":
      return log(s, `warning: ${e.message}`);
    case "vad":
      return { ...s, speaking: { ...s.speaking, [e.channel]: e.speaking } };
    case "latency":
      return { ...s, latency: { ...s.latency, [e.question.question_id]: e.question } };
    case "server":
      break;
  }
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
      const line = { key, channel: m.channel, text: m.text, final: m.type === "transcript.final" };
      const i = s.transcript.findIndex((t) => t.key === key);
      if (i >= 0) return { ...s, transcript: s.transcript.map((t, j) => (j === i ? line : t)) };
      return { ...s, transcript: [...s.transcript, line], timeline: [...s.timeline, { kind: "line", key }] };
    }
    case "question.detected": {
      const exists = s.questions.some((q) => q.id === m.question_id);
      if (exists) {
        return updateQuestion(s, m.question_id, (q) => ({
          ...q,
          text: m.text,
          type: m.question_type,
          confidence: m.confidence,
          speculative: m.speculative,
          cancelled: false,
          deepRequested: q.deepRequested || deepStartsAlone(s, m.question_type),
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
        deepRequested: deepStartsAlone(s, m.question_type),
      };
      return { ...s, questions: [...s.questions, q], timeline: [...s.timeline, { kind: "question", id: q.id }] };
    }
    case "question.cancelled":
      return updateQuestion(s, m.question_id, (q) => ({ ...q, cancelled: true }));
    case "answer.delta":
      return updateQuestion(s, m.question_id, (q) => ({ ...q, [m.pass]: { ...q[m.pass], text: q[m.pass].text + m.text } }));
    case "answer.done":
      return updateQuestion(s, m.question_id, (q) => ({
        ...q,
        [m.pass]: { ...q[m.pass], done: true, refs: m.memory_refs, source: m.source ?? null },
      }));
    case "answer.error":
      return updateQuestion(s, m.question_id, (q) => ({ ...q, [m.pass]: { ...q[m.pass], done: true, error: m.code } }));
    case "error":
      return log(s, `gateway error ${m.code}${m.fatal ? " (fatal)" : ""}: ${m.message}`);
    case "session.ready":
      return log({ ...s, deepAuto: m.deep_auto_types ?? null }, `session ready, ${m.limits.max_minutes} min limit${m.me_audio ? ", your words live" : ""}`);
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
    .map((seg) => ({ key: `${seg.channel}:${seg.seg_id}`, channel: seg.channel, text: seg.text, final: true }));
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

/// Apply the core's snapshot, then live events from its `next` number on. A live event numbered
/// 0 after others means a new session started in this run: its numbering starts over.
export function snapshotApplier(dispatch: (a: Action) => void) {
  let next = -1; // -1 until the snapshot is in
  const early: Numbered[] = [];
  const apply = (e: Numbered) => {
    if (e.n === 0 && next > 0) {
      dispatch({ type: "reset", running: true });
      next = 0;
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
    snapshot(events: Numbered[], running: boolean, nextN?: number) {
      dispatch({ type: "reset", running });
      // Compacted entries keep their first place but may hold later events, so their own
      // numbers are not in order: apply them all, then count from the core's `next`.
      for (const e of events) dispatch({ type: "event", event: e.event });
      next = nextN ?? (events.length ? Math.max(...events.map((e) => e.n)) + 1 : 0);
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
    api.snapshot().then(([events, running, next]) => applier.snapshot(events, running, next));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);
  return [state, dispatch];
}

/// One badge for the overlay: the connection first, then the worst component status.
export function badge(s: SessionView): { state: BadgeState | "off"; text: string } {
  const c = s.connection;
  if (!s.running || !c) return { state: "off", text: s.stopped ? `Stopped: ${s.stopped}` : "Not in a session" };
  if (c.state === "connecting") return { state: "reconnecting", text: "Connecting" };
  if (c.state === "reconnecting") return { state: "reconnecting", text: "Reconnecting" };
  if (c.state === "stopped") return { state: "off", text: `Stopped: ${c.reason}` };
  const statuses = Object.entries(s.status).filter(([, v]) => v.state !== "ok");
  const worst = statuses.find(([, v]) => v.state === "reconnecting") ?? statuses[0];
  if (worst) return { state: worst[1].state, text: worst[1].message || `${worst[0]} ${worst[1].state}` };
  return { state: "ok", text: "Listening" };
}
