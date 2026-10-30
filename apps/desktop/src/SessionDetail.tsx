// One session after its call: the post-call screen right after a session ends, and a past
// session opened from History. Polls GET /sessions/{id} for up to 5 minutes until the
// outputs are ready, then shows them and, when the transcript was stored, the call as the
// overlay showed it (both sides and every suggestion), with the plain transcript below.
import { useCallback, useEffect, useMemo, useState } from "react";
import { rest, type Replay, type SessionDetail as Detail } from "./api";
import { ConversationView, useViewPrefs, ViewControls } from "./Overlay";
import { fromReplay } from "./session";
import { when } from "./ui";
import { OUTPUT_TITLES, outputsInOrder, pollSession, transcriptRows, waitingText, type PollState, type TranscriptRow } from "./postcall";

export function SessionDetail({ sessionId, onDeleted, onClose }: { sessionId: string; onDeleted?: () => void; onClose?: () => void }) {
  const [state, setState] = useState<PollState>({ phase: "waiting", session: null, elapsedMs: 0, error: null });
  const [round, setRound] = useState(0); // bumped by "Look again"
  const [transcript, setTranscript] = useState<TranscriptRow[] | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTranscript(null);
    setReplay(null);
    pollSession(() => rest.session(sessionId), (s) => !cancelled && setState(s), { cancelled: () => cancelled }).then((final) => {
      if (cancelled || final.phase !== "ready" || !final.session.store_transcript) return;
      rest.replay(sessionId).then(
        (r) => {
          if (cancelled) return;
          setReplay(r);
          setTranscript(transcriptRows(r.segments));
        },
        (e) => !cancelled && setMessage(`Transcript: ${e}`),
      );
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, round]);

  const remove = async () => {
    if (!window.confirm("Delete this session, its transcript and its outputs? Facts already approved into your memory stay.")) return;
    try {
      await rest.deleteSession(sessionId);
      onDeleted?.();
    } catch (e) {
      setMessage(String(e));
    }
  };

  const session = state.phase === "gone" ? null : state.session;
  return (
    <section className="panel detail">
      <div className="detail-head">
        <h2>{session ? heading(session) : "Session"}</h2>
        <span className="spacer" />
        {onClose && <button onClick={onClose}>Close</button>}
        {onDeleted && (
          <button className="danger" onClick={remove}>
            Delete
          </button>
        )}
      </div>
      {session && <p className="note">{facts(session)}</p>}
      {session?.store_transcript && <AudioStatus session={session} />}

      {state.phase !== "ready" && (
        <div className="callout">
          {state.phase === "waiting" && <span className="spinner" />}
          {waitingText(state, Boolean(session?.store_transcript))}
          {state.phase === "timeout" && (
            <button className="link" onClick={() => setRound((r) => r + 1)}>
              Look again
            </button>
          )}
          {state.phase === "waiting" && state.error && <p className="note warn">{state.error}</p>}
        </div>
      )}

      {state.phase === "ready" &&
        outputsInOrder(state.session).map(([kind, text]) => (
          <article key={kind} className="output">
            <div className="output-head">
              <h3>{OUTPUT_TITLES[kind]}</h3>
              {(kind === "follow_up_email" || kind === "standup_report") && <CopyButton text={text} />}
            </div>
            <div className="output-text">{text}</div>
            {kind === "standup_report" && state.session.employer_id && (
              <p className="note">The commitments were added to this company's open commitments for the next standup; remove any under Work, Facts.</p>
            )}
          </article>
        ))}
      {state.phase === "ready" && outputsInOrder(state.session).length === 0 && <p className="note">No summary: this call was saved with its transcript only.</p>}

      {state.phase === "ready" && session?.store_transcript && (
        <article className="output">
          <h3>The call</h3>
          {replay === null && !message && <p className="note">Loading…</p>}
          {replay && <CallReplay replay={replay} />}
        </article>
      )}
      {state.phase === "ready" && session?.store_transcript && transcript && transcript.length > 0 && (
        <details className="output">
          <summary>
            <h3>Transcript</h3>
          </summary>
          <ul className="plain transcript-rows">
            {transcript.map((r) => (
              <li key={r.key}>
                <span className="muted">{r.at}</span> <b className={r.who === "You" ? "you" : ""}>{r.who}</b> {r.text}
              </li>
            ))}
          </ul>
        </details>
      )}
      {state.phase === "ready" && session && !session.store_transcript && (
        <p className="note">No transcript: it was not stored for this call (no consent).</p>
      )}
      {message && <p className="note warn">{message}</p>}
    </section>
  );
}

/** The overlay as it was during the call: the same layouts, text size and compact view, read
 * only (the thumbs show what was rated). */
function CallReplay({ replay }: { replay: Replay }) {
  const s = useMemo(() => fromReplay(replay), [replay]);
  const view = useViewPrefs();
  const [compact, setCompact] = useState(false);
  const ratings = useMemo(() => Object.fromEntries(replay.questions.map((q) => [q.id, q.feedback])), [replay]);
  return (
    <div className={compact ? "overlay replay compact" : "overlay replay"}>
      <header className="overlay-head">
        <span className="badge badge-off">
          <span className="dot" />
          Replay
        </span>
        <span className="muted">
          {replay.questions.length} suggestion{replay.questions.length === 1 ? "" : "s"}
        </span>
        <span className="spacer" />
        <ViewControls view={view} compact={compact} onCompact={() => setCompact(!compact)} />
      </header>
      <ConversationView
        s={s}
        compact={compact}
        layout={view.layout}
        textScale={view.textScale}
        live={false}
        ratings={ratings}
        emptyAnswers="Nothing was suggested in this call."
        emptyTranscript="Nothing was transcribed."
      />
    </div>
  );
}

function heading(s: Detail): string {
  const mode = s.mode ? s.mode[0].toUpperCase() + s.mode.slice(1) : "Session";
  return s.title ? `${mode}: ${s.title}` : mode;
}

function facts(s: Detail): string {
  const parts: string[] = [];
  if (s.started_at) parts.push(when(s.started_at));
  if (s.billed_seconds != null && s.ended_at) parts.push(`${Math.max(1, Math.round(s.billed_seconds / 60))} min`);
  parts.push(`${s.questions} question${s.questions === 1 ? "" : "s"}`);
  if (s.platform) parts.push(s.platform);
  return parts.join(" · ");
}

function AudioStatus({ session }: { session: Detail }) {
  let text: string;
  if (session.me_audio_status === "live") text = "Your side of the call was transcribed live; no audio was kept.";
  else if (session.me_audio_status === "transcribed") text = "Your side of the call was transcribed; the audio is deleted.";
  else text = "Your side of the call was not transcribed; the summary is from the other side only.";
  return <p className="note">{text}</p>;
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    setDone(true);
    setTimeout(() => setDone(false), 1500);
  }, [text]);
  return <button onClick={copy}>{done ? "Copied" : "Copy"}</button>;
}
