import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Badge, Banner, Button, Card, ConfirmDialog, Field, Input, Select, SkeletonList, Spinner, Textarea } from "./ui";
import "./feature-pages.css";
import { PracticeAudio, audioBase64, registerPracticePause } from "./practiceAudio";
export type PracticeEntry = { application_id?: string; source_question?: { session_id: string; channel: "them" | "me"; seg_id: number }; topic?: string; n: number };
export const openPractice = (entry: Omit<PracticeEntry, "n">) => window.dispatchEvent(new CustomEvent("copilot:open-practice", { detail: entry }));
type Feedback = { rubric_version: string; dimensions: Record<string, number | { score: number; reason?: string }>; strengths: string[]; improvements: string[]; excerpts: string[]; uncertainty: string[]; suggested_wording: string };
type Question = { id: string; text: string; kind: string; source?: { id: string; kind: string; label?: string; start_ms?: number }; hint?: string; sample?: string; saved_for_later: boolean; assistance: string[]; attempts: { id: string; answer: string; assistance: string[]; feedback: Feedback | null; comparison?: { comparable: boolean; note: string; summary?: string; improvements?: string[]; remaining_gaps?: string[]; recurring_gaps?: string[]; evidence?: { previous: string[]; current: string[] }; dimensions: Record<string, { previous: number; current: number }> } }[]; comparison?: unknown };
type Session = { id: string; status: string; setup: { topic: string; application_id?: string }; current_question_id: string; questions: Question[]; elapsed_seconds: number; sources: { id: string; label: string }[]; spoken_language: string; voice_available?: boolean; speech_available?: boolean; error?: string; retention_notice?: string; rubric?: Record<string, string>; score_scale?: Record<string, string> };
const request = <T,>(method: string, path: string, body?: unknown) => invoke<T>("api_call", { method, path, body: body ?? null });
const base = "/practice/sessions";
export function Practice({ active, running, entry }: { active: boolean; running: boolean; entry?: PracticeEntry | null }) {
  const [loadingSession, setLoadingSession] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]), [current, setCurrent] = useState<Session | null>(null);
  const voiceAvailable = current?.voice_available === true, speechAvailable = current?.speech_available === true;
  const [topic, setTopic] = useState("General interview preparation"), [application, setApplication] = useState(""), [kind, setKind] = useState("behavioral"), [duration, setDuration] = useState(15);
  const [applications, setApplications] = useState<{ id: string; label: string }[]>([]);
  const [draft, setDraft] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null), [recording, setRecording] = useState(false), [speaking, setSpeaking] = useState(false), [deleting, setDeleting] = useState(false), [consent, setConsent] = useState(false);
  const [context, setContext] = useState<{ sources: { id: string; label: string; excerpt: string }[]; notice: string } | null>(null);
  const [originalSource, setOriginalSource] = useState<{ content: string; start_ms?: number } | null>(null);
  const [voice, setVoice] = useState(true);
  const [sourceQuestion, setSourceQuestion] = useState(entry?.source_question);
  const spoken = useRef<string | null>(null);
  const [review, setReview] = useState<string | null>(null);
  const audio = useRef(new PracticeAudio()), state = useRef(current), alive = useRef(true), mediaGeneration = useRef(0);
  state.current = current;
  const availability = useRef({ active, running }); availability.current = { active, running };
  const generation = useRef(0), sourceGeneration = useRef(0);
  const valid = (ticket: number) => alive.current && generation.current === ticket;
  const invalidate = () => { generation.current++; sourceGeneration.current++; setBusy(false); setDeleting(false); setLoadingSession(false); };
  const pending = useRef<{ payload: string; id: string } | null>(null);
  const recordingRetry = useRef<{ request_id: string; content_type: string; audio_base64: string } | null>(null);
  const stop = () => { mediaGeneration.current++; audio.current.stop(); setRecording(false); setSpeaking(false); };
  const loadList = () => { const ticket = generation.current; return request<{ sessions: Session[] }>("GET", base).then(v => { if (valid(ticket)) setSessions(v.sessions); }); };
  const action = async (name: string, questionId?: string, answer?: string) => {
    const session = state.current; if (!session) return; const ticket = generation.current;
    stop(); setBusy(true); setError(null);
    const body = { action: name, ...(questionId ? { question_id: questionId } : {}), ...(answer !== undefined ? { answer } : {}) };
    const payload = JSON.stringify(body);
    if (pending.current?.payload !== payload) pending.current = { payload, id: crypto.randomUUID() };
    try { const next = await request<Session>("POST", `${base}/${session.id}/actions`, { ...body, request_id: pending.current.id }); if (!valid(ticket)) return; setCurrent(next); if (name === "answer") setDraft(value => value === answer ? "" : value); setReview(null); pending.current = null; void loadList(); }
    catch (e) { if (valid(ticket)) setError(String(e)); throw e; }
    finally { if (valid(ticket)) setBusy(false); }
  };
  const act = (name: string, id?: string, answer?: string) => void action(name, id, answer).catch(() => {});
  useEffect(() => {
    alive.current = true;
    void loadList().catch(e => setError(String(e)));
    void request<{ options: { id: string; label: string }[] }>("GET", "/ask/context-options?kind=application").then(v => { if (alive.current) setApplications(v.options); }).catch(e => setError(String(e)));
    audio.current.onPlaybackEnd = () => setSpeaking(false);
    audio.current.onInterrupted = message => { setRecording(false); setError(message); };
    const unregister = registerPracticePause(async () => { stop(); if (state.current && !["paused", "finished", "failed"].includes(state.current.status)) await action("pause"); });
    return () => { alive.current = false; generation.current++; sourceGeneration.current++; unregister(); audio.current.stop(); mediaGeneration.current++; const s = state.current; if (s && !["paused", "finished", "failed"].includes(s.status)) void request("POST", `${base}/${s.id}/actions`, { request_id: crypto.randomUUID(), action: "pause" }).catch(() => {}); };
  }, []);
  useEffect(() => { if (!active || running) { stop(); if (state.current && !["paused", "finished", "failed"].includes(state.current.status)) act("pause"); } }, [active, running]);
  useEffect(() => {
    if (!entry) return;
    invalidate(); const ticket = generation.current;
    let cancelled = false;
    void (async () => {
      stop();
      if (state.current && !["paused", "finished", "failed"].includes(state.current.status)) { try { await action("pause"); } catch { return; } }
      if (cancelled || !valid(ticket)) return;
      setCurrent(null); setApplication(entry.application_id ?? ""); setSourceQuestion(entry.source_question); setTopic(entry.topic ?? "Practice a saved interview question"); setDraft(""); setReview(null); setOriginalSource(null); recordingRetry.current = null; pending.current = null;
    })();
    return () => { cancelled = true; };
  }, [entry?.n]);
  useEffect(() => {
    if (!active || !current || !["preparing", "processing"].includes(current.status)) return;
    const ticket = generation.current; let stopped = false; const timer = setInterval(() => void request<Session>("GET", `${base}/${current.id}`).then(v => { if (!stopped && valid(ticket)) setCurrent(v); }).catch(e => { if (!stopped && valid(ticket)) setError(String(e)); }), 1200);
    return () => { stopped = true; clearInterval(timer); };
  }, [active, current?.id, current?.status]);
  useEffect(() => {
    if (!active || current) return;
    let cancelled = false; setContext(null);
    void request<{ sources: { id: string; label: string; excerpt: string }[]; notice: string }>("GET", `/practice/context${application ? `?application_id=${encodeURIComponent(application)}` : ""}`).then(v => { if (!cancelled) setContext(v); }).catch(e => { if (!cancelled) setError(`Context preview unavailable: ${String(e)}`); });
    return () => { cancelled = true; };
  }, [active, application, current?.id]);
  const start = async () => {
    if (!consent || running || busy || loadingSession || !topic.trim() || !Number.isFinite(duration) || duration < 3 || duration > 60) return; stop(); const ticket = generation.current; setBusy(true); setError(null);
    const setup = { topic, application_id: application || null, interview_type: kind, duration_minutes: duration, retain_audio: false, ...(sourceQuestion ? { source_question: sourceQuestion } : {}) };
    const payload = JSON.stringify(setup); if (pending.current?.payload !== payload) pending.current = { payload, id: crypto.randomUUID() };
    try {
      let value = await request<Session>("POST", base, { ...setup, request_id: pending.current.id });
      if (!valid(ticket)) return;
      setCurrent(value); pending.current = null;
      if ((!availability.current.active || availability.current.running) && !["paused", "finished", "failed"].includes(value.status)) {
        value = await request<Session>("POST", `${base}/${value.id}/actions`, { action: "pause", request_id: crypto.randomUUID() });
        if (!valid(ticket)) return;
        setCurrent(value);
      }
      void loadList();
    } catch (e) { if (valid(ticket)) setError(String(e)); } finally { if (valid(ticket)) setBusy(false); }
  };
  const latestFeedback = current?.questions.flatMap(q => q.attempts.filter(a => a.feedback).map(a => ({ question: q, attempt: a }))).slice(-1)[0];
  const question = current?.questions.find(q => q.id === (review ?? current.current_question_id));
  useEffect(() => { sourceGeneration.current++; setOriginalSource(null); }, [current?.id, question?.id]);
  const speak = async () => {
    if (!active || !current || !question || running || current.speech_available !== true) return; stop(); const generation = mediaGeneration.current; setSpeaking(true);
    try { const data = await request<{ audio_base64: string; content_type: string }>("GET", `${base}/${current.id}/questions/${question.id}/speech-json`); if (generation === mediaGeneration.current) await audio.current.speak(data.audio_base64, data.content_type); }
    catch (e) { if (alive.current && generation === mediaGeneration.current) { setSpeaking(false); setError(`Question playback unavailable. You can read it below. ${String(e)}`); } }
  };
  useEffect(() => {
    if (!active || running || !voice || !speechAvailable || !current || current.status !== "answering" || review || !question) return;
    const key = `${current.id}/${question.id}`;
    if (spoken.current === key) return;
    spoken.current = key; void speak();
  }, [active, running, voice, speechAvailable, current?.status, current?.id, question?.id, review]);
  const transcribe = async () => {
    if (!active || running || !current || current.voice_available !== true || !recordingRetry.current) return; const sessionId = current.id; const generation = mediaGeneration.current; setBusy(true);
    try { const value = await request<{ text: string; silence: boolean }>("POST", `${base}/${current.id}/transcribe-json`, recordingRetry.current); if (!alive.current || generation !== mediaGeneration.current || state.current?.id !== sessionId) return; setDraft(d => `${d}${d ? " " : ""}${value.text}`); recordingRetry.current = null; if (value.silence) setError("No speech was recognized. Try again or type your answer."); }
    catch (e) { if (!alive.current || generation !== mediaGeneration.current) return; setError(`Transcription failed. Retry the same recording or use text. ${String(e)}`); } finally { if (alive.current && generation === mediaGeneration.current) setBusy(false); }
  };
  const doneRecording = async () => { const mediaTicket = mediaGeneration.current; const blob = await audio.current.finish(); if (!alive.current || mediaTicket !== mediaGeneration.current) return; setRecording(false); if (!blob?.size) { setError("No microphone audio was captured."); return; } const encoded = await audioBase64(blob); if (!alive.current || mediaTicket !== mediaGeneration.current) return; recordingRetry.current = { request_id: crypto.randomUUID(), content_type: blob.type, audio_base64: encoded }; await transcribe(); };
  const choose = async (id: string) => {
    invalidate(); const ticket = generation.current; stop();
    if (state.current && !["paused", "finished", "failed"].includes(state.current.status)) { try { await action("pause"); } catch { return; } }
    if (!valid(ticket)) return;
    setDraft(""); setReview(null); setOriginalSource(null); recordingRetry.current = null; pending.current = null; setCurrent(null);
    if (!id) { setSourceQuestion(undefined); return; }
    setLoadingSession(true);
    try { const value = await request<Session>("GET", `${base}/${id}`); if (valid(ticket)) setCurrent(value); } catch (e) { if (valid(ticket)) setError(String(e)); } finally { if (valid(ticket)) setLoadingSession(false); }
  };
  const inspect = async (source: NonNullable<Question["source"]>) => {
    const ticket = generation.current, sourceTicket = ++sourceGeneration.current;
    try { const value = await request<{content:string;start_ms?:number}>("GET", `/ask/sources/${encodeURIComponent(source.kind)}/${encodeURIComponent(source.id)}`); if (valid(ticket) && sourceTicket === sourceGeneration.current) setOriginalSource(value); }
    catch (e) { if (valid(ticket) && sourceTicket === sourceGeneration.current) setError(`Original transcript unavailable: ${String(e)}`); }
  };
  const remove = async () => {
    const ticket = generation.current, id = current?.id; stop();
    try { if (id) await request("DELETE", `${base}/${id}`); if (!valid(ticket)) return; invalidate(); setCurrent(null); setDraft(""); void loadList(); }
    catch (e) { if (valid(ticket)) setError(String(e)); }
  };
  const statusLabel = recording ? "Listening" : speaking ? "Speaking" : ({ answering: "Your turn", preparing: "Preparing a question", processing: "Reviewing your answer", paused: "Paused", finished: "Practice complete", failed: "Needs attention" }[current?.status ?? ""] ?? current?.status);
  return <><div className="practice-workspace feature-page">
    {!current && <header className="feature-heading"><div><h2>Interview practice</h2><p>Rehearse a focused conversation and get feedback on each answer.</p></div></header>}
    {running && <Banner tone="warning">Finish the real call before starting or resuming practice.</Banner>}
    {error && <Banner tone="warning">{error}</Banner>}
    <div className="feature-toolbar practice-toolbar"><Field label="Practice history"><Select aria-label="Practice history" value={current?.id ?? ""} onChange={e => void choose(e.target.value)}><option value="">New practice</option>{sessions.map(s => <option key={s.id} value={s.id}>{s.setup.topic} — {s.status.replace(/_/g, " ")}</option>)}</Select></Field>{current && <Button onClick={() => void choose("")}>New practice</Button>}</div>
    {loadingSession ? <SkeletonList rows={3} label="Loading practice" /> : !current ? <div className="practice-setup"><div className="practice-setup-main"><div className="feature-form-grid">
      <Field label="Application"><Select value={application} onChange={e => setApplication(e.target.value)}><option value="">Current profile only</option>{applications.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}</Select></Field>
      <Field label="Role or topic"><Input value={topic} onChange={e => setTopic(e.target.value)} maxLength={500} /></Field>
      <Field label="Interview type"><Select value={kind} onChange={e => setKind(e.target.value)}><option value="recruiter">Recruiter screen</option><option value="behavioral">Behavioral</option><option value="technical">Technical discussion</option><option value="system_design">System design</option></Select></Field>
      <Field label="Practice length (minutes)" hint="Between 3 and 60 minutes."><Input type="number" min={3} max={60} value={duration} onChange={e => setDuration(Number(e.target.value))} /></Field>
      </div><label className="feature-check"><input type="checkbox" checked={voice} onChange={e => setVoice(e.target.checked)} /> Read interviewer questions aloud when speech is available</label>

      </div><aside className="practice-setup-aside"><h3>What to expect</h3><ol className="practice-steps"><li><strong>A question at a time</strong><span>Based on your profile and selected application.</span></li><li><strong>Room to rehearse</strong><span>Type your answer, or use your microphone when available.</span></li><li><strong>Useful feedback</strong><span>Review what worked and try the question again.</span></li></ol><p className="ui-field-hint">Your saved call language applies. Practice stays separate from your resume and real calls.</p>
      {context && <details><summary>Review context ({context.sources.length} sources)</summary><p>{context.notice}</p>{context.sources.map(s => <section key={s.id}><strong>{s.label}</strong><p>{s.excerpt}</p></section>)}</details>}
      </aside><footer className="practice-setup-footer"><label className="feature-check"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} /> Save practice transcripts and feedback until I delete them. Raw microphone audio is never retained.</label>
      <Button variant="primary" loading={busy} disabled={!consent || running || !topic.trim() || !Number.isFinite(duration) || duration < 3 || duration > 60} onClick={() => void start()}>Start practice</Button>
    </footer></div> : <>
      <div className="practice-session-head"><div><h2>{current.setup.topic}</h2><p>{Math.floor(current.elapsed_seconds / 60)} min practiced · {current.spoken_language}{!voiceAvailable ? " · Text practice" : ""}</p></div><Badge tone={current.status === "finished" ? "success" : current.status === "paused" ? "neutral" : "info"}><span role="status">{statusLabel}</span></Badge><Button size="sm" variant="danger-ghost" onClick={() => setDeleting(true)}>Delete practice</Button></div>
      {["preparing", "processing"].includes(current.status) && <div className="practice-progress" role="status"><Spinner size="sm" label={null} /><div><strong>{current.status === "preparing" ? "Preparing your next question" : "Reviewing your answer"}</strong><p>{current.status === "preparing" ? "Your question will appear here when it is ready." : "Your answer is saved. Feedback will appear here when it is ready."}</p></div></div>}
      {current.status === "paused" && <Banner tone="info">Your progress is saved. Resume when you are ready to continue.</Banner>}
      {current.status === "finished" && <Banner tone="success">Your practice is saved. Review your answers below, or start a new practice.</Banner>}
      <details className="feature-disclosure"><summary>Context and scoring</summary>{current.sources.map(s => <p key={s.id}>{s.label}</p>)}{Object.entries(current.rubric ?? {}).map(([k,v]) => <p key={k}>{k}: {v}</p>)}<p>{Object.entries(current.score_scale ?? {}).map(([k,v]) => `${k}: ${v}`).join(" · ")}. Scores describe this answer, not hiring prospects.</p></details>
      {current.error && <Banner tone="warning">{current.error}<Button onClick={() => act("retry_generation")}>Retry generation</Button></Banner>}
      {latestFeedback && latestFeedback.question.id !== question?.id && <details className="feature-disclosure"><summary>Feedback on your last answer</summary><p>{latestFeedback.attempt.feedback?.strengths.join(" · ")}</p><p>{latestFeedback.attempt.feedback?.improvements.join(" · ")}</p><Button onClick={() => { stop(); setReview(latestFeedback.question.id); }}>Review the attempt</Button></details>}
      {question && <Card className="practice-question"><div className="ui-stack"><p className="feature-eyebrow">Question {current.questions.indexOf(question) + 1} · {question.kind === "original" ? "Original saved question" : "Generated interviewer question"}{question.source?.label ? ` · ${question.source.label}` : ""}</p><h2 dir="auto" lang={current.spoken_language}>{question.text}</h2>{question.source && <Button onClick={() => void inspect(question.source!)}>View original transcript</Button>}{originalSource && <div><p>Original transcript{originalSource.start_ms !== undefined ? ` at ${Math.floor(originalSource.start_ms / 1000)} seconds` : ""}</p><blockquote dir="auto">{originalSource.content}</blockquote><Button onClick={() => { sourceGeneration.current++; setOriginalSource(null); }}>Close source</Button></div>}
        <div className="ui-row-inline">{speechAvailable && <Button disabled={busy || recording || running} onClick={() => void speak()}>Read / repeat aloud</Button>}{(recording || speaking) && <Button onClick={stop}>Stop audio</Button>}<Button disabled={busy} onClick={() => act("save", question.id)}>{question.saved_for_later ? "Saved for later" : "Save question"}</Button></div>

        {current.status === "answering" && !review && <>
          <div className="ui-row-inline">{voiceAvailable && <Button disabled={busy || running} onClick={() => { if (recording) void doneRecording(); else { stop(); const ticket = mediaGeneration.current; void audio.current.start().then(ok => { if (alive.current && ticket === mediaGeneration.current) setRecording(ok); }).catch(e => { if (alive.current && ticket === mediaGeneration.current) setError(String(e)); }); } }}>{recording ? "I'm done recording" : "Answer with microphone"}</Button>}{voiceAvailable && recordingRetry.current && <Button disabled={busy} onClick={() => void transcribe()}>Retry transcription</Button>}</div>
          <Field label="Your answer — review the transcript before submitting" hint={recording ? "Microphone is recording. Finish recording to review your transcript." : "Your response stays editable until you submit it."}><Textarea dir="auto" lang={current.spoken_language} value={draft} onChange={e => setDraft(e.target.value)} maxLength={16000} rows={5} /></Field>
          <div className="ui-row-inline practice-answer-actions"><Button variant="primary" loading={busy} disabled={recording || running || !draft.trim()} onClick={() => act("answer", question.id, draft)}>I'm done — submit answer</Button><Button disabled={busy || running} onClick={() => act("skip", question.id)}>Skip</Button><Button disabled={busy || running} onClick={() => act("hint", question.id)}>Hint</Button><Button disabled={busy || running} onClick={() => act("sample", question.id)}>Show sample answer</Button></div>
          {question.hint && <div className="practice-guidance"><strong>Hint</strong><p>{question.hint}</p></div>}{question.sample && <div className="practice-guidance"><strong>Sample answer</strong><p dir="auto">{question.sample}</p></div>}
        </>}
        {question.attempts.map((a, i) => <details className="practice-attempt feature-disclosure" key={a.id} open={i === question.attempts.length - 1}><summary>Attempt {i + 1} · {a.assistance.length ? "Coached" : "Independent"}</summary><div className="practice-submitted-answer"><span className="feature-eyebrow">Your answer</span><p dir="auto">{a.answer}</p></div>{a.feedback ? <PracticeFeedback feedback={a.feedback} /> : <p className="feature-loading"><Spinner size="sm" label={null} /> Feedback pending</p>}{a.comparison && <div className="practice-comparison"><h3>Progress since your last attempt</h3><p>{a.comparison.summary ?? a.comparison.note}</p><p className="ui-field-hint">{a.comparison.note}</p>{!!a.comparison.improvements?.length && <><h4>What improved</h4><ul>{a.comparison.improvements.map((text,index) => <li key={index}>{text}</li>)}</ul></>}{!!a.comparison.remaining_gaps?.length && <><h4>What to work on next</h4><ul>{a.comparison.remaining_gaps.map((text,index) => <li key={index}>{text}</li>)}</ul></>}{!!a.comparison.recurring_gaps?.length && <p>Still recurring: {a.comparison.recurring_gaps.join(" · ")}</p>}{a.comparison.evidence && <details><summary>Compare supporting excerpts</summary><p>Previous answer</p>{a.comparison.evidence.previous.map((text,index) => <blockquote key={index}>{text}</blockquote>)}<p>This answer</p>{a.comparison.evidence.current.map((text,index) => <blockquote key={index}>{text}</blockquote>)}</details>}{Object.entries(a.comparison.dimensions).map(([k,v]) => <p key={k}>{k}: {v.previous} → {v.current}</p>)}</div>}</details>)}
        {question.comparison != null && <p>Attempt comparison: {typeof question.comparison === "string" ? question.comparison : JSON.stringify(question.comparison)}</p>}
        {question.attempts.length > 0 && <Button disabled={busy || running} onClick={() => act("retry", question.id)}>Retry this question</Button>}
      </div></Card>}
      <div className="ui-row-inline practice-session-actions">{current.status !== "finished" && <><Button disabled={busy || running} onClick={() => act(current.status === "paused" ? "resume" : "pause")}>{current.status === "paused" ? "Resume" : "Pause"}</Button><Button disabled={busy} onClick={() => act("finish")}>Finish practice</Button></>}<Button onClick={() => { setReview(null); setDraft(""); }}>Current question</Button></div>
      <details className="feature-disclosure practice-question-history"><summary>Questions and previous attempts</summary>{current.questions.map(q => <p key={q.id}><Button onClick={() => { stop(); setReview(q.id); }}>{q.text}{q.saved_for_later ? " · Saved" : ""} · {q.attempts.length} attempts</Button></p>)}</details>
      <p className="feature-footnote">{current.retention_notice}</p>
    </>}
  </div><ConfirmDialog open={deleting} title="Delete practice?" confirmLabel="Delete" onOpenChange={setDeleting} onConfirm={remove}>Deletes questions, answers, feedback, and cached generated speech.</ConfirmDialog></>;
}

