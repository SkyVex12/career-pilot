// Generated from protocol.schema.json by packages/protocol/scripts/codegen_rs.py. Do not edit; run `pnpm codegen`.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::validate::{self as check, Validate, ValidationError};

/// Any JSON message the client sends to the gateway.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ClientMessage {
    #[serde(rename = "session.start")]
    SessionStart(SessionStart),
    #[serde(rename = "session.resume")]
    SessionResume(SessionResume),
    #[serde(rename = "auth.refresh")]
    AuthRefresh(AuthRefresh),
    #[serde(rename = "question.force")]
    QuestionForce(QuestionForce),
    #[serde(rename = "screenshot")]
    Screenshot(Screenshot),
    #[serde(rename = "answer.expand")]
    AnswerExpand(AnswerExpand),
    #[serde(rename = "answer.feedback")]
    AnswerFeedback(AnswerFeedback),
    #[serde(rename = "latency.report")]
    LatencyReport(LatencyReport),
    #[serde(rename = "mode.set")]
    ModeSet(ModeSet),
    #[serde(rename = "answer_style.set")]
    AnswerStyleSet(AnswerStyleSet),
    #[serde(rename = "session.end")]
    SessionEnd(SessionEnd),
}

impl ClientMessage {
    /// The wire `type` of this message.
    pub fn type_name(&self) -> &'static str {
        match self {
            Self::SessionStart(_) => "session.start",
            Self::SessionResume(_) => "session.resume",
            Self::AuthRefresh(_) => "auth.refresh",
            Self::QuestionForce(_) => "question.force",
            Self::Screenshot(_) => "screenshot",
            Self::AnswerExpand(_) => "answer.expand",
            Self::AnswerFeedback(_) => "answer.feedback",
            Self::LatencyReport(_) => "latency.report",
            Self::ModeSet(_) => "mode.set",
            Self::AnswerStyleSet(_) => "answer_style.set",
            Self::SessionEnd(_) => "session.end",
        }
    }
}

impl Validate for ClientMessage {
    fn validate(&self) -> Result<(), ValidationError> {
        match self {
            Self::SessionStart(m) => m.validate(),
            Self::SessionResume(m) => m.validate(),
            Self::AuthRefresh(m) => m.validate(),
            Self::QuestionForce(m) => m.validate(),
            Self::Screenshot(m) => m.validate(),
            Self::AnswerExpand(m) => m.validate(),
            Self::AnswerFeedback(m) => m.validate(),
            Self::LatencyReport(m) => m.validate(),
            Self::ModeSet(m) => m.validate(),
            Self::AnswerStyleSet(m) => m.validate(),
            Self::SessionEnd(m) => m.validate(),
        }
    }
}

impl From<SessionStart> for ClientMessage {
    fn from(m: SessionStart) -> Self {
        Self::SessionStart(m)
    }
}

impl From<SessionResume> for ClientMessage {
    fn from(m: SessionResume) -> Self {
        Self::SessionResume(m)
    }
}

impl From<AuthRefresh> for ClientMessage {
    fn from(m: AuthRefresh) -> Self {
        Self::AuthRefresh(m)
    }
}

impl From<QuestionForce> for ClientMessage {
    fn from(m: QuestionForce) -> Self {
        Self::QuestionForce(m)
    }
}

impl From<Screenshot> for ClientMessage {
    fn from(m: Screenshot) -> Self {
        Self::Screenshot(m)
    }
}

impl From<AnswerExpand> for ClientMessage {
    fn from(m: AnswerExpand) -> Self {
        Self::AnswerExpand(m)
    }
}

impl From<AnswerFeedback> for ClientMessage {
    fn from(m: AnswerFeedback) -> Self {
        Self::AnswerFeedback(m)
    }
}

impl From<LatencyReport> for ClientMessage {
    fn from(m: LatencyReport) -> Self {
        Self::LatencyReport(m)
    }
}

impl From<ModeSet> for ClientMessage {
    fn from(m: ModeSet) -> Self {
        Self::ModeSet(m)
    }
}

impl From<AnswerStyleSet> for ClientMessage {
    fn from(m: AnswerStyleSet) -> Self {
        Self::AnswerStyleSet(m)
    }
}

