// The applications screens' logic (src/pairing.ts).
import { describe, expect, it } from "vitest";
import type { ApplicationDetail } from "../src/api";
import { applicationOption, isTextFile, pairingChoices, pairingValue, pullState, resumeLabel } from "../src/pairing";
import { queryString } from "../src/api";

const detail = (manual: boolean): Pick<ApplicationDetail, "versions" | "resume"> => ({
  resume: { version_id: "t1", kind: "tailored", label: "Globex cut", status: "extracted", manual },
  versions: [
    { id: "b1", kind: "base", label: "Base resume", status: "extracted" },
    { id: "t1", kind: "tailored", label: "Globex cut", status: "extracted" },
  ],
});

describe("resume pairing", () => {
  it("offers automatic, the base and the application's own versions", () => {
    const choices = pairingChoices(detail(false));
    expect(choices.map((c) => c.value)).toEqual(["auto", "b1", "t1"]);
    expect(choices[0].label).toContain("now: Globex cut");
    expect(pairingChoices(detail(true))[0].label).not.toContain("now:");
  });

  it("shows the automatic choice unless the user picked one", () => {
    expect(pairingValue(detail(false))).toBe("auto");
    expect(pairingValue(detail(true))).toBe("t1");
    expect(resumeLabel(detail(true))).toBe("Tailored: Globex cut (chosen)");
    expect(resumeLabel({ resume: null })).toBe("No resume yet");
  });
});

describe("Career-OS pull", () => {
  it("offers a pull, a pull again after a change, or nothing", () => {
    expect(pullState({ pulled: null })).toEqual({ label: "", action: "Pull" });
    const pulled = { application_id: "a", pulled_at: "2026-10-22T10:00:00Z", changed: false };
    expect(pullState({ pulled }).action).toBeNull();
    expect(pullState({ pulled: { ...pulled, changed: true } }).action).toBe("Pull again");
  });

  it("builds the search query without empty values", () => {
    expect(queryString({ q: "acme corp", stage: "", from: "2026-10-01", to: undefined })).toBe("?q=acme%20corp&from=2026-10-01");
    expect(queryString({})).toBe("");
  });
});

describe("start screen", () => {
  it("names the application with its stage and resume", () => {
    const app = { company: "Globex", role: "Staff Engineer", stage: "interview" as const, resume: detail(false).resume };
    expect(applicationOption(app)).toBe("Globex · Staff Engineer · Interviewing · Tailored: Globex cut");
  });

  it("reads text files in the app and uploads the rest", () => {
    expect(isTextFile("jd.md")).toBe(true);
    expect(isTextFile("resume.PDF")).toBe(false);
  });
});
