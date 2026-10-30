// Shared data for the screen tests (fictional).
import type { ApplicationDetail, ApplicationItem, Employer, InterviewItem, ResumeVersion, Settings, SettingsView } from "../src/api";

export const iso = (minFromNow: number) => new Date(Date.now() + minFromNow * 60_000).toISOString();

export function app(id: string, company: string, role: string, stage: ApplicationItem["stage"] = "applied"): ApplicationItem {
  return {
    id,
    company,
    role,
    url: null,
    source_site: null,
    stage,
    note: null,
    created_at: iso(-1000),
    updated_at: iso(-100),
    resume: { version_id: "rb", kind: "base", label: "Base", status: "extracted", manual: false },
    career_os: null,
    pack: { pack_id: "p", available: true, prepared_answers: 1 },
  };
}

export function detail(a: ApplicationItem): ApplicationDetail {
  return { ...a, jd_text: "We build routing.", versions: [{ id: "rb", kind: "base", label: "Base", status: "extracted" }] };
}

export function interview(id: string, jobId: string, company: string, inMin: number): InterviewItem {
  return { id, job_id: jobId, round: "technical", starts_at: iso(inMin), pack_id: "p", company, title: "Engineer", pack: { pack_id: "p", available: true } };
}

export function resume(id: string, kind: "base" | "tailored", label: string, appRef: ResumeVersion["application"] = null): ResumeVersion {
  return {
    id,
    kind,
    label,
    source: "text",
    status: "extracted",
    application: appRef,
    career_os: null,
    file: null,
    created_at: iso(-1000),
    updated_at: iso(-100),
    used_by: appRef ? [appRef.id] : [],
    digest: { version: "d", built_at: iso(-100), facts: 12 },
  };
}

export function employer(id: string, name: string, over: Partial<Employer> = {}): Employer {
  return { id, name, title: "Engineer", team: "Routing", started_on: null, ended_on: null, status: "active", current: true, created_at: iso(-1000), pending_proposals: 0, ...over };
}

export const SETTINGS: Settings = {
  environment: "cloud",
  gateway_url: "",
  api_url: "",
  cloud_gateway_url: "ws://stage.example.com/v1/session",
  cloud_api_url: "http://stage.example.com/v1",
  oauth: null,
  mic_device: null,
  output_device: null,
  echo_cancellation: true,
  hotkey: "CommandOrControl+Shift+Space",
  toggle_overlay_hotkey: "CommandOrControl+Shift+O",
  click_through_hotkey: "CommandOrControl+Shift+T",
  click_through_off_hotkey: "CommandOrControl+Shift+G",
  opacity_up_hotkey: "CommandOrControl+Shift+Up",
  opacity_down_hotkey: "CommandOrControl+Shift+Down",
  layouts: {},
  overlay_monitor: null,
  overlay_on_top: true,
  show_debug: false,
  last_session_id: null,
  profile: null,
  update_mode: "auto",
  last_version: null,
  pending_update: null,
};

export function settingsView(over: Partial<SettingsView> = {}): SettingsView {
  return {
    settings: SETTINGS,
    version: "0.1.16",
    release: { stage: "stage0", api_url: "http://stage.example.com/v1" },
    dev_token_set: false,
    signed_in_as: "alex@example.com",
    ready: true,
    warnings: [],
    updates: { enabled: true, checked_at: Date.now() },
    hotkey_review: { invalid: [], conflicts: [] },
    ...over,
  };
}
