// Commands and events of the Rust core (apps/desktop/src-tauri/src/lib.rs). The UI only
// renders: audio, the gateway connection, the Copilot API client and settings storage all
// live in Rust. `rest` below is the API (ARCHITECTURE.md, "REST API") through `api_call`.
import { invoke } from "@tauri-apps/api/core";
import type { CodeSent } from "./emailCode";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { AnswerStyle, Channel, LatencyMs, Mode, QuestionType, ServerMessage } from "@copilot/protocol";

export const inTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/** "outdated": the gateway closed with 4426; the app is updating. */
export type StopReason = "ended" | "no_minutes" | "session_active" | "replaced" | "unauthorized" | "outdated" | "seat_removed" | "seat_closed" | "left";

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

// copilot_client::engine::UiEvent, plus the desktop's profile boundary reset.
export type UiEvent =
  | { kind: "context_reset" }
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
  /** Whether the stage also signs in with an emailed code; null until the app has asked it. */
  email_sign_in: boolean | null;
  /** Whether the stage also signs in with a Career-OS account; null until the app has asked it. */
  career_os_sign_in: boolean | null;
  mic_device: string | null;
  output_device: string | null;
  echo_cancellation: boolean;
  /** The force-question hotkey, and the overlay's comfort hotkeys (all rebindable). */
  hotkey: string;
  toggle_overlay_hotkey: string;
  toggle_stealth_hotkey: string;
  click_through_hotkey: string;
  click_through_off_hotkey: string;
  opacity_up_hotkey: string;
  opacity_down_hotkey: string;
  /** Coding mode: a shot of the window in front into the tray, and the tray sent to be solved. */
  add_shot_hotkey: string;
  solve_hotkey: string;
  /** Coding mode's answer mode and the language picked for the code ("" lets the screen decide);
   * the overlay sets these (`api.setCodingPrefs`), the settings screen sends them back unchanged. */
  coding_mode: CodingMode;
  coding_language: string;
  /** Per-monitor overlay frame/opacity/view and the monitor last used: the app manages these,
   * the settings screen sends them back unchanged. */
  layouts: Record<string, MonitorLayout>;
  overlay_monitor: string | null;
  /** The overlay stays above other windows ("Keep on top"). */
  overlay_on_top: boolean;
  /** Leave every app window out of screen capture while it stays visible here. */
  stealth: boolean;
  /** Window title Task Manager shows while Stealth is on. The built program name stays Copilot Desktop. */
  display_name: string;
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
  | "toggle_stealth"
  | "click_through"
  | "click_through_off"
  | "opacity_up"
  | "opacity_down"
  | "add_shot"
  | "solve";

/** Coding mode's answer mode: a call is on (say it, then type it), or an online test (paste it). */
export type CodingMode = "interview" | "assessment";

/** The shots one screenshot question was asked with (lib.rs `SentShots`). */
export interface SentShots {
  question_id: string;
  shots: ShotView[];
}

/** A shot waiting in the coding tray (src-tauri/src/shots.rs `ShotView`). */
export interface ShotView {
  id: number;
  /** The image itself, as a data URL. */
  url: string;
  width: number;
  height: number;
  title: string | null;
  bytes: number;
  source: "window" | "monitor";
  capture_ms: number;
  encode_ms: number;
}

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
  /** How they signed in: an emailed code, Career-OS, or the browser (Google or Cognito). */
  signed_in_with: "email" | "career_os" | "browser" | null;
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
  spoken_language?: string;
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
  /** How the detailed answer is written (the Session tab). */
  answer_style?: AnswerStyle;
  /** An online test with no call (coding mode): no audio, nothing transcribed. */
  no_call?: boolean;
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

/** The audio test's result per side (audio::check in the Rust core). */
export interface AudioVerdict {
  ok: boolean;
  message: string;
  peak_db: number;
  sound_ms: number;
}
export type AudioPart = "mic" | "call";
export interface AudioCheckResult {
  mic: AudioVerdict;
  call: AudioVerdict;
}
/** The live meter while the test runs: "speak" first, then "tone" on the output. */
export interface AudioCheckMeter {
  phase: "speak" | "tone";
  mic_db: number;
  call_db: number;
}

