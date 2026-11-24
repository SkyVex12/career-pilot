import "./feature-pages.css";
import { PersonalAI } from "./PersonalAI";
import { Ask, openAsk, type AskEntry } from "./Ask";
// Work: the companies the user works at (apart from the jobs they apply to), and per company a
// daily work log, its engineering context (documents kept as searchable sections and facts,
// with a box to test a question against them) and its facts.
// Standup and meeting calls answer from these (the Call screen picks the company).
import { useCallback, useEffect, useRef, useState } from "react";
import { api, rest, POLL_READ, type AskResult, type CompanyDocument, type DocumentDetail, type Employer, type MemoryItem, type Proposal, type WorkLog } from "./api";
import { ACCEPT, TEXT_ACCEPT, plural, useOnShow, when } from "./shared";
import { DataActivity, usePolling } from "./usePolling";
import { Icon } from "./ui/Icon";
import { KIND_LABELS, QUESTION_TYPE_LABELS, byKind, itemDetails, itemLine, localDay } from "./workday";
import {
  Badge,
  Banner,
  Button,
  Card,
  ConfirmDialog,
  Dialog,
  EmptyState,
  Field,
  IconButton,
  Input,
  List,
  ListRow,
  ListState,
  Menu,
  PageHeader,
  Select,
  SkeletonList,
  Spinner,
  Tabs,
  Textarea,
  required,
  useToast,
} from "./ui";

export function Work({ active = true, askEntry }: { active?: boolean; askEntry?: AskEntry | null }) {
  const [asking, setAsking] = useState(false);
  const [askOpened, setAskOpened] = useState(false);
  useEffect(() => { if (askEntry) { setAsking(true); setAskOpened(true); } }, [askEntry?.n]);
  const [employers, setEmployers] = useState<Employer[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<"add" | "edit" | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [tab, setTab] = useState("log");
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const list = (await rest.employers()).employers;
      setEmployers(list);
      setSelected((cur) => (cur && list.some((e) => e.id === cur) ? cur : (list.find((e) => e.current && e.status === "active") ?? list[0])?.id ?? null));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  useOnShow(active, load);

  const employer = employers?.find((e) => e.id === selected) ?? null;
  const archive = async (e: Employer) => {
    const to = e.status === "archived" ? "active" : "archived";
    try {
      await rest.updateEmployer(e.id, { status: to });
      await load();
      toast({
        tone: "success",
        text: to === "archived" ? `Archived ${e.name}.` : `${e.name} is active again.`,
        action: {
          label: "Undo",
          onClick: () => {
            rest.updateEmployer(e.id, { status: e.status }).then(load, (err) => toast({ tone: "error", text: String(err) }));
          },
        },
      });
    } catch (err) {
      toast({ tone: "error", text: String(err) });
    }
  };

  return (
    <div className="work-workspace feature-page">
      <PageHeader
        title="Work context"
        actions={
          <>
            <Button variant={asking ? "ghost" : "primary"} onClick={() => { setAsking(v => !v); setAskOpened(true); }}>{asking ? "Back to work context" : "Ask a question"}</Button>
            {!asking && !!employers?.length && (
              <Button variant="secondary" onClick={() => setForm("add")}>
                + Add company
              </Button>
            )}
            {!asking && employer && (
              <Menu
                label="Company actions"
                trigger="⋯"
                items={[
                  { label: "Edit company…", onSelect: () => setForm("edit") },
                  { label: employer.status === "archived" ? "Unarchive" : "Archive", hint: "Archived companies are not offered for calls", onSelect: () => archive(employer) },
                  "separator",
                  { label: "Delete all its data…", danger: true, onSelect: () => setDeleting(true) },
                ]}
              />
            )}
          </>
        }
      />
      {askOpened && <div hidden={!asking}><Ask active={active && asking} entry={askEntry} /></div>}
      <div hidden={asking}>
      {!employers && !error && <SkeletonList rows={3} label="Loading companies" />}
      {error && <Banner tone="danger" title={employers ? "Could not refresh your companies" : "Could not load your companies"} action={<Button size="sm" onClick={load}>Retry</Button>}>{error}</Banner>}
      {employers && <details className="work-personal-context feature-disclosure"><summary><span>Context from my AI</span><span className="work-personal-caption">Bring a private brief into your work</span></summary><PersonalAI /></details>}
      {!employers ? null : employers.length === 0 ? (
        <EmptyState
          icon={<Icon name="work" size={28} />}
          title="Add the company you work at"
          action={
            <Button variant="primary" onClick={() => setForm("add")}>
              Add company
            </Button>
          }
        >
          Standups and meetings answer from your work log and the company's engineering documents.
        </EmptyState>
      ) : (
        <>
          <div className="work-company">
            <Field label="Company" inline>
              <Select value={selected ?? ""} onChange={(e) => setSelected(e.target.value)}>
                {employers.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                    {e.status === "archived" ? " (archived)" : e.current ? "" : " (past)"}
                  </option>
                ))}
              </Select>
            </Field>
            {employer && <span className="ui-text-2 ui-truncate">{[employer.title, employer.team].filter(Boolean).join(" · ")}</span>}
            {employer?.status === "archived" && <Badge tone="neutral">Archived</Badge>}
          </div>
          {employer && (
            <Tabs
              key={employer.id}
              label="Company"
              value={tab}
              onChange={setTab}
              items={[
                { value: "log", label: "Work log", content: <DataActivity.Provider value={active && !asking && tab === "log"}><LogTab employer={employer} /></DataActivity.Provider> },
                { value: "material", label: "Documents", count: employer.pending_proposals ?? 0, content: <DataActivity.Provider value={active && !asking && tab === "material"}><MaterialTab employer={employer} onDecided={load} /></DataActivity.Provider> },
                { value: "facts", label: "Saved facts", content: <DataActivity.Provider value={active && !asking && tab === "facts"}><FactsTab employer={employer} /></DataActivity.Provider> },
              ]}
            />
          )}
        </>
      )}
      </div>
      <EmployerForm
        open={form !== null}
        employer={form === "edit" ? employer : null}
        onOpenChange={(o) => !o && setForm(null)}
        onSaved={async (e) => {
          await load();
          setSelected(e.id);
        }}
      />
      {employer && (
        <ConfirmDialog
          open={deleting}
          onOpenChange={setDeleting}
          title={`Delete everything for ${employer.name}?`}
          typed={employer.name}
          confirmLabel="Delete all data"
          onConfirm={async () => {
            await rest.deleteEmployer(employer.id, employer.name);
            toast({ tone: "success", text: `Deleted ${employer.name} and its data.` });
            setSelected(null);
            await load();
          }}
        >
          Its work logs and attachments, documents, facts, and its standup and meeting calls with their transcripts. This cannot be undone.
        </ConfirmDialog>
      )}
    </div>
  );
}

