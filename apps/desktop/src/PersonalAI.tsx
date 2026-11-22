import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Badge, Banner, Button, Card, EmptyState, Field, Input, Select, SkeletonList, Textarea } from "./ui";
import "./feature-pages.css";
type Item = { id: string; text: string; provenance: string; source_ids: string[] };
type Brief = { title: string; prepared_at: string; items: Item[]; sources: { id: string; title: string; url?: string; attribution: string; source_date?: string }[]; coverage_gaps: string[] };
type Request = { id: string; scope: { title: string; questions?: string[]; date_from?: string; date_to?: string }; request_revision: number; active_version_id: string | null; active_brief?: { id: string; brief: Brief; origin: string }; expected_brief_format?: Record<string, unknown>; instructions?: string };
const api = <T,>(method: string, path: string, body?: unknown) => invoke<T>("api_call", { method, path: `/personal-ai${path}`, body: body ?? null });
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0,10);
const preparedDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); };
export function PersonalAI() {
  const alive = useRef(true); const connectionRevision = useRef(0); const viewRevision = useRef(0);
  const [connections, setConnections] = useState<{id:string;client_id:string;scope:string;expires_at:number}[]>([]);
  const [connectionsState, setConnectionsState] = useState<"loading" | "ready" | "failed">("loading");
  const loadConnections = async () => { const revision = ++connectionRevision.current; setConnectionsState("loading"); try { const result = await api<{connections:typeof connections}>("GET", "/oauth/connections"); if(alive.current && revision === connectionRevision.current) { setConnections(result.connections); setConnectionsState("ready"); } } catch (e) { if(alive.current && revision === connectionRevision.current) setConnectionsState("failed"); throw e; } };
  useEffect(() => { alive.current = true; void loadConnections().catch(() => {}); return () => { alive.current = false; connectionRevision.current++; viewRevision.current++; }; }, []);
  const operation = useRef(false);
  const importAttempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const [requests, setRequests] = useState<Request[]>([]);
  const [requestsLoaded, setRequestsLoaded] = useState(false);
  const [selected, setSelected] = useState<Request | null>(null);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("Meeting preparation");
  const [question, setQuestion] = useState("Summarize recent progress, decisions, blockers and next steps with sources and coverage gaps.");
  const [from, setFrom] = useState(today()); const [to, setTo] = useState(today());
  const [kind, setKind] = useState("profile"); const [target, setTarget] = useState("");
  const [options, setOptions] = useState<{id:string;label:string}[]>([]);
  const [paste, setPaste] = useState(""); const [handoff, setHandoff] = useState("");
  const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const reload = async () => { const result = await api<{ requests: Request[] }>("GET", "/requests"); if (alive.current) { setRequests(result.requests); setRequestsLoaded(true); } };
  useEffect(() => { void reload().catch(e => { if (alive.current) { setRequestsLoaded(true); setError(String(e)); } }); }, []);
  useEffect(() => { let alive = true; setTarget(""); setOptions([]); if (kind !== "profile") invoke<{options:{id:string;label:string}[]}>("api_call", { method: "GET", path: `/ask/context-options?kind=${kind}`, body: null }).then(v => { if(alive) setOptions(v.options); }).catch(e => { if(alive) setError(String(e)); }); return () => { alive=false; }; }, [kind]);
  const run = async (work: () => Promise<void>) => { if (operation.current) return; operation.current = true; setBusy(true); setError(null); try { await work(); await reload(); } catch(e) { setError(String(e)); } finally { operation.current = false; setBusy(false); } };
  const view = async (id: string) => { const revision = ++viewRevision.current; const result = await api<Request>("GET", `/requests/${id}`); if(!alive.current || revision !== viewRevision.current) return; setSelected(result); setPaste(""); setHandoff(""); };
  const requestText = (r: Request) => `Prepare context for Career Pilot from this conversation and other sources you actually have permission to access. Do not assume access to other chats or memory. Preserve stable item IDs on refresh and state coverage gaps.

If connected, call get_context_request with request_id "${r.id}" for current scope and revision, then publish_context_brief with that request_id, request_revision and a unique idempotency_key.

If the tools are unavailable, return only brief JSON matching the schema below for manual import, without the publication envelope.

Preparation request:
${JSON.stringify({ request_id:r.id, request_revision:r.request_revision, scope:r.scope, instructions:r.instructions, expected_brief_format:r.expected_brief_format }, null, 2)}`;
  const importBrief = async () => {
    if (!selected) return;
    const raw = paste.trim().replace(/^```(?:json)?\s*\n?([\s\S]*?)\n?```$/, "$1");
    const brief: unknown = JSON.parse(raw);
    if (new TextEncoder().encode(JSON.stringify(brief)).length > 32768) throw new Error("Brief must be at most 32 KiB. Ask your AI for a shorter brief.");
    const fingerprint = JSON.stringify([selected.id, selected.request_revision, brief]);
    if (importAttempt.current?.fingerprint !== fingerprint) importAttempt.current = { fingerprint, key:crypto.randomUUID() };
    await api("POST", `/requests/${selected.id}/publish`, { request_revision:selected.request_revision, idempotency_key:importAttempt.current.key, brief });
    await view(selected.id);
  };
  return <section className="personal-ai-workspace feature-page">
    <header className="feature-heading"><div><h2>Bring your context together</h2><p>Prepare a private brief with the AI you already use.</p></div><Button variant={creating ? "ghost" : "secondary"} aria-expanded={creating} onClick={() => setCreating(!creating)}>{creating ? "Cancel request" : "Prepare context"}</Button></header>
    <p className="feature-footnote">Briefs stay private to your account and can inform Ask, practice, and calls. They do not change approved resume facts.</p>
    {error && <Banner tone="danger" title="Could not complete the request">{error}</Banner>}
    {creating && <Card className="personal-ai-create" title="Preparation request">
      <div className="personal-ai-form">
        <Field label="Title"><Input value={title} maxLength={200} onChange={e => setTitle(e.target.value)} placeholder="For example, Friday team meeting" /></Field>
        <Field label="Context"><Select value={kind} onChange={e => setKind(e.target.value)}><option value="profile">Current profile</option><option value="company">Company</option><option value="application">Application</option></Select></Field>
        {kind !== "profile" && <Field label="Selected context"><Select value={target} onChange={e => setTarget(e.target.value)}><option value="">Choose context</option>{options.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}</Select></Field>}
        <Field label="Preparation question" className="personal-ai-question"><Textarea value={question} onChange={e => setQuestion(e.target.value)} rows={3} /></Field>
        <Field label="From"><Input type="date" value={from} onChange={e => setFrom(e.target.value)} /></Field>
        <Field label="Through" error={from && to && from > to ? "Choose a date on or after the start date." : null}><Input type="date" min={from} value={to} onChange={e => setTo(e.target.value)} /></Field>
      </div>
      <div className="feature-card-footer"><span className="ui-field-hint">You can review the request before sharing it.</span><Button variant="primary" loading={busy} disabled={!title.trim() || !question.trim() || !from || !to || from > to || (kind !== "profile" && !target)} onClick={() => void run(async () => { const r = await api<Request>("POST", "/requests", { title, questions:[question], purpose:"call", date_from:from, date_to:to, ...(kind === "company" ? { employer_id:target } : kind === "application" ? { job_id:target } : {}) }); setSelected(r); setHandoff(requestText(r)); setCreating(false); })}>Create request</Button></div>
    </Card>}
    {!requestsLoaded ? <SkeletonList rows={2} label="Loading preparation requests" /> : requests.length === 0 && !creating && !error ? <EmptyState title="Your first brief starts with a question" action={<Button onClick={() => setCreating(true)}>Create a preparation request</Button>}>Choose what to prepare, share the request with your AI, and bring the brief back here.</EmptyState> : null}
    {!!requests.length && <div className="personal-ai-requests" aria-label="Preparation requests">{requests.map(r => <div key={r.id} className={`personal-ai-request${selected?.id === r.id ? " is-selected" : ""}`}>
      <div className="personal-ai-request-title"><h3>{r.scope.title}</h3><p>{r.active_brief ? `${preparedDate(r.active_brief.brief.prepared_at)} · ${r.active_brief.brief.sources.length} sources` : "Share the request with your AI to prepare a brief."}</p></div>
      <Badge tone={r.active_brief ? "success" : "neutral"}>{r.active_brief ? "Brief ready" : "Awaiting brief"}</Badge>
      <div className="personal-ai-request-actions"><Button size="sm" disabled={busy} onClick={() => void run(() => view(r.id))}>View</Button><Button size="sm" variant="ghost" disabled={busy} onClick={() => void run(async () => { const updated = await api<Request>("POST", `/requests/${r.id}/refresh`, { expected_revision:r.request_revision }); await view(updated.id); setHandoff(requestText(updated)); })}>Prepare refresh request</Button><Button size="sm" variant="danger-ghost" disabled={busy} onClick={() => void run(async () => { await api("DELETE", `/requests/${r.id}`); if(selected?.id===r.id) { setSelected(null); setPaste(""); } setHandoff(""); })}>Remove</Button></div>
    </div>)}</div>}
    {handoff && <Card className="personal-ai-handoff" title="Send this request to your AI" actions={<Button size="sm" onClick={() => void navigator.clipboard.writeText(handoff).catch(() => setError("Select and copy the request above."))}>Copy request</Button>}><p className="ui-field-hint">Paste this into your AI conversation. A connected AI can publish the brief directly; otherwise, paste its brief back below.</p><Field label="Request to run in your AI"><Textarea readOnly value={handoff} rows={5} /></Field></Card>}
    {selected && <Card className="personal-ai-brief" title={selected.scope.title} actions={<Button size="sm" onClick={() => { viewRevision.current++; setSelected(null); }}>Close brief</Button>}>
      {selected.active_brief ? <>
        <p className="feature-footnote">Prepared {preparedDate(selected.active_brief.brief.prepared_at)} · {selected.active_brief.origin === "paste" ? "Pasted from your AI" : selected.active_brief.origin === "correction" ? "Your correction" : "Published through MCP"}</p>
        <div className="personal-ai-items">{selected.active_brief.brief.items.map(item => <BriefItem key={`${selected.active_version_id}:${item.id}`} item={item} busy={busy} onSave={text => void run(async () => { await api("PATCH", `/requests/${selected.id}/items/${item.id}`, { expected_version_id:selected.active_version_id, text }); await view(selected.id); })} />)}</div>
        {!!selected.active_brief.brief.sources.length && <details className="feature-disclosure"><summary>Sources ({selected.active_brief.brief.sources.length})</summary><ul className="personal-ai-sources">{selected.active_brief.brief.sources.map(s => <li key={s.id}>{s.url && /^https?:\/\//i.test(s.url) ? <a href={s.url} target="_blank" rel="noopener noreferrer">{s.title}</a> : s.title}<span>{s.attribution}{s.source_date ? ` · ${s.source_date}` : ""}</span></li>)}</ul></details>}
        {!!selected.active_brief.brief.coverage_gaps.length && <div className="personal-ai-gaps"><h3>What this brief does not cover</h3><ul>{selected.active_brief.brief.coverage_gaps.map((gap,i) => <li key={i}>{gap}</li>)}</ul></div>}
      </> : <p className="ui-text-2">This request is ready to share. Copy it to your AI, then import the brief it prepares.</p>}
      <details className="feature-disclosure personal-ai-import"><summary>Paste a brief from your AI</summary><Field label="Brief JSON"><Textarea value={paste} maxLength={32768} rows={6} onChange={e => setPaste(e.target.value)} placeholder="Paste the brief JSON returned by your AI" /></Field><Button variant="primary" loading={busy} disabled={!paste.trim()} onClick={() => void run(importBrief)}>Import pasted brief</Button><p className="ui-field-hint">Pasted briefs are checked before import. Review their sources and dates before using them.</p></details>
      <Button size="sm" onClick={() => setHandoff(requestText(selected))}>Show AI request</Button>
    </Card>}
    <div className="personal-ai-connection-help">
      <details className="feature-disclosure"><summary>Connect your AI or use copy and paste</summary><div className="personal-ai-instructions"><ol><li><strong>Create a request.</strong> Choose a question, context, and date range.</li><li><strong>Ask your AI to prepare it.</strong> Use Copy request or Show AI request.</li><li><strong>Review the brief here.</strong> A connected AI publishes directly. You can also paste its returned JSON.</li></ol><details><summary>MCP connection details</summary><p>Connect a compatible AI client to your Career Pilot service’s HTTPS endpoint ending in <code>/v1/personal-ai/mcp</code>. Your deployment must have OAuth and a registered client configured. Sign in and review read and publish permissions in the browser.</p><p>Access expires automatically. MCP does not grant access to other chats or source credentials. Never paste credentials into a brief or chat. Your AI plan’s usage terms apply.</p></details></div></details>
      <details className="feature-disclosure"><summary>Connected AI clients</summary><div className="personal-ai-connections"><p className="ui-field-hint">Disconnecting stops future reads and publications. Prepared briefs stay until you remove them.</p><Button size="sm" disabled={busy} onClick={() => void run(loadConnections)}>Refresh AI connections</Button>{connectionsState === "loading" && <p role="status">Checking connections…</p>}{connectionsState === "failed" && <p className="ui-field-error">Connection status is unavailable. Try refreshing.</p>}{connectionsState === "ready" && connections.length === 0 && <p className="ui-text-2">No active OAuth connections.</p>}{connections.map(c => <div key={c.id} className="personal-ai-connection"><div><strong>{c.client_id}</strong><p className="ui-field-hint">Expires {preparedDate(new Date(c.expires_at * 1000).toISOString())}</p><span className="ui-field-hint">{c.scope.split(" ").map(s => s === "context:read" ? "Read requests" : s === "context:publish" ? "Publish briefs" : s).join(" · ")}</span></div><Button size="sm" variant="danger-ghost" disabled={busy} onClick={() => void run(async () => { connectionRevision.current++; await api("DELETE", `/oauth/connections/${c.id}`); await loadConnections(); })}>Disconnect {c.client_id}</Button></div>)}</div></details>
    </div>
  </section>;
}
function BriefItem({item,busy,onSave}:{item:Item;busy:boolean;onSave:(text:string|null)=>void}) {
  const [text,setText]=useState(item.text);
  return <div className="personal-ai-item"><div className="personal-ai-item-meta"><span>{item.provenance.replace(/_/g, " ")}</span><span>{item.source_ids.length ? `${item.source_ids.length} linked source${item.source_ids.length === 1 ? "" : "s"}` : "No linked source"}</span></div><Textarea aria-label="Brief item" dir="auto" maxLength={1200} value={text} rows={3} onChange={e=>setText(e.target.value)} /><div className="ui-row-inline"><Button size="sm" disabled={busy || text===item.text || !text.trim()} onClick={()=>onSave(text)}>Save correction</Button><Button size="sm" variant="danger-ghost" disabled={busy} onClick={()=>onSave(null)}>Remove item</Button></div></div>;
}
