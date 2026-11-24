import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { PageHeadingActive } from "./ui/Layout";
import { request, POLL_READ } from "./api";
import { usePolling } from "./usePolling";
import { Banner, Button, Card, ConfirmDialog, Field, Input, Select, SkeletonList, Spinner, Textarea } from "./ui";
import { Icon } from "./ui/Icon";
import "./feature-pages.css";
export type AskScope = { kind: "auto" | "all" | "general" | "company" | "application" | "document" | "call"; id?: string; from_date?: string; to_date?: string };
export type AskEntry = { scope: AskScope; n: number };
export function openAsk(scope: AskScope) { window.dispatchEvent(new CustomEvent("copilot:open-ask", { detail: scope })); }
type Source = { id: string; kind: string; label: string; date?: string; start_ms?: number; target_id?: string };
type Message = { id: string; request_id: string; question: string; answer: string; status: "pending" | "done" | "failed" | "cancelled"; sources: Source[]; error?: string };
type Conversation = { id: string; title: string; scope: AskScope; messages?: Message[]; retrieval_notice?: string; retention_notice?: string };
const base = "/ask/conversations";
const SavedCall = lazy(() => import("./SessionDetail").then(m => ({ default: m.SessionDetail })));
const scopeLabels: Record<AskScope["kind"], string> = { auto: "This profile", all: "All available context", general: "General knowledge", company: "Company", application: "Application", document: "Document", call: "Saved call" };
export function Ask({ active = true, entry }: { active?: boolean; entry?: AskEntry | null }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState<Conversation | null>(null);
  const [scope, setScope] = useState<AskScope>({ kind: "auto" });
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [loadingSource, setLoadingSource] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [source, setSource] = useState<(Source & { content: string }) | null>(null);
  const [showCall, setShowCall] = useState(false);
  const [options, setOptions] = useState<{ id: string; label: string }[]>([]);
  const conversationElement = useRef<HTMLDivElement>(null);
  const draftElement = useRef<HTMLTextAreaElement>(null);
  const scrollPosition = useRef<{ element: Element; top: number } | null>(null);
  const alive = useRef(true);
  const generation = useRef(0), sourceGeneration = useRef(0), sending = useRef(false);
  const valid = (ticket: number) => alive.current && generation.current === ticket;
  const invalidate = () => { generation.current++; sourceGeneration.current++; sending.current = false; setBusy(false); setUpdateError(null); setDeleting(false); setLoadingConversation(false); setLoadingSource(false); };
  const currentRef = useRef(current); currentRef.current = current;
  const drafts = useRef(new Map<string, string>());
  const [pendingRequest, setPendingRequest] = useState<{ id: string; question: string; scope: AskScope } | null>(null);
  const reload = () => { const ticket = generation.current; return request<{ conversations: Conversation[] }>("GET", base).then(data => { if (valid(ticket)) setConversations(data.conversations); }); };
  useEffect(() => { alive.current = true; void reload().catch(e => setError(String(e))); return () => { alive.current = false; generation.current++; sourceGeneration.current++; for (const m of currentRef.current?.messages ?? []) if (m.status === "pending") void request("POST", `${base}/${currentRef.current!.id}/messages/${m.id}/cancel`).catch(() => {}); }; }, []);
  useEffect(() => { if (entry) { invalidate(); drafts.current.set(current?.id ?? "new", draft); setCurrent(null); setScope(entry.scope); setDraft(""); setSource(null); setPendingRequest(null); } }, [entry?.n]);
  const pending = current?.messages?.some(m => m.status === "pending");
  usePolling(async () => {
    const conversation = currentRef.current;
    if (!conversation) return;
    const ticket = generation.current;
    try {
      const messages = await Promise.all((conversation.messages ?? []).filter(m => m.status === "pending").map(m =>
        request<Message>("GET", `${base}/${conversation.id}/messages/${m.id}`, undefined, POLL_READ)));
      if (!valid(ticket)) return;
      setCurrent(previous => previous?.id === conversation.id ? { ...previous, messages: previous.messages?.map(m => messages.find(next => next.id === m.id) ?? m) } : previous);
      setUpdateError(null);
    } catch (e) {
      if (valid(ticket)) setUpdateError(`Your question is saved. Reconnecting to the answer… ${String(e)}`);
      return false;
    }
  }, 1000, active && !!pending, { immediate: true, key: `${current?.id}/${current?.messages?.filter(m => m.status === "pending").map(m => m.id).join(",")}`, onTimeout: () => setUpdateError("Your question is saved. Automatic checks paused; reopen the conversation to check the answer.") });
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
    setLoadingConversation(true);
    try { const value = await request<Conversation>("GET", `${base}/${id}`); if (!valid(ticket)) return; setCurrent(value); setScope(value.scope); setDraft(value => value || drafts.current.get(id) || ""); } catch (e) { if (valid(ticket)) setError(String(e)); } finally { if (valid(ticket)) setLoadingConversation(false); }
  };
  const needsContext = !["auto", "all", "general"].includes(scope.kind) && !scope.id;
  const invalidDates = !!(scope.from_date && scope.to_date && scope.from_date > scope.to_date);
  const send = async () => {
    if (sending.current || busy || pending || loadingConversation || needsContext || invalidDates || !draft.trim()) return;
    const ticket = generation.current;
    sending.current = true;
    setBusy(true); setError(null);
    const sameScope = pendingRequest && ["kind", "id", "from_date", "to_date"].every(key => pendingRequest.scope[key as keyof AskScope] === scope[key as keyof AskScope]);
    const attempt = pendingRequest?.question === draft && sameScope ? pendingRequest : { id: crypto.randomUUID(), question: draft, scope: { ...scope } };
    setPendingRequest(attempt);
    try {
      let conversation = current;
      if (!conversation) { conversation = await request<Conversation>("POST", base, { title: draft.slice(0, 80), scope }); if (!valid(ticket)) return; setCurrent(conversation); }
      const message = await request<Message>("POST", `${base}/${conversation.id}/messages`, { request_id: attempt.id, question: attempt.question, scope: attempt.scope });
      if (!valid(ticket)) return;
      // The POST receipt is already persisted. Publish it before another network read so
      // a brief outage cannot hide an accepted question or stop answer polling.
      setCurrent({ ...conversation, scope: attempt.scope, messages: [...(conversation.messages ?? []).filter(m => m.id !== message.id), message] });
      setDraft(value => value === attempt.question ? "" : value); drafts.current.delete(conversation.id); setPendingRequest(null);
      void reload().catch(() => {});
    } catch (e) { if (valid(ticket)) setError(String(e)); } finally { if (valid(ticket)) { sending.current = false; setBusy(false); } }
  };
  const action = async (message: Message, command: "cancel" | "retry") => {
    if (!current) return;
    const ticket = generation.current, id = current.id;
    try { const value = await request<Message>("POST", `${base}/${id}/messages/${message.id}/${command}`); if (!valid(ticket)) return; setCurrent(previous => previous?.id === id ? { ...previous, messages: previous.messages?.map(m => m.id === value.id ? value : m) } : previous); setError(null); if (command === "cancel") setDraft(value => value || message.question); } catch (e) { if (valid(ticket)) setError(String(e)); }
  };
  const inspect = async (item: Source) => {
    setShowCall(false);
    setLoadingSource(true);
    const ticket = generation.current, sourceTicket = ++sourceGeneration.current;
    let element: Element | null = conversationElement.current;
    while (element && element.scrollHeight <= element.clientHeight) element = element.parentElement;
    if (element) scrollPosition.current = { element, top: element.scrollTop };
    try { const value = await request<{ content: string; start_ms?: number }>("GET", `/ask/sources/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}`); if (valid(ticket) && sourceTicket === sourceGeneration.current) setSource({ ...item, ...value, label: item.label }); } catch (e) { if (valid(ticket) && sourceTicket === sourceGeneration.current) setError(`Source unavailable: ${String(e)}`); } finally { if (valid(ticket) && sourceTicket === sourceGeneration.current) setLoadingSource(false); }
  };
  const remove = async () => {
    const ticket = generation.current, id = current?.id;
    try { if (id) await request("DELETE", `${base}/${id}`); if (!valid(ticket)) return; invalidate(); setCurrent(null); setDraft(""); await reload(); } catch (e) { if (valid(ticket)) setError(String(e)); }
  };
  const callId = source?.kind === "transcript" ? source.target_id ?? source.id.split(":")[0] : null;
  const sourceParts = source?.id.split(":") ?? [];
  const hasMessages = !!current?.messages?.length;
  const composer = <form className="ask-composer" onSubmit={e => { e.preventDefault(); void send(); }}>
    <Field label="Ask a question"><Textarea ref={draftElement} dir="auto" value={draft} maxLength={8000} onChange={e => setDraft(e.target.value)} onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} placeholder="Ask about your work…" rows={2} /></Field>
    <div className="ask-composer-footer"><span className="ui-field-hint">{needsContext ? "Choose a source in Context to continue." : invalidDates ? "Check the date range in Context." : pending ? "Your answer is being prepared." : "Ctrl / ⌘ + Enter to ask"}</span><Button type="submit" variant="primary" loading={busy} disabled={pending || loadingConversation || !draft.trim() || needsContext || invalidDates}>Ask</Button></div>
  </form>;
  return <div className="ask-workspace feature-page">
    <header className="feature-heading">
      <div><h2>Ask about your work</h2><p>Answers grounded in your calls, documents, and experience.</p></div>
      <span className="feature-heading-icon" aria-hidden="true"><Icon name="work" size={20} /></span>
    </header>
    <div className="feature-toolbar ask-toolbar">
      <Field label="Saved conversations"><Select aria-label="Saved conversations" value={current?.id ?? ""} onChange={e => void choose(e.target.value)}><option value="">New conversation</option>{conversations.map(c => <option key={c.id} value={c.id}>{c.title}</option>)}</Select></Field>
      <Button onClick={() => void choose("")}>New</Button>
      {current && <Button variant="danger-ghost" size="sm" onClick={() => setDeleting(true)}>Delete conversation</Button>}
    </div>
    {error && <Banner tone="danger" title="Could not complete your request" action={scope.id ? <Button size="sm" onClick={() => { setScope({ kind: "auto" }); setPendingRequest(null); setError(null); }}>Use profile context</Button> : undefined}>{error}</Banner>}
    {updateError && <Banner tone="warning" title="Answer connection interrupted">{updateError}</Banner>}
    {loadingSource && <p className="feature-loading" role="status"><Spinner size="sm" label={null} /> Opening source…</p>}
    {source && <Card className="ask-source" title={source.label} actions={<Button size="sm" onClick={() => { sourceGeneration.current++; setSource(null); requestAnimationFrame(() => { const saved = scrollPosition.current; if (saved) saved.element.scrollTop = saved.top; }); }}>Back to conversation</Button>}>
      {source.start_ms !== undefined && <p className="ui-field-hint">Transcript at {Math.floor(source.start_ms / 1000)} seconds</p>}
      <div className="feature-prose" dir="auto">{source.content}</div>
      {callId && !showCall && <Button onClick={() => setShowCall(true)}>Open saved call at this passage</Button>}
      {callId && showCall && <PageHeadingActive.Provider value={false}><Suspense fallback={<SkeletonList rows={2} label="Loading saved call" />}><SavedCall sessionId={callId} transcriptFocus={{sessionId:callId,startMs:source.start_ms,channel:sourceParts[1] === "them" || sourceParts[1] === "me" ? sourceParts[1] : undefined,segId:Number(sourceParts[2])}} onClose={() => setShowCall(false)} onDeleted={() => { setShowCall(false); setSource(null); setError("The saved call was deleted. This citation is no longer available."); }} /></Suspense></PageHeadingActive.Provider>}
    </Card>}
    <div ref={conversationElement} hidden={source !== null} className={`ask-conversation${hasMessages ? "" : " ask-conversation-empty"}`}>
      <details className="ask-context feature-disclosure">
        <summary><span>Context</span><span className="feature-summary-meta">{scopeLabels[scope.kind]}{scope.id ? ` · ${options.find(o => o.id === scope.id)?.label ?? "Selected source"}` : ""}</span></summary>
        <div className="ui-stack">
          <Field label="Context"><Select value={scope.kind} onChange={e => setScope({ ...scope, kind: e.target.value as AskScope["kind"], id: undefined })}>{Object.entries(scopeLabels).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}</Select></Field>
          {!["auto", "all", "general"].includes(scope.kind) && <Field label="Selected source" hint={options.length ? undefined : "No available sources in this category."}><Select value={scope.id ?? ""} onChange={e => setScope({ ...scope, id: e.target.value })}><option value="">Choose context</option>{scope.id && !options.some(o => o.id === scope.id) && <option value={scope.id}>Selected source</option>}{options.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}</Select></Field>}
          <Field label="From date" optional><Input type="date" value={scope.from_date ?? ""} onChange={e => setScope({ ...scope, from_date: e.target.value || undefined })} /></Field>
          <Field label="To date" optional error={invalidDates ? "Choose a date on or after the start date." : null}><Input type="date" min={scope.from_date} value={scope.to_date ?? ""} onChange={e => setScope({ ...scope, to_date: e.target.value || undefined })} /></Field>
        </div>
      </details>
      {!hasMessages && composer}
      {loadingConversation ? <SkeletonList rows={3} label="Loading conversation" /> : <div aria-live="polite" aria-relevant="additions text" className="ask-messages">
        {!current?.messages?.length && <div className="ask-empty">
          <h3>What would you like to work through?</h3><p>Start with a question, or choose one below.</p>
          <div className="ask-prompts">{["What did I decide in my last meeting?", "Help me prepare for my next interview", "Summarize my recent work"].map(prompt => <Button key={prompt} onClick={() => { setDraft(prompt); draftElement.current?.focus(); }}>{prompt}<span aria-hidden="true">↗</span></Button>)}</div>
        </div>}
        {current?.messages?.map(m => <article key={m.id} className="ask-message">
          <div className="ask-question"><span className="feature-eyebrow">You asked</span><h3 dir="auto">{m.question}</h3></div>
          <div className="ask-response">
            {m.answer ? <div className="feature-prose" dir="auto">{m.answer}</div> : m.status === "pending" ? <p className="feature-loading"><Spinner size="sm" label={null} /> Preparing an answer…</p> : <p className="ui-text-2">{m.error || (m.status === "cancelled" ? "Answer cancelled. Your question is saved." : "An answer could not be prepared. Try again.")}</p>}
            {!!m.sources.length && <div className="ask-citations"><span className="feature-eyebrow">Sources</span><div className="ui-row-inline">{m.sources.map(s => <Button size="sm" key={`${s.kind}/${s.id}`} onClick={() => void inspect(s)}>{s.label}{s.date ? ` · ${s.date.slice(0, 10)}` : ""}</Button>)}</div></div>}
            {m.status === "pending" && <Button size="sm" variant="ghost" onClick={() => void action(m, "cancel")}>Cancel answer</Button>}
            {["failed", "cancelled"].includes(m.status) && <Button size="sm" onClick={() => void action(m, "retry")}>Retry answer</Button>}
          </div>
        </article>)}
      </div>}
      {hasMessages && composer}
      <p className="feature-footnote">{current?.retrieval_notice ?? "Answers use available information in this profile. Public web search is not connected."}</p>
      {current?.retention_notice && <p className="feature-footnote">{current.retention_notice}</p>}
    </div>
    <ConfirmDialog open={deleting} onOpenChange={setDeleting} title="Delete conversation?" confirmLabel="Delete" onConfirm={remove}>This removes the saved questions, answers, and citations in this conversation.</ConfirmDialog>
  </div>;
}
