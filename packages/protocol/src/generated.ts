/* Generated from protocol.schema.json by json-schema-to-typescript. Do not edit; run `pnpm codegen`. */

/**
 * Realtime protocol between the desktop client and the gateway (ARCHITECTURE.md, section 1). JSON text frames carry these messages; binary frames carry audio with AudioFrameHeader. Python and TypeScript types are generated from this file; edit it, then run `pnpm codegen`.
 */
export type Message = ClientMessage | ServerMessage;
/**
 * Any JSON message the client sends to the gateway.
 */
export type ClientMessage =
  | SessionStart
  | SessionResume
  | AuthRefresh
  | QuestionForce
  | Screenshot
  | AnswerExpand
  | AnswerFeedback
  | LatencyReport
  | ModeSet
  | SessionEnd;
/**
 * Session mode; picks the session pack and prompt template.
 */
export type Mode = "interview" | "standup" | "meeting";
/**
 * UUID (v7 for ids we generate), lowercase hex with dashes.
 */
export type Uuid = string;
/**
 * Answer pass: fast (Haiku talking points) or deep (Sonnet).
 */
export type Pass = "fast" | "deep";
/**
 * Any JSON message the gateway sends to the client. Every server message carries seq.
 */
export type ServerMessage =
  | SessionReady
  | TranscriptPartial
  | TranscriptFinal
  | QuestionDetected
  | QuestionCancelled
  | AnswerDelta
  | AnswerDone
  | AnswerError
  | Status
  | AuthExpiring
  | ServerError;
/**
 * Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
 */
export type Seq = number;
/**
 * Audio source: them = system output (the other side), me = microphone (the user).
 */
export type Channel = "them" | "me";
/**
 * Question type from the detector.
 */
export type QuestionType =
  | "behavioral"
  | "technical"
  | "system_design"
  | "coding"
  | "standup_status"
  | "small_talk"
  | "other";

/**
 * First message of a new session.
 */
export interface SessionStart {
  type: "session.start";
  mode: Mode;
  job_id?: Uuid;
  /**
   * UUID (v7 for ids we generate), lowercase hex with dashes.
   */
  interview_id?: string;
  meeting_title?: string;
  /**
   * Call platform, for example zoom, meet or teams.
   */
  platform: string;
  client_version: string;
  consent: Consent;
  audio: AudioFormat;
}
export interface Consent {
  /**
   * Whether the transcript may be stored after the call.
   */
  store_transcript: boolean;
}
/**
 * Audio frames are 16 kHz, mono, 16-bit little-endian PCM.
 */
export interface AudioFormat {
  sample_rate: 16000;
  encoding: "pcm16";
}
/**
 * First message after a reconnect.
 */
export interface SessionResume {
  type: "session.resume";
  session_id: Uuid;
  /**
   * seq of the last server message the client processed, 0 if none.
   */
  last_seq: number;
}
/**
 * Sent before the current access token expires.
 */
export interface AuthRefresh {
  type: "auth.refresh";
  access_token: string;
}
/**
 * Hotkey pressed (no text) or a typed question (with text). At most once per second.
 */
export interface QuestionForce {
  type: "question.force";
  text?: string;
}
/**
 * Coding question from the screen. The whole message is at most 2 MB.
 */
export interface Screenshot {
  type: "screenshot";
  /**
   * Base64 JPEG, at most 1.5 MB before encoding (2,000,000 base64 characters).
   */
  image: string;
  question_id?: Uuid;
}
/**
 * User opens the deep answer.
 */
export interface AnswerExpand {
  type: "answer.expand";
  question_id: Uuid;
}
/**
 * Thumbs up or down on an answer.
 */
export interface AnswerFeedback {
  type: "answer.feedback";
  question_id: Uuid;
  rating: "up" | "down";
  reason?: string;
}
/**
 * The client's own first-word time for one answer pass, sent once per pass when its first non-empty answer.delta arrives, so the gateway can store it with the answer (answers.latency_ms.client_first_word_ms). Measurement only: the gateway drops a report for a question it does not hold, without an error.
 */
export interface LatencyReport {
  type: "latency.report";
  question_id: Uuid;
  pass: Pass;
  /**
   * Anchor to the first answer word, measured on the client.
   */
  first_word_ms: number;
  /**
   * What first_word_ms counts from: speech_end (the true end of the other side's speech, VAD hangover included), forced (the hotkey or a typed question), or expand (the tap that started a deep pass).
   */
  anchor: "speech_end" | "forced" | "expand";
}
/**
 * User switches mode mid-call.
 */
export interface ModeSet {
  type: "mode.set";
  mode: Mode;
}
/**
 * User ends the session.
 */
export interface SessionEnd {
  type: "session.end";
}
/**
 * Session accepted; audio may flow.
 */
export interface SessionReady {
  type: "session.ready";
  seq: Seq;
  session_id: Uuid;
  server_time: string;
  limits: SessionLimits;
}
export interface SessionLimits {
  /**
   * Minutes this session may run under the user's plan.
   */
  max_minutes: number;
}
/**
 * Interim STT text, replaced by later partials for the same segment.
 */