export function onAudioCheck(handler: (m: AudioCheckMeter) => void): Promise<UnlistenFn> {
  return listen<AudioCheckMeter>("copilot://audio-check", (e) => handler(e.payload));
}

/** The on-demand server (src-tauri wake): the VM stops itself when idle and starts when the
 * app needs it, which takes a minute or two. */
export type ServerState =
  | { state: "starting"; detail: string | null }
  | { state: "ready"; detail: null }
  | { state: "failed"; detail: string };

export function onServer(handler: (s: ServerState) => void): Promise<UnlistenFn> {
  return listen<ServerState>("copilot://server", (e) => handler(e.payload));
}

export function onUpdate(handler: (u: UpdateState) => void): Promise<UnlistenFn> {
  return listen<UpdateState>("copilot://update", (e) => handler(e.payload));
}

export const api = {
  getSettings: () => invoke<SettingsView>("get_settings"),
  wakeServer: () => invoke<void>("wake_server"),
  updateStatus: () => invoke<UpdateState>("update_status"),
  installUpdate: () => invoke<void>("install_update"),
  checkUpdates: () => invoke<void>("check_updates"),
  /** A click, key or scroll (throttled): an automatic update waits for a quiet moment. */
  noteActivity: () => invoke<void>("note_activity"),
  signIn: () => invoke<string>("sign_in"),
  /** Email sign-in: mail a code to the address. Rejects with an EmailFailure (emailCode.ts). */
  emailSignInStart: (email: string) => invoke<CodeSent>("email_sign_in_start", { email }),
  /** Email sign-in: the code for the sign-in. Resolves to the address; rejects like the above. */
  emailSignInVerify: (email: string, code: string) => invoke<string>("email_sign_in_verify", { email, code }),
  /** Career-OS sign-in: the browser page, then either a sign-in or a choice the screen shows. */
  careerOsSignIn: () => invoke<CareerOsOutcome>("career_os_sign_in"),
  /** The answer to a Career-OS choice. `existing` uses the sign-in now on this computer as proof. */
  careerOsComplete: (ticket: string, choice: "existing" | "new", accountId: string | null) =>
    invoke<CareerOsOutcome>("career_os_complete", { ticket, choice, accountId }),
  signOut: () => invoke<void>("sign_out"),
  /** The profile switch: every API call and the next session act for it (null: "Me"). */
  setProfile: (profile: string | null) => invoke<void>("set_profile", { profile }),
  saveSettings: (settings: Settings, devToken: string | null) =>
    invoke<void>("save_settings", { settings, devToken }),
  listDevices: () => invoke<{ inputs: DeviceInfo[]; outputs: DeviceInfo[] }>("list_devices"),
  /** About five seconds: speak for three, then a test sound plays on the output. */
  /** "mic" listens only, "call" plays the test tone only; without a part, both in turn. */
  audioCheck: (micDevice: string | null, outputDevice: string | null, part?: AudioPart) =>
    invoke<AudioCheckResult>("audio_check", { micDevice, outputDevice, part: part ?? null }),
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
  /** Take over the profile's running call in this app (Session and History, "Live now"). */
  openSession: (live: LiveSession) =>
    invoke<void>("open_session", {
      request: {
        session_id: live.id,
        mode: live.mode,
        platform: live.platform ?? "",
        next_frame: [Math.max(0, live.next_frame.them), Math.max(0, live.next_frame.me)],
        me_audio: live.me_audio,
        no_call: live.no_call ?? false,
        elapsed_ms: live.started_at ? Math.max(0, Date.now() - Date.parse(live.started_at)) : 0,
      },
    }),
  /** End the profile's running call wherever it runs; `profile` is the app's (null: "Me"). */
  endLiveSession: (sessionId: string, profile: string | null) => invoke<string>("end_live_session", { sessionId, profile }),
  forceQuestion: (text: string | null) => invoke<void>("force_question", { text }),
  /** Coding mode: a shot of the window in front into the tray; Solve sends the tray (or one new
   * shot) as one problem; the tray's shots; take one out; the answer mode and language. */
  codingAddShot: () => invoke<void>("coding_add_shot"),
  codingSolve: () => invoke<void>("coding_solve"),
  codingShots: () => invoke<ShotView[]>("coding_shots"),
  /** The shots of the last problems sent, by question, for their cards. */
  codingSentShots: () => invoke<SentShots[]>("coding_sent_shots"),
  codingRemoveShot: (id: number) => invoke<void>("coding_remove_shot", { id }),
  setCodingPrefs: (mode: CodingMode, language: string) => invoke<void>("set_coding_prefs", { mode, language }),
  /** A suggested reply to what was just said (or to `text`), for a remark that is not a question. */
  respond: (text: string | null) => invoke<void>("respond", { text }),
  expandAnswer: (questionId: string) => invoke<void>("expand_answer", { questionId }),
  sendFeedback: (questionId: string, up: boolean) =>
    invoke<void>("send_feedback", { questionId, up, reason: null }),
  setMode: (mode: Mode) => invoke<void>("set_mode", { mode }),
  setSpokenLanguage: (language: string, requestId: string) => invoke<void>("set_spoken_language", { language, requestId }),
  setAnswerStyle: (style: AnswerStyle) => invoke<void>("set_answer_style", { style }),
  // Guest seats (ARCHITECTURE.md, "Guest seats"). The host: Ask AI on a note, and pause or
  // resume what guests see. The guest: join an admitted seat, leave it, send a note.
  askNote: (noteId: string, text: string) => invoke<void>("ask_note", { noteId, text }),
  setSharing: (on: boolean) => invoke<void>("set_sharing", { on }),
  seatJoin: (sessionId: string, seatId: string) => invoke<void>("seat_join", { request: { session_id: sessionId, seat_id: seatId } }),
  seatLeave: () => invoke<void>("seat_leave"),
  seatNote: (text: string) => invoke<string>("seat_note", { text }),
  // Guest seats' voice: what Windows says each output device is (the window plays voice).
  voiceDevices: () => invoke<VoiceDevice[]>("voice_devices"),
  /** The session's events so far (compacted), whether one is running, and the next event's n. */
  snapshot: () => invoke<[Numbered[], boolean, number]>("snapshot"),
  liveStatus: () => invoke<LiveStatus>("live_status"),
  showOverlay: (show: boolean) => invoke<void>("show_overlay", { show }),
  /** Preferences for the main workspace window. The live overlay remains independent. */
  windowPrefs: () => invoke<OverlayPrefs>("window_prefs"),
  setWindowOpacity: (value: number) => invoke<void>("set_window_opacity", { value }),
  setWindowOnTop: (on: boolean) => invoke<void>("set_window_on_top", { on }),
  setWindowClickThrough: (on: boolean) => invoke<void>("set_window_click_through", { on }),
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
  /** Return from the overlay to the main Call screen. */
  openMainCall: () => invoke<void>("open_main_call"),
  /** Show one session-bound consent prompt without changing saved window preferences. */
  screenControlPrompt: (sessionId: string, requestId: string, active: boolean, durationMs: number) =>
    invoke<void>("screen_control_prompt", { sessionId, requestId, active, durationMs }),
  /** Minimize the overlay (it gets a taskbar button until it is restored). */
  minimizeOverlay: () => invoke<void>("minimize_overlay"),
  /** Quit the whole app. */
  quitApp: () => invoke<void>("quit_app"),
};

