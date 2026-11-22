import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Banner, Button, Card, Checkbox, Field, Select, Textarea } from "./ui";

type Connection = { provider: "google" | "outlook"; available: boolean; connected: boolean; status: string; calendars: { id: string; name: string }[]; selected_calendars: string[]; last_sync_at?: string; last_error?: string };
type Application = { application_id: string; company: string; job_title: string };
type SavedSession = { id: string; title: string; started_at: string };
type Preparation = {
  meeting: { account_id: string; calendar_id: string; instance_id: string; title: string; start: string; end: string; series_id?: string | null };
  candidates: Application[]; selected_application_id: string | null; requires_confirmation: boolean; overlaps: boolean;
  previous: { notes: string; unfinished_actions: string[] }[];
  linked_session_id?: string | null; unfinished_actions?: string[];
};
type PreparationResponse = { items: Preparation[]; connections: { connection_id: string; provider: string }[]; applications?: Application[]; saved_sessions?: SavedSession[] };
const api = <T,>(method: string, path: string, body?: unknown) => invoke<T>("api_call", { method, path, body: body ?? null });

export function MeetingPreparation({ onChoose }: { onChoose?: (title: string, applicationId: string | null) => void }) {
  const [open, setOpen] = useState(false), [connections, setConnections] = useState<Connection[]>([]);
  const [preparation, setPreparation] = useState<PreparationResponse>({ items: [], connections: [] });
  const [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const revision = useRef(0), alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; revision.current++; }; }, []);
  const reload = async () => {
    const ticket = ++revision.current;
    const [calendars, meetings] = await Promise.all([api<{ items: Connection[] }>("GET", "/calendars"), api<PreparationResponse>("GET", "/meeting-preparation")]);
    if (!alive.current || ticket !== revision.current) return;
    setConnections(calendars.items); setPreparation(meetings);
  };
  useEffect(() => { if (open) void reload().catch(e => { if (alive.current) setError(String(e)); }); }, [open]);
  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await operation(); await reload(); }
    catch (e) { if (alive.current) setError(String(e)); }
    finally { if (alive.current) setBusy(false); }
  };
  const providers = Object.fromEntries(preparation.connections.map(c => [c.connection_id, c.provider]));
  return <details open={open} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary>Meeting preparation</summary><div className="ui-stack">
      {error && <Banner tone="danger">{error}</Banner>}
      {connections.map(c => <Card key={c.provider} title={c.provider === "google" ? "Google Calendar" : "Outlook Calendar"}>
        {!c.available ? <p className="ui-field-hint">Calendar connection is not configured on this deployment.</p> : !c.connected ?
          <Button disabled={busy} onClick={() => void run(async () => {
            const value = await api<{ authorization_url: string }>("POST", `/calendars/${c.provider}/connect`);
            await invoke("open_calendar_authorization", { url: value.authorization_url });
          })}>Connect read-only calendar</Button> : <>
            <p>{c.status}{c.last_sync_at ? ` · Synced ${new Date(c.last_sync_at).toLocaleString()}` : ""}</p>
            {c.last_error && <p role="alert">{c.last_error}</p>}
            {c.calendars.map(calendar => <Checkbox key={calendar.id} label={calendar.name} checked={c.selected_calendars.includes(calendar.id)} disabled={busy}
              onChange={on => void run(async () => { await api("PATCH", `/calendars/${c.provider}/selection`, { calendar_ids: on ? [...c.selected_calendars, calendar.id] : c.selected_calendars.filter(id => id !== calendar.id) }); })} />)}
            <div className="ui-row-inline">
              <Button disabled={busy} onClick={() => void run(async () => { await api("POST", `/calendars/${c.provider}/sync`); })}>Sync calendar</Button>
              <Button disabled={busy} onClick={() => void run(async () => {
                const result = await api<{ provider_consent_removal_required: boolean }>("DELETE", `/calendars/${c.provider}`);
                if (result.provider_consent_removal_required && alive.current) setError("Disconnected and removed cached events. Remove CoRuby access in your calendar provider's account settings to revoke its consent too.");
              })}>Disconnect</Button>
            </div>
          </>}
      </Card>)}
      <Button disabled={busy} onClick={() => void run(async () => {})}>Refresh connection status</Button>
      {preparation.items.map(item => <Meeting key={`${item.meeting.account_id}/${item.meeting.calendar_id}/${item.meeting.instance_id}`} item={item}
        provider={providers[item.meeting.account_id]} applications={preparation.applications ?? item.candidates} sessions={preparation.saved_sessions ?? []}
        onSaved={reload} onChoose={onChoose} />)}
      {open && preparation.items.length === 0 && <p className="ui-field-hint">No meetings in the next 30 minutes in the selected calendars. Calls work without a calendar.</p>}
    </div>
  </details>;
}

