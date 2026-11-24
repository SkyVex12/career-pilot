import { openAsk } from "./Ask";
// One call after it ended: the Call screen right after it ends, and a past call opened from
// History. Polls compact status for up to 5 minutes until the outputs are ready, then shows
// them; when the transcript was stored, also the call as the overlay showed it (Replay) and the
// plain transcript (Transcript).
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { rest, POLL_READ, type Replay, type SessionDetail as Detail } from "./api";
import { useDataActivity } from "./usePolling";
import { ConversationView, useViewPrefs, ViewControls } from "./Overlay";
import { fromReplay } from "./session";
import { when } from "./shared";
import { OUTPUT_TITLES, isSummaryNotice, outputsInOrder, pollSession, transcriptRows, waitingText, type PollState, type TranscriptRow } from "./postcall";
import { Banner, Button, ConfirmDialog, EmptyState, Field, Menu, PageHeader, Skeleton, SkeletonList, Spinner, Tabs, Textarea, useToast } from "./ui";
import { MODE_LABEL } from "./calls";
import { Icon } from "./ui/Icon";

export type TranscriptFocus = { sessionId: string; startMs?: number; channel?: "me" | "them"; segId?: number };

export function SessionDetail({
  sessionId,
  onDeleted,
  onClose,
  companies = {},
  justEnded = false,
  onNewCall,
  transcriptFocus,
}: {
  sessionId: string;
  onDeleted?: () => void;
  onClose?: () => void;
  companies?: Record<string, string>;
  /** The Call screen's view right after a call: "Call ended" and a New call button. */
  justEnded?: boolean;
  onNewCall?: () => void;
  /** Open a cited transcript moment only when it belongs to this session. */
  transcriptFocus?: TranscriptFocus;
}) {
  const [state, setState] = useState<PollState>({ phase: "waiting", session: null, elapsedMs: 0, error: null });
  const [round, setRound] = useState(0); // bumped by "Look again"
  const [replayRound, setReplayRound] = useState(0);
  const active = useDataActivity();
  const [transcript, setTranscript] = useState<TranscriptRow[] | null>(null);
  const [replay, setReplay] = useState<Replay | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [tab, setTab] = useState("primary");
  const [deleting, setDeleting] = useState(false);
  const [leaving, setLeaving] = useState<(() => void) | null>(null);
  const [notes, setNotes] = useState<{ id: string; loaded: boolean; saved: string; draft: string; revision?: number }>({ id: sessionId, loaded: false, saved: "", draft: "" });
  const [notesReview, setNotesReview] = useState<string | null>(null);
  const [savingNotes, setSavingNotes] = useState(false);
  const [notesError, setNotesError] = useState<string | null>(null);
  const [notesSaved, setNotesSaved] = useState(false);
  const focusedRow = useRef<HTMLLIElement>(null);
  const currentId = useRef(sessionId);
  currentId.current = sessionId;

  useEffect(() => {
    setTab("primary");
    setLeaving(null);
    setNotes({ id: sessionId, loaded: false, saved: "", draft: "" });
    setNotesError(null);
    setNotesReview(null);
    setNotesSaved(false);
    setSavingNotes(false);
  }, [sessionId]);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    let last: Detail | null = null;
    pollSession(async () => {
      if (!last) return last = await rest.session(sessionId, round ? { refresh: true } : undefined);
      const status = await rest.sessionStatus(sessionId);
      return last = status.status === "ready" ? await rest.session(sessionId, POLL_READ) : { ...last, status: status.status };
    }, (s) => !cancelled && setState(s), { cancelled: () => cancelled });
    return () => { cancelled = true; };
  }, [sessionId, round, active]);

  useEffect(() => {
    setState({ phase: "waiting", session: null, elapsedMs: 0, error: null });
    setTranscript(null);
    setReplay(null);
    setReplayError(null);
  }, [sessionId]);

  const session = state.phase === "gone" ? null : state.session;
  const title = session ? heading(session, companies) : "Call";
  const stored = Boolean(session?.store_transcript);
  const outputs = state.phase === "ready" ? outputsInOrder(state.session) : [];
  const matchingFocus = transcriptFocus?.sessionId === sessionId ? transcriptFocus : undefined;
  const wantsReplay = session?.id === sessionId && stored && state.phase === "ready" && (tab === "transcript" || tab === "replay" || !!matchingFocus);
  useEffect(() => {
    if (!active || !wantsReplay) return;
    let cancelled = false;
    setReplayError(null);
    void rest.replay(sessionId, replayRound ? { refresh: true } : undefined).then(r => {
      if (!cancelled) { setReplay(r); setTranscript(transcriptRows(r.segments)); }
    }, e => { if (!cancelled) setReplayError(String(e)); });
    return () => { cancelled = true; };
  }, [sessionId, wantsReplay, active, replayRound]);
  const focusKey = useMemo(() => {
    if (!matchingFocus || replay?.session_id !== sessionId) return null;
    if (matchingFocus.channel && Number.isInteger(matchingFocus.segId)) {
      const exact = replay.segments.find(s => s.channel === matchingFocus.channel && s.seg_id === matchingFocus.segId);
      if (exact) return `${exact.channel}:${exact.seg_id}`;
    }
    const at = matchingFocus.startMs;
    if (at === undefined || !Number.isFinite(at) || at < 0) return null;
    const containing = replay.segments.find(s => s.start_ms <= at && at <= s.end_ms);
    const nearest = containing ?? [...replay.segments].sort((a,b) => Math.abs(a.start_ms-at)-Math.abs(b.start_ms-at))[0];
    if (!nearest || (!containing && Math.abs(nearest.start_ms-at)>2000)) return null;
    return `${nearest.channel}:${nearest.seg_id}`;
  }, [sessionId, replay, matchingFocus?.sessionId, matchingFocus?.channel, matchingFocus?.segId, matchingFocus?.startMs]);
  useEffect(() => {
    if (matchingFocus && stored && state.phase === "ready") setTab("transcript");
  }, [sessionId, matchingFocus?.sessionId, matchingFocus?.channel, matchingFocus?.segId, matchingFocus?.startMs, stored, state.phase]);
  useEffect(() => {
    if (tab === "transcript" && focusKey) focusedRow.current?.scrollIntoView({ block:"center", behavior:"auto" });
  }, [tab, focusKey, transcript]);


  useEffect(() => {
    if (session?.id !== sessionId || typeof session.notes !== "string") return;
    const saved = session.notes;
    setNotes((current) => current.id === sessionId && current.loaded && current.draft !== current.saved ? current : { id: sessionId, loaded: true, saved, draft: saved, revision: session.revision });
  }, [sessionId, session?.id, session?.notes, session?.revision]);

  const leave = (action: () => void) => {
    if (notes.draft !== notes.saved) setLeaving(() => action);
    else action();
  };

  const saveNotes = async () => {
    const draft = notes.draft;
    setSavingNotes(true);
    setNotesError(null);
    setNotesSaved(false);
    try {
      const saved = await rest.saveSessionNotes(sessionId, draft, notes.revision);
      if (currentId.current !== sessionId) return;
      setNotes((current) => ({ ...current, saved: saved.notes, revision: saved.revision ?? current.revision }));
      setNotesSaved(true);
      setNotesReview(null);
    } catch (e) {
      if (currentId.current === sessionId) setNotesError(String(e));
    } finally {
      if (currentId.current === sessionId) setSavingNotes(false);
    }
  };

  const reloadNotes = async () => {
    setSavingNotes(true);
    try {
      const latest = await rest.session(sessionId, { refresh: true });
      if (currentId.current !== sessionId) return;
      setNotes(current => ({ ...current, saved: latest.notes ?? "", revision: latest.revision }));
      setNotesReview(latest.notes ?? "");
      setNotesError(null);
    } catch (e) { if (currentId.current === sessionId) setNotesError(String(e)); }
    finally { if (currentId.current === sessionId) setSavingNotes(false); }
  };

  const pendingOutput = (
    <div className="sd-reading">
      {state.phase === "ready" && outputs.length === 0 && (
        <EmptyState icon={<Icon name="document" size={28} />} title="No summary">
          {stored ? "The transcript and replay are available in their tabs." : "No summary or transcript was stored for this call."}
        </EmptyState>
      )}
      {state.phase === "waiting" && (
        <div>
          <div className="ui-row-inline waiting-line" role="status"><Spinner size="sm" label={null} /><span>{waitingText(state, stored)}</span></div>
          <div className="ui-stack waiting-skeleton" aria-hidden="true"><Skeleton width="92%" height={12} /><Skeleton width="80%" height={12} /><Skeleton width="64%" height={12} /></div>
          {state.error && <p className="ui-field-hint">Still trying: {state.error}</p>}
        </div>
      )}
      {state.phase === "timeout" && <Banner tone="warning" action={<Button size="sm" onClick={() => setRound((r) => r + 1)}>Look again</Button>}>{waitingText(state, stored)}</Banner>}
      {state.phase === "gone" && <Banner tone="neutral">{waitingText(state, stored)}</Banner>}
    </div>
  );

  const notesPanel = (
    <div className="sd-reading sd-notes">
      {notes.loaded ? <>
        <div className="sd-output-toolbar">
          <span className="ui-spacer" />
          <span className="ui-field-hint" role="status">{savingNotes ? "Saving…" : notes.draft !== notes.saved ? "Unsaved changes" : notesSaved ? "Saved" : ""}</span>
          <Button size="sm" disabled={savingNotes || notes.draft === notes.saved} onClick={() => { setNotes((n) => ({ ...n, draft: n.saved })); setNotesError(null); setNotesSaved(false); }}>Discard</Button>
          <Button size="sm" variant="primary" loading={savingNotes} disabled={notes.draft === notes.saved || notesError?.includes("HTTP 409")} onClick={saveNotes}>Save notes</Button>
        </div>
        <Field label="Call notes" hint="Private notes saved with this call. The transcript setting does not affect these notes." hintMode="tooltip">
          <Textarea rows={12} maxLength={20000} value={notes.draft} disabled={savingNotes} placeholder="Add reminders, questions or next steps…" onChange={(e) => { setNotes((n) => ({ ...n, draft: e.target.value })); setNotesSaved(false); }} />
        </Field>
        {notesError && <Banner tone="danger" title="Could not save notes" action={notesError.includes("HTTP 409") ? <Button size="sm" onClick={reloadNotes} loading={savingNotes}>Load latest notes</Button> : <Button size="sm" onClick={saveNotes} loading={savingNotes}>Retry</Button>}>{notesError}</Banner>}
        {notesReview !== null && <Banner tone="warning" title="Latest saved notes"><p>Your draft is preserved above. Review the saved version before saving again.</p><div className="feature-prose">{notesReview || "No saved notes."}</div></Banner>}
      </> : <Banner tone="neutral">Notes are unavailable with this server version.</Banner>}
    </div>
  );

  const outputTabs = outputs.map(([kind, text], index) => ({
    value: index === 0 ? "primary" : kind,
    label: kind === "follow_up_email" ? "Follow-up" : OUTPUT_TITLES[kind],
    content: kind === "summary" && isSummaryNotice(text) ? <section className="sd-reading sd-unavailable">
      <Icon name="document" size={28} />
      <div><h2>{stored ? "No conversation was captured" : "No written summary"}</h2>
        <p>{stored ? "CoRuby did not receive a transcript for this call. Check microphone and call audio before your next call." : "The transcript wasn't saved, so a written summary isn't available. You can still keep your own notes."}</p>
        <Button size="sm" onClick={() => setTab("notes")}>Add call notes</Button>
      </div>
    </section> : <section className="sd-reading">
      <div className="sd-output-toolbar"><CopyButton text={text} /></div>
      <ReviewText text={text} />
      {kind === "standup_report" && session?.employer_id && <p className="ui-field-hint sd-output-note">Commitments are saved under Work context → Saved facts.</p>}
      {!stored && index === 0 && <p className="ui-field-hint detail-note">The transcript was not stored, so transcript and replay are unavailable.</p>}
    </section>,
  }));

  const replayPanel = replayError ? (
    <Banner tone="danger" title="Could not load the replay" action={<Button size="sm" onClick={() => setReplayRound((r) => r + 1)}>Retry</Button>}>
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
    <EmptyState icon={<Icon name="mic" size={28} />} title="Nothing was transcribed" />
  ) : (
    <div className="sd-transcript-panel">
      <div className="sd-output-toolbar">
        {matchingFocus && <span className="ui-field-hint">{focusKey ? "Supporting transcript moment highlighted." : "The referenced transcript moment is unavailable."}</span>}
        <CopyButton text={transcript.map((r) => r.at + " " + r.who + ": " + r.text).join("\n")} />
      </div>
      <ol className="sd-transcript">
        {transcript.map((r) => (
          <li key={r.key} ref={r.key === focusKey ? focusedRow : undefined} aria-current={r.key === focusKey ? "location" : undefined}>
            <span className="sd-at ui-num">{r.at}</span>
            <span className={r.who === "You" ? "sd-who is-you" : "sd-who"}>{r.who}</span>
            <span className="sd-text">{r.key === focusKey ? <mark style={{ background:"rgba(183, 38, 75, 0.16)", color:"inherit", borderRadius:3 }}>{r.text}</mark> : r.text}</span>
          </li>
        ))}
      </ol>
    </div>
  );

  return (
    <article className="session-detail">
      <PageHeader
        back={
          onClose && !justEnded ? (
            <Button variant="ghost" size="sm" onClick={() => leave(onClose)}>
              ← Past calls
            </Button>
          ) : undefined
        }
        title={justEnded ? `Call ended${session ? ` · ${title}` : ""}` : title}
        meta={session ? facts(session) : <Skeleton width={260} height={12} />}
        actions={
          <>
            <Button onClick={() => openAsk({ kind: "call", id: sessionId })}>Ask about this call</Button>
            {justEnded && onNewCall && (
              <Button variant="primary" onClick={() => leave(onNewCall)}>
                New call
              </Button>
            )}
            {onDeleted && <Menu label="More actions" trigger="⋯" items={[{ label: "Delete call…", danger: true, onSelect: () => setDeleting(true) }]} />}
          </>
        }
      />
      {state.phase === "ready" ? (
        <Tabs label="Call review" value={tab} onChange={setTab} items={[
          ...(outputTabs.length ? outputTabs : [{ value: "primary", label: "Summary", content: pendingOutput }]),
          ...(stored ? [{ value: "transcript", label: "Transcript", content: transcriptPanel }, { value: "replay", label: "Replay", content: replayPanel }] : []),
          { value: "notes", label: "Notes", content: notesPanel },
        ]} />
      ) : pendingOutput}
      <ConfirmDialog open={leaving !== null} onOpenChange={(open) => !open && setLeaving(null)} title="Discard unsaved notes?" confirmLabel="Discard notes" onConfirm={async () => { leaving?.(); setLeaving(null); }}>Your changes have not been saved.</ConfirmDialog>
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
          Its notes, transcript and outputs are deleted. Facts already approved into your memory stay.
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

function facts(s: Detail): string {
  const parts: string[] = [];
  if (s.started_at) parts.push(when(s.started_at));
  if (s.billed_seconds != null && s.ended_at) parts.push(`${Math.max(1, Math.round(s.billed_seconds / 60))} min`);
  return parts.join(" · ");
}

/** Format the stored output without interpreting HTML or inventing section content. */
function ReviewText({ text }: { text: string }) {
  return <div className="sd-output">{text.split(/\n\s*\n/).filter(Boolean).map((block, index) => {
    const lines = block.trim().split("\n");
    const heading = lines[0].match(/^(?:#{1,3}\s+(.+)|\*\*(.+?)\*\*:?)$/);
    return <section key={index}>
      {heading && <h3>{heading[1] ?? heading[2]}</h3>}
      {lines.slice(heading ? 1 : 0).map((line, at) => <p className="sd-output-line" key={at}>{line}</p>)}
    </section>;
  })}</div>;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const toast = useToast();
  const copy = useCallback(async () => {
    try {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const focused = document.activeElement;
        const area = document.createElement("textarea");
        area.value = text;
        area.readOnly = true;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        try {
          area.select();
          if (!document.execCommand("copy")) throw new Error("Copy was refused");
        } finally {
          area.remove();
          if (focused instanceof HTMLElement) focused.focus({ preventScroll: true });
        }
      }
      toast({ tone: "success", text: "Copied" });
    } catch {
      toast({ tone: "error", text: "Could not copy. Select the text and copy it manually." });
    }
  }, [text, toast]);
  return (
    <Button size="sm" onClick={copy}>
      {label}
    </Button>
  );
}
