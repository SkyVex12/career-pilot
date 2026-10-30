import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Spinner } from "./Feedback";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "danger-ghost" | "link";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner and keeps the width; the button is busy and does not click. */
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading = false, icon, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx("ui-btn", `ui-btn-${variant}`, `ui-btn-${size}`, loading && "is-loading", className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <Spinner size="sm" label={null} /> : icon}
      {children != null && <span className="ui-btn-label">{children}</span>}
    </button>
  );
});

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  /** The accessible name, also shown as the tooltip. Required: an icon alone names nothing. */
  label: string;
  icon: ReactNode;
  size?: "sm" | "md";
  variant?: "ghost" | "subtle";
  pressed?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, size = "md", variant = "ghost", pressed, className, type = "button", title, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={title ?? label}
      aria-pressed={pressed}
      className={cx("ui-icon-btn", `ui-icon-btn-${size}`, `ui-icon-btn-${variant}`, className)}
      {...rest}
    >
      <span aria-hidden="true">{icon}</span>
    </button>
  );
});

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
