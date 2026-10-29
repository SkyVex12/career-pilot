# Generated from protocol.schema.json by datamodel-code-generator. Do not edit; run `pnpm codegen`.

from __future__ import annotations

from uuid import UUID
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel
from typing import Annotated, Literal


class Consent(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    store_transcript: bool
    """
    Whether the transcript may be stored after the call.
    """


class AudioFormat(BaseModel):
    """
    Audio frames are 16 kHz, mono, 16-bit little-endian PCM.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    sample_rate: Literal[16000]
    encoding: Literal["pcm16"]


class SessionLimits(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
    )
    max_minutes: Annotated[int, Field(ge=0)]
    """
    Minutes this session may run under the user's plan.
    """


class LatencyMs(BaseModel):
    """
    Server-side timings for one answer pass, in milliseconds from t0 (speech end, or the force message) unless noted. turn_end_ms: question confirmed; detector_ms: detector call (a duration); first_token_ms: request to first model token (a duration); first_delta_ms: the first answer.delta sent; final_ms: the STT final arrived; start_ms: the pass started (the model request, or the prepared-answer lookup); expand_ms: the user tapped expand (deep pass after expand; its first word on screen is first_delta_ms minus expand_ms after the tap); release_ms: a fast pass started speculatively before the detector decided was released to the client (its first_delta_ms is never earlier); prepared_ms: the prepared-answer embedding and match (a duration); prepared_similarity: the best prepared answer's cosine similarity, hit or miss; memory_wait_ms: how long the pass waited for live memory search (a standup or meeting at a company); memory_hits: the <memory> lines it got; memory_fts_ms and memory_hybrid_ms: the full-text and hybrid queries (durations). Fields are absent when they do not apply.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    turn_end_ms: Annotated[int | None, Field(ge=0)] = None
    detector_ms: Annotated[int | None, Field(ge=0)] = None
    first_token_ms: Annotated[int | None, Field(ge=0)] = None
    first_delta_ms: Annotated[int | None, Field(ge=0)] = None
    total_ms: Annotated[int | None, Field(ge=0)] = None
    final_ms: Annotated[int | None, Field(ge=0)] = None
    start_ms: Annotated[int | None, Field(ge=0)] = None
    expand_ms: Annotated[int | None, Field(ge=0)] = None
    release_ms: Annotated[int | None, Field(ge=0)] = None
    prepared_ms: Annotated[int | None, Field(ge=0)] = None
    prepared_similarity: Annotated[float | None, Field(ge=-1.0, le=1.0)] = None
    memory_wait_ms: Annotated[int | None, Field(ge=0)] = None
    memory_hits: Annotated[int | None, Field(ge=0)] = None
    memory_fts_ms: Annotated[int | None, Field(ge=0)] = None
    memory_hybrid_ms: Annotated[int | None, Field(ge=0)] = None


class MemoryRef(BaseModel):
    """
    A memory node an answer cited; the gateway maps short ids like n:3f2a back to node ids.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    node_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    label: str


class Usage(BaseModel):
    """
    Token usage for one answer pass.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    input_tokens: Annotated[int, Field(ge=0)]
    cached_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]


class SessionStart(BaseModel):
    """
    First message of a new session.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["session.start"]
    mode: Literal["interview", "standup", "meeting"]
    """
    Session mode; picks the session pack and prompt template.
    """
    job_id: UUID | None = None
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    interview_id: UUID | None = None
    """
    The scheduled interview this call is; its session pack is loaded first. Send job_id too.
    """
    meeting_title: str | None = None
    employer_id: UUID | None = None
    """
    The company a standup or meeting is at: its pack is loaded and its memory searched live. Ignored in interviews.
    """
    agenda: Annotated[str | None, Field(max_length=2000)] = None
    """
    The meeting's agenda or topic, as the user typed it; added to the meeting pack.
    """
    material_ids: list[UUID] | None = None
    """
    A standup or meeting at a company: the company documents to focus on. Their approved facts are put in the session pack, read first; live search still covers all of the company's memory. The gateway uses the first 20.
    """
    platform: Annotated[str, Field(min_length=1)]
    """
    Call platform, for example zoom, meet or teams.
    """
    client_version: Annotated[str, Field(min_length=1)]
    consent: Consent
    audio: AudioFormat
    answer_style: Literal["bullets", "paragraph"] | None = None
    """
    The detailed answer's style; absent means bullets.
    """


class SessionResume(BaseModel):
    """
    First message after a reconnect.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["session.resume"]
    session_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    last_seq: Annotated[int, Field(ge=0)]
    """
    seq of the last server message the client processed, 0 if none.
    """


