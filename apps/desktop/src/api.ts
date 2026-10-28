// Commands and events of the Rust core (apps/desktop/src-tauri/src/lib.rs). The UI only
// renders: audio, the gateway connection, the Copilot API client and settings storage all
// live in Rust. `rest` below is the API (ARCHITECTURE.md, "REST API") through `api_call`.
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { Channel, LatencyMs, Mode, QuestionType, ServerMessage } from "@copilot/protocol";

export const inTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/** "outdated": the gateway closed with 4426; the app is updating. */
export type StopReason = "ended" | "no_minutes" | "session_active" | "replaced" | "unauthorized" | "outdated";

export type ConnectionState =
  | { state: "connecting"; attempt: number }
  | { state: "live"; session_id: string; resumed: boolean }
  | { state: "reconnecting"; attempt: number; in_ms: number; close_code: number | null }
  | { state: "stopped"; reason: StopReason };

export interface QuestionLatency {
  question_id: string;
  text: string;
  question_type: QuestionType;
  anchor: "speech_end" | "forced" | "expand" | "unknown";
  detected_ms: number | null;
  fast_first_word_ms: number | null;
  deep_first_word_ms: number | null;
  /** What deep_first_word_ms counts from: the question's anchor, or the expand tap. */
  deep_anchor: "speech_end" | "forced" | "expand" | "unknown";
  fast_gateway: LatencyMs | null;
  deep_gateway: LatencyMs | null;
}

// copilot_client::engine::UiEvent
export type UiEvent =
  | { kind: "connection"; state: ConnectionState }
  | { kind: "server"; message: ServerMessage }
  | { kind: "vad"; channel: Channel; speaking: boolean; edge_ms: number }
  | { kind: "latency"; question: QuestionLatency }
  | { kind: "closed"; code: number | null; reason: string }
  | { kind: "warning"; message: string }
  | { kind: "stopped"; reason: StopReason };

export type Environment = "local" | "cloud";

/** The stage's browser sign-in (Google on GCP, Cognito on AWS), from GET /v1/auth/config. */
export interface OAuthConfig {
  provider: string;
  domain: string;
  client_id: string;
  scopes: string[];
}

export interface Settings {
  environment: Environment;
  /** Local: the gateway's /v1/session WebSocket URL and the API's /v1 root. */
  gateway_url: string;
  api_url: string;
  /** Cloud: the deployed stage's (terraform output gateway_url api_url). */
  cloud_gateway_url: string;
  cloud_api_url: string;
  /** Set by sign-in from the stage's GET /v1/auth/config. */
  oauth: OAuthConfig | null;
  mic_device: string | null;
  output_device: string | null;
  echo_cancellation: boolean;
  /** The force-question hotkey, and the overlay's comfort hotkeys (all rebindable). */
  hotkey: string;
  toggle_overlay_hotkey: string;
  click_through_hotkey: string;
  click_through_off_hotkey: string;
  opacity_up_hotkey: string;
  opacity_down_hotkey: string;
  /** Per-monitor overlay frame/opacity/view and the monitor last used: the app manages these,
   * the settings screen sends them back unchanged. */
  layouts: Record<string, MonitorLayout>;
  overlay_monitor: string | null;
  /** The overlay stays above other windows ("Keep on top"). */
  overlay_on_top: boolean;
  show_debug: boolean;
  last_session_id: string | null;
  /** The profile the app acts for (set with `api.setProfile`); null is the account ("Me"). */
  profile: string | null;
  /** Release builds: install updates by themselves when no session runs, or ask first. */
  update_mode: "auto" | "ask";
  /** Kept by the app for the "Updated to" note; the screen sends them back unchanged. */
  last_version: string | null;
  pending_update: string | null;
}

export interface MonitorLayout {
  x: number;
  y: number;
  w: number;
  h: number;
  opacity: number;
  compact: boolean;
}

/** The overlay's comfort hotkeys (src-tauri/src/hotkeys.rs). Each maps to a settings field. */
export type HotkeyAction =
  | "force_question"
  | "toggle_overlay"
  | "click_through"
  | "click_through_off"
  | "opacity_up"
  | "opacity_down";

/** Hotkeys that do not parse, and sets of hotkeys sharing one key (hotkeys.rs `Review`). */
export interface HotkeyReview {
  invalid: { action: HotkeyAction; accelerator: string; error: string }[];
  conflicts: { accelerator: string; actions: HotkeyAction[] }[];
}