impl From<SessionEnd> for ClientMessage {
    fn from(m: SessionEnd) -> Self {
        Self::SessionEnd(m)
    }
}

/// Any JSON message the gateway sends to the client. Every server message carries seq.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ServerMessage {
    #[serde(rename = "session.ready")]
    SessionReady(SessionReady),
    #[serde(rename = "transcript.partial")]
    TranscriptPartial(TranscriptPartial),
    #[serde(rename = "transcript.final")]
    TranscriptFinal(TranscriptFinal),
    #[serde(rename = "question.detected")]
    QuestionDetected(QuestionDetected),
    #[serde(rename = "question.cancelled")]
    QuestionCancelled(QuestionCancelled),
    #[serde(rename = "answer.delta")]
    AnswerDelta(AnswerDelta),
    #[serde(rename = "answer.done")]
    AnswerDone(AnswerDone),
    #[serde(rename = "answer.error")]
    AnswerError(AnswerError),
    #[serde(rename = "status")]
    Status(Status),
    #[serde(rename = "auth.expiring")]
    AuthExpiring(AuthExpiring),
    #[serde(rename = "error")]
    ServerError(ServerError),
}

impl ServerMessage {
    /// The wire `type` of this message.
    pub fn type_name(&self) -> &'static str {
        match self {
            Self::SessionReady(_) => "session.ready",
            Self::TranscriptPartial(_) => "transcript.partial",
            Self::TranscriptFinal(_) => "transcript.final",
            Self::QuestionDetected(_) => "question.detected",
            Self::QuestionCancelled(_) => "question.cancelled",
            Self::AnswerDelta(_) => "answer.delta",
            Self::AnswerDone(_) => "answer.done",
            Self::AnswerError(_) => "answer.error",
            Self::Status(_) => "status",
            Self::AuthExpiring(_) => "auth.expiring",
            Self::ServerError(_) => "error",
        }
    }

    /// The message's `seq`; every variant carries one.
    pub fn seq(&self) -> u64 {
        match self {
            Self::SessionReady(m) => m.seq,
            Self::TranscriptPartial(m) => m.seq,
            Self::TranscriptFinal(m) => m.seq,
            Self::QuestionDetected(m) => m.seq,
            Self::QuestionCancelled(m) => m.seq,
            Self::AnswerDelta(m) => m.seq,
            Self::AnswerDone(m) => m.seq,
            Self::AnswerError(m) => m.seq,
            Self::Status(m) => m.seq,
            Self::AuthExpiring(m) => m.seq,
            Self::ServerError(m) => m.seq,
        }
    }
}

impl Validate for ServerMessage {
    fn validate(&self) -> Result<(), ValidationError> {
        match self {
            Self::SessionReady(m) => m.validate(),
            Self::TranscriptPartial(m) => m.validate(),
            Self::TranscriptFinal(m) => m.validate(),
            Self::QuestionDetected(m) => m.validate(),
            Self::QuestionCancelled(m) => m.validate(),
            Self::AnswerDelta(m) => m.validate(),
            Self::AnswerDone(m) => m.validate(),
            Self::AnswerError(m) => m.validate(),
            Self::Status(m) => m.validate(),
            Self::AuthExpiring(m) => m.validate(),
            Self::ServerError(m) => m.validate(),
        }
    }
}

impl From<SessionReady> for ServerMessage {
    fn from(m: SessionReady) -> Self {
        Self::SessionReady(m)
    }
}

impl From<TranscriptPartial> for ServerMessage {
    fn from(m: TranscriptPartial) -> Self {
        Self::TranscriptPartial(m)
    }
}

impl From<TranscriptFinal> for ServerMessage {
    fn from(m: TranscriptFinal) -> Self {
        Self::TranscriptFinal(m)
    }
}

impl From<QuestionDetected> for ServerMessage {
    fn from(m: QuestionDetected) -> Self {
        Self::QuestionDetected(m)
    }
}

impl From<QuestionCancelled> for ServerMessage {
    fn from(m: QuestionCancelled) -> Self {
        Self::QuestionCancelled(m)
    }
}