class AuthRefresh(BaseModel):
    """
    Sent before the current access token expires.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["auth.refresh"]
    access_token: Annotated[str, Field(min_length=1)]


class QuestionForce(BaseModel):
    """
    Hotkey pressed (no text) or a typed question (with text). At most once per second.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["question.force"]
    text: Annotated[str | None, Field(min_length=1)] = None
    kind: Literal["answer", "respond"] | None = None
    """
    answer (the default): answer it as a question. respond: suggest a reply to what was just said (or to text), as the detector's respond type.
    """


class Screenshot(BaseModel):
    """
    Coding question from the screen. The whole message is at most 2 MB.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["screenshot"]
    image: Annotated[
        str,
        Field(
            json_schema_extra={
                "contentEncoding": "base64",
                "contentMediaType": "image/jpeg",
            },
            max_length=2000000,
            min_length=1,
        ),
    ]
    """
    Base64 JPEG, at most 1.5 MB before encoding (2,000,000 base64 characters).
    """
    question_id: UUID | None = None
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """


class AnswerExpand(BaseModel):
    """
    User opens the deep answer.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["answer.expand"]
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """


class LatencyReport(BaseModel):
    """
    The client's own first-word time for one answer pass, sent once per pass when its first non-empty answer.delta arrives, so the gateway can store it with the answer (answers.latency_ms.client_first_word_ms). Measurement only: the gateway drops a report for a question it does not hold, without an error.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["latency.report"]
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    pass_: Annotated[Literal["fast", "deep"], Field(alias="pass")]
    """
    Answer pass: fast (Haiku talking points) or deep (Sonnet).
    """
    first_word_ms: Annotated[int, Field(ge=0, le=600000)]
    """
    Anchor to the first answer word, measured on the client.
    """
    anchor: Literal["speech_end", "forced", "expand"]
    """
    What first_word_ms counts from: speech_end (the true end of the other side's speech, VAD hangover included), forced (the hotkey or a typed question), or expand (the tap that started a deep pass).
    """


class AnswerFeedback(BaseModel):
    """
    Thumbs up or down on an answer.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["answer.feedback"]
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    rating: Literal["up", "down"]
    reason: str | None = None


class ModeSet(BaseModel):
    """
    User switches mode mid-call.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["mode.set"]
    mode: Literal["interview", "standup", "meeting"]
    """
    Session mode; picks the session pack and prompt template.
    """


class AnswerStyleSet(BaseModel):
    """
    User switches the detailed answer's style mid-call; applies to the next answers.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["answer_style.set"]
    answer_style: Literal["bullets", "paragraph"]
    """
    How the detailed (deep) answer is written: bullets = short lines to speak from, deeper than the talking points; paragraph = connected spoken prose. The talking points stay bullets either way.
    """


class SessionEnd(BaseModel):
    """
    User ends the session.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["session.end"]
    summarize: bool | None = None
    """
    Whether to write the post-call outputs (summary, standup report, commitments). Absent means true; false keeps the transcript only.
    """


class SessionReady(BaseModel):
    """
    Session accepted; audio may flow.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["session.ready"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    session_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    server_time: AwareDatetime
    limits: SessionLimits
    me_audio: bool | None = None
    """
    true: the gateway transcribes the user's own speech live, so the client sends speech audio on the me channel (after echo cancellation and VAD, never silence). Absent or false: me frames carry speech flags only, as before.
    """
    deep_auto_types: (
        list[
            Literal[
                "behavioral",
                "technical",
                "system_design",
                "coding",
                "standup_status",
                "small_talk",
                "respond",
                "other",
            ]
        ]
        | None
    ) = None
    """
    Question types whose deep answer starts on its own once the fast answer is done; other types get one only on answer.expand. Absent: the client assumes behavioral, technical and system_design.
    """


class TranscriptPartial(BaseModel):
    """
    Interim STT text, replaced by later partials for the same segment.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["transcript.partial"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    channel: Literal["them", "me"]
    """
    Audio source: them = system output (the other side), me = microphone (the user).
    """
    seg_id: Annotated[int, Field(ge=0)]
    text: str


class TranscriptFinal(BaseModel):
    """
    Final text for a segment, on either channel (the me channel only when session.ready said me_audio).
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["transcript.final"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    channel: Literal["them", "me"]
    """
    Audio source: them = system output (the other side), me = microphone (the user).
    """
    seg_id: Annotated[int, Field(ge=0)]
    text: str
    start_ms: Annotated[int, Field(ge=0)]
    end_ms: Annotated[int, Field(ge=0)]
    echo: bool | None = None
    """
    Only on the me channel: true when the gateway took the segment for the other side's voice leaking into the microphone. Its text is not used or stored, and the client drops the line.
    """


class QuestionDetected(BaseModel):
    """
    A question aimed at the user. speculative: true means it may still be cancelled; confirmation repeats the same question_id with speculative: false.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["question.detected"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    text: str
    question_type: Literal[
        "behavioral",
        "technical",
        "system_design",
        "coding",
        "standup_status",
        "small_talk",
        "respond",
        "other",
    ]
    """
    Question type from the detector. respond: not a question, but said to the user and needing a reply (a request, a reminder, "makes sense?"); also what the Respond button forces. Clients before desktop 0.1.5 get other instead.
    """
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    speculative: bool


class QuestionCancelled(BaseModel):
    """
    A speculative question was dropped because the speaker kept talking.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["question.cancelled"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """


class AnswerDelta(BaseModel):
    """
    Next chunk of streamed answer text.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["answer.delta"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    pass_: Annotated[Literal["fast", "deep"], Field(alias="pass")]
    """
    Answer pass: fast (Haiku talking points) or deep (Sonnet).
    """
    text: str


class AnswerDone(BaseModel):
    """
    Answer pass finished.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["answer.done"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    pass_: Annotated[Literal["fast", "deep"], Field(alias="pass")]
    """
    Answer pass: fast (Haiku talking points) or deep (Sonnet).
    """
    memory_refs: list[MemoryRef]
    usage: Usage
    source: Literal["model", "prepared", "cache"] | None = None
    """
    Where the answer came from: model, prepared (a prepared answer in the session pack) or cache (an exact repeat).
    """
    latency_ms: LatencyMs | None = None


class AnswerError(BaseModel):
    """
    That pass failed; the other pass may still arrive.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["answer.error"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    question_id: UUID
    """
    UUID (v7 for ids we generate), lowercase hex with dashes.
    """
    pass_: Annotated[Literal["fast", "deep"], Field(alias="pass")]
    """
    Answer pass: fast (Haiku talking points) or deep (Sonnet).
    """
    code: Annotated[str, Field(min_length=1)]


class Status(BaseModel):
    """
    Drives the small status badge in the overlay.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["status"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    component: Literal["stt", "llm", "memory"]
    state: Literal["ok", "degraded", "reconnecting"]
    message: str


class AuthExpiring(BaseModel):
    """
    Asks the client to send auth.refresh now.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["auth.expiring"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    expires_in_s: Annotated[int, Field(ge=0)]


class ServerError(BaseModel):
    """
    Protocol or server error; fatal: true is followed by a close. Before a 4429 close, retry_after_ms says when to retry.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["error"]
    seq: Annotated[int, Field(ge=1)]
    """
    Per-session server message counter. Starts at 1 and increases by one per message; the client sends the last one it processed on resume (0 if none).
    """
    code: Annotated[str, Field(min_length=1)]
    message: str
    fatal: bool
    retry_after_ms: Annotated[int | None, Field(ge=0)] = None


class CloseCode(
    RootModel[Literal[1000, 1001, 4401, 4403, 4408, 4409, 4410, 4426, 4429, 4500]]
):
    root: Literal[1000, 1001, 4401, 4403, 4408, 4409, 4410, 4426, 4429, 4500]
    """
    WebSocket close codes. 1000 ended normally; 1001 server going away (resume); 4401 token invalid or expired; 4403 no minutes left; 4408 no start or resume in time; 4409 another session active; 4410 replaced by a newer connection for this session (do not reconnect or prompt); 4426 the app is older than the server supports (do not reconnect; update first; the error before it is client_outdated); 4429 rate limited; 4500 internal error (resume).
    """


class ClientOutdated(BaseModel):
    """
    The app is older than the oldest version the server supports (its min_client_version setting). The REST API answers any request with HTTP 426 and this as the JSON body; the gateway sends an error with code client_outdated (this message) before closing with 4426. The app names its version as desktop-<version>: in session.start's client_version and in the x-copilot-client header of API requests.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    code: Literal["client_outdated"]
    message: str
    min_version: Annotated[str, Field(min_length=1)]
    client_version: str | None = None


class AudioFrameHeader(BaseModel):
    """
    Header of a binary audio frame: 10 bytes, then 16 kHz mono pcm16 audio (a 100 ms frame is 3,200 bytes; may be empty on a speech-end frame; on the me channel empty unless session.ready said me_audio). Byte layout: [0] channel uint8, [1] flags uint8, [2..5] frame_seq uint32 big-endian, [6..9] capture_ms uint32 big-endian. Binary frames are at most 64 KB.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    channel: Literal[0, 1]
    """
    0 = them (system output), 1 = me (microphone, echo-cancelled; speech audio when session.ready said me_audio, else speech flags only).
    """
    flags: Annotated[int, Field(ge=0, le=3)]
    """
    Bit 0 = VAD speech start, bit 1 = VAD speech end. Other bits are 0.
    """
    frame_seq: Annotated[int, Field(ge=0, le=4294967295)]
    """
    Per channel, starts at 0. The server drops frames it has already seen.
    """
    capture_ms: Annotated[int, Field(ge=0, le=4294967295)]
    """
    Milliseconds since session start.
    """


class ClientMessage(
    RootModel[
        SessionStart
        | SessionResume
        | AuthRefresh
        | QuestionForce
        | Screenshot
        | AnswerExpand
        | AnswerFeedback
        | LatencyReport
        | ModeSet
        | AnswerStyleSet
        | SessionEnd
    ]
):
    root: Annotated[
        SessionStart
        | SessionResume
        | AuthRefresh
        | QuestionForce
        | Screenshot
        | AnswerExpand
        | AnswerFeedback
        | LatencyReport
        | ModeSet
        | AnswerStyleSet
        | SessionEnd,
        Field(discriminator="type"),
    ]
    """
    Any JSON message the client sends to the gateway.
    """


class ServerMessage(
    RootModel[
        SessionReady
        | TranscriptPartial
        | TranscriptFinal
        | QuestionDetected
        | QuestionCancelled
        | AnswerDelta
        | AnswerDone
        | AnswerError
        | Status
        | AuthExpiring
        | ServerError
    ]
):
    root: Annotated[
        SessionReady
        | TranscriptPartial
        | TranscriptFinal
        | QuestionDetected
        | QuestionCancelled
        | AnswerDelta
        | AnswerDone
        | AnswerError
        | Status
        | AuthExpiring
        | ServerError,
        Field(discriminator="type"),
    ]
    """
    Any JSON message the gateway sends to the client. Every server message carries seq.
    """
