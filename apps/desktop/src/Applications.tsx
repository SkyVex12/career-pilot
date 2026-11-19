// Applications: what the user applied to (company, role, stage, JD), each paired with a resume
// version (ARCHITECTURE.md, "Resume versions"); added by hand or pulled from Career-OS. The
// application workspace keeps each role, resume and interview context together.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, rest, type ApplicationDetail, type ApplicationFields, type ApplicationItem, type InterviewItem, type Round, type Stage } from "./api";
import { STAGES, isTextFile, pairingChoices, pairingValue, resumeStatusLabel } from "./pairing";
import { upcoming } from "./catalog";
import { BaseResumeUtility, RenameResume, statusTone } from "./Resumes";
import "./prep.css";
import { PullDialog, useCareerOs } from "./CareerOs";
import { ACCEPT, PackBadge, useOnShow, when } from "./shared";
import { usePolling } from "./usePolling";
import { Icon } from "./ui/Icon";
import {
  Badge,
  Banner,
  Button,
  ConfirmDialog,
  Dialog,
  EmptyState,
  Field,
  Input,
  List,
  ListRow,
  ListState,
  Menu,
  PageHeader,
  Select,
  Textarea,
  required,
  url,
  useToast,
} from "./ui";

export const ROUNDS: [Round, string][] = [
  ["phone_screen", "Phone screen"],
  ["technical", "Technical"],
  ["system_design", "System design"],
  ["final", "Final"],
];
export const roundLabel = (r: string) => ROUNDS.find(([v]) => v === r)?.[1] ?? r;

export interface Catalog {
  applications: ApplicationItem[] | null;
  interviews: InterviewItem[] | null;
  error: string | null;
  reload: () => Promise<void>;
  includeApplication: (application: ApplicationItem) => void;
}

/// Applications and interviews from the API, polled while a pack is still being built.
export function useCatalog(enabled = true): Catalog {
  const [applications, setApplications] = useState<ApplicationItem[] | null>(null);
  const [interviews, setInterviews] = useState<InterviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const revision = useRef(0);
  const reload = useCallback(async () => {
    const request = ++revision.current;
    try {
      const [a, i] = await Promise.all([rest.applications(), rest.interviews()]);
      if (request !== revision.current) return;
      setApplications(a.applications);
      setInterviews(i.interviews);
      setError(null);
    } catch (e) {
      if (request === revision.current) setError(String(e));
    }
  }, []);
  const includeApplication = useCallback((application: ApplicationItem) => {
    ++revision.current;
    setApplications(items => [application, ...(items ?? []).filter(item => item.id !== application.id)]);
  }, []);
  useEffect(() => {
    if (enabled) reload();
  }, [enabled, reload]);
  const building =
    (applications ?? []).some((a) => !a.pack || a.resume?.status === "extracting") || upcoming(interviews ?? []).some((i) => !i.pack);
  usePolling(reload, 4000, enabled && building);
  return { applications, interviews, error, reload, includeApplication };
}

