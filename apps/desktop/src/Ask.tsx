import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { PageHeadingActive } from "./ui/Layout";
import { invoke } from "@tauri-apps/api/core";
import { Banner, Button, Card, ConfirmDialog, Field, Input, Select, Textarea } from "./ui";
export type AskScope = { kind: "auto" | "all" | "general" | "company" | "application" | "document" | "call"; id?: string; from_date?: string; to_date?: string };
export type AskEntry = { scope: AskScope; n: number };
export function openAsk(scope: AskScope) { window.dispatchEvent(new CustomEvent("copilot:open-ask", { detail: scope })); }
type Source = { id: string; kind: string; label: string; date?: string; start_ms?: number; target_id?: string };
type Message = { id: string; request_id: string; question: string; answer: string; status: "pending" | "done" | "failed" | "cancelled"; sources: Source[]; error?: string };
type Conversation = { id: string; title: string; scope: AskScope; messages?: Message[]; retrieval_notice?: string; retention_notice?: string };
const request = <T,>(method: string, path: string, body?: unknown) => invoke<T>("api_call", { method, path, body: body ?? null });
const base = "/ask/conversations";
const SavedCall = lazy(() => import("./SessionDetail").then(m => ({ default: m.SessionDetail })));
export function Ask({ active = true, entry }: { active?: boolean; entry?: AskEntry | null }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<Conversation | null>(null);
  const [scope, setScope] = useState<AskScope>({ kind: "auto" });
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [source, setSource] = useState<(Source & { content: string }) | null>(null);
  const [showCall, setShowCall] = useState(false);
  const [options, setOptions] = useState<{ id: string; label: string }[]>([]);
  const conversationElement = useRef<HTMLDivElement>(null);
  const scrollPosition = useRef<{ element: Element; top: number } | null>(null);
  const alive = useRef(true);
  const generation = useRef(0), sourceGeneration = useRef(0);
  const valid = (ticket: number) => alive.current && generation.current === ticket;
  const invalidate = () => { generation.current++; sourceGeneration.current++; setBusy(false); setDeleting(false); };
  const currentRef = useRef(current); currentRef.current = current;
  const drafts = useRef(new Map<string, string>());
  const [pendingRequest, setPendingRequest] = useState<{ id: string; question: string } | null>(null);
  const reload = () => { const ticket = generation.current; return request<{ conversations: Conversation[] }>("GET", base).then(data => { if (valid(ticket)) setConversations(data.conversations); }); };
  useEffect(() => { alive.current = true; void reload().catch(e => setError(String(e))); return () => { alive.current = false; generation.current++; sourceGeneration.current++; for (const m of currentRef.current?.messages ?? []) if (m.status === "pending") void request("POST", `${base}/${currentRef.current!.id}/messages/${m.id}/cancel`).catch(() => {}); }; }, []);
  useEffect(() => { if (entry) { invalidate(); drafts.current.set(current?.id ?? "new", draft); setCurrent(null); setScope(entry.scope); setDraft(""); setSource(null); setPendingRequest(null); } }, [entry?.n]);
  const pending = current?.messages?.some(m => m.status === "pending");
  useEffect(() => {
    if (!active || !current?.id || !pending) return;
    const ticket = generation.current; let stopped = false;
    const timer = setInterval(() => { request<Conversation>("GET", `${base}/${current.id}`).then(value => { if (!stopped && valid(ticket)) setCurrent(value); }).catch(e => { if (!stopped && valid(ticket)) setError(String(e)); }); }, 1000);
    return () => { stopped = true; clearInterval(timer); };
  }, [active, current?.id, pending]);
  useEffect(() => {
    let stopped = false; setOptions([]);
    if (!["auto", "all", "general"].includes(scope.kind)) request<{ options: { id: string; label: string }[] }>("GET", `/ask/context-options?kind=${scope.kind}`).then(data => {
      if (!stopped) setOptions(data.options);
    }).catch(e => { if (!stopped) setError(String(e)); });
    return () => { stopped = true; };
  }, [scope.kind]);
  const choose = async (id: string) => {
    invalidate(); const ticket = generation.current;
    drafts.current.set(current?.id ?? "new", draft); setSource(null); setError(null); setPendingRequest(null);
    setCurrent(null); setDraft("");
    if (!id) { setScope({ kind: "auto" }); setDraft(drafts.current.get("new") ?? ""); return; }
    try { const value = await request<Conversation>("GET", `${base}/${id}`); if (!valid(ticket)) return; setCurrent(value); setScope(value.scope); setDraft(value => value || drafts.current.get(id) || ""); } catch (e) { if (valid(ticket)) setError(String(e)); }
  };
  const send = async () => {
    if (busy || pending || !draft.trim()) return;
    const ticket = generation.current;
    setBusy(true); setError(null);
    const attempt = pendingRequest?.question === draft ? pendingRequest : { id: crypto.randomUUID(), question: draft };
    setPendingRequest(attempt);
    try {
      let conversation = current;
      if (!conversation) { conversation = await request<Conversation>("POST", base, { title: draft.slice(0, 80), scope }); if (!valid(ticket)) return; setCurrent(conversation); }
      const message = await request<Message>("POST", `${base}/${conversation.id}/messages`, { request_id: attempt.id, question: attempt.question, scope });
      if (!valid(ticket)) return;
      const updated = await request<Conversation>("GET", `${base}/${conversation.id}`);
      if (!valid(ticket)) return;
      setCurrent({ ...updated, messages: updated.messages ?? [message] }); setDraft(value => value === attempt.question ? "" : value); drafts.current.delete(conversation.id); setPendingRequest(null); void reload();
    } catch (e) { if (valid(ticket)) setError(String(e)); } finally { if (valid(ticket)) setBusy(false); }
  };
  const action = async (message: Message, command: "cancel" | "retry") => {
    if (!current) return;
    const ticket = generation.current, id = current.id;
    try { await request("POST", `${base}/${id}/messages/${message.id}/${command}`); if (!valid(ticket)) return; const value = await request<Conversation>("GET", `${base}/${id}`); if (!valid(ticket)) return; setCurrent(value); if (command === "cancel") setDraft(value => value || message.question); } catch (e) { if (valid(ticket)) setError(String(e)); }
  };
  const inspect = async (item: Source) => {
    setShowCall(false);
    const ticket = generation.current, sourceTicket = ++sourceGeneration.current;
    let element: Element | null = conversationElement.current;
    while (element && element.scrollHeight <= element.clientHeight) element = element.parentElement;
    if (element) scrollPosition.current = { element, top: element.scrollTop };
    try { const value = await request<{ content: string; start_ms?: number }>("GET", `/ask/sources/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}`); if (valid(ticket) && sourceTicket === sourceGeneration.current) setSource({ ...item, ...value, label: item.label }); } catch (e) { if (valid(ticket) && sourceTicket === sourceGeneration.current) setError(`Source unavailable: ${String(e)}`); }
  };
  const remove = async () => {
    const ticket = generation.current, id = current?.id;
    try { if (id) await request("DELETE", `${base}/${id}`); if (!valid(ticket)) return; invalidate(); setCurrent(null); setDraft(""); await reload(); } catch (e) { if (valid(ticket)) setError(String(e)); }
  };
  const callId = source?.kind === "transcript" ? source.target_id ?? source.id.split(":")[0] : null;
  const sourceParts = source?.id.split(":") ?? [];
  return <><div className="ask-workspace">
    <header className="feature-heading"><div><span className="feature-eyebrow">ASK</span><h2>Ask about your work</h2><p>Find answers across your calls, documents, and experience.</p></div></header>
    <div className="feature-toolbar"><Select aria-label="Saved conversations" value={current?.id ?? ""} onChange={e => void choose(e.target.value)}><option value="">New conversation</option>{conversations.map(c => <option key={c.id} value={c.id}>{c.title}</option>)}</Select><Button onClick={() => void choose("")}>New</Button>{current && <Button onClick={() => setDeleting(true)}>Delete conversation</Button>}</div>
    {error && <Banner tone="danger">{error}</Banner>}
    {source && <Card title={source.label}><Button onClick={() => { sourceGeneration.current++; setSource(null); requestAnimationFrame(() => { const saved = scrollPosition.current; if (saved) saved.element.scrollTop = saved.top; }); }}>Back to conversation</Button>{source.start_ms !== undefined && <p>Transcript at {Math.floor(source.start_ms / 1000)} seconds</p>}<p style={{ whiteSpace: "pre-wrap" }}>{source.content}</p>{callId && !showCall && <Button onClick={() => setShowCall(true)}>Open saved call at this passage</Button>}{callId && showCall && <PageHeadingActive.Provider value={false}><Suspense fallback={<p>Loading saved call…</p>}><SavedCall sessionId={callId} transcriptFocus={{sessionId:callId,startMs:source.start_ms,channel:sourceParts[1] === "them" || sourceParts[1] === "me" ? sourceParts[1] : undefined,segId:Number(sourceParts[2])}} onClose={() => setShowCall(false)} onDeleted={() => { setShowCall(false); setSource(null); setError("The saved call was deleted. This citation is no longer available."); }} /></Suspense></PageHeadingActive.Provider>}</Card>}<div ref={conversationElement} hidden={source !== null}>
      <details className="ask-context"><summary>Context: {scope.kind}{scope.id ? ` · ${options.find(o => o.id === scope.id)?.label ?? scope.id}` : ""}</summary><div className="ui-stack"><Field label="Context"><Select value={scope.kind} onChange={e => setScope({ ...scope, kind: e.target.value as AskScope["kind"], id: undefined })}>{["auto", "all", "general", "company", "application", "document", "call"].map(kind => <option key={kind} value={kind}>{kind === "auto" ? "Auto · this profile" : kind}</option>)}</Select></Field>{!["auto", "all", "general"].includes(scope.kind) && <Field label="Selected source"><Select value={scope.id ?? ""} onChange={e => setScope({ ...scope, id: e.target.value })}><option value="">Choose context</option>{scope.id && !options.some(o => o.id === scope.id) && <option value={scope.id}>{scope.id}</option>}{options.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}</Select></Field>}<Field label="From date"><Input type="date" value={scope.from_date ?? ""} onChange={e => setScope({ ...scope, from_date: e.target.value || undefined })} /></Field><Field label="To date"><Input type="date" value={scope.to_date ?? ""} onChange={e => setScope({ ...scope, to_date: e.target.value || undefined })} /></Field></div></details>
      <div aria-live="polite" className="ask-messages">{!current?.messages?.length && <div className="ask-empty"><h3>What would you like to work through?</h3><p>Find a decision, prepare for a conversation, or reflect on your progress.</p><div className="ask-prompts">{["What did I decide in my last meeting?", "Help me prepare for my next interview", "Summarize my recent work"].map(prompt => <Button key={prompt} onClick={() => setDraft(prompt)}>{prompt}</Button>)}</div></div>}{current?.messages?.map(m => <Card key={m.id} title={m.question}><p style={{ whiteSpace: "pre-wrap" }}>{m.answer || (m.status === "pending" ? "Preparing an answer…" : m.error || m.status)}</p><div className="ui-row-inline">{m.sources.map(s => <Button size="sm" key={`${s.kind}/${s.id}`} onClick={() => void inspect(s)}>{s.label}{s.date ? ` · ${s.date.slice(0, 10)}` : ""}</Button>)}</div>{m.status === "pending" && <Button onClick={() => void action(m, "cancel")}>Cancel answer</Button>}{["failed", "cancelled"].includes(m.status) && <Button onClick={() => void action(m, "retry")}>Retry answer</Button>}</Card>)}</div>
      <form className="ask-composer" onSubmit={e => { e.preventDefault(); void send(); }}><Field label="Ask a question"><Textarea value={draft} maxLength={8000} onChange={e => setDraft(e.target.value)} placeholder="Ask about your work…" rows={3} /></Field><Button type="submit" variant="primary" loading={busy} disabled={pending || !draft.trim() || (!["auto", "all", "general"].includes(scope.kind) && !scope.id)}>Ask</Button></form>
      <p className="ui-field-hint">{current?.retrieval_notice ?? "Answers use available information in this profile. Public web search is not connected."}</p>{current?.retention_notice && <p className="ui-field-hint">{current.retention_notice}</p>}
    </div>
    <ConfirmDialog open={deleting} onOpenChange={setDeleting} title="Delete conversation?" confirmLabel="Delete" onConfirm={remove} />
  </div></>;
}