export interface TranscriptPartial {
  type: "transcript.partial";
  seq: Seq;
  channel: Channel;
  seg_id: number;
  text: string;
}
/**
 * Final text for a segment.
 */
export interface TranscriptFinal {
  type: "transcript.final";
  seq: Seq;
  channel: Channel;
  seg_id: number;
  text: string;
  start_ms: number;
  end_ms: number;
}
/**
 * A question aimed at the user. speculative: true means it may still be cancelled; confirmation repeats the same question_id with speculative: false.
 */
export interface QuestionDetected {
  type: "question.detected";
  seq: Seq;
  question_id: Uuid;
  text: string;
  question_type: QuestionType;
  confidence: number;
  speculative: boolean;
}
/**
 * A speculative question was dropped because the speaker kept talking.
 */
export interface QuestionCancelled {
  type: "question.cancelled";
  seq: Seq;
  question_id: Uuid;
}
/**
 * Next chunk of streamed answer text.
 */
export interface AnswerDelta {
  type: "answer.delta";
  seq: Seq;
  question_id: Uuid;
  pass: Pass;
  text: string;
}
/**
 * Answer pass finished.
 */
export interface AnswerDone {
  type: "answer.done";
  seq: Seq;
  question_id: Uuid;
  pass: Pass;
  memory_refs: MemoryRef[];
  usage: Usage;
  /**
   * Where the answer came from: model, prepared (a prepared answer in the session pack) or cache (an exact repeat).
   */
  source?: "model" | "prepared" | "cache";
  latency_ms?: LatencyMs;
}
/**
 * A memory node an answer cited; the gateway maps short ids like n:3f2a back to node ids.
 */
export interface MemoryRef {
  node_id: Uuid;
  label: string;
}
/**
 * Token usage for one answer pass.
 */
export interface Usage {
  input_tokens: number;
  cached_tokens: number;
  output_tokens: number;
}
/**
 * Server-side timings for one answer pass, in milliseconds from t0 (speech end, or the force message) unless noted. turn_end_ms: question confirmed; detector_ms: detector call (a duration); first_token_ms: request to first model token (a duration); first_delta_ms: the first answer.delta sent; final_ms: the STT final arrived; start_ms: the pass started (the model request, or the prepared-answer lookup); expand_ms: the user tapped expand (deep pass after expand; its first word on screen is first_delta_ms minus expand_ms after the tap); prepared_ms: the prepared-answer embedding and match (a duration); prepared_similarity: the best prepared answer's cosine similarity, hit or miss. Fields are absent when they do not apply.
 */
export interface LatencyMs {
  turn_end_ms?: number;
  detector_ms?: number;
  first_token_ms?: number;
  first_delta_ms?: number;
  total_ms?: number;
  final_ms?: number;
  start_ms?: number;
  expand_ms?: number;
  prepared_ms?: number;
  prepared_similarity?: number;
}
/**
 * That pass failed; the other pass may still arrive.
 */
export interface AnswerError {
  type: "answer.error";
  seq: Seq;
  question_id: Uuid;
  pass: Pass;
  code: string;
}
/**
 * Drives the small status badge in the overlay.
 */
export interface Status {
  type: "status";
  seq: Seq;
  component: "stt" | "llm" | "memory";
  state: "ok" | "degraded" | "reconnecting";
  message: string;
}
/**
 * Asks the client to send auth.refresh now.
 */
export interface AuthExpiring {
  type: "auth.expiring";
  seq: Seq;
  expires_in_s: number;
}
/**
 * Protocol or server error; fatal: true is followed by a close. Before a 4429 close, retry_after_ms says when to retry.
 */
export interface ServerError {
  type: "error";
  seq: Seq;
  code: string;
  message: string;
  fatal: boolean;
  retry_after_ms?: number;
}

/**
 * WebSocket close codes. 1000 ended normally; 1001 server going away (resume); 4401 token invalid or expired; 4403 no minutes left; 4408 no start or resume in time; 4409 another session active; 4410 replaced by a newer connection for this session (do not reconnect or prompt); 4429 rate limited; 4500 internal error (resume).
 */
export type CloseCode = 1000 | 1001 | 4401 | 4403 | 4408 | 4409 | 4410 | 4429 | 4500;

/**
 * Header of a binary audio frame: 10 bytes, then 16 kHz mono pcm16 audio (a 100 ms frame is 3,200 bytes; may be empty on a speech-end frame, and is always empty on the me channel). Byte layout: [0] channel uint8, [1] flags uint8, [2..5] frame_seq uint32 big-endian, [6..9] capture_ms uint32 big-endian. Binary frames are at most 64 KB.
 */
export interface AudioFrameHeader {
  /**
   * 0 = them (system output), 1 = me (microphone; speech flags only, no audio).
   */
  channel: 0 | 1;
  /**
   * Bit 0 = VAD speech start, bit 1 = VAD speech end. Other bits are 0.
   */
  flags: number;
  /**
   * Per channel, starts at 0. The server drops frames it has already seen.
   */
  frame_seq: number;
  /**
   * Milliseconds since session start.
   */
  capture_ms: number;
}
