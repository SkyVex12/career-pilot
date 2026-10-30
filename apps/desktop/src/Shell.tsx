// The main window's frame (docs/design/ui-proposal.md, "App shell"): a sidebar grouped as Call,
// Prepare and Review, with Settings, the profile, the connection status and the version at its
// foot, and the page beside it. Below 900 px the sidebar is a rail of icons.
import type { ReactNode } from "react";
import { Select, Tooltip } from "./ui";
import type { ProfileState } from "./profiles";
import type { SessionView } from "./session";
import { badge } from "./session";

export type Tab = "start" | "applications" | "resumes" | "work" | "history" | "settings" | "debug";

interface NavItem {
  tab: Tab;
  label: string;
  icon: string;
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
  children: ReactNode;
}) {
  const live = session.running;
  const groups: [string | null, NavItem[]][] = [
    [null, [{ tab: "start", label: "Call", icon: "◉", badge: live ? <span className="shell-live">Live</span> : undefined }]],
    [
      "Prepare",
      [
        { tab: "applications", label: "Applications", icon: "▤" },
        { tab: "resumes", label: "Resumes", icon: "▧" },
        { tab: "work", label: "Work", icon: "▦", badge: pending ? <span className="shell-count">{pending}</span> : undefined },
      ],
    ],
    ["Review", [{ tab: "history", label: "History", icon: "↺" }]],
  ];
  if (showDebug) groups.push(["Developer", [{ tab: "debug", label: "Debug", icon: "⌁" }]]);

  const link = (it: NavItem) => (
    <li key={it.tab}>
      <button type="button" className="shell-link" aria-current={tab === it.tab ? "page" : undefined} onClick={() => onTab(it.tab)} title={it.label}>
        <span className="shell-link-icon" aria-hidden="true">
          {it.icon}
        </span>
        <span className="shell-link-text">{it.label}</span>
        {it.badge}
      </button>
    </li>
  );
  const b = badge(session);
  return (
    <div className="shell">
      <nav className="shell-side" aria-label="Main">
        <div className="shell-brand">
          <span className="shell-brand-mark" aria-hidden="true">
            C
          </span>
          <span className="shell-brand-text">Copilot</span>
        </div>
        {groups.map(([group, items], i) => (
          <div key={group ?? i}>
            {group && <div className="shell-group">{group}</div>}
            <ul className="shell-nav">{items.map(link)}</ul>
          </div>
        ))}
        <div className="shell-foot">
          <ul className="shell-nav">{link({ tab: "settings", label: "Settings", icon: "⚙" })}</ul>
          <ProfileSwitch state={profiles} running={live} onSwitch={onSwitchProfile} error={profileError} />
          <div className={`shell-status status-${b.state}`} role="status" title={b.text}>
            <span className="status-dot" aria-hidden="true" />
            <span className="shell-hide-narrow ui-truncate">{b.text}</span>
          </div>
          {version && <div className="shell-version">Version {version}</div>}
        </div>
      </nav>
      <main className="shell-main" id="main">
        {banner}
        {children}
      </main>
    </div>
  );
}

/** Whom the app works for: "Me" or a Career-OS user an admin link reads. Its place is kept while
 * profiles load, so nothing shifts; locked during a call, with the reason said. */
function ProfileSwitch({
  state,
  running,
  onSwitch,
  error,
}: {
  state: ProfileState;
  running: boolean;
  onSwitch: (id: string | null) => void;
  error: string | null;
}) {
  if (!state.profiles.some((p) => !p.me)) return null;
  const select = (
    <Select aria-label="Profile" value={state.here ?? ""} disabled={running} onChange={(e) => onSwitch(e.target.value || null)}>
      {state.profiles.map((p) => (
        <option key={p.id} value={p.id}>
          {p.me ? `Me (${p.name})` : p.name}
        </option>
      ))}
    </Select>
  );
  return (
    <div className="shell-profile shell-hide-narrow">
      {running ? <Tooltip content="End the call to switch profiles">{select}</Tooltip> : select}
      {error && (
        <p className="ui-field-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
