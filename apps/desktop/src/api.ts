// Commands and events of the Rust core (apps/desktop/src-tauri/src/lib.rs). The UI only
// renders: audio, the gateway connection and settings storage all live in Rust.
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

export interface Job {
  job_id: string;
  pack_id: string;
  pack_available: boolean;
  company: string | null;
  role_title: string | null;
  round: string | null;
  built_at: string | null;
}

export interface JobList {
  user_name: string;
  jobs: Job[];
  mode_packs: string[];
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
  meeting_title: string | null;
  platform: string;
  store_transcript: boolean;
}

export const api = {
  getSettings: () => invoke<[Settings, boolean]>("get_settings"),
  saveSettings: (settings: Settings, devToken: string | null) =>
    invoke<void>("save_settings", { settings, devToken }),
  listDevices: () => invoke<{ inputs: DeviceInfo[]; outputs: DeviceInfo[] }>("list_devices"),
  listJobs: () => invoke<JobList>("list_jobs"),
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