function Meeting({ item, provider, applications, sessions, onSaved, onChoose }: {
  item: Preparation; provider?: string; applications: Application[]; sessions: SavedSession[]; onSaved: () => Promise<void>;
  onChoose?: (title: string, applicationId: string | null) => void;
}) {
  const effectiveActions = (item.unfinished_actions ?? item.previous.flatMap(p => p.unfinished_actions)).join("\n");
  const [application, setApplication] = useState(item.selected_application_id ?? ""), [session, setSession] = useState(item.linked_session_id ?? "");
  const [actions, setActions] = useState(effectiveActions), [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false), [error, setError] = useState<string | null>(null), [saved, setSaved] = useState(false);
  useEffect(() => {
    if (!dirty) { setApplication(item.selected_application_id ?? ""); setSession(item.linked_session_id ?? ""); setActions(effectiveActions); }
  }, [item.selected_application_id, item.linked_session_id, effectiveActions, dirty]);
  const changed = () => { setDirty(true); setSaved(false); };
  const save = async () => {
    if (!provider || saving) return;
    setSaving(true); setError(null); setSaved(false);
    try {
      await api("PUT", `/calendars/${provider}/context`, {
        calendar_id: item.meeting.calendar_id, instance_id: item.meeting.instance_id,
        application_id: application || null, session_id: session || null,
        unfinished_actions: [...new Set(actions.split("\n").map(value => value.trim()).filter(Boolean))],
      });
      await onSaved(); setDirty(false); setSaved(true);
    } catch (e) { setError(String(e)); }
    finally { setSaving(false); }
  };
  return <Card title={item.meeting.title}><div className="ui-stack">
    <p>{new Date(item.meeting.start).toLocaleString()} – {new Date(item.meeting.end).toLocaleTimeString()}</p>
    {item.overlaps && <p className="ui-field-hint">Overlapping meetings. Confirm the one you want.</p>}
    {item.requires_confirmation && <p className="ui-field-hint">Suggested: {item.candidates.map(c => `${c.company} · ${c.job_title}`).join("; ")}. Confirm or choose another application.</p>}
    <div className="feature-form-grid">
      <Field label="Application"><Select aria-label="Meeting application" value={application} disabled={saving} onChange={e => { changed(); setApplication(e.target.value); }}>
        <option value="">No application</option>{applications.map(c => <option key={c.application_id} value={c.application_id}>{c.company} · {c.job_title}</option>)}
      </Select></Field>
      <Field label="Related saved call"><Select aria-label="Related saved call" value={session} disabled={saving} onChange={e => { changed(); setSession(e.target.value); }}>
        <option value="">No saved call linked</option>{sessions.map(s => <option key={s.id} value={s.id}>{s.title} · {new Date(s.started_at).toLocaleDateString()}</option>)}
      </Select></Field>
    </div>
    <p className="ui-field-hint">Link a saved call to reuse its notes for this recurring meeting.</p>
    {item.previous.some(p => p.notes) && <details><summary>Notes from linked calls</summary>{item.previous.filter(p => p.notes).map((p, i) => <p key={i}>{p.notes}</p>)}</details>}
    <Field label="Unfinished actions"><Textarea aria-label="Unfinished actions" value={actions} onChange={e => { changed(); setActions(e.target.value); }} maxLength={8000} disabled={saving} /></Field>
    <p className="ui-field-hint">One action per line. Remove completed actions and save; this replaces the open-action list for future meetings in this series.</p>
    <div className="ui-row-inline">
      {provider && <Button disabled={saving} onClick={() => void save()}>{saving ? "Saving…" : "Save meeting context"}</Button>}
      {onChoose && <Button disabled={saving} onClick={() => onChoose(item.meeting.title, application || null)}>Use for call setup</Button>}
      {saved && <span role="status">Meeting context saved.</span>}
    </div>
    {error && <Banner tone="danger">{error}</Banner>}
  </div></Card>;
}
