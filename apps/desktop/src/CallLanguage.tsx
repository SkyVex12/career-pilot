import { useEffect, useRef, useState, type ReactNode } from "react";
import { request } from "./api";
import { Button, Checkbox, Field, HelpTip, Select } from "./ui";
import { Icon } from "./ui/Icon";
import { useDataActivity } from "./usePolling";

type Preference = { spoken_language: string };
export type Language = { id: string; name: string; native_name?: string; direction?: "ltr" | "rtl" };
export type Catalog = { items: Language[] };
export function validCatalog(value: unknown): value is Catalog {
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

/** Mounted under the account/profile boundary; a draft never crosses profiles. */
export function CallLanguage({ value, onChange, disabled = false, onReady, answerFormat, children }: { value: string; onChange: (value: string) => void; disabled?: boolean; onReady?: (ready: boolean) => void; answerFormat?: ReactNode; children?: ReactNode }) {
  const active = useDataActivity();
  const initialized = useRef(false);
  const [items, setItems] = useState<Catalog["items"]>([{ id: "en", name: "English" }]);
  const [error, setError] = useState<string | null>(null);
  const [remember, setRemember] = useState(false);
  const [expanded, setExpanded] = useState(true);
  const [legacyEnglish, setLegacyEnglish] = useState(false);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    setLoading(true); setLegacyEnglish(false); onReady?.(false);
    const policy = revision ? { refresh: true } : undefined;
    Promise.all([request<Catalog>("GET", "/call-languages", undefined, policy), request<Preference>("GET", "/profile/call-preferences", undefined, policy)]).then(([catalog, pref]) => {
      if (!alive) return;
      if (!validCatalog(catalog) || !pref || typeof pref.spoken_language !== "string") throw new Error("The server returned invalid language settings. Retry or update the server.");
      const selection = initialized.current ? value : pref.spoken_language;
      if (!initialized.current) onChange(selection);
      initialized.current = true;
      setItems(catalog.items); setError(null); onReady?.(catalog.items.some(item => item.id === selection));
    }).catch(e => { if (alive) { if (String(e).includes("HTTP 404")) { setLegacyEnglish(true); setItems([{ id: "en", name: "English" }]); setRemember(false); setError(null); onChange("en"); onReady?.(true); } else setError(String(e)); } }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [revision, active]); // Profile switches remount this component.
  const change = async (language: string) => {
    onChange(language); onReady?.(items.some(item => item.id === language)); setError(null);
    if (remember) {
      try { await request("PATCH", "/profile/call-preferences", { spoken_language: language }); }
      catch (e) { setError(`Could not save the profile default: ${String(e)}`); }
    }
  };
  const selection = items.find(item => item.id === value);
  return <details className="call-options ui-disclosure" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary>
      <Icon name="settings" size={16} /><span className="call-options-title">Customize</span>
      <span className="ui-disclosure-meta" role={error && !loading ? "alert" : undefined}>
        {loading ? "Loading languages…" : error ? <>Needs attention<span className="ui-visually-hidden">{error}</span></> : `${selection?.name ?? value} · call language`}
      </span>
    </summary>
    <div className="ui-disclosure-body call-customize">
      <div className="customize-group">
        <div className="customize-row">
          <span className="customize-label">Language<HelpTip label={error ? "Language error" : "Language"}>{error && <>{error} </>}Spoken language for transcription and answers. Choose “Use as default” to remember it for future calls with this profile.{legacyEnglish && <> Using English compatibility mode. Update the CoRuby server to choose other languages.</>}</HelpTip></span>
          <div className="customize-control customize-language-control">
            <Field label="Language" hideLabel>
              <Select aria-label="Language" searchLabel="Search languages" value={value} disabled={disabled || loading} onChange={e => void change(e.target.value)}>
                {!items.some(item => item.id === value) && <option value={value} disabled>{value} · unavailable</option>}
                {items.map(item => <option key={item.id} value={item.id} lang={item.id} dir={item.direction ?? "auto"}>{item.native_name && item.native_name !== item.name ? `${item.native_name} · ${item.name}` : item.name}</option>)}
              </Select>
            </Field>
            <Checkbox label="Use as default" checked={remember} disabled={disabled || loading || legacyEnglish} onChange={checked => {
              setRemember(checked);
              if (checked) request("PATCH", "/profile/call-preferences", { spoken_language: value }).catch(e => setError(String(e)));
            }} />
            {error && <Button size="sm" aria-label="Retry language settings" disabled={disabled || loading} onClick={() => setRevision(n => n + 1)}>Retry</Button>}
          </div>
        </div>
        {answerFormat}
      </div>
      {children && <div className="customize-group">{children}</div>}
    </div>
  </details>;
}
