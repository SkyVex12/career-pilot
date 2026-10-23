// What the applications screens decide, apart from rendering (tested in tests/pairing.test.ts).
import type { ApplicationDetail, ApplicationItem, CareerOsItem, ResumeStatus, Stage } from "./api";

export const STAGES: [Stage, string][] = [
  ["applied", "Applied"],
  ["interview", "Interviewing"],
  ["offer", "Offer"],
  ["rejected", "Rejected"],
];
export const stageLabel = (s: string) => STAGES.find(([v]) => v === s)?.[1] ?? s;

/** The resume picker's choices: automatic first (and what it resolves to now), then the base
 * and this application's own tailored versions. Never another application's version. */
export function pairingChoices(app: Pick<ApplicationDetail, "versions" | "resume">): { value: string; label: string }[] {
  const auto = app.resume && !app.resume.manual ? ` (now: ${app.resume.label})` : "";
  const out = [{ value: "auto", label: `Automatic: its tailored resume, else the base${auto}` }];
  for (const v of app.versions) {
    out.push({ value: v.id, label: v.kind === "base" ? `Base resume: ${v.label}` : `Tailored: ${v.label}` });
  }
  return out;
}

/** The picker's current value. */
export function pairingValue(app: Pick<ApplicationItem, "resume">): string {
  return app.resume?.manual ? app.resume.version_id : "auto";
}

/** "Which resume" in the applications list. */
export function resumeLabel(app: Pick<ApplicationItem, "resume">): string {
  if (!app.resume) return "No resume yet";
  const kind = app.resume.kind === "base" ? "Base" : "Tailored";
  return `${kind}: ${app.resume.label}${app.resume.manual ? " (chosen)" : ""}`;
}

export function resumeStatusLabel(status: ResumeStatus): string {
  return { saved: "Saved", extracting: "Reading", extracted: "Ready", failed: "Could not read it" }[status];
}

/** A Career-OS search result as the pull dialog shows it. */
export function pullState(item: Pick<CareerOsItem, "pulled">): { label: string; action: "Pull" | "Pull again" | null } {
  if (!item.pulled) return { label: "", action: "Pull" };
  if (item.pulled.changed) return { label: "Changed in Career-OS since the pull", action: "Pull again" };
  return { label: "Pulled", action: null };
}

/** The start screen's line for an application. */
export function applicationOption(app: Pick<ApplicationItem, "company" | "role" | "stage" | "resume">): string {
  return [app.company, app.role, stageLabel(app.stage), resumeLabel(app)].join(" · ");
}

/** Files read in the app as pasted text; a PDF goes up as a file instead. */
export function isTextFile(name: string): boolean {
  return /\.(txt|md|markdown)$/i.test(name);
}
