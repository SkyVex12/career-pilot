// Company workspaces in the UI (src/workday.ts).
import { describe, expect, it } from "vitest";
import type { Employer, MemoryItem } from "../src/api";
import { byKind, defaultEmployer, itemLine, localDay, packLabel } from "../src/workday";

const employer = (id: string, over: Partial<Employer> = {}): Employer => ({
  id,
  name: id,
  title: "",
  team: "",
  started_on: null,
  ended_on: null,
  status: "active",
  current: true,
  created_at: "2026-10-01T00:00:00Z",
  ...over,
});

const item = (kind: MemoryItem["kind"], title: string, body: Record<string, unknown> = {}, summary = title): MemoryItem => ({
  id: title,
  kind,
  title,
  summary,
  body,
  day: null,
  status: "active",
  source: {},
  edited: false,
});

describe("work log and company picker", () => {
  it("uses the local date", () => {
    expect(localDay(new Date(2026, 9, 5, 23, 30))).toBe("2026-10-05");
  });

  it("picks the last used company, else the current one", () => {
    const list = [employer("old", { current: false, ended_on: "2025-01-01" }), employer("now"), employer("gone", { status: "archived" })];
    expect(defaultEmployer(list, "old")).toBe("old");
    expect(defaultEmployer(list, "gone")).toBe("now");
    expect(defaultEmployer(list, null)).toBe("now");
    expect(defaultEmployer([], null)).toBe("");
  });

  it("says a missing pack does not block the call", () => {
    expect(packLabel(null).tone).toBe("off");
    expect(packLabel({ mode: "meeting", status: "building" }).text).toMatch(/can start now/);
    expect(packLabel({ mode: "standup", status: "ready" }).tone).toBe("ok");
  });

  it("shows facts as lines, grouped by kind", () => {
    expect(itemLine(item("work_item", "Label fix", { state: "in_progress" }, "Retries back off"))).toBe("Label fix: Retries back off [in progress]");
    expect(itemLine(item("commitment", "Send the plan", { state: "open", due_date: "2026-10-23" }))).toBe("Send the plan · due 2026-10-23");
    const groups = byKind([item("person", "Priya"), item("work_item", "A"), item("person", "Sam")]);
    expect(groups.map(([k, l]) => [k, l.length])).toEqual([
      ["work_item", 1],
      ["person", 2],
    ]);
  });
});
