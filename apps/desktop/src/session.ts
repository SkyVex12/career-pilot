// The session as the UI sees it, folded from the core's event stream. Both windows run the
// same reducer; a window opened mid-call replays the snapshot first.
import { useEffect, useReducer } from "react";
import type { MemoryRef, QuestionType } from "@copilot/protocol";
import { api, inTauri, onEvent, type ConnectionState, type Numbered, type QuestionLatency, type StopReason, type UiEvent } from "./api";

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

export interface SessionView {
  running: boolean;
  /** The session last live in this run; kept after it stops, for the post-call screen. */
  sessionId: string | null;
  connection: ConnectionState | null;
  stopped: StopReason | null;
  status: Record<string, { state: BadgeState; message: string }>;
  questions: QuestionView[];
  transcript: TranscriptLine[];
  latency: Record<string, QuestionLatency>;
  log: string[];
  speaking: { them: boolean; me: boolean };
}

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
  latency: {},
  log: [],
  speaking: { them: false, me: false },
};

type Action = { type: "event"; event: UiEvent } | { type: "reset"; running: boolean } | { type: "deep_requested"; id: string };

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
      const line = { key, channel: m.channel, text: m.text, final: m.type === "transcript.final" };
      const i = s.transcript.findIndex((t) => t.key === key);
      const transcript = i >= 0 ? s.transcript.map((t, j) => (j === i ? line : t)) : [...s.transcript.slice(-299), line];
      return { ...s, transcript };
    }
    case "question.detected": {
      const exists = s.questions.some((q) => q.id === m.question_id);
      if (exists) {
        return updateQuestion(s, m.question_id, (q) => ({ ...q, text: m.text, type: m.question_type, confidence: m.confidence, speculative: m.speculative, cancelled: false }));
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
        deepRequested: AUTO_DEEP.includes(m.question_type),
      };
      return { ...s, questions: [...s.questions.slice(-49), q] };
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
      return log(s, `session ready, ${m.limits.max_minutes} min limit`);
    default:
      return s;
  }
}

export function useSession(): [SessionView, (a: Action) => void] {
  const [state, dispatch] = useReducer(reduce, initial);
  useEffect(() => {
    if (!inTauri) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;
    let next = -1; // first n not yet applied; -1 until the snapshot is in
    const early: Numbered[] = [];
    const apply = (e: Numbered) => {
      if (e.n === 0 && next > 0) {
        // A new session started: its numbering starts over.
        dispatch({ type: "reset", running: true });
        next = 0;
      }
      if (e.n < next) return;
      dispatch({ type: "event", event: e.event });
      next = e.n + 1;
    };
    onEvent((e) => (next < 0 ? early.push(e) : apply(e))).then((u) => {
      if (disposed) u();
      else unlisten = u;
    });
    api.snapshot().then(([events, running]) => {
      dispatch({ type: "reset", running });
      next = 0;
      for (const e of events) apply(e);
      for (const e of early) apply(e);
    });
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
