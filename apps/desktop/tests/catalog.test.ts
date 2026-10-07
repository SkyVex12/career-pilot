// The start screen's choices (src/catalog.ts).
import { describe, expect, it } from "vitest";
import type { InterviewItem } from "../src/api";
import { parseTarget, upcoming } from "../src/catalog";

const interview = (id: string, startsAt: string): InterviewItem => ({
  id,
  job_id: `job-${id}`,
  round: "technical",
  starts_at: startsAt,
  pack_id: null,
  company: "Acme",
  title: "Engineer",
});

describe("start screen", () => {
  it("offers interviews from two hours ago on, soonest first", () => {
    const now = Date.parse("2026-10-06T12:00:00Z");
    const list = [interview("later", "2026-10-07T09:00:00Z"), interview("past", "2026-10-06T09:00:00Z"), interview("now", "2026-10-06T11:00:00Z")];
    expect(upcoming(list, now).map((i) => i.id)).toEqual(["now", "later"]);
  });

  it("turns the choice into session.start fields", () => {
    const list = [interview("i1", "2026-10-07T09:00:00Z")];
    expect(parseTarget("interview:i1", list)).toEqual({ interview_id: "i1", job_id: "job-i1" });
    expect(parseTarget("job:j9", list)).toEqual({ job_id: "j9", interview_id: null });
    expect(parseTarget("", list)).toEqual({ job_id: null, interview_id: null });
  });
});
