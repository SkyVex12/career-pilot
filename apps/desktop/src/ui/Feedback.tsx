// Status and feedback pieces: badge, banner, empty state, skeleton, spinner, and the three states
// every list has (loading, error, empty).
import { cloneElement, isValidElement, useId, type ReactElement, type ReactNode } from "react";
import { cx } from "./Button";

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
  return (
    <div className={cx("ui-banner", `ui-tone-${tone}`, className)} role={tone === "danger" ? "alert" : "status"}>
      <span className="ui-banner-icon" aria-hidden="true">
        {TONE_ICON[tone]}
      </span>
      <div className="ui-banner-body">
        {title && <div className="ui-banner-title">{title}</div>}
        {children && <div className="ui-banner-text">{children}</div>}
      </div>
      {action && <div className="ui-banner-action">{action}</div>}
    </div>
  );
}

export function EmptyState({ icon = "○", title, children, action }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; action?: ReactNode }) {
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
        {error}
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
  return (
    <span className="ui-tooltip-wrap">
      {isValidElement(children) ? cloneElement(children, { "aria-describedby": id }) : children}
      <span role="tooltip" id={id} className="ui-tooltip">
        {content}
      </span>
    </span>
  );
}