/** The overlay's hotkeys shown as chips on its settings menu. */
export interface OverlayHotkeys {
  toggle_overlay: string;
  click_through: string;
  click_through_off: string;
  opacity_up: string;
  opacity_down: string;
}

/** The overlay's live comfort prefs, from `overlay_prefs` and the `copilot://overlay` event. */
export interface OverlayPrefs {
  opacity: number;
  compact: boolean;
  click_through: boolean;
  /** The overlay stays above other windows. */
  on_top: boolean;
  hotkeys: OverlayHotkeys;
}

// src-tauri get_settings
export interface SettingsView {
  settings: Settings;
  /** The app's version (the release tag's, in an installed build). */
  version: string;
  /** An installed release build: pinned to its stage, no local environment or dev token. */
  release: { stage: string; api_url: string } | null;
  dev_token_set: boolean;
  /** The cloud environment's signed-in user (their email), if any. */
  signed_in_as: string | null;
  /** The current environment can make calls: the dev token is set, or the user signed in. */
  ready: boolean;
  /** Plain HTTP or WS to another machine in the current environment. */
  warnings: string[];
  updates: {
    /** A release build: it updates itself. */
    enabled: boolean;
    /** The last update check, Unix milliseconds. */
    checked_at: number | null;
  };
  /** Hotkeys that do not parse, and sets of hotkeys bound to one key. */
  hotkey_review: HotkeyReview;
}

export interface DeviceInfo {
  id: string;
  name: string;
  is_default: boolean;
}


export interface Levels {
  them_rms: number;
  me_rms: number;
  them_speech: number;
  me_speech: number;
  them_underruns: number;
  me_underruns: number;
}

export interface ClientStats {
  connects: number;
  resumes: number;
  new_sessions: number;
  frames_sent: number;
  frames_resent: number;
  frames_dropped: number;
  pings_sent: number;
  token_refreshes: number;
  close_codes: (number | null)[];
}

export interface LiveStatus {
  running: boolean;
  session_id: string | null;
  levels: Levels | null;
  stats: ClientStats | null;
}

export interface StartRequest {
  mode: Mode;
  job_id: string | null;
  interview_id: string | null;
  meeting_title: string | null;
  /** A standup or meeting at a company: its pack and its memory. */
  employer_id?: string | null;
  agenda?: string | null;
  /** A standup or meeting at a company: the company documents to focus on. */
  material_ids?: string[];
  platform: string;
  store_transcript: boolean;
}

/** src-tauri release.rs UpdateState (release builds). */
export type UpdateState =
  | { state: "none" }
  | { state: "checking" }
  /** The last check found nothing newer than this (the running) version. */
  | { state: "up_to_date"; version: string; checked_at: number }
  | { state: "check_failed"; message: string; checked_at: number }
  /** percent: null until the size is known. */
  | { state: "downloading"; version: string; percent: number | null }
  /** Downloaded and verified. auto: installs by itself when no session runs; install_in_s:
   * seconds until it does if nothing changes (null: not by itself, or a session runs). */
  | { state: "ready"; version: string; notes: string | null; auto: boolean; install_in_s: number | null }
  | { state: "installing"; version: string }
  /** The app restarted into this version. */
  | { state: "updated"; version: string }
  /** The server needs a newer app than any that could be installed. */
  | { state: "outdated"; message: string }
  | { state: "failed"; message: string };

/** The Rust side changed the settings on its own (e.g. the sign-in settings at startup). */
export function onSettingsChanged(handler: () => void): Promise<UnlistenFn> {
  return listen("copilot://settings", () => handler());
}

export function onUpdate(handler: (u: UpdateState) => void): Promise<UnlistenFn> {
  return listen<UpdateState>("copilot://update", (e) => handler(e.payload));
}

