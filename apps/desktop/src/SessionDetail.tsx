// One call after it ended: the Call screen right after it ends, and a past call opened from
// History. Polls GET /sessions/{id} for up to 5 minutes until the outputs are ready, then shows
// them; when the transcript was stored, also the call as the overlay showed it (Replay) and the
// plain transcript (Transcript).
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { rest, type Replay, type SessionDetail as Detail } from "./api";
import { ConversationView, useViewPrefs, ViewControls } from "./Overlay";
import { fromReplay } from "./session";
import { when } from "./shared";
import { OUTPUT_TITLES, outputsInOrder, pollSession, transcriptRows, waitingText, type PollState, type TranscriptRow } from "./postcall";
import { Banner, Button, Card, ConfirmDialog, EmptyState, Menu, PageHeader, Skeleton, SkeletonList, Spinner, Tabs, useToast } from "./ui";
import { MODE_LABEL } from "./calls";

export function SessionDetail({
  sessionId,
  onDeleted,
  onClose,
  companies = {},
  justEnded = false,
  onNewCall,
}: {
  sessionId: string;
  onDeleted?: () => void;
  onClose?: () => void;
  companies?: Record<string, string>;
  /** The Call screen's view right after a call: "Call ended" and a New call button. */
  justEnded?: boolean;
  onNewCall?: () => void;
}) {
  const [state, setState] = useState<PollState>({ phase: "waiting", session: null, elapsedMs: 0, error: null });
  const [round, setRound] = useState(0); // bumped by "Look again"
  const [transcript, setTranscript] = useState<TranscriptRow[] | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [tab, setTab] = useState("summary");
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTranscript(null);
    setReplay(null);
    setReplayError(null);
    pollSession(() => rest.session(sessionId), (s) => !cancelled && setState(s), { cancelled: () => cancelled }).then((final) => {
      if (cancelled || final.phase !== "ready" || !final.session.store_transcript) return;
      rest.replay(sessionId).then(
        (r) => {
          if (cancelled) return;
          setReplay(r);
          setTranscript(transcriptRows(r.segments));
        },
        (e) => !cancelled && setReplayError(String(e)),
      );
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, round]);

  const session = state.phase === "gone" ? null : state.session;
  const title = session ? heading(session, companies) : "Call";
  const stored = Boolean(session?.store_transcript);
  const outputs = state.phase === "ready" ? outputsInOrder(state.session) : [];

  const summary = (
    <div className="ui-stack">
      {state.phase === "ready" &&
        outputs.map(([kind, text]) => (
          <Card key={kind} title={OUTPUT_TITLES[kind]} actions={(kind === "follow_up_email" || kind === "standup_report") && <CopyButton text={text} />}>
            <div className="ui-prewrap sd-output">{text}</div>
            {kind === "standup_report" && state.session.employer_id && (
              <p className="ui-field-hint sd-output-note">The commitments were added to this company's open commitments for the next standup; remove any under Work, Facts.</p>
            )}
          </Card>
        ))}
      {state.phase === "ready" && outputs.length === 0 && (
        <EmptyState icon="✓" title="No summary">
          This call was saved with its transcript only.
        </EmptyState>
      )}
      {state.phase === "waiting" && (
        <Card>
          <div className="ui-row-inline waiting-line" role="status">
            <Spinner size="sm" label={null} />
            <span>{waitingText(state, stored)}</span>
          </div>
          <div className="ui-stack waiting-skeleton" aria-hidden="true">
            <Skeleton width="92%" height={12} />
            <Skeleton width="80%" height={12} />
            <Skeleton width="64%" height={12} />
          </div>
          {state.error && <p className="ui-field-hint">Still trying: {state.error}</p>}
        </Card>
      )}
      {state.phase === "timeout" && (
        <Banner tone="warning" action={<Button size="sm" onClick={() => setRound((r) => r + 1)}>Look again</Button>}>
          {waitingText(state, stored)}
        </Banner>
      )}
      {state.phase === "gone" && <Banner tone="neutral">{waitingText(state, stored)}</Banner>}
    </div>
  );

  const replayPanel = replayError ? (
    <Banner tone="danger" title="Could not load the replay" action={<Button size="sm" onClick={() => setRound((r) => r + 1)}>Retry</Button>}>
      {replayError}
    </Banner>
  ) : replay ? (
    <CallReplay replay={replay} />
  ) : (
    <SkeletonList rows={3} label="Loading the replay" />
  );

  const transcriptPanel = transcript === null ? (
    replayError ? replayPanel : <SkeletonList rows={4} label="Loading the transcript" />
  ) : transcript.length === 0 ? (
    <EmptyState icon="…" title="Nothing was transcribed" />
  ) : (
    <ol className="sd-transcript">
      {transcript.map((r) => (
        <li key={r.key}>
          <span className="sd-at ui-num">{r.at}</span>
          <span className={r.who === "You" ? "sd-who is-you" : "sd-who"}>{r.who}</span>
          <span className="sd-text">{r.text}</span>
        </li>
      ))}
    </ol>
  );

  return (
    <article className="session-detail">
      <PageHeader
        back={
          onClose && !justEnded ? (
            <Button variant="ghost" size="sm" onClick={onClose}>
              ← History
            </Button>
          ) : undefined
        }
        title={justEnded ? `Call ended${session ? ` · ${title}` : ""}` : title}
        meta={session ? facts(session, stored) : <Skeleton width={260} height={12} />}
        actions={
          <>
            {justEnded && onNewCall && (
              <Button variant="primary" onClick={onNewCall}>
                New call
              </Button>
            )}
            {onDeleted && <Menu label="More actions" trigger="⋯" items={[{ label: "Delete call…", danger: true, onSelect: () => setDeleting(true) }]} />}
          </>
        }
      />
      {state.phase === "ready" && stored ? (
        <Tabs
          label="Call"
          value={tab}
          onChange={setTab}
          items={[
            { value: "summary", label: "Summary", content: summary },
            { value: "replay", label: "Replay", content: replayPanel },
            { value: "transcript", label: "Transcript", content: transcriptPanel },
          ]}
        />
      ) : (
        summary
      )}
      {state.phase === "ready" && session && !stored && <p className="ui-field-hint detail-note">No transcript or replay: the transcript was not stored for this call.</p>}
      {onDeleted && (
        <ConfirmDialog
          open={deleting}
          onOpenChange={setDeleting}
          title="Delete this call?"
          confirmLabel="Delete call"
          onConfirm={async () => {
            await rest.deleteSession(sessionId);
            onDeleted();
          }}
        >
          Its transcript and outputs are deleted. Facts already approved into your memory stay.
        </ConfirmDialog>
      )}
    </article>
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
    <div className={compact ? "ov ov-replay is-compact" : "ov ov-replay"} style={{ "--ov-alpha": 1 } as CSSProperties}>
      <header className="ov-head">
        <span className="ov-status ov-status-off">
          <span className="ov-status-icon" aria-hidden="true">
            ○
          </span>
          Replay
        </span>
        <span className="ov-replay-count">
          {replay.questions.length} suggestion{replay.questions.length === 1 ? "" : "s"}
        </span>
        <span className="ov-spacer" />
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
        showMe
      />
    </div>
  );
}

