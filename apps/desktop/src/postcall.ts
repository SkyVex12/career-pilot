// After a call (ARCHITECTURE.md, "REST API"): the app polls GET /sessions/{id} for up to
// 5 minutes until the post-call outputs are ready. Pure functions, so the tests drive them
// with a fake clock.
import type { Mode } from "@copilot/protocol";
import type { OutputKind, Segment, SessionDetail } from "./api";

export const POLL_INTERVAL_MS = 3_000;
export const POLL_TIMEOUT_MS = 5 * 60_000;

export type PollState =
  | { phase: "waiting"; session: SessionDetail | null; elapsedMs: number; error: string | null }
  | { phase: "ready"; session: SessionDetail }
  | { phase: "timeout"; session: SessionDetail | null; error: string | null };

export interface PollOptions {
  intervalMs?: number;
  timeoutMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  /** Stop early (the screen closed); the last state is returned as it was. */
  cancelled?: () => boolean;
}

const realSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/// Fetch until the session is `ready` or the time is up. Errors (the API restarting, a
/// session not flushed yet) keep the poll going; they show in `error`.
export async function pollSession(
  fetch: () => Promise<SessionDetail>,
  onUpdate: (s: PollState) => void,
  { intervalMs = POLL_INTERVAL_MS, timeoutMs = POLL_TIMEOUT_MS, now = Date.now, sleep = realSleep, cancelled = () => false }: PollOptions = {},
): Promise<PollState> {
  const started = now();
  let session: SessionDetail | null = null;
  let error: string | null = null;
  for (;;) {
    try {
      session = await fetch();
      error = null;
    } catch (e) {
      error = String(e);
    }
    if (cancelled()) return { phase: "waiting", session, elapsedMs: now() - started, error };
    if (session?.status === "ready") {
      const ready: PollState = { phase: "ready", session };
      onUpdate(ready);
      return ready;
    }
    const elapsedMs = now() - started;
    if (elapsedMs >= timeoutMs) {
      const timeout: PollState = { phase: "timeout", session, error };
      onUpdate(timeout);
      return timeout;
    }
    onUpdate({ phase: "waiting", session, elapsedMs, error });
    await sleep(Math.min(intervalMs, timeoutMs - elapsedMs));
  }
}

export const OUTPUT_TITLES: Record<OutputKind, string> = {
  summary: "Summary",
  feedback: "Feedback",
  follow_up_email: "Follow-up email",
  standup_report: "Standup report",
};

const ORDER: Record<Mode, OutputKind[]> = {
  interview: ["summary", "feedback", "follow_up_email"],
  standup: ["standup_report", "summary"],
  meeting: ["summary", "feedback"],
};

/// The outputs to show, in the mode's order, then anything else the worker wrote.
export function outputsInOrder(session: SessionDetail): [OutputKind, string][] {
  const order = ORDER[session.mode ?? "meeting"] ?? ORDER.meeting;
  const all = Object.entries(session.outputs).filter((e): e is [OutputKind, string] => typeof e[1] === "string" && e[1].length > 0);
  const known = order.flatMap((k) => all.filter(([kind]) => kind === k));
  return [...known, ...all.filter(([kind]) => !order.includes(kind))];
}

export function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(h ? 2 : 1, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export interface TranscriptRow {
  key: string;
  at: string;
  who: "Them" | "You";
  text: string;
}

/// Both channels interleaved by start time.
export function transcriptRows(segments: Segment[]): TranscriptRow[] {
  return [...segments]
    .sort((a, b) => a.start_ms - b.start_ms || a.channel.localeCompare(b.channel) || a.seg_id - b.seg_id)
    .map((s) => ({ key: `${s.channel}:${s.seg_id}`, at: clock(s.start_ms), who: s.channel === "me" ? "You" : "Them", text: s.text }));
}

/// What the post-call screen says while it waits.
export function waitingText(state: PollState, storeTranscript: boolean): string {
  if (state.phase === "ready") return "Ready.";
  if (state.phase === "timeout") return "Not ready after 5 minutes. The workers may be busy or stopped; look again in History.";
  const s = state.session;
  if (!s || s.status === "live") return "Ending the session…";
  if (storeTranscript && s.me_audio_status === "pending") return "Waiting for your audio, then writing the summary…";
  if (s.me_audio_status === "uploaded") return "Transcribing your side of the call…";
  return "Writing the summary…";
}
