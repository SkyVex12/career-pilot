// The Call screen (docs/design/ui-proposal.md, "Call"): one screen in four states.
// - setup: what a new user still needs (sign in, the Career-OS link when the stage requires it,
//   a resume, an audio check), each step with its own action;
// - ready: the next interview (or the company, for a standup or meeting) and Start call;
// - live: the call running, its last question and levels, mode and answer style, End call;
// - just ended: the call's summary as it is written (SessionDetail).
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type { AnswerStyle, Mode } from "@copilot/protocol";
import { api, rest, type CareerOsConnection, type CompanyDocument, type DeviceInfo, type Employer, type LiveSession, type PackState, type Settings } from "./api";
import { parseTarget, upcoming } from "./catalog";
import { roundLabel, useCatalog } from "./Applications";
import { applicationOption, resumeLabel } from "./pairing";
import { DeviceTest } from "./AudioCheck";
import { CareerOsConnect } from "./CareerOs";
import { LiveNow, MODE_LABEL } from "./calls";
import { STOPPED, type SessionView } from "./session";
import { clock } from "./postcall";
import { defaultEmployer, packLabel } from "./workday";
import { PackBadge, plural, relative } from "./shared";
import { Badge, Banner, Button, Card, Checkbox, ConfirmDialog, EmptyState, Field, Input, PageHeader, SegmentedControl, Select, SkeletonList, Spinner, Textarea, type Tone } from "./ui";

// The post-call view (with the call's replay) loads when a call ends.
const SessionDetail = lazy(() => import("./SessionDetail").then((m) => ({ default: m.SessionDetail })));

const PLATFORMS: [string, string][] = [
  ["zoom", "Zoom"],
  ["meet", "Google Meet"],
  ["teams", "Microsoft Teams"],
  ["other", "Other"],
];

const MODES = [
  ["interview", "Interview"],
  ["standup", "Standup"],
  ["meeting", "Meeting"],
] as const;

const ANSWER_STYLES = [
  ["bullets", "Bulleted"],
  ["paragraph", "Paragraph"],
] as const;

const LAST_COMPANY = "copilot.lastCompany";
const LAST_ANSWER_STYLE = "copilot.answerStyle";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // a convenience only
  }
}

const atWorkMode = (mode: Mode) => mode === "standup" || mode === "meeting";

export interface CallProps {
  session: SessionView;
  /** Signed in (cloud) or the dev token set (local). */
  tokenSet: boolean;
  environment: string;
  /** An installed release build (it signs in through the browser). */
  release: boolean;
  signedInAs: string | null;
  settings: Settings | null;
  /** The Career-OS link, when the stage has one; `locked` when it is required and missing. */
  careerOs: CareerOsConnection | null;
  locked: boolean;
  onCareerOsChanged: () => void;
  onSettingsSaved: () => void;
  onSettings: () => void;
  onResumes: () => void;
  onOpenLive: (live: LiveSession) => Promise<void>;
  /** An interview to start a call for, chosen elsewhere (Applications' "Start"). */
  preset?: { target: string; n: number } | null;
}

export function CallScreen(props: CallProps) {
  const { session, tokenSet, locked } = props;
  const [postCall, setPostCall] = useState<string | null>(null);
  // The post-call view for the call that just ended.
  useEffect(() => {
    if (session.running) setPostCall(null);
    else if (session.stopped === "ended" && session.sessionId) setPostCall(session.sessionId);
  }, [session.stopped, session.sessionId, session.running]);

  if (!tokenSet || locked) return <Setup {...props} />;
  if (postCall && !session.running) {
    return (
      <Suspense fallback={<SkeletonList rows={3} label="Loading the call" />}>
        <SessionDetail sessionId={postCall} justEnded onNewCall={() => setPostCall(null)} />
      </Suspense>
    );
  }
  return <Ready {...props} />;
}

// --- Setup ---------------------------------------------------------------------------------

