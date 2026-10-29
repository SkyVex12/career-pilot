// "Test audio": the chosen microphone and call-audio device, checked before a call (Settings
// and the Session tab). The Rust core does the work (audio_check); this shows the meters and
// the verdicts.
import { useEffect, useRef, useState } from "react";
import { api, onAudioCheck } from "./api";
import { type CheckState, checkLine, meterFill } from "./audiotest";

function Meter({ label, db, active }: { label: string; db: number | null; active: boolean }) {
  return (
    <div className={`audio-meter${active ? " active" : ""}`}>
      <span className="note">{label}</span>
      <div className="audio-meter-bar">
        <div className="audio-meter-fill" style={{ width: `${Math.round(meterFill(db ?? -100) * 100)}%` }} />
      </div>
    </div>
  );
}

export function AudioCheck({
  micDevice,
  outputDevice,
  disabled,
  note,
}: {
  micDevice: string | null;
  outputDevice: string | null;
  disabled?: boolean;
  note?: string;
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
  const run = async () => {
    runningRef.current = true;
    setCheck({ state: "running", meter: null });
    try {
      const result = await api.audioCheck(micDevice, outputDevice);
      setCheck({ state: "done", result });
    } catch (e) {
      setCheck({ state: "failed", message: String(e) });
    } finally {
      runningRef.current = false;
    }
  };
  const meter = check.state === "running" ? check.meter : null;
  return (
    <div className="audio-check">
      <div className="row">
        <button className="small" onClick={run} disabled={disabled || running}>
          {running && <span className="spinner" />}
          {running ? "Testing…" : "Test audio"}
        </button>
        <span className={`note${check.state === "failed" ? " bad" : ""}`} aria-live="polite">
          {checkLine(check)}
        </span>
      </div>
      {running && (
        <div className="audio-meters">
          <Meter label="Microphone" db={meter?.mic_db ?? null} active={meter?.phase !== "tone"} />
          <Meter label="Call audio" db={meter?.call_db ?? null} active={meter?.phase === "tone"} />
        </div>
      )}
      {check.state === "done" && (
        <ul className="audio-verdicts">
          <li className={check.result.mic.ok ? "ok" : "bad"}>
            <b>Microphone:</b> {check.result.mic.message}
          </li>
          <li className={check.result.call.ok ? "ok" : "bad"}>
            <b>Call audio:</b> {check.result.call.message}
          </li>
        </ul>
      )}
      {note && <p className="note">{note}</p>}
    </div>
  );
}
