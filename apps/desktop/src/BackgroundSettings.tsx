import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { inTauri } from "./api";
import { Card, Switch } from "./ui";
export type BackgroundStatus = { preferences: { launch_at_login: boolean; keep_running: boolean; suggestions_paused: boolean }; background_start: boolean; launch_at_login_supported: boolean; login_entry_present: boolean; call_detection_available: boolean; limitation: string };
export function BackgroundSettings() {
  const [value, setValue] = useState<BackgroundStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (inTauri) invoke<BackgroundStatus>("background_status").then(setValue).catch(e => setError(String(e))); }, []);
  if (!inTauri || (!value && !error)) return null;
  const save = async (patch: Partial<BackgroundStatus["preferences"]>) => {
    if (!value) return;
    setBusy(true); setError(null);
    try { setValue(await invoke<BackgroundStatus>("background_set_preferences", { preferences: { ...value.preferences, ...patch } })); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return <Card title="Background"><div className="ui-stack">
    {value && <><Switch label="Launch at login" checked={value.preferences.launch_at_login} disabled={busy || !value.launch_at_login_supported} onChange={on => void save({ launch_at_login: on })} />
    <Switch label="Keep running in the background" checked={value.preferences.keep_running} disabled={busy} onChange={on => void save({ keep_running: on })} />
    <Switch label="Pause call suggestions" checked={value.preferences.suggestions_paused} disabled={busy || !value.call_detection_available} onChange={on => void save({ suggestions_paused: on })} />
    {!value.call_detection_available && <p className="ui-field-hint">{value.limitation}</p>}</>}
    {error && <p role="alert">{error}</p>}
  </div></Card>;
}
