import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Button, Checkbox, Field, Select } from "./ui";

type Preference = { spoken_language: string };
type Catalog = { items: { id: string; name: string }[] };
const request = <T,>(method: string, path: string, body?: unknown) => invoke<T>("api_call", { method, path, body: body ?? null });

/** Mounted under the account/profile boundary; a draft never crosses profiles. */
export function CallLanguage({ value, onChange, disabled = false, onReady }: { value: string; onChange: (value: string) => void; disabled?: boolean; onReady?: (ready: boolean) => void }) {
  const [items, setItems] = useState<Catalog["items"]>([{ id: "en", name: "English" }]);
  const [error, setError] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true); onReady?.(false);
    Promise.all([request<Catalog>("GET", "/call-languages"), request<Preference>("GET", "/profile/call-preferences")]).then(([catalog, pref]) => {
      if (!alive) return;
      if (!Array.isArray(catalog.items) || typeof pref.spoken_language !== "string") throw new Error("The server returned invalid language settings. Retry or update the server.");
      setItems(catalog.items); onChange(pref.spoken_language); setError(null); onReady?.(catalog.items.some(item => item.id === pref.spoken_language));
    }).catch(e => { if (alive) { if (String(e).includes("HTTP 404")) { onChange("en"); onReady?.(true); } else setError(String(e)); } }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [revision]); // Profile switches remount this component.
  const change = async (language: string) => {
    onChange(language); onReady?.(items.some(item => item.id === language)); setError(null);
    if (remember) {
      try { await request("PATCH", "/profile/call-preferences", { spoken_language: language }); }
      catch (e) { setError(`Could not save the profile default: ${String(e)}`); }
    }
  };
  return <details className="call-options"><summary>Customize</summary><div className="ui-stack">
    <Field label="Language" hint="Choose before starting. The language stays fixed during the call.">
      <Select aria-label="Language" value={value} disabled={disabled || loading} onChange={e => void change(e.target.value)}>
        {!items.some(item => item.id === value) && <option value={value} disabled>{value} · unavailable</option>}
        {items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </Select>
    </Field>
    <Checkbox label="Save selection as this profile’s default" checked={remember} disabled={disabled || loading} onChange={checked => {
      setRemember(checked);
      if (checked) request("PATCH", "/profile/call-preferences", { spoken_language: value }).catch(e => setError(String(e)));
    }} />
    {error && <p role="alert">{error} <Button size="sm" disabled={disabled} onClick={() => setRevision(n => n + 1)}>Retry language settings</Button></p>}
  </div></details>;
}
