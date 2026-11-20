import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { inTauri } from "./api";
import { Button, Card, Switch } from "./ui";
export type BackgroundStatus = { preferences: { launch_at_login: boolean; keep_running: boolean; suggestions_paused: boolean; call_suggestions?: boolean; disabled_apps?: string[] }; background_start: boolean; launch_at_login_supported: boolean; login_entry_present: boolean; call_detection_available: boolean; accessibility_required?: boolean; limitation: string };
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
    {value.accessibility_required && <><Button disabled={busy} onClick={()=>{setBusy(true);void invoke<boolean>("background_request_permissions").then(granted=>setError(granted ? null : "Enable notifications and Accessibility in System Settings, then return here.")).catch(e=>setError(String(e))).finally(()=>setBusy(false));}}>Allow macOS call suggestions</Button><p className="ui-field-hint">Accessibility reads call controls only. Notifications use macOS permissions and Focus settings. No screen recording permission is needed.</p></>}
    <Switch label="Suggest assistance for detected calls" checked={value.preferences.call_suggestions ?? false} disabled={busy || !value.call_detection_available} onChange={on => void save({ call_suggestions: on })} />
    <Switch label="Pause call suggestions" checked={value.preferences.suggestions_paused} disabled={busy || !value.call_detection_available} onChange={on => void save({ suggestions_paused: on })} />
    <p className="ui-field-hint">{value.limitation}</p>
    {value.call_detection_available && value.preferences.call_suggestions && <details><summary>Applications</summary>{["Zoom","Teams","Slack","Discord","WhatsApp","Browser","BlueStacks","LDPlayer","Phone Link", ...(value.accessibility_required ? ["FaceTime"] : [])].map(app=><Switch key={app} label={app} checked={!(value.preferences.disabled_apps ?? []).includes(app)} disabled={busy} onChange={on=>void save({disabled_apps:on ? (value.preferences.disabled_apps ?? []).filter(v=>v!==app) : [...(value.preferences.disabled_apps ?? []),app]})} />)}<p className="ui-field-hint">Experimental English call controls. Incoming calls and phone-side audio are not certified. Windows notification state and paused suggestions suppress prompts.</p></details>}</>}
    {error && <p role="alert">{error}</p>}
  </div></Card>;
}
