import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { request as api } from "./api";
import { Badge, Banner, Button, Card, Checkbox, ConfirmDialog, Field, Select, SkeletonList, Textarea } from "./ui";
import { Icon } from "./ui/Icon";

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

export function MeetingPreparation({ onChoose }: { onChoose?: (title: string, applicationId: string | null) => void }) {
  const [open, setOpen] = useState(false), [connections, setConnections] = useState<Connection[]>([]);
  const [preparation, setPreparation] = useState<PreparationResponse>({ items: [], connections: [] });
  const [error, setError] = useState<string | null>(null), [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false), [loaded, setLoaded] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [disconnect, setDisconnect] = useState<Connection | null>(null);
  const revision = useRef(0), alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; revision.current++; }; }, []);
  const reload = async (refresh = false) => {
    const ticket = ++revision.current;
    setLoading(true); setError(null);
    try {
      const [calendars, meetings] = await Promise.all([api<{ items: Connection[] }>("GET", "/calendars", undefined, { refresh }), api<PreparationResponse>("GET", "/meeting-preparation", undefined, { refresh })]);
      if (!alive.current || ticket !== revision.current) return false;
      setConnections(calendars.items); setPreparation(meetings); setLoaded(true);
      return true;
    } catch (e) { if (alive.current && ticket === revision.current) setError(String(e)); return false; }
    finally { if (alive.current && ticket === revision.current) setLoading(false); }
  };
  useEffect(() => { if (open) void reload(); }, [open]);
  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError(null); setNotice(null);
    try { await operation(); if (alive.current) await reload(true); }
    catch (e) { if (alive.current) setError(String(e)); }
    finally { if (alive.current) setBusy(false); }
  };
  const providers = Object.fromEntries(preparation.connections.map(c => [c.connection_id, c.provider]));
  const connected = connections.filter(c => c.connected && !["reauth_required", "error"].includes(c.status)).length;
  return <details className="ui-disclosure meeting-preparation" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
    <summary><Icon name="calendar" size={18} /><span>Meeting preparation</span><span className="ui-disclosure-meta">{connected ? `${connected} calendar${connected === 1 ? "" : "s"} connected` : "Optional calendar"}</span></summary><div className="ui-disclosure-body ui-stack">
      <p className="ui-field-hint">Bring the next meeting and its notes into your call. Calendar access is read-only.</p>
      {error && <Banner tone="danger" title="Calendar could not be loaded" action={<Button size="sm" disabled={busy || loading} onClick={() => void run(async () => {})}>Retry</Button>}>{error}</Banner>}
      {notice && <Banner tone="info">{notice}</Banner>}
      {loading && !loaded && <SkeletonList rows={2} label="Loading calendars" />}
      {loaded && <div className="calendar-connections">{connections.map(c => <Card className="calendar-connection" key={c.provider} title={c.provider === "google" ? "Google Calendar" : "Outlook Calendar"} actions={<Badge tone={["reauth_required", "error"].includes(c.status) ? "warning" : c.connected ? "success" : "neutral"} icon={false}>{c.status === "reauth_required" ? "Reconnect needed" : c.status === "error" ? "Sync needs attention" : c.connected ? "Connected" : c.available ? "Not connected" : "Setup needed"}</Badge>}>
        {!c.available ? <p className="ui-field-hint">Your server hasn’t enabled this calendar yet. You can choose your call context manually.</p> : !c.connected || c.status === "reauth_required" ? <div className="ui-stack-tight">
          <p className="ui-field-hint">Choose which calendars to use after signing in.</p>
          <div><Button size="sm" disabled={busy || loading} onClick={() => void run(async () => {
            const value = await api<{ authorization_url: string }>("POST", `/calendars/${c.provider}/connect`);
            if (!alive.current) return;
            await invoke("open_calendar_authorization", { url: value.authorization_url });
          })}>{c.status === "reauth_required" ? "Reconnect calendar" : "Connect read-only calendar"}</Button></div></div> : <div className="ui-stack-tight">
            <p className="ui-field-hint">{c.last_sync_at ? `Last synced ${new Date(c.last_sync_at).toLocaleString()}` : "Choose calendars to prepare upcoming meetings."}</p>
            {c.last_error && <Banner tone="warning" title="Calendar needs attention">{c.last_error}</Banner>}
            <div className="calendar-selection">{c.calendars.map(calendar => <Checkbox key={calendar.id} label={calendar.name} checked={c.selected_calendars.includes(calendar.id)} disabled={busy || loading}
              onChange={on => void run(async () => { await api("PATCH", `/calendars/${c.provider}/selection`, { calendar_ids: on ? [...c.selected_calendars, calendar.id] : c.selected_calendars.filter(id => id !== calendar.id) }); })} />)}
            </div>
          </div>}
        {c.connected && <div className="ui-row-inline calendar-connection-actions">
          {c.available && c.status !== "reauth_required" && <Button size="sm" disabled={busy || loading} onClick={() => void run(async () => { await api("POST", `/calendars/${c.provider}/sync`); })}>Sync calendar</Button>}
          <Button size="sm" variant="ghost" disabled={busy || loading} onClick={() => setDisconnect(c)}>Disconnect</Button>
        </div>}
      </Card>)}</div>}
      <div className="ui-row-inline"><Button size="sm" loading={loading && loaded} disabled={busy || loading} onClick={() => void run(async () => {})}>Refresh connection status</Button><span className="ui-field-hint">Refresh after connecting in your browser.</span></div>
      {preparation.items.map(item => <Meeting key={`${item.meeting.account_id}/${item.meeting.calendar_id}/${item.meeting.instance_id}`} item={item}
        provider={providers[item.meeting.account_id]} applications={preparation.applications ?? item.candidates} sessions={preparation.saved_sessions ?? []}
        onSaved={reload} onChoose={onChoose} />)}
      {loaded && !error && !loading && preparation.items.length === 0 && <p className="calendar-empty">{connected ? "No meetings in the next 30 minutes in your selected calendars." : "Calendar is optional. You can start a call whenever you’re ready."}</p>}
      <ConfirmDialog open={disconnect !== null} onOpenChange={next => { if (!next) setDisconnect(null); }} title="Disconnect calendar?" confirmLabel="Disconnect" onConfirm={async () => {
        if (!disconnect) return;
        const result = await api<{ provider_consent_removal_required: boolean }>("DELETE", `/calendars/${disconnect.provider}`);
        if (alive.current) {
          const provider = disconnect.provider;
          setConnections(current => current.map(c => c.provider === provider ? { ...c, connected: false, status: "disconnected", calendars: [], selected_calendars: [] } : c));
          setPreparation(current => {
            const removed = new Set(current.connections.filter(c => c.provider === provider).map(c => c.connection_id));
            return { ...current, connections: current.connections.filter(c => c.provider !== provider), items: current.items.filter(item => !removed.has(item.meeting.account_id)) };
          });
          if (result.provider_consent_removal_required) setNotice("Calendar disconnected. To remove its authorization too, remove CoRuby in your calendar account’s connected apps.");
          await reload();
        }
      }}>Cached calendar events will be removed. Your saved calls and notes will stay.</ConfirmDialog>
    </div>
  </details>;
}

function Meeting({ item, provider, applications, sessions, onSaved, onChoose }: {
  item: Preparation; provider?: string; applications: Application[]; sessions: SavedSession[]; onSaved: () => Promise<boolean>;
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
      const refreshed = await onSaved();
      // A successful save is still authoritative if the subsequent read fails. Keep the
      // edited values until fresh props arrive instead of restoring the previous snapshot.
      if (refreshed) setDirty(false);
      setSaved(true);
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
