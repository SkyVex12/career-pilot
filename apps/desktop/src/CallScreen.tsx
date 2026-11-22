import { CompanionDevices } from "./CompanionDevices";
import { prepareForRealCall } from "./practiceAudio";
import { MeetingPreparation } from "./MeetingPreparation";
// The Call screen (docs/design/ui-proposal.md, "Call"): one screen in four states.
// - setup: what a new user still needs (sign in, the Career-OS link when the stage requires it,
//   a resume, an audio check), each step with its own action;
// - ready: the next interview (or the company, for a standup or meeting) and Start call;
// - live: the call running, its last question and levels, mode and answer style, End call;
// - just ended: the call's summary as it is written (SessionDetail).
import { CallLanguage } from "./CallLanguage";
import { SignIn, signInWays } from "./SignIn";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import type { AnswerStyle, Mode } from "@copilot/protocol";
import { api, rest, type ApplicationDetail, type CareerOsConnection, type CompanyDocument, type DeviceInfo, type Employer, type LiveSession, type PackState, type ResumeVersion, type Settings } from "./api";
import { parseTarget, upcoming } from "./catalog";
import { AddApplication, useCatalog } from "./Applications";
import { isTextFile, resumeStatusLabel } from "./pairing";
import { DeviceTest } from "./AudioCheck";
import { useWindowMode } from "./windowMode";
import { Icon } from "./ui/Icon";
import { useHoverMenu } from "./ui/useHoverMenu";
import { WorkspaceQuickSettings } from "./WorkspaceQuickSettings";
import { RubyLauncher } from "./RubyLauncher";
import { useWorkspaceCallBusy, useWorkspaceCompactWindow, useWorkspaceOpenQuickSettings } from "./Shell";
import { CareerOsConnect } from "./CareerOs";
import { LiveNow, MODE_LABEL } from "./calls";
import { GuestInvites, GuestPanel, Watching } from "./seats";
import { STOPPED, postCallFor, type SessionView } from "./session";
import { clock } from "./postcall";
import { isMac } from "./platform";
import { defaultEmployer, packLabel } from "./workday";
import { plural, useShowCount } from "./shared";
import { Badge, Banner, Button, Card, Checkbox, ConfirmDialog, EmptyState, Field, PageHeader, SegmentedControl, Select, SkeletonList, Spinner, Textarea, type Tone } from "./ui";

import { GuestPicker } from "./GuestPicker";
import { guestScope, invitePlannedGuests, type PlannedGuest } from "./guestStorage";
import "./callDesign.css";

// The post-call view (with the call's replay) loads when a call ends.
const SessionDetail = lazy(() => import("./SessionDetail").then((m) => ({ default: m.SessionDetail })));

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
  /** The session's reducer: a guest's notes are recorded as sent (seats.tsx). */
  dispatch?: Parameters<typeof Watching>[0]["dispatch"];
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
  onApplications?: (applicationId?: string) => void;
  onWork?: () => void;
  onHistory?: () => void;
  onOpenLive: (live: LiveSession) => Promise<void>;
  /** An interview to start a call for, chosen elsewhere (Applications' "Start"). */
  preset?: { target: string; n: number } | null;
  /** The Call tab is shown. It stays mounted while hidden, so each time it is shown again it
   * reloads what other screens may have changed: applications and packs (a Career-OS pull),
   * resumes, companies and their documents. */
  active?: boolean;
}