export const api = {
  getSettings: () => invoke<SettingsView>("get_settings"),
  updateStatus: () => invoke<UpdateState>("update_status"),
  installUpdate: () => invoke<void>("install_update"),
  checkUpdates: () => invoke<void>("check_updates"),
  /** A click, key or scroll (throttled): an automatic update waits for a quiet moment. */
  noteActivity: () => invoke<void>("note_activity"),
  signIn: () => invoke<string>("sign_in"),
  signOut: () => invoke<void>("sign_out"),
  /** The profile switch: every API call and the next session act for it (null: "Me"). */
  setProfile: (profile: string | null) => invoke<void>("set_profile", { profile }),
  saveSettings: (settings: Settings, devToken: string | null) =>
    invoke<void>("save_settings", { settings, devToken }),
  listDevices: () => invoke<{ inputs: DeviceInfo[]; outputs: DeviceInfo[] }>("list_devices"),
  /** `target`: a tailored resume's application, a version whose content it replaces, a label;
   * or the application a job description belongs to. */
  uploadDocument: (kind: DocumentKind, file: File, target?: UploadTarget) =>
    file.arrayBuffer().then((buf) =>
      invoke<DocumentItem>("upload_document", new Uint8Array(buf), {
        headers: {
          "x-kind": kind,
          "x-file-name": encodeURIComponent(file.name),
          ...(target ? { "x-target": encodeURIComponent(JSON.stringify(target)) } : {}),
        },
      }),
    ),
  /** A company document or work log attachment, posted raw to `path` (the API seals it). */
  uploadCompanyFile: (path: string, file: File) =>
    file.arrayBuffer().then((buf) =>
      invoke<CompanyDocument>("upload_company_file", new Uint8Array(buf), {
        headers: { "x-api-path": encodeURIComponent(path), "x-file-name": encodeURIComponent(file.name) },
      }),
    ),
  deleteAccount: () => invoke<{ status: string }>("delete_account"),
  startSession: (request: StartRequest) => invoke<void>("start_session", { request }),
  /** `summarize: false` keeps the transcript only, with no post-call outputs. */
  endSession: (summarize = true) => invoke<void>("end_session", { summarize }),
  endOtherSession: () => invoke<string>("end_other_session"),
  forceQuestion: (text: string | null) => invoke<void>("force_question", { text }),
  /** A suggested reply to what was just said (or to `text`), for a remark that is not a question. */
  respond: (text: string | null) => invoke<void>("respond", { text }),
  expandAnswer: (questionId: string) => invoke<void>("expand_answer", { questionId }),
  sendFeedback: (questionId: string, up: boolean) =>
    invoke<void>("send_feedback", { questionId, up, reason: null }),
  setMode: (mode: Mode) => invoke<void>("set_mode", { mode }),
  /** The session's events so far (compacted), whether one is running, and the next event's n. */
  snapshot: () => invoke<[Numbered[], boolean, number]>("snapshot"),
  liveStatus: () => invoke<LiveStatus>("live_status"),
  showOverlay: (show: boolean) => invoke<void>("show_overlay", { show }),
  /** The overlay's starting opacity, view and click-through (the overlay asks on mount). */
  overlayPrefs: () => invoke<OverlayPrefs>("overlay_prefs"),
  /** Set the overlay's opacity for the monitor it is on (the settings slider). */
  setOverlayOpacity: (value: number) => invoke<void>("set_overlay_opacity", { value }),
  /** Remember the overlay's view (compact or full) for the monitor it is on. */
  setOverlayView: (compact: boolean) => invoke<void>("set_overlay_view", { compact }),
  /** Turn click-through on (clicks pass to the window below). Off is a hotkey, always working. */
  setClickThrough: (on: boolean) => invoke<void>("set_click_through", { on }),
  /** The overlay "Keep on top" toggle. */
  setOverlayOnTop: (on: boolean) => invoke<void>("set_overlay_on_top", { on }),
  /** Bring the main window forward on its Settings screen ("All settings…"). */
  openMainSettings: () => invoke<void>("open_main_settings"),
  /** Quit the whole app. */
  quitApp: () => invoke<void>("quit_app"),
};

/** The overlay's comfort prefs changed (a hotkey, or a move to another monitor). */
export function onOverlayPrefs(handler: (p: OverlayPrefs) => void): Promise<UnlistenFn> {
  return listen<OverlayPrefs>("copilot://overlay", (e) => handler(e.payload));
}

/** The overlay asked the main window to open its Settings screen ("All settings…"). */
export function onOpenSettings(handler: () => void): Promise<UnlistenFn> {
  return listen("copilot://open-settings", () => handler());
}

/// `n` numbers events within a session, from 0.
export interface Numbered {
  n: number;
  event: UiEvent;
}

export function onEvent(handler: (e: Numbered) => void): Promise<UnlistenFn> {
  return listen<Numbered>("copilot://event", (e) => handler(e.payload));
}

// The Copilot API's responses (apps/api/src/copilot_api/routes).

export type DocumentKind = "resume" | "job_description";

