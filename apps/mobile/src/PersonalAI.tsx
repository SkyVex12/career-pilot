import React, { useEffect, useRef, useState } from "react";
import { Alert, Linking, Share, Switch, View } from "react-native";
import { randomUUID } from "expo-crypto";
import type { Api } from "./api";
import { ApiError } from "./api";
import { ActionCard, Badge, Button, Card, EmptyState, Field, IconButton, Input, Loading, Notice, Row, Screen, ScreenTitle, Select, Txt } from "./ui";
import { FloatingNotice } from "./FloatingNotice";
import { useWorkspaceRequests, type WorkspaceRequest } from "./useWorkspaceRequests";
import { list, localDay, record, text } from "./workspaceData";
import { handoffText, importBriefText, parseAIConnections, parseBriefReceipt, parsePreparationRequest, parsePreparationRequests, preparationBody, publicationAttempt, reviewedBrief, withBriefReceipt, withPreparationRequest, type AIConnection, type Brief, type BriefFact, type ContextKind, type ImportAttempt, type PreparationRequest, type Purpose } from "./personalAIData";

const strong = { fontSize: 17, lineHeight: 24, fontWeight: "600" as const };
const base = "/personal-ai";
const pathOf = (id: string) => `${base}/requests/${encodeURIComponent(id)}`;
const defaultQuestion = "Summarize recent progress, decisions, blockers and next steps with sources and coverage gaps.";
const dateLabel = (value: string | number) => new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
const confirm = (title: string, detail: string, label: string, action: () => void) => Alert.alert(title, detail, [{ text: "Cancel", style: "cancel" }, { text: label, style: "destructive", onPress: action }]);

async function requestAI(request: WorkspaceRequest, path: string, body?: unknown, method = "GET") {
  try { return await request(path, body, method); }
  catch (error) {
    if (error instanceof ApiError && error.status === 404 && (path === `${base}/requests` || path === `${base}/oauth/connections`)) throw new Error("Personal AI is unavailable on this server. Update the server to enable private requests, briefs, and MCP connections.");
    if (error instanceof ApiError && error.status === 409) throw new Error(`${error.message} Refresh this request before retrying; another device or AI may have updated it.`);
    throw error;
  }
}

