// The native workspace frame keeps navigation, the active profile and quick settings
// in one stable header. Only an explicit Small or Medium action changes native bounds.
import { useCardWindow } from "./cardWindow";
import { inTauri } from "./api";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { CoRubyWordmark, RubyMark } from "./ui/RubyMark";
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Menu, Select, Tooltip } from "./ui";
import type { ProfileState } from "./profiles";
import type { SessionView } from "./session";
import { Icon, type IconName } from "./ui/Icon";

import { WorkspaceQuickSettings } from "./WorkspaceQuickSettings";
import { useHoverMenu, WORKSPACE_MENU_OPEN } from "./ui/useHoverMenu";
import { PageHeadingHost } from "./ui/Layout";

export interface WorkspaceWindowActions {
  compact: (mode?: "toolbar" | "dot") => void;
  expand: () => Promise<boolean>;
}
const CompactWindowRegistration = createContext<((actions: WorkspaceWindowActions | null) => void) | null>(null);
export function useWorkspaceCompactWindow(compact: (mode?: "toolbar" | "dot") => void, expand: () => Promise<boolean>) {
  const register = useContext(CompactWindowRegistration);
  const latest = useRef({ compact, expand });
  latest.current = { compact, expand };
  useEffect(() => {
    register?.({ compact: mode => latest.current.compact(mode), expand: () => latest.current.expand() });
    return () => register?.(null);
  }, [register]);
}

const WorkspaceQuickSettingsContext = createContext<(() => void) | null>(null);
export const useWorkspaceOpenQuickSettings = () => useContext(WorkspaceQuickSettingsContext);