function heading(s: Detail, companies: Record<string, string>): string {
  const mode = s.mode ? (MODE_LABEL[s.mode] ?? s.mode) : "Call";
  const company = s.employer_id ? companies[s.employer_id] : undefined;
  return [mode, company, s.title].filter(Boolean).join(" · ");
}

function facts(s: Detail, stored: boolean): string {
  const parts: string[] = [];
  if (s.started_at) parts.push(when(s.started_at));
  if (s.billed_seconds != null && s.ended_at) parts.push(`${Math.max(1, Math.round(s.billed_seconds / 60))} min`);
  parts.push(`${s.questions} question${s.questions === 1 ? "" : "s"}`);
  if (s.platform) parts.push(PLATFORM[s.platform] ?? s.platform);
  if (stored) parts.push(audioStatus(s));
  return parts.join(" · ");
}

const PLATFORM: Record<string, string> = { zoom: "Zoom", meet: "Google Meet", teams: "Microsoft Teams", other: "Other app" };

function audioStatus(s: Detail): string {
  if (s.me_audio_status === "live") return "your side transcribed live";
  if (s.me_audio_status === "transcribed") return "your side transcribed, audio deleted";
  return "your side not transcribed";
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const toast = useToast();
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
    toast({ tone: "success", text: "Copied" });
  }, [text, toast]);
  return (
    <Button size="sm" onClick={copy}>
      {label}
    </Button>
  );
}
