import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Banner, Button, Card, Field, Input, Select, Textarea } from "./ui";
type Item = { id: string; text: string; provenance: string; source_ids: string[] };
type Brief = { title: string; prepared_at: string; items: Item[]; sources: { id: string; title: string; url?: string; attribution: string; source_date?: string }[]; coverage_gaps: string[] };
type Request = { id: string; scope: { title: string; questions?: string[]; date_from?: string; date_to?: string }; request_revision: number; active_version_id: string | null; active_brief?: { id: string; brief: Brief; origin: string }; expected_brief_format?: Record<string, unknown>; instructions?: string };
const api = <T,>(method: string, path: string, body?: unknown) => invoke<T>("api_call", { method, path: `/personal-ai${path}`, body: body ?? null });
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0,10);
export function PersonalAI() {
  const alive = useRef(true); const connectionRevision = useRef(0); const viewRevision = useRef(0);
  const [connections, setConnections] = useState<{id:string;client_id:string;scope:string;expires_at:number}[]>([]);
  const loadConnections = async () => { const revision = ++connectionRevision.current; const result = await api<{connections:typeof connections}>("GET", "/oauth/connections"); if(alive.current && revision === connectionRevision.current) setConnections(result.connections); };
  useEffect(() => { alive.current = true; void loadConnections().catch(() => {}); return () => { alive.current = false; connectionRevision.current++; viewRevision.current++; }; }, []);
  const operation = useRef(false);
  const importAttempt = useRef<{ fingerprint: string; key: string } | null>(null);
  const [requests, setRequests] = useState<Request[]>([]);
  const [selected, setSelected] = useState<Request | null>(null);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("Meeting preparation");
  const [question, setQuestion] = useState("Summarize recent progress, decisions, blockers and next steps with sources and coverage gaps.");
  const [from, setFrom] = useState(today()); const [to, setTo] = useState(today());
  const [kind, setKind] = useState("profile"); const [target, setTarget] = useState("");
  const [options, setOptions] = useState<{id:string;label:string}[]>([]);
  const [paste, setPaste] = useState(""); const [handoff, setHandoff] = useState("");
  const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const reload = async () => setRequests((await api<{ requests: Request[] }>("GET", "/requests")).requests);
  useEffect(() => { void reload().catch(e => setError(String(e))); }, []);
  useEffect(() => { let alive = true; setTarget(""); setOptions([]); if (kind !== "profile") invoke<{options:{id:string;label:string}[]}>("api_call", { method: "GET", path: `/ask/context-options?kind=${kind}`, body: null }).then(v => { if(alive) setOptions(v.options); }).catch(e => { if(alive) setError(String(e)); }); return () => { alive=false; }; }, [kind]);
  const run = async (work: () => Promise<void>) => { if (operation.current) return; operation.current = true; setBusy(true); setError(null); try { await work(); await reload(); } catch(e) { setError(String(e)); } finally { operation.current = false; setBusy(false); } };
  const view = async (id: string) => { const revision = ++viewRevision.current; const result = await api<Request>("GET", `/requests/${id}`); if(!alive.current || revision !== viewRevision.current) return; setSelected(result); setPaste(""); setHandoff(""); };
  const requestText = (r: Request) => `Prepare context for CoRuby from this conversation and other sources you actually have permission to access. Do not assume access to other chats or memory. Preserve stable item IDs on refresh and state coverage gaps.

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
  return <section className="ui-stack"><div className="ui-row-inline"><h2>Context from my AI</h2><Button onClick={() => setCreating(!creating)}>Prepare context</Button></div>
    <p className="ui-field-hint">Your AI prepares a brief from information it can access. Imported context stays private to your account and does not update approved resume facts.</p>
    <details><summary>Connect your AI or use copy and paste</summary><p>Your AI receives a preparation request and publishes a brief to the selected profile and company or application. Connect a compatible AI client to your CoRuby service’s HTTPS endpoint ending in <code>/v1/personal-ai/mcp</code>. When OAuth is configured, sign in and review the permissions in the browser. Never paste credentials into a brief or chat.</p><p>A local Codex connection has been validated with fictional data. OAuth requires a registered client on your deployment; review the requested read and publish permissions in the browser before allowing access. Access expires automatically. If your host is not configured, use Show AI request and paste the returned brief below. MCP does not grant access to other chats or source credentials. Your AI plan's usage terms apply; live call costs remain.</p></details>
    <details><summary>Connected AI clients</summary><p className="ui-field-hint">These short-lived OAuth grants belong to your account. Disconnecting stops future reads and publications; already prepared briefs remain until you remove them.</p><Button disabled={busy} onClick={() => void run(loadConnections)}>Refresh AI connections</Button>{connections.length === 0 && <p>No active OAuth connections.</p>}{connections.map(c => <div key={c.id}><p>{c.client_id} · {c.scope} · expires {new Date(c.expires_at * 1000).toLocaleString()}</p><Button disabled={busy} onClick={() => void run(async () => { connectionRevision.current++; await api("DELETE", `/oauth/connections/${c.id}`); await loadConnections(); })}>Disconnect {c.client_id}</Button></div>)}</details>
    {error && <Banner tone="danger">{error}</Banner>}
    {creating && <Card title="Preparation request"><Field label="Title"><Input value={title} maxLength={200} onChange={e => setTitle(e.target.value)} /></Field><Field label="Context"><Select value={kind} onChange={e => setKind(e.target.value)}><option value="profile">Current profile</option><option value="company">Company</option><option value="application">Application</option></Select></Field>{kind !== "profile" && <Field label="Selected context"><Select value={target} onChange={e => setTarget(e.target.value)}><option value="">Choose context</option>{options.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}</Select></Field>}<Field label="Preparation question"><Textarea value={question} onChange={e => setQuestion(e.target.value)} /></Field><div className="ui-row-inline"><Field label="From"><Input type="date" value={from} onChange={e => setFrom(e.target.value)} /></Field><Field label="Through"><Input type="date" value={to} onChange={e => setTo(e.target.value)} /></Field></div><Button loading={busy} disabled={!title.trim() || !question.trim() || !from || !to || from > to || (kind !== "profile" && !target)} onClick={() => void run(async () => { const r = await api<Request>("POST", "/requests", { title, questions:[question], purpose:"call", date_from:from, date_to:to, ...(kind === "company" ? { employer_id:target } : kind === "application" ? { job_id:target } : {}) }); setSelected(r); setHandoff(requestText(r)); setCreating(false); })}>Create request</Button></Card>}
    {requests.map(r => <Card key={r.id} title={r.scope.title}><p className="ui-field-hint">{r.active_brief ? `${r.active_brief.brief.prepared_at} · ${r.active_brief.brief.sources.length} sources` : "Awaiting a brief"}</p><div className="ui-row-inline"><Button disabled={busy} onClick={() => void run(() => view(r.id))}>View</Button><Button disabled={busy} onClick={() => void run(async () => { const updated = await api<Request>("POST", `/requests/${r.id}/refresh`, { expected_revision:r.request_revision }); await view(updated.id); setHandoff(requestText(updated)); })}>Prepare refresh request</Button><Button disabled={busy} onClick={() => void run(async () => { await api("DELETE", `/requests/${r.id}`); if(selected?.id===r.id) { setSelected(null); setPaste(""); } setHandoff(""); })}>Remove</Button></div></Card>)}
    {handoff && <div><Field label="Request to run in your AI"><Textarea readOnly value={handoff} rows={4} /></Field><Button onClick={() => void navigator.clipboard.writeText(handoff).catch(() => setError("Select and copy the request above."))}>Copy request</Button></div>}
    {selected && <Card title={selected.scope.title}><Button onClick={() => { viewRevision.current++; setSelected(null); }}>Close brief</Button>{selected.active_brief && <><p>Prepared {selected.active_brief.brief.prepared_at} · {selected.active_brief.origin === "paste" ? "Pasted from your AI" : selected.active_brief.origin === "correction" ? "Your correction" : "Published through MCP"} · version {selected.active_version_id}</p>{selected.active_brief.brief.items.map(item => <BriefItem key={`${selected.active_version_id}:${item.id}`} item={item} busy={busy} onSave={text => void run(async () => { await api("PATCH", `/requests/${selected.id}/items/${item.id}`, { expected_version_id:selected.active_version_id, text }); await view(selected.id); })} />)}<h3>Sources</h3>{selected.active_brief.brief.sources.map(s => <p key={s.id}>{s.url && /^https?:\/\//i.test(s.url) ? <a href={s.url} target="_blank" rel="noopener noreferrer">{s.title}</a> : s.title} · {s.attribution}{s.source_date ? ` · ${s.source_date}` : ""}</p>)}{selected.active_brief.brief.coverage_gaps.map((gap,i) => <p key={i}>Coverage gap: {gap}</p>)}</>}
    <details><summary>Paste a brief from your AI</summary><Field label="Brief JSON"><Textarea value={paste} maxLength={32768} rows={6} onChange={e => setPaste(e.target.value)} /></Field><Button loading={busy} disabled={!paste.trim()} onClick={() => void run(importBrief)}>Import pasted brief</Button><p className="ui-field-hint">Manual imports use the same validation and are labeled as pasted. Preparation time is not proof that the sources are current.</p></details>
    <Button onClick={() => setHandoff(requestText(selected))}>Show AI request</Button></Card>}
  </section>;
}
function BriefItem({item,busy,onSave}:{item:Item;busy:boolean;onSave:(text:string|null)=>void}) { const [text,setText]=useState(item.text); return <div className="ui-stack"><p>{item.provenance} · sources {item.source_ids.join(", ") || "none"}</p><Textarea aria-label="Brief item" maxLength={1200} value={text} onChange={e=>setText(e.target.value)} /><div className="ui-row-inline"><Button disabled={busy || text===item.text || !text.trim()} onClick={()=>onSave(text)}>Save correction</Button><Button disabled={busy} onClick={()=>onSave(null)}>Remove item</Button></div></div>; }