/** Main-window preferences changed through quick settings or a native hotkey. */
export function onWorkspaceWindowPrefs(handler: (p: OverlayPrefs) => void): Promise<UnlistenFn> {
  return listen<OverlayPrefs>("copilot://workspace-window", (e) => handler(e.payload));
}

/** The overlay's comfort prefs changed (a hotkey, or a move to another monitor). */
export function onOverlayPrefs(handler: (p: OverlayPrefs) => void): Promise<UnlistenFn> {
  return listen<OverlayPrefs>("copilot://overlay", (e) => handler(e.payload));
}

/** The overlay asked the main window to open its Settings screen ("All settings…"). */
export function onOpenSettings(handler: () => void): Promise<UnlistenFn> {
  return listen("copilot://open-settings", () => handler());
}

/** A native call transition asks the workspace to show its Call screen. */
export function onOpenCall(handler: () => void): Promise<UnlistenFn> {
  return listen("copilot://open-call", () => handler());
}

/// `n` numbers events within a session, from 0.
export interface Numbered {
  n: number;
  event: UiEvent;
}

/** The coding tray changed; and what went wrong with a hotkey's shot. */
export function onShots(handler: (shots: ShotView[]) => void): Promise<UnlistenFn> {
  return listen<ShotView[]>("copilot://coding-shots", (e) => handler(e.payload));
}
export function onShotsSent(handler: (sent: SentShots) => void): Promise<UnlistenFn> {
  return listen<SentShots>("copilot://coding-sent", (e) => handler(e.payload));
}
export function onShotError(handler: (message: string) => void): Promise<UnlistenFn> {
  return listen<string>("copilot://coding-error", (e) => handler(e.payload));
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
  /** The stage requires a link before the app does anything else (Settings stays open). */
  required?: boolean;
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
  pack_id: string | null;
  available: boolean;
  operation_state?: "queued" | "running" | "succeeded" | "failed" | "idle";
  availability?: "current" | "missing" | "expired";
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
  revision?: number;
  id: string;
  status: SessionStatus;
  outputs: Partial<Record<OutputKind, string>>;
  questions: number;
  /** Explicitly saved personal notes; omitted by older API deployments. */
  notes?: string;
}

