import { useViewPrefs } from "./viewPreferences";
import { SessionNoteDrafts } from "./SessionNoteDrafts";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
// The main window: the app shell (Shell.tsx) around the Call screen, Interview prep, Work,
// History, Settings, and the debug panel (hidden unless turned on in the settings, or with
// Ctrl+Shift+D). Signing in and the Career-OS link, when the stage requires one, come first:
// the Call screen walks through them, and the other screens wait.
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, inTauri, onOpenCall, onOpenSettings, onSettingsChanged, onUpdate, onWorkspaceWindowPrefs, rest, type CareerOsConnection, type LiveSession, type SettingsView, type UpdateState } from "./api";
import { QuitNotification, ServerNotification, UpdateNotification } from "./AppNotifications";
import { Applications, CatalogProvider } from "./Applications";
import { DataActivity, useDataActivity } from "./usePolling";
import { MeetingPreparation } from "./MeetingPreparation";
import { CallScreen } from "./CallScreen";
import { useSession } from "./session";
import { startVoice } from "./voice";
import { startScreenHost } from "./screen";
import { HostControlRequests } from "./screenui";
import { Shell, type Tab, type WorkspaceWindowActions } from "./Shell";
import { ProfileContext, accountOf, isLocked, useProfiles } from "./profiles";
import { Button, EmptyState, SkeletonList, ToastProvider } from "./ui";
import { PageHeadingActive } from "./ui/Layout";
import { CoRubyWordmark } from "./ui/RubyMark";
import { Icon } from "./ui/Icon";
import { type AskEntry, type AskScope } from "./Ask";
import { type PracticeEntry } from "./Practice";
import { Work } from "./Work";

// Screens not needed at startup load when first opened: History (with the call replay, which
// brings the overlay's components), Settings and the debug panel.
const History = lazy(() => import("./History").then((m) => ({ default: m.History })));
const SettingsScreen = lazy(() => import("./SettingsScreen").then((m) => ({ default: m.SettingsScreen })));
const DebugPanel = lazy(() => import("./Debug").then((m) => ({ default: m.DebugPanel })));

export function useWorkspaceWindowOpacity() {
  useEffect(() => {
    if (!inTauri) return;
    let disposed = false;
    let revision = 0;
    let unlisten: (() => void) | undefined;
    const apply = (opacity: number) => {
      if (!disposed && Number.isFinite(opacity)) document.documentElement.style.setProperty("--workspace-opacity", String(Math.max(0.1, Math.min(1, opacity))));
    };
    const load = async () => {
      try {
        unlisten = await onWorkspaceWindowPrefs(prefs => { revision++; apply(prefs.opacity); });
      } catch { /* The initial preferences can still be loaded if events are unavailable. */ }
      if (disposed) { unlisten?.(); return; }
      const initialRevision = revision;
      try {
        const prefs = await api.windowPrefs();
        if (revision === initialRevision) apply(prefs?.opacity);
      } catch { /* Keep the readable default until the next preference change. */ }
    };
    void load();
    return () => { disposed = true; unlisten?.(); document.documentElement.style.removeProperty("--workspace-opacity"); };
  }, []);
}

