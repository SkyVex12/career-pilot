// Applications: what the user applied to (company, role, stage, JD), each paired with a resume
// version (ARCHITECTURE.md, "Resume versions"); added by hand or pulled from Career-OS. The
// upcoming interviews on top, the list beside the one chosen, and per application its details,
// job description, resume and interviews (docs/design/ui-proposal.md, "Applications").
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, rest, type ApplicationDetail, type ApplicationFields, type ApplicationItem, type InterviewItem, type Round, type Stage } from "./api";
import { STAGES, isTextFile, pairingChoices, pairingValue, resumeLabel, stageLabel } from "./pairing";
import { upcoming } from "./catalog";
import { PullDialog, useCareerOs } from "./CareerOs";
import { ACCEPT, PackBadge, relative, useOnShow, when } from "./shared";
import { usePolling } from "./usePolling";
import {
  Badge,
  Banner,
  Button,
  Card,
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
  SegmentedControl,
  Select,
  Tabs,
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
}

/// Applications and interviews from the API, polled while a pack is still being built.
export function useCatalog(enabled = true): Catalog {
  const [applications, setApplications] = useState<ApplicationItem[] | null>(null);
  const [interviews, setInterviews] = useState<InterviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try {
      const [a, i] = await Promise.all([rest.applications(), rest.interviews()]);
      setApplications(a.applications);
      setInterviews(i.interviews);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    if (enabled) reload();
  }, [enabled, reload]);
  const building =
    (applications ?? []).some((a) => !a.pack || a.resume?.status === "extracting") || upcoming(interviews ?? []).some((i) => !i.pack);
  usePolling(reload, 4000, enabled && building);
  return { applications, interviews, error, reload };
}

