// What the start screen offers from the API: upcoming interviews and jobs.
import type { InterviewItem } from "./api";

/// Interviews not long past (a pack lives until a day after its interview), soonest first.
export function upcoming(interviews: InterviewItem[], now = Date.now()): InterviewItem[] {
  return interviews
    .filter((i) => new Date(i.starts_at).getTime() > now - 2 * 3600_000)
    .sort((a, b) => new Date(a.starts_at).getTime() - new Date(b.starts_at).getTime());
}

/// The start screen's "Call for" value, `interview:<id>`, `job:<id>`, or "" for the mode's
/// pack, as `session.start` fields. An interview sends its job too.
export function parseTarget(
  value: string,
  interviews: Pick<InterviewItem, "id" | "job_id">[],
): { job_id: string | null; interview_id: string | null } {
  const [kind, id] = value.split(":");
  if (kind === "interview" && id) return { interview_id: id, job_id: interviews.find((i) => i.id === id)?.job_id ?? null };
  if (kind === "job" && id) return { job_id: id, interview_id: null };
  return { job_id: null, interview_id: null };
}
