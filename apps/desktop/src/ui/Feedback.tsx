// Status and feedback pieces: badge, banner, empty state, skeleton, spinner, and the three states
// every list has (loading, error, empty).
import { cloneElement, isValidElement, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cx } from "./Button";
import { Icon } from "./Icon";
import { NotificationOutlet, NotificationVisibility } from "./NotificationScope";
import { DataActivity } from "../usePolling";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

const TONE_ICON: Record<Tone, string> = { neutral: "○", info: "ℹ", success: "✓", warning: "◌", danger: "✕" };

/** Never colour alone: the text says it, and an icon or dot backs it up. */
export function Badge({ tone = "neutral", icon, children, className }: { tone?: Tone; icon?: ReactNode | false; children: ReactNode; className?: string }) {
  return (
    <span className={cx("ui-badge", `ui-tone-${tone}`, className)}>
      {icon !== false && <span className="ui-badge-icon" aria-hidden="true">{icon ?? TONE_ICON[tone]}</span>}
      {children}
    </span>
  );
}

function noticeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(noticeText).join(" ");
  if (isValidElement<{ children?: ReactNode }>(node)) return noticeText(node.props.children);
  return "";
}

export function Banner({
  tone = "info",
  title,
  children,
  action,
  className,
}: {
  tone?: Tone;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const outlet = useContext(NotificationOutlet);
  const active = useContext(DataActivity);
  const visible = useContext(NotificationVisibility);
  const identity = tone + ":" + noticeText(title) + ":" + noticeText(children);
  const [dismissed, setDismissed] = useState<string | null>(null);
  if (!active || !visible || dismissed === identity) return null;
  const notice = (
    <div className={cx("ui-banner", outlet && "ui-floating-notice", `ui-tone-${tone}`, className)} role={tone === "danger" ? "alert" : "status"}>
      <span className="ui-banner-icon" aria-hidden="true">
        {TONE_ICON[tone]}
      </span>
      <div className="ui-banner-body">
        {title && <div className="ui-banner-title">{title}</div>}
        {children && <div className="ui-banner-text">{children}</div>}
      </div>
      {action && <div className="ui-banner-action">{action}</div>}
      {outlet && <button type="button" className="ui-icon-btn ui-icon-btn-sm ui-icon-btn-ghost ui-notice-dismiss" aria-label="Dismiss notification" onClick={() => setDismissed(identity)}><Icon name="close" size={14} /></button>}
    </div>
  );
  return outlet ? createPortal(notice, outlet) : notice;
}

export function EmptyState({ icon = <Icon name="document" size={28} />, title, children, action }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="ui-empty">
      <div className="ui-empty-icon" aria-hidden="true">
        {icon}
      </div>
      <h3 className="ui-empty-title">{title}</h3>
      {children && <p className="ui-empty-text">{children}</p>}
      {action && <div className="ui-empty-action">{action}</div>}
    </div>
  );
}

export function Spinner({ size = "md", label = "Loading" }: { size?: "sm" | "md"; label?: string | null }) {
  return (
    <span className={cx("ui-spinner", `ui-spinner-${size}`)} role={label ? "status" : undefined} aria-label={label ?? undefined} aria-hidden={label ? undefined : true} />
  );
}

export function Skeleton({ width, height = 12, className }: { width?: number | string; height?: number; className?: string }) {
  return <span className={cx("ui-skeleton", className)} style={{ width, height }} aria-hidden="true" />;
}

/** A list on its way: rows of skeleton lines, announced once as loading. */
export function SkeletonList({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="ui-skeleton-list" aria-busy="true">
      <span className="ui-visually-hidden" role="status">
        {label}…
      </span>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="ui-skeleton-row">
          <Skeleton width={`${60 - i * 10}%`} height={14} />
          <Skeleton width={`${35 + i * 8}%`} height={10} />
        </div>
      ))}
    </div>
  );
}

/** The three states every list has, in the list's place. Renders children once loaded and not empty. */
export function ListState<T>({
  items,
  error,
  onRetry,
  loadingLabel,
  empty,
  children,
}: {
  /** null while loading. */
  items: T[] | null | undefined;
  error: string | null | undefined;
  onRetry?: () => unknown;
  loadingLabel: string;
  empty: ReactNode;
  children: (items: T[]) => ReactNode;
}) {
  if (error && !items?.length) {
    return (
      <Banner
        tone="danger"
        title={`Could not load ${loadingLabel}`}
        action={
          onRetry && (
            <button type="button" className="ui-btn ui-btn-secondary ui-btn-sm" onClick={() => onRetry()}>
              <span className="ui-btn-label">Retry</span>
            </button>
          )
        }
      >
        {error.includes("server URL needs a host") ? <>
          Set your server connection in Settings → Advanced. <HelpTip label="Connection error">{error}</HelpTip>
        </> : error}
      </Banner>
    );
  }
  if (!items) return <SkeletonList label={`Loading ${loadingLabel}`} />;
  if (items.length === 0) return <>{empty}</>;
  return <>{children(items)}</>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="ui-kbd">{children}</kbd>;
}

/** A hint shown on hover and on keyboard focus of its trigger. */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactElement<{ "aria-describedby"?: string }> }) {
  const id = useId();
  const trigger = useRef<HTMLSpanElement>(null);
  const tip = useRef<HTMLSpanElement>(null);
  const leave = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const open = (hovered || focused) && !dismissed;
  const enter = () => { clearTimeout(leave.current); setDismissed(false); setHovered(true); };
  const exit = () => { clearTimeout(leave.current); leave.current = setTimeout(() => setHovered(false), 100); };
  useEffect(() => () => clearTimeout(leave.current), []);
  useLayoutEffect(() => {
    if (!open) { setPosition(null); return; }
    const place = () => {
      if (!trigger.current || !tip.current) return;
      const anchor = trigger.current.getBoundingClientRect();
      const rect = tip.current.getBoundingClientRect();
      const margin = 8;
      const left = Math.max(margin, Math.min(anchor.left + (anchor.width - rect.width) / 2, window.innerWidth - rect.width - margin));
      const above = anchor.top - rect.height - margin;
      const top = Math.max(margin, Math.min(above >= margin ? above : anchor.bottom + margin, window.innerHeight - rect.height - margin));
      setPosition({ left, top });
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setDismissed(true); }
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    document.addEventListener("keydown", escape, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      document.removeEventListener("keydown", escape, true);
    };
  }, [open, content]);
  const describedBy = [children.props["aria-describedby"], id].filter(Boolean).join(" ");
  return (
    <span ref={trigger} className="ui-tooltip-wrap" onPointerEnter={enter} onPointerLeave={exit}
      onFocus={() => { setDismissed(false); setFocused(true); }}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false); }}>
      {isValidElement(children) ? cloneElement(children, { "aria-describedby": describedBy }) : children}
      {createPortal(<span ref={tip} role="tooltip" id={id} className="ui-tooltip" hidden={!open} style={position ?? { visibility: "hidden" }} onPointerEnter={enter} onPointerLeave={exit}>
        {content}
      </span>, document.body)}
    </span>
  );
}

/** A compact, keyboard-accessible explanation beside a control's title. */
export function HelpTip({ label, children }: { label: ReactNode; children: ReactNode }) {
  return <Tooltip content={children}><button type="button" className="ui-help-tip">
    <span aria-hidden="true">?</span><span className="ui-visually-hidden">About {label}</span>
  </button></Tooltip>;
}
