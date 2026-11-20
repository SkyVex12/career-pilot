import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { inTauri } from "./api";
import { Button, Card, Switch } from "./ui";
import { HushedDetectionSettings, type HushedDetectionStatus, type HushedPreferences } from "./HushedDetectionSettings";
export type BackgroundStatus = { preferences: { launch_at_login: boolean; keep_running: boolean; suggestions_paused: boolean; call_suggestions?: boolean; disabled_apps?: string[]; hushed?: HushedPreferences }; background_start: boolean; launch_at_login_supported: boolean; login_entry_present: boolean; call_detection_available: boolean; accessibility_required?: boolean; hushed_supported?: boolean; hushed_adb_default?: string; limitation: string };
export type DetectionStatus = {
  state: string; last_checked_unix_ms: number | null; error: string | null; presentation_error: string | null;
  pending: { platform: string; kind: string } | null;
  diagnostics: { microphone_active: boolean | null; microphone_apps: string[]; inspected_windows: number; matched_calls: string[]; inspection_limited: boolean; notifications_allowed: boolean } | null;
  hushed?: HushedDetectionStatus | null;
};
function detectionMessage(value: DetectionStatus): string {
  if (value.presentation_error && value.state === "suggestion_pending") return `Call detected, but the suggestion could not open: ${value.presentation_error}. Check again to retry.`;
  const messages: Record<string, string> = {
    unavailable: "Call detection is unavailable on this system.",
    disabled: "Call suggestions are off. Enable them above to detect calls.",
    paused: "Call suggestions are paused. Resume them above to receive prompts.",
    checking: "Waiting for the next detection check...",
    error: `Detection could not complete: ${value.error || "native check failed"}. Check again to retry.`,
    active_session: "Copilot is already running a session. A new-call prompt is suppressed.",
    notifications_suppressed: "System notification settings or Focus are suppressing call prompts. Allow notifications, then check again.",
    suggestion_pending: `${value.pending?.platform || "A call"} detected. A suggestion is waiting for your response.`,
    app_disabled: "This application's call suggestions are disabled. Enable it under Applications.",
    waiting_microphone: "Waiting for microphone activity. Join the call and enable its microphone, then check again.",
    no_call_controls: "No supported call controls found. Keep the call window open, then check again.",
    watching: "Watching for a supported call. Checks run every five seconds.",
    watching_hushed: "Watching for Hushed call activity. Microphone activity is not required.",
  };
  return messages[value.state] || "Detection status is unavailable. Check again to retry.";
}
export function BackgroundSettings() {
  const [value, setValue] = useState<BackgroundStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [detection, setDetection] = useState<DetectionStatus | null>(null);
  const [detectionError, setDetectionError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const alive = useRef(false);
  const revision = useRef(0);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    alive.current = true;
    if (inTauri) void invoke<BackgroundStatus>("background_status").then(v => { if (alive.current) setValue(v); }).catch(e => { if (alive.current) setError(String(e)); });
    return () => { alive.current = false; revision.current++; if (retryTimer.current) clearTimeout(retryTimer.current); };
  }, []);
  const checkDetection = async (followup = false) => {
    const current = ++revision.current;
    if (retryTimer.current) clearTimeout(retryTimer.current);
    setChecking(true); setDetectionError(null);
    try {
      const next = await invoke<DetectionStatus>("background_detection_status");
      if (!alive.current || current !== revision.current) return;
      setDetection(next);
      // One delayed cached read lets the native five-second poll finish without extra scans.
      if (next?.state === "checking" && !followup) retryTimer.current = setTimeout(() => { if (alive.current) void checkDetection(true); }, 5500);
    } catch (e) { if (alive.current && current === revision.current) setDetectionError(`Detection could not be checked: ${String(e)}. Try again.`); }
    finally { if (alive.current && current === revision.current) setChecking(false); }
  };
  if (!inTauri || (!value && !error)) return null;
  const save = async (patch: Partial<BackgroundStatus["preferences"]>) => {
    if (!value) return false;
    setBusy(true); setError(null); setDetection(null); setChecking(false); setDetectionError(null); revision.current++; if (retryTimer.current) clearTimeout(retryTimer.current);
    try { const next = await invoke<BackgroundStatus>("background_set_preferences", { preferences: { ...value.preferences, ...patch } }); if (!alive.current) return false; setValue(next); return true; }
    catch (e) { if (alive.current) setError(String(e)); return false; } finally { if (alive.current) setBusy(false); }
  };
  return <Card title="Background"><div className="ui-stack">
    {value && <><Switch label="Launch at login" checked={value.preferences.launch_at_login} disabled={busy || !value.launch_at_login_supported} onChange={on => void save({ launch_at_login: on })} />
    <Switch label="Keep running in the background" checked={value.preferences.keep_running} disabled={busy} onChange={on => void save({ keep_running: on })} />
    {value.accessibility_required && <><Button disabled={busy} onClick={()=>{setBusy(true);void invoke<boolean>("background_request_permissions").then(granted=>setError(granted ? null : "Enable notifications and Accessibility in System Settings, then return here.")).catch(e=>setError(String(e))).finally(()=>setBusy(false));}}>Allow macOS call suggestions</Button><p className="ui-field-hint">Accessibility reads call controls only. Notifications use macOS permissions and Focus settings. No screen recording permission is needed.</p></>}
    <Switch label="Suggest assistance for detected calls" checked={value.preferences.call_suggestions ?? false} disabled={busy || !value.call_detection_available} onChange={on => void save({ call_suggestions: on })} />
    <Switch label="Pause call suggestions" checked={value.preferences.suggestions_paused} disabled={busy || !value.call_detection_available} onChange={on => void save({ suggestions_paused: on })} />
    <p className="ui-field-hint">{value.limitation}</p>
    <div className="ui-stack"><div className="ui-row-inline"><Button disabled={busy || checking} onClick={() => void checkDetection()}>{checking ? "Checking..." : "Check detection"}</Button></div>
    {detectionError ? <p role="alert">{detectionError}</p> : detection && <p className="ui-field-hint" role="status">{detectionMessage(detection)}{detection.diagnostics?.inspection_limited && " The call controls could not all be checked. Keep the call window visible and check again."}</p>}
    </div>
    <HushedDetectionSettings preferences={value.preferences.hushed} defaultAdb={value.hushed_adb_default} supported={value.hushed_supported ?? false} disabled={busy} detection={detection?.hushed} onSave={preferences => save({ hushed: preferences })} />
    {value.call_detection_available && value.preferences.call_suggestions && <details><summary>Applications</summary>{["Zoom","Teams","Slack","Discord","WhatsApp","Browser","BlueStacks","LDPlayer","Phone Link", ...(value.accessibility_required ? ["FaceTime"] : [])].map(app=><Switch key={app} label={app} checked={!(value.preferences.disabled_apps ?? []).includes(app)} disabled={busy} onChange={on=>void save({disabled_apps:on ? (value.preferences.disabled_apps ?? []).filter(v=>v!==app) : [...(value.preferences.disabled_apps ?? []),app]})} />)}<p className="ui-field-hint">Experimental English call controls. Incoming calls and phone-side audio are not certified. Windows notification state and paused suggestions suppress prompts.</p></details>}</>}
    {error && <p role="alert">{error}</p>}
  </div></Card>;
}
