// The main window: the app shell (Shell.tsx) around the Call screen, Applications, Resumes, Work,
// History, Settings, and the debug panel (hidden unless turned on in the settings, or with
// Ctrl+Shift+D). Signing in and the Career-OS link, when the stage requires one, come first:
// the Call screen walks through them, and the other screens wait.
import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from "react";
import { api, inTauri, onOpenSettings, onSettingsChanged, onUpdate, rest, type CareerOsConnection, type LiveSession, type SettingsView, type UpdateState } from "./api";
import { bar } from "./updates";
import { Applications } from "./Applications";
import { CallScreen } from "./CallScreen";
import { Resumes } from "./Resumes";
import { useSession } from "./session";
import { Shell, type Tab } from "./Shell";
import { ProfileContext, accountOf, isLocked, useProfiles } from "./profiles";
import { Banner, Button, EmptyState, SkeletonList, ToastProvider } from "./ui";
import { Work } from "./Work";

// Screens not needed at startup load when first opened: History (with the call replay, which
// brings the overlay's components), Settings and the debug panel.
const History = lazy(() => import("./History").then((m) => ({ default: m.History })));
const SettingsScreen = lazy(() => import("./SettingsScreen").then((m) => ({ default: m.SettingsScreen })));
const DebugPanel = lazy(() => import("./Debug").then((m) => ({ default: m.DebugPanel })));

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
  const [profileError, setProfileError] = useState<string | null>(null);
  // An interview to start a call for (Applications' Start), an application to open (Resumes).
  const [preset, setPreset] = useState<{ target: string; n: number } | null>(null);
  const [focusApp, setFocusApp] = useState<{ id: string; n: number } | null>(null);
  // The current environment can make calls: the dev token is set (local) or signed in (cloud).
  const tokenSet = view?.ready ?? false;
  const environment = view?.settings.environment ?? "local";
  // Whose data the screens show: everything loaded is dropped when this changes.
  const account = accountOf(view);
  const profiles = useProfiles(tokenSet, view?.settings.profile ?? null, account);
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
      <main className="shell-content">
        <h1>Copilot</h1>
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
        <EmptyState icon="→" title={environment === "cloud" || view?.release ? "Sign in first" : "Set the dev token first"} action={<Button variant="primary" onClick={() => setTab("start")}>Go to setup</Button>}>
          This screen shows your data once you are signed in.
        </EmptyState>
      );
    }
    if (locked) {
      return (
        <EmptyState icon="→" title="Link Career-OS first" action={<Button variant="primary" onClick={() => setTab("start")}>Go to setup</Button>}>
          This stage needs your Career-OS account linked before the app can work for you.
        </EmptyState>
      );
    }
    return body;
  };
  const page = (t: Tab, body: ReactNode, wide = false) =>
    keep(t) && (
      <div hidden={tab !== t} className={wide ? "shell-content shell-content-wide" : "shell-content"}>
        <Suspense fallback={<SkeletonList rows={3} />}>{body}</Suspense>
      </div>
    );

  return (
    <ToastProvider>
      <Shell
        tab={tab}
        onTab={setTab}
        session={session}
        profiles={profiles.state}
        onSwitchProfile={switchProfile}
        profileError={profileError}
        version={view?.version ?? null}
        showDebug={showDebug}
        banner={<UpdateBar update={update} running={session.running} />}
      >
        {/* A profile switch or another account remounts every screen, so each one loads its data. */}
        <ProfileContext.Provider value={profiles.state} key={`${account}/${profiles.state.here ?? "me"}`}>
          {page(
            "start",
            <CallScreen
              session={session}
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
              onResumes={() => setTab("resumes")}
              onOpenLive={openLive}
              preset={preset}
            />,
          )}
          {page(
            "applications",
            gate(
              <Applications
                active={tab === "applications"}
                focus={focusApp}
                onStartInterview={(id) => {
                  setPreset({ target: `interview:${id}`, n: Date.now() });
                  setTab("start");
                }}
              />,
            ),
            true,
          )}
          {page(
            "resumes",
            gate(
              <Resumes
                active={tab === "resumes"}
                onOpenApplication={(id) => {
                  setFocusApp({ id, n: Date.now() });
                  setTab("applications");
                }}
              />,
            ),
          )}
          {page("work", gate(<Work active={tab === "work"} />), true)}
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
        </ProfileContext.Provider>
        {tab === "settings" && view && (
          <div className="shell-content">
            <Suspense fallback={<SkeletonList rows={3} />}>
              <SettingsScreen view={view} running={session.running} onSaved={reload} update={update} check={check} section={locked ? "account" : undefined} />
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

/** Release builds update themselves (src-tauri/src/release.rs). While an update downloads,
 * waits or installs, this bar above the page says what is happening, with its progress. */
function UpdateBar({ update, running }: { update: UpdateState; running: boolean }) {
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
    <div className="update-bar" aria-live="polite">
      <Banner
        tone={b.tone === "error" ? "danger" : b.tone === "done" ? "success" : "info"}
        action={
          <span className="ui-row-inline">
            {b.action && (
              <Button size="sm" variant="primary" onClick={install} disabled={!b.action.enabled}>
                {b.action.label}
              </Button>
            )}
            {b.dismissible && (
              <Button size="sm" variant="ghost" onClick={() => setDismissed(key)}>
                OK
              </Button>
            )}
          </span>
        }
      >
        {b.text}
        {error && <span className="ui-field-error"> {error}</span>}
        {b.progress !== undefined && (
          <span className={`update-track${b.progress === null ? " moving" : ""}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={b.progress ?? undefined}>
            <span className="update-fill" style={b.progress === null ? undefined : { width: `${b.progress}%` }} />
          </span>
        )}
      </Banner>
    </div>
  );
}

/** The Career-OS link and whether the app is locked until one: the server says the stage
 * requires it and the signed-in account has none. Loaded per account; until it answers, nothing
 * is locked (the server refuses the work anyway). */
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
  return { conn, locked: ready && isLocked(conn), reload };
}