impl From<AnswerDelta> for ServerMessage {
    fn from(m: AnswerDelta) -> Self {
        Self::AnswerDelta(m)
    }
}

impl From<AnswerDone> for ServerMessage {
    fn from(m: AnswerDone) -> Self {
        Self::AnswerDone(m)
    }
}

impl From<AnswerError> for ServerMessage {
    fn from(m: AnswerError) -> Self {
        Self::AnswerError(m)
    }
}

impl From<Status> for ServerMessage {
    fn from(m: Status) -> Self {
        Self::Status(m)
    }
}

impl From<AuthExpiring> for ServerMessage {
    fn from(m: AuthExpiring) -> Self {
        Self::AuthExpiring(m)
    }
}

impl From<ServerError> for ServerMessage {
    fn from(m: ServerError) -> Self {
        Self::ServerError(m)
    }
}

/// Session mode; picks the session pack and prompt template.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Mode {
    #[serde(rename = "interview")]
    Interview,
    #[serde(rename = "standup")]
    Standup,
    #[serde(rename = "meeting")]
    Meeting,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Interview => "interview",
            Self::Standup => "standup",
            Self::Meeting => "meeting",
        }
    }
}

/// How the detailed (deep) answer is written: bullets = short lines to speak from, deeper than the
/// talking points; paragraph = connected spoken prose. The talking points stay bullets either way.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum AnswerStyle {
    #[serde(rename = "bullets")]
    Bullets,
    #[serde(rename = "paragraph")]
    Paragraph,
}

impl AnswerStyle {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Bullets => "bullets",
            Self::Paragraph => "paragraph",
        }
    }
}

/// Audio source: them = system output (the other side), me = microphone (the user).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Channel {
    #[serde(rename = "them")]
    Them,
    #[serde(rename = "me")]
    Me,
}

impl Channel {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Them => "them",
            Self::Me => "me",
        }
    }
}

/// Answer pass: fast (Haiku talking points) or deep (Sonnet).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Pass {
    #[serde(rename = "fast")]
    Fast,
    #[serde(rename = "deep")]
    Deep,
}

impl Pass {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Fast => "fast",
            Self::Deep => "deep",
        }
    }
}

/// Question type from the detector. respond: not a question, but said to the user and needing a
/// reply (a request, a reminder, "makes sense?"); also what the Respond button forces. Clients
/// before desktop 0.1.5 get other instead.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum QuestionType {
    #[serde(rename = "behavioral")]
    Behavioral,
    #[serde(rename = "technical")]
    Technical,
    #[serde(rename = "system_design")]
    SystemDesign,
    #[serde(rename = "coding")]
    Coding,
    #[serde(rename = "standup_status")]
    StandupStatus,
    #[serde(rename = "small_talk")]
    SmallTalk,
    #[serde(rename = "respond")]
    Respond,
    #[serde(rename = "other")]
    Other,
}

impl QuestionType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Behavioral => "behavioral",
            Self::Technical => "technical",
            Self::SystemDesign => "system_design",
            Self::Coding => "coding",
            Self::StandupStatus => "standup_status",
            Self::SmallTalk => "small_talk",
            Self::Respond => "respond",
            Self::Other => "other",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Consent {
    /// Whether the transcript may be stored after the call.
    pub store_transcript: bool,
}

impl Validate for Consent {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// Audio frames are 16 kHz, mono, 16-bit little-endian PCM.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AudioFormat {
    pub sample_rate: u32,
    pub encoding: String,
}

