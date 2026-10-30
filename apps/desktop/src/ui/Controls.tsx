// Choice controls: segmented control (a radio group), tabs (Radix), switch, and the menu.
import * as RadixTabs from "@radix-ui/react-tabs";
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { cx } from "./Button";

export function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
  label,
  size = "md",
  disabled,
}: {
  value: T;
  options: readonly (readonly [T, ReactNode])[];
  onChange: (v: T) => void;
  /** The group's accessible name. */
  label: string;
  size?: "sm" | "md";
  disabled?: boolean;
}) {
  const name = useId();
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  return (
    <div className={cx("ui-segmented", `ui-segmented-${size}`)} role="radiogroup" aria-label={label}>
      {options.map(([v, text], i) => (
        <label key={v} className={cx("ui-segment", value === v && "is-on", disabled && "is-disabled")}>
          <input
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="radio"
            name={name}
            value={v}
            checked={value === v}
            disabled={disabled}
            onChange={() => onChange(v)}
          />
          <span>{text}</span>
        </label>
      ))}
    </div>
  );
}

export interface TabItem {
  value: string;
  label: ReactNode;
  count?: number;
  content: ReactNode;
}

/** Page tabs: a tablist with arrow keys; panels render when chosen. */
export function Tabs({
  items,
  value,
  onChange,
  label,
  variant = "underline",
}: {
  items: TabItem[];
  value: string;
  onChange: (v: string) => void;
  label: string;
  variant?: "underline" | "pill";
}) {
  return (
    <RadixTabs.Root value={value} onValueChange={onChange} className="ui-tabs">
      <RadixTabs.List aria-label={label} className={cx("ui-tablist", `ui-tablist-${variant}`)}>
        {items.map((t) => (
          <RadixTabs.Trigger key={t.value} value={t.value} className="ui-tab">
            {t.label}
            {t.count != null && t.count > 0 && <span className="ui-tab-count">{t.count}</span>}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {items.map((t) => (
        <RadixTabs.Content key={t.value} value={t.value} className="ui-tabpanel">
          {t.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (on: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="ui-switch-row">
      <div className="ui-switch-text">
        <span className="ui-switch-label" id={`${id}-label`}>
          {label}
        </span>
        {hint && (
          <span className="ui-field-hint" id={`${id}-hint`}>
            {hint}
          </span>
        )}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-labelledby={`${id}-label`}
        aria-describedby={hint ? `${id}-hint` : undefined}
        disabled={disabled}
        className={cx("ui-switch", checked && "is-on")}
        onClick={() => onChange(!checked)}
      >
        <span className="ui-switch-thumb" aria-hidden="true" />
      </button>
    </div>
  );
}

export interface MenuItem {
  label: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  hint?: ReactNode;
}

/** A button that opens a list of actions: arrow keys move, Enter picks, Esc closes and
 * returns focus to the button. */
export function Menu({ label, trigger, items, align = "end" }: { label: string; trigger: ReactNode; items: (MenuItem | "separator")[]; align?: "start" | "end" }) {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const id = useId();

  const focusItem = (i: number) => {
    const els = list.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]:not(:disabled)");
    if (!els?.length) return;
    els[(i + els.length) % els.length].focus();
  };
  useEffect(() => {
    if (!open) return;
    focusItem(0);
    const onDown = (e: MouseEvent) => {
      if (!list.current?.contains(e.target as Node) && !button.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const close = () => {
    setOpen(false);
    button.current?.focus();
  };
  const onKey = (e: KeyboardEvent) => {
    const els = [...(list.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]:not(:disabled)") ?? [])];
    const at = els.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") (e.preventDefault(), focusItem(at + 1));
    else if (e.key === "ArrowUp") (e.preventDefault(), focusItem(at - 1));
    else if (e.key === "Home") (e.preventDefault(), focusItem(0));
    else if (e.key === "End") (e.preventDefault(), focusItem(-1));
    else if (e.key === "Escape" || e.key === "Tab") {
      e.preventDefault();
      close();
    }
  };
  return (
    <span className="ui-menu-wrap">
      <button
        ref={button}
        type="button"
        className="ui-icon-btn ui-icon-btn-md ui-icon-btn-ghost"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span aria-hidden="true">{trigger}</span>
      </button>
      {open && (
        <div ref={list} id={id} role="menu" aria-label={label} className={cx("ui-menu", `ui-menu-${align}`)} onKeyDown={onKey}>
          {items.map((it, i) =>
            it === "separator" ? (
              <div key={i} role="separator" className="ui-menu-sep" />
            ) : (
              <button
                key={i}
                type="button"
                role="menuitem"
                disabled={it.disabled}
                className={cx("ui-menu-item", it.danger && "is-danger")}
                onClick={() => {
                  setOpen(false);
                  it.onSelect();
                }}
              >
                <span>{it.label}</span>
                {it.hint && <span className="ui-menu-hint">{it.hint}</span>}
              </button>
            ),
          )}
        </div>
      )}
    </span>
  );
}