function Setup({ tokenSet, environment, release, signedInAs, settings, careerOs, locked, onCareerOsChanged, onSettingsSaved, onSettings, onResumes }: CallProps) {
  const [signingIn, setSigningIn] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasResume, setHasResume] = useState<boolean | null>(null);
  useEffect(() => {
    if (!tokenSet) return;
    rest.resumes().then(
      (r) => setHasResume(Boolean(r.base) || r.versions.length > 0),
      () => setHasResume(null),
    );
  }, [tokenSet]);
  const browserSignIn = release || environment === "cloud";
  const signIn = async () => {
    setSigningIn(true);
    setError(null);
    try {
      await api.signIn();
      onSettingsSaved();
    } catch (e) {
      setError(String(e));
    } finally {
      setSigningIn(false);
    }
  };
  const linkNeeded = careerOs?.available && (careerOs.required || !careerOs.connected);
  const showLink = Boolean(linkNeeded || locked || careerOs?.connected);
  // Steps are numbered as shown: the Career-OS step only when the stage has Career-OS.
  const num = (step: "resume" | "audio") => (step === "resume" ? 2 : 3) + (showLink ? 1 : 0);
  return (
    <>
      <PageHeader title="Get ready for your first call" meta="A few steps, once. Settings has all of them later." />
      <ol className="setup">
        <Step n={1} done={tokenSet} title="Sign in" doneText={signedInAs ?? (browserSignIn ? "Signed in" : "Dev token set")}>
          {browserSignIn ? (
            <>
              <p className="ui-text-2">Opens your browser at the sign-in page. You stay signed in on this computer.</p>
              <div className="ui-row-inline">
                <Button variant="primary" loading={signingIn} onClick={signIn} disabled={!release && !settings?.cloud_api_url}>
                  {signingIn ? "Waiting for the browser…" : "Sign in with your browser"}
                </Button>
                {!release && !settings?.cloud_api_url && (
                  <Button variant="link" onClick={onSettings}>
                    Set the cloud URLs first (Settings, Advanced)
                  </Button>
                )}
              </div>
              {error && <p className="ui-field-error">{error}</p>}
            </>
          ) : (
            <div className="ui-row-inline">
              <p className="ui-text-2">A development build signs in with the dev token.</p>
              <Button onClick={onSettings}>Set it in Settings</Button>
            </div>
          )}
        </Step>
        {showLink && (
          <Step
            n={2}
            done={Boolean(careerOs?.connected)}
            title={
              <>
                Link Career-OS {careerOs?.required ? <Badge tone="warning">Required</Badge> : <Badge tone="neutral">Optional</Badge>}
              </>
            }
            doneText={careerOs?.user ? `Linked as ${careerOs.user.name || careerOs.user.id}` : "Linked"}
            disabled={!tokenSet}
          >
            {careerOs && tokenSet ? <CareerOsConnect conn={careerOs} onConnected={onCareerOsChanged} /> : <p className="ui-text-2">After you sign in.</p>}
          </Step>
        )}
        <Step n={num("resume")} done={hasResume === true} title={<>Add your resume <Badge tone="neutral">Recommended</Badge></>} doneText="Resume added" disabled={!tokenSet || locked}>
          <div className="ui-row-inline">
            <p className="ui-text-2">Answers draw on it. Without one they stay general.</p>
            <Button onClick={onResumes} disabled={!tokenSet || locked}>
              Add resume
            </Button>
          </div>
        </Step>
        <Step n={num("audio")} done={false} title="Check your audio" doneText="">
          {settings ? <DevicePickers settings={settings} disabled={false} onSaved={onSettingsSaved} /> : <Spinner label="Loading devices" />}
        </Step>
      </ol>
      <div className="setup-start">
        <Button variant="primary" size="lg" disabled>
          Start call
        </Button>
        <span className="ui-field-hint">{!tokenSet ? "Sign in first." : "Link Career-OS first."}</span>
      </div>
    </>
  );
}