impl Validate for AudioFormat {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.sample_rate;
            check::const_int("sample_rate", u64::from(*v), 16000)?;
        }
        {
            let v = &self.encoding;
            check::const_str("encoding", v, "pcm16")?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionLimits {
    /// Minutes this session may run under the user's plan.
    pub max_minutes: u64,
}

impl Validate for SessionLimits {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// Server-side timings for one answer pass, in milliseconds from t0 (speech end, or the force
/// message) unless noted. turn_end_ms: question confirmed; detector_ms: detector call (a duration);
/// first_token_ms: request to first model token (a duration); first_delta_ms: the first
/// answer.delta sent; final_ms: the STT final arrived; start_ms: the pass started (the model
/// request, or the prepared-answer lookup); expand_ms: the user tapped expand (deep pass after
/// expand; its first word on screen is first_delta_ms minus expand_ms after the tap); release_ms: a
/// fast pass started speculatively before the detector decided was released to the client (its
/// first_delta_ms is never earlier); prepared_ms: the prepared-answer embedding and match (a
/// duration); prepared_similarity: the best prepared answer's cosine similarity, hit or miss;
/// memory_wait_ms: how long the pass waited for live memory search (a standup or meeting at a
/// company); memory_hits: the <memory> lines it got; memory_fts_ms and memory_hybrid_ms: the full-
/// text and hybrid queries (durations). Fields are absent when they do not apply.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LatencyMs {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_end_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detector_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub first_token_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub first_delta_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub total_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expand_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub release_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prepared_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prepared_similarity: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_wait_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_hits: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_fts_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_hybrid_ms: Option<u64>,
}

impl Validate for LatencyMs {
    fn validate(&self) -> Result<(), ValidationError> {
        if let Some(v) = &self.prepared_similarity {
            check::min_num("prepared_similarity", *v, -1.0)?;
            check::max_num("prepared_similarity", *v, 1.0)?;
        }
        Ok(())
    }
}

/// A memory node an answer cited; the gateway maps short ids like n:3f2a back to node ids.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MemoryRef {
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub node_id: Uuid,
    pub label: String,
}

impl Validate for MemoryRef {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// Token usage for one answer pass.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Usage {
    pub input_tokens: u64,
    pub cached_tokens: u64,
    pub output_tokens: u64,
}

impl Validate for Usage {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// First message of a new session.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionStart {
    pub mode: Mode,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_id: Option<Uuid>,
    /// The scheduled interview this call is; its session pack is loaded first. Send job_id too.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub interview_id: Option<Uuid>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub meeting_title: Option<String>,
    /// The company a standup or meeting is at: its pack is loaded and its memory searched live.
    /// Ignored in interviews.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub employer_id: Option<Uuid>,
    /// The meeting's agenda or topic, as the user typed it; added to the meeting pack.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agenda: Option<String>,
    /// A standup or meeting at a company: the company documents to focus on. Their approved facts
    /// are put in the session pack, read first; live search still covers all of the company's
    /// memory. The gateway uses the first 20.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub material_ids: Option<Vec<Uuid>>,
    /// Call platform, for example zoom, meet or teams.
    pub platform: String,
    pub client_version: String,
    pub consent: Consent,
    pub audio: AudioFormat,
    /// The detailed answer's style; absent means bullets.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub answer_style: Option<AnswerStyle>,
}

impl Validate for SessionStart {
    fn validate(&self) -> Result<(), ValidationError> {
        if let Some(v) = &self.agenda {
            check::max_len("agenda", v, 2000)?;
        }
        {
            let v = &self.platform;
            check::min_len("platform", v, 1)?;
        }
        {
            let v = &self.client_version;
            check::min_len("client_version", v, 1)?;
        }
        {
            let v = &self.consent;
            v.validate()?;
        }
        {
            let v = &self.audio;
            v.validate()?;
        }
        Ok(())
    }
}

/// First message after a reconnect.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionResume {
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub session_id: Uuid,
    /// seq of the last server message the client processed, 0 if none.
    pub last_seq: u64,
}

impl Validate for SessionResume {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// Sent before the current access token expires.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AuthRefresh {
    pub access_token: String,
}

impl Validate for AuthRefresh {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.access_token;
            check::min_len("access_token", v, 1)?;
        }
        Ok(())
    }
}

/// Hotkey pressed (no text) or a typed question (with text). At most once per second.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuestionForce {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    /// answer (the default): answer it as a question. respond: suggest a reply to what was just
    /// said (or to text), as the detector's respond type.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<QuestionForceKind>,
}

impl Validate for QuestionForce {
    fn validate(&self) -> Result<(), ValidationError> {
        if let Some(v) = &self.text {
            check::min_len("text", v, 1)?;
        }
        Ok(())
    }
}

