// The start screen's choices (src/catalog.ts).
import { describe, expect, it } from "vitest";
import type { InterviewItem } from "../src/api";
import { parseTarget, upcoming } from "../src/catalog";
import { cloudUrlsFor } from "../src/urls";

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

describe("cloudUrlsFor", () => {
  it("takes the VM's IP", () => {
    expect(cloudUrlsFor("34.170.1.2")).toEqual({
      cloud_api_url: "http://34.170.1.2/v1",
      cloud_gateway_url: "ws://34.170.1.2/v1/session",
    });
  });
  it("builds both AWS URLs from the stage's host name", () => {
    expect(cloudUrlsFor(" ec2-3-90-1-2.compute-1.amazonaws.com ")).toEqual({
      cloud_api_url: "http://ec2-3-90-1-2.compute-1.amazonaws.com/v1",
      cloud_gateway_url: "ws://ec2-3-90-1-2.compute-1.amazonaws.com/v1/session",
    });
  });
  it("keeps TLS when given an https URL", () => {
    expect(cloudUrlsFor("https://api.example.com/v1")).toEqual({
      cloud_api_url: "https://api.example.com/v1",
      cloud_gateway_url: "wss://api.example.com/v1/session",
    });
  });
  it("needs a host", () => {
    expect(cloudUrlsFor("   ")).toBeNull();
  });
});