function Step({ n, done, title, doneText, disabled, children }: { n: number; done: boolean; title: React.ReactNode; doneText: string; disabled?: boolean; children: React.ReactNode }) {
  return (
    <li className={`setup-step${done ? " is-done" : ""}${disabled ? " is-disabled" : ""}`}>
      <span className="setup-mark" aria-hidden="true">
        {done ? "✓" : n}
      </span>
      <div className="setup-body">
        <h2 className="setup-title">
          {title}
          {done && <span className="ui-visually-hidden"> (done)</span>}
        </h2>
        {done ? <p className="ui-text-2">{doneText}</p> : children}
      </div>
    </li>
  );
}

// --- Ready and live -----------------------------------------------------------------------------

function Ready({ session, tokenSet, environment, settings, onSettingsSaved, onSettings, onResumes, onOpenLive, preset }: CallProps) {
  const running = session.running;
  const [mode, setMode] = useState<Mode>("interview");
  const [answerStyle, setAnswerStyle] = useState<AnswerStyle>(() => (read(LAST_ANSWER_STYLE) === "paragraph" ? "paragraph" : "bullets"));
  const catalog = useCatalog(tokenSet);
  const [target, setTarget] = useState("");
  const [title, setTitle] = useState("");
  const [platform, setPlatform] = useState("zoom");
  const [consent, setConsent] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const work = useCompanies(tokenSet);
  const [employerId, setEmployerId] = useState("");
  const [agenda, setAgenda] = useState("");
  const atWork = atWorkMode(mode);
  const materials = useMaterials(atWork && tokenSet ? employerId : "");
  const [materialIds, setMaterialIds] = useState<string[]>([]);
  const [hasResume, setHasResume] = useState<boolean | null>(null);
  useEffect(() => setMaterialIds([]), [employerId]);
  useEffect(() => {
    if (!employerId && work.employers) setEmployerId(defaultEmployer(work.employers, read(LAST_COMPANY)));
  }, [work.employers]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!tokenSet) return;
    rest.resumes().then(
      (r) => setHasResume(Boolean(r.base) || r.versions.length > 0),
      () => setHasResume(null),
    );
  }, [tokenSet]);
  const pack = usePackFor(atWork && !running ? employerId : "", mode);

  const interviews = upcoming(catalog.interviews ?? []);
  useEffect(() => {
    // Default to the next interview, else the newest application.
    if (target) return;
    if (interviews.length) setTarget(`interview:${interviews[0].id}`);
    else if (catalog.applications?.length) setTarget(`job:${catalog.applications[0].id}`);
  }, [catalog.applications, catalog.interviews]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (preset) {
      setMode("interview");
      setTarget(preset.target);
    }
  }, [preset]);

  const changeMode = (m: Mode) => {
    setMode(m);
    if (running) api.setMode(m).catch(() => undefined);
  };
  const changeStyle = (s: AnswerStyle) => {
    setAnswerStyle(s);
    write(LAST_ANSWER_STYLE, s);
    if (running) api.setAnswerStyle(s).catch(() => undefined);
  };

  const start = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const picked = mode === "interview" ? parseTarget(target, catalog.interviews ?? []) : { job_id: null, interview_id: null };
      const company = atWork && employerId ? employerId : null;
      if (company) write(LAST_COMPANY, company);
      await api.startSession({
        mode,
        ...picked,
        meeting_title: title.trim() || null,
        employer_id: company,
        agenda: mode === "meeting" ? agenda.trim() || null : null,
        material_ids: company ? materialIds : [],
        platform,
        store_transcript: consent,
        answer_style: answerStyle,
      });
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };
  const endOther = async () => {
    setMessage(null);
    try {
      setMessage(`${await api.endOtherSession()}. You can start now.`);
    } catch (e) {
      setMessage(String(e));
    }
  };

  // What the call is for, in words (the Next up card and the live panel).
  const interview = target.startsWith("interview:") ? (catalog.interviews ?? []).find((i) => `interview:${i.id}` === target) : undefined;
  const application = target.startsWith("job:") ? catalog.applications?.find((a) => `job:${a.id}` === target) : interview ? catalog.applications?.find((a) => a.id === interview.job_id) : undefined;
  const employer = work.employers?.find((e) => e.id === employerId);
  const forWhat = atWork
    ? employer
      ? `${employer.name}${employer.title ? ` · ${employer.title}` : ""}`
      : "No company's notes"
    : interview
      ? `${interview.company ?? "?"} · ${roundLabel(interview.round)}`
      : application
        ? `${application.company} · ${application.role}`
        : "No application (your base resume only)";

  const stopped = session.stopped && session.stopped !== "ended" && !running ? session.stopped : null;

  if (running) {
    return (
      <Live
        session={session}
        mode={mode}
        onMode={changeMode}
        answerStyle={answerStyle}
        onAnswerStyle={changeStyle}
        forWhat={forWhat}
        consent={consent}
        message={message}
        onMessage={setMessage}
      />
    );
  }

  return (
    <>
      <PageHeader title="Call" />
      {tokenSet && <LiveNow running={running} liveId={null} onOpen={onOpenLive} />}
      {stopped && (
        <Banner
          tone={stopped === "outdated" ? "info" : "warning"}
          title={STOPPED[stopped]}
          action={
            stopped === "session_active" ? (
              <Button size="sm" onClick={endOther}>
                End the other call
              </Button>
            ) : stopped === "unauthorized" ? (
              <Button size="sm" onClick={onSettings}>
                Open settings
              </Button>
            ) : undefined
          }
        >
          {stopped === "session_active" && "Another call is running for this profile."}
          {stopped === "no_minutes" && "No minutes left on the plan."}
          {stopped === "outdated" && "This version is older than the server supports. The app is updating and restarts by itself."}
          {stopped === "unauthorized" && (environment === "cloud" ? "Sign in again under Settings, Account." : "Check the dev token in Settings, Advanced.")}
          {stopped === "replaced" && "The call continued in another app."}
        </Banner>
      )}
      {hasResume === false && (
        <Banner tone="info" title="No resume yet" action={<Button size="sm" onClick={onResumes}>Add resume</Button>}>
          Answers stay general until you add one.
        </Banner>
      )}

      <div className="call-mode">
        <SegmentedControl label="Mode" value={mode} options={MODES} onChange={changeMode} />
      </div>

      <Card className="next-up">
        {atWork ? (
          <div className="ui-stack">
            <Field
              label="Company"
              hint={work.employers?.length === 0 ? "No company yet: add the one you work at under Work." : undefined}
              error={work.error}
            >
              <div className="ui-row-inline">
                <Select value={employerId} onChange={(e) => setEmployerId(e.target.value)}>
                  <option value="">None (no company's notes)</option>
                  {work.employers
                    ?.filter((e) => e.status === "active")
                    .map((e) => (
                      <option key={e.id} value={e.id}>
                        {e.name}
                        {e.current ? "" : " (past)"}
                      </option>
                    ))}
                </Select>
                {employerId && <PackStateBadge state={pack} />}
              </div>
            </Field>
            {employerId && <MaterialPicker materials={materials} selected={materialIds} onChange={setMaterialIds} />}
            {mode === "meeting" && (
              <Field label="Agenda" optional hint="The topic, so answers start from the right systems and decisions.">
                <Textarea rows={3} value={agenda} onChange={(e) => setAgenda(e.target.value)} />
              </Field>
            )}
          </div>
        ) : (
          <div className="ui-stack">
            <div className="next-up-head">
              <span className="ui-section-title">{interview ? "Next up" : "Call for"}</span>
              {interview ? (
                <>
                  <h2 className="next-up-title ui-truncate" title={forWhat}>
                    {interview.company ?? "?"} · {interview.title ?? ""}
                  </h2>
                  <p className="ui-text-2">
                    {roundLabel(interview.round)} round · {relative(interview.starts_at)}
                  </p>
                </>
              ) : (
                <h2 className="next-up-title ui-truncate" title={forWhat}>
                  {forWhat}
                </h2>
              )}
              <div className="ui-row-inline">
                {(interview || application) && <PackBadge pack={interview ? interview.pack : application?.pack} />}
                {application && <span className="ui-muted">{resumeLabel(application)}</span>}
              </div>
            </div>
            <Field label="Call for" hint="The answers use this application's job description and its paired resume only." error={catalog.error}>
              <Select value={target} onChange={(e) => setTarget(e.target.value)}>
                <option value="">No application (your base resume only)</option>
                {interviews.length > 0 && (
                  <optgroup label="Interviews">
                    {interviews.map((i) => (
                      <option key={i.id} value={`interview:${i.id}`}>
                        {[i.company, i.title, roundLabel(i.round), relative(i.starts_at)].filter(Boolean).join(" · ")}
                      </option>
                    ))}
                  </optgroup>
                )}
                {(catalog.applications?.length ?? 0) > 0 && (
                  <optgroup label="Applications">
                    {catalog.applications?.map((a) => (
                      <option key={a.id} value={`job:${a.id}`}>
                        {applicationOption(a)}
                      </option>
                    ))}
                  </optgroup>
                )}
              </Select>
            </Field>
          </div>
        )}
        <div className="call-consent">
          <Checkbox
            checked={consent}
            onChange={setConsent}
            label="Store the transcript after the call (everyone on the call has agreed)"
            hint={
              consent
                ? "Both sides are transcribed live and the transcript is stored, encrypted. No audio is recorded."
                : "Both sides are transcribed live for the answers, but no words are stored; the summary covers only the call's length and the questions' types."
            }
          />
        </div>
        <div className="call-start">
          <Button variant="primary" size="lg" loading={busy} disabled={!tokenSet} onClick={start}>
            Start call
          </Button>
          <Button onClick={() => api.showOverlay(true)}>Show overlay</Button>
        </div>
        {message && <Banner tone="danger">{message}</Banner>}
      </Card>

      {settings && <AudioSummary settings={settings} onSaved={onSettingsSaved} />}

      <div className="call-more">
        <Button variant="link" aria-expanded={more} onClick={() => setMore((m) => !m)}>
          {more ? "▾ Fewer options" : "▸ More options (title, call app, detailed answer style)"}
        </Button>
        {more && (
          <Card inset>
            <div className="form-2">
              <Field label="Title" optional hint="Shown in History.">
                <Input value={title} onChange={(e) => setTitle(e.target.value)} />
              </Field>
              <Field label="Call app">
                <Select value={platform} onChange={(e) => setPlatform(e.target.value)}>
                  {PLATFORMS.map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <Field
              label="Detailed answer"
              hint={
                answerStyle === "paragraph"
                  ? "The talking points stay short; the detailed answer is a connected paragraph you can say as it is."
                  : "The talking points stay short; the detailed answer is more lines, going deeper than the points."
              }
            >
              <SegmentedControl label="Detailed answer" value={answerStyle} options={ANSWER_STYLES} onChange={changeStyle} />
            </Field>
          </Card>
        )}
      </div>
    </>
  );
}

function Live({
  session,
  mode,
  onMode,
  answerStyle,
  onAnswerStyle,
  forWhat,
  consent,
  message,
  onMessage,
}: {
  session: SessionView;
  mode: Mode;
  onMode: (m: Mode) => void;
  answerStyle: AnswerStyle;
  onAnswerStyle: (s: AnswerStyle) => void;
  forWhat: string;
  consent: boolean;
  message: string | null;
  onMessage: (m: string | null) => void;
}) {
  const [ending, setEnding] = useState(false);
  const elapsed = useElapsed(session);
  const last = [...session.questions].reverse().find((q) => !q.cancelled);
  const levels = useLevels();
  const b = liveBadge(session);
  const endNow = async (summarize: boolean) => {
    try {
      await api.endSession(summarize);
    } catch (e) {
      onMessage(String(e));
      throw e;
    }
  };
  return (
    <>
      <PageHeader
        title={
          <span className="ui-row-inline">
            <Badge tone="success" icon="●">
              Live
            </Badge>
            {MODE_LABEL[mode]} <span className="ui-num ui-text-2">{elapsed}</span>
          </span>
        }
        actions={
          <>
            <Button onClick={() => api.showOverlay(true)}>Show overlay</Button>
            <Button variant="danger" onClick={() => setEnding(true)}>
              End call…
            </Button>
          </>
        }
      />
      <Card>
        <div className="ui-stack">
          <div className="live-row">
            <span className="ui-muted">For</span>
            <span className="ui-truncate">{forWhat}</span>
          </div>
          <div className="live-row">
            <span className="ui-muted">Last question</span>
            <span className="ui-clamp ui-clamp-2">{last ? `"${last.text}"` : "None yet. Press the hotkey to answer the last thing said."}</span>
          </div>
          <div className="live-row">
            <span className="ui-muted">Levels</span>
            <div className="live-levels">
              <Level label="Them" value={levels?.them_rms ?? 0} speaking={session.speaking.them} />
              <Level label="You" value={levels?.me_rms ?? 0} speaking={session.speaking.me} />
            </div>
          </div>
          <div className="live-row">
            <span className="ui-muted">Status</span>
            <Badge tone={b.tone}>{b.text}</Badge>
          </div>
        </div>
      </Card>
      <Card>
        <div className="form-2">
          <Field label="Mode">
            <SegmentedControl label="Mode" value={mode} options={MODES} onChange={onMode} />
          </Field>
          <Field label="Detailed answer" hint="Changes apply to the next answers.">
            <SegmentedControl label="Detailed answer" value={answerStyle} options={ANSWER_STYLES} onChange={onAnswerStyle} />
          </Field>
        </div>
      </Card>
      {message && <Banner tone="danger">{message}</Banner>}
      <ConfirmDialog
        open={ending}
        onOpenChange={setEnding}
        title="End this call?"
        danger={false}
        cancelLabel="Keep going"
        confirmLabel="Summarize and end"
        onConfirm={() => endNow(true)}
        extra={
          <Button
            onClick={() => {
              setEnding(false);
              endNow(false).catch(() => undefined);
            }}
          >
            {consent ? "End, transcript only" : "End without a summary"}
          </Button>
        }
      >
        {mode === "standup"
          ? "Summarize if you will need this later: the standup report and your commitments feed the next standup."
          : "Summarize if you will need this context later, as with standups and recurring meetings. Otherwise keep the transcript only."}
        {!consent && " The transcript is not being stored for this call, so without a summary only its length is kept."}
      </ConfirmDialog>
    </>
  );
}

function liveBadge(s: SessionView): { tone: Tone; text: string } {
  const c = s.connection;
  if (c?.state === "connecting") return { tone: "warning", text: "Connecting" };
  if (c?.state === "reconnecting") return { tone: "warning", text: "Reconnecting" };
  const bad = Object.entries(s.status).find(([, v]) => v.state !== "ok");
  if (bad) return { tone: "warning", text: bad[1].message || `${bad[0]} ${bad[1].state}` };
  return { tone: "success", text: "Listening" };
}

function Level({ label, value, speaking }: { label: string; value: number; speaking: boolean }) {
  const fill = value > 0 ? Math.max(0, Math.min(1, (20 * Math.log10(value) + 60) / 60)) : 0;
  return (
    <div className="level">
      <span className="level-label">
        {label}
        {speaking && <span className="ui-visually-hidden"> speaking</span>}
      </span>
      <span className="level-bar" role="meter" aria-label={`${label} level`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(fill * 100)}>
        <span className="level-fill" style={{ width: `${Math.round(fill * 100)}%` }} />
      </span>
    </div>
  );
}

/** The call's audio levels, while it runs. */
function useLevels() {
  const [levels, setLevels] = useState<{ them_rms: number; me_rms: number } | null>(null);
  useEffect(() => {
    const t = setInterval(() => {
      api.liveStatus().then((s) => setLevels(s.levels), () => undefined);
    }, 300);
    return () => clearInterval(t);
  }, []);
  return levels;
}

/** Time since the call started: from the server's start time when it says, else from when this
 * window saw it go live. */
function useElapsed(s: SessionView): string {
  const [since, setSince] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!s.sessionId) return;
    rest.liveSessions().then(
      (r) => {
        const live = r.sessions.find((l) => l.id === s.sessionId);
        if (live?.started_at) setSince(Date.parse(live.started_at));
      },
      () => undefined,
    );
  }, [s.sessionId]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return clock(now - since);
}