/** A native output device, including the call route and current system default. */
export interface VoiceDevice {
  id: string;
  name: string;
  form_factor: "headphones" | "headset" | "speakers" | "other" | "unknown";
  private: boolean;
  call: boolean;
  system_default?: boolean;
}

/** A guest seat as the host lists it (GET /sessions/{id}/seats). */
export interface HostSeat {
  seat_id: string;
  name: string;
  email: string;
  status: "invited" | "accepted" | "admitted" | "declined" | "removed" | "expired" | "ended";
  capabilities: string[];
  created_at: string | null;
  accepted_at: string | null;
  admitted_at: string | null;
}

/** An open seat of this account on someone else's live call (GET /seats). */
export interface MySeat {
  seat_id: string;
  session_id: string;
  host_name: string;
  mode: Mode;
  title: string | null;
  status: "invited" | "accepted" | "admitted";
  capabilities: string[];
  expires_at: string | null;
}

/** GET /sessions/live: the profile's call still running on the gateway. */
export interface LiveSession {
  id: string;
  mode: Mode;
  title: string | null;
  platform: string | null;
  started_at: string | null;
  /** False while the gateway waits for an app to come back. */
  connected: boolean;
  next_frame: { them: number; me: number };
  me_audio: boolean;
  /** An online test with no call (coding mode); absent from older stages. */
  no_call?: boolean;
}

export interface Segment {
  channel: "them" | "me";
  seg_id: number;
  start_ms: number;
  end_ms: number;
  text: string;
}

/** One pass of a question's answer, as it was shown in the overlay. */
export interface ReplayPass {
  text: string;
  source: "model" | "prepared";
  refs: { node_id: string; label: string }[];
  expanded: boolean;
}

/** A question the overlay showed, timed from the start of the call. */
export interface ReplayQuestion {
  id: string;
  text: string;
  type: string;
  forced: boolean;
  offset_ms: number;
  feedback: "up" | "down" | null;
  fast: ReplayPass | null;
  deep: ReplayPass | null;
}

/** GET /sessions/{id}/replay: the call as the overlay showed it. */
export interface Replay {
  session_id: string;
  stored: boolean;
  started_at: string | null;
  segments: Segment[];
  questions: ReplayQuestion[];
}

