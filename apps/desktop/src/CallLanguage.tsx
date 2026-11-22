import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Button, Checkbox, Field, Select } from "./ui";
import { Icon } from "./ui/Icon";

type Preference = { spoken_language: string };
type Language = { id: string; name: string; native_name?: string; direction?: "ltr" | "rtl" };
type Catalog = { items: Language[] };
function validCatalog(value: unknown): value is Catalog {
  if (!value || typeof value !== "object" || !("items" in value) || !Array.isArray(value.items)) return false;
  const seen = new Set<string>();
  return value.items.every(item => {
    if (!item || typeof item !== "object" || typeof item.id !== "string" || !/^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$/.test(item.id)
      || typeof item.name !== "string" || !item.name.trim() || item.name.length > 100
      || (item.native_name !== undefined && (typeof item.native_name !== "string" || !item.native_name.trim() || item.native_name.length > 100))
      || (item.direction !== undefined && item.direction !== "ltr" && item.direction !== "rtl") || seen.has(item.id)) return false;
    seen.add(item.id); return true;
  });
}
const request = <T,>(method: string, path: string, body?: unknown) => invoke<T>("api_call", { method, path, body: body ?? null });

/** Mounted under the account/profile boundary; a draft never crosses profiles. */
export function CallLanguage({ value, onChange, disabled = false, onReady }: { value: string; onChange: (value: string) => void; disabled?: boolean; onReady?: (ready: boolean) => void }) {
  const [items, setItems] = useState<Catalog["items"]>([{ id: "en", name: "English" }]);
  const [error, setError] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);
  const [legacyEnglish, setLegacyEnglish] = useState(false);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true); setLegacyEnglish(false); onReady?.(false);
    Promise.all([request<Catalog>("GET", "/call-languages"), request<Preference>("GET", "/profile/call-preferences")]).then(([catalog, pref]) => {
      if (!alive) return;
      if (!validCatalog(catalog) || !pref || typeof pref.spoken_language !== "string") throw new Error("The server returned invalid language settings. Retry or update the server.");
      setItems(catalog.items); onChange(pref.spoken_language); setError(null); onReady?.(catalog.items.some(item => item.id === pref.spoken_language));
    }).catch(e => { if (alive) { if (String(e).includes("HTTP 404")) { setLegacyEnglish(true); setItems([{ id: "en", name: "English" }]); setRemember(false); setError(null); onChange("en"); onReady?.(true); } else setError(String(e)); } }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [revision]); // Profile switches remount this component.
  const change = async (language: string) => {
    onChange(language); onReady?.(items.some(item => item.id === language)); setError(null);
    if (remember) {
      try { await request("PATCH", "/profile/call-preferences", { spoken_language: language }); }
      catch (e) { setError(`Could not save the profile default: ${String(e)}`); }
    }
  };
  const selection = items.find(item => item.id === value);
  return <details className="call-options ui-disclosure"><summary><Icon name="settings" size={18} /><span>Customize</span><span className="ui-disclosure-meta">{loading ? "Loading languages…" : error ? "Language needs attention" : `${selection?.name ?? value} · call language`}</span></summary><div className="ui-disclosure-body ui-stack">
    <Field label="Language" hint="Choose before starting. The language stays fixed during the call.">
      <Select aria-label="Language" value={value} disabled={disabled || loading} onChange={e => void change(e.target.value)}>
        {!items.some(item => item.id === value) && <option value={value} disabled>{value} · unavailable</option>}
        {items.map(item => <option key={item.id} value={item.id} lang={item.id} dir={item.direction ?? "auto"}>{item.native_name && item.native_name !== item.name ? `${item.native_name} · ${item.name}` : item.name}</option>)}
      </Select>
    </Field>
    {legacyEnglish && <p className="ui-field-hint">Using English compatibility mode. Update the Career Pilot server to choose other languages.</p>}
    <Checkbox label="Save selection as this profile’s default" checked={remember} disabled={disabled || loading || legacyEnglish} onChange={checked => {
      setRemember(checked);
      if (checked) request("PATCH", "/profile/call-preferences", { spoken_language: value }).catch(e => setError(String(e)));
    }} />
    {error && <p role="alert">{error} <Button size="sm" disabled={disabled} onClick={() => setRevision(n => n + 1)}>Retry language settings</Button></p>}
  </div></details>;
}