/// Coding question from the screen. The whole message is at most 2 MB.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Screenshot {
    /// Base64 JPEG, at most 1.5 MB before encoding (2,000,000 base64 characters).
    pub image: String,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub question_id: Option<Uuid>,
}

impl Validate for Screenshot {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.image;
            check::min_len("image", v, 1)?;
            check::max_len("image", v, 2000000)?;
        }
        Ok(())
    }
}

/// User opens the deep answer.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerExpand {
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
}

impl Validate for AnswerExpand {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// The client's own first-word time for one answer pass, sent once per pass when its first non-
/// empty answer.delta arrives, so the gateway can store it with the answer
/// (answers.latency_ms.client_first_word_ms). Measurement only: the gateway drops a report for a
/// question it does not hold, without an error.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LatencyReport {
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
    pub pass: Pass,
    /// Anchor to the first answer word, measured on the client.
    pub first_word_ms: u64,
    /// What first_word_ms counts from: speech_end (the true end of the other side's speech, VAD
    /// hangover included), forced (the hotkey or a typed question), or expand (the tap that started
    /// a deep pass).
    pub anchor: LatencyReportAnchor,
}

impl Validate for LatencyReport {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.first_word_ms;
            check::max_int("first_word_ms", u64::from(*v), 600000)?;
        }
        Ok(())
    }
}

/// Thumbs up or down on an answer.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerFeedback {
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
    pub rating: AnswerFeedbackRating,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

impl Validate for AnswerFeedback {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// User switches mode mid-call.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModeSet {
    pub mode: Mode,
}

impl Validate for ModeSet {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// User switches the detailed answer's style mid-call; applies to the next answers.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerStyleSet {
    pub answer_style: AnswerStyle,
}

impl Validate for AnswerStyleSet {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// User ends the session.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionEnd {
    /// Whether to write the post-call outputs (summary, standup report, commitments). Absent means
    /// true; false keeps the transcript only.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub summarize: Option<bool>,
}

impl Validate for SessionEnd {
    fn validate(&self) -> Result<(), ValidationError> {
        Ok(())
    }
}

/// Session accepted; audio may flow.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionReady {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub session_id: Uuid,
    pub server_time: String,
    pub limits: SessionLimits,
    /// true: the gateway transcribes the user's own speech live, so the client sends speech audio
    /// on the me channel (after echo cancellation and VAD, never silence). Absent or false: me
    /// frames carry speech flags only, as before.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub me_audio: Option<bool>,
    /// Question types whose deep answer starts on its own once the fast answer is done; other types
    /// get one only on answer.expand. Absent: the client assumes behavioral, technical and
    /// system_design.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deep_auto_types: Option<Vec<QuestionType>>,
}

impl Validate for SessionReady {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        {
            let v = &self.server_time;
            check::date_time("server_time", v)?;
        }
        {
            let v = &self.limits;
            v.validate()?;
        }
        Ok(())
    }
}

/// Interim STT text, replaced by later partials for the same segment.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TranscriptPartial {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    pub channel: Channel,
    pub seg_id: u64,
    pub text: String,
}

impl Validate for TranscriptPartial {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        Ok(())
    }
}

/// Final text for a segment, on either channel (the me channel only when session.ready said
/// me_audio).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TranscriptFinal {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    pub channel: Channel,
    pub seg_id: u64,
    pub text: String,
    pub start_ms: u64,
    pub end_ms: u64,
    /// Only on the me channel: true when the gateway took the segment for the other side's voice
    /// leaking into the microphone. Its text is not used or stored, and the client drops the line.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub echo: Option<bool>,
}

impl Validate for TranscriptFinal {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        Ok(())
    }
}

/// A question aimed at the user. speculative: true means it may still be cancelled; confirmation
/// repeats the same question_id with speculative: false.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuestionDetected {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
    pub text: String,
    pub question_type: QuestionType,
    pub confidence: f64,
    pub speculative: bool,
}

impl Validate for QuestionDetected {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        {
            let v = &self.confidence;
            check::min_num("confidence", *v, 0.0)?;
            check::max_num("confidence", *v, 1.0)?;
        }
        Ok(())
    }
}