// --- Audio ------------------------------------------------------------------------------------

/** One line about the devices, with Test and Change; Change shows the pickers. */
function AudioSummary({ settings, onSaved }: { settings: Settings; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const devices = useDevices();
  const name = (list: DeviceInfo[] | undefined, id: string | null) => (id ? (list?.find((d) => d.id === id)?.name ?? "Unknown device") : "System default");
  return (
    <Card className="audio-summary">
      <div className="ui-row-inline audio-line">
        <span>
          <span aria-hidden="true">🎙 </span>
          <span className="ui-visually-hidden">Microphone: </span>
          {name(devices?.inputs, settings.mic_device)}
        </span>
        <span>
          <span aria-hidden="true">🔊 </span>
          <span className="ui-visually-hidden">Call audio: </span>
          {name(devices?.outputs, settings.output_device)}
        </span>
        <span className="ui-muted">Echo cancellation {settings.echo_cancellation ? "on" : "off"}</span>
        <span className="ui-spacer" />
        <Button size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Done" : "Change or test"}
        </Button>
      </div>
      {open && (
        <div className="audio-pickers">
          <DevicePickers settings={settings} disabled={false} onSaved={onSaved} />
        </div>
      )}
    </Card>
  );
}

function useDevices() {
  const [devices, setDevices] = useState<{ inputs: DeviceInfo[]; outputs: DeviceInfo[] } | null>(null);
  useEffect(() => {
    api.listDevices().then(setDevices, () => setDevices({ inputs: [], outputs: [] }));
  }, []);
  return devices;
}

/** The microphone and call-audio pickers, each with its Test; saved the moment one is picked. */
export function DevicePickers({ settings, disabled, onSaved }: { settings: Settings; disabled: boolean; onSaved: () => void }) {
  const devices = useDevices();
  const [mic, setMic] = useState(settings.mic_device);
  const [output, setOutput] = useState(settings.output_device);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setMic(settings.mic_device), [settings.mic_device]);
  useEffect(() => setOutput(settings.output_device), [settings.output_device]);
  const pick = async (key: "mic_device" | "output_device", value: string | null) => {
    (key === "mic_device" ? setMic : setOutput)(value);
    setError(null);
    try {
      await api.saveSettings({ ...settings, [key]: value }, null);
      onSaved();
    } catch (e) {
      setError(String(e));
    }
  };
  const select = (list: DeviceInfo[] | undefined, value: string | null, key: "mic_device" | "output_device", label: string) => (
    <Select aria-label={label} value={value ?? ""} disabled={disabled} onChange={(e) => pick(key, e.target.value || null)}>
      <option value="">System default</option>
      {list?.map((d) => (
        <option key={d.id} value={d.id}>
          {d.name}
          {d.is_default ? " (default)" : ""}
        </option>
      ))}
    </Select>
  );
  return (
    <div className="ui-stack devices">
      <div className="ui-field">
        <span className="ui-field-label">Microphone</span>
        <DeviceTest part="mic" micDevice={mic} outputDevice={output} disabled={disabled}>
          {select(devices?.inputs, mic, "mic_device", "Microphone")}
        </DeviceTest>
      </div>
      <div className="ui-field">
        <span className="ui-field-label">Call audio from</span>
        <DeviceTest part="call" micDevice={mic} outputDevice={output} disabled={disabled}>
          {select(devices?.outputs, output, "output_device", "Call audio from")}
        </DeviceTest>
        <p className="ui-field-hint">The speakers or headset the call plays on; captured in loopback.</p>
      </div>
      {error && <p className="ui-field-error">{error}</p>}
    </div>
  );
}

