// A device row with its own Test button (Settings, and the Session tab before a call): the
// device's control on the left, Test on the right, and while it runs a level bar under the
// row, then the verdict. The Rust core does the work (audio_check, one part at a time).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, onAudioCheck, type AudioPart } from "./api";
import { type CheckState, meterFill, partLevel, partLine, partVerdict } from "./audiotest";

export function DeviceTest({
  part,
  micDevice,
  outputDevice,
  disabled,
  disabledNote,
  children,
}: {
  part: AudioPart;
  micDevice: string | null;
  outputDevice: string | null;
  disabled?: boolean;
  /** Said under the row while testing is not possible. */
  disabledNote?: string;
  /** The device's dropdown, or a label when the device is chosen elsewhere. */
  children: ReactNode;
}) {
  const [check, setCheck] = useState<CheckState>({ state: "idle" });
  const running = check.state === "running";
  const runningRef = useRef(false);
  useEffect(() => {
    const off = onAudioCheck((meter) => {
      if (runningRef.current) setCheck({ state: "running", meter });
    });
    return () => {
      off.then((f) => f());
    };
  }, []);
  // A different device makes the last verdict stale.
  useEffect(() => {
    if (!runningRef.current) setCheck({ state: "idle" });
  }, [micDevice, outputDevice]);

  const run = async () => {
    runningRef.current = true;
    setCheck({ state: "running", meter: null });
    try {
      const result = await api.audioCheck(micDevice, outputDevice, part);
      setCheck({ state: "done", result });
    } catch (e) {
      setCheck({ state: "failed", message: String(e) });
    } finally {
      runningRef.current = false;
    }
  };
  const line = partLine(part, check);
  const ok = check.state === "done" ? partVerdict(part, check.result).ok : null;
  return (
    <div className="device-test">
      <div className="device-row">
        <div className="device-control">{children}</div>
        <button
          className="small"
          onClick={run}
          disabled={disabled || running}
          title={part === "mic" ? "Speak for 3 seconds to check this microphone" : "Plays a short tone on this device to check the app hears it"}
        >
          {running && <span className="spinner" />}
          {running ? "Testing…" : "Test"}
        </button>
      </div>
      {running && (
        <div className="audio-meter-bar" role="meter" aria-label={part === "mic" ? "Microphone level" : "Call audio level"}>
          <div className="audio-meter-fill" style={{ width: `${Math.round(meterFill(partLevel(part, check.meter)) * 100)}%` }} />
        </div>
      )}
      {line && (
        <p className={`note${ok === true ? " ok" : ok === false || check.state === "failed" ? " bad" : ""}`} aria-live="polite">
          {ok === true ? "✓ " : ok === false ? "✗ " : ""}
          {line}
        </p>
      )}
      {disabled && disabledNote && <p className="note">{disabledNote}</p>}
    </div>
  );
}