/// A speculative question was dropped because the speaker kept talking.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuestionCancelled {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
}

impl Validate for QuestionCancelled {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        Ok(())
    }
}

/// Next chunk of streamed answer text.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerDelta {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
    pub pass: Pass,
    pub text: String,
}

impl Validate for AnswerDelta {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        Ok(())
    }
}

/// Answer pass finished.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerDone {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
    pub pass: Pass,
    pub memory_refs: Vec<MemoryRef>,
    pub usage: Usage,
    /// Where the answer came from: model, prepared (a prepared answer in the session pack) or cache
    /// (an exact repeat).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<AnswerDoneSource>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub latency_ms: Option<LatencyMs>,
}

impl Validate for AnswerDone {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        {
            let v = &self.memory_refs;
            for item in v {
                item.validate()?;
            }
        }
        {
            let v = &self.usage;
            v.validate()?;
        }
        if let Some(v) = &self.latency_ms {
            v.validate()?;
        }
        Ok(())
    }
}

/// That pass failed; the other pass may still arrive.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerError {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    /// UUID (v7 for ids we generate), lowercase hex with dashes.
    pub question_id: Uuid,
    pub pass: Pass,
    pub code: String,
}

impl Validate for AnswerError {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        {
            let v = &self.code;
            check::min_len("code", v, 1)?;
        }
        Ok(())
    }
}

/// Drives the small status badge in the overlay.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Status {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    pub component: StatusComponent,
    pub state: StatusState,
    pub message: String,
}

impl Validate for Status {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        Ok(())
    }
}

/// Asks the client to send auth.refresh now.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AuthExpiring {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    pub expires_in_s: u64,
}

impl Validate for AuthExpiring {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        Ok(())
    }
}

/// Protocol or server error; fatal: true is followed by a close. Before a 4429 close,
/// retry_after_ms says when to retry.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServerError {
    /// Per-session server message counter. Starts at 1 and increases by one per message; the client
    /// sends the last one it processed on resume (0 if none).
    pub seq: u64,
    pub code: String,
    pub message: String,
    pub fatal: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after_ms: Option<u64>,
}

impl Validate for ServerError {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.seq;
            check::min_int("seq", u64::from(*v), 1)?;
        }
        {
            let v = &self.code;
            check::min_len("code", v, 1)?;
        }
        Ok(())
    }
}

/// WebSocket close codes. 1000 ended normally; 1001 server going away (resume); 4401 token invalid
/// or expired; 4403 no minutes left; 4408 no start or resume in time; 4409 another session active;
/// 4410 replaced by a newer connection for this session (do not reconnect or prompt); 4426 the app
/// is older than the server supports (do not reconnect; update first; the error before it is
/// client_outdated); 4429 rate limited; 4500 internal error (resume).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CloseCode {
    Normal = 1000,
    GoingAway = 1001,
    Unauthorized = 4401,
    NoMinutes = 4403,
    NoStart = 4408,
    SessionActive = 4409,
    Replaced = 4410,
    ClientOutdated = 4426,
    RateLimited = 4429,
    Internal = 4500,
}

impl CloseCode {
    pub const ALL: [Self; 10] = [
        Self::Normal,
        Self::GoingAway,
        Self::Unauthorized,
        Self::NoMinutes,
        Self::NoStart,
        Self::SessionActive,
        Self::Replaced,
        Self::ClientOutdated,
        Self::RateLimited,
        Self::Internal,
    ];

    pub fn code(self) -> u16 {
        self as u16
    }

    pub fn from_code(code: u16) -> Option<Self> {
        match code {
            1000 => Some(Self::Normal),
            1001 => Some(Self::GoingAway),
            4401 => Some(Self::Unauthorized),
            4403 => Some(Self::NoMinutes),
            4408 => Some(Self::NoStart),
            4409 => Some(Self::SessionActive),
            4410 => Some(Self::Replaced),
            4426 => Some(Self::ClientOutdated),
            4429 => Some(Self::RateLimited),
            4500 => Some(Self::Internal),
            _ => None,
        }
    }
}

