// Jobs and interviews: the jobs from GET /jobs (from uploaded job descriptions), scheduling
// an interview for one (POST /interviews builds its pack for the round), and the interviews.
import { useCallback, useEffect, useState } from "react";
import { rest, type InterviewItem, type JobItem, type Round } from "./api";
import { upcoming } from "./catalog";
import { PackStatus, when } from "./Documents";
import { usePolling } from "./usePolling";

export const ROUNDS: [Round, string][] = [
  ["phone_screen", "Phone screen"],
  ["technical", "Technical"],
  ["system_design", "System design"],
  ["final", "Final"],
];
export const roundLabel = (r: string) => ROUNDS.find(([v]) => v === r)?.[1] ?? r;

export interface Catalog {
  jobs: JobItem[] | null;
  interviews: InterviewItem[] | null;
  error: string | null;
  reload: () => Promise<void>;
}

/// Jobs and interviews from the API, polled while a pack is still being built.
export function useCatalog(enabled = true): Catalog {
  const [jobs, setJobs] = useState<JobItem[] | null>(null);
  const [interviews, setInterviews] = useState<InterviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try {
      const [j, i] = await Promise.all([rest.jobs(), rest.interviews()]);
      setJobs(j.jobs);
      setInterviews(i.interviews);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);
  useEffect(() => {
    if (enabled) reload();
  }, [enabled, reload]);
  const building = (jobs ?? []).some((j) => !j.pack) || upcoming(interviews ?? []).some((i) => !i.pack);
  usePolling(reload, 4000, enabled && building);
  return { jobs, interviews, error, reload };
}

/// `datetime-local` value one hour from now, rounded to 5 minutes.
function soon(): string {
  const d = new Date(Date.now() + 3600_000);
  d.setMinutes(Math.ceil(d.getMinutes() / 5) * 5, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function Jobs() {
  const { jobs, interviews, error, reload } = useCatalog();
  const [jobId, setJobId] = useState("");
  const [round, setRound] = useState<Round>("technical");
  const [startsAt, setStartsAt] = useState(soon);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId && jobs?.length) setJobId(jobs[0].id);
  }, [jobs, jobId]);

  const schedule = async () => {
    setMessage(null);
    const at = new Date(startsAt);
    if (!jobId || Number.isNaN(at.getTime())) return setMessage("Pick a job and a start time.");
    try {
      await rest.scheduleInterview(jobId, round, at.toISOString());
      setMessage("Scheduled. Its session pack is being built for the round.");
      await reload();
    } catch (e) {
      setMessage(String(e));
    }
  };

  return (
    <section className="panel">
      <h3>Jobs</h3>
      {error && <p className="note warn">{error}</p>}
      {jobs?.length === 0 && <p className="note">No jobs yet: upload a job description under Documents.</p>}
      <table className="list">
        <tbody>
          {jobs?.map((j) => (
            <tr key={j.id}>
              <td>
                <b>{j.company}</b> · {j.title}
              </td>
              <td className="muted">{when(j.created_at)}</td>
              <td>
                <PackStatus pack={j.pack} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>Schedule an interview</h3>
      <div className="grid">
        <label>Job</label>
        <select value={jobId} onChange={(e) => setJobId(e.target.value)} disabled={!jobs?.length}>
          {jobs?.map((j) => (
            <option key={j.id} value={j.id}>
              {j.company} · {j.title}
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
        <button className="primary" disabled={!jobs?.length} onClick={schedule}>
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
    </section>
  );
}