export function PersonalAI({ api, visible = true }: { api: Api; visible?: boolean }) {
  const work = useWorkspaceRequests(api);
  const [requests, setRequests] = useState<PreparationRequest[]>([]);
  const [loaded, setLoaded] = useState(false), [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<PreparationRequest | null>(null);
  const [message, setMessage] = useState("");
  async function refresh() {
    const value = await work.read(async request => parsePreparationRequests(await requestAI(request, `${base}/requests`)));
    if (value) { setRequests(value); setLoaded(true); }
  }
  useEffect(() => {
    if (visible && work.foreground && !creating && !selected) void refresh(); else work.cancelRead();
  }, [api, visible, work.foreground, creating, selected?.id]);
  if (creating) return <CreateRequest api={api} visible={visible} onBack={() => setCreating(false)} onSaved={value => { setRequests(old => withPreparationRequest(old, value)); setCreating(false); setSelected(value); setMessage("Preparation request created."); }} />;
  if (selected) return <RequestDetail key={selected.id} api={api} visible={visible} initial={selected} initialMessage={message} onBack={value => { setRequests(old => withPreparationRequest(old, value)); setSelected(null); setMessage(""); }} onDeleted={() => { setRequests(old => old.filter(r => r.id !== selected.id)); setSelected(null); setMessage("Preparation request deleted."); }} />;
  return <View style={{ flex: 1 }}><Screen>
    <ScreenTitle title="Personal AI" subtitle="Bring a private brief from the AI you already use into calls, practice, and Ask." action={<IconButton label="Refresh preparation requests" icon="refresh" onPress={() => void refresh()} disabled={work.loading} />} />
    <Button label="Prepare context" icon="plus" onPress={() => setCreating(true)} />
    {work.error ? <Notice tone="error" action={<Button label="Try again" secondary compact onPress={() => void refresh()} />}>{work.error}</Notice> : null}
    {!loaded && work.loading ? <Loading label="Loading preparation requests…" /> : null}
    {loaded && !requests.length ? <EmptyState icon="file" title="Start with a question" description="Choose what to prepare, share the request with your AI, then review its brief here." /> : null}
    {requests.map(request => <ActionCard key={request.id} label={`Open ${request.scope.title}`} onPress={() => { setMessage(""); setSelected(request); }}>
      <Txt style={strong}>{request.scope.title}</Txt>
      <Txt muted>{request.scope.date_from} – {request.scope.date_to}</Txt>
      <Badge label={request.active_version_id ? "Brief ready" : "Awaiting brief"} tone={request.active_version_id ? "success" : "neutral"} />
    </ActionCard>)}
    <AIConnections api={api} visible={visible} onMessage={setMessage} />
  </Screen><FloatingNotice message={message} onDismiss={() => setMessage("")} /></View>;
}

function CreateRequest({ api, visible, onBack, onSaved }: { api: Api; visible: boolean; onBack: () => void; onSaved: (request: PreparationRequest) => void }) {
  const work = useWorkspaceRequests(api), choices = useWorkspaceRequests(api);
  const today = useRef(localDay()).current;
  const [title, setTitle] = useState("Meeting preparation"), [purpose, setPurpose] = useState<Purpose>("call");
  const [kind, setKind] = useState<ContextKind>("profile"), [target, setTarget] = useState("");
  const [questions, setQuestions] = useState([defaultQuestion]);
  const [from, setFrom] = useState(today), [to, setTo] = useState(today);
  const [options, setOptions] = useState<{ value: string; label: string }[]>([]);
  const dirty = title !== "Meeting preparation" || purpose !== "call" || kind !== "profile" || questions.length !== 1 || questions[0] !== defaultQuestion || from !== today || to !== today;
  async function loadOptions() {
    if (kind === "profile") return;
    const value = await choices.read(async request => list(record(await request(`/ask/context-options?kind=${kind}`)).options, item => { const r = record(item); return { value: text(r.id), label: text(r.label) }; }));
    if (value) setOptions(value);
  }
  useEffect(() => { if (visible && choices.foreground) void loadOptions(); else choices.cancelRead(); }, [api, visible, choices.foreground, kind]);
  async function save() {
    let body;
    try { body = preparationBody({ title, purpose, kind, target, questions, from, to }); }
    catch (error) { work.setError(String(error)); return; }
    const result = await work.mutate(async request => parsePreparationRequest(await requestAI(request, `${base}/requests`, body, "POST")));
    if (result) onSaved(result.value);
  }
  return <Screen>
    <Button label="Back to requests" secondary compact disabled={work.busy} onPress={() => dirty ? confirm("Discard preparation request?", "Your new request has not been saved.", "Discard", onBack) : onBack()} />
    <ScreenTitle title="Prepare context" subtitle="Choose a focused question and the period your AI should cover." />
    <Card>
      <Field label="Request title"><Input accessibilityLabel="Request title" value={title} onChangeText={setTitle} maxLength={200} editable={!work.busy} /></Field>
      <Select label="Use this brief for" value={purpose} options={[{ value: "call", label: "Calls" }, { value: "practice", label: "Interview practice" }, { value: "ask", label: "Ask about my work" }]} onChange={value => setPurpose(value as Purpose)} disabled={work.busy} />
      <Select label="Context" value={kind} options={[{ value: "profile", label: "Current profile" }, { value: "company", label: "Company" }, { value: "application", label: "Application" }]} disabled={work.busy} onChange={value => { choices.cancelRead(); setTarget(""); setOptions([]); setKind(value as ContextKind); }} />
      {kind !== "profile" ? <>
        <Select label={kind === "company" ? "Company" : "Application"} value={target} options={options} onChange={setTarget} disabled={work.busy || choices.loading} />
        {choices.loading ? <Loading label="Loading available context…" /> : choices.error ? <Notice tone="error" action={<Button label="Retry context" secondary compact onPress={() => void loadOptions()} />}>{choices.error}</Notice> : !options.length ? <Txt muted>{kind === "company" ? "Add a company in Work first." : "Add an application in Interview prep first."}</Txt> : null}
      </> : null}
      {questions.map((question, index) => <Field key={index} label={`Preparation question ${index + 1}`}>
        <Input accessibilityLabel={`Preparation question ${index + 1}`} value={question} multiline maxLength={1200} style={{ minHeight: 100 }} editable={!work.busy} onChangeText={value => setQuestions(old => old.map((q, i) => i === index ? value : q))} />
        {questions.length > 1 ? <Button label={`Remove question ${index + 1}`} secondary compact disabled={work.busy} onPress={() => setQuestions(old => old.filter((_, i) => i !== index))} /> : null}
      </Field>)}
      {questions.length < 8 ? <Button label="Add question" secondary icon="plus" compact disabled={work.busy} onPress={() => setQuestions(old => [...old, ""])} /> : null}
      <Field label="From" hint="YYYY-MM-DD"><Input accessibilityLabel="From date" value={from} onChangeText={setFrom} autoCapitalize="none" maxLength={10} editable={!work.busy} /></Field>
      <Field label="Through" hint="An ordered range of up to 366 days."><Input accessibilityLabel="Through date" value={to} onChangeText={setTo} autoCapitalize="none" maxLength={10} editable={!work.busy} /></Field>
      {work.error ? <Notice tone="error">{work.error}</Notice> : null}
      <Button label="Create request" loading={work.busy} disabled={!title.trim() || questions.some(q => !q.trim()) || (kind !== "profile" && !target)} onPress={() => void save()} />
    </Card>
  </Screen>;
}

function RequestDetail({ api, visible, initial, initialMessage, onBack, onDeleted }: { api: Api; visible: boolean; initial: PreparationRequest; initialMessage: string; onBack: (value: PreparationRequest) => void; onDeleted: () => void }) {
  const work = useWorkspaceRequests(api);
  const [value, setValue] = useState(initial), [message, setMessage] = useState(initialMessage);
  const [handoff, setHandoff] = useState(false), [importing, setImporting] = useState(false);
  const [paste, setPaste] = useState(""), [preview, setPreview] = useState<Brief | null>(null);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [needsRefresh, setNeedsRefresh] = useState(!!initial.active_version_id && !initial.active_brief);
  const [edit, setEdit] = useState<{ id: string; text: string; original: string; version: string } | null>(null);
  const attempt = useRef<ImportAttempt | null>(null);
  const brief = value.active_brief?.brief;
  const busy = work.busy || needsRefresh;
  async function refresh() {
    const next = await work.read(async request => parsePreparationRequest(await requestAI(request, pathOf(initial.id))));
    if (next) { setValue(next); setNeedsRefresh(false); }
  }
  useEffect(() => { if (visible && work.foreground) void refresh(); else work.cancelRead(); }, [api, visible, work.foreground]);
  function back() {
    if (work.busy) return;
    if (paste.trim() || (edit && edit.text !== edit.original)) confirm("Leave this request?", "Your pasted brief or unsaved correction will be discarded.", "Discard changes", () => onBack(value)); else onBack(value);
  }
  function review() {
    try { setPreview(importBriefText(paste)); setExcluded(new Set()); work.setError(""); }
    catch (error) { setPreview(null); work.setError(String(error)); }
  }
  async function publish() {
    if (!preview || needsRefresh) return;
    const brief = reviewedBrief(preview, excluded);
    attempt.current = publicationAttempt(value, brief, attempt.current, randomUUID);
    const result = await work.mutate(async request => parseBriefReceipt(await requestAI(request, `${pathOf(value.id)}/publish`, { request_revision: value.request_revision, idempotency_key: attempt.current!.key, brief }, "POST"), value.id, true));
    if (!result) return;
    // The POST receipt is final even when the subsequent GET is offline. Do not invite a duplicate import.
    setValue(old => withBriefReceipt(old, result.value));
    setPaste(""); setPreview(null); setImporting(false); setEdit(null); setNeedsRefresh(true);
    setMessage("Brief imported. It can now inform calls, practice, and Ask.");
    await refresh();
  }
  async function prepareRefresh() {
    const result = await work.mutate(async request => parsePreparationRequest(await requestAI(request, `${pathOf(value.id)}/refresh`, { expected_revision: value.request_revision }, "POST")));
    if (!result) return;
    // Refresh returns the new revision, but intentionally omits active_brief. Preserve the displayed saved brief.
    setValue(old => ({ ...result.value, ...(old.active_brief?.id === result.value.active_version_id ? { active_brief: old.active_brief } : {}) }));
    setPreview(null); setNeedsRefresh(true); setMessage("New request revision ready. Share the updated request with your AI.");
    await refresh();
  }
  async function correct(item: BriefFact, nextText: string | null, version: string) {
    const result = await work.mutate(async request => parseBriefReceipt(await requestAI(request, `${pathOf(value.id)}/items/${encodeURIComponent(item.id)}`, { expected_version_id: version, text: nextText }, "PATCH"), value.id));
    if (!result) return;
    setValue(old => withBriefReceipt(old, result.value));
    setEdit(null); setNeedsRefresh(true); setMessage(nextText === null ? "Fact excluded from this request and future refreshes." : "Correction saved.");
    await refresh();
  }
  async function shareRequest() {
    try { await Share.share({ title: value.scope.title, message: handoffText(value) }); }
    catch { work.setError("Could not open sharing. Long-press the request text to select and copy it."); }
  }
  return <View style={{ flex: 1 }}><Screen>
    <Row><Button label="Back to requests" secondary compact disabled={work.busy} onPress={back} /><IconButton label="Refresh brief" icon="refresh" disabled={work.loading || work.busy} onPress={() => void refresh()} /></Row>
    <ScreenTitle title={value.scope.title} subtitle={`${value.scope.date_from} – ${value.scope.date_to}`} />
    <Row><Badge label={`Revision ${value.request_revision}`} /><Badge label={value.scope.purpose === "practice" ? "Interview practice" : value.scope.purpose === "ask" ? "Ask" : "Calls"} /></Row>
    {work.error ? <Notice tone="error" action={<Button label="Refresh request" secondary compact disabled={work.loading || work.busy} onPress={() => void refresh()} />}>{work.error}</Notice> : null}
    {needsRefresh ? <Notice>The change was saved. Refresh to view the current brief before editing again.</Notice> : null}
    {work.loading ? <Loading label="Refreshing brief…" /> : null}
    <Card>
      <Txt style={strong}>Your preparation questions</Txt>{value.scope.questions.map((question, index) => <Txt key={index}>{question}</Txt>)}
      <Txt muted>Share this request with your AI. A connected AI can publish directly; otherwise, paste the brief JSON it returns below.</Txt>
      <Button label="Share AI request" secondary icon="arrow" onPress={() => void shareRequest()} />
      <Button label={handoff ? "Hide request text" : "View or copy request text"} secondary compact onPress={() => setHandoff(!handoff)} />
      {handoff ? <>
        <Txt muted>Long-press the text to select and copy the complete request.</Txt>
        <Txt selectable style={{ fontSize: 12, lineHeight: 19 }}>{handoffText(value)}</Txt>
      </> : null}
    </Card>
    {brief ? <>
      <Txt style={strong}>{brief.title}</Txt>
      <Txt muted>{dateLabel(brief.prepared_at)} · {value.active_brief?.origin === "paste" ? "Pasted brief" : value.active_brief?.origin === "correction" ? "Your correction" : "Published through MCP"}</Txt>
      {brief.items.map((item, index) => <Card key={item.id}>
        <Row><Badge label={item.kind.replaceAll("_", " ")} /><Badge label={item.provenance.replaceAll("_", " ")} /></Row>
        {edit?.id === item.id ? <>
          <Input accessibilityLabel={`Correct brief fact ${index + 1}`} multiline value={edit.text} onChangeText={text => setEdit({ ...edit, text })} editable={!busy} maxLength={1200} style={{ minHeight: 100 }} />
          {edit.version !== value.active_version_id ? <Notice tone="error">This brief changed on another device. Cancel this edit, review the current fact, then edit again.</Notice> : null}
          <Row><Button label="Save correction" compact disabled={busy || !edit.text.trim() || edit.text.trim() === edit.original || edit.version !== value.active_version_id} onPress={() => void correct(item, edit.text.trim(), edit.version)} /><Button label="Cancel edit" secondary compact disabled={work.busy} onPress={() => setEdit(null)} /></Row>
        </> : <>
          <Txt selectable>{item.text}</Txt>
          <Row><Button label={`Edit fact ${index + 1}`} secondary compact disabled={busy || !!edit} onPress={() => setEdit({ id: item.id, text: item.text, original: item.text, version: value.active_version_id! })} /><Button label={`Exclude fact ${index + 1}`} secondary compact danger disabled={busy || !!edit} onPress={() => confirm("Permanently exclude this fact?", "It will stop informing answers. The same fact ID stays excluded when your AI refreshes this request; this cannot be undone here.", "Exclude fact", () => void correct(item, null, value.active_version_id!))} /></Row>
        </>}
        {item.source_ids.length ? <Txt muted>Sources: {item.source_ids.map(id => brief.sources.find(source => source.id === id)?.title ?? id).join(" · ")}</Txt> : <Txt muted>No linked source</Txt>}
      </Card>)}
      {edit && !brief.items.some(item => item.id === edit.id) ? <Notice action={<Button label="Discard stale correction" secondary compact onPress={() => setEdit(null)} />}>The fact you were editing was removed by another update. Your correction was not submitted.</Notice> : null}
      {!!brief.sources.length ? <Card><Txt style={strong}>Sources</Txt>{brief.sources.map(source => <View key={source.id} style={{ gap: 5 }}>
        <Txt style={{ fontWeight: "600" }}>{source.title}</Txt><Txt muted>{source.attribution}{source.source_date ? ` · ${source.source_date}` : ""}</Txt>
        {source.url ? <Button label={`Open ${source.title}`} secondary compact onPress={() => void Linking.openURL(source.url!).catch(() => work.setError("Could not open the source link. Check your connection and try again."))} /> : null}
      </View>)}</Card> : null}
      {!!brief.coverage_gaps.length ? <Card><Txt style={strong}>What this brief does not cover</Txt>{brief.coverage_gaps.map((gap, index) => <Txt key={index}>{gap}</Txt>)}</Card> : null}
    </> : !needsRefresh ? <Notice>Share the preparation request with your AI, then import its brief below. No live call is needed.</Notice> : null}
    <Card>
      <Button label={importing ? "Hide brief import" : "Paste a brief from your AI"} secondary disabled={work.busy} onPress={() => setImporting(!importing)} />
      {importing ? <>
        <Field label="Brief JSON" hint="Paste only the brief object. You can review its facts before saving."><Input accessibilityLabel="Brief JSON" value={paste} onChangeText={text => { setPaste(text); setPreview(null); }} multiline editable={!work.busy} maxLength={262144} autoCapitalize="none" autoCorrect={false} style={{ minHeight: 150 }} placeholder='{"title": "…", "prepared_at": "…", "items": […]}' /></Field>
        <Button label="Review pasted brief" secondary disabled={work.busy || !paste.trim()} onPress={review} />
        {preview ? <>
          <Txt style={strong}>{preview.title}</Txt><Txt muted>{dateLabel(preview.prepared_at)} · {preview.sources.length} sources. Excluded facts will not be imported.</Txt>
          {preview.items.map((item, index) => <View key={item.id} style={{ gap: 5 }}>
            <Row><Txt style={{ flex: 1 }}>{item.kind.replaceAll("_", " ")} · {item.provenance.replaceAll("_", " ")}</Txt><Switch accessibilityLabel={`Include fact ${index + 1}`} value={!excluded.has(item.id)} disabled={work.busy} onValueChange={include => setExcluded(old => { const next = new Set(old); if (include) next.delete(item.id); else next.add(item.id); return next; })} /></Row>
            <Txt selectable>{item.text}</Txt>
            {item.source_ids.map(id => { const source = preview.sources.find(source => source.id === id)!; return <Txt key={id} muted>{source.title} · {source.attribution}{source.source_date ? ` · ${source.source_date}` : ""}</Txt>; })}
          </View>)}
          {preview.coverage_gaps.map((gap, index) => <Txt key={index} muted>Coverage gap: {gap}</Txt>)}
          <Button label={`Import ${preview.items.filter(item => !excluded.has(item.id)).length} facts`} loading={work.busy} disabled={needsRefresh} onPress={() => void publish()} />
        </> : null}
      </> : null}
    </Card>
    <Button label="Prepare an updated AI request" secondary disabled={busy || !!edit || !!paste.trim()} onPress={() => confirm("Prepare a new revision?", "Your saved brief stays available. Existing AI handoffs must be replaced with the new revision before publishing again.", "Prepare revision", () => void prepareRefresh())} />
    <Button label="Delete preparation request" secondary danger disabled={work.busy} onPress={() => confirm("Delete this request and its briefs?", "This permanently removes its saved versions. Your AI will no longer be able to publish to this request.", "Delete request", () => void work.mutate(request => requestAI(request, pathOf(value.id), undefined, "DELETE")).then(result => { if (result) onDeleted(); }))} />
  </Screen><FloatingNotice message={message} onDismiss={() => setMessage("")} /></View>;
}

function AIConnections({ api, visible, onMessage }: { api: Api; visible: boolean; onMessage: (message: string) => void }) {
  const work = useWorkspaceRequests(api);
  const [open, setOpen] = useState(false), [loaded, setLoaded] = useState(false), [setup, setSetup] = useState(false);
  const [connections, setConnections] = useState<AIConnection[]>([]);
  const endpoint = `${api.base}/v1/personal-ai/mcp`;
  async function refresh() {
    const value = await work.read(async request => parseAIConnections(await requestAI(request, `${base}/oauth/connections`)));
    if (value) { setConnections(value); setLoaded(true); }
  }
  useEffect(() => { if (visible && open && work.foreground) void refresh(); else work.cancelRead(); }, [api, visible, open, work.foreground]);
  return <Card>
    <Txt style={strong}>Your AI connections</Txt>
    <Txt muted>Copy and paste works without a connection. Compatible AI clients can read your requests and publish briefs through MCP.</Txt>
    <Button label={setup ? "Hide MCP setup" : "MCP setup"} secondary onPress={() => setSetup(!setup)} />
    {setup ? <>
      <Txt>Choose Streamable HTTP in your AI client's MCP settings, then enter this server URL.</Txt><Txt selectable>{endpoint}</Txt>
      <Txt muted>Long-press the URL to copy it. Your server must have OAuth and a registered client configured. Sign in through the AI client's browser flow and review the requested permissions.</Txt>
      {!api.base.startsWith("https://") ? <Notice>Remote AI clients require a reachable HTTPS server. This HTTP test address can still create requests and import pasted briefs.</Notice> : null}
      <Txt muted>MCP does not automatically read other chats. Only the brief your AI prepares is published here.</Txt>
    </> : null}
    <Button label={open ? "Hide connected clients" : "Manage connected clients"} secondary onPress={() => setOpen(!open)} />
    {open ? <>
      <Button label="Refresh AI connections" secondary compact disabled={work.busy || work.loading} onPress={() => void refresh()} />
      {work.loading ? <Loading label="Checking AI connections…" /> : null}
      {work.error ? <Notice tone="error">{work.error}</Notice> : null}
      {loaded && !connections.length ? <Txt muted>No active AI connections.</Txt> : null}
      {connections.map(connection => <View key={connection.id} style={{ gap: 8 }}>
        <Txt style={strong}>{connection.client_id}</Txt>
        <Txt muted>Expires {dateLabel(connection.expires_at * 1000)}</Txt>
        <Txt muted>{connection.scope.split(" ").map(scope => scope === "context:read" ? "Read requests" : scope === "context:publish" ? "Publish briefs" : scope).join(" · ")}</Txt>
        <Button label={`Disconnect ${connection.client_id}`} secondary danger disabled={work.busy} onPress={() => confirm("Disconnect this AI client?", "Future reads and publications will stop. Existing briefs stay saved.", "Disconnect", () => void work.mutate(request => requestAI(request, `${base}/oauth/connections/${encodeURIComponent(connection.id)}`, undefined, "DELETE")).then(result => { if (result) { setConnections(old => old.filter(c => c.id !== connection.id)); onMessage("AI client disconnected."); } }))} />
      </View>)}
    </> : null}
  </Card>;
}