/** Field checks for a company: a name, and an end that is not before the start. */
export function checkEmployer(f: { name: string; started: string; ended: string }): { name?: string; ended?: string } {
  const out: { name?: string; ended?: string } = {};
  const n = required(f.name, "the company's name");
  if (n) out.name = n;
  if (f.started && f.ended && f.ended < f.started) out.ended = "The end can't be before the start.";
  return out;
}

function EmployerForm({ open, employer, onOpenChange, onSaved }: { open: boolean; employer: Employer | null; onOpenChange: (o: boolean) => void; onSaved: (e: Employer) => void }) {
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [team, setTeam] = useState("");
  const [started, setStarted] = useState("");
  const [ended, setEnded] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    setName(employer?.name ?? "");
    setTitle(employer?.title ?? "");
    setTeam(employer?.team ?? "");
    setStarted(employer?.started_on ?? "");
    setEnded(employer?.ended_on ?? "");
    setTouched(false);
    setError(null);
  }, [open, employer]);
  const checks = checkEmployer({ name, started, ended });
  const save = async () => {
    setTouched(true);
    if (Object.keys(checks).length) return;
    setBusy(true);
    setError(null);
    const fields = { name: name.trim(), title: title.trim(), team: team.trim(), started_on: started || null, ended_on: ended || null };
    try {
      const out = employer ? await rest.updateEmployer(employer.id, fields) : await rest.createEmployer(fields);
      onOpenChange(false);
      onSaved(out);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={employer ? `Edit ${employer.name}` : "Add a company"}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={save}>
            {employer ? "Save" : "Add company"}
          </Button>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <Field label="Company" error={touched ? (checks.name ?? null) : null}>
          <Input value={name} onChange={(e) => setName(e.target.value)} onBlur={() => setTouched(true)} />
        </Field>
        <div className="form-2">
          <Field label="Your role" optional>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="Team" optional>
            <Input value={team} onChange={(e) => setTeam(e.target.value)} />
          </Field>
        </div>
        <div className="form-2">
          <Field label="Started" optional>
            <Input type="date" value={started} onChange={(e) => setStarted(e.target.value)} />
          </Field>
          <Field label="Ended" optional hint="Empty while you work there." error={touched ? (checks.ended ?? null) : null}>
            <Input type="date" value={ended} onChange={(e) => setEnded(e.target.value)} />
          </Field>
        </div>
        {error && <Banner tone="danger">{error}</Banner>}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

// --- Log ---------------------------------------------------------------------------------------

function shiftDay(day: string, by: number): string {
  const d = new Date(`${day}T12:00:00`);
  d.setDate(d.getDate() + by);
  return localDay(d);
}

function LogTab({ employer }: { employer: Employer }) {
  const [day, setDay] = useState(localDay());
  const [entries, setEntries] = useState<WorkLog[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [draftRevision, setDraftRevision] = useState<number | undefined>(undefined);
  const [conflict, setConflict] = useState(false);
  const [latestLog, setLatestLog] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      setEntries((await rest.logs(employer.id)).logs);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [employer.id]);
  useEffect(() => {
    load();
  }, [load]);
  useOnShow(true, load);
  const entry = entries?.find((e) => e.day === day) ?? null;
  useEffect(() => {
    if (!dirty) { setText(entry?.text ?? ""); setDraftRevision(entry ? entry.revision : -1); }
  }, [entry?.text, entry?.revision, day]); // eslint-disable-line react-hooks/exhaustive-deps
  usePolling(async () => {
    try {
      const status = await rest.workStatus(employer.id);
      if (entries?.some(entry => status.work_logs.find(item => item.id === entry.id)?.status !== entry.status)) {
        setEntries((await rest.logs(employer.id, undefined, POLL_READ)).logs);
      }
      setError(null);
    } catch (e) { setError(String(e)); return false; }
  }, 3000, entries?.some((e) => e.status === "extracting") ?? false, { key: employer.id });

  const go = (d: string) => {
    setDay(d);
    setDirty(false);
    setConflict(false); setLatestLog(null); setDraftRevision(-1);
  };
  const save = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      const saved = await rest.putLog(employer.id, day, text, draftRevision);
      setDraftRevision(saved.revision); setConflict(false); setLatestLog(null);
      setDirty(false);
      await load();
      toast({ tone: "success", text: "Saved. Its facts are being read." });
    } catch (e) {
      if (String(e).includes("HTTP 409")) setConflict(true);
      toast({ tone: "error", text: String(e) });
    } finally {
      setBusy(false);
    }
  };
  const attach = async (f: File) => {
    setBusy(true);
    try {
      if (dirty || !entry) {
        const saved = await rest.putLog(employer.id, day, text, draftRevision);
        setDraftRevision(saved.revision);
      }
      setDirty(false);
      await api.uploadCompanyFile(`/employers/${employer.id}/logs/${day}/attachments`, f);
      await load();
    } catch (e) {
      if (String(e).includes("HTTP 409")) setConflict(true);
      toast({ tone: "error", text: `${f.name}: ${e}` });
    } finally {
      setBusy(false);
    }
  };

  const reloadConflict = async () => {
    setBusy(true);
    try {
      const latest = await rest.log(employer.id, day, { refresh: true });
      setEntries(current => [latest, ...(current ?? []).filter(item => item.id !== latest.id)]);
      setDraftRevision(latest.revision); setLatestLog(latest.text); setConflict(false);
    } catch (e) { toast({ tone: "error", text: String(e) }); }
    finally { setBusy(false); }
  };

  return (
    <div className="work-log feature-work-log">
      <div className="work-log-main">
        <div className="work-day">
          <IconButton label="Previous day" icon="‹" variant="subtle" onClick={() => go(shiftDay(day, -1))} />
          <Input type="date" aria-label="Day" value={day} onChange={(e) => go(e.target.value || localDay())} />
          <IconButton label="Next day" icon="›" variant="subtle" onClick={() => go(shiftDay(day, 1))} />
          {day !== localDay() && (
            <Button size="sm" variant="ghost" onClick={() => go(localDay())}>
              Today
            </Button>
          )}
          <span className="ui-spacer" />
          {dirty ? <span className="work-unsaved" role="status">Unsaved changes</span> : entry && <LogStatus log={entry} />}
        </div>
        <div className="form-actions work-log-toolbar">
          <input
            ref={file}
            type="file"
            accept={ACCEPT}
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) attach(f);
            }}
          />
          <Button disabled={busy || !text.trim()} onClick={() => file.current?.click()} title={!text.trim() ? "Write the day's log first" : undefined}>
            Attach a file…
          </Button>
          {entry && (
            <Button variant="danger-ghost" onClick={() => setDeleting(true)}>
              Delete this day…
            </Button>
          )}
          <span className="ui-spacer" />
          <Button variant="primary" disabled={conflict || !text.trim() || (!dirty && entry?.status !== "failed")} loading={busy} onClick={save}>
            Save
          </Button>
        </div>
        <Field label="What you worked on" hint="Tasks, decisions, blockers and next steps." hintMode="tooltip">
          <Textarea
            disabled={busy}
            rows={10}
            placeholder="What moved forward today? Add decisions, blockers, and what comes next."
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setDirty(true);
            }}
          />
        </Field>
        {conflict && <Banner tone="warning" title="This work log changed" action={<Button size="sm" loading={busy} onClick={reloadConflict}>Load latest log</Button>}>Your draft is preserved. Load the saved version before saving again.</Banner>}
        {latestLog !== null && <Banner tone="warning" title="Latest saved log"><p>Your draft is preserved above. Review the saved version before saving again.</p><div className="feature-prose">{latestLog || "No saved text."}</div></Banner>}
        {entry && entry.attachments.length > 0 && (
          <ul className="work-files" aria-label="Attachments">
            {entry.attachments.map((a) => (
              <li key={a.id}>
                <span aria-hidden="true">📎</span> <span className="ui-truncate">{a.name}</span> <span className="ui-muted">{Math.ceil(a.size_bytes / 1024)} KB</span>
                <RemoveAttachment name={a.name} onRemove={() => rest.deleteCompanyDocument(employer.id, a.id).then(load)} />
              </li>
            ))}
          </ul>
        )}
        {entry?.items && entry.items.length > 0 && (
          <Card title="From this day's log" inset>
            <p className="ui-field-hint">Your log is kept as it is; fix anything that came out wrong.</p>
            <Facts items={entry.items} onChanged={load} />
          </Card>
        )}
      </div>
      <aside className="work-log-past" aria-label="Past entries">
        <h2 className="ui-section-title">Past entries</h2>
        <ListState items={entries} error={error} onRetry={load} loadingLabel="the work log" empty={<p className="work-past-empty">Your saved daily logs will appear here. Start with today’s progress.</p>}>
          {(list) => (
            <List label="Past entries">
              {list.map((e) => (
                <ListRow key={e.id} selected={e.day === day} onClick={() => go(e.day)} title={e.day} meta={e.text.split("\n")[0]} trailing={<LogStatus log={e} short />} />
              ))}
            </List>
          )}
        </ListState>
      </aside>
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete the log for ${day}?`}
        confirmLabel="Delete log"
        onConfirm={async () => {
          await rest.deleteLog(employer.id, day);
          setText("");
          setDirty(false);
          await load();
        }}
      >
        The facts that came from it are deleted too.
      </ConfirmDialog>
    </div>
  );
}

function RemoveAttachment({ name, onRemove }: { name: string; onRemove: () => Promise<unknown> }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <IconButton label={`Remove ${name}`} icon="✕" size="sm" onClick={() => setOpen(true)} />
      <ConfirmDialog open={open} onOpenChange={setOpen} title={`Remove ${name}?`} confirmLabel="Remove" onConfirm={onRemove}>
        The file and the facts read from it are deleted.
      </ConfirmDialog>
    </>
  );
}

function LogStatus({ log, short }: { log: WorkLog; short?: boolean }) {
  switch (log.status) {
    case "extracting":
      return <Badge tone="warning">Reading</Badge>;
    case "extracted":
      return <Badge tone="success">{plural(log.items?.length ?? 0, "fact")}</Badge>;
    case "failed":
      return <Badge tone="danger">{short ? "Not read" : "Could not read it; save again"}</Badge>;
    default:
      return <Badge tone="neutral">Saved</Badge>;
  }
}

// --- Material ------------------------------------------------------------------------------------

function MaterialTab({ employer, onDecided }: { employer: Employer; onDecided: () => void }) {
  const [docs, setDocs] = useState<CompanyDocument[] | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [writing, setWriting] = useState(false);
  const [confirm, setConfirm] = useState<{ title: string; body: string; label: string; run: () => Promise<unknown> } | null>(null);
  const [detail, setDetail] = useState<CompanyDocument | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const [d, p] = await Promise.all([rest.companyDocuments(employer.id), rest.proposals(employer.id)]);
      setDocs(d.documents);
      setProposals(p.proposals);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [employer.id]);
  useEffect(() => {
    load();
  }, [load]);
  useOnShow(true, load);
  usePolling(async () => {
    try {
      const status = await rest.workStatus(employer.id);
      if (docs?.some(doc => status.documents.find(item => item.id === doc.id)?.status !== doc.status)) {
        const [d, p] = await Promise.all([rest.companyDocuments(employer.id, POLL_READ), rest.proposals(employer.id, POLL_READ)]);
        setDocs(d.documents); setProposals(p.proposals);
      }
      setError(null);
    } catch (e) { setError(String(e)); return false; }
  }, 3000, docs?.some((d) => d.status === "ingesting") ?? false, { key: employer.id });

  const upload = async (f: File): Promise<boolean> => {
    setBusy(true);
    try {
      await api.uploadCompanyFile(`/employers/${employer.id}/documents`, f);
      await load();
      toast({ tone: "success", text: `Uploaded ${f.name}. It is being read; answers can use it in a minute.` });
      return true;
    } catch (e) {
      toast({ tone: "error", text: `${f.name}: ${e}` });
      return false;
    } finally {
      setBusy(false);
    }
  };
  const decide = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await load();
      onDecided();
    } catch (e) {
      toast({ tone: "error", text: String(e) });
    }
  };
  const pendingDocs = (docs ?? []).filter((d) => proposals.some((p) => p.company_document_id === d.id));

  return (
    <div className="ui-stack">
      <div className="form-actions material-add">
        <input
          ref={file}
          type="file"
          accept={TEXT_ACCEPT}
          multiple
          hidden
          onChange={async (e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            // One at a time: each is sealed and stored before the next starts.
            for (const f of files) await upload(f);
          }}
        />
        <Button onClick={() => file.current?.click()} loading={busy}>
          Upload files…
        </Button>
        <Button onClick={() => setWriting(true)}>Write or paste text…</Button>
        <span className="ui-field-hint">
          Architecture and design documents, decision records, READMEs, diagrams as text, notes: Markdown or text, several at once. A file with the name of an earlier one replaces it.
        </span>
      </div>

      <TestQuestion employer={employer} ready={(docs ?? []).some((d) => (d.sections ?? 0) > 0)} />

      {pendingDocs.length > 0 && (
        <section className="ui-section" aria-labelledby="review-title">
          <h2 className="ui-section-title" id="review-title">
            To review ({proposals.length})
          </h2>
          <div className="ui-stack">
            {pendingDocs.map((d) => {
              const mine = proposals.filter((p) => p.company_document_id === d.id);
              return (
                <Card
                  key={d.id}
                  title={<span className="ui-truncate">{d.name}</span>}
                  actions={
                    <>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() =>
                          setConfirm({
                            title: `Reject all ${plural(mine.length, "fact")} from ${d.name}?`,
                            body: "None of them is kept. The document stays.",
                            label: "Reject all",
                            run: () => decide(() => rest.decideDocument(d.id, "reject")),
                          })
                        }
                      >
                        Reject all
                      </Button>
                      <Button size="sm" variant="primary" onClick={() => decide(() => rest.decideDocument(d.id, "approve"))}>
                        Approve all {mine.length}
                      </Button>
                    </>
                  }
                >
                  <ul className="proposals">
                    {mine.map((p) => (
                      <ProposalRow key={p.id} proposal={p} decide={decide} />
                    ))}
                  </ul>
                </Card>
              );
            })}
          </div>
        </section>
      )}

      <section className="ui-section" aria-labelledby="docs-title">
        <h2 className="ui-section-title" id="docs-title">
          Documents
        </h2>
        <ListState items={docs} error={error} onRetry={load} loadingLabel="documents" empty={<EmptyState icon={<Icon name="document" size={28} />} title="No documents yet">Upload or paste the company's engineering documents, so standups and meetings can answer how things work and why.</EmptyState>}>
          {(list) => (
            <List label="Documents">
              {list.map((d) => (
                <ListRow
                  key={d.id}
                  title={d.name}
                  fullTitle={d.name}
                  meta={documentMeta(d)}
                  trailing={
                    d.status === "ingesting" ? (
                      <Badge tone="warning">Reading</Badge>
                    ) : d.status === "failed" ? (
                      <Badge tone="danger">Could not read it</Badge>
                    ) : (d.sections ?? 0) === 0 ? (
                      <Badge tone="neutral">Waiting to be read</Badge>
                    ) : (
                      <Badge tone="success">
                        {plural(d.sections ?? 0, "section")}, {plural(d.facts ?? 0, "fact")}
                      </Badge>
                    )
                  }
                  actions={
                    <Menu
                      label={`Actions for ${d.name}`}
                      trigger="⋯"
                      items={[
                        { label: "What it gave…", onSelect: () => setDetail(d) },
                        {
                          label: "Delete…",
                          danger: true,
                          onSelect: () =>
                            setConfirm({
                              title: `Delete ${d.name}?`,
                              body: "The document, its sections and the facts taken from it are deleted. Facts you edited stay.",
                              label: "Delete document",
                              run: () => decide(() => rest.deleteCompanyDocument(employer.id, d.id)),
                            }),
                        },
                      ]}
                    />
                  }
                />
              ))}
            </List>
          )}
        </ListState>
      </section>

      <WriteText open={writing} onOpenChange={setWriting} upload={upload} />
      <DocumentGave employer={employer} document={detail} onClose={() => setDetail(null)} />
      <ConfirmDialog open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)} title={confirm?.title ?? ""} confirmLabel={confirm?.label ?? "OK"} onConfirm={() => confirm?.run()}>
        {confirm?.body}
      </ConfirmDialog>
    </div>
  );
}

/// A file's second line: when it came, what it replaces, and why it could not be read.
function documentMeta(d: CompanyDocument): string | undefined {
  const parts = [d.created_at ? when(d.created_at) : "", d.replaces ? "replaces the earlier upload once read" : "", d.status === "failed" || d.error ? (d.error ?? "") : ""];
  return parts.filter(Boolean).join(" · ") || undefined;
}

/// What one file gave: the headings of its sections and the facts taken from it, each with
/// what its kind carries (a decision's reason and what was rejected, a flow's steps).
function DocumentGave({ employer, document, onClose }: { employer: Employer; document: CompanyDocument | null; onClose: () => void }) {
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const id = document?.id;
  useEffect(() => {
    setDetail(null);
    setError(null);
    if (!id) return;
    let live = true;
    rest
      .companyDocument(employer.id, id)
      .then((d) => live && setDetail(d))
      .catch((e) => live && setError(String(e)));
    return () => {
      live = false;
    };
  }, [employer.id, id]);
  return (
    <Dialog
      open={document !== null}
      onOpenChange={(o) => !o && onClose()}
      size="lg"
      title={document ? `What ${document.name} gave` : ""}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {error && <Banner tone="danger">{error}</Banner>}
      {!detail && !error && <Spinner label="Loading what the file gave" />}
      {detail && (
        <div className="ui-stack doc-gave">
          {detail.error && <Banner tone="warning">{detail.error}</Banner>}
          <section aria-label="Sections">
            <h3 className="ui-section-title">Sections searched during a call ({detail.sections})</h3>
            {detail.headings.length === 0 ? (
              <p className="ui-text-2">{detail.sections > 0 ? "The file has no headings; it is cut by paragraphs." : "None yet."}</p>
            ) : (
              <ul className="doc-headings">
                {detail.headings.map((h) => (
                  <li key={h}>{h}</li>
                ))}
              </ul>
            )}
          </section>
          {detail.facts.length === 0 && <p className="ui-text-2">No facts yet. They appear once the file is read.</p>}
          {byKind(detail.facts).map(([kind, list]) => (
            <section key={kind} aria-label={KIND_LABELS[kind]}>
              <h3 className="ui-section-title">
                {KIND_LABELS[kind]} ({list.length})
              </h3>
              <ul className="doc-facts">
                {list.map((item) => (
                  <li key={item.id}>
                    <span>{itemLine(item)}</span>
                    {itemDetails(item).map((line) => (
                      <span key={line} className="ui-text-2">
                        {line}
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
            </section>
          ))}
          <p className="ui-field-hint">Edit or remove a fact under Facts.</p>
        </div>
      )}
    </Dialog>
  );
}

const ASK_POLL_MS = 1000;
const ASK_TIMEOUT_MS = 90_000;

/// "Test a question": ask what a teammate might, and see the answer a standup would show,
/// the type it gets and where each fact came from. A worker answers; the box polls for it.
function TestQuestion({ employer, ready }: { employer: Employer; ready: boolean }) {
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [result, setResult] = useState<AskResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingAsk, setPendingAsk] = useState<string | null>(null);
  const run = useRef(0);
  useEffect(() => () => void (run.current += 1), []);
  usePolling(async () => {
    if (!pendingAsk) return;
    const mine = run.current;
    try {
      const got = await rest.askedKnowledge(employer.id, pendingAsk);
      if (mine !== run.current) return;
      setError(null);
      if (got.status !== "pending") { setResult(got); setPendingAsk(null); setAsking(false); }
    } catch (e) { if (mine === run.current) setError(String(e)); return false; }
  }, ASK_POLL_MS, !!pendingAsk, { key: pendingAsk ?? "", timeoutMs: ASK_TIMEOUT_MS, onTimeout: () => {
    setPendingAsk(null); setAsking(false); setError("No answer yet. Automatic checks paused; try again when the server is available.");
  } });

  const ask = async () => {
    const text = question.trim();
    if (text.length < 3 || asking) return;
    const mine = (run.current += 1);
    setAsking(true);
    setResult(null);
    setError(null);
    try {
      const started = await rest.askKnowledge(employer.id, text);
      if (run.current !== mine) return;
      if (started.status === "pending") setPendingAsk(started.ask_id);
      else { setResult(started); setAsking(false); }
    } catch (e) {
      if (run.current === mine) { setError(String(e)); setAsking(false); }
    }
  };

  return (
    <section className="ui-section work-company-question" aria-labelledby="ask-title">
      <div className="work-company-question-header">
        <h2 className="ui-section-title" id="ask-title">
          Ask about this company
        </h2>
        <Button size="sm" variant="secondary" onClick={() => openAsk({ kind: "company", id: employer.id })}>Open Ask</Button>
      </div>
      <form
        className="ask-form"
        onSubmit={(e) => {
          e.preventDefault();
          ask();
        }}
      >
        <Input aria-label="A question a teammate might ask" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="e.g. Why did we choose the queue over a state machine?" maxLength={500} />
        <Button type="submit" variant="primary" loading={asking} disabled={question.trim().length < 3}>
          Ask
        </Button>
      </form>
      {!ready && !result && !asking && <p className="ui-field-hint">Upload a document first; the answer comes from what it holds and from your work log.</p>}
      {asking && <Spinner size="sm" label="Answering as a standup would" />}
      {error && <Banner tone="danger">{error}</Banner>}
      {result?.status === "failed" && <Banner tone="danger">{result.error ?? "The question could not be answered."}</Banner>}
      {result?.status === "done" && (
        <div className="ask-result" aria-label="Answer">
          <Badge tone="neutral" icon={false}>
            {QUESTION_TYPE_LABELS[result.type ?? ""] ?? result.type}
          </Badge>
          <p className="ask-answer">{result.answer}</p>
          <h3 className="ui-section-title">Sources</h3>
          {(result.sources ?? []).length === 0 ? (
            <p className="ui-text-2">None cited: the answer is general knowledge, or says what it will confirm.</p>
          ) : (
            <ul className="doc-headings">
              {(result.sources ?? []).map((s) => (
                <li key={`${s.kind}:${s.label}`}>
                  {s.kind === "section" ? "Document: " : "Fact: "}
                  {s.label}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function WriteText({ open, onOpenChange, upload }: { open: boolean; onOpenChange: (o: boolean) => void; upload: (f: File) => Promise<boolean> }) {
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (open) {
      setName("");
      setText("");
      setTouched(false);
    }
  }, [open]);
  const nameError = touched ? required(name, "a name") : null;
  const textError = touched ? required(text, "the text") : null;
  // Pasted text goes through the same upload as a Markdown file, named after the user's title.
  const save = async () => {
    setTouched(true);
    if (!name.trim() || !text.trim()) return;
    const title = name.trim();
    const fileName = /\.(md|markdown|txt)$/i.test(title) ? title : `${title}.md`;
    setBusy(true);
    const ok = await upload(new File([text], fileName, { type: "text/markdown" }));
    setBusy(false);
    if (ok) onOpenChange(false);
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title="Write or paste text"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={save}>
            Save
          </Button>
        </>
      }
    >
      <Field label="Name" error={nameError}>
        <Input value={name} onChange={(e) => setName(e.target.value)} onBlur={() => setTouched(true)} placeholder="e.g. Payments service architecture" />
      </Field>
      <Field label="Text" error={textError}>
        <Textarea rows={12} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
    </Dialog>
  );
}

function ProposalRow({ proposal, decide }: { proposal: Proposal; decide: (fn: () => Promise<unknown>) => void }) {
  const [summary, setSummary] = useState(proposal.summary);
  const [editing, setEditing] = useState(false);
  return (
    <li className="proposal">
      <Badge tone="neutral" icon={false}>
        {KIND_LABELS[proposal.kind] ?? proposal.kind}
      </Badge>
      <div className="proposal-text">
        <strong>{proposal.title}</strong>
        {editing ? <Input aria-label={`Summary of ${proposal.title}`} value={summary} onChange={(e) => setSummary(e.target.value)} /> : <span className="ui-text-2">{summary}</span>}
        {proposal.evidence && <span className="ui-muted proposal-evidence">"{proposal.evidence}"</span>}
      </div>
      <div className="proposal-actions">
        <Button size="sm" variant="ghost" onClick={() => setEditing((v) => !v)} aria-label={`${editing ? "Done editing" : "Edit"} ${proposal.title}`}>
          {editing ? "Done" : "Edit"}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => decide(() => rest.rejectProposal(proposal.id))} aria-label={`Reject ${proposal.title}`}>
          Reject
        </Button>
        <Button
          size="sm"
          onClick={() => decide(() => rest.approveProposal(proposal.id, summary !== proposal.summary ? { summary } : undefined))}
          aria-label={`Approve ${proposal.title}`}
        >
          Approve
        </Button>
      </div>
    </li>
  );
}

// --- Facts -------------------------------------------------------------------------------------

function FactsTab({ employer }: { employer: Employer }) {
  const [items, setItems] = useState<MemoryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setItems((await rest.memory(employer.id)).memory);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [employer.id]);
  useEffect(() => {
    load();
  }, [load]);
  useOnShow(true, load);
  return (
    <div className="ui-stack">
      <p className="ui-field-hint">What standups and meetings at {employer.name} can draw on. Work logs add to it; documents add what you approve.</p>
      <ListState items={items} error={error} onRetry={load} loadingLabel="facts" empty={<EmptyState icon={<Icon name="layers" size={28} />} title="No facts yet">Write a work log or upload a document.</EmptyState>}>
        {(list) => <Facts items={list} onChanged={load} />}
      </ListState>
    </div>
  );
}

function Facts({ items, onChanged }: { items: MemoryItem[]; onChanged: () => void }) {
  const [editing, setEditing] = useState<MemoryItem | null>(null);
  const [removing, setRemoving] = useState<MemoryItem | null>(null);
  return (
    <div className="ui-stack">
      {byKind(items).map(([kind, list]) => (
        <section key={kind} aria-label={KIND_LABELS[kind]}>
          <h3 className="ui-section-title">{KIND_LABELS[kind]}</h3>
          <List label={KIND_LABELS[kind]}>
            {list.map((i) => (
              <ListRow
                key={i.id}
                title={itemLine(i)}
                fullTitle={itemLine(i)}
                meta={[i.day, i.edited ? "edited" : null].filter(Boolean).join(" · ") || undefined}
                actions={
                  <Menu
                    label={`Actions for ${i.title}`}
                    trigger="⋯"
                    items={[
                      { label: "Edit…", onSelect: () => setEditing(i) },
                      { label: "Remove…", danger: true, onSelect: () => setRemoving(i) },
                    ]}
                  />
                }
              />
            ))}
          </List>
        </section>
      ))}
      <EditFact item={editing} onClose={() => setEditing(null)} onDone={onChanged} />
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Remove "${removing?.title ?? ""}"?`}
        confirmLabel="Remove fact"
        onConfirm={async () => {
          if (removing) await rest.archiveMemory(removing.id);
          onChanged();
        }}
      >
        Standups and meetings stop using it.
      </ConfirmDialog>
    </div>
  );
}

function EditFact({ item, onClose, onDone }: { item: MemoryItem | null; onClose: () => void; onDone: () => void }) {
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setTitle(item?.title ?? "");
    setSummary(item?.summary ?? "");
    setError(null);
  }, [item]);
  const save = async () => {
    if (!item) return;
    if (!title.trim()) {
      setError("Enter a title.");
      return;
    }
    setBusy(true);
    try {
      await rest.updateMemory(item.id, { title: title.trim(), summary: summary.trim() });
      onClose();
      onDone();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={item !== null}
      onOpenChange={(o) => !o && onClose()}
      title="Edit fact"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={save}>
            Save
          </Button>
        </>
      }
    >
      <Field label="Title" error={error}>
        <Input value={title} onChange={(e) => setTitle(e.target.value)} />
      </Field>
      <Field label="Summary" optional>
        <Textarea rows={3} value={summary} onChange={(e) => setSummary(e.target.value)} />
      </Field>
    </Dialog>
  );
}