const CallBusyContext = createContext<((busy: boolean) => void) | null>(null);
export function useWorkspaceCallBusy(busy: boolean) {
  const report = useContext(CallBusyContext);
  useEffect(() => { report?.(busy); return () => report?.(false); }, [busy, report]);
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

export type Tab = "start" | "applications" | "work" | "history" | "settings" | "debug";

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
  const [callBusy, setCallBusy] = useState(false);
  const [quickSettingsOpen, setQuickSettingsOpen] = useState(false);
  const openQuickSettings = useCallback(() => setQuickSettingsOpen(true), []);
  const [windowError, setWindowError] = useState<string | null>(null);
  const [windowActions, setWindowActions] = useState<WorkspaceWindowActions | null>(null);
  const registerCompactWindow = useCallback((actions: WorkspaceWindowActions | null) => {
    setWindowActions(actions);
    onWindowActionsChanged?.(actions);
  }, [onWindowActionsChanged]);
  const live = session.running;
  const windowAction = async (action: "minimize" | "maximize" | "close") => {
    if (!inTauri) return;
    setWindowError(null);
    try {
      const win = getCurrentWindow();
      if (action === "minimize") await win.minimize();
      else if (action === "maximize") { if (await win.isMaximized()) await win.unmaximize(); else await win.maximize(); }
      else await win.close();
    } catch (error) { setWindowError(String(error)); }
  };
  const groups: [string | null, NavItem[]][] = [
    [null, [{ tab: "start", label: "Call", icon: "call", badge: live ? <span className="shell-live">Live</span> : undefined }]],
    [
      null,
      [
        { tab: "applications", label: "Interview prep", icon: "briefcase" },
        { tab: "work", label: "Work context", icon: "work", badge: pending ? <span className="shell-count">{pending}</span> : undefined },
      ],
    ],
    [null, [{ tab: "history", label: "Past calls", icon: "history" }]],
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
  const navigation = <nav className="shell-side" aria-label="Main">
        {groups.map(([group, items], i) => (
          <div key={group ?? i}>
            {group && <div className="shell-group">{group}</div>}
            <ul className="shell-nav">{items.map(link)}</ul>
          </div>
        ))}
      </nav>;
  return (
    <NavigationContext.Provider value={navigation}>
    <div className="shell">
      <main className="shell-main" id="main">
        <div className="workspace-card desktop-workspace">
          <div className="workspace-card-header" data-native-drag-region="header">
            <div className="desktop-titlebar" data-native-drag-region="header" onDoubleClick={e => { if (!(e.target as Element).closest("button")) void windowAction("maximize"); }}>
              <span className="desktop-brand" title={version ? `CoRuby ${version}` : "CoRuby"}><RubyMark /><CoRubyWordmark /></span>
              <div className="desktop-title-actions">
                <div className="desktop-command-tools">
                  <Menu label="Window size" trigger={<span>Full</span>} items={[
                    { label: "Small", disabled: !windowActions || live || callBusy, onSelect: () => { onTab("start"); windowActions?.compact("dot"); } },
                    { label: "Medium", disabled: !windowActions || live || callBusy, onSelect: () => { onTab("start"); windowActions?.compact("toolbar"); } },
                    { label: "Full", onSelect: () => {} },
                  ]} />
                  <ProfileSwitch state={profiles} running={live || callBusy} onSwitch={onSwitchProfile} error={profileError} inline avatar />
                  <WorkspaceQuickSettings running={live} open={quickSettingsOpen} onOpenChange={setQuickSettingsOpen} />
                </div>
                <div className="desktop-window-actions">
                  <button type="button" aria-label="Minimize window" onClick={() => void windowAction("minimize")}><Icon name="minus" size={15} /></button>
                  <button type="button" aria-label="Maximize or restore window" onClick={() => void windowAction("maximize")}><Icon name="square" size={13} /></button>
                  <button type="button" className="desktop-close" aria-label="Close window" onClick={() => void windowAction("close")}><Icon name="close" size={15} /></button>
                </div>
              </div>
            </div>
            <div className="desktop-commandbar">{navigation}</div>
          </div>
          <div className="workspace-card-body">
            {windowError && <p className="ui-field-error" role="alert">{windowError}</p>}
            <div className="workspace-banners">{banner}</div>
            <ProfileSwitchContext.Provider value={{ state: profiles, running: live, onSwitch: onSwitchProfile, error: profileError }}>
              <CompactWindowRegistration.Provider value={registerCompactWindow}>
                <CallBusyContext.Provider value={setCallBusy}>
                <WorkspaceQuickSettingsContext.Provider value={openQuickSettings}>
                  <PageHeadingHost.Provider value={null}>
                    {children}
                  </PageHeadingHost.Provider>
                </WorkspaceQuickSettingsContext.Provider>
                </CallBusyContext.Provider>
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
  avatar = false,
}: ProfileSwitchProps & { inline?: boolean; avatar?: boolean }) {
  const errorId = useId();
  if (!inline && !state.profiles.some((p) => !p.me)) return null;
  const unavailable = state.profiles.length === 0;
  const current = state.profiles.find(p => state.here ? p.id === state.here : p.me);
  const words = current?.name.trim().split(/\s+/) ?? [];
  const initials = words.length > 1 ? words[0][0] + words[words.length - 1][0] : words[0]?.slice(0, 2) ?? "?";
  const select = (
    <Select className={avatar ? "desktop-profile-select" : undefined} displayValue={avatar ? <span className="desktop-profile-initials" aria-hidden="true">{initials.toUpperCase()}</span> : undefined} aria-label="Profile" aria-describedby={error ? errorId : undefined} title={unavailable ? "Profiles unavailable — check your server connection" : running ? "Switch profiles when the call is stopped" : current?.name ?? "Profile"} value={state.here ?? current?.id ?? ""} disabled={running || unavailable} onChange={(e) => onSwitch(e.target.value || null)}>
      {unavailable && <option value={state.here ?? ""}>Profile unavailable</option>}
      {state.profiles.map((p) => (
        <option key={p.id} value={p.id}>
          {p.me ? `Me (${p.name})` : p.name}
        </option>
      ))}
    </Select>
  );
  return (
    <div className={avatar ? "desktop-profile" : inline ? "call-profile" : "shell-profile shell-hide-narrow"}>
      {running ? <Tooltip content="End the call to switch profiles">{select}</Tooltip> : select}
      {error && (
        <p className="ui-field-error" id={errorId} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