export function CallScreen(props: CallProps) {
  const { session, tokenSet, locked } = props;
  const [postCall, setPostCall] = useState<string | null>(null);
  // Only a call observed live in this workspace may open its review automatically.
  // Restored event history is useful to History, but must not become the startup page.
  const activeCall = useRef<string | null>(session.running && !session.seat ? session.sessionId : null);
  useEffect(() => {
    if (session.running) {
      activeCall.current = session.seat ? null : session.sessionId;
      setPostCall(null);
    } else {
      const ended = postCallFor(session);
      if (ended && activeCall.current === ended) setPostCall(ended);
      // Native stop emits connection.stopped before the separate stopped event.
      // Keep this live call eligible until its final reason reaches the reducer.
      const awaitingStoppedEvent = session.stopped === null
        && session.connection?.state === "stopped"
        && session.connection.reason === "ended"
        && !session.seat
        && activeCall.current === session.sessionId;
      if (!awaitingStoppedEvent) activeCall.current = null;
    }
  }, [session]);
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

function Setup({ tokenSet, environment, release, signedInAs, settings, careerOs, locked, onCareerOsChanged, onSettingsSaved, onSettings, onResumes, active = true }: CallProps) {
  const hasResume = useHasResume(tokenSet, useShowCount(active));
  const browserSignIn = release || environment === "cloud";
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
              <p className="ui-text-2">{signInWays(settings?.email_sign_in ?? null, settings?.career_os_sign_in ?? null)}</p>
              <SignIn
                browserName={settings?.oauth?.provider === "cognito" ? null : "Google"}
                emailOffered={settings?.email_sign_in ?? null}
                careerOsOffered={settings?.career_os_sign_in ?? null}
                disabled={!release && !settings?.cloud_api_url}
                disabledHint={
                  <Button variant="link" onClick={onSettings}>
                    Set the cloud URLs first (Settings, Advanced)
                  </Button>
                }
                onSignedIn={onSettingsSaved}
              />
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

function Ready({ session, dispatch, tokenSet, environment, signedInAs, settings, onSettingsSaved, onSettings, onResumes, onApplications, onWork, onHistory, onOpenLive, preset, active = true }: CallProps) {
  const mini = useWindowMode(false, active && !session.running);
  const openWorkspaceQuickSettings = useWorkspaceOpenQuickSettings();
  useWorkspaceCompactWindow((mode) => { void mini.change(mode ?? "toolbar"); }, () => mini.change("full"));
  const running = session.running;
  const [mode, setMode] = useState<Mode>("interview");
  const [meetingTitle, setMeetingTitle] = useState<string | null>(null);
  const [spokenLanguage, setSpokenLanguage] = useState("en");
  const [languageReady, setLanguageReady] = useState(false);
  const [answerStyle, setAnswerStyle] = useState<AnswerStyle>(() => read(LAST_ANSWER_STYLE) === "paragraph" ? "paragraph" : "bullets");
  useEffect(() => {
    const changed = (event: StorageEvent) => { if (event.key === LAST_ANSWER_STYLE) setAnswerStyle(event.newValue === "paragraph" ? "paragraph" : "bullets"); };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, []);
  const shown = useShowCount(active);
  const catalog = useCatalog(tokenSet);
  const reloadCatalog = catalog.reload;
  useEffect(() => { if (tokenSet && shown > 0) reloadCatalog(); }, [shown]); // eslint-disable-line react-hooks/exhaustive-deps
  const [target, setTarget] = useState("");
  const targetInitialized = useRef(false);
  const [addingApplication, setAddingApplication] = useState(false);
  const [consent, setConsent] = useState(true);
  const [noCall, setNoCall] = useState(false);
  const testing = noCall && mode === "interview";
  const [busy, setBusy] = useState(false);
  const [joining, setJoining] = useState(false);
  useWorkspaceCallBusy(busy || joining);
  const [joinOpen, setJoinOpen] = useState(false);
  const [inviteCount, setInviteCount] = useState(0);
  const [quickSettingsOpen, setQuickSettingsOpen] = useState(false);
  const [quickSettingsHover, setQuickSettingsHover] = useState(false);
  const changeQuickSettings = useCallback((open: boolean) => { setQuickSettingsHover(false); setQuickSettingsOpen(open); }, []);
  const compactSettingsHover = useHoverMenu({
    onOpen: () => { if (mini.mode === "toolbar") void mini.change("full").then(changed => { if (changed) { if (openWorkspaceQuickSettings) openWorkspaceQuickSettings(); else { setQuickSettingsHover(true); setQuickSettingsOpen(true); } } }); },
    onClose: () => {},
  });
  const [message, setMessage] = useState<string | null>(null);
  const scope = guestScope(settings, signedInAs, environment);
  const [guests, setGuests] = useState<PlannedGuest[]>([]);
  const [pendingInvites, setPendingInvites] = useState<{ beforeId: string | null; scope: string; guests: PlannedGuest[] } | null>(null);
  useEffect(() => { setGuests([]); setPendingInvites(null); }, [scope]);
  useEffect(() => {
    if (!pendingInvites || !running || session.seat || !session.sessionId || session.sessionId === pendingInvites.beforeId || pendingInvites.scope !== scope) return;
    const pending = pendingInvites;
    setPendingInvites(null);
    void invitePlannedGuests(session.sessionId, pending.scope, pending.guests);
  }, [pendingInvites, running, session.sessionId, session.seat, scope]);

  const work = useCompanies(tokenSet, shown);
  const [employerId, setEmployerId] = useState("");
  const [agenda, setAgenda] = useState("");
  const atWork = atWorkMode(mode);
  const materials = useMaterials(atWork && tokenSet ? employerId : "", shown);
  const [materialIds, setMaterialIds] = useState<string[]>([]);
  const baseResume = useBaseResume(tokenSet, shown);
  useEffect(() => setMaterialIds([]), [employerId]);
  useEffect(() => {
    if (materials.documents) setMaterialIds(ids => ids.filter(id => materials.documents!.some(d => d.id === id)));
  }, [materials.documents]);
  useEffect(() => { if (!employerId && work.employers) setEmployerId(defaultEmployer(work.employers, read(LAST_COMPANY))); }, [work.employers]); // eslint-disable-line react-hooks/exhaustive-deps
  const pack = usePackFor(atWork && !running ? employerId : "", mode);
  const interviews = upcoming(catalog.interviews ?? []);
  useEffect(() => {
    if (!target || !catalog.applications || !catalog.interviews) return;
    if (target.startsWith("job:") && !catalog.applications.some(app => `job:${app.id}` === target)) setTarget("");
    if (target.startsWith("interview:") && !catalog.interviews.some(item => `interview:${item.id}` === target)) setTarget("");
  }, [catalog.applications, catalog.interviews]); // Validate only when the catalog refreshes, not before a new preset has loaded.
  useEffect(() => {
    if (targetInitialized.current || !catalog.applications || !catalog.interviews) return;
    targetInitialized.current = true;
    if (target) return;
    if (interviews.length) setTarget(`job:${interviews[0].job_id}`);
    else if (catalog.applications?.length) setTarget(`job:${catalog.applications[0].id}`);
  }, [catalog.applications, catalog.interviews]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (preset) { setMode("interview"); setNoCall(false); setTarget(preset.target); } }, [preset]);
  const interview = target.startsWith("interview:") ? catalog.interviews?.find(i => `interview:${i.id}` === target) : undefined;
  const application = target.startsWith("job:") ? catalog.applications?.find(a => `job:${a.id}` === target) : interview ? catalog.applications?.find(a => a.id === interview.job_id) : undefined;
  const [detail, setDetail] = useState<ApplicationDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [editingJd, setEditingJd] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setDetail(null); setDetailError(null); setEditingJd(false);
    if (application) rest.application(application.id).then(value => { if (!cancelled) setDetail(value); }, error => { if (!cancelled) setDetailError(String(error)); });
    return () => { cancelled = true; };
  }, [application?.id, shown]);
  const savedDetail = (value: ApplicationDetail) => { setDetail(value); catalog.includeApplication(value); };
  const changeMode = (value: Mode) => { setMode(value); if (running) api.setMode(value).catch(() => undefined); };
  const changeStyle = (value: AnswerStyle) => { setAnswerStyle(value); write(LAST_ANSWER_STYLE, value); if (running) api.setAnswerStyle(value).catch(() => undefined); };
  const start = async () => {
    if (busy || joining) return;
    if (!languageReady) { setMessage("Load an available call language under Customize before starting."); return; }
    setBusy(true); setMessage(null); setPendingInvites(null);
    const planned = guests.map(g => ({ ...g }));
    const beforeId = session.sessionId;
    try {
      const picked = mode === "interview" ? parseTarget(target, catalog.interviews ?? []) : { job_id: null, interview_id: null };
      const company = atWork && employerId ? employerId : null;
      if (company) write(LAST_COMPANY, company);
      if (mini.mode !== "full" && !await mini.change("full")) return;
      await prepareForRealCall();
      await api.startSession({ mode, ...picked, meeting_title: meetingTitle, employer_id: company, agenda: mode === "meeting" ? agenda.trim() || null : null,
        material_ids: company ? materialIds : [], platform: testing ? "test" : "other", store_transcript: consent, answer_style: answerStyle, spoken_language: spokenLanguage, no_call: testing });
      if (planned.length) setPendingInvites({ beforeId, scope, guests: planned });
      if (testing) { const value = await api.getSettings(); await api.setCodingPrefs("assessment", value.settings.coding_language ?? ""); }
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const endOther = async () => { setMessage(null); try { setMessage(`${await api.endOtherSession()}. You can start now.`); } catch (error) { setMessage(String(error)); } };
  const employer = work.employers?.find(e => e.id === employerId);
  const forWhat = atWork ? employer?.name ?? "No company" : application ? `${application.company} · ${application.role}` : "Base resume";
  const stopped = session.stopped && session.stopped !== "ended" && session.stopped !== "left" && !running ? session.stopped : null;
  const watchedEnded = session.seat && !running && session.stopped === "ended" ? session.seat.hostName : null;
  if (session.seat && running) return <Watching s={session} dispatch={dispatch ?? (() => undefined)} />;
  if (running) return <Live session={session} mode={mode} onMode={changeMode} answerStyle={answerStyle} onAnswerStyle={changeStyle} forWhat={forWhat} consent={consent} message={message} onMessage={setMessage} />;
  const disabled = busy || joining;
  const manageApplication = () => onApplications ? onApplications(application?.id) : setAddingApplication(true);
  const selectedResume = application ? (detail?.id === application.id ? detail : application).resume : baseResume.resume;
  const resumeSummary = selectedResume ? `Resume · ${selectedResume.label} · ${resumeStatusLabel(selectedResume.status)}`
    : application || baseResume.resume === null ? "No resume attached" : baseResume.error ? "Resume unavailable" : "Loading resume…";
  return <div className="session-window" data-mini={mini.mode} onPointerEnter={mini.onPointerEnter} onPointerLeave={mini.onPointerLeave} onPointerDownCapture={mini.cancelHover}>
    <header ref={mini.compactRef} className="session-commandbar" data-shape={mini.mode === "dot" ? "dot" : "toolbar"} data-morphing={mini.busy || undefined} inert={mini.busy || undefined} hidden={mini.mode === "full"} data-native-drag-region="header">
      <span className="session-compact-outline" aria-hidden="true" />
            {mini.mode === "toolbar" && inviteCount > 0 ? <Button className="window-mode-toggle" variant="ghost" aria-label={`Assist a call · ${inviteCount} invitation${inviteCount === 1 ? "" : "s"}`} title="Invitations to assist" onClick={async () => { if (await mini.change("full")) setJoinOpen(true); }} icon={<Icon name="users" size={16} />} /> : <Button className="window-mode-toggle" variant="ghost" disabled={mini.mode === "dot"} aria-label="Expand setup" title="Expand window" onClick={() => void mini.change("full")} icon={<Icon name="chevronDown" size={16} />} />}
      <RubyLauncher className="session-compact-ruby" label={mini.mode === "dot" ? "Expand compact toolbar" : "Expand full window"} title={mini.mode === "dot" ? "Hover to open · Drag to move" : "Click to expand · Drag to move"} onOpen={() => void mini.change(mini.mode === "dot" ? "toolbar" : "full")} onDrag={mini.drag} />
      {inviteCount > 0 && <span className="call-compact-invites" aria-label={`${inviteCount} invitations`}>{inviteCount}</span>}
      {mini.mode === "toolbar" && <><Button className="session-start" variant="primary" loading={busy} disabled={joining} onClick={start}>{testing ? "Start test" : "Start call"}<Icon name="arrow" size={16} /></Button>
        <Button variant="ghost" aria-label="Quick settings" onPointerEnter={compactSettingsHover.onPointerEnter} onPointerLeave={compactSettingsHover.onPointerLeave} onClick={async () => { compactSettingsHover.cancel(); if (await mini.change("full")) { if (openWorkspaceQuickSettings) openWorkspaceQuickSettings(); else changeQuickSettings(true); } }} icon={<Icon name="settings" size={16} />} /></>}
    </header>
    <section className="call-design" hidden={mini.mode !== "full"}>
      {editingJd && detail ? <CallJdEditor app={detail} onBack={() => setEditingJd(false)} onSaved={savedDetail} /> : <>
        <header className="call-design-heading"><div><h1>Start a call</h1><p className="call-design-description">Choose your context. Get guidance when you need it.</p></div><GuestInvites running={running} disabled={busy} open={joinOpen} onOpenChange={setJoinOpen} onCountChange={setInviteCount} onBusyChange={setJoining} /></header>
        <div className="call-design-body">
          <SegmentedControl label="Session type" value={testing ? "test" : mode} options={[["interview", "Interview"], ["standup", "Standup"], ["meeting", "Meeting"], ["test", "Online test"]] as const} disabled={disabled} onChange={value => { setNoCall(value === "test"); changeMode(value === "test" ? "interview" : value); }} />
          <div className="call-notices" hidden={mini.mode !== "full" || mini.busy}>
            {mini.error && <Banner tone="danger">{mini.error}</Banner>}
            {tokenSet && <LiveNow running={running} liveId={null} onOpen={onOpenLive} />}
            {watchedEnded && <Banner tone="info" title={`${watchedEnded}'s call ended`} />}
            {stopped && <Banner tone={stopped === "outdated" ? "info" : "warning"} title={STOPPED[stopped]} action={stopped === "session_active" ? <Button size="sm" onClick={endOther}>End the other call</Button> : stopped === "unauthorized" ? <Button size="sm" onClick={onSettings}>Open settings</Button> : undefined} />}
            {message && <Banner tone="danger">{message}</Banner>}
          </div>
          <div className="call-setup-fields" aria-label="Call context">
            {atWork ? <>
              <div className="call-row"><label className="call-row-label" htmlFor="call-company">Company</label><Select id="call-company" value={employerId} disabled={disabled} onChange={e => setEmployerId(e.target.value)}><option value="">Choose a company</option>{work.employers?.filter(e => e.status === "active").map(e => <option key={e.id} value={e.id}>{e.name}</option>)}</Select><Button className="call-row-action" size="sm" disabled={!onWork || disabled} onClick={onWork}>Manage</Button></div>
              {work.error && <p className="ui-field-error" role="alert">{work.error}</p>}
              <div className="call-row"><span className="call-row-label">Work log</span><span className="call-row-value">{employerId ? "Recent work and decisions" : "Choose a company"}</span><Button className="call-row-action" size="sm" disabled={!onWork || disabled} onClick={onWork}>Update</Button></div>
              <div className="call-row"><span className="call-row-label">Documents</span><span className="call-row-value">{materials.documents?.length ? plural(materials.documents.length, "document") : "Company context"}</span><Button className="call-row-action" size="sm" disabled={!onWork || disabled} onClick={onWork}>Open</Button></div>
              {pack && pack.status !== "ready" && <div className="call-row-hint"><PackStateBadge state={pack} /></div>}
              {mode === "meeting" && <div className="call-row call-agenda-row"><label className="call-row-label" htmlFor="call-agenda">Agenda</label><Textarea id="call-agenda" rows={2} placeholder="What are you discussing?" value={agenda} disabled={disabled} onChange={e => setAgenda(e.target.value)} /></div>}
              {employerId && <details className="call-focus"><summary>Focus on documents{materialIds.length ? ` · ${materialIds.length}` : ""}</summary><MaterialPicker materials={materials} selected={materialIds} onChange={setMaterialIds} /></details>}
            </> : <>
              <div className="call-row call-application-row">
                <label className="call-row-label" htmlFor="call-application">Application</label>
                <div className="call-application-field">
                  <Select id="call-application" aria-describedby="call-application-resume" value={application?.id ?? ""} disabled={disabled} onChange={e => setTarget(e.target.value ? `job:${e.target.value}` : "")}><option value="">General interview · Base resume</option>{catalog.applications?.map(a => <option key={a.id} value={a.id}>{a.company} · {a.role}</option>)}</Select>
                  <div className="call-application-resume" id="call-application-resume"><span>{resumeSummary}</span>{!application && baseResume.resume === null && <Button variant="link" size="sm" disabled={disabled} onClick={onResumes}>Add resume</Button>}</div>
                </div>
                <Button className="call-row-action" size="sm" disabled={disabled} onClick={manageApplication}>{catalog.applications?.length ? "Manage" : "Add"}</Button>
              </div>
              {catalog.error && <p className="ui-field-error" role="alert">{catalog.error}</p>}
              <div className="call-row call-jd-row"><span className="call-row-label">Job description</span><p className="call-jd-preview">{detail?.jd_text.trim() ? detail.jd_text.replace(/\s+/g, " ").trim() : application ? detail ? "Add the responsibilities and requirements." : detailError ? "Job description unavailable" : "Loading job description…" : "Choose a role to add a job description."}</p><Button className="call-row-action" size="sm" disabled={disabled || !detail} onClick={() => setEditingJd(true)}>{detail?.jd_text ? "Edit" : "Add"}</Button></div>
              {detailError && <p className="ui-field-error" role="alert">{detailError}</p>}
            </>}
          </div>
          <div className="call-row call-answer-row"><span className="call-row-label">Answers</span><SegmentedControl label="Answer format" value={answerStyle} options={[["bullets", "Bullet points"], ["paragraph", "Paragraphs"]] as const} disabled={disabled} onChange={changeStyle} /></div>
          <div className="call-optional-settings">
            <CallLanguage onReady={setLanguageReady} value={spokenLanguage} onChange={setSpokenLanguage} disabled={disabled} />
            <MeetingPreparation onChoose={(title, applicationId) => { setMeetingTitle(title); setTarget(applicationId ? `job:${applicationId}` : ""); setMode(applicationId ? "interview" : "meeting"); }} />
          </div>
          <GuestPicker scope={scope} selected={guests} onChange={setGuests} disabled={disabled} />
          <div className="call-preferences"><Checkbox checked={consent} onChange={setConsent} disabled={disabled} label="Save transcript" /><span className="ui-field-hint">With everyone's agreement.</span></div>
          {testing ? <p className="ui-field-hint">Audio off. Type a question or capture your screen.</p> : settings && <AudioSummary settings={settings} onSaved={onSettingsSaved} />}
        </div>
        <footer className="call-design-footer">{onHistory && <Button variant="ghost" size="sm" onClick={onHistory} disabled={disabled}>Past calls</Button>}<span className="ui-spacer" /><Button className="session-start" variant="primary" loading={busy} disabled={joining} onClick={start}>{testing ? "Start test" : "Start call"}<Icon name="arrow" size={16} /></Button></footer>
      </>}
      {quickSettingsOpen && !openWorkspaceQuickSettings && <div className="call-compact-settings"><WorkspaceQuickSettings running={running} open={quickSettingsOpen} hoverOpen={quickSettingsHover} onOpenChange={changeQuickSettings} /></div>}
      <AddApplication open={addingApplication} onOpenChange={setAddingApplication} onAdded={app => { catalog.includeApplication(app); setTarget(`job:${app.id}`); void reloadCatalog(); }} />
    </section>
  </div>;
}

function CallJdEditor({ app, onBack, onSaved }: { app: ApplicationDetail; onBack: () => void; onSaved: (app: ApplicationDetail) => void }) {
  const [text, setText] = useState(app.jd_text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  useWorkspaceCallBusy(busy);
  const save = async () => {
    if (!text.trim()) { setError("Add a job description first."); return; }
    setBusy(true); setError(null);
    try { onSaved(await rest.updateApplication(app.id, { jd_text: text })); onBack(); }
    catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  const upload = async (selected: File) => {
    setBusy(true); setError(null);
    try {
      if (isTextFile(selected.name)) setText(await selected.text());
      else { await api.uploadDocument("job_description", selected, { application_id: app.id }); const value = await rest.application(app.id); onSaved(value); setText(value.jd_text); }
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  };
  return <><header className="call-design-heading"><Button size="sm" disabled={busy} onClick={onBack}>Back</Button><h1>Job description</h1></header><div className="call-design-body call-jd-editor"><p className="ui-muted">{app.company} · {app.role}</p><Field label="Job description" hideLabel error={error}><Textarea rows={14} value={text} disabled={busy} onChange={e => setText(e.target.value)} /></Field></div><footer className="call-design-footer"><input ref={file} type="file" hidden accept=".pdf,.txt,.md,.markdown" onChange={e => { const value = e.target.files?.[0]; e.target.value = ""; if (value) void upload(value); }} /><Button disabled={busy} onClick={() => file.current?.click()}>Upload file</Button><span className="ui-spacer" /><Button variant="primary" loading={busy} onClick={save}>Save</Button></footer></>;
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
  useEffect(() => { const review = () => setEnding(true); window.addEventListener("copilot:review-end-call", review); return () => window.removeEventListener("copilot:review-end-call", review); }, []);
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
      <CompanionDevices sessionId={session.sessionId} />
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
      <Card title="Assistants">
        <GuestPanel s={session} />
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
        secondaryAction={{
          label: consent ? "End, transcript only" : "End without a summary",
          onConfirm: () => endNow(false),
        }}
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
    <div className="audio-summary">
      <div className="ui-row-inline audio-line">
        <div className="audio-device"><Icon name="mic" /><div><span>Microphone</span><strong>{name(devices?.inputs, settings.mic_device)}</strong></div></div>
        <div className="audio-device"><Icon name="headphones" /><div><span>{isMac() ? "System audio" : "Call audio"}</span><strong>{isMac() ? "All apps" : name(devices?.outputs, settings.output_device)}</strong></div></div>
        <Button size="sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          {open ? "Done" : "Change or test"}
        </Button>
      </div>
      {open && (
        <div className="audio-pickers">
          <DevicePickers settings={settings} disabled={false} onSaved={onSaved} />
        </div>
      )}
    </div>
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
  const outputLabel = isMac() ? "Playback output" : "Call audio from";
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
        <span className="ui-field-label">{outputLabel}</span>
        <DeviceTest part="call" micDevice={mic} outputDevice={output} disabled={disabled}>
          {select(devices?.outputs, output, "output_device", outputLabel)}
        </DeviceTest>
        <p className="ui-field-hint">{isMac() ? "Assistant audio and test tone. Transcription captures system audio from all apps." : "The speakers or headset the call plays on; captured in loopback."}</p>
      </div>
      {error && <p className="ui-field-error">{error}</p>}
    </div>
  );
}

// --- Companies and their material (standups and meetings) -------------------------------------

/** Whether the user has any resume yet; asked again each time `shown` changes. */
function useHasResume(ready: boolean, shown: number): boolean | null {
  const [hasResume, setHasResume] = useState<boolean | null>(null);
  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    rest.resumes().then(
      (r) => !cancelled && setHasResume(Boolean(r.base) || r.versions.length > 0),
      () => !cancelled && setHasResume(null),
    );
    return () => {
      cancelled = true;
    };
  }, [ready, shown]);
  return hasResume;
}

/** The general interview uses the base resume, never another application's tailored version. */
function useBaseResume(ready: boolean, shown: number) {
  const [state, setState] = useState<{ resume: ResumeVersion | null | undefined; error: boolean }>({ resume: undefined, error: false });
  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    rest.resumes().then(
      ({ base }) => { if (!cancelled) setState({ resume: base, error: false }); },
      () => { if (!cancelled) setState({ resume: undefined, error: true }); },
    );
    return () => { cancelled = true; };
  }, [ready, shown]);
  return state;
}