export interface DocumentItem {
  id: string;
  kind: DocumentKind | "linkedin_export" | "other";
  content_type: string | null;
  status: "uploaded" | "ingesting" | "ingested" | "failed";
  created_at: string;
  job_id: string | null;
  resume_version_id?: string | null;
}

export interface UploadTarget {
  application_id?: string | null;
  resume_version_id?: string | null;
  label?: string | null;
}

// Applications and resume versions (routes/applications.py, routes/resumes.py).

export type Stage = "applied" | "interview" | "offer" | "rejected";

export interface PairedResume {
  version_id: string;
  kind: "base" | "tailored";
  label: string;
  status: ResumeStatus;
  /** The user chose it; otherwise the automatic rule (its tailored version, else the base). */
  manual: boolean;
}

export interface ApplicationItem {
  id: string;
  company: string;
  role: string;
  url: string | null;
  source_site: string | null;
  stage: Stage;
  note: string | null;
  created_at: string;
  updated_at: string;
  resume: PairedResume | null;
  career_os: { application_id: string; pulled_at: string; content_hash: string | null } | null;
  pack: PackInfo | null;
}

export interface ApplicationDetail extends ApplicationItem {
  jd_text: string;
  /** The versions it may pair with: the base and its own tailored versions. */
  versions: { id: string; kind: "base" | "tailored"; label: string; status: ResumeStatus }[];
  pulled?: "new" | "again";
}

export interface ApplicationFields {
  company: string;
  role: string;
  url: string | null;
  source_site: string | null;
  stage: Stage;
  note: string | null;
  jd_text: string;
}

export type ResumeStatus = "saved" | "extracting" | "extracted" | "failed";

export interface ResumeVersion {
  id: string;
  kind: "base" | "tailored";
  label: string;
  source: "file" | "text" | "career_os";
  status: ResumeStatus;
  application: { id: string; company: string; role: string } | null;
  career_os: { ref: string; pulled_at: string } | null;
  file: { content_type: string } | null;
  created_at: string;
  updated_at: string;
  used_by: string[];
  digest: DigestInfo | null;
  text?: string | null;
}

// Profiles (routes/profiles.py): "Me" and one per Career-OS user an admin link reads.

export interface Profile {
  id: string;
  name: string;
  career_os_user_id: string | null;
  /** The account itself. */
  me: boolean;
}

// The Career-OS link (routes/career_os.py).

export interface CareerOsConnection {
  connected: boolean;
  /** This server has Career-OS set up. */
  available: boolean;
  status?: "active" | "revoked";
  /** The linked Career-OS account: a user, or an admin (who reads their users' applications). */
  user?: CareerOsUser;
  account_type?: "user" | "admin" | "super_admin";
  connected_at?: string | null;
  last_pull_at?: string | null;
}

export interface CareerOsUser {
  id: string;
  name: string;
}

export interface CareerOsItem {
  id: string;
  company: string;
  role: string;
  stage: string;
  source_site: string | null;
  url: string | null;
  created_at: string;
  updated_at: string;
  /** Whose application it is in Career-OS, and the profile it is (or would be) pulled into. */
  user: CareerOsUser | null;
  profile_id: string | null;
  resume: "tailored" | "base" | "none";
  pulled: { application_id: string; pulled_at: string; changed: boolean } | null;
}

export interface CareerOsQuery {
  q?: string;
  stage?: Stage | "";
}

export interface DigestInfo {
  version: string;
  built_at: string | null;
  facts: number;
}

export interface PackInfo {
  pack_id: string;
  available: boolean;
  resume_version_id?: string | null;
  built_at?: string;
  round?: string | null;
  prepared_answers?: number;
  ranked_stories?: number;
}

export interface JobItem {
  id: string;
  company: string;
  title: string;
  url: string | null;
  created_at: string;
  pack: PackInfo | null;
}

export type Round = "phone_screen" | "technical" | "system_design" | "final";

export interface InterviewItem {
  id: string;
  /** The application's id. */
  job_id: string;
  round: Round;
  starts_at: string;
  pack_id: string | null;
  company: string | null;
  title: string | null;
  pack?: PackInfo | null;
}

export type SessionStatus = "live" | "processing" | "ready";

export interface SessionItem {
  id: string;
  mode: Mode;
  title: string | null;
  platform: string | null;
  job_id: string | null;
  interview_id: string | null;
  employer_id?: string | null;
  store_transcript: boolean;
  started_at: string | null;
  ended_at: string | null;
  billed_seconds: number;
  /** "live": the user's words came from the live stream; the others are from before it. */
  me_audio_status: "live" | "pending" | "uploaded" | "transcribed" | "none";
  status: SessionStatus;
}

