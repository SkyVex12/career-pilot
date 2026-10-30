// Dialogs (Radix: focus trap, Esc, focus returned on close) and toasts (a live region).
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import * as RadixDialog from "@radix-ui/react-dialog";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Button, cx } from "./Button";
import { Input } from "./Field";

/** Remembers what had focus when a dialog opened, and gives it back when it closes (the dialogs
 * open from our own buttons, not from a Radix trigger). */
function useReturnFocus(open: boolean) {
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (open) opener.current = document.activeElement as HTMLElement | null;
  }, [open]);
  return (e: Event) => {
    const el = opener.current;
    if (el && el.isConnected) {
      e.preventDefault();
      el.focus();
    }
  };
}

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = "md",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  const returnFocus = useReturnFocus(open);
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="ui-scrim" />
        <RadixDialog.Content className={cx("ui-dialog", `ui-dialog-${size}`)} onCloseAutoFocus={returnFocus}>
          <div className="ui-dialog-head">
            <RadixDialog.Title className="ui-dialog-title">{title}</RadixDialog.Title>
            <RadixDialog.Close className="ui-icon-btn ui-icon-btn-sm ui-icon-btn-ghost" aria-label="Close">
              <span aria-hidden="true">✕</span>
            </RadixDialog.Close>
          </div>
          {description ? (
            <RadixDialog.Description className="ui-dialog-desc">{description}</RadixDialog.Description>
          ) : (
            <RadixDialog.Description className="ui-visually-hidden">{typeof title === "string" ? title : "Dialog"}</RadixDialog.Description>
          )}
          <div className="ui-dialog-body">{children}</div>
          {footer && <div className="ui-dialog-foot">{footer}</div>}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

/** Asks before something that cannot be undone. With `typed`, the user types that word or name
 * first. The confirm button is not focused first: Enter alone never deletes. */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  children,
  confirmLabel,
  cancelLabel = "Cancel",
  danger = true,
  typed,
  onConfirm,
  extra,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  children?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  danger?: boolean;
  typed?: string;
  /** May return a promise: the dialog stays open and busy until it settles, and shows its error. */
  onConfirm: () => unknown;
  /** More choices between Cancel and the confirm button. */
  extra?: ReactNode;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const returnFocus = useReturnFocus(open);
  const ok = !typed || text.trim().toLowerCase() === typed.trim().toLowerCase();
  const change = (o: boolean) => {
    if (busy) return;
    if (!o) {
      setText("");
      setError(null);
    }
    onOpenChange(o);
  };
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
      setText("");
      onOpenChange(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <AlertDialog.Root open={open} onOpenChange={change}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="ui-scrim" />
        <AlertDialog.Content className={extra ? "ui-dialog ui-dialog-md" : "ui-dialog ui-dialog-sm"} onCloseAutoFocus={returnFocus}>
          <AlertDialog.Title className="ui-dialog-title">{title}</AlertDialog.Title>
          <AlertDialog.Description asChild>
            <div className="ui-dialog-desc">{children}</div>
          </AlertDialog.Description>
          {typed && (
            <div className="ui-field">
              <label className="ui-field-label" htmlFor="ui-confirm-typed">
                Type <b>{typed}</b> to confirm
              </label>
              <Input id="ui-confirm-typed" value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" spellCheck={false} />
            </div>
          )}
          {error && (
            <p className="ui-field-error" role="alert">
              {error}
            </p>
          )}
          <div className="ui-dialog-foot">
            <AlertDialog.Cancel asChild>
              <Button variant="ghost" disabled={busy}>
                {cancelLabel}
              </Button>
            </AlertDialog.Cancel>
            {extra}
            <Button variant={danger ? "danger" : "primary"} disabled={!ok} loading={busy} onClick={confirm}>
              {confirmLabel}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

export type ToastTone = "success" | "info" | "error";
interface ToastItem {
  id: number;
  tone: ToastTone;
  text: ReactNode;
  action?: { label: string; onClick: () => void };
}

const ToastContext = createContext<(t: Omit<ToastItem, "id">) => void>(() => {});

/** `toast({ tone, text, action })`: success and info leave after 5 s (paused while hovered or
 * focused), errors stay until dismissed. Announced politely; errors assertively. */
export function useToast() {
  return useContext(ToastContext);
}

let nextToast = 1;
const TOAST_MS = 5000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((t: Omit<ToastItem, "id">) => {
    setItems((cur) => [...cur.slice(-2), { ...t, id: nextToast++ }]);
  }, []);
  const remove = useCallback((id: number) => setItems((cur) => cur.filter((t) => t.id !== id)), []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <section className="ui-toast-viewport" aria-label="Notifications">
        <div aria-live="polite" className="ui-toast-region">
          {items
            .filter((t) => t.tone !== "error")
            .map((t) => (
              <Toast key={t.id} item={t} onClose={() => remove(t.id)} />
            ))}
        </div>
        <div aria-live="assertive" className="ui-toast-region">
          {items
            .filter((t) => t.tone === "error")
            .map((t) => (
              <Toast key={t.id} item={t} onClose={() => remove(t.id)} />
            ))}
        </div>
      </section>
    </ToastContext.Provider>
  );
}

function Toast({ item, onClose }: { item: ToastItem; onClose: () => void }) {
  const [paused, setPaused] = useState(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (item.tone === "error" || paused) return;
    const t = setTimeout(() => closeRef.current(), TOAST_MS);
    return () => clearTimeout(t);
  }, [item.tone, paused]);
  return (
    <div
      className={cx("ui-toast", `ui-toast-${item.tone}`)}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <span className="ui-toast-icon" aria-hidden="true">
        {item.tone === "success" ? "✓" : item.tone === "error" ? "✕" : "ℹ"}
      </span>
      <div className="ui-toast-text">{item.text}</div>
      {item.action && (
        <button
          type="button"
          className="ui-btn ui-btn-link ui-btn-sm"
          onClick={() => {
            item.action?.onClick();
            onClose();
          }}
        >
          <span className="ui-btn-label">{item.action.label}</span>
        </button>
      )}
      <button type="button" className="ui-icon-btn ui-icon-btn-sm ui-icon-btn-ghost" aria-label="Dismiss" onClick={onClose}>
        <span aria-hidden="true">✕</span>
      </button>
    </div>
  );
}