/// `datetime-local` value one hour from now, rounded to 5 minutes.
function soon(): string {
  const d = new Date(Date.now() + 3600_000);
  d.setMinutes(Math.ceil(d.getMinutes() / 5) * 5, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

type StageFilter = "all" | Stage;
const STAGE_FILTERS: readonly (readonly [StageFilter, string])[] = [["all", "All"], ...STAGES];

/** Filters by company or role, and by stage. */
export function filterApplications(list: ApplicationItem[], q: string, stage: StageFilter): ApplicationItem[] {
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return list.filter((a) => (stage === "all" || a.stage === stage) && words.every((w) => `${a.company} ${a.role}`.toLowerCase().includes(w)));
}

export function Applications({
  active = true,
  focus,
  onStartInterview,
}: {
  active?: boolean;
  /** An application to show (Resumes' "Open application"). */
  focus?: { id: string; n: number } | null;
  /** Start a call for this interview (the Call screen, with it chosen). */
  onStartInterview?: (interviewId: string) => void;
}) {
  const { applications, interviews, error, reload } = useCatalog();
  useOnShow(active, reload);
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [q, setQ] = useState("");
  const [stage, setStage] = useState<StageFilter>("all");
  const { conn } = useCareerOs(true);

  useEffect(() => {
    if (focus) setSelected(focus.id);
  }, [focus]);
  // Nothing chosen yet: the first one, on a wide window.
  useEffect(() => {
    if (!selected && applications?.length && window.innerWidth >= 900) setSelected(applications[0].id);
  }, [applications]); // eslint-disable-line react-hooks/exhaustive-deps

  const shown = useMemo(() => (applications ? filterApplications(applications, q, stage) : null), [applications, q, stage]);
  const next = upcoming(interviews ?? []);

  return (
    <div className="apps">
      <PageHeader
        title="Applications"
        actions={
          <>
            {conn?.connected && <Button onClick={() => setPulling(true)}>Pull from Career-OS…</Button>}
            <Button variant="primary" onClick={() => setAdding(true)}>
              + Add application
            </Button>
          </>
        }
        meta={conn && !conn.connected && conn.available ? "Connect Career-OS under Settings, Account, to pull applications from it." : undefined}
      />

      {next.length > 0 && (
        <Card title="Upcoming interviews" className="apps-upcoming">
          <List label="Upcoming interviews">
            {next.slice(0, 4).map((i) => (
              <ListRow
                key={i.id}
                leading={<span className="apps-when">{relative(i.starts_at)}</span>}
                title={`${i.company ?? "?"} · ${roundLabel(i.round)}`}
                meta={i.title ?? undefined}
                trailing={
                  <>
                    <PackBadge pack={i.pack} />
                    {onStartInterview && (
                      <Button size="sm" onClick={() => onStartInterview(i.id)} aria-label={`Start a call for ${i.company ?? ""} ${roundLabel(i.round)}`}>
                        Start
                      </Button>
                    )}
                  </>
                }
              />
            ))}
          </List>
        </Card>
      )}

      <div className={selected ? "apps-split has-selection" : "apps-split"}>
        <section className="apps-list" aria-label="Applications">
          <div className="apps-filters">
            <Input type="search" aria-label="Search company or role" placeholder="Search company or role" value={q} onChange={(e) => setQ(e.target.value)} />
            <SegmentedControl label="Stage" size="sm" value={stage} options={STAGE_FILTERS} onChange={setStage} />
          </div>
          <ListState
            items={shown}
            error={error}
            onRetry={reload}
            loadingLabel="applications"
            empty={
              applications?.length ? (
                <EmptyState title="No applications match">Try another word or stage.</EmptyState>
              ) : (
                <EmptyState
                  icon="▤"
                  title="No applications yet"
                  action={
                    <>
                      <Button variant="primary" onClick={() => setAdding(true)}>
                        Add application
                      </Button>
                      {conn?.connected && <Button onClick={() => setPulling(true)}>Pull from Career-OS…</Button>}
                    </>
                  }
                >
                  Add one with its job description, so answers in its interviews use that JD and its resume.
                </EmptyState>
              )
            }
          >
            {(list) => (
              <List label="All applications">
                {list.map((a) => (
                  <ListRow
                    key={a.id}
                    selected={a.id === selected}
                    onClick={() => setSelected(a.id)}
                    title={a.company}
                    fullTitle={`${a.company} · ${a.role}`}
                    meta={`${a.role} · ${stageLabel(a.stage)}`}
                    trailing={<PackDot pack={a.pack} />}
                  />
                ))}
              </List>
            )}
          </ListState>
        </section>
        <section className="apps-detail" aria-label="Application">
          {selected ? (
            <ApplicationView
              key={selected}
              id={selected}
              interviews={(interviews ?? []).filter((i) => i.job_id === selected)}
              onBack={() => setSelected(null)}
              onChanged={reload}
              onDeleted={() => {
                setSelected(null);
                reload();
              }}
            />
          ) : (
            applications &&
            applications.length > 0 && (
              <EmptyState title="Pick an application">Its details, job description, resume and interviews show here.</EmptyState>
            )
          )}
        </section>
      </div>

      <AddApplication
        open={adding}
        onOpenChange={setAdding}
        onAdded={(app) => {
          reload();
          setSelected(app.id);
        }}
      />
      <PullDialog
        open={pulling}
        onOpenChange={setPulling}
        onPulled={(app) => {
          reload();
          setSelected(app.id);
        }}
      />
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
  return { errors, touch, shown, all, ok: Object.keys(errors).length === 0 };
}

function AddApplication({ open, onOpenChange, onAdded }: { open: boolean; onOpenChange: (o: boolean) => void; onAdded: (a: ApplicationDetail) => void }) {
  const [fields, setFields] = useState<ApplicationFields>(EMPTY);
  const [resume, setResume] = useState("");
  const [more, setMore] = useState(false);
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
      setResume("");
      setError(null);
      setFileNote(null);
      setMore(false);
    }
  }, [open]);

  const submit = async () => {
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
        resume_text: resume.trim() || null,
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
        <Field label="Job description" error={checks.shown("jd_text")} hint={fileNote ?? "Paste it, or load a .txt or .md file."}>
          <Textarea rows={8} value={fields.jd_text} onChange={(e) => set("jd_text", e.target.value)} onBlur={checks.touch("jd_text")} />
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
                set("jd_text", await f.text());
                setFileNote(`Loaded ${f.name}.`);
              } else setFileNote("A PDF can be uploaded once the application is added (its Job description tab). Paste the text for now.");
            }}
          />
          <Button size="sm" onClick={() => file.current?.click()}>
            Load from a file…
          </Button>
        </div>
        <Field label="Stage">
          <Select value={fields.stage} onChange={(e) => set("stage", e.target.value as Stage)}>
            {STAGES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </Select>
        </Field>
        <Button variant="link" className="form-more" aria-expanded={more} onClick={() => setMore((m) => !m)}>
          {more ? "▾ Fewer details" : "▸ More details (posting link, source, note, tailored resume)"}
        </Button>
        {more && (
          <>
            <Field label="Posting link" optional error={checks.shown("url")}>
              <Input value={fields.url ?? ""} onChange={(e) => set("url", e.target.value)} onBlur={checks.touch("url")} placeholder="https://" />
            </Field>
            <div className="form-2">
              <Field label="Source" optional>
                <Input value={fields.source_site ?? ""} onChange={(e) => set("source_site", e.target.value)} placeholder="LinkedIn, the company site…" />
              </Field>
              <Field label="Note" optional>
                <Input value={fields.note ?? ""} onChange={(e) => set("note", e.target.value)} />
              </Field>
            </div>
            <Field label="Tailored resume" optional hint="Otherwise it uses your base resume. A file can be added once it is saved.">
              <Textarea rows={4} value={resume} onChange={(e) => setResume(e.target.value)} placeholder="Paste a resume tailored for this application" />
            </Field>
          </>
        )}
        {error && <Banner tone="danger">{error}</Banner>}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

function ApplicationView({
  id,
  interviews,
  onBack,
  onChanged,
  onDeleted,
}: {
  id: string;
  interviews: InterviewItem[];
  onBack: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [app, setApp] = useState<ApplicationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState("details");
  const [deleting, setDeleting] = useState(false);
  const [scheduling, setScheduling] = useState(false);

  const load = useCallback(async () => {
    try {
      setApp(await rest.application(id));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [id]);
  useEffect(() => {
    load();
  }, [load]);
  usePolling(load, 3000, !!app && app.versions.some((v) => v.status === "extracting"));

  const saved = (a: ApplicationDetail | null) => {
    if (a) setApp(a);
    onChanged();
  };

  if (error && !app) {
    return (
      <Banner tone="danger" title="Could not load the application" action={<Button size="sm" onClick={load}>Retry</Button>}>
        {error}
      </Banner>
    );
  }
  return (
    <div className="app-view">
      <Button variant="ghost" size="sm" className="apps-back" onClick={onBack}>
        ← Applications
      </Button>
      <div className="app-view-head">
        <div className="app-view-title">
          <h2 className="ui-card-title" title={app ? `${app.company} · ${app.role}` : undefined}>
            {app ? app.company : "…"}
          </h2>
          <div className="ui-text-2 ui-truncate">{app ? `${app.role} · ${stageLabel(app.stage)}` : ""}</div>
        </div>
        <Menu
          label="Application actions"
          trigger="⋯"
          items={[
            { label: "Schedule interview…", onSelect: () => setScheduling(true) },
            "separator",
            { label: "Delete application…", danger: true, onSelect: () => setDeleting(true) },
          ]}
        />
      </div>
      {app && (
        <div className="ui-row-inline app-view-badges">
          <PackBadge pack={app.pack} />
          {app.career_os && <Badge tone="info">From Career-OS, {when(app.career_os.pulled_at)}</Badge>}
        </div>
      )}
      {app ? (
        <Tabs
          label="Application"
          value={tab}
          onChange={setTab}
          items={[
            { value: "details", label: "Details", content: <DetailsTab app={app} onSaved={saved} /> },
            { value: "jd", label: "Job description", content: <JdTab app={app} onSaved={saved} /> },
            { value: "resume", label: "Resume", content: <ResumeTab app={app} onSaved={saved} /> },
            {
              value: "interviews",
              label: "Interviews",
              count: interviews.length,
              content: <InterviewsTab interviews={interviews} onSchedule={() => setScheduling(true)} />,
            },
          ]}
        />
      ) : (
        <div className="ui-skeleton-list" aria-busy="true">
          <span className="ui-visually-hidden" role="status">
            Loading the application…
          </span>
        </div>
      )}
      {app && (
        <ScheduleInterview
          open={scheduling}
          onOpenChange={setScheduling}
          app={app}
          onScheduled={() => {
            onChanged();
            setTab("interviews");
          }}
        />
      )}
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete ${app?.company ?? "this application"}?`}
        confirmLabel="Delete application"
        onConfirm={async () => {
          await rest.deleteApplication(id);
          onDeleted();
        }}
      >
        Its tailored resumes and its interviews are deleted too.
      </ConfirmDialog>
    </div>
  );
}

function DetailsTab({ app, onSaved }: { app: ApplicationDetail; onSaved: (a: ApplicationDetail | null) => void }) {
  const initial: ApplicationFields = { company: app.company, role: app.role, url: app.url, source_site: app.source_site, stage: app.stage, note: app.note, jd_text: app.jd_text };
  const [fields, setFields] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const checks = useChecks(fields);
  const toast = useToast();
  const set = <K extends keyof ApplicationFields>(k: K, v: ApplicationFields[K]) => setFields((f) => ({ ...f, [k]: v }));
  const dirty = JSON.stringify({ ...fields, jd_text: "" }) !== JSON.stringify({ ...initial, jd_text: "" });
  const save = async () => {
    checks.all();
    if (!checks.ok) return;
    setBusy(true);
    setError(null);
    try {
      const { jd_text: _jd, ...rest_ } = fields;
      void _jd;
      const out = await rest.updateApplication(app.id, { ...rest_, url: fields.url || null, source_site: fields.source_site || null, note: fields.note || null });
      toast({ tone: "success", text: "Saved." });
      onSaved(out);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save();
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
      <div className="form-2">
        <Field label="Stage">
          <Select value={fields.stage} onChange={(e) => set("stage", e.target.value as Stage)}>
            {STAGES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Source" optional>
          <Input value={fields.source_site ?? ""} onChange={(e) => set("source_site", e.target.value)} />
        </Field>
      </div>
      <Field label="Posting link" optional error={checks.shown("url")}>
        <Input value={fields.url ?? ""} onChange={(e) => set("url", e.target.value)} onBlur={checks.touch("url")} placeholder="https://" />
      </Field>
      <Field label="Note" optional>
        <Input value={fields.note ?? ""} onChange={(e) => set("note", e.target.value)} />
      </Field>
      {error && <Banner tone="danger">{error}</Banner>}
      <div className="form-actions">
        <Button variant="ghost" disabled={!dirty || busy} onClick={() => setFields(initial)}>
          Discard
        </Button>
        <Button type="submit" variant="primary" disabled={!dirty} loading={busy}>
          Save
        </Button>
      </div>
      {app.career_os && <p className="ui-field-hint">Pulled from Career-OS. Pull it again from "Pull from Career-OS…" to take its changes; nothing updates by itself.</p>}
    </form>
  );
}

function JdTab({ app, onSaved }: { app: ApplicationDetail; onSaved: (a: ApplicationDetail | null) => void }) {
  const [text, setText] = useState(app.jd_text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const dirty = text !== app.jd_text;
  const save = async () => {
    if (!text.trim()) {
      setError("The job description can't be empty.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSaved(await rest.updateApplication(app.id, { jd_text: text }));
      toast({ tone: "success", text: "Saved. Its pack is rebuilt with this JD." });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const pick = async (f: File) => {
    if (isTextFile(f.name)) {
      setText(await f.text());
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.uploadDocument("job_description", f, { application_id: app.id });
      onSaved(await rest.application(app.id));
      toast({ tone: "success", text: "Uploaded. The JD is read from the PDF and fills this application." });
    } catch (e) {
      setError(`${f.name}: ${e}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <Field label="Job description" error={error}>
        <Textarea rows={14} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
      <div className="form-actions">
        <input
          ref={file}
          type="file"
          accept={ACCEPT}
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) pick(f);
          }}
        />
        <Button onClick={() => file.current?.click()} disabled={busy}>
          Upload a file (PDF, text)…
        </Button>
        <span className="ui-spacer" />
        <Button variant="ghost" disabled={!dirty || busy} onClick={() => setText(app.jd_text)}>
          Discard
        </Button>
        <Button variant="primary" disabled={!dirty} loading={busy} onClick={save}>
          Save
        </Button>
      </div>
    </div>
  );
}

function ResumeTab({ app, onSaved }: { app: ApplicationDetail; onSaved: (a: ApplicationDetail | null) => void }) {
  const [text, setText] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const toast = useToast();
  const run = async (what: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setError(null);
    try {
      await what();
      onSaved(await rest.application(app.id));
      toast({ tone: "success", text: done });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ui-stack">
      <Field label="Resume for this application" hint="Interview packs for this application use this resume only, with its JD.">
        <Select
          value={pairingValue(app)}
          disabled={busy}
          onChange={(e) => run(() => rest.updateApplication(app.id, { resume_version_id: e.target.value }), "Saved. Its packs are rebuilt with this resume.")}
        >
          {pairingChoices(app).map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </Select>
      </Field>
      <p className="ui-text-2">Now: {resumeLabel(app)}</p>
      <Card title="Add a tailored resume" inset>
        <Field label="Label" optional>
          <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Northwind backend v3" />
        </Field>
        <Field label="Resume text" error={error}>
          <Textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder="Paste a resume tailored for this application" />
        </Field>
        <div className="form-actions">
          <input
            ref={file}
            type="file"
            accept={ACCEPT}
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) run(() => api.uploadDocument("resume", f, { application_id: app.id, label: label.trim() || f.name }), "Uploaded. It becomes this application's resume once read.");
            }}
          />
          <Button onClick={() => file.current?.click()} disabled={busy}>
            Upload a file…
          </Button>
          <span className="ui-spacer" />
          <Button
            variant="primary"
            disabled={!text.trim()}
            loading={busy}
            onClick={() =>
              run(async () => {
                await rest.createResume({ kind: "tailored", application_id: app.id, label: label.trim() || null, text });
                setText("");
                setLabel("");
              }, "Added. This application now uses it, unless you picked another resume.")
            }
          >
            Add pasted resume
          </Button>
        </div>
      </Card>
    </div>
  );
}

function InterviewsTab({ interviews, onSchedule }: { interviews: InterviewItem[]; onSchedule: () => void }) {
  const sorted = [...interviews].sort((a, b) => Date.parse(b.starts_at) - Date.parse(a.starts_at));
  const upcomingIds = new Set(upcoming(interviews).map((i) => i.id));
  return (
    <div className="ui-stack">
      <div>
        <Button variant="primary" onClick={onSchedule}>
          Schedule interview…
        </Button>
      </div>
      {sorted.length === 0 ? (
        <EmptyState title="No interviews yet">Schedule one, and its pack is built for the round from this application's resume.</EmptyState>
      ) : (
        <List label="Interviews">
          {sorted.map((i) => (
            <ListRow
              key={i.id}
              className={upcomingIds.has(i.id) ? undefined : "is-past"}
              leading={<span className="apps-when">{when(i.starts_at)}</span>}
              title={roundLabel(i.round)}
              trailing={upcomingIds.has(i.id) ? <PackBadge pack={i.pack} /> : <Badge tone="neutral">Past</Badge>}
            />
          ))}
        </List>
      )}
    </div>
  );
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