export type OutputKind = "summary" | "feedback" | "follow_up_email" | "standup_report";

export interface SessionDetail extends Partial<SessionItem> {
  id: string;
  status: SessionStatus;
  outputs: Partial<Record<OutputKind, string>>;
  questions: number;
}

export interface Segment {
  channel: "them" | "me";
  seg_id: number;
  start_ms: number;
  end_ms: number;
  text: string;
}

export interface Usage {
  period: string;
  live_seconds: number;
  live_minutes: number;
  sessions: number;
}

export interface Me {
  id: string;
  email: string;
  display_name: string;
  plan: string;
}

// Company workspaces (apps/api/src/copilot_api/routes/employers.py).

export interface Employer {
  id: string;
  name: string;
  title: string;
  team: string;
  started_on: string | null;
  ended_on: string | null;
  status: "active" | "archived";
  current: boolean;
  created_at: string;
  logs?: number;
  last_log?: string | null;
  documents?: number;
  pending_proposals?: number;
}

export type EmployerFields = Partial<Pick<Employer, "name" | "title" | "team" | "started_on" | "ended_on" | "status">>;

export type WorkplaceKind = "work_item" | "commitment" | "project" | "system" | "person" | "decision" | "term";

export interface MemoryItem {
  id: string;
  kind: WorkplaceKind;
  title: string;
  summary: string;
  body: Record<string, unknown>;
  day: string | null;
  status: "active" | "archived";
  source: { type?: string; ref?: string };
  edited: boolean;
}

export interface Attachment {
  id: string;
  name: string;
  content_type: string;
  size_bytes: number;
}

export interface WorkLog {
  id: string;
  day: string;
  text: string;
  status: "saved" | "extracting" | "extracted" | "failed";
  attachments: Attachment[];
  updated_at: string | null;
  items?: MemoryItem[];
}

export interface CompanyDocument {
  id: string;
  name: string;
  content_type: string;
  size_bytes: number;
  status: "uploaded" | "ingesting" | "ingested" | "failed";
  created_at?: string;
  pending_proposals?: number;
  approved_proposals?: number;
}

export interface Proposal {
  id: string;
  employer_id: string | null;
  company_document_id: string | null;
  document_name: string | null;
  status: "pending" | "approved" | "rejected";
  kind: WorkplaceKind;
  title: string;
  summary: string;
  evidence: string | null;
  body: Record<string, unknown>;
}

export interface PackState {
  mode: "standup" | "meeting";
  status: "none" | "stale" | "ready" | "building";
  pack_id?: string;
  built_at?: string;
  day?: string | null;
  token_count?: number;
}

const call = <T>(method: string, path: string, body?: unknown) =>
  invoke<T>("api_call", { method, path, body: body ?? null });

