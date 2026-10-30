// The main window: the session (start screen, then the post-call screen), resumes,
// applications and interviews, work (the companies the user works at), history, settings, and the debug
// panel (hidden unless turned on in the settings, or with Ctrl+Shift+D).
import { useCallback, useEffect, useState } from "react";
import type { AnswerStyle, Mode } from "@copilot/protocol";
import {
  api,
  inTauri,
  onOpenSettings,
  onSettingsChanged,
  onUpdate,
  rest,
  type CompanyDocument,
  type DeviceInfo,
  type Employer,
  type HotkeyAction,
  type LiveStatus,
  type PackState,
  type Settings,
  type SettingsView,
  type UpdateState,
  type CareerOsConnection,
  type LiveSession,
} from "./api";
import { cloudUrlsFor } from "./urls";
import { ago, bar, settingsLine } from "./updates";
import { parseTarget, upcoming } from "./catalog";
import { Applications, roundLabel, useCatalog } from "./Applications";
import { applicationOption } from "./pairing";
import { DeviceTest } from "./AudioCheck";
import { CareerOsSettings } from "./CareerOs";
import { DeleteAccount, History, LiveNow } from "./History";
import { Resumes } from "./Resumes";
import { when } from "./ui";
import { SessionDetail } from "./SessionDetail";
import { badge, useSession, type SessionView } from "./session";
import { Work } from "./Work";
import { defaultEmployer, packLabel } from "./workday";
import { ProfileContext, accountOf, isLocked, showSwitch, useProfiles, type ProfileState } from "./profiles";

type Tab = "start" | "resumes" | "applications" | "work" | "history" | "settings" | "debug";

const TABS: [Tab, string][] = [
  ["start", "Session"],
  ["applications", "Applications"],
  ["resumes", "Resumes"],
  ["work", "Work"],
  ["history", "History"],
  ["settings", "Settings"],
];