export interface Usage {
  period: string;
  period_start?: string;
  period_end?: string;
  as_of?: string;
  last_recorded_at?: string | null;
  /** Compatibility display totals include historical estimates; never use them for billing. */
  live_seconds: number;
  live_minutes: number;
  sessions: number;
  tracking_only?: boolean;
  measurement?: "connected-v1";
  measured_seconds?: number;
  measured_sessions?: number;
  legacy_estimated_seconds?: number;
  legacy_sessions?: number;
  legacy_billable?: false;
  /** estimated_minor already includes adjustment_minor. Currencies are never combined. */
  estimated_costs?: { currency: string; estimated_minor: number; adjustment_minor: number }[];
  prices?: { currency: string; price_version: string; rate_minor_per_hour: number; measured_seconds: number }[];
  days?: { day: string; live_seconds: number; measured_seconds: number; legacy_estimated_seconds: number }[];
}

export interface Me {
  id: string;
  email: string;
  display_name: string;
  plan: string;
  /** How this account signs in. Career-OS cannot be disconnected while it is the only one. */
  sign_in_methods?: { provider: string; label: string; added_at: string }[];
}

/** One account on the Career-OS choice screen. `id` is present when several accounts match. */
export interface CareerOsAccount {
  id?: string;
  email?: string;
  methods: string[];
  created_at: string;
}

/** `POST /auth/career-os/sign-in` and `/auth/career-os/complete`. */
export type CareerOsOutcome =
  | { status: "signed_in"; name: string; account: string }
  | { status: "added" }
  | { status: "choose_account"; ticket: string; expires_in_s: number; accounts: CareerOsAccount[] }
  | { status: "existing_or_new"; ticket: string; expires_in_s: number; verified: boolean; account: CareerOsAccount | null };

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

export type WorkplaceKind = "work_item" | "commitment" | "project" | "system" | "person" | "decision" | "term" | "flow" | "constraint" | "risk";

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
  revision?: number;
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
  /// Engineering context: how many searchable sections and facts the file gave, why it
  /// could not be read, and the earlier upload it replaces once it is read.
  sections?: number;
  facts?: number;
  error?: string | null;
  replaces?: string | null;
}

/// What one file gave: its sections' headings and the facts taken from it.
export interface DocumentDetail {
  id: string;
  name: string;
  status: CompanyDocument["status"];
  error: string | null;
  sections: number;
  headings: string[];
  facts: MemoryItem[];
}

