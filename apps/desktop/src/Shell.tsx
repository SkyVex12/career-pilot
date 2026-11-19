import { startWindowDrag } from "./windowDrag";
// The main window's frame (docs/design/ui-proposal.md, "App shell"): a sidebar grouped as Call,
// Prepare and Review, with Settings, the profile, the connection status and the version at its
// foot, and the page beside it. Below 900 px the sidebar is a rail of icons.
import { useCardWindow } from "./cardWindow";
import { inTauri } from "./api";
import { RubyLauncher } from "./RubyLauncher";
import { RubyMark } from "./ui/RubyMark";
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Button, Select, Tooltip } from "./ui";
import type { ProfileState } from "./profiles";
import type { SessionView } from "./session";
import { badge } from "./session";
import { Icon, type IconName } from "./ui/Icon";

import { WorkspaceQuickSettings } from "./WorkspaceQuickSettings";
import { useHoverMenu, WORKSPACE_MENU_OPEN } from "./ui/useHoverMenu";
import { PageHeadingHost } from "./ui/Layout";

export interface WorkspaceWindowActions {
  compact: () => void;
  expand: () => Promise<boolean>;
}
const CompactWindowRegistration = createContext<((actions: WorkspaceWindowActions | null) => void) | null>(null);
export function useWorkspaceCompactWindow(compact: () => void, expand: () => Promise<boolean>) {
  const register = useContext(CompactWindowRegistration);
  const latest = useRef({ compact, expand });
  latest.current = { compact, expand };
  useEffect(() => {
    register?.({ compact: () => latest.current.compact(), expand: () => latest.current.expand() });
    return () => register?.(null);
  }, [register]);
}

const NavigationContext = createContext<ReactNode>(null);
type ProfileSwitchProps = {
  state: ProfileState;
  running: boolean;
  onSwitch: (id: string | null) => void;
  error: string | null;
};
const ProfileSwitchContext = createContext<ProfileSwitchProps | null>(null);
export function WorkspaceProfileSwitch({ busy = false }: { busy?: boolean }) {
  const props = useContext(ProfileSwitchContext);
  return props && <ProfileSwitch {...props} running={props.running || busy} inline />;
}
export function WorkspaceMenu() {
  const navigation = useContext(NavigationContext);
  const menuRef = useRef<HTMLDetailsElement>(null);
  const id = useId();
  const openedByHover = useRef(false);
  const fitMenu = () => {
    const panel = menuRef.current?.querySelector<HTMLElement>(".workspace-menu-panel");
    if (!panel || !menuRef.current?.open) return;
    const cardBottom = menuRef.current.closest(".workspace-card")?.getBoundingClientRect().bottom ?? window.innerHeight;
    const available = Math.min(window.innerHeight - 16, cardBottom - 12) - panel.getBoundingClientRect().top;
    panel.style.setProperty("--menu-available-height", `${Math.max(0, available)}px`);
  };
  const contains = (target: Node | null) => {
    if (!target) return false;
    if (menuRef.current?.contains(target)) return true;
    // A profile's listbox is portaled outside this details element.
    return [...(menuRef.current?.querySelectorAll('[aria-controls]') ?? [])].some(control => {
      const popup = document.getElementById(control.getAttribute("aria-controls") ?? "");
      return popup?.contains(target);
    });
  };
  const changeOpen = (next: boolean, fromHover = false) => {
    if (!menuRef.current) return;
    if (next && !menuRef.current.open) document.dispatchEvent(new CustomEvent(WORKSPACE_MENU_OPEN, { detail: { id } }));
    openedByHover.current = next && fromHover;
    menuRef.current.open = next;
  };
  const hover = useHoverMenu({
    onOpen: () => { if (!menuRef.current?.open) changeOpen(true, true); },
    onClose: () => { if (openedByHover.current) changeOpen(false); },
    keepOpen: () => contains(document.activeElement) && document.activeElement !== menuRef.current?.querySelector("summary"),
  });
  useEffect(() => {
    window.addEventListener("resize", fitMenu);
    const dismiss = (event: Event) => {
      if (menuRef.current?.open && !contains(event.target as Node)) { hover.cancel(); changeOpen(false); }
    };
    const otherOpened = (event: Event) => {
      if ((event as CustomEvent<{ id: string }>).detail.id !== id) { hover.cancel(); changeOpen(false); }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !menuRef.current?.open) return;
      const returnFocus = contains(document.activeElement);
      hover.cancel(); changeOpen(false);
      if (returnFocus) menuRef.current.querySelector("summary")?.focus();
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("focusin", dismiss);
    document.addEventListener("keydown", escape);
    document.addEventListener(WORKSPACE_MENU_OPEN, otherOpened);
    return () => {
      window.removeEventListener("resize", fitMenu);
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("focusin", dismiss);
      document.removeEventListener("keydown", escape);
      document.removeEventListener(WORKSPACE_MENU_OPEN, otherOpened);
    };
  }, [id]);
  return <details ref={menuRef} onToggle={fitMenu} className="workspace-menu">
    <summary aria-label="App menu" title="App menu" onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} onClick={event => {
      event.preventDefault(); hover.cancel();
      if (openedByHover.current) openedByHover.current = false;
      else changeOpen(!menuRef.current?.open);
    }}><Icon name="menu" size={20} /></summary>
    <div className="workspace-menu-panel" onPointerEnter={hover.onPointerEnter} onPointerLeave={hover.onPointerLeave} onClick={(e) => { if ((e.target as HTMLElement).closest("button.shell-link")) { hover.cancel(); changeOpen(false); } }}>{navigation}</div>
  </details>;
}