/// `datetime-local` value one hour from now, rounded to 5 minutes.
function soon(): string {
  const d = new Date(Date.now() + 3600_000);
  d.setMinutes(Math.ceil(d.getMinutes() / 5) * 5, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Search every application by company or role, regardless of stage. */
export function filterApplications(list: ApplicationItem[], q: string): ApplicationItem[] {
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return list.filter((a) => words.every((w) => `${a.company} ${a.role}`.toLowerCase().includes(w)));
}

export function Applications({
  active = true,
  focus,
  baseResumeRequest = 0,
  onStartInterview,
}: {
  active?: boolean;
  /** An application to show, selected from call setup. */
  focus?: { id: string; n: number } | null;
  /** Open the shared resume editor from call setup or onboarding. */
  baseResumeRequest?: number;
  /** Start a call for an explicitly selected interview. */
  onStartInterview?: (interviewId: string) => void;
}) {
  const { applications, interviews, error, reload } = useCatalog();
  useOnShow(active, reload);
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [q, setQ] = useState("");
  const { conn } = useCareerOs(true);

  useEffect(() => { if (focus) setSelected(focus.id); }, [focus]);
  useEffect(() => {
    if (!selected && applications?.length && window.innerWidth >= 720) setSelected(applications[0].id);
  }, [applications]); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = useMemo(() => (applications ? filterApplications(applications, q) : null), [applications, q]);

  return (
    <div className="apps prep-workspace">
      <PageHeader title="Interview prep" actions={<>
        {conn?.connected && <Button onClick={() => setPulling(true)}>Pull from Career-OS…</Button>}
        <Button variant="primary" onClick={() => setAdding(true)}>+ Add application</Button>
      </>} />
      <div className={selected ? "apps-split prep-split has-selection" : "apps-split prep-split"}>
        <section className="apps-list prep-sidebar" aria-label="Applications">
          <h2 className="prep-sidebar-heading">Applications <span>{applications?.length ?? ""}</span></h2>
          <Input type="search" aria-label="Search company or role" placeholder="Search applications" value={q} onChange={(e) => setQ(e.target.value)} />
          <ListState items={shown} error={error} onRetry={reload} loadingLabel="applications" empty={applications?.length ?
            <EmptyState title="No applications match">Try another company or role.</EmptyState> :
            <EmptyState icon={<Icon name="briefcase" size={28} />} title="No applications yet" action={<Button onClick={() => setAdding(true)}>Add application</Button>} />
          }>
            {(list) => <List label="All applications">{list.map((a) => <ListRow
              key={a.id} selected={a.id === selected} onClick={() => setSelected(a.id)} title={a.company}
              fullTitle={`${a.company} · ${a.role}`}
              meta={<><span className="prep-application-role">{a.role}</span><span className="prep-application-resume"><Icon name="document" size={12} /><span>{a.resume?.label ?? "No resume attached"}</span></span></>}
              trailing={<PackDot pack={a.pack} />}
            />)}</List>}
          </ListState>
          <BaseResumeUtility active={active} request={baseResumeRequest} revision={applications?.map(a => a.resume?.label).join("|")} onChanged={reload} />
        </section>
        <section className="apps-detail prep-detail" aria-label="Application">
          {selected ? <ApplicationView key={selected} id={selected}
            stamp={listStamp(applications?.find((a) => a.id === selected))}
            interviews={(interviews ?? []).filter((i) => i.job_id === selected)}
            onStartInterview={onStartInterview} onBack={() => setSelected(null)} onChanged={reload}
            onDeleted={() => { setSelected(null); void reload(); }}
          /> : applications && applications.length > 0 ? <EmptyState icon={<Icon name="briefcase" size={28} />} title="Choose an application">Its resume and job description stay together here.</EmptyState> : null}
        </section>
      </div>
      <AddApplication open={adding} onOpenChange={setAdding} onAdded={(app) => { void reload(); setSelected(app.id); }} />
      <PullDialog open={pulling} onOpenChange={setPulling} onPulled={(app) => { void reload(); setSelected(app.id); }} />
    </div>
  );
}

/** The list's pack state: an icon with its words for screen readers and the tooltip. */
function PackDot({ pack }: { pack: ApplicationItem["pack"] }) {
  const [icon, text, tone] = !pack ? ["◌", "Pack building", "warning"] : pack.available ? ["●", "Pack ready", "success"] : ["○", "Pack expired", "neutral"];
  return (
    <span className={`pack-dot ui-tone-${tone}`} title={text}>
      <span aria-hidden="true">{icon}</span>
      <span className="ui-visually-hidden">{text}</span>
    </span>
  );
}

const EMPTY: ApplicationFields = { company: "", role: "", url: null, source_site: null, stage: "applied", note: null, jd_text: "" };

/** Field checks for an application, shown inline. */
export function checkApplication(f: ApplicationFields): Partial<Record<keyof ApplicationFields, string>> {
  const out: Partial<Record<keyof ApplicationFields, string>> = {};
  const c = required(f.company, "the company");
  if (c) out.company = c;
  const r = required(f.role, "the role");
  if (r) out.role = r;
  const j = required(f.jd_text, "the job description");
  if (j) out.jd_text = "Paste the job description. A PDF can be uploaded once the application is added.";
  const u = url(f.url ?? "");
  if (u) out.url = u;
  return out;
}

function useChecks(fields: ApplicationFields) {
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const errors = checkApplication(fields);
  const touch = (k: keyof ApplicationFields) => () => setTouched((t) => new Set(t).add(k));
  const shown = (k: keyof ApplicationFields) => (touched.has(k) ? (errors[k] ?? null) : null);
  const all = () => setTouched(new Set(Object.keys(EMPTY)));
  return { errors, touch, shown, all, reset: () => setTouched(new Set()), ok: Object.keys(errors).length === 0 };
}

export function AddApplication({ open, onOpenChange, onAdded }: { open: boolean; onOpenChange: (o: boolean) => void; onAdded: (a: ApplicationDetail) => void }) {
  const [fields, setFields] = useState<ApplicationFields>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileNote, setFileNote] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const checks = useChecks(fields);
  const toast = useToast();
  const set = <K extends keyof ApplicationFields>(k: K, v: ApplicationFields[K]) => setFields((f) => ({ ...f, [k]: v }));
  useEffect(() => {
    if (open) {
      setFields(EMPTY);
      checks.reset();
      setError(null);
      setFileNote(null);
    }
  }, [open]);

  const submit = async () => {
    if (busy) return;
    checks.all();
    if (!checks.ok) {
      document.querySelector<HTMLElement>(".ui-dialog [aria-invalid=true]")?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const app = await rest.createApplication({
        ...fields,
        url: fields.url || null,
        source_site: fields.source_site || null,
        note: fields.note || null,
        resume_text: null,
        resume_label: null,
      });
      toast({ tone: "success", text: `Added ${app.company}. Its interview pack is being built.` });
      onOpenChange(false);
      onAdded(app);
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
      title="Add application"
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            Add application
          </Button>
        </>
      }
    >
      <form
        className="application-create"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="form-2">
          <Field label="Company" error={checks.shown("company")}>
            <Input value={fields.company} onChange={(e) => set("company", e.target.value)} onBlur={checks.touch("company")} />
          </Field>
          <Field label="Role" error={checks.shown("role")}>
            <Input value={fields.role} onChange={(e) => set("role", e.target.value)} onBlur={checks.touch("role")} />
          </Field>
        </div>
        <Field label="Job description" error={checks.shown("jd_text")} hint={fileNote ?? "Paste it, or load a .txt or .md file."} hintMode="tooltip">
          <Textarea rows={4} value={fields.jd_text} onChange={(e) => set("jd_text", e.target.value)} onBlur={checks.touch("jd_text")} />
        </Field>
        <div className="ui-row-inline form-under">
          <input
            ref={file}
            type="file"
            accept={ACCEPT}
            hidden
            onChange={async (e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (!f) return;
              if (isTextFile(f.name)) {
                try {
                  set("jd_text", await f.text());
                  setFileNote(`Loaded ${f.name}.`);
                  setError(null);
                } catch (e) { setError(`Could not read ${f.name}: ${e}`); }
              } else setFileNote("A PDF can be uploaded once the application is added (in Job description). Paste the text for now.");
            }}
          />
          <Button size="sm" onClick={() => file.current?.click()}>
            Load from a file…
          </Button>
        </div>
        {error && <Banner tone="danger">{error}</Banner>}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

/** What the list knows of an application that its open view also shows: when the list reloads
 * (shown again, polled while a pack builds, after a pull) and this changes, the view reloads. */
export function listStamp(a: ApplicationItem | undefined): string {
  if (!a) return "";
  return JSON.stringify([a.updated_at, a.pack?.pack_id ?? null, a.pack?.available ?? null, a.resume?.version_id ?? null, a.resume?.label ?? null, a.resume?.status ?? null, a.career_os?.pulled_at ?? null]);
}

function ApplicationView({ id, stamp, interviews, onStartInterview, onBack, onChanged, onDeleted }: {
  id: string;
  stamp: string;
  interviews: InterviewItem[];
  onStartInterview?: (interviewId: string) => void;
  onBack: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [app, setApp] = useState<ApplicationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [scheduling, setScheduling] = useState(false);
  const load = useCallback(async () => {
    try { setApp(await rest.application(id)); setError(null); }
    catch (e) { setError(String(e)); }
  }, [id]);
  useEffect(() => { void load(); }, [load, stamp]);
  usePolling(load, 3000, !!app && app.versions.some((v) => v.status === "extracting"));
  const saved = (a: ApplicationDetail | null) => { if (a) setApp(a); onChanged(); };

  return <div className="app-view prep-document">
    <Button variant="ghost" size="sm" className="apps-back" onClick={onBack}>← Applications</Button>
    {error && <Banner tone="danger" title="Could not refresh the application" action={<Button size="sm" onClick={load}>Retry</Button>}>{error}</Banner>}
    {app ? <>
      <header className="app-view-head prep-document-head">
        <div className="app-view-title"><h2>{app.company}</h2><p>{app.role}</p></div>
        <Button size="sm" onClick={() => setEditing(!editing)} aria-expanded={editing} aria-label="Edit application">Edit</Button>
        <Menu label="Application actions" trigger="⋯" items={[
          { label: "Schedule interview…", onSelect: () => setScheduling(true) }, "separator",
          { label: "Delete application…", danger: true, onSelect: () => setDeleting(true) },
        ]} />
      </header>
      <div className="prep-document-status"><PackBadge pack={app.pack} /></div>
      {editing && <DetailsEditor app={app} onCancel={() => setEditing(false)} onSaved={a => { saved(a); setEditing(false); }} />}
      <ResumeSection app={app} onSaved={saved} />
      <JobDescription app={app} onSaved={saved} />
      <ApplicationNotes app={app} onSaved={saved} />
      <InterviewSection interviews={interviews} onStartInterview={onStartInterview} onSchedule={() => setScheduling(true)} />
      <ScheduleInterview open={scheduling} onOpenChange={setScheduling} app={app} onScheduled={onChanged} />
    </> : !error && <div className="ui-skeleton-list" aria-busy="true"><span className="ui-visually-hidden" role="status">Loading the application…</span></div>}
    <ConfirmDialog open={deleting} onOpenChange={setDeleting} title={`Delete ${app?.company ?? "this application"}?`} confirmLabel="Delete application" onConfirm={async () => { await rest.deleteApplication(id); onDeleted(); }}>
      Its tailored resumes and its interviews are deleted too.
    </ConfirmDialog>
  </div>;
}

function DetailsEditor({ app, onSaved, onCancel }: { app: ApplicationDetail; onSaved: (a: ApplicationDetail | null) => void; onCancel: () => void }) {
  const initial: ApplicationFields = { company: app.company, role: app.role, url: app.url, source_site: app.source_site, stage: app.stage, note: app.note, jd_text: app.jd_text };
  const [fields, setFields] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const checks = useChecks(fields);
  const toast = useToast();
  const set = <K extends keyof ApplicationFields>(k: K, v: ApplicationFields[K]) => setFields((f) => ({ ...f, [k]: v }));
  const dirty = JSON.stringify(fields) !== JSON.stringify(initial);
  const save = async () => {
    checks.all(); if (!checks.ok || busy) return;
    setBusy(true); setError(null);
    try {
      const out = await rest.updateApplication(app.id, { company: fields.company, role: fields.role, stage: fields.stage, url: fields.url || null, source_site: fields.source_site || null });
      toast({ tone: "success", text: "Application saved." }); onSaved(out);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return <form className="prep-editor" onSubmit={(e) => { e.preventDefault(); void save(); }}>
    <div className="form-2">
      <Field label="Company" error={checks.shown("company")}><Input autoFocus value={fields.company} onChange={e => set("company", e.target.value)} onBlur={checks.touch("company")} /></Field>
      <Field label="Role" error={checks.shown("role")}><Input value={fields.role} onChange={e => set("role", e.target.value)} onBlur={checks.touch("role")} /></Field>
    </div>
    <details className="context-details" open={checks.shown("url") ? true : undefined}>
      <summary>Application details</summary>
      <div className="form-2">
        <Field label="Stage"><Select value={fields.stage} onChange={e => set("stage", e.target.value as Stage)}>{STAGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select></Field>
        <Field label="Source" optional><Input value={fields.source_site ?? ""} onChange={e => set("source_site", e.target.value)} /></Field>
      </div>
      <Field label="Posting link" optional error={checks.shown("url")}><Input value={fields.url ?? ""} onChange={e => set("url", e.target.value)} onBlur={checks.touch("url")} placeholder="https://" /></Field>
    </details>
    {error && <Banner tone="danger">{error}</Banner>}
    <div className="prep-inline-actions prep-editor-actions"><Button variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button><Button type="submit" variant="primary" disabled={!dirty} loading={busy}>Save application</Button></div>
  </form>;
}

function JobDescription({ app, onSaved }: { app: ApplicationDetail; onSaved: (a: ApplicationDetail | null) => void }) {
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [text, setText] = useState(app.jd_text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const save = async () => {
    if (!text.trim()) { setError("The job description can't be empty."); return; }
    if (busy) return;
    setBusy(true); setError(null);
    try { onSaved(await rest.updateApplication(app.id, { jd_text: text })); setEditing(false); toast({ tone: "success", text: "Job description saved." }); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const pick = async (f: File) => {
    setBusy(true); setError(null);
    try {
      if (isTextFile(f.name)) { setText(await f.text()); return; }
      await api.uploadDocument("job_description", f, { application_id: app.id });
      const updated = await rest.application(app.id); onSaved(updated); setText(updated.jd_text);
      setEditing(false); toast({ tone: "success", text: "Uploaded. The job description is being read." });
    } catch (e) { setError(`${f.name}: ${e}`); } finally { setBusy(false); }
  };
  return <section className="prep-section" aria-labelledby={`jd-${app.id}`}>
    <div className="prep-section-head"><h3 id={`jd-${app.id}`}>Job description</h3><Button size="sm" onClick={() => { setText(app.jd_text); setError(null); setEditing(!editing); }} disabled={busy} aria-expanded={editing}>Edit job description</Button></div>
    {editing ? <div className="prep-editor">
      <Field label="Job description" error={error}><Textarea autoFocus rows={7} value={text} onChange={e => setText(e.target.value)} /></Field>
      <div className="prep-inline-actions prep-editor-actions">
        <input ref={file} type="file" accept={ACCEPT} hidden onChange={e => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void pick(f); }} />
        <Button onClick={() => file.current?.click()} disabled={busy}>Upload file…</Button><span className="ui-spacer" />
        <Button variant="ghost" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
        <Button variant="primary" disabled={text === app.jd_text} loading={busy} onClick={save}>Save description</Button>
      </div>
    </div> : <>
      <p className={expanded ? "prep-description" : "prep-description is-collapsed"}>{app.jd_text || "No job description added."}</p>
      {(app.jd_text.length > 340 || app.jd_text.split(/\r?\n/).length > 5) && <Button size="sm" variant="ghost" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>{expanded ? "Show less" : "Show full description"}</Button>}
    </>}
  </section>;
}

function ApplicationNotes({ app, onSaved }: { app: ApplicationDetail; onSaved: (a: ApplicationDetail | null) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(app.note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    if (busy) return; setBusy(true); setError(null);
    try { onSaved(await rest.updateApplication(app.id, { note: text.trim() || null })); setEditing(false); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  return <section className="prep-section" aria-labelledby={`notes-${app.id}`}>
    <div className="prep-section-head"><h3 id={`notes-${app.id}`}>Call notes</h3><Button size="sm" onClick={() => { setText(app.note ?? ""); setError(null); setEditing(!editing); }} disabled={busy} aria-expanded={editing}>{app.note ? "Edit notes" : "Add notes"}</Button></div>
    {editing ? <div className="prep-editor">
      <Field label="Call notes" error={error}><Textarea rows={3} autoFocus value={text} onChange={e => setText(e.target.value)} /></Field>
      <div className="prep-inline-actions prep-editor-actions"><Button variant="ghost" onClick={() => setEditing(false)} disabled={busy}>Cancel</Button><Button variant="primary" onClick={save} disabled={text === (app.note ?? "")} loading={busy}>Save notes</Button></div>
    </div> : app.note ? <p className="prep-description">{app.note}</p> : null}
  </section>;
}

function ResumeSection({ app, onSaved }: { app: ApplicationDetail; onSaved: (a: ApplicationDetail | null) => void }) {
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<ApplicationDetail["versions"][number] | null>(null);
  const [deleting, setDeleting] = useState<ApplicationDetail["versions"][number] | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const run = async (what: () => Promise<unknown>, done: string) => {
    if (busy) return; setBusy(true); setError(null);
    try { await what(); onSaved(await rest.application(app.id)); toast({ tone: "success", text: done }); }
    catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const tailored = app.versions.filter(v => v.kind === "tailored");
  return <section className="prep-section prep-resume" aria-labelledby={`resume-${app.id}`}>
    <div className="prep-section-head"><h3 id={`resume-${app.id}`}>Resume</h3><Button size="sm" onClick={() => setEditing(!editing)} aria-expanded={editing} disabled={busy}>Change resume</Button></div>
    <div className="prep-attached-resume"><Icon name="document" size={21} /><div><strong title={app.resume?.label}>{app.resume?.label ?? "No resume attached"}</strong><span>{app.resume?.kind === "tailored" ? "Tailored for this application" : app.resume ? "Default resume" : "Add a resume for this application"}</span></div>{app.resume && <Badge tone={statusTone(app.resume.status)}>{resumeStatusLabel(app.resume.status)}</Badge>}</div>
    {editing && <div className="prep-editor">
      <Field label="Resume for this application"><Select value={pairingValue(app)} disabled={busy} onChange={e => run(() => rest.updateApplication(app.id, { resume_version_id: e.target.value }), "Application resume changed.")}>
        {pairingChoices(app).map(c => <option key={c.value} value={c.value}>{c.value === "auto" ? "Automatic · latest tailored resume or default" : c.label.replace("Base resume:", "Default:")}</option>)}
      </Select></Field>
      {tailored.length > 0 && <List label="Saved resumes for this application" className="prep-version-list">{tailored.map(v => <ListRow key={v.id} title={v.label} fullTitle={v.label} meta={v.id === app.resume?.version_id ? "In use" : resumeStatusLabel(v.status)} actions={<Menu label={`Actions for ${v.label}`} trigger="⋯" items={[
        { label: "Rename…", onSelect: () => setRenaming(v) }, "separator", { label: "Delete…", danger: true, onSelect: () => setDeleting(v) },
      ]} />}/>)}</List>}
      <Button size="sm" onClick={() => setAdding(!adding)} aria-expanded={adding}>Add tailored resume</Button>
      {adding && <div className="prep-add-resume">
        <Field label="Resume name" optional><Input value={label} onChange={e => setLabel(e.target.value)} placeholder={`${app.company} resume`} /></Field>
        <Field label="Resume text"><Textarea rows={5} value={text} onChange={e => setText(e.target.value)} placeholder="Paste your tailored resume" /></Field>
        <div className="prep-inline-actions prep-editor-actions">
          <input ref={file} type="file" accept={ACCEPT} hidden onChange={e => { const f = e.target.files?.[0]; e.target.value = ""; if (f) void run(() => api.uploadDocument("resume", f, { application_id: app.id, label: label.trim() || f.name }), "Resume uploaded."); }} />
          <Button onClick={() => file.current?.click()} disabled={busy}>Upload file…</Button><span className="ui-spacer" />
          <Button variant="primary" disabled={!text.trim()} loading={busy} onClick={() => run(async () => { await rest.createResume({ kind: "tailored", application_id: app.id, label: label.trim() || null, text }); setText(""); setLabel(""); setAdding(false); }, "Tailored resume added.")}>Add pasted resume</Button>
        </div>
      </div>}
      {error && <Banner tone="danger">{error}</Banner>}
    </div>}
    <RenameResume version={renaming} onClose={() => setRenaming(null)} onDone={() => { void rest.application(app.id).then(onSaved).catch(e => setError(String(e))); }} />
    <ConfirmDialog open={deleting !== null} onOpenChange={open => { if (!open) setDeleting(null); }} title={`Delete "${deleting?.label ?? ""}"?`} confirmLabel="Delete resume" onConfirm={async () => {
      if (!deleting) return; await rest.deleteResume(deleting.id); onSaved(await rest.application(app.id)); toast({ tone: "success", text: "Resume deleted." });
    }}>This application will use its next available resume.</ConfirmDialog>
  </section>;
}

function InterviewSection({ interviews, onSchedule, onStartInterview }: { interviews: InterviewItem[]; onSchedule: () => void; onStartInterview?: (id: string) => void }) {
  const sorted = [...interviews].sort((a, b) => Date.parse(b.starts_at) - Date.parse(a.starts_at));
  const upcomingIds = new Set(upcoming(interviews).map(i => i.id));
  return <section className="prep-section" aria-label="Interviews">
    <div className="prep-section-head"><h3>Interviews</h3><Button size="sm" onClick={onSchedule}>Schedule interview…</Button></div>
    {sorted.length === 0 ? <p className="prep-muted">No interviews scheduled.</p> : <List label="Interviews" className="prep-interviews">{sorted.map(i => <ListRow key={i.id} className={upcomingIds.has(i.id) ? undefined : "is-past"} title={roundLabel(i.round)} meta={when(i.starts_at)} trailing={<>{!upcomingIds.has(i.id) && <Badge tone="neutral">Past</Badge>}{onStartInterview && <Button size="sm" onClick={() => onStartInterview(i.id)} aria-label={`Start a call for ${i.company ?? ""} ${roundLabel(i.round)}`}>Start call</Button>}</>} />)}</List>}
  </section>;
}

function ScheduleInterview({ open, onOpenChange, app, onScheduled }: { open: boolean; onOpenChange: (o: boolean) => void; app: ApplicationDetail; onScheduled: () => void }) {
  const [round, setRound] = useState<Round>("technical");
  const [startsAt, setStartsAt] = useState(soon);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  useEffect(() => {
    if (open) {
      setStartsAt(soon());
      setError(null);
    }
  }, [open]);
  const schedule = async () => {
    const at = new Date(startsAt);
    if (Number.isNaN(at.getTime())) {
      setError("Pick a date and time.");
      return;
    }
    setBusy(true);
    try {
      await rest.scheduleInterview(app.id, round, at.toISOString());
      toast({ tone: "success", text: "Scheduled. Its pack is being built for the round." });
      onOpenChange(false);
      onScheduled();
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
      size="sm"
      title="Schedule interview"
      description={`${app.company} · ${app.role}`}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={schedule}>
            Schedule
          </Button>
        </>
      }
    >
      <Field label="Round">
        <Select value={round} onChange={(e) => setRound(e.target.value as Round)}>
          {ROUNDS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Starts" error={error}>
        <Input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} />
      </Field>
    </Dialog>
  );
}