// --- Companies and their material (standups and meetings) -------------------------------------

function useCompanies(ready: boolean): { employers: Employer[] | null; error: string | null } {
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!ready) return;
    rest.employers().then(
      (r) => {
        setEmployers(r.employers);
        setError(null);
      },
      (e) => setError(String(e)),
    );
  }, [ready]);
  return { employers, error };
}

/** A company's documents that have approved facts: the ones a call can focus on. */
function useMaterials(employerId: string): { documents: CompanyDocument[] | null; error: string | null } {
  const [documents, setDocuments] = useState<CompanyDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setDocuments(null);
    setError(null);
    if (!employerId) return;
    let cancelled = false;
    rest.companyDocuments(employerId).then(
      (out) => !cancelled && setDocuments(out.documents.filter((d) => d.status === "ingested" && (d.approved_proposals ?? 0) > 0)),
      (e) => !cancelled && setError(String(e)),
    );
    return () => {
      cancelled = true;
    };
  }, [employerId]);
  return { documents, error };
}

function MaterialPicker({ materials, selected, onChange }: { materials: { documents: CompanyDocument[] | null; error: string | null }; selected: string[]; onChange: (ids: string[]) => void }) {
  const { documents, error } = materials;
  const toggle = useCallback((id: string, on: boolean) => onChange(on ? [...selected, id] : selected.filter((s) => s !== id)), [onChange, selected]);
  return (
    <fieldset className="material-pick">
      <legend className="ui-field-label">
        Focus on <span className="ui-field-optional">(optional)</span>
      </legend>
      {error && <p className="ui-field-error">{error}</p>}
      {documents === null && !error && <Spinner size="sm" label="Loading the company's material" />}
      {documents?.length === 0 && <EmptyState title="No material with approved facts yet">Upload or paste some under Work, Material.</EmptyState>}
      {documents?.map((d) => (
        <Checkbox key={d.id} checked={selected.includes(d.id)} onChange={(on) => toggle(d.id, on)} label={`${d.name} (${plural(d.approved_proposals ?? 0, "fact")})`} />
      ))}
      <p className="ui-field-hint">Answers read the chosen material first; everything else the company has is still searched for each question.</p>
    </fieldset>
  );
}