/** `?q=…&stage=…`, leaving out empty values. */
export function queryString(query: object): string {
  const parts = Object.entries(query)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export const rest = {
  me: () => call<Me>("GET", "/me"),
  usage: () => call<Usage>("GET", "/usage"),
  documents: () => call<{ documents: DocumentItem[]; digest: DigestInfo | null }>("GET", "/documents"),
  jobs: () => call<{ jobs: JobItem[] }>("GET", "/jobs"),
  interviews: () => call<{ interviews: InterviewItem[] }>("GET", "/interviews"),
  applications: () => call<{ applications: ApplicationItem[] }>("GET", "/applications"),
  application: (id: string) => call<ApplicationDetail>("GET", `/applications/${id}`),
  createApplication: (fields: ApplicationFields & { resume_text?: string | null; resume_label?: string | null }) =>
    call<ApplicationDetail>("POST", "/applications", fields),
  updateApplication: (id: string, fields: Partial<ApplicationFields> & { resume_version_id?: string }) =>
    call<ApplicationDetail>("PATCH", `/applications/${id}`, fields),
  deleteApplication: (id: string) => call<null>("DELETE", `/applications/${id}`),
  resumes: () => call<{ base: ResumeVersion | null; versions: ResumeVersion[] }>("GET", "/resumes"),
  resume: (id: string) => call<ResumeVersion>("GET", `/resumes/${id}`),
  createResume: (body: { kind: "base" | "tailored"; application_id?: string | null; label?: string | null; text: string }) =>
    call<ResumeVersion>("POST", "/resumes", body),
  updateResume: (id: string, body: { label?: string; text?: string }) => call<ResumeVersion>("PATCH", `/resumes/${id}`, body),
  deleteResume: (id: string) => call<null>("DELETE", `/resumes/${id}`),
  careerOs: () => call<CareerOsConnection>("GET", "/career-os/connection"),
  connectCareerOs: (code: string) => call<CareerOsConnection>("POST", "/career-os/connect", { code }),
  disconnectCareerOs: () => call<null>("DELETE", "/career-os/connection"),
  careerOsUsers: () => call<{ items: CareerOsUser[] }>("GET", "/career-os/users"),
  profiles: () => call<{ items: Profile[]; career_os: "user" | "admin" | "super_admin" | null }>("GET", "/profiles"),
  searchCareerOs: (query: CareerOsQuery) =>
    call<{ items: CareerOsItem[]; next_cursor: string | null }>("GET", `/career-os/applications${queryString(query)}`),
  pullCareerOs: (id: string) =>
    call<ApplicationDetail & { profile_id: string }>("POST", `/career-os/applications/${encodeURIComponent(id)}/pull`),
  scheduleInterview: (jobId: string, round: Round, startsAt: string) =>
    call<InterviewItem>("POST", "/interviews", { job_id: jobId, round, starts_at: startsAt }),
  sessions: (before?: string | null) =>
    call<{ sessions: SessionItem[]; next_before: string | null }>(
      "GET",
      `/sessions?limit=20${before ? `&before=${encodeURIComponent(before)}` : ""}`,
    ),
  session: (id: string) => call<SessionDetail>("GET", `/sessions/${id}`),
  transcript: (id: string) => call<{ stored: boolean; segments: Segment[] }>("GET", `/sessions/${id}/transcript`),
  deleteSession: (id: string) => call<null>("DELETE", `/sessions/${id}`),

  employers: () => call<{ employers: Employer[] }>("GET", "/employers"),
  createEmployer: (fields: EmployerFields & { name: string }) => call<Employer>("POST", "/employers", fields),
  updateEmployer: (id: string, fields: EmployerFields) => call<Employer>("PATCH", `/employers/${id}`, fields),
  deleteEmployer: (id: string, confirm: string) =>
    call<{ deleted: Record<string, number> }>("DELETE", `/employers/${id}?confirm=${encodeURIComponent(confirm)}`),
  logs: (employerId: string, before?: string | null) =>
    call<{ logs: WorkLog[]; next_before: string | null }>(
      "GET",
      `/employers/${employerId}/logs${before ? `?before=${encodeURIComponent(before)}` : ""}`,
    ),
  putLog: (employerId: string, day: string, text: string) => call<WorkLog>("PUT", `/employers/${employerId}/logs/${day}`, { text }),
  deleteLog: (employerId: string, day: string) => call<null>("DELETE", `/employers/${employerId}/logs/${day}`),
  companyDocuments: (employerId: string) => call<{ documents: CompanyDocument[] }>("GET", `/employers/${employerId}/documents`),
  deleteCompanyDocument: (employerId: string, id: string) => call<null>("DELETE", `/employers/${employerId}/documents/${id}`),
  memory: (employerId: string, kind?: WorkplaceKind) =>
    call<{ memory: MemoryItem[] }>("GET", `/employers/${employerId}/memory${kind ? `?kind=${kind}` : ""}`),
  updateMemory: (id: string, fields: { title?: string; summary?: string; body?: Record<string, unknown> }) =>
    call<MemoryItem>("PATCH", `/memory/${id}`, fields),
  archiveMemory: (id: string) => call<null>("DELETE", `/memory/${id}`),
  proposals: (employerId: string) => call<{ proposals: Proposal[] }>("GET", `/proposals?employer_id=${employerId}`),
  approveProposal: (id: string, edit?: { title?: string; summary?: string }) => call<unknown>("POST", `/proposals/${id}/approve`, edit ?? null),
  rejectProposal: (id: string) => call<unknown>("POST", `/proposals/${id}/reject`),
  decideDocument: (documentId: string, action: "approve" | "reject") =>
    call<{ decided: number }>("POST", "/proposals/bulk", { action, company_document_id: documentId }),
  packs: (employerId: string) => call<{ packs: PackState[] }>("GET", `/employers/${employerId}/packs`),
  ensurePack: (employerId: string, mode: "standup" | "meeting") => call<PackState>("POST", `/employers/${employerId}/packs/${mode}`),
};