/// "Test a question": pending until a worker has answered, then the type a standup would
/// give the question, the answer and where each cited fact came from.
export interface AskResult {
  ask_id: string;
  status: "pending" | "done" | "failed";
  type?: string;
  answer?: string;
  sources?: { kind: "fact" | "section"; label: string }[];
  error?: string;
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

export type ReadPolicy = { refresh?: boolean; wake?: boolean };
export const POLL_READ: ReadPolicy = { refresh: true, wake: false };
export const request = <T>(method: string, path: string, body?: unknown, policy?: ReadPolicy) =>
  invoke<T>("api_call", { method, path, body: body ?? null,
    ...(method === "GET" && typeof document !== "undefined" && document.visibilityState === "hidden" ? { wake: false } : {}), ...policy });
const call = request;

/** `?q=…&stage=…`, leaving out empty values. */
export function queryString(query: object): string {
  const parts = Object.entries(query)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export const rest = {
  me: () => call<Me>("GET", "/me"),
  usage: (policy?: ReadPolicy) => call<Usage>("GET", "/usage", undefined, { ...policy, refresh: true }),
  documents: () => call<{ documents: DocumentItem[]; digest: DigestInfo | null }>("GET", "/documents"),
  jobs: () => call<{ jobs: JobItem[] }>("GET", "/jobs"),
  interviews: (policy?: ReadPolicy) => call<{ interviews: InterviewItem[] }>("GET", "/interviews", undefined, policy),
  applications: (policy?: ReadPolicy) => call<{ applications: ApplicationItem[] }>("GET", "/applications", undefined, policy),
  catalogStatus: () => call<{
    applications: { id: string; resume_status: ResumeStatus | null; pack: Pick<PackInfo, "available" | "availability" | "operation_state"> }[];
    interviews: { id: string; pack: Pick<PackInfo, "available" | "availability" | "operation_state"> }[];
    resumes: { id: string; status: ResumeStatus }[];
    documents: { id: string; status: string }[];
  }>("GET", "/applications/status", undefined, POLL_READ),
  application: (id: string, policy?: ReadPolicy) => call<ApplicationDetail>("GET", `/applications/${id}`, undefined, policy),
  createApplication: (fields: ApplicationFields & { resume_text?: string | null; resume_label?: string | null }) =>
    call<ApplicationDetail>("POST", "/applications", fields),
  updateApplication: (id: string, fields: Partial<ApplicationFields> & { resume_version_id?: string }) =>
    call<ApplicationDetail>("PATCH", `/applications/${id}`, fields),
  deleteApplication: (id: string) => call<null>("DELETE", `/applications/${id}`),
  resumes: (policy?: ReadPolicy) => call<{ base: ResumeVersion | null; versions: ResumeVersion[] }>("GET", "/resumes", undefined, policy),
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
  sessions: (before?: string | null, policy?: ReadPolicy, cursor?: string | null) =>
    call<{ sessions: SessionItem[]; next_before: string | null; next_cursor?: string | null }>(
      "GET",
      `/sessions?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : before ? `&before=${encodeURIComponent(before)}` : ""}`, undefined, policy,
    ),
  liveSessions: (policy?: ReadPolicy) => call<{ sessions: LiveSession[] }>("GET", "/sessions/live", undefined, policy),
  session: (id: string, policy?: ReadPolicy) => call<SessionDetail>("GET", `/sessions/${id}`, undefined, policy),
  sessionStatus: (id: string) => call<{ id: string; status: SessionDetail["status"]; revision: number }>("GET", `/sessions/${id}/status`, undefined, POLL_READ),
  saveSessionNotes: (id: string, text: string, expectedRevision?: number) => call<{ notes: string; revision?: number }>("PUT", `/sessions/${id}/notes`, { text, ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }) }),
  transcript: (id: string) => call<{ stored: boolean; segments: Segment[] }>("GET", `/sessions/${id}/transcript`),
  replay: (id: string, policy?: ReadPolicy) => call<Replay>("GET", `/sessions/${id}/replay`, undefined, policy),
  deleteSession: (id: string) => call<null>("DELETE", `/sessions/${id}`),
  /** Invite with what the guest may do besides watching (`view` is always included). */
  inviteGuest: (sessionId: string, email: string, capabilities: string[]) =>
    call<{ status: string }>("POST", `/sessions/${sessionId}/seats`, { email, capabilities: ["view", ...capabilities.filter((c) => c !== "view")] }),
  /** The call's seats, and whether this server can carry guests' voice and the host's screen. */
  seatsOf: (sessionId: string) => call<{ seats: HostSeat[]; voice?: boolean; screen?: boolean }>("GET", `/sessions/${sessionId}/seats`),
  admitGuest: (sessionId: string, seatId: string) => call<HostSeat>("PATCH", `/sessions/${sessionId}/seats/${seatId}`, { admit: true }),
  setGuestCapabilities: (sessionId: string, seatId: string, capabilities: string[], expectedCapabilities?: string[]) =>
    call<HostSeat>("PATCH", `/sessions/${sessionId}/seats/${seatId}`, { capabilities, ...(expectedCapabilities ? { expected_capabilities: expectedCapabilities } : {}) }),
  removeGuest: (sessionId: string, seatId: string) => call<null>("DELETE", `/sessions/${sessionId}/seats/${seatId}`),
  hostVoice: (sessionId: string) => call<{ url: string; token: string }>("POST", `/sessions/${sessionId}/voice`),
  guestVoice: (seatId: string) => call<{ url: string; token: string }>("POST", `/seats/${seatId}/voice`),
  hostScreen: (sessionId: string) => call<{ url: string; token: string }>("POST", `/sessions/${sessionId}/screen`),
  guestScreen: (seatId: string) => call<{ url: string; token: string; control: boolean }>("POST", `/seats/${seatId}/screen`),
  mySeats: () => call<{ seats: MySeat[] }>("GET", "/seats", undefined, POLL_READ),
  acceptSeat: (seatId: string) => call<{ status: string }>("POST", `/seats/${seatId}/accept`),
  declineSeat: (seatId: string) => call<{ status: string }>("POST", `/seats/${seatId}/decline`),