export type Tab = "start" | "applications" | "resumes" | "work" | "history" | "settings" | "debug";

interface NavItem {
  tab: Tab;
  label: string;
  icon: IconName;
  badge?: ReactNode;
}

export function Shell({
  tab,
  onTab,
  session,
  profiles,
  onSwitchProfile,
  profileError,
  version,
  showDebug,
  pending,
  banner,
  onWindowActionsChanged,
  children,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  session: SessionView;
  profiles: ProfileState;
  onSwitchProfile: (id: string | null) => void;
  profileError: string | null;
  version: string | null;
  showDebug: boolean;
  /** Facts waiting for review across companies (Work's count). */
  pending?: number;
  /** The update bar, above the page. */
  banner?: ReactNode;
  /** Native page navigation restores compact bounds through the mounted call screen. */
  onWindowActionsChanged?: (actions: WorkspaceWindowActions | null) => void;
  children: ReactNode;
}) {
  useCardWindow();
  const [headingHost, setHeadingHost] = useState<HTMLDivElement | null>(null);
  const [windowActions, setWindowActions] = useState<WorkspaceWindowActions | null>(null);
  const registerCompactWindow = useCallback((actions: WorkspaceWindowActions | null) => {
    setWindowActions(actions);
    onWindowActionsChanged?.(actions);
  }, [onWindowActionsChanged]);
  const live = session.running;
  const groups: [string | null, NavItem[]][] = [
    [null, [{ tab: "start", label: "Call", icon: "call", badge: live ? <span className="shell-live">Live</span> : undefined }]],
    [
      null,
      [
        { tab: "applications", label: "Applications", icon: "briefcase" },
        { tab: "resumes", label: "Resumes", icon: "document" },
        { tab: "work", label: "Work", icon: "work", badge: pending ? <span className="shell-count">{pending}</span> : undefined },
      ],
    ],
    [null, [{ tab: "history", label: "History", icon: "history" }]],
  ];
  if (showDebug) groups.push([null, [{ tab: "debug", label: "Debug", icon: "code" }]]);

  const link = (it: NavItem) => (
    <li key={it.tab}>
      <button type="button" className="shell-link" aria-label={it.label} aria-current={tab === it.tab ? "page" : undefined} onClick={() => onTab(it.tab)} title={it.label}>
        <span className="shell-link-icon" aria-hidden="true">
          <Icon name={it.icon} />
        </span>
        <span className="shell-link-text">{it.label}</span>
        {it.badge}
      </button>
    </li>
  );
  const b = badge(session);
  const currentProfile = profiles.profiles.find((p) => profiles.here ? p.id === profiles.here : p.me);
  const navigation = <nav className="shell-side" aria-label="Main">
        <div className="shell-brand">
          <span className="shell-brand-mark" aria-hidden="true">
            <RubyMark />
          </span>
          <span className="shell-brand-text">Career Pilot<span className="brand-slash">/</span></span>
        </div>
        {groups.map(([group, items], i) => (
          <div key={group ?? i}>
            {group && <div className="shell-group">{group}</div>}
            <ul className="shell-nav">{items.map(link)}</ul>
          </div>
        ))}
        <div className="shell-foot">
          <ul className="shell-nav">{link({ tab: "settings", label: "Settings", icon: "settings" })}</ul>
          {currentProfile && <div className="shell-account shell-hide-narrow"><span className="shell-avatar" aria-hidden="true">{currentProfile.name.slice(0, 1).toUpperCase()}</span><div><strong>{currentProfile.name}</strong></div></div>}
          {(tab !== "start" || live) && <ProfileSwitch state={profiles} running={live} onSwitch={onSwitchProfile} error={profileError} />}
          <div className={`shell-status status-${b.state}`} role="status" title={b.text}>
            <span className="status-dot" aria-hidden="true" />
            <span className="shell-hide-narrow ui-truncate">{b.text}</span>
          </div>
          {version && <div className="shell-version">Version {version}</div>}
        </div>
      </nav>;
  return (
    <NavigationContext.Provider value={navigation}>
    <div className="shell">
      <main className="shell-main" id="main">
        <div className="workspace-card">
          <div className="workspace-card-header" data-native-drag-region="header">
            <div className="workspace-card-tools" data-native-drag-region="header">
              <Button className="window-mode-toggle" variant="ghost" disabled={!windowActions || live} aria-label="Compact window" title="Compact toolbar" onClick={() => { onTab("start"); windowActions?.compact(); }} icon={<Icon name="chevronUp" size={16} />} />
              <RubyLauncher className="call-ruby-button" label="Back to call" title="Back to call · Drag to move" onOpen={() => onTab("start")} onDrag={() => { if (inTauri) void startWindowDrag().catch(() => {}); }} />
              <div className="workspace-page-heading" ref={setHeadingHost} />
              <WorkspaceMenu />
              <WorkspaceQuickSettings running={live} />
            </div>
            {tab !== "start" && (
              <div className="workspace-return-row">
                <Button size="sm" onClick={() => onTab("start")} icon={<Icon name="call" size={16} />}>Go to call</Button>
              </div>
            )}
          </div>
          <div className="workspace-card-body">
            <div className="workspace-banners">{banner}</div>
            <ProfileSwitchContext.Provider value={{ state: profiles, running: live, onSwitch: onSwitchProfile, error: profileError }}>
              <CompactWindowRegistration.Provider value={registerCompactWindow}>
                <PageHeadingHost.Provider value={headingHost}>{children}</PageHeadingHost.Provider>
              </CompactWindowRegistration.Provider>
            </ProfileSwitchContext.Provider>
          </div>
        </div>
      </main>
    </div>
    </NavigationContext.Provider>
  );
}