/** The user's companies; asked again each time `shown` changes (one added under Work). */
function useCompanies(ready: boolean, shown: number): { employers: Employer[] | null; error: string | null } {
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    rest.employers().then(
      (r) => {
        if (cancelled) return;
        setEmployers(r.employers);
        setError(null);
      },
      (e) => !cancelled && setError(String(e)),
    );
    return () => {
      cancelled = true;
    };
  }, [ready, shown]);
  return { employers, error };
}

/** A company's documents that have given facts: the ones a call can focus on. Asked again each
 * time `shown` changes; the list empties only when the company changes, so a refresh does not flicker. */
function useMaterials(employerId: string, shown: number): { documents: CompanyDocument[] | null; error: string | null } {
  const [documents, setDocuments] = useState<CompanyDocument[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setDocuments(null);
    setError(null);
  }, [employerId]);
  useEffect(() => {
    if (!employerId) return;
    let cancelled = false;
    rest.companyDocuments(employerId).then(
      (out) => !cancelled && setDocuments(out.documents.filter((d) => d.status === "ingested" && (d.approved_proposals ?? 0) > 0)),
      (e) => !cancelled && setError(String(e)),
    );
    return () => {
      cancelled = true;
    };
  }, [employerId, shown]);
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
      {documents?.length === 0 && <EmptyState icon={<Icon name="document" size={28} />} title="No documents read yet">Upload or paste some under Work, Engineering context.</EmptyState>}
      {documents?.map((d) => (
        <Checkbox key={d.id} checked={selected.includes(d.id)} onChange={(on) => toggle(d.id, on)} label={`${d.name} (${plural(d.approved_proposals ?? 0, "fact")})`} />
      ))}
      <p className="ui-field-hint">Answers read the chosen documents' facts first; everything else the company has is still loaded and searched for each question.</p>
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