  employers: (policy?: ReadPolicy) => call<{ employers: Employer[] }>("GET", "/employers", undefined, policy),
  createEmployer: (fields: EmployerFields & { name: string }) => call<Employer>("POST", "/employers", fields),
  updateEmployer: (id: string, fields: EmployerFields) => call<Employer>("PATCH", `/employers/${id}`, fields),
  deleteEmployer: (id: string, confirm: string) =>
    call<{ deleted: Record<string, number> }>("DELETE", `/employers/${id}?confirm=${encodeURIComponent(confirm)}`),
  workStatus: (employerId: string) => call<{ revision: number; work_logs: { id: string; status: string }[]; documents: { id: string; status: string }[]; pending: boolean }>("GET", `/employers/${employerId}/status`, undefined, POLL_READ),
  logs: (employerId: string, before?: string | null, policy?: ReadPolicy) =>
    call<{ logs: WorkLog[]; next_before: string | null }>(
      "GET",
      `/employers/${employerId}/logs${before ? `?before=${encodeURIComponent(before)}` : ""}`, undefined, policy,
    ),
  log: (employerId: string, day: string, policy?: ReadPolicy) => call<WorkLog>("GET", `/employers/${employerId}/logs/${day}`, undefined, policy),
  putLog: (employerId: string, day: string, text: string, expectedRevision?: number) => call<WorkLog>("PUT", `/employers/${employerId}/logs/${day}`, { text, ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }) }),
  deleteLog: (employerId: string, day: string) => call<null>("DELETE", `/employers/${employerId}/logs/${day}`),
  companyDocuments: (employerId: string, policy?: ReadPolicy) => call<{ documents: CompanyDocument[] }>("GET", `/employers/${employerId}/documents`, undefined, policy),
  deleteCompanyDocument: (employerId: string, id: string) => call<null>("DELETE", `/employers/${employerId}/documents/${id}`),
  companyDocument: (employerId: string, id: string) => call<DocumentDetail>("GET", `/employers/${employerId}/documents/${id}`),
  askKnowledge: (employerId: string, question: string) => call<AskResult>("POST", `/employers/${employerId}/knowledge/ask`, { question }),
  askedKnowledge: (employerId: string, askId: string) => call<AskResult>("GET", `/employers/${employerId}/knowledge/ask/${askId}`, undefined, POLL_READ),
  memory: (employerId: string, kind?: WorkplaceKind) =>
    call<{ memory: MemoryItem[] }>("GET", `/employers/${employerId}/memory${kind ? `?kind=${kind}` : ""}`),
  updateMemory: (id: string, fields: { title?: string; summary?: string; body?: Record<string, unknown> }) =>
    call<MemoryItem>("PATCH", `/memory/${id}`, fields),
  archiveMemory: (id: string) => call<null>("DELETE", `/memory/${id}`),
  proposals: (employerId: string, policy?: ReadPolicy) => call<{ proposals: Proposal[] }>("GET", `/proposals?employer_id=${employerId}`, undefined, policy),
  approveProposal: (id: string, edit?: { title?: string; summary?: string }) => call<unknown>("POST", `/proposals/${id}/approve`, edit ?? null),
  rejectProposal: (id: string) => call<unknown>("POST", `/proposals/${id}/reject`),
  decideDocument: (documentId: string, action: "approve" | "reject") =>
    call<{ decided: number }>("POST", "/proposals/bulk", { action, company_document_id: documentId }),
  packs: (employerId: string, policy?: ReadPolicy) => call<{ packs: PackState[] }>("GET", `/employers/${employerId}/packs`, undefined, policy),
  ensurePack: (employerId: string, mode: "standup" | "meeting") => call<PackState>("POST", `/employers/${employerId}/packs/${mode}`),
};
