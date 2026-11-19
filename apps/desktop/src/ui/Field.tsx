// Form fields: the label above, a hint, and an inline error tied to the control.
import { cloneElement, forwardRef, isValidElement, useId, type InputHTMLAttributes, type ReactElement, type ReactNode, type TextareaHTMLAttributes } from "react";
import { cx } from "./Button";
import { HelpTip } from "./Feedback";

export function Field({
  label,
  hint,
  error,
  optional,
  children,
  className,
  inline,
  hideLabel,
  hintMode = "inline",
}: {
  label: ReactNode;
  hint?: ReactNode;
  /** Compact explanatory help beside the title; validation stays inline. */
  hintMode?: "inline" | "tooltip";
  error?: string | null;
  optional?: boolean;
  /** The control. It gets the id, aria-describedby and aria-invalid. */
  children: ReactElement<{ id?: string; "aria-describedby"?: string; "aria-invalid"?: boolean }>;
  className?: string;
  /** Label beside the control (checkbox rows, short controls). */
  inline?: boolean;
  /** Keep an accessible name without a visible heading above a compact control. */
  hideLabel?: boolean;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, hintId].filter(Boolean).join(" ") || undefined;
  const control = isValidElement(children)
    ? cloneElement(children, { id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })
    : children;
  const help = hint && hintMode === "tooltip" && !hideLabel;
  const heading = <label className={cx("ui-field-label", hideLabel && "ui-visually-hidden")} htmlFor={id}>
    {label}
    {optional && <span className="ui-field-optional"> (optional)</span>}
  </label>;
  return (
    <div className={cx("ui-field", inline && "ui-field-inline", error && "has-error", className)}>
      {help ? <div className="ui-field-heading">{heading}<HelpTip label={label}>{hint}</HelpTip></div> : heading}
      <div className="ui-field-control">{control}</div>
      {error && (
        <p className="ui-field-error" id={errorId} role="alert">
          {error}
        </p>
      )}
      {hint && (
        <p className={help ? "ui-visually-hidden" : "ui-field-hint"} id={hintId}>
          {hint}
        </p>
      )}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...rest }, ref) {
  return <input ref={ref} className={cx("ui-input", className)} {...rest} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...rest }, ref) {
  return <textarea ref={ref} className={cx("ui-input", "ui-textarea", className)} {...rest} />;
});

export { Select } from "./Select";

export function Checkbox({
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
    <div className="ui-check">
      <input id={id} type="checkbox" className="ui-checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} aria-describedby={hint ? `${id}-hint` : undefined} />
      <label htmlFor={id}>{label}</label>
      {hint && (
        <p className="ui-field-hint" id={`${id}-hint`}>
          {hint}
        </p>
      )}
    </div>
  );
}

/** Required-field and shape checks, run on blur and on submit. */
export function required(value: string, what: string): string | null {
  return value.trim() ? null : `Enter ${what}.`;
}

export function url(value: string): string | null {
  if (!value.trim()) return null;
  try {
    const u = new URL(value.trim());
    return u.protocol === "http:" || u.protocol === "https:" ? null : "Use an http or https link.";
  } catch {
    return "That is not a link. It should start with https://";
  }
}