function PracticeFeedback({ feedback }: { feedback: Feedback }) {
  return <div className="practice-feedback">
    <h3>Feedback on your answer</h3>
    {!!Object.keys(feedback.dimensions).length && <dl className="practice-scores">{Object.entries(feedback.dimensions).map(([key, value]) => <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd><strong>{typeof value === "number" ? value : value.score}</strong>{typeof value !== "number" && value.reason && <span>{value.reason}</span>}</dd></div>)}</dl>}
    <div className="practice-feedback-columns">
      {!!feedback.strengths.length && <section><h4>What worked well</h4><ul>{feedback.strengths.map((text, index) => <li key={index}>{text}</li>)}</ul></section>}
      {!!feedback.improvements.length && <section><h4>What to improve</h4><ul>{feedback.improvements.map((text, index) => <li key={index}>{text}</li>)}</ul></section>}
    </div>
    {!!feedback.excerpts.length && <details><summary>Supporting excerpts</summary>{feedback.excerpts.map((text,index) => <blockquote key={index} dir="auto">{text}</blockquote>)}</details>}
    {!!feedback.uncertainty.length && <p className="ui-field-hint">{feedback.uncertainty.join(" · ")}</p>}
    {feedback.suggested_wording && <details className="practice-wording"><summary>Suggested wording</summary><p dir="auto">{feedback.suggested_wording}</p></details>}
    <p className="feature-footnote">Scores describe this answer, not hiring prospects. Rubric: {feedback.rubric_version}</p>
  </div>;
}
