// Documents: upload a resume and job descriptions (presigned upload, then ingest), with
// their processing status, and what they produced: the hot digest from the resume, and for
// each job description its job and session pack.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, rest, type DigestInfo, type DocumentItem, type DocumentKind, type JobItem } from "./api";
import { usePolling } from "./usePolling";

const ACCEPT = ".pdf,.md,.markdown,.txt";
const KIND_LABEL: Record<string, string> = { resume: "Resume", job_description: "Job description", linkedin_export: "LinkedIn export", other: "Other" };

export function Documents() {
  const [docs, setDocs] = useState<DocumentItem[] | null>(null);
  const [digest, setDigest] = useState<DigestInfo | null>(null);
  const [jobs, setJobs] = useState<Record<string, JobItem>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<DocumentKind | null>(null);

  const load = useCallback(async () => {
    try {
      const [d, j] = await Promise.all([rest.documents(), rest.jobs()]);
      setDocs(d.documents);
      setDigest(d.digest);
      setJobs(Object.fromEntries(j.jobs.map((x) => [x.id, x])));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  // Keep looking while something is still being processed.
  const resumeIngesting = docs?.some((d) => d.kind === "resume" && d.status === "ingesting") ?? false;
  const pending =
    docs?.some((d) => d.status === "ingesting" || (d.kind === "job_description" && d.status === "ingested" && !jobs[d.job_id ?? ""]?.pack?.available)) ?? false;
  usePolling(load, 3000, pending);

  const upload = async (kind: DocumentKind, file: File) => {
    setBusy(kind);
    setError(null);
    try {
      await api.uploadDocument(kind, file);
      await load();
    } catch (e) {
      setError(`${file.name}: ${e}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel">
      <div className="grid">
        <label>Resume</label>
        <Picker kind="resume" busy={busy} onPick={upload} hint="PDF, Markdown or text. A new resume replaces the facts from the previous one." />
        <label>Job description</label>
        <Picker kind="job_description" busy={busy} onPick={upload} hint="One posting per file. It becomes a job with its own session pack." />
        <label>Career digest</label>
        <p className="value">
          {digest ? (
            <>
              <Chip state="ok">Ready</Chip> {digest.facts} facts{digest.built_at ? `, built ${when(digest.built_at)}` : ""}
            </>
          ) : resumeIngesting ? (
            <>
              <Chip state="busy">Building</Chip> from your resume…
            </>
          ) : (
            <span className="muted">Not built yet: upload your resume.</span>
          )}
        </p>
      </div>
      {error && <p className="note warn">{error}</p>}

      <h3>Uploaded</h3>
      {docs === null && !error && <p className="note">Loading…</p>}
      {docs?.length === 0 && <p className="note">Nothing yet.</p>}
      <table className="list">
        <tbody>
          {docs?.map((d) => (
            <tr key={d.id}>
              <td>{KIND_LABEL[d.kind] ?? d.kind}</td>
              <td className="muted">{when(d.created_at)}</td>
              <td>
                <DocStatus doc={d} />
              </td>
              <td>{d.kind === "job_description" && <JobOf doc={d} job={jobs[d.job_id ?? ""]} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Picker({ kind, busy, onPick, hint }: { kind: DocumentKind; busy: DocumentKind | null; onPick: (k: DocumentKind, f: File) => void; hint: string }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <div>
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onPick(kind, f);
          e.target.value = "";
        }}
      />
      <button disabled={busy !== null} onClick={() => input.current?.click()}>
        {busy === kind ? "Uploading…" : "Choose file…"}
      </button>
      <p className="note">{hint}</p>
    </div>
  );
}

export function Chip({ state, children }: { state: "ok" | "busy" | "bad" | "off"; children: React.ReactNode }) {
  return <span className={`chip chip-${state}`}>{children}</span>;
}

function DocStatus({ doc }: { doc: DocumentItem }) {
  switch (doc.status) {
    case "ingesting":
      return <Chip state="busy">Processing</Chip>;
    case "ingested":
      return <Chip state="ok">Processed</Chip>;
    case "failed":
      return <Chip state="bad">Could not read it; upload again</Chip>;
    default:
      return <Chip state="off">Uploaded</Chip>;
  }
}

function JobOf({ doc, job }: { doc: DocumentItem; job: JobItem | undefined }) {
  if (doc.status !== "ingested" || !job) return null;
  return (
    <span>
      {job.company} · {job.title} · <PackStatus pack={job.pack} />
    </span>
  );
}

export function PackStatus({ pack }: { pack: JobItem["pack"] | undefined }) {
  if (!pack) return <Chip state="busy">Pack building</Chip>;
  if (!pack.available) return <Chip state="off">Pack expired</Chip>;
  return <Chip state="ok">Pack ready{pack.prepared_answers != null ? ` (${pack.prepared_answers} answers)` : ""}</Chip>;
}

export function when(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
