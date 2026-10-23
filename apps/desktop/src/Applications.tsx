// Applications: what the user applied to (company, role, stage, JD), each paired with a resume
// version (ARCHITECTURE.md, "Resume versions"); added by hand or pulled from Career-OS. Also
// scheduling an interview for one, and the interviews.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  rest,
  type ApplicationDetail,
  type ApplicationFields,
  type ApplicationItem,
  type CareerOsConnection,
  type InterviewItem,
  type Round,
} from "./api";
import { STAGES, isTextFile, pairingChoices, pairingValue, resumeLabel, stageLabel } from "./pairing";
import { upcoming } from "./catalog";
import { PullDialog } from "./CareerOs";
import { Chip, PackStatus, when } from "./ui";
import { usePolling } from "./usePolling";

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

export function Applications() {
  const { applications, interviews, error, reload } = useCatalog();
  const [editing, setEditing] = useState<string | "new" | null>(null);
  const [pulling, setPulling] = useState(false);
  const [careerOs, setCareerOs] = useState<CareerOsConnection | null>(null);
  useEffect(() => {
    rest.careerOs().then(setCareerOs, () => setCareerOs(null));
  }, []);

  return (
    <section className="panel">
      <div className="actions">
        <button className="primary" onClick={() => setEditing("new")}>
          Add application
        </button>
        {careerOs?.connected && <button onClick={() => setPulling(true)}>Pull from Career-OS…</button>}
        {careerOs && !careerOs.connected && careerOs.available && (
          <span className="note">Connect Career-OS under Settings to pull applications from it.</span>
        )}
      </div>
      {error && <p className="note warn">{error}</p>}
      {pulling && (
        <PullDialog
          onClose={() => setPulling(false)}
          onPulled={(app) => {
            reload();
            setEditing(app.id);
          }}
        />
      )}
      {editing && (
        <ApplicationEditor
          id={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(app) => {
            reload();
            setEditing(app?.id ?? null);
          }}
        />
      )}

      <h3>Applications</h3>
      {applications?.length === 0 && <p className="note">None yet: add one, or pull one from Career-OS.</p>}
      <table className="list">
        <tbody>
          {applications?.map((a) => (
            <tr key={a.id} className={editing === a.id ? "selected" : ""} onClick={() => setEditing(a.id)}>
              <td>
                <b>{a.company}</b> · {a.role}
              </td>
              <td>{stageLabel(a.stage)}</td>
              <td className="muted">{resumeLabel(a)}</td>
              <td>{a.career_os ? <Chip state="off">From Career-OS</Chip> : null}</td>
              <td>
                <PackStatus pack={a.pack} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <Interviews applications={applications} interviews={interviews} reload={reload} />
    </section>
  );
}

const EMPTY: ApplicationFields = { company: "", role: "", url: null, source_site: null, stage: "applied", note: null, jd_text: "" };

function ApplicationEditor({
  id,
  onClose,
  onSaved,
}: {
  id: string | null;
  onClose: () => void;
  onSaved: (app: ApplicationDetail | null) => void;
}) {
  const [app, setApp] = useState<ApplicationDetail | null>(null);
  const [fields, setFields] = useState<ApplicationFields>(EMPTY);
  const [resumeText, setResumeText] = useState("");
  const [resumeLabelText, setResumeLabelText] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const jdFile = useRef<HTMLInputElement>(null);
  const resumeFile = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    if (!id) return;
    const a = await rest.application(id);
    setApp(a);
    setFields({ company: a.company, role: a.role, url: a.url, source_site: a.source_site, stage: a.stage, note: a.note, jd_text: a.jd_text });
  }, [id]);
  useEffect(() => {
    setApp(null);
    setFields(EMPTY);
    load().catch((e) => setMessage(String(e)));
  }, [load]);
  usePolling(load, 3000, !!app && app.versions.some((v) => v.status === "extracting"));

  const set = <K extends keyof ApplicationFields>(k: K, v: ApplicationFields[K]) => setFields((f) => ({ ...f, [k]: v }));
  const run = async (what: () => Promise<ApplicationDetail | null>, done: string) => {
    setBusy(true);
    setMessage(null);
    try {
      const out = await what();
      setMessage(done);
      onSaved(out);
      if (out) setApp(out);
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    run(async () => {
      const clean = { ...fields, url: fields.url || null, source_site: fields.source_site || null, note: fields.note || null };
      if (!id) {
        return rest.createApplication({ ...clean, resume_text: resumeText.trim() || null, resume_label: resumeLabelText.trim() || null });
      }
      return rest.updateApplication(id, clean);
    }, id ? "Saved. Its pack is rebuilt if the JD changed." : "Added. Its session pack is being built.");

  const pickJd = async (file: File) => {
    if (isTextFile(file.name)) {
      set("jd_text", await file.text());
      return;
    }
    if (!id) return setMessage("Save the application first, then upload a PDF job description.");
    await run(async () => {
      await api.uploadDocument("job_description", file, { application_id: id });
      return rest.application(id);
    }, "Uploaded. The JD is read from the PDF and fills this application.");
  };

  const pickResume = async (file: File) => {
    if (!id) {
      if (isTextFile(file.name)) setResumeText(await file.text());
      else setMessage("Save the application first, then upload a PDF resume.");
      return;
    }
    await run(async () => {
      await api.uploadDocument("resume", file, { application_id: id, label: resumeLabelText.trim() || file.name });
      return rest.application(id);
    }, "Uploaded. It becomes this application's resume once read.");
  };

  const addPasted = () =>
    run(async () => {
      if (!id) return null;
      await rest.createResume({ kind: "tailored", application_id: id, label: resumeLabelText.trim() || null, text: resumeText });
      setResumeText("");
      return rest.application(id);
    }, "Added. This application now uses it, unless you picked another resume.");

  return (
    <div className="card">
      <div className="row">
        <h3>{id ? `${app?.company ?? "…"} · ${app?.role ?? ""}` : "New application"}</h3>
        <span className="spacer" />
        <button className="link" onClick={onClose}>
          Close
        </button>
      </div>
      {app?.career_os && (
        <p className="note">
          Pulled from Career-OS {when(app.career_os.pulled_at)}. Pull it again from “Pull from Career-OS…” to take its changes; nothing
          updates by itself.
        </p>
      )}
      <div className="grid">
        <label>Company</label>
        <input value={fields.company} onChange={(e) => set("company", e.target.value)} />
        <label>Role</label>
        <input value={fields.role} onChange={(e) => set("role", e.target.value)} />
        <label>Stage</label>
        <select value={fields.stage} onChange={(e) => set("stage", e.target.value as ApplicationFields["stage"])}>
          {STAGES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <label>Posting URL</label>
        <input value={fields.url ?? ""} onChange={(e) => set("url", e.target.value)} placeholder="Optional" />
        <label>Source site</label>
        <input value={fields.source_site ?? ""} onChange={(e) => set("source_site", e.target.value)} placeholder="LinkedIn, the company site…" />
        <label>Note</label>
        <input value={fields.note ?? ""} onChange={(e) => set("note", e.target.value)} placeholder="Optional" />
        <label>Job description</label>
        <div>
          <textarea rows={8} value={fields.jd_text} onChange={(e) => set("jd_text", e.target.value)} placeholder="Paste the job description" />
          <FilePicker input={jdFile} onPick={pickJd} label="Load from a file…" />
        </div>

        <label>Resume</label>
        <div>
          {app && (
            <select
              value={pairingValue(app)}
              onChange={(e) =>
                run(() => rest.updateApplication(app.id, { resume_version_id: e.target.value }), "Saved. Its packs are rebuilt with this resume.")
              }
              disabled={busy}
            >
              {pairingChoices(app).map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
          )}
          <p className="note">
            {app ? "Interview packs use this resume only, with this JD." : "Optional: a resume tailored for this application. Otherwise it uses your base resume."}
          </p>
          <input value={resumeLabelText} onChange={(e) => setResumeLabelText(e.target.value)} placeholder="Label for a new tailored resume" />
          <textarea rows={4} value={resumeText} onChange={(e) => setResumeText(e.target.value)} placeholder="Paste a tailored resume" />
          <div className="row">
            {app && (
              <button disabled={busy || !resumeText.trim()} onClick={addPasted}>
                Add pasted resume
              </button>
            )}
            <FilePicker input={resumeFile} onPick={pickResume} label="Upload a resume file…" />
          </div>
        </div>
      </div>
      <div className="actions">
        <button className="primary" disabled={busy || !fields.company || !fields.role || !fields.jd_text.trim()} onClick={save}>
          {id ? "Save" : "Add"}
        </button>
        {id && (
          <button
            className="danger"
            disabled={busy}
            onClick={() => {
              if (window.confirm("Delete this application, its tailored resumes and its interviews?")) {
                run(async () => {
                  await rest.deleteApplication(id);
                  onClose();
                  return null;
                }, "Deleted.");
              }
            }}
          >
            Delete
          </button>
        )}
        {message && <span className="note">{message}</span>}
      </div>
    </div>
  );
}

function FilePicker({ input, onPick, label }: { input: React.RefObject<HTMLInputElement | null>; onPick: (f: File) => void; label: string }) {
  return (
    <>
      <input
        ref={input}
        type="file"
        accept=".pdf,.md,.markdown,.txt"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onPick(f);
          e.target.value = "";
        }}
      />
      <button className="link" onClick={() => input.current?.click()}>
        {label}
      </button>
    </>
  );
}

function Interviews({
  applications,
  interviews,
  reload,
}: {
  applications: ApplicationItem[] | null;
  interviews: InterviewItem[] | null;
  reload: () => Promise<void>;
}) {
  const [appId, setAppId] = useState("");
  const [round, setRound] = useState<Round>("technical");
  const [startsAt, setStartsAt] = useState(soon);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    if (!appId && applications?.length) setAppId(applications[0].id);
  }, [applications, appId]);

  const schedule = async () => {
    setMessage(null);
    const at = new Date(startsAt);
    if (!appId || Number.isNaN(at.getTime())) return setMessage("Pick an application and a start time.");
    try {
      await rest.scheduleInterview(appId, round, at.toISOString());
      setMessage("Scheduled. Its session pack is being built for the round, from this application's resume.");
      await reload();
    } catch (e) {
      setMessage(String(e));
    }
  };

  return (
    <>
      <h3>Schedule an interview</h3>
      <div className="grid">
        <label>Application</label>
        <select value={appId} onChange={(e) => setAppId(e.target.value)} disabled={!applications?.length}>
          {applications?.map((a) => (
            <option key={a.id} value={a.id}>
              {a.company} · {a.role}
            </option>
          ))}
        </select>
        <label>Round</label>
        <select value={round} onChange={(e) => setRound(e.target.value as Round)}>
          {ROUNDS.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
        <label>Starts</label>
        <input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} />
      </div>
      <div className="actions">
        <button className="primary" disabled={!applications?.length} onClick={schedule}>
          Schedule
        </button>
        {message && <span className="note">{message}</span>}
      </div>

      <h3>Interviews</h3>
      {interviews?.length === 0 && <p className="note">None scheduled.</p>}
      <table className="list">
        <tbody>
          {interviews?.map((i) => (
            <tr key={i.id} className={upcoming([i]).length ? "" : "muted"}>
              <td>
                <b>{i.company ?? "?"}</b> · {i.title ?? ""}
              </td>
              <td>{roundLabel(i.round)}</td>
              <td>{when(i.starts_at)}</td>
              <td>
                <PackStatus pack={i.pack} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