/// The app is older than the oldest version the server supports (its min_client_version setting).
/// The REST API answers any request with HTTP 426 and this as the JSON body; the gateway sends an
/// error with code client_outdated (this message) before closing with 4426. The app names its
/// version as desktop-<version>: in session.start's client_version and in the x-copilot-client
/// header of API requests.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClientOutdated {
    pub code: String,
    pub message: String,
    pub min_version: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_version: Option<String>,
}

impl Validate for ClientOutdated {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.code;
            check::const_str("code", v, "client_outdated")?;
        }
        {
            let v = &self.min_version;
            check::min_len("min_version", v, 1)?;
        }
        Ok(())
    }
}

/// Header of a binary audio frame: 10 bytes, then 16 kHz mono pcm16 audio (a 100 ms frame is 3,200
/// bytes; may be empty on a speech-end frame; on the me channel empty unless session.ready said
/// me_audio). Byte layout: [0] channel uint8, [1] flags uint8, [2..5] frame_seq uint32 big-endian,
/// [6..9] capture_ms uint32 big-endian. Binary frames are at most 64 KB.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AudioFrameHeader {
    /// 0 = them (system output), 1 = me (microphone, echo-cancelled; speech audio when
    /// session.ready said me_audio, else speech flags only).
    pub channel: u8,
    /// Bit 0 = VAD speech start, bit 1 = VAD speech end. Other bits are 0.
    pub flags: u8,
    /// Per channel, starts at 0. The server drops frames it has already seen.
    pub frame_seq: u32,
    /// Milliseconds since session start.
    pub capture_ms: u32,
}

impl Validate for AudioFrameHeader {
    fn validate(&self) -> Result<(), ValidationError> {
        {
            let v = &self.channel;
            check::one_of("channel", u64::from(*v), &[0, 1])?;
        }
        {
            let v = &self.flags;
            check::max_int("flags", u64::from(*v), 3)?;
        }
        Ok(())
    }
}

/// answer (the default): answer it as a question. respond: suggest a reply to what was just said
/// (or to text), as the detector's respond type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum QuestionForceKind {
    #[serde(rename = "answer")]
    Answer,
    #[serde(rename = "respond")]
    Respond,
}

impl QuestionForceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Answer => "answer",
            Self::Respond => "respond",
        }
    }
}

/// What first_word_ms counts from: speech_end (the true end of the other side's speech, VAD
/// hangover included), forced (the hotkey or a typed question), or expand (the tap that started a
/// deep pass).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum LatencyReportAnchor {
    #[serde(rename = "speech_end")]
    SpeechEnd,
    #[serde(rename = "forced")]
    Forced,
    #[serde(rename = "expand")]
    Expand,
}

impl LatencyReportAnchor {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SpeechEnd => "speech_end",
            Self::Forced => "forced",
            Self::Expand => "expand",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum AnswerFeedbackRating {
    #[serde(rename = "up")]
    Up,
    #[serde(rename = "down")]
    Down,
}

impl AnswerFeedbackRating {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Up => "up",
            Self::Down => "down",
        }
    }
}

/// Where the answer came from: model, prepared (a prepared answer in the session pack) or cache (an
/// exact repeat).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum AnswerDoneSource {
    #[serde(rename = "model")]
    Model,
    #[serde(rename = "prepared")]
    Prepared,
    #[serde(rename = "cache")]
    Cache,
}

impl AnswerDoneSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Model => "model",
            Self::Prepared => "prepared",
            Self::Cache => "cache",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum StatusComponent {
    #[serde(rename = "stt")]
    Stt,
    #[serde(rename = "llm")]
    Llm,
    #[serde(rename = "memory")]
    Memory,
}

impl StatusComponent {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Stt => "stt",
            Self::Llm => "llm",
            Self::Memory => "memory",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum StatusState {
    #[serde(rename = "ok")]
    Ok,
    #[serde(rename = "degraded")]
    Degraded,
    #[serde(rename = "reconnecting")]
    Reconnecting,
}

impl StatusState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Degraded => "degraded",
            Self::Reconnecting => "reconnecting",
        }
    }
}