function PackStateBadge({ state }: { state: PackState | null }) {
  const p = packLabel(state);
  return <Badge tone={p.tone === "ok" ? "success" : p.tone === "busy" ? "warning" : "neutral"}>{p.text}</Badge>;
}

/// Picking a company asks for its pack, so the call starts on a pack built from the latest
/// notes; a call never waits for it (the gateway renders one itself if needed).
function usePackFor(employerId: string, mode: Mode): PackState | null {
  const [state, setState] = useState<PackState | null>(null);
  useEffect(() => {
    setState(null);
    if (!employerId || !atWorkMode(mode)) return;
    let stop = false;
    let tries = 0;
    const poll = async () => {
      try {
        const asked = await rest.ensurePack(employerId, mode as "standup" | "meeting");
        if (stop) return;
        setState(asked);
        while (!stop && asked.status !== "ready" && tries++ < 20) {
          await new Promise((r) => setTimeout(r, 3000));
          const now = (await rest.packs(employerId)).packs.find((p) => p.mode === mode) ?? null;
          if (stop) return;
          setState(now?.status === "ready" ? now : { ...(now ?? asked), status: "building" });
          if (now?.status === "ready") return;
        }
      } catch {
        if (!stop) setState(null);
      }
    };
    poll();
    return () => {
      stop = true;
    };
  }, [employerId, mode]);
  return state;
}