export default function App() {
  useWorkspaceWindowOpacity();
  const { textScale } = useViewPrefs();
  useEffect(() => {
    document.documentElement.style.setProperty("--app-text-scale", String(textScale));
    return () => { document.documentElement.style.removeProperty("--app-text-scale"); };
  }, [textScale]);
  const [session, dispatch] = useSession();
  // Guest seats' voice lives in this window alone (voice.ts); the overlay drives it by events.
  useEffect(() => (inTauri ? startVoice() : undefined), []);
  // Sharing the screen runs in the window where the host clicked (screen.ts).
  useEffect(() => (inTauri ? startScreenHost() : undefined), []);
  const [tab, setTab] = useState<Tab>("start");
  const contextKey = useRef("");
  const [practiceEntry, setPracticeEntry] = useState<(PracticeEntry & { owner: string }) | null>(null);
  useEffect(() => { const open = (event: Event) => { setPracticeEntry({ ...(event as CustomEvent).detail, n: Date.now(), owner: contextKey.current }); setTab("applications"); }; window.addEventListener("copilot:open-practice", open); return () => window.removeEventListener("copilot:open-practice", open); }, []);
  const [askEntry, setAskEntry] = useState<(AskEntry & { owner: string }) | null>(null);
  useEffect(() => { const open = (event: Event) => { setAskEntry({ scope: (event as CustomEvent<AskScope>).detail, n: Date.now(), owner: contextKey.current }); setTab("work"); }; window.addEventListener("copilot:open-ask", open); return () => window.removeEventListener("copilot:open-ask", open); }, []);
  useEffect(() => {
    if (!inTauri) return;
    let disposed = false;
    const accept = async () => {
      const kind = await invoke<"start" | "end" | null>("background_take_accepted").catch(() => null);
      if (disposed || !kind) return;
      await windowActions.current?.expand();
      setTab("start");
      if (kind === "end") window.dispatchEvent(new Event("copilot:review-end-call"));
    };
    const off = listen("copilot://call-accepted", () => void accept());
    void off.then(() => { if (!disposed) void accept(); });
    return () => { disposed = true; void off.then(stop => stop()); };
  }, []);
  const tabRef = useRef(tab);
  tabRef.current = tab;
  const windowActions = useRef<WorkspaceWindowActions | null>(null);
  const registerWindowActions = useCallback((actions: WorkspaceWindowActions | null) => { windowActions.current = actions; }, []);
  // Tabs opened once stay open (hidden), so coming back shows what they had at once and they
  // refresh in the background (useOnShow) instead of starting from nothing.
  const [opened, setOpened] = useState<Set<Tab>>(() => new Set(["start"]));
  useEffect(() => {
    setOpened((o) => (o.has(tab) ? o : new Set(o).add(tab)));
  }, [tab]);
  const keep = (t: Tab) => tab === t || opened.has(t);
  const [view, setView] = useState<SettingsView | null>(null);
  const [showDebug, setShowDebug] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  // Interview context and the application or shared resume to open in Interview prep.
  const [preset, setPreset] = useState<{ target: string; n: number; meetingTitle?: string; mode?: "interview" | "meeting" } | null>(null);
  const [baseResumeRequest, setBaseResumeRequest] = useState(0);
  const [focusApp, setFocusApp] = useState<{ id: string; n: number } | null>(null);
  // The current environment can make calls: the dev token is set (local) or signed in (cloud).
  const tokenSet = view?.ready ?? false;
  const environment = view?.settings.environment ?? "local";
  // Whose data the screens show: everything loaded is dropped when this changes.
  const account = accountOf(view);
  const clearProfileContext = useCallback(() => {
    if (!session.running) dispatch({ type: "reset", running: false });
    setPreset(null);
    setAskEntry(null);
    setPracticeEntry(null);
    setFocusApp(null);
    setBaseResumeRequest(0);
  }, [session.running, dispatch]);
  const profiles = useProfiles(tokenSet, view?.settings.profile ?? null, account, clearProfileContext);
  contextKey.current = `${account}/${profiles.state.here ?? "me"}`;
  const link = useLinkRequired(tokenSet, account);
  const update = useUpdate();
  const check = useCheckNow(update);
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
    let disposed = false;
    let navigating = false;
    let destination: Tab = "start";
    const navigate = async (next: Tab | ((current: Tab) => Tab)) => {
      destination = typeof next === "function" ? next(navigating ? destination : tabRef.current) : next;
      if (navigating) return;
      navigating = true;
      try {
        // Keep compact content mounted until its native bounds and input region are restored.
        // A second request during expansion updates the destination without racing the resize.
        const expanded = await (windowActions.current?.expand() ?? true);
        if (expanded && !disposed) setTab(destination);
      } catch { /* A failed expansion keeps the current usable page and its recovery controls. */ }
      finally { navigating = false; }
    };
    // The overlay's "All settings…" opens this window on the Settings screen.
    const offOpen = onOpenSettings(() => { void navigate("settings"); });
    const offCall = onOpenCall(() => { void navigate("start"); });
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "d") {
        e.preventDefault();
        setShowDebug((v) => !v);
        void navigate((current) => current === "debug" ? "start" : "debug");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      disposed = true;
      window.removeEventListener("keydown", onKey);
      off.then((f) => f());
      offOpen.then((f) => f());
      offCall.then((f) => f());
    };
  }, []);

  if (!inTauri) {
    return (
      <main className="shell-content">
        <h1><CoRubyWordmark /></h1>
        <p>This UI needs the Rust core: run <code>pnpm --filter @copilot/desktop tauri dev</code>.</p>
      </main>
    );
  }

  // "Open" on the profile's running call (Call and History, "Live now"): take it over here.
  const openLive = async (live: LiveSession) => {
    await api.openSession(live);
    setTab("start");
  };
  const switchProfile = (id: string | null) => {
    setProfileError(null);
    profiles.switchTo(id).catch((err) => setProfileError(String(err)));
  };
  const locked = link.locked;
  // A screen that needs the account: before sign-in, or while the Career-OS link is missing,
  // it says what to do first and points at the Call screen's setup.
  const gate = (body: ReactNode) => {
    if (!tokenSet) {
      return (
        <EmptyState icon={<Icon name="arrow" size={28} />} title={environment === "cloud" || view?.release ? "Sign in first" : "Set the dev token first"} action={<Button variant="primary" onClick={() => setTab("start")}>Go to setup</Button>}>
          This screen shows your data once you are signed in.
        </EmptyState>
      );
    }
    if (locked) {
      return (
        <EmptyState icon={<Icon name="arrow" size={28} />} title="Link HubRuby first" action={<Button variant="primary" onClick={() => setTab("start")}>Go to setup</Button>}>
          This stage needs your HubRuby account linked before the app can work for you.
        </EmptyState>
      );
    }
    return body;
  };
  const page = (t: Tab, body: ReactNode, wide = false) =>
    keep(t) && (
      <div hidden={tab !== t} className={wide ? "shell-content shell-content-wide" : "shell-content"}>
        <DataActivity.Provider value={tab === t}><PageHeadingActive.Provider value={tab === t && t !== "start"}><Suspense fallback={<SkeletonList rows={3} />}>{body}</Suspense></PageHeadingActive.Provider></DataActivity.Provider>
      </div>
    );

  return (
    <ToastProvider persistent={<><QuitNotification /><ServerNotification /><UpdateNotification update={update} running={session.running} /></>}>
      <HostControlRequests s={session} />
      <Shell
        tab={tab}
        onTab={setTab}
        onWindowActionsChanged={registerWindowActions}
        session={session}
        profiles={profiles.state}
        onSwitchProfile={switchProfile}
        profileError={profileError}
        version={view?.version ?? null}
        showDebug={showDebug}
      >
        {/* A profile switch or another account remounts every screen, so each one loads its data. */}
        <ProfileContext.Provider value={profiles.state} key={`${account}/${profiles.state.here ?? "me"}`}>
          <SessionNoteDrafts>
          <CatalogProvider enabled={tokenSet && !locked && (tab === "start" || tab === "applications")}>
          {page(
            "start",
            <CallScreen
              session={session}
              dispatch={dispatch}
              tokenSet={tokenSet}
              environment={environment}
              release={Boolean(view?.release)}
              signedInAs={view?.signed_in_as ?? null}
              settings={view?.settings ?? null}
              careerOs={link.conn}
              locked={locked}
              onCareerOsChanged={() => {
                reloadLink();
                reloadProfiles();
              }}
              onSettingsSaved={reload}
              onSettings={() => setTab("settings")}
              onResumes={() => { setBaseResumeRequest(n => n + 1); setTab("applications"); }}
              onApplications={(id) => { if (id) setFocusApp({ id, n: Date.now() }); setTab("applications"); }}
              onWork={() => setTab("work")}
              onHistory={() => setTab("history")}
              onOpenLive={openLive}
              preset={preset}
              active={tab === "start"}
            />,
          )}
          {page("calendar", gate(<MeetingPreparation standalone onChoose={(title, applicationId) => {
            setPreset({ target: applicationId ? `job:${applicationId}` : "", meetingTitle: title, mode: applicationId ? "interview" : "meeting", n: Date.now() });
            setTab("start");
          }} />), true)}
          {page(
            "applications",
            gate(
              <Applications
                active={tab === "applications"}
                practiceEntry={practiceEntry?.owner === contextKey.current ? practiceEntry : null}
                running={session.running}
                focus={focusApp}
                baseResumeRequest={baseResumeRequest}
                onStartInterview={(id) => {
                  setPreset({ target: `interview:${id}`, n: Date.now() });
                  setTab("start");
                }}
              />,
            ),
            true,
          )}
          {page("work", gate(<Work active={tab === "work"} askEntry={askEntry?.owner === contextKey.current ? askEntry : null} />), true)}
          {page(
            "history",
            locked ? (
              gate(null)
            ) : (
              <History
                active={tab === "history"}
                ready={tokenSet}
                running={session.running}
                liveId={session.running ? session.sessionId : null}
                onOpenLive={openLive}
                onSignIn={() => setTab("start")}
              />
            ),
          )}
          </CatalogProvider>
          </SessionNoteDrafts>
        </ProfileContext.Provider>
        {tab === "settings" && view && (
          <div className="shell-content">
            <Suspense fallback={<SkeletonList rows={3} />}>
              <SettingsScreen view={view} running={session.running} onSaved={reload} update={update} check={check} section={locked ? "account" : undefined} profileScope={contextKey.current} />
            </Suspense>
          </div>
        )}
        {tab === "debug" && (
          <div className="shell-content shell-content-wide">
            <Suspense fallback={<SkeletonList rows={3} />}>
              <DebugPanel session={session} />
            </Suspense>
          </div>
        )}
      </Shell>
    </ToastProvider>
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

/** The Career-OS link and whether the app is locked until one: the server says the stage
 * requires it and the signed-in account has none. Loaded per account; until it answers, nothing
 * is locked (the server refuses the work anyway). */
function useLinkRequired(ready: boolean, account: string) {
  const visible = useDataActivity(ready);
  const [conn, setConn] = useState<CareerOsConnection | null>(null);
  const generation = useRef(0);
  const reload = useCallback(async () => {
    if (!visible) return;
    const ticket = generation.current;
    try {
      const loaded = await rest.careerOs();
      if (ticket === generation.current) setConn(loaded);
    } catch {
      if (ticket === generation.current) setConn(null);
    }
  }, [visible]);
  useEffect(() => {
    generation.current++;
    setConn(null);
  }, [ready, account]);
  useEffect(() => { if (visible) void reload(); }, [visible, ready, account]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { generation.current++; }, []);
  return { conn, locked: ready && isLocked(conn), reload };
}
