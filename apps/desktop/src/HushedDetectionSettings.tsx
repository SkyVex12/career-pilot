import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Button, Checkbox, Field, Input, Switch } from "./ui";

export type HushedPreferences = { enabled: boolean; adb_path: string; serials: string[] };
export type HushedDetectionStatus = { state: string; message: string; connected: number; monitored: number };

function localEndpoint(value: string): boolean {
  const match = /^127\.0\.0\.1:([1-9]\d{0,4})$/.exec(value.trim());
  return Boolean(match && Number(match[1]) <= 65535);
}

export function HushedDetectionSettings({ preferences, defaultAdb = "", supported, disabled, detection, onSave }: {
  preferences?: HushedPreferences;
  defaultAdb?: string;
  supported: boolean;
  disabled: boolean;
  detection?: HushedDetectionStatus | null;
  onSave: (preferences: HushedPreferences) => Promise<boolean>;
}) {
  const savedEnabled = preferences?.enabled ?? false;
  const savedAdb = preferences?.adb_path || defaultAdb;
  const savedSerials = JSON.stringify(preferences?.serials ?? []);
  const [enabled, setEnabled] = useState(savedEnabled);
  const [adbPath, setAdbPath] = useState(savedAdb);
  const [serials, setSerials] = useState<string[]>(preferences?.serials ?? []);
  const [devices, setDevices] = useState<string[] | null>(null);
  const [endpoint, setEndpoint] = useState("127.0.0.1:5555");
  const [operation, setOperation] = useState<"finding" | "connecting" | "saving" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const alive = useRef(false);
  const revision = useRef(0);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; revision.current++; };
  }, []);
  // Other background preferences can be saved while this form has an unsaved draft.
  // Reset the draft only when the saved Hushed values actually change.
  useEffect(() => {
    setEnabled(savedEnabled); setAdbPath(savedAdb); setSerials(JSON.parse(savedSerials) as string[]);
  }, [savedEnabled, savedAdb, savedSerials]);
  const clearFeedback = () => { setError(null); setNotice(null); };
  const findDevices = async (connect: boolean) => {
    clearFeedback();
    if (!adbPath.trim()) { setError("Enter the full path to LDPlayer's adb.exe."); return; }
    if (connect && !localEndpoint(endpoint)) { setError("Use 127.0.0.1 with a port from 1 to 65535, such as 127.0.0.1:5555."); return; }
    const current = ++revision.current;
    setOperation(connect ? "connecting" : "finding");
    try {
      const found = await invoke<string[]>(connect ? "background_hushed_connect" : "background_hushed_devices", {
        adbPath: adbPath.trim(), ...(connect ? { serial: endpoint.trim() } : {}),
      });
      if (!alive.current || current !== revision.current) return;
      if (!Array.isArray(found) || found.some(value => typeof value !== "string")) throw new Error("The emulator list could not be read.");
      const next = [...new Set(found)];
      setDevices(next);
      setNotice(next.length ? "Select the emulators to monitor, then save." : "No connected emulators found. Start LDPlayer and enable its local ADB connection, then try again.");
    } catch (e) { if (alive.current && current === revision.current) setError(String(e)); }
    finally { if (alive.current && current === revision.current) setOperation(null); }
  };
  const save = async () => {
    clearFeedback();
    if (enabled && !adbPath.trim()) { setError("Enter the full path to LDPlayer's adb.exe."); return; }
    if (enabled && !serials.length) { setError("Find emulators and select at least one before enabling detection."); return; }
    const current = ++revision.current;
    setOperation("saving");
    try {
      const saved = await onSave({ enabled, adb_path: adbPath.trim(), serials });
      if (!alive.current || current !== revision.current) return;
      if (saved) setNotice("Hushed detection settings saved. Use Check detection to see its status.");
      else setError("Hushed detection settings were not saved. Check the error below and try again.");
    } catch (e) { if (alive.current && current === revision.current) setError(String(e)); }
    finally { if (alive.current && current === revision.current) setOperation(null); }
  };
  const busy = disabled || operation !== null;
  const available = [...new Set([...(devices ?? []), ...serials])];
  const dirty = enabled !== savedEnabled || adbPath.trim() !== savedAdb || JSON.stringify(serials) !== savedSerials;
  return <details>
    <summary>Hushed in LDPlayer</summary>
    <div className="ui-stack">
      {!supported ? <p className="ui-field-hint">Hushed detection in LDPlayer is available on Windows only.</p> : <>
        <Switch label="Detect Hushed calls" checked={enabled} disabled={busy} onChange={on => { setEnabled(on); clearFeedback(); }} />
        <p className="ui-field-hint">Enable local ADB in LDPlayer first. Select up to four emulators. Detection reads Hushed call-state metadata; no audio, caller details, or messages are saved.</p>
        <Field label="LDPlayer ADB executable" hint="Full path to LDPlayer's adb.exe."><Input value={adbPath} disabled={busy} spellCheck={false} autoComplete="off" onChange={e => { setAdbPath(e.target.value); setDevices(null); clearFeedback(); }} /></Field>
        <div className="ui-row-inline"><Button disabled={busy} onClick={() => void findDevices(false)}>{operation === "finding" ? "Finding..." : "Find emulators"}</Button></div>
        {available.map(serial => <Checkbox key={serial} label={serial} checked={serials.includes(serial)} disabled={busy || (!serials.includes(serial) && serials.length >= 4)} hint={devices && !devices.includes(serial) ? "Saved selection; not currently connected." : undefined} onChange={on => { setSerials(current => on ? [...current, serial] : current.filter(value => value !== serial)); clearFeedback(); }} />)}
        <details>
          <summary>Connect a local emulator</summary>
          <div className="ui-stack">
            <Field label="Local emulator address" hint="Use 127.0.0.1 and the ADB port for this LDPlayer instance."><Input value={endpoint} disabled={busy} spellCheck={false} autoComplete="off" onChange={e => { setEndpoint(e.target.value); clearFeedback(); }} /></Field>
            <div className="ui-row-inline"><Button disabled={busy} onClick={() => void findDevices(true)}>{operation === "connecting" ? "Connecting..." : "Connect local emulator"}</Button></div>
          </div>
        </details>
        <p className="ui-field-hint">Call suggestions must be on, unpaused, and enabled for LDPlayer under Applications. This mode reads Android call state and supported Hushed notifications. Microphone activity is not required. It is experimental and has not been verified against your Hushed version.</p>
        <div className="ui-row-inline">
          <Button disabled={busy || !dirty} onClick={() => void save()}>{operation === "saving" ? "Saving..." : "Save Hushed detection"}</Button>
          {(operation === "finding" || operation === "connecting") && <Button onClick={() => { revision.current++; setOperation(null); setNotice("Stopped waiting for the emulator list. Detection settings are unchanged."); }}>Cancel</Button>}
        </div>
      </>}
      {error && <p role="alert">{error}</p>}
      {notice && <p className="ui-field-hint" role="status">{notice}</p>}
      {detection && <p className="ui-field-hint" role="status" aria-label="Hushed detection status">Hushed: {detection.message || detection.state}. {detection.monitored} selected, {detection.connected} connected.</p>}
    </div>
  </details>;
}