/** Whom the app works for: "Me" or a Career-OS user an admin link reads. Its place is kept while
 * profiles load, so nothing shifts; locked during a call, with the reason said. */
function ProfileSwitch({
  state,
  running,
  onSwitch,
  error,
  inline = false,
}: ProfileSwitchProps & { inline?: boolean }) {
  const errorId = useId();
  if (!inline && !state.profiles.some((p) => !p.me)) return null;
  const unavailable = state.profiles.length === 0;
  const select = (
    <Select aria-label="Profile" aria-describedby={error ? errorId : undefined} title={unavailable ? "Profiles unavailable — check your server connection" : running ? "Switch profiles when the call is stopped" : "Profile"} value={state.here ?? ""} disabled={running || unavailable} onChange={(e) => onSwitch(e.target.value || null)}>
      {unavailable && <option value={state.here ?? ""}>Profile unavailable</option>}
      {state.profiles.map((p) => (
        <option key={p.id} value={p.id}>
          {p.me ? `Me (${p.name})` : p.name}
        </option>
      ))}
    </Select>
  );
  return (
    <div className={inline ? "call-profile" : "shell-profile shell-hide-narrow"}>
      {running ? <Tooltip content="End the call to switch profiles">{select}</Tooltip> : select}
      {error && (
        <p className="ui-field-error" id={errorId} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
