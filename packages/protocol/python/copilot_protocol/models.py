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
    Server-side timings for one answer pass, in milliseconds. turn_end_ms: speech end to question confirmed; detector_ms: detector call; first_token_ms: request to first model token; first_delta_ms: speech end (or force) to the first answer.delta sent. Fields are absent when they do not apply.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    turn_end_ms: Annotated[int | None, Field(ge=0)] = None
    detector_ms: Annotated[int | None, Field(ge=0)] = None
    first_token_ms: Annotated[int | None, Field(ge=0)] = None
    first_delta_ms: Annotated[int | None, Field(ge=0)] = None
    total_ms: Annotated[int | None, Field(ge=0)] = None


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
    meeting_title: str | None = None
    platform: Annotated[str, Field(min_length=1)]
    """
    Call platform, for example zoom, meet or teams.
    """
    client_version: Annotated[str, Field(min_length=1)]
    consent: Consent
    audio: AudioFormat


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


class SessionEnd(BaseModel):
    """
    User ends the session.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    type: Literal["session.end"]


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
    Final text for a segment.
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
        "other",
    ]
    """
    Question type from the detector.
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


class CloseCode(RootModel[Literal[1000, 1001, 4401, 4403, 4408, 4409, 4429, 4500]]):
    root: Literal[1000, 1001, 4401, 4403, 4408, 4409, 4429, 4500]
    """
    WebSocket close codes. 1000 ended normally; 1001 server going away (resume); 4401 token invalid or expired; 4403 no minutes left; 4408 no start or resume in time; 4409 another session active; 4429 rate limited; 4500 internal error (resume).
    """


class AudioFrameHeader(BaseModel):
    """
    Header of a binary audio frame: 10 bytes, then 16 kHz mono pcm16 audio (a 100 ms frame is 3,200 bytes; may be empty on a speech-end frame, and is always empty on the me channel). Byte layout: [0] channel uint8, [1] flags uint8, [2..5] frame_seq uint32 big-endian, [6..9] capture_ms uint32 big-endian. Binary frames are at most 64 KB.
    """

    model_config = ConfigDict(
        populate_by_name=True,
    )
    channel: Literal[0, 1]
    """
    0 = them (system output), 1 = me (microphone; speech flags only, no audio).
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
        | ModeSet
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
        | ModeSet
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