export default function App() {
  const [session] = useSession();
  const [tab, setTab] = useState<Tab>("start");
  // Tabs opened once stay open (hidden), so coming back shows what they had at once and they
  // refresh in the background (useOnShow) instead of starting from nothing.
  const [opened, setOpened] = useState<Set<Tab>>(() => new Set(["start"]));
  useEffect(() => {
    setOpened((o) => (o.has(tab) ? o : new Set(o).add(tab)));
  }, [tab]);
  const keep = (t: Tab) => tab === t || opened.has(t);
  const [view, setView] = useState<SettingsView | null>(null);
  const [showDebug, setShowDebug] = useState(false);
  // The current environment can make calls: the dev token is set (local) or signed in (cloud).
  const tokenSet = view?.ready ?? false;
  const environment = view?.settings.environment ?? "local";
  // Whose data the screens show: everything loaded is dropped when this changes.
  const account = accountOf(view);
  const profiles = useProfiles(tokenSet, view?.settings.profile ?? null, account);
  const link = useLinkRequired(tokenSet, account);
  // Back from Settings (a Career-OS link made or removed): the profiles and the lock may have changed.
  const { reload: reloadProfiles } = profiles;
  const { reload: reloadLink } = link;
  useEffect(() => {
    if (tab !== "settings") {
      reloadProfiles();
      reloadLink();
    }
  }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps

  const reload = () => {
    api.getSettings().then((v) => {
      setView(v);
      setShowDebug(v.settings.show_debug);
    });
  };
  useEffect(() => {
    if (!inTauri) return;
    reload();
    const off = onSettingsChanged(reload);
    // The overlay's "All settings…" opens this window on the Settings screen.
    const offOpen = onOpenSettings(() => setTab("settings"));
    return () => {
      off.then((f) => f());
      offOpen.then((f) => f());
    };
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "d") {
        setShowDebug((v) => !v);
        setTab((t) => (t === "debug" ? "start" : "debug"));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!inTauri) {
    return (
      <main className="page">
        <h1>Copilot</h1>
        <p>This UI needs the Rust core: run <code>pnpm --filter @copilot/desktop tauri dev</code>.</p>
      </main>
    );
  }

  // "Open" on the profile's running call (Session and History, "Live now"): take it over here.
  const openLive = async (live: LiveSession) => {
    await api.openSession(live);
    setTab("start");
  };

  const needsToken = !tokenSet && (tab === "resumes" || tab === "applications" || tab === "work" || tab === "history");
  return (
    <main className="page">
      <nav className="tabs">
        <div className="tab-list">
          {TABS.map(([t, label]) => (
            <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
              {label}
            </button>
          ))}
          {showDebug && (
            <button className={tab === "debug" ? "on" : ""} onClick={() => setTab("debug")}>
              Debug
            </button>
          )}
        </div>
        <div className="tab-meta">
          <ProfileSwitch state={profiles.state} running={session.running} onSwitch={profiles.switchTo} />
          <StatusBadge session={session} />
        </div>
      </nav>
      <UpdateBar running={session.running} />
      {/* A profile switch or another account remounts every screen, so each one loads its data. */}
      <ProfileContext.Provider value={profiles.state} key={`${account}/${profiles.state.here ?? "me"}`}>
        {link.locked && tab !== "settings" && tab !== "debug" && <LinkCareerOsFirst onSettings={() => setTab("settings")} />}
        {!link.locked && tab === "start" && (
          <StartScreen
            session={session}
            tokenSet={tokenSet}
            environment={environment}
            settings={view?.settings ?? null}
            onSettingsSaved={reload}
            onSettings={() => setTab("settings")}
            onOpenLive={openLive}
          />
        )}
        {!link.locked && needsToken && <SetTokenFirst environment={environment} onSettings={() => setTab("settings")} />}
        {!link.locked && tokenSet && keep("resumes") && (
          <div hidden={tab !== "resumes"}>
            <Resumes active={tab === "resumes"} />
          </div>
        )}
        {!link.locked && tokenSet && keep("applications") && (
          <div hidden={tab !== "applications"}>
            <Applications active={tab === "applications"} />
          </div>
        )}
        {!link.locked && tokenSet && keep("work") && (
          <div hidden={tab !== "work"}>
            <Work active={tab === "work"} />
          </div>
        )}
        {!link.locked && tokenSet && keep("history") && (
          <div hidden={tab !== "history"}>
            <History
              active={tab === "history"}
              running={session.running}
              liveId={session.running ? session.sessionId : null}
              onOpenLive={openLive}
            />
          </div>
        )}
      </ProfileContext.Provider>
      {tab === "settings" && view && <SettingsScreen view={view} running={session.running} onSaved={reload} />}
      {tab === "debug" && <DebugPanel session={session} />}
    </main>
  );
}

/** Top right: whom the app works for, "Me" or one of the Career-OS users an admin link reads.
 * Locked while a session runs: a session keeps the profile it started with. */
function ProfileSwitch({
  state,
  running,
  onSwitch,
}: {
  state: ProfileState;
  running: boolean;
  onSwitch: (id: string | null) => Promise<void>;
}) {
  const [error, setError] = useState<string | null>(null);
  if (!showSwitch(state.profiles)) return null;
  return (
    <span className="row" title={running ? "End the session to switch" : "Whose applications, resumes and sessions"}>
      <select
        aria-label="Profile"
        value={state.here ?? ""}
        disabled={running}
        onChange={(e) => {
          setError(null);
          onSwitch(e.target.value || null).catch((err) => setError(String(err)));
        }}
      >
        {state.profiles.map((p) => (
          <option key={p.id} value={p.id}>
            {p.me ? `Me (${p.name})` : p.name}
          </option>
        ))}
      </select>
      {error && <span className="warn">{error}</span>}
    </span>
  );
}

/** The updater's state, live: every step the Rust side takes arrives as an event. */
function useUpdate(): UpdateState {
  const [update, setUpdate] = useState<UpdateState>({ state: "none" });
  useEffect(() => {
    if (!inTauri) return;
    let live = false;
    const off = onUpdate((u) => {
      live = true;
      setUpdate(u);
    });
    // The state before this window listened; an event that came first wins.
    api
      .updateStatus()
      .then((u) => {
        if (!live) setUpdate(u);
      })
      .catch(() => {});
    return () => {
      off.then((f) => f());
    };
  }, []);
  return update;
}

/** "Check now": busy from the click until the check's result arrives (shown for at least a
 * moment, so a fast check is still seen), or an error if the check could not start. */
function useCheckNow(update: UpdateState) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [since, setSince] = useState(0);
  // The result: any state after "checking" (the updater always sends both for this check).
  const [sawChecking, setSawChecking] = useState(false);
  useEffect(() => {
    if (!busy) return;
    if (update.state === "checking") {
      setSawChecking(true);
      return;
    }
    if (!sawChecking) return;
    const t = setTimeout(() => setBusy(false), Math.max(0, since + 700 - Date.now()));
    return () => clearTimeout(t);
  }, [busy, update, sawChecking, since]);
  // Never spin forever (a check that hangs is cut off by the server's timeout anyway).
  useEffect(() => {
    if (!busy) return;
    const t = setTimeout(() => setBusy(false), 60_000);
    return () => clearTimeout(t);
  }, [busy]);
  const start = () => {
    setError(null);
    setSawChecking(false);
    setSince(Date.now());
    setBusy(true);
    api.checkUpdates().catch((e) => {
      setBusy(false);
      setError(String(e));
    });
  };
  return { busy, error, start };
}

/** Release builds update themselves (src-tauri/src/release.rs). While an update downloads,
 * waits or installs, this bar under the tabs says what is happening, with its progress. */
function UpdateBar({ running }: { running: boolean }) {
  const update = useUpdate();
  const [error, setError] = useState<string | null>(null);
  // Dismissing hides this state only; the next step shows again.
  const [dismissed, setDismissed] = useState<string | null>(null);
  const key = JSON.stringify(update);
  useEffect(() => setError(null), [update.state]);
  const b = bar(update, running);
  if (b.kind === "none" || dismissed === key) return null;
  const install = () => {
    setError(null);
    api.installUpdate().catch((e) => setError(String(e)));
  };
  return (
    <div className={`update-bar update-${b.tone}`} role="status" aria-live="polite">
      <div className="update-row">
        <span className="update-text">{b.text}</span>
        {error && <span className="update-error">{error}</span>}
        {b.action && (
          <button className="primary small" onClick={install} disabled={!b.action.enabled}>
            {b.action.label}
          </button>
        )}
        {b.dismissible && (
          <button className="link" onClick={() => setDismissed(key)}>
            OK
          </button>
        )}
      </div>
      {b.progress !== undefined && (
        <div className={`update-track${b.progress === null ? " moving" : ""}`}>
          <div className="update-fill" style={b.progress === null ? undefined : { width: `${b.progress}%` }} />
        </div>
      )}
    </div>
  );
}

function StatusBadge({ session }: { session: SessionView }) {
  const b = badge(session);
  return (
    <span className={`badge badge-${b.state}`}>
      <span className="dot" />
      {b.text}
    </span>
  );
}

/** The stage requires a Career-OS link and this account has none: everything but Settings waits. */
function LinkCareerOsFirst({ onSettings }: { onSettings: () => void }) {
  return (
    <p className="callout">
      Please link your Career-OS account to use this app.{" "}
      <button className="link" onClick={onSettings}>
        Open settings
      </button>
    </p>
  );
}

/** Whether the app is locked until a Career-OS link: the server says the stage requires one and
 * the signed-in account has none. Loaded per account; until it answers, nothing is locked (the
 * server refuses the work anyway). */
function useLinkRequired(ready: boolean, account: string) {
  const [conn, setConn] = useState<CareerOsConnection | null>(null);
  const reload = useCallback(async () => {
    if (!ready) return;
    try {
      setConn(await rest.careerOs());
    } catch {
      setConn(null);
    }
  }, [ready]);
  useEffect(() => {
    setConn(null);
    reload();
  }, [ready, account]); // eslint-disable-line react-hooks/exhaustive-deps
  return { locked: ready && isLocked(conn), reload };
}

function SetTokenFirst({ environment, onSettings }: { environment: string; onSettings: () => void }) {
  return (
    <p className="callout">
      {environment === "cloud" ? "Sign in first." : "Set the dev token first."}{" "}
      <button className="link" onClick={onSettings}>
        Open settings
      </button>
    </p>
  );
}

const PLATFORMS = [
  ["zoom", "Zoom"],
  ["meet", "Google Meet"],
  ["teams", "Microsoft Teams"],
  ["other", "Other"],
];

function StartScreen({
  session,
  tokenSet,
  environment,
  settings,
  onSettingsSaved,
  onSettings,
  onOpenLive,
}: {
  session: SessionView;
  tokenSet: boolean;
  environment: string;
  /** The saved settings: the microphone and call audio are picked and tested here. */
  settings: Settings | null;
  onSettingsSaved: () => void;
  onSettings: () => void;
  onOpenLive: (live: LiveSession) => Promise<void>;
}) {
  const [mode, setMode] = useState<Mode>("interview");
  const [answerStyle, setAnswerStyle] = useState<AnswerStyle>(lastAnswerStyle);
  const catalog = useCatalog(tokenSet);
  const [target, setTarget] = useState("");
  const [title, setTitle] = useState("");
  const [platform, setPlatform] = useState("zoom");
  const [consent, setConsent] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [postCall, setPostCall] = useState<string | null>(null);
  const work = useCompanies(tokenSet);
  const [employerId, setEmployerId] = useState("");
  const [agenda, setAgenda] = useState("");
  // The company documents to focus on in this standup or meeting.
  const materials = useMaterials(atWorkMode(mode) && tokenSet ? employerId : "");
  const [materialIds, setMaterialIds] = useState<string[]>([]);
  useEffect(() => setMaterialIds([]), [employerId]);
  const atWork = mode === "standup" || mode === "meeting";
  useEffect(() => {
    if (!employerId && work.employers) setEmployerId(defaultEmployer(work.employers, lastCompany()));
  }, [work.employers]); // eslint-disable-line react-hooks/exhaustive-deps
  const pack = usePackFor(atWork && !session.running ? employerId : "", mode);

  const interviews = upcoming(catalog.interviews ?? []);
  useEffect(() => {
    // Default to the next interview, else the newest application.
    if (target) return;
    if (interviews.length) setTarget(`interview:${interviews[0].id}`);
    else if (catalog.applications?.length) setTarget(`job:${catalog.applications[0].id}`);
  }, [catalog.applications, catalog.interviews]); // eslint-disable-line react-hooks/exhaustive-deps

  // The post-call screen for the session that just ended.
  useEffect(() => {
    if (session.running) setPostCall(null);
    else if (session.stopped === "ended" && session.sessionId) setPostCall(session.sessionId);
  }, [session.stopped, session.sessionId, session.running]);

  const start = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const picked = mode === "interview" ? parseTarget(target, catalog.interviews ?? []) : { job_id: null, interview_id: null };
      const company = atWork && employerId ? employerId : null;
      if (company) rememberCompany(company);
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

  // Ending asks first: summarize the call, or keep the transcript only.
  const [ending, setEnding] = useState(false);
  useEffect(() => {
    if (!session.running) setEnding(false);
  }, [session.running]);
  const endNow = (summarize: boolean) => {
    api.endSession(summarize).catch((e) => {
      setMessage(String(e));
      setEnding(false);
    });
  };

  const endOther = async () => {
    setMessage(null);
    try {
      setMessage(`${await api.endOtherSession()}. You can start now.`);
    } catch (e) {
      setMessage(String(e));
    }
  };

  const running = session.running;
  return (
    <>
    {tokenSet && <LiveNow running={running} liveId={running ? session.sessionId : null} onOpen={onOpenLive} />}
    <section className="panel">
      {!tokenSet && <SetTokenFirst environment={environment} onSettings={onSettings} />}
      <div className="grid">
        <label>Mode</label>
        <div className="segmented caps">
          {(["interview", "standup", "meeting"] as Mode[]).map((m) => (
            <button
              key={m}
              className={mode === m ? "on" : ""}
              onClick={() => {
                setMode(m);
                if (running) api.setMode(m).catch(() => undefined);
              }}
            >
              {m}
            </button>
          ))}
        </div>

        <label>Detailed answer</label>
        <div>
          <div className="segmented">
            {ANSWER_STYLES.map(([style, label]) => (
              <button
                key={style}
                className={answerStyle === style ? "on" : ""}
                onClick={() => {
                  setAnswerStyle(style);
                  rememberAnswerStyle(style);
                  if (running) api.setAnswerStyle(style).catch(() => undefined);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="note">
            {answerStyle === "paragraph"
              ? "The talking points stay short; the detailed answer is a connected paragraph you can say as it is."
              : "The talking points stay short; the detailed answer is more lines, going deeper than the points."}
            {running ? " A change applies to the next answers." : ""}
          </p>
        </div>

        {atWork ? (
          <>
            <label>Company</label>
            <div>
              <div className="inline">
                <select value={employerId} disabled={running} onChange={(e) => setEmployerId(e.target.value)}>
                  <option value="">None (no company's notes)</option>
                  {work.employers
                    ?.filter((e) => e.status === "active")
                    .map((e) => (
                      <option key={e.id} value={e.id}>
                        {e.name}
                        {e.current ? "" : " (past)"}
                      </option>
                    ))}
                </select>
                {employerId && <span className={`chip chip-${packLabel(pack).tone}`}>{packLabel(pack).text}</span>}
              </div>
              {work.employers?.length === 0 && <p className="note">No company yet: add the one you work at under Work.</p>}
              {work.error && <p className="note warn">{work.error}</p>}
            </div>
            {employerId && (
              <>
                <label>Focus on</label>
                <MaterialPicker materials={materials} selected={materialIds} disabled={running} onChange={setMaterialIds} />
              </>
            )}
          </>
        ) : null}
        <label className={atWork ? "hidden" : ""}>Call for</label>
        <div className={atWork ? "hidden" : ""}>
          <div className="inline">
          <select value={target} disabled={running || mode !== "interview"} onChange={(e) => setTarget(e.target.value)}>
            <option value="">No application (your base resume only)</option>
            {interviews.length > 0 && (
              <optgroup label="Interviews">
                {interviews.map((i) => (
                  <option key={i.id} value={`interview:${i.id}`}>
                    {[i.company, i.title, roundLabel(i.round), when(i.starts_at)].filter(Boolean).join(" · ")}
                    {packNote(i.pack)}
                  </option>
                ))}
              </optgroup>
            )}
            {(catalog.applications?.length ?? 0) > 0 && (
              <optgroup label="Applications">
                {catalog.applications?.map((a) => (
                  <option key={a.id} value={`job:${a.id}`}>
                    {applicationOption(a)}
                    {packNote(a.pack)}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <button className="link" onClick={catalog.reload} disabled={!tokenSet}>
            Refresh
          </button>
          </div>
          {catalog.error && <p className="note warn">{catalog.error}</p>}
          {catalog.applications?.length === 0 && <p className="note">No applications yet: add one under Applications.</p>}
          {mode === "interview" && <p className="note">The answers use the application's job description and its paired resume only.</p>}
        </div>

        <label>Meeting title</label>
        <input value={title} disabled={running} onChange={(e) => setTitle(e.target.value)} placeholder="Optional" />

        {mode === "meeting" && (
          <>
            <label>Agenda</label>
            <textarea
              rows={3}
              value={agenda}
              disabled={running}
              onChange={(e) => setAgenda(e.target.value)}
              placeholder="Optional: the topic or agenda, so answers start from the right systems and decisions"
            />
          </>
        )}

        <label>Call app</label>
        <select value={platform} disabled={running} onChange={(e) => setPlatform(e.target.value)}>
          {PLATFORMS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>

        <label>Transcript</label>
        <div>
          <label className="check">
            <input type="checkbox" checked={consent} disabled={running} onChange={(e) => setConsent(e.target.checked)} />
            Store the transcript after the call (everyone on the call has agreed)
          </label>
          <p className="note">
            {consent
              ? "Both sides are transcribed live and the transcript is stored, encrypted. No audio is recorded."
              : "Both sides are transcribed live for the answers, but no words are stored; the summary covers only the call's length and the questions' types."}
          </p>
        </div>

        {settings && <DevicePickers settings={settings} disabled={running || busy} onSaved={onSettingsSaved} />}
      </div>

      <div className="actions">
        {!running ? (
          <button className="primary" disabled={busy || !tokenSet} onClick={start}>
            Start session
          </button>
        ) : (
          <button className="danger" disabled={ending} onClick={() => setEnding(true)}>
            End session…
          </button>
        )}
        <button onClick={() => api.showOverlay(true)}>Show overlay</button>
      </div>

      {running && ending && (
        <div className="end-choice">
          <p>How should this call be saved?</p>
          <p className="note">
            {mode === "standup"
              ? "Summarize if you will need this later: the standup report and your commitments feed the next standup."
              : "Summarize if you will need this context later, as with standups and recurring meetings. Otherwise keep the transcript only."}
            {!consent && " The transcript is not being stored for this call, so without a summary only its length is kept."}
          </p>
          <div className="actions">
            <button className="primary" onClick={() => endNow(true)}>
              Summarize and end
            </button>
            <button onClick={() => endNow(false)}>{consent ? "End, keep the transcript only" : "End without a summary"}</button>
            <button className="link" onClick={() => setEnding(false)}>
              Keep going
            </button>
          </div>
        </div>
      )}

      {session.stopped === "session_active" && (
        <p className="callout">
          Another session is active for this user. <button className="link" onClick={endOther}>End the other session</button>
        </p>
      )}
      {session.stopped === "no_minutes" && <p className="callout">No minutes left on the plan.</p>}
      {session.stopped === "outdated" && (
        <p className="callout">This version is older than the server supports. The app is updating; it restarts by itself.</p>
      )}
      {session.stopped === "unauthorized" && (
        <p className="callout">
          {environment === "cloud"
            ? "The gateway rejected the sign-in. Sign in again under Settings."
            : "The gateway rejected the dev token. Check it in Settings."}
        </p>
      )}
      {message && <p className="note warn">{message}</p>}
      {postCall && !running && (
        <div className="postcall">
          <SessionDetail sessionId={postCall} onClose={() => setPostCall(null)} />
        </div>
      )}
    </section>
    </>
  );
}

const LAST_COMPANY = "copilot.lastCompany";
const LAST_ANSWER_STYLE = "copilot.answerStyle";

const ANSWER_STYLES: [AnswerStyle, string][] = [
  ["bullets", "Bulleted"],
  ["paragraph", "Paragraph"],
];

function lastAnswerStyle(): AnswerStyle {
  try {
    return localStorage.getItem(LAST_ANSWER_STYLE) === "paragraph" ? "paragraph" : "bullets";
  } catch {
    return "bullets";
  }
}

function rememberAnswerStyle(style: AnswerStyle) {
  try {
    localStorage.setItem(LAST_ANSWER_STYLE, style);
  } catch {
    // a convenience only
  }
}

function lastCompany(): string | null {
  try {
    return localStorage.getItem(LAST_COMPANY);
  } catch {
    return null;
  }
}

function rememberCompany(id: string) {
  try {
    localStorage.setItem(LAST_COMPANY, id);
  } catch {
    // a convenience only
  }
}

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

/// Picking a company asks for its pack, so the session starts on a pack built from the
/// latest notes; a call never waits for it (the gateway renders one itself if needed).
function atWorkMode(mode: Mode): boolean {
  return mode === "standup" || mode === "meeting";
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

function MaterialPicker({
  materials,
  selected,
  disabled,
  onChange,
}: {
  materials: { documents: CompanyDocument[] | null; error: string | null };
  selected: string[];
  disabled: boolean;
  onChange: (ids: string[]) => void;
}) {
  const { documents, error } = materials;
  const toggle = (id: string, on: boolean) => onChange(on ? [...selected, id] : selected.filter((s) => s !== id));
  return (
    <div>
      {error && <p className="note warn">{error}</p>}
      {documents === null && !error && <p className="note">Loading the company's material…</p>}
      {documents?.length === 0 && <p className="note">No material with approved facts yet: upload or paste some under Work, Material.</p>}
      {documents && documents.length > 0 && (
        <ul className="material-pick">
          {documents.map((d) => (
            <li key={d.id}>
              <label className="check">
                <input type="checkbox" checked={selected.includes(d.id)} disabled={disabled} onChange={(e) => toggle(d.id, e.target.checked)} />
                {d.name} <span className="muted">{d.approved_proposals === 1 ? "1 fact" : `${d.approved_proposals} facts`}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <p className="note">
        Optional. Answers read the chosen material first; everything else the company has is still searched for each question.
      </p>
    </div>
  );
}

function usePackFor(employerId: string, mode: Mode): PackState | null {
  const [state, setState] = useState<PackState | null>(null);
  useEffect(() => {
    setState(null);
    if (!employerId || (mode !== "standup" && mode !== "meeting")) return;
    let stop = false;
    let tries = 0;
    const poll = async () => {
      try {
        const asked = await rest.ensurePack(employerId, mode);
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

function packNote(pack: { available: boolean } | null | undefined): string {
  if (!pack) return " (pack building)";
  return pack.available ? "" : " (pack expired)";
}

/** The overlay's hotkeys, each tied to its settings field and action, in the order shown. */
const HOTKEYS: { key: keyof Settings; action: HotkeyAction; label: string; hint?: string }[] = [
  { key: "hotkey", action: "force_question", label: "Answer the last thing said", hint: "When detection missed a question. Example: CommandOrControl+Shift+Space." },
  { key: "toggle_overlay_hotkey", action: "toggle_overlay", label: "Hide or show the overlay", hint: "Hides and shows the overlay instantly; the session keeps running." },
  { key: "click_through_hotkey", action: "click_through", label: "Click-through on or off", hint: "Lets clicks pass to the window underneath." },
  { key: "click_through_off_hotkey", action: "click_through_off", label: "Turn click-through off", hint: "The way back. Always works: if cleared or mistyped it falls back to CommandOrControl+Shift+G." },
  { key: "opacity_up_hotkey", action: "opacity_up", label: "Overlay more opaque" },
  { key: "opacity_down_hotkey", action: "opacity_down", label: "Overlay more see-through" },
];

/** A rough normalization matching the Rust parser (hotkeys.rs), for live duplicate hints as the
 * user types. The saved settings are also checked on the Rust side (view.hotkey_review), which
 * additionally catches keys that do not parse. */
function normAccel(a: string): string | null {
  const parts = a.trim().split("+").map((p) => p.trim().toUpperCase()).filter(Boolean);
  if (!parts.length) return null;
  const mods = new Set<string>();
  let key = "";
  for (const p of parts) {
    if (["CONTROL", "CTRL", "COMMANDORCONTROL", "COMMANDORCTRL", "CMDORCTRL", "CMDORCONTROL"].includes(p)) mods.add("CTRL");
    else if (["COMMAND", "CMD", "SUPER"].includes(p)) mods.add("SUPER");
    else if (["ALT", "OPTION"].includes(p)) mods.add("ALT");
    else if (p === "SHIFT") mods.add("SHIFT");
    else key = p;
  }
  if (!key) return null;
  return [...[...mods].sort(), key].join("+");
}

/** The settings keys that share a hotkey with another, from the live draft. */
function duplicateHotkeys(draft: Settings): Set<keyof Settings> {
  const seen = new Map<string, (keyof Settings)[]>();
  for (const { key } of HOTKEYS) {
    const n = normAccel(String(draft[key] ?? ""));
    if (!n) continue;
    seen.set(n, [...(seen.get(n) ?? []), key]);
  }
  const dup = new Set<keyof Settings>();
  for (const keys of seen.values()) if (keys.length > 1) keys.forEach((k) => dup.add(k));
  return dup;
}

/** The Session tab's microphone and call-audio rows: picked before each call, each with its
 * Test button, and saved to the settings the moment one is picked. */
function DevicePickers({ settings, disabled, onSaved }: { settings: Settings; disabled: boolean; onSaved: () => void }) {
  const [devices, setDevices] = useState<{ inputs: DeviceInfo[]; outputs: DeviceInfo[] } | null>(null);
  const [mic, setMic] = useState(settings.mic_device);
  const [output, setOutput] = useState(settings.output_device);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.listDevices().then(setDevices);
  }, []);
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
  const select = (list: DeviceInfo[] | undefined, value: string | null, key: "mic_device" | "output_device") => (
    <select value={value ?? ""} disabled={disabled} onChange={(e) => pick(key, e.target.value || null)}>
      <option value="">System default</option>
      {list?.map((d) => (
        <option key={d.id} value={d.id}>
          {d.name}
          {d.is_default ? " (default)" : ""}
        </option>
      ))}
    </select>
  );
  return (
    <>
      <label>Microphone</label>
      <DeviceTest part="mic" micDevice={mic} outputDevice={output} disabled={disabled}>
        {select(devices?.inputs, mic, "mic_device")}
      </DeviceTest>

      <label>Call audio from</label>
      <div>
        <DeviceTest part="call" micDevice={mic} outputDevice={output} disabled={disabled}>
          {select(devices?.outputs, output, "output_device")}
        </DeviceTest>
        <p className="note">The speakers or headset the call plays on; captured in loopback.</p>
        {error && <p className="note warn">{error}</p>}
      </div>
    </>
  );
}

function SettingsScreen({ view, running, onSaved }: { view: SettingsView; running: boolean; onSaved: () => void }) {
  const settings = view.settings;
  const tokenSet = view.dev_token_set;
  const update = useUpdate();
  const [draft, setDraft] = useState(settings);
  const [token, setToken] = useState("");
  const [host, setHost] = useState("");
  const [signingIn, setSigningIn] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const dupes = duplicateHotkeys(draft);

  const save = async () => {
    setMessage(null);
    try {
      await api.saveSettings(draft, token ? token : null);
      setToken("");
      setMessage(running ? "Saved. Echo cancellation applies to the next session." : "Saved.");
      onSaved();
    } catch (e) {
      setMessage(String(e));
    }
  };
  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setDraft({ ...draft, [k]: v });
  const unsaved = JSON.stringify(draft) !== JSON.stringify(settings);
  const signIn = async () => {
    setMessage(null);
    setSigningIn(true);
    try {
      const email = await api.signIn();
      setMessage(`Signed in${email ? ` as ${email}` : ""}.`);
      onSaved();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setSigningIn(false);
    }
  };
  const signOut = async () => {
    setMessage(null);
    try {
      await api.signOut();
      setMessage("Signed out.");
      onSaved();
    } catch (e) {
      setMessage(String(e));
    }
  };
  const release = view.release;
  const updates = view.updates;
  const updateLine = settingsLine(update);
  const check = useCheckNow(update);
  return (
    <>
      <section className="panel settings">
        <h3>App</h3>
        <div className="grid">
        <label>Version</label>
        <div>
          <span>{view.version}</span>
          {updates.enabled ? (
            <div className="check-row">
              <button
                className="small"
                onClick={check.start}
                disabled={check.busy || update.state === "downloading" || update.state === "installing"}
              >
                {check.busy && <span className="spinner" />}
                {check.busy ? "Checking…" : "Check now"}
              </button>
              <span className={`note check-result${check.error || update.state === "check_failed" ? " bad" : ""}`} aria-live="polite">
                {check.busy
                  ? "Asking the release server for a newer version…"
                  : (check.error ??
                    updateLine ??
                    `Last checked ${updates.checked_at ? ago(updates.checked_at) : "not yet"}. Checks at start and every four hours.`)}
              </span>
            </div>
          ) : (
            <p className="note">A development build: it does not update itself.</p>
          )}
        </div>

        {updates.enabled && (
          <>
            <label>Updates</label>
            <div>
              <select value={draft.update_mode} onChange={(e) => set("update_mode", e.target.value as Settings["update_mode"])}>
                <option value="auto">Install by themselves</option>
                <option value="ask">Ask me first</option>
              </select>
              <p className="note">
                Never during a call. Automatic updates install as soon as they are downloaded if you have not used the app
                yet, otherwise after two minutes without a click or key press; the app then restarts and says “Updated to …”.
              </p>
            </div>
          </>
        )}
        </div>

        <h3>Account</h3>
        <div className="grid">
        {!release && (
          <>
            <label>Environment</label>
            <div className="row">
              <label className="check">
                <input type="radio" checked={draft.environment === "local"} onChange={() => set("environment", "local")} />
                Local (this machine, dev token)
              </label>
              <label className="check">
                <input type="radio" checked={draft.environment === "cloud"} onChange={() => set("environment", "cloud")} />
                Cloud (Google sign-in)
              </label>
            </div>
          </>
        )}

        {!release && draft.environment === "local" && (
          <>
            <label>Gateway URL</label>
            <input value={draft.gateway_url} onChange={(e) => set("gateway_url", e.target.value)} />

            <label>API URL</label>
            <div>
              <input value={draft.api_url} onChange={(e) => set("api_url", e.target.value)} />
              <p className="note">The Copilot API's /v1 root, for example http://127.0.0.1:8080/v1. The same token signs in to both.</p>
            </div>

            <label>Dev token</label>
            <div>
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={tokenSet ? "Saved (type to replace)" : "COPILOT_DEV_TOKEN from the .env"}
              />
              <p className="note">
                Kept in the Windows Credential Manager, not in the settings file. Local only: the services refuse it in the cloud.
              </p>
            </div>
          </>
        )}

        {!release && draft.environment === "cloud" && (
          <>
            <label>Cloud host</label>
            <div className="row">
              <input
                value={host}
                onChange={(e) => setHost(e.target.value)}
                placeholder="The VM's IP (terraform output external_ip)"
              />
              <button
                onClick={() => {
                  const urls = cloudUrlsFor(host);
                  if (urls) setDraft({ ...draft, ...urls });
                }}
              >
                Fill URLs
              </button>
            </div>

            <label>Gateway URL</label>
            <input value={draft.cloud_gateway_url} onChange={(e) => set("cloud_gateway_url", e.target.value)} />

            <label>API URL</label>
            <input value={draft.cloud_api_url} onChange={(e) => set("cloud_api_url", e.target.value)} />
          </>
        )}

        {(release || draft.environment === "cloud") && (
          <>
            <label>Account</label>
            <div>
              {view.signed_in_as !== null ? (
                <div className="row">
                  <span>Signed in{view.signed_in_as ? ` as ${view.signed_in_as}` : ""}.</span>
                  <button onClick={signOut}>Sign out</button>
                </div>
              ) : (
                <div className="row">
                  <button className="primary" onClick={signIn} disabled={signingIn || unsaved || !settings.cloud_api_url}>
                    {signingIn ? "Waiting for the browser…" : "Sign in"}
                  </button>
                  {unsaved && <span className="note">Save first.</span>}
                </div>
              )}
              <p className="note">
                Opens your browser at the stage's sign-in page. The sign-in stays in the Windows Credential Manager and renews
                itself, also during a call.
              </p>
            </div>
          </>
        )}
        </div>

        <h3>During calls</h3>
        <div className="grid">
        <label>Echo cancellation</label>
        <label className="check">
          <input type="checkbox" checked={draft.echo_cancellation} onChange={(e) => set("echo_cancellation", e.target.checked)} />
          Remove the call's sound from the microphone (needed without headphones)
        </label>

        <label>Hotkeys</label>
        <div className="hotkeys">
          {HOTKEYS.map(({ key, action, label, hint }) => {
            const invalid = view.hotkey_review.invalid.find((i) => i.action === action);
            const clash = dupes.has(key);
            return (
              <div className="hotkey" key={key}>
                <span className="hotkey-label">{label}</span>
                <input
                  value={String(draft[key] ?? "")}
                  spellCheck={false}
                  placeholder="Unbound"
                  className={clash || invalid ? "bad" : ""}
                  onChange={(e) => set(key, e.target.value as Settings[typeof key])}
                />
                {hint && <span className="note hotkey-hint">{hint}</span>}
                {clash && <span className="note warn">Shares a key with another action.</span>}
                {invalid && <span className="note warn">Not a valid key ({invalid.error}).</span>}
              </div>
            );
          })}
          <p className="note">
            Each action can have its own key, in Tauri's accelerator form (e.g. CommandOrControl+Shift+H). Leave one blank to unbind
            it. Conflicts are flagged here and re-checked when you save.
          </p>
        </div>
        </div>

        <h3>Advanced</h3>
        <div className="grid">
        <label>Debug panel</label>
        <label className="check">
          <input type="checkbox" checked={draft.show_debug} onChange={(e) => set("show_debug", e.target.checked)} />
          Show (also Ctrl+Shift+D)
        </label>
        </div>

        <div className="actions save-bar">
          <button className="primary" onClick={save} disabled={!unsaved && !token}>
            Save
          </button>
          <span className="note">{message ?? (unsaved || token ? "Unsaved changes." : "")}</span>
        </div>
      </section>
      <section className="panel settings">
        <CareerOsSettings key={accountOf(view)} ready={view.ready} />
      </section>
      {view.ready && <DeleteAccount />}
    </>
  );
}

function usePoll(ms: number): LiveStatus | null {
  const [live, setLive] = useState<LiveStatus | null>(null);
  useEffect(() => {
    const t = setInterval(() => api.liveStatus().then(setLive), ms);
    return () => clearInterval(t);
  }, [ms]);
  return live;
}

function Meter({ label, rms, speech }: { label: string; rms: number; speech: number }) {
  const db = rms > 0 ? Math.max(0, Math.min(1, (20 * Math.log10(rms) + 60) / 60)) : 0;
  return (
    <div className="meter">
      <span>{label}</span>
      <div className="bar">
        <div style={{ width: `${db * 100}%` }} />
      </div>
      <span className="muted">speech {(speech * 100).toFixed(0)}%</span>
    </div>
  );
}

function Meters() {
  const live = usePoll(200);
  if (!live?.levels) return <p className="note">Levels show while a session runs.</p>;
  const l = live.levels;
  return (
    <div className="meters">
      <Meter label="Call (them)" rms={l.them_rms} speech={l.them_speech} />
      <Meter label="Mic (you, after AEC)" rms={l.me_rms} speech={l.me_speech} />
    </div>
  );
}

function ms(v: number | null | undefined): string {
  return v == null ? "–" : `${v}`;
}

function DebugPanel({ session }: { session: SessionView }) {
  const live = usePoll(500);
  const latency = Object.values(session.latency);
  return (
    <section className="panel debug">
      <h3>Audio levels</h3>
      <Meters />
      <h3>Latency per question (ms)</h3>
      <table>
        <thead>
          <tr>
            <th>Question</th>
            <th>From</th>
            <th title="Client: speech end to question.detected">detected</th>
            <th title="Client: speech end to the first fast answer word">first word</th>
            <th title="Client: speech end to the first deep answer word">deep word</th>
            <th title="Gateway latency_ms of the fast pass">gw turn_end / detector / first_token / first_delta / total</th>
          </tr>
        </thead>
        <tbody>
          {latency.map((l) => {
            const g = l.fast_gateway;
            return (
              <tr key={l.question_id}>
                <td className="clip" title={l.text}>
                  {l.text}
                </td>
                <td>{l.anchor.replace("_", " ")}</td>
                <td>{ms(l.detected_ms)}</td>
                <td className={l.fast_first_word_ms != null && l.fast_first_word_ms > 1500 ? "warn" : ""}>{ms(l.fast_first_word_ms)}</td>
                <td>{ms(l.deep_first_word_ms)}</td>
                <td>{g ? [g.turn_end_ms, g.detector_ms, g.first_token_ms, g.first_delta_ms, g.total_ms].map(ms).join(" / ") : "–"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h3>Detected questions</h3>
      <ul className="plain">
        {session.questions.map((q) => (
          <li key={q.id} className={q.cancelled ? "muted strike" : ""}>
            <code>{q.type}</code> {q.confidence.toFixed(2)} {q.speculative ? "(speculative)" : ""} {q.cancelled ? "(cancelled)" : ""} {q.text}
          </li>
        ))}
      </ul>

      <h3>
        Transcript <span className="muted">them {session.speaking.them ? "●" : "○"} · you {session.speaking.me ? "●" : "○"}</span>
      </h3>
      <ul className="plain transcript">
        {session.transcript.slice(-40).map((t) => (
          <li key={t.key} className={t.final ? "" : "muted"}>
            <code>{t.channel}</code> {t.text}
          </li>
        ))}
      </ul>

      <h3>Connection</h3>
      {live?.stats && (
        <p className="note">
          session {live.session_id ?? "–"} · connects {live.stats.connects} · resumes {live.stats.resumes} · frames sent{" "}
          {live.stats.frames_sent}, resent {live.stats.frames_resent}, dropped {live.stats.frames_dropped} · pings {live.stats.pings_sent} ·
          closes {live.stats.close_codes.map((c) => c ?? "drop").join(", ") || "none"}
          {live.levels && ` · capture underruns them ${live.levels.them_underruns}, me ${live.levels.me_underruns}`}
        </p>
      )}
      <pre className="log">{session.log.join("\n")}</pre>
    </section>
  );
}
