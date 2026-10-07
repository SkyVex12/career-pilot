// Commands and events of the Rust core (apps/desktop/src-tauri/src/lib.rs). The UI only
// renders: audio, the gateway connection, the Copilot API client and settings storage all
// live in Rust. `rest` below is the API (ARCHITECTURE.md, "REST API") through `api_call`.
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { Channel, LatencyMs, Mode, QuestionType, ServerMessage } from "@copilot/protocol";

export const inTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export type StopReason = "ended" | "no_minutes" | "session_active" | "replaced" | "unauthorized";

export type ConnectionState =
  | { state: "connecting"; attempt: number }
  | { state: "live"; session_id: string; resumed: boolean }
  | { state: "reconnecting"; attempt: number; in_ms: number; close_code: number | null }
  | { state: "stopped"; reason: StopReason };

export interface QuestionLatency {
  question_id: string;
  text: string;
  question_type: QuestionType;
  anchor: "speech_end" | "forced" | "unknown";
  detected_ms: number | null;
  fast_first_word_ms: number | null;
  deep_first_word_ms: number | null;
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

export interface Settings {
  gateway_url: string;
  api_url: string;
  mic_device: string | null;
  output_device: string | null;
  echo_cancellation: boolean;
  hotkey: string;
  show_debug: boolean;
  last_session_id: string | null;
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
  platform: string;
  store_transcript: boolean;
}

export const api = {
  getSettings: () => invoke<[Settings, boolean]>("get_settings"),
  saveSettings: (settings: Settings, devToken: string | null) =>
    invoke<void>("save_settings", { settings, devToken }),
  listDevices: () => invoke<{ inputs: DeviceInfo[]; outputs: DeviceInfo[] }>("list_devices"),
  uploadDocument: (kind: DocumentKind, file: File) =>
    file.arrayBuffer().then((buf) =>
      invoke<DocumentItem>("upload_document", new Uint8Array(buf), {
        headers: { "x-kind": kind, "x-file-name": encodeURIComponent(file.name) },
      }),
    ),
  deleteAccount: () => invoke<{ status: string }>("delete_account"),
  uploadStatus: (sessionId: string) => invoke<UploadOutcome | null>("upload_status", { sessionId }),
  startSession: (request: StartRequest) => invoke<void>("start_session", { request }),
  endSession: () => invoke<void>("end_session"),
  endOtherSession: () => invoke<string>("end_other_session"),
  forceQuestion: (text: string | null) => invoke<void>("force_question", { text }),
  expandAnswer: (questionId: string) => invoke<void>("expand_answer", { questionId }),
  sendFeedback: (questionId: string, up: boolean) =>
    invoke<void>("send_feedback", { questionId, up, reason: null }),
  setMode: (mode: Mode) => invoke<void>("set_mode", { mode }),
  snapshot: () => invoke<[Numbered[], boolean]>("snapshot"),
  liveStatus: () => invoke<LiveStatus>("live_status"),
  showOverlay: (show: boolean) => invoke<void>("show_overlay", { show }),
};

/// `n` numbers events within a session, from 0.
export interface Numbered {
  n: number;
  event: UiEvent;
}

export function onEvent(handler: (e: Numbered) => void): Promise<UnlistenFn> {
  return listen<Numbered>("copilot://event", (e) => handler(e.payload));
}

// The user's own audio after a call (copilot_client::recording::UploadOutcome).
export type UploadOutcome = { state: "uploaded" } | { state: "discarded"; message: string } | { state: "kept"; message: string };

export function onUpload(handler: (sessionId: string, outcome: UploadOutcome) => void): Promise<UnlistenFn> {
  return listen<{ session_id: string; outcome: UploadOutcome }>("copilot://upload", (e) => handler(e.payload.session_id, e.payload.outcome));
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
}

export interface DigestInfo {
  version: string;
  built_at: string | null;
  facts: number;
}

export interface PackInfo {
  pack_id: string;
  available: boolean;
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
  store_transcript: boolean;
  started_at: string | null;
  ended_at: string | null;
  billed_seconds: number;
  me_audio_status: "pending" | "uploaded" | "transcribed" | "none";
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

const call = <T>(method: string, path: string, body?: unknown) =>
  invoke<T>("api_call", { method, path, body: body ?? null });

export const rest = {
  me: () => call<Me>("GET", "/me"),
  usage: () => call<Usage>("GET", "/usage"),
  documents: () => call<{ documents: DocumentItem[]; digest: DigestInfo | null }>("GET", "/documents"),
  jobs: () => call<{ jobs: JobItem[] }>("GET", "/jobs"),
  interviews: () => call<{ interviews: InterviewItem[] }>("GET", "/interviews"),
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
};
