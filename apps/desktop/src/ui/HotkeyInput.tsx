// Records a hotkey by pressing it: Change, press the keys, done. Esc cancels; Clear unbinds.
import { useEffect, useRef, useState } from "react";
import { prettyKey, recordKey } from "../hotkeys";
import { Button } from "./Button";
import { Kbd } from "./Feedback";

export function HotkeyInput({
  label,
  value,
  onChange,
  error,
  hint,
  clearable = true,
}: {
  label: string;
  value: string;
  /** The recorded accelerator, or "" when cleared. May reject (shown as the error). */
  onChange: (accel: string) => unknown;
  error?: string | null;
  hint?: string;
  clearable?: boolean;
}) {
  const [recording, setRecording] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const change = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (recording) box.current?.focus();
  }, [recording]);

  const stop = () => {
    setRecording(false);
    change.current?.focus();
  };
  const shown = error ?? problem;
  const hintId = `${label.replace(/\W+/g, "-")}-hint`;
  return (
    <div className="ui-hotkey">
      <div className="ui-hotkey-label">
        <span>{label}</span>
        {hint && (
          <span className="ui-field-hint" id={hintId}>
            {hint}
          </span>
        )}
      </div>
      <div className="ui-hotkey-control">
        {recording ? (
          <div
            ref={box}
            tabIndex={0}
            role="textbox"
            aria-label={`${label}: press the new keys, Escape to cancel`}
            className="ui-hotkey-recording"
            onBlur={() => setRecording(false)}
            onKeyDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              if (e.key === "Escape" && !e.ctrlKey && !e.altKey && !e.shiftKey && !e.metaKey) {
                setProblem(null);
                stop();
                return;
              }
              const r = recordKey(e);
              if ("waiting" in r) return;
              if ("error" in r) {
                setProblem(r.error);
                return;
              }
              setProblem(null);
              stop();
              Promise.resolve(onChange(r.accel)).catch((err) => setProblem(String(err)));
            }}
          >
            Press the keys…
          </div>
        ) : value ? (
          <Kbd>{prettyKey(value)}</Kbd>
        ) : (
          <span className="ui-muted">Not set</span>
        )}
        <Button ref={change} size="sm" onClick={() => (setProblem(null), setRecording(true))} aria-describedby={hint ? hintId : undefined} disabled={recording}>
          {value ? "Change" : "Set"}
        </Button>
        {clearable && value && !recording && (
          <Button size="sm" variant="ghost" onClick={() => Promise.resolve(onChange("")).catch((err) => setProblem(String(err)))} aria-label={`Clear ${label}`}>
            Clear
          </Button>
        )}
      </div>
      {shown && (
        <p className="ui-field-error ui-hotkey-error" role="alert">
          {shown}
        </p>
      )}
    </div>
  );
}
