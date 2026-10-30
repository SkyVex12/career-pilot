// Work: the companies the user works at (apart from the jobs they apply to), and per company a
// daily work log, its material (documents whose facts wait for approval) and its facts.
// Standup and meeting calls answer from these (the Call screen picks the company).
import { useCallback, useEffect, useRef, useState } from "react";
import { api, rest, type CompanyDocument, type Employer, type MemoryItem, type Proposal, type WorkLog } from "./api";
import { ACCEPT, plural, useOnShow, when } from "./shared";
import { usePolling } from "./usePolling";
import { KIND_LABELS, byKind, itemLine, localDay } from "./workday";
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
  Tabs,
  Textarea,
  required,
  useToast,
} from "./ui";

export function Work({ active = true }: { active?: boolean }) {
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

  if (error && !employers) {
    return (
      <>
        <PageHeader title="Work" />
        <Banner tone="danger" title="Could not load your companies" action={<Button size="sm" onClick={load}>Retry</Button>}>
          {error}
        </Banner>
      </>
    );
  }
  if (!employers) {
    return (
      <>
        <PageHeader title="Work" />
        <SkeletonList rows={3} label="Loading companies" />
      </>
    );
  }
  return (
    <>
      <PageHeader
        title="Work"
        actions={
          <>
            {employers.length > 0 && (
              <Button variant="secondary" onClick={() => setForm("add")}>
                + Add company
              </Button>
            )}
            {employer && (
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
        meta={employers.length ? "Companies you work at, apart from the jobs you apply to. Standups and meetings answer from the one picked on the Call screen." : undefined}
      />
      {employers.length === 0 ? (
        <EmptyState
          icon="▦"
          title="Add the company you work at"
          action={
            <Button variant="primary" onClick={() => setForm("add")}>
              Add company
            </Button>
          }
        >
          Standups and meetings answer from your work log and the company's material.
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
                { value: "log", label: "Log", content: <LogTab employer={employer} /> },
                { value: "material", label: "Material", count: employer.pending_proposals ?? 0, content: <MaterialTab employer={employer} onDecided={load} /> },
                { value: "facts", label: "Facts", content: <FactsTab employer={employer} /> },
              ]}
            />
          )}
        </>
      )}
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
    </>
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
  const entry = entries?.find((e) => e.day === day) ?? null;
  useEffect(() => {
    if (!dirty) setText(entry?.text ?? "");
  }, [entry?.text, day]); // eslint-disable-line react-hooks/exhaustive-deps
  usePolling(load, 3000, entries?.some((e) => e.status === "extracting") ?? false);

  const go = (d: string) => {
    setDay(d);
    setDirty(false);
  };
  const save = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await rest.putLog(employer.id, day, text);
      setDirty(false);
      await load();
      toast({ tone: "success", text: "Saved. Its facts are being read." });
    } catch (e) {
      toast({ tone: "error", text: String(e) });
    } finally {
      setBusy(false);
    }
  };
  const attach = async (f: File) => {
    setBusy(true);
    try {
      if (dirty || !entry) await rest.putLog(employer.id, day, text);
      setDirty(false);
      await api.uploadCompanyFile(`/employers/${employer.id}/logs/${day}/attachments`, f);
      await load();
    } catch (e) {
      toast({ tone: "error", text: `${f.name}: ${e}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="work-log">
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
          {entry && <LogStatus log={entry} />}
        </div>
        <Field label="What you worked on" hint="Tasks, tickets and PRs, decisions, blockers, plans for tomorrow. Paste from your notes, Jira or Slack as it is.">
          <Textarea
            rows={10}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setDirty(true);
            }}
          />
        </Field>
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
        <div className="form-actions">
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
          <Button variant="primary" disabled={!text.trim() || !dirty} loading={busy} onClick={save}>
            Save
          </Button>
        </div>
        {entry?.items && entry.items.length > 0 && (
          <Card title="From this day's log" inset>
            <p className="ui-field-hint">Your log is kept as it is; fix anything that came out wrong.</p>
            <Facts items={entry.items} onChanged={load} />
          </Card>
        )}
      </div>
      <aside className="work-log-past" aria-label="Past entries">
        <h2 className="ui-section-title">Past entries</h2>
        <ListState items={entries} error={error} onRetry={load} loadingLabel="the work log" empty={<p className="ui-field-hint">No entries yet.</p>}>
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
  usePolling(load, 3000, docs?.some((d) => d.status === "ingesting") ?? false);

  const upload = async (f: File): Promise<boolean> => {
    setBusy(true);
    try {
      await api.uploadCompanyFile(`/employers/${employer.id}/documents`, f);
      await load();
      toast({ tone: "success", text: `Uploaded ${f.name}. Its facts appear here to review once read.` });
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
          accept={ACCEPT}
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) upload(f);
          }}
        />
        <Button onClick={() => file.current?.click()} loading={busy}>
          Upload a file…
        </Button>
        <Button onClick={() => setWriting(true)}>Write or paste text…</Button>
        <span className="ui-field-hint">Architecture notes, wiki pages, meeting notes, specs: PDF, Markdown or text. You review its facts before they are kept.</span>
      </div>

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
        <ListState items={docs} error={error} onRetry={load} loadingLabel="documents" empty={<EmptyState title="No documents yet">Upload or paste the company's notes, so meetings can answer from them.</EmptyState>}>
          {(list) => (
            <List label="Documents">
              {list.map((d) => (
                <ListRow
                  key={d.id}
                  title={d.name}
                  fullTitle={d.name}
                  meta={d.created_at ? when(d.created_at) : undefined}
                  trailing={
                    d.status === "ingesting" ? (
                      <Badge tone="warning">Reading</Badge>
                    ) : d.status === "failed" ? (
                      <Badge tone="danger">Could not read it</Badge>
                    ) : (
                      <Badge tone="success">{plural(d.approved_proposals ?? 0, "fact")} kept</Badge>
                    )
                  }
                  actions={
                    <Menu
                      label={`Actions for ${d.name}`}
                      trigger="⋯"
                      items={[
                        {
                          label: "Delete…",
                          danger: true,
                          onSelect: () =>
                            setConfirm({
                              title: `Delete ${d.name}?`,
                              body: "The document and the facts kept from it are deleted.",
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
      <ConfirmDialog open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)} title={confirm?.title ?? ""} confirmLabel={confirm?.label ?? "OK"} onConfirm={() => confirm?.run()}>
        {confirm?.body}
      </ConfirmDialog>
    </div>
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
  return (
    <div className="ui-stack">
      <p className="ui-field-hint">What standups and meetings at {employer.name} can draw on. Work logs add to it; documents add what you approve.</p>
      <ListState items={items} error={error} onRetry={load} loadingLabel="facts" empty={<EmptyState title="No facts yet">Write a work log or upload a document.</EmptyState>}>
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
