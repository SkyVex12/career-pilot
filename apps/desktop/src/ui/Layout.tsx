// Page structure: page header, card, list rows, a definition row, and text that truncates.
import { createContext, useContext, type ButtonHTMLAttributes, type ReactNode } from "react";
import { createPortal } from "react-dom";

export const PageHeadingHost = createContext<HTMLElement | null>(null);
export const PageHeadingActive = createContext(true);
import { cx } from "./Button";

export function PageHeader({ title, meta, actions, back }: { title: ReactNode; meta?: ReactNode; actions?: ReactNode; back?: ReactNode }) {
  const host = useContext(PageHeadingHost);
  const active = useContext(PageHeadingActive);
  const heading = <h1 className="ui-page-title" data-native-drag-region="header">{title}</h1>;
  const docked = host !== null && active;
  return (
    <header className="ui-page-head">
      {docked && createPortal(heading, host)}
      {back && <div className="ui-page-back">{back}</div>}
      {(!docked || actions) && <div className="ui-page-head-row">
        {!docked && heading}
        {actions && <div className="ui-page-actions">{actions}</div>}
      </div>}
      {meta && <div className="ui-page-meta">{meta}</div>}
    </header>
  );
}

export function Card({
  title,
  actions,
  children,
  footer,
  className,
  inset,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
  inset?: boolean;
}) {
  return (
    <section className={cx("ui-card", inset && "ui-card-inset", className)}>
      {(title || actions) && (
        <div className="ui-card-head">
          {title && <h2 className="ui-card-title">{title}</h2>}
          {actions && <div className="ui-card-actions">{actions}</div>}
        </div>
      )}
      {children != null && <div className="ui-card-body">{children}</div>}
      {footer && <div className="ui-card-foot">{footer}</div>}
    </section>
  );
}

export function List({ children, label, className }: { children: ReactNode; label?: string; className?: string }) {
  return (
    <ul className={cx("ui-list", className)} aria-label={label}>
      {children}
    </ul>
  );
}

interface RowParts {
  leading?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
  /** The full title, shown as a tooltip when it is cut off. */
  fullTitle?: string;
}

function RowBody({ leading, title, meta, trailing, fullTitle }: RowParts) {
  return (
    <>
      {leading && <span className="ui-row-leading">{leading}</span>}
      <span className="ui-row-main">
        <span className="ui-row-title" title={fullTitle}>
          {title}
        </span>
        {meta && <span className="ui-row-meta">{meta}</span>}
      </span>
      {trailing && <span className="ui-row-trailing">{trailing}</span>}
    </>
  );
}

/** A list row. Clickable rows are buttons (keyboard and screen readers reach them); `selected`
 * marks the one shown beside the list. */
export function ListRow({
  onClick,
  selected,
  actions,
  className,
  ...parts
}: RowParts & { onClick?: () => void; selected?: boolean; actions?: ReactNode; className?: string } & Pick<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label">) {
  return (
    <li className={cx("ui-row", selected && "is-selected", onClick && "is-clickable", className)}>
      {onClick ? (
        <button type="button" className="ui-row-hit" onClick={onClick} aria-current={selected ? "true" : undefined} aria-label={parts["aria-label"]}>
          <RowBody {...parts} />
        </button>
      ) : (
        <div className="ui-row-hit">
          <RowBody {...parts} />
        </div>
      )}
      {actions && <span className="ui-row-actions">{actions}</span>}
    </li>
  );
}

/** Label and value pairs (facts about a thing). */
export function Facts({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="ui-facts">
      {items.map(([k, v], i) => (
        <div key={i} className="ui-facts-row">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One line with an ellipsis; the full text as a tooltip. */
export function Truncate({ text, lines = 1, className }: { text: string; lines?: 1 | 2 | 3; className?: string }) {
  return (
    <span className={cx(lines === 1 ? "ui-truncate" : `ui-clamp ui-clamp-${lines}`, className)} title={text}>
      {text}
    </span>
  );
}

/** Cuts the middle of a long path or id, keeping both ends readable. */
export function middleTruncate(text: string, max = 40): string {
  if (text.length <= max) return text;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
}
