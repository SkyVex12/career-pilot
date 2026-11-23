import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { api } from "./api";
import { validCatalog, type Catalog, type Language } from "./CallLanguage";
import type { SessionView } from "./session";
import { Button, Select } from "./ui";
import "./liveCallLanguage.css";

/** One selected language applies to both incoming speech and new answers. Existing text stays. */
export function LiveCallLanguage({ session, compact = false }: { session: SessionView; compact?: boolean }) {
  const [items, setItems] = useState<Language[]>([]);
  const [loading, setLoading] = useState(true), [loadError, setLoadError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [pending, setPending] = useState<{ id: string; language: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const lastRequest = useRef<string | null>(null);
  const connected = session.running && session.connection?.state === "live";

  useEffect(() => {
    const ticket = ++generation.current;
    setItems([]); setLoading(true); setLoadError(null); setPending(null); setError(null); lastRequest.current = null;
    void invoke<Catalog>("api_call", { method: "GET", path: "/call-languages", body: null }).then(catalog => {
      if (ticket !== generation.current) return;
      if (!validCatalog(catalog)) throw new Error("The server returned invalid language settings.");
      setItems(catalog.items);
    }).catch(reason => { if (ticket === generation.current) setLoadError(`Could not load call languages. ${String(reason)}`); })
      .finally(() => { if (ticket === generation.current) setLoading(false); });
    return () => { generation.current++; };
  }, [session.sessionId, revision]);

  useEffect(() => {
    if (!lastRequest.current || session.languageResult?.requestId !== lastRequest.current) return;
    setPending(null); setError(session.languageResult.error); lastRequest.current = null;
  }, [session.languageResult]);

  useEffect(() => {
    if (!connected && pending) { setPending(null); setError("Connection interrupted. The current language will be confirmed after reconnecting."); }
    else if (connected) setError(value => value?.startsWith("Connection interrupted.") ? null : value);
  }, [connected, pending]);

  useEffect(() => {
    if (!pending) return;
    const timer = setTimeout(() => { setPending(null); setError("The language change was not confirmed. Check the current language, then try again."); }, 30000);
    return () => clearTimeout(timer);
  }, [pending]);

  const change = async (language: string) => {
    if (!connected || session.seat || !session.languageSwitching || pending || language === session.spokenLanguage || !items.some(item => item.id === language)) return;
    const requestId = crypto.randomUUID(), ticket = generation.current;
    lastRequest.current = requestId; setPending({ id: requestId, language }); setError(null);
    try { await api.setSpokenLanguage(language, requestId); }
    catch (reason) { if (ticket === generation.current && lastRequest.current === requestId) { setPending(null); setError(`Could not change the call language. ${String(reason)}`); } }
  };

  const value = session.spokenLanguage ?? "";
  const pendingName = items.find(item => item.id === pending?.language)?.name ?? pending?.language;
  const disabled = loading || !items.length || !connected || !!session.seat || !session.languageSwitching || !value || !!pending;
  const hint = pending ? `Changing to ${pendingName}…` : !connected ? "Reconnect to change language." : !session.languageSwitching ? "Update the server to change language during calls." : "Changes apply to new speech and answers.";
  return <div className={`live-call-language${compact ? " is-compact" : ""}`}>
    {!compact && <span className="ui-field-label">Spoken language</span>}
    <Select aria-label="Call language" searchLabel="Search call languages" value={value} disabled={disabled} title={hint} onChange={event => void change(event.target.value)}>
      {!items.some(item => item.id === value) && <option value={value} disabled>{value || (loading ? "Loading languages…" : "Language unavailable")}</option>}
      {items.map(item => <option key={item.id} value={item.id} lang={item.id} dir={item.direction ?? "auto"}>{item.native_name && item.native_name !== item.name ? `${item.native_name} · ${item.name}` : item.name}</option>)}
    </Select>
    {(pending || !compact) && <span className="ui-field-hint" role={pending ? "status" : undefined}>{hint}</span>}
    {(loadError || error) && <p className="live-call-language-error" role="alert">{loadError ?? error}{loadError && <> <Button size="sm" onClick={() => setRevision(value => value + 1)}>Retry languages</Button></>}</p>}
  </div>;
}
