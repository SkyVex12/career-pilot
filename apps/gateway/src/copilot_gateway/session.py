"""Live sessions (ARCHITECTURE.md, "Session lifecycle and resume" and "Gateway internals").

A `Session` lives for the whole call, across reconnects. Its core tasks (STT, the Valkey writer,
the lease, the 30 s flush, the watchdog) run in one TaskGroup. Each WebSocket connection runs a
reader and a sender on top, so a dropped socket leaves STT and answers running for the 120 s
resume window. Answers run outside the group, one task per question and pass.

Every server message gets the next `seq`, goes to a replay ring in memory and to the
`sess:{sid}:events` stream, and to the socket if one is attached.
"""

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast, get_args
from uuid import UUID

import asyncpg
import httpx2
from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketState

from copilot_core import (
    careeros,
    jobs,
    keys,
    knowledge,
    languages,
    logs,
    personal_ai,
    profiles,
    prompts,
    resumes,
    screen,
    versions,
    workplace,
)
from copilot_core.db import actor_account, user_tx
from copilot_core.embed import Embedder
from copilot_core.identities import Identities
from copilot_core.ids import uuid7
from copilot_core.llm import LLMDone, ModelRouter, prompt_cache_key
from copilot_core.packs import (
    PREPARED_DIGEST_POLICY,
    PREPARED_STANDUP_POLICY,
    Digest,
    FocusMaterial,
    MeetingContext,
    NodeRef,
    PackItem,
    SessionPack,
    assign_short_id,
    pack_items,
    ref_map,
    render_pack,
)
from copilot_core.pricing import cost_usd
from copilot_gateway import audio
from copilot_gateway.admission import ContextNotReady, DurableSession, WriterOwnershipLost
from copilot_gateway.answer import (
    AnswerEngine,
    MatchResult,
    PreparedMatcher,
    Question,
    ScreenAsk,
    is_answer_command,
    normalize_question,
)
from copilot_gateway.auth import AuthError, Principal, TokenVerifier, bearer_token
from copilot_gateway.companionhost import CompanionDesk
from copilot_gateway.config import Settings
from copilot_gateway.conversation import Budget, Conversation, Line, echo_share
from copilot_gateway.detector import Detector, named_or_model
from copilot_gateway.metering import METERING_VERSION, ConnectedLiveMeter, LivePrice
from copilot_gateway.retrieval import CompanyMemory
from copilot_gateway.router import decisions_route
from copilot_gateway.seathost import SeatDesk
from copilot_gateway.store import UNAVAILABLE, GatewayStore, RetainedLogOverflowError, SessionWriter
from copilot_gateway.stt import ResilientSTT, STTFactory, TimedResult
from copilot_gateway.stt.deepgram import DeepgramSTT
from copilot_gateway.turn import (
    Detection,
    TurnDetector,
    TurnModel,
    addressed_by_name,
    asking,
    is_backchannel,
    is_closing,
    substantive_words,
    update_opening,
)
from copilot_protocol import models

log = logging.getLogger(__name__)

# Close codes (ARCHITECTURE.md, "Close codes, reconnect policy and limits").
# question.detected carries a confidence. The detector no longer returns one (it never kept a
# question from firing in 546 benchmark decisions, and cost output tokens on every call): a
# detector decision is sent as 0.9, a hotkey question as 1.0, which the client uses to tell
# them apart.
DETECTED_CONFIDENCE = 0.9

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_UNAUTHORIZED = 4401
CLOSE_NO_MINUTES = 4403
CLOSE_NO_START = 4408
CLOSE_SESSION_ACTIVE = 4409
CLOSE_REPLACED = 4410  # a newer connection took this session; the client must not reconnect
CLOSE_CLIENT_OUTDATED = 4426  # older than min_client_version: update, do not reconnect
CLOSE_RATE_LIMITED = 4429
CLOSE_INTERNAL = 4500


@dataclass
class Services:
    """Everything a gateway task shares between its sessions."""

    settings: Settings
    store: GatewayStore
    jobs: jobs.JobQueue
    router: ModelRouter
    verifier: TokenVerifier
    stt_providers: Sequence[tuple[str, STTFactory]]
    embedder: Embedder | None = None
    # The user's own mic stream; None: the same providers as the other side's.
    stt_me_providers: Sequence[tuple[str, STTFactory]] | None = None
    turn_model: TurnModel | None = None
    task_id: str = field(default_factory=lambda: str(uuid7()))
    # Postgres as app_gateway (read only), for company memory; None without a database.
    pool: asyncpg.Pool | None = None
    # Which account a Google sign-in belongs to (app.identities), read through `pool`.
    identities: Identities | None = None


@dataclass
class SessionInfo:
    mode: str
    job_id: UUID | None
    title: str | None
    platform: str
    client_version: str
    store_transcript: bool
    started_at: datetime
    interview_id: UUID | None = None
    # A standup or meeting at a company (company workspaces), and the agenda typed for it.
    employer_id: UUID | None = None
    agenda: str | None = None
    # The company documents chosen for this call: their facts are read first.
    material_ids: list[UUID] = field(default_factory=list[UUID])
    # An interview for an application: the resume version its digest and pack come from.
    resume_version_id: UUID | None = None
    resume_is_base: bool = False
    # How the detailed answer is written: "bullets" or "paragraph" (answer_style.set).
    answer_style: str = "bullets"
    response_length: str = "default"
    # An online test with no call (coding mode): no audio, nothing transcribed.
    no_call: bool = False
    spoken_language: str = "en"


@dataclass
class ApplicationMemory:
    """What an interview for an application loads: its paired resume version's digest and the
    application's pack, never another version's."""

    digest: Digest | None
    pack: SessionPack | None
    resume_version_id: UUID | None
    is_base: bool


@dataclass(frozen=True)
class QuestionSource:
    """Capture timing when the utterance was detected, before asynchronous model work."""

    at_ms: int
    observed_at: float
    turn_at: float | None


@dataclass
class AutomaticQuestion:
    """The original words and visible output of one automatic card, kept only in memory."""

    text: str
    at_ms: int
    emitted: set[str] = field(default_factory=set[str])
    completed: set[str] = field(default_factory=set[str])


# Echo (see Session._settle_me): this many lines taken for echo within ECHO_WINDOW_S turn the
# status badge to a headphones hint, until ECHO_WINDOW_S passes without one.
ECHO_WARN_COUNT = 3
ECHO_WINDOW_S = 120.0
TOTALS_SAVE_S = 10.0  # how often the end line's running totals go into the live state
# The other side's speech counts as "around the same time" as a line of the user's this close.
ECHO_NEAR_MS = 500
# A question that fired this long before the other side's last line, with the user silent in
# between, still answers the turn (a remark or an audio check came after it).
FOLLOW_ANSWERED_MS = 15000
ECHO_TEXT_MS = 1500


def _type_of(cls: type[Any]) -> str:
    return get_args(cls.model_fields["type"].annotation)[0]


def _error_json(code: str, message: str, fatal: bool, seq: int = 1, **extra: Any) -> str:
    return models.ServerError.model_validate(
        {"type": "error", "seq": seq, "code": code, "message": message, "fatal": fatal, **extra}
    ).model_dump_json(exclude_none=True)


async def close_ws(ws: WebSocket, code: int, reason: str = "") -> None:
    if ws.application_state == WebSocketState.DISCONNECTED:
        return
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason[:120])


class HostSendQueue(asyncio.Queue[str | None]):
    """One socket's pending output; replay remains in its existing bounded event ring."""

    def __init__(self, max_entries: int, max_bytes: int) -> None:
        super().__init__(maxsize=max_entries)
        self.max_bytes = max_bytes
        self.bytes = 0

    def put_nowait(self, item: str | None) -> None:
        size = len(item.encode("utf-8")) if item is not None else 0
        if self.bytes + size > self.max_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)
        self.bytes += size

    def get_nowait(self) -> str | None:
        item = super().get_nowait()
        self.bytes -= len(item.encode("utf-8")) if item is not None else 0
        return item

    def close(self) -> None:
        # Closing must succeed even when the very reason to close is a full queue.
        while not self.empty():
            self.get_nowait()
        self.put_nowait(None)


class Session:
    def __init__(
        self,
        svc: Services,
        manager: "SessionManager",
        sid: UUID,
        principal: Principal,
        info: SessionInfo,
        digest: Digest | None,
        pack: SessionPack | None,
        *,
        seq: int = 0,
        ring: Sequence[tuple[int, str]] = (),
        restored: Mapping[str, str] | None = None,
    ) -> None:
        self.svc = svc
        self.settings = svc.settings
        self.router: ModelRouter = svc.router
        self.manager = manager
        self.sid = sid
        self.principal = principal
        self.info = info
        self.spoken_language = info.spoken_language
        self._language_lock = asyncio.Lock()
        self._language_generation = 0
        self._language_changes: set[asyncio.Task[None]] = set()
        self._language_requests: OrderedDict[UUID, str] = OrderedDict()
        self.account_id: UUID | None = None
        self.account_token: str | None = None
        self.private_context_account_id: str | None = (restored or {}).get(
            "private_context_account_id"
        )
        self.private_context_versions: tuple[str, ...] = ()
        self.digest = digest
        self.pack = pack
        restored = restored or {}
        self.metering_version = (
            restored.get("metering_version", "legacy-wall-v0") if restored else METERING_VERSION
        )
        payer = restored.get("usage_payer_account_id") or restored.get("account_id")
        self.usage_payer_account_id = UUID(payer) if payer else None
        self.live_price = LivePrice(
            restored.get("billing_price_version") or self.settings.billing_price_version,
            restored.get("billing_currency") or self.settings.billing_currency,
            int(
                restored.get(
                    "billing_rate_minor_per_hour", self.settings.billing_rate_minor_per_hour
                )
            ),
        )
        self.usage_meter = ConnectedLiveMeter(sid, self.live_price, self.log_entry)
        self._attach_lock = asyncio.Lock()
        self._retention_failed = False
        self.durable: DurableSession | None = None
        self._durable_renewed_at = time.monotonic()
        self._host_close_tasks: set[asyncio.Task[None]] = set()

        self.seq = seq
        self.ring: deque[tuple[int, str]] = deque(ring, maxlen=self.settings.events_maxlen)
        self.ws: WebSocket | None = None
        self.send_q: HostSendQueue | None = None
        self.status: dict[str, str] = {"stt": "ok", "llm": "ok", "memory": "ok"}

        self.writer = SessionWriter(
            svc.store,
            sid,
            svc.task_id,
            on_lost=self.on_lease_lost,
            buffer_max=self.settings.store_buffer_max,
            on_overflow=self._on_retention_overflow,
        )
        self.stt: ResilientSTT | None = None
        # The user's own microphone, transcribed live as a second stream (me_stt).
        self.stt_me: ResilientSTT | None = None
        if svc.stt_providers and not info.no_call:
            self.stt = self._stt_stream(self._on_stt, label="them")
            if self.settings.me_stt:
                self.stt_me = self._stt_stream(self._on_me_stt, svc.stt_me_providers, label="me")
        self.detector = Detector(
            svc.router,
            principal.display_name,
            self.settings.detector_max_tokens,
            on_decision=self._log_detection,
            cost=self._cost,
            decisions=decisions_route(self.settings, svc.router),
            spoken_language=self.spoken_language,
        )
        self.matcher = PreparedMatcher(
            svc.embedder,
            pack.prepared_answers if pack else [],
            self.settings.prepared_threshold(svc.embedder.model_id if svc.embedder else None),
            pack.embedding_model if pack else None,
        )
        self.turns = TurnDetector(
            self,
            silence_timer_ms=self.settings.silence_timer_ms,
            speculative_start_ms=self.settings.speculative_start_ms,
            final_wait_max_ms=self.settings.final_wait_max_ms,
            turn_model=svc.turn_model,
            hold=self.settings.speculative_hold,
            spoken_language=self.spoken_language,
            hold_min_words=self.settings.speculative_min_words,
            group_call=lambda: self.info.mode in prompts.GROUP_MODES,
            hold_any_min_words=self.settings.group_hold_min_words,
            early_interval_ms=self.settings.early_decision_interval_ms,
            carry_gap_ms=self.settings.continuation_gap_ms,
            first_show_min_words=self.settings.first_show_min_words,
            keep_added_words=self.settings.held_keep_added_words,
            strict_extensions=self.settings.answer_architecture != "legacy_dual",
            early_all_modes=self.settings.early_decision_all_modes,
        )
        self.answers = AnswerEngine(self)
        self._automatic_questions: OrderedDict[UUID, AutomaticQuestion] = OrderedDict()
        self._question_sources: OrderedDict[tuple[str, int], QuestionSource] = OrderedDict()
        self._latest_them_result_ms = 0
        self.dedupe = audio.FrameDedupe(
            {ch: int(restored[f"hw_{ch}"]) for ch in ("them", "me") if f"hw_{ch}" in restored}
        )
        self.seg_id = int(restored.get("seg_them", "0"))
        self.seg_me = int(restored.get("seg_me", "0"))
        s = self.settings
        self.conv = Conversation(
            principal.display_name,
            Budget(
                total=s.conversation_budget_tokens,
                summary=s.conversation_summary_tokens,
                answers=s.conversation_answers_tokens,
                chunk=s.conversation_summary_chunk_tokens,
            ),
            self._summarize,
            retry_s=s.conversation_summary_retry_s,
            on_change=self._save_conversation,
        )
        self._restore_conversation(restored)
        self.me_since: int | None = None
        self.me_audio_seen = False
        # The other side's speech, from the frames' flags: closed spans and the open one, for
        # the echo check on the user's lines.
        self.them_spans: deque[tuple[int, int]] = deque(maxlen=64)
        self.them_open: int | None = None
        self.echo_at: deque[float] = deque(maxlen=ECHO_WARN_COUNT)
        self.echo_warned = False
        # No mic speech at all while the other side has talked for a while (2026-10-29: one
        # computer sent none for two whole interviews and nothing said so).
        self.them_speech_ms = 0
        self.me_heard = False
        self.mic_warned = False
        # The mirror: the user has talked for a while and nothing at all came from the call
        # (2026-11-05: a two-hour interview with nine fragments of the other side).
        self.me_speech_ms = 0
        self.them_heard = False
        self.call_warned = False
        # A standup's update card (the pack's prepared points) is shown once, when the user
        # starts their update with nothing on screen.
        self._update_shown = False
        # The other side's last line was a goodbye: an interview ends sooner once it is quiet.
        self.them_closing = False
        # The end line's totals from earlier gateway tasks of this session (a resume).
        self.prior_totals: dict[str, Any] = {}
        with contextlib.suppress(ValueError, TypeError):
            self.prior_totals = dict(json.loads(restored.get("totals") or "{}"))
        self.totals_saved_at = 0.0
        self._settling: set[asyncio.Task[None]] = set()
        self._forced: set[asyncio.Task[None]] = set()  # forced questions awaiting their type
        # A takeover keeps its classifier after the typing deadline. A named-address
        # wrapper may finish first, so retain its underlying call until it settles too.
        self._forced_detections: dict[asyncio.Future[Detection], asyncio.Future[Detection]] = {}
        self._named_detections: dict[asyncio.Future[Detection], asyncio.Future[Detection]] = {}
        self._followed: set[int] = set()  # ends (capture ms) of the turns already followed
        self._last_detection: Detection | None = None
        self._refs = ref_map(digest, pack)
        self._prompt_ctx = self._build_prompt_ctx()
        # Company workspaces: live search over the chosen company's memory, and the question
        # vectors that prepared-answer matching and that search share (one call per text).
        self.company_memory: CompanyMemory | None = None
        if svc.pool is not None and info.employer_id is not None:
            self.company_memory = CompanyMemory(
                svc.pool,
                principal.user_id,
                info.employer_id,
                embedding_model=svc.embedder.model_id if svc.embedder else None,
                budget_ms=self.settings.retrieval_budget_ms,
                limit=self.settings.rerank_keep,
            )
        self._embeds: OrderedDict[str, asyncio.Task[list[float] | None]] = OrderedDict()
        # Engineering context: the company's document sections, searched by keyword here.
        self.sections: knowledge.Index | None = None
        self.sections_total = 0

        self.last_speech = time.monotonic()
        self.last_force = 0.0
        self.last_screenshot = 0.0
        self.disconnected_at: float | None = None
        self.auth_warned = False
        self.flushed_upto = restored.get("flushed_upto", "")
        self.ending = False
        self.lost = False
        # Guest seats: runs beside the call, never inside its task group (seathost.py).
        self.seatdesk = SeatDesk(self, dict(restored), resumed=bool(restored))
        self.companiondesk = CompanionDesk(self, dict(restored))
        self.core_task: asyncio.Task[None] | None = None
        self._core_group: asyncio.TaskGroup | None = None
        self._stt_tasks: list[asyncio.Task[None]] = []
        self.end_task: asyncio.Task[None] | None = None
        self._warm: asyncio.Task[None] | None = None
        self._warm_tasks: set[asyncio.Task[None]] = set()

    # Messages to the client.

    def emit(self, cls: type[Any], **fields: Any) -> int:
        qid = fields.get("question_id")
        if cls is models.QuestionDetected:
            q = self.answers.questions.get(qid) if isinstance(qid, UUID) else None
            fields.setdefault(
                "answer_strategy",
                (
                    self.answers.strategy(q)
                    if q is not None
                    else "legacy_dual"
                    if fields.get("screen") is not None or self.answers.open_problem()
                    else self.settings.answer_architecture
                ),
            )
        automatic = self._automatic_questions.get(qid) if isinstance(qid, UUID) else None
        pass_ = fields.get("pass")
        if automatic is not None and isinstance(pass_, str):
            if cls is models.AnswerDelta and fields.get("text", "").strip():
                automatic.emitted.add(pass_)
            elif cls is models.AnswerDone and pass_ in automatic.emitted:
                automatic.completed.add(pass_)
        if fields.get("question_type") == "respond" and not self.knows_respond:
            fields["question_type"] = "other"
        if fields.get("question_type") == "follow":  # the protocol has no such type
            fields["question_type"] = "other"
        if fields.get("question_type") in prompts.WIRE_TYPES:  # nor the types used at work
            fields["question_type"] = prompts.WIRE_TYPES[fields["question_type"]]
        self.seq += 1
        message = cls.model_validate({"type": _type_of(cls), "seq": self.seq, **fields})
        data = message.model_dump_json(exclude_none=True, by_alias=True)
        self.ring.append((self.seq, data))
        self.seatdesk.observe(self.seq, data)
        self.companiondesk.observe(self.seq, data)
        self.writer.event(self.seq, data)
        self.writer.set_state(last_seq=str(self.seq), updated_at=str(time.time()))
        if self.send_q is not None:
            try:
                self.send_q.put_nowait(data)
            except asyncio.QueueFull:
                self._disconnect_slow_host()
        return self.seq

    def error(self, code: str, message: str, fatal: bool = False, **extra: Any) -> None:
        self.emit(models.ServerError, code=code, message=message, fatal=fatal, **extra)

    def set_status(self, component: str, state: str, message: str) -> None:
        if self.status.get(component) == state:
            return
        self.status[component] = state
        self.emit(models.Status, component=component, state=state, message=message)

    def log_entry(self, entry: Mapping[str, Any]) -> None:
        # The writer invokes the fatal callback once. Retention must not interrupt
        # shutdown itself or strand the session's lease and active-session slot.
        with contextlib.suppress(RetainedLogOverflowError):
            self.writer.log_entry({**entry, "at": datetime.now(UTC).isoformat()})

    def _on_retention_overflow(self, error: RetainedLogOverflowError) -> None:
        self._retention_failed = True
        self.usage_meter.stop(discard=True)
        self.turns.cancel()
        self.answers.cancel_all()
        for task in (*self._settling, *self._forced, *self._language_changes):
            task.cancel()
        self._cancel_warm()
        log.error("%s", error)
        self.error(
            "retention_unavailable",
            "Session history cannot be retained. The session has stopped; please retry later.",
            fatal=True,
        )
        self.request_end("retention_unavailable", CLOSE_INTERNAL)

    def _disconnect_slow_host(self) -> None:
        """Bound a slow socket without discarding the session's durable output."""
        self.usage_meter.stop()
        q, self.send_q = self.send_q, None
        if q is not None:
            q.close()
        if self.ws is not None:
            task = asyncio.create_task(
                close_ws(self.ws, CLOSE_GOING_AWAY, "slow client; reconnect")
            )
            self._host_close_tasks.add(task)
            task.add_done_callback(self._host_close_tasks.discard)

    def _stt_stream(
        self,
        on_result: Callable[[TimedResult], Awaitable[None]],
        providers: Sequence[tuple[str, STTFactory]] | None = None,
        label: str = "",
        *,
        language: str | None = None,
        generation: int | None = None,
    ) -> ResilientSTT:
        s = self.settings
        selected = language or self.spoken_language
        generation = self._language_generation if generation is None else generation

        def configured(factory: STTFactory) -> STTFactory:
            def create(keyterms: Sequence[str]):
                stream = factory(keyterms)
                if isinstance(stream, DeepgramSTT):
                    stream.set_language(selected)
                elif selected != "en":
                    raise ValueError("Recognition provider does not support this call language")
                return stream

            return create

        async def current_result(result: TimedResult) -> None:
            if generation == self._language_generation:
                await on_result(result)

        def current_status(state: str, message: str) -> None:
            if generation == self._language_generation:
                self._on_stt_status(state, message)

        return ResilientSTT(
            [
                (name, configured(factory))
                for name, factory in (providers or self.svc.stt_providers)
            ],
            on_result=current_result,
            on_status=current_status,
            stall_s=s.stt_stall_s,
            failures_before_switch=s.stt_failures_before_switch,
            keepalive_s=s.stt_keepalive_s,
            nudge_s=s.stt_nudge_s,
            label=label,
            keyterms=self.pack.keyterms if self.pack is not None else (),
        )

    @property
    def capture_now(self) -> int:
        return self.conv.capture_now

    @property
    def me_audio(self) -> bool:
        """The client should send the user's speech audio (session.ready me_audio)."""
        return self.stt_me is not None

    # Conversation memory.

    def _restore_conversation(self, restored: Mapping[str, str]) -> None:
        if restored.get("conversation"):
            with contextlib.suppress(ValueError):
                self.conv.restore(json.loads(restored["conversation"]))
        elif restored.get("tail"):  # state written before conversation memory
            with contextlib.suppress(ValueError, TypeError, KeyError):
                self.conv.lines = [Line(**item) for item in json.loads(restored["tail"])]

    def _save_conversation(self) -> None:
        self.writer.set_state(conversation=self.conv.to_state())

    async def _summarize(self, previous: str, new_lines: str) -> str | None:
        """The rolling summary's update, on the cheapest route (job "summary"), logged with its
        tokens and cost; its text is never logged."""
        s = self.settings
        req = prompts.summary(
            self.principal.display_name, self.info.mode, previous, new_lines,
            s.conversation_summary_tokens, self.spoken_language,
        )  # fmt: skip
        started = time.monotonic()
        text, done = await self.router.complete("summary", req)
        ms = int((time.monotonic() - started) * 1000)
        log.info(
            "conversation summary on %s: %s in / %s out tokens in %s ms, stop=%s",
            done.model, done.usage.input_tokens, done.usage.output_tokens, ms, done.stop_reason,
        )  # fmt: skip
        self.log_entry(
            {
                "kind": "summary",
                "model": done.model,
                "tier": done.service_tier,
                "input_tokens": done.usage.input_tokens,
                "cached_tokens": done.usage.cached_tokens,
                "reasoning_tokens": done.usage.reasoning_tokens,
                "cache_write_tokens": done.usage.cache_write_tokens,
                "cache_write_30m_tokens": done.usage.cache_write_30m_tokens,
                "cache_write_12h_tokens": done.usage.cache_write_12h_tokens,
                "output_tokens": done.usage.output_tokens,
                "stop_reason": done.stop_reason,
                "cost_usd": self._cost(done),
                "latency_ms": ms,
            }
        )
        if done.stop_reason == "max_tokens":
            # Cut off at the cap: its end, the newest notes, is missing. The previous summary
            # stays and the lines stay verbatim until the next try.
            log.warning("conversation summary hit its cap; kept the previous one")
            return None
        return text

    # AnswerHost.

    def call_context(self) -> prompts.CallContext:
        return self.conv.context()

    def prompt_context(self) -> prompts.PromptContext:
        return self._prompt_ctx

    def prepared_source_version(self, policy: str) -> str | None:
        if policy == PREPARED_DIGEST_POLICY:
            return Digest.version_of(self.digest.text) if self.digest is not None else None
        if policy == PREPARED_STANDUP_POLICY:
            # Live company memory can change after the pack is loaded. Until lookup gives
            # a change-token proof, a pack hash alone cannot establish current workplace facts.
            if self.company_memory is not None or self.pack is None or self.pack.standup is None:
                return None
            return Digest.version_of(self.pack.standup.model_dump_json())
        return None

    def _build_prompt_ctx(self) -> prompts.PromptContext:
        employer = self.pack.employer if self.pack is not None else None
        at_work = employer is not None and employer.id == self.info.employer_id
        return prompts.PromptContext(
            user_name=self.principal.display_name,
            spoken_language=self.spoken_language,
            mode=self.info.mode,
            digest_text=self.digest.text if self.digest else None,
            pack_text=(
                render_pack(
                    self.pack, include_prepared=self.settings.answer_architecture == "legacy_dual"
                )
                if self.pack
                else None
            ),
            company=employer.name if at_work and employer else None,
            role=(employer.title or None) if at_work and employer else None,
            company_digest=(
                self.pack.knowledge.text
                if at_work and self.pack is not None and self.pack.knowledge is not None
                else None
            ),
        )

    # Company memory.

    def embedding(self, text: str) -> "asyncio.Task[list[float] | None] | None":
        """The text's vector, made once and shared by prepared-answer matching and memory
        search; None without an embedder. A failed call gives None."""
        embedder = self.svc.embedder
        if embedder is None:
            return None
        task = self._embeds.get(text)
        if task is None:

            async def run() -> list[float] | None:
                try:
                    [vec] = await embedder.embed([text])
                except Exception as e:
                    log.warning("question embedding failed: %s", e)
                    return None
                return vec

            task = asyncio.create_task(run())
            self._embeds[text] = task
            while len(self._embeds) > 32:
                self._embeds.popitem(last=False)  # dropped, not cancelled: others may await it
        return task

    def _vector(self, text: str) -> "Callable[[], Awaitable[list[float] | None]] | None":
        task = self.embedding(text)
        if task is None:
            return None

        async def get() -> list[float] | None:
            return await asyncio.shield(task)

        return get

    async def load_sections(self) -> None:
        """Load the company's document sections (a worker opened them into Valkey) and index
        them for keyword search. Without them the call still has the company's digest and
        facts; a worker is asked to open them for next time."""
        if not self._warm_current():
            return
        eid = self.info.employer_id
        if eid is None or self.info.mode not in prompts.GROUP_MODES or self.sections is not None:
            return
        uid = self.principal.user_id
        try:
            live = await self.svc.store.get_knowledge(uid, eid)
        except (*UNAVAILABLE, ValidationError, ValueError) as e:
            log.warning("could not load the company's document sections: %s", e)
            return
        if not self._warm_current():
            return
        if live is None:
            stamp = datetime.now(UTC).strftime("%Y%m%d%H")
            with contextlib.suppress(Exception):
                await self.svc.jobs.send(jobs.knowledge_publish(uid, eid, "missing", stamp))
            return
        # Tokenizing a few hundred sections takes tens of ms: off the event loop.
        index = await asyncio.to_thread(knowledge.Index.build, live.chunks)
        if not self._warm_current():
            return
        self.sections = index
        self.sections_total = len(live.chunks)
        log.info("loaded %d document sections of employer %s", len(live.chunks), eid)

    def _context_names(self) -> list[str]:
        """The company's names a question may point back at: its pack's keyterms and its
        digest's components, flows and terms."""
        if self.pack is None:
            return []
        extra = self.pack.knowledge.names if self.pack.knowledge is not None else []
        return list(dict.fromkeys([*self.pack.keyterms, *extra]))

    async def section_lines(
        self, text: str, deep: bool, *, call: prompts.CallContext | None = None
    ) -> list[str]:
        """The `<memory>` lines from the company's documents for a pass. Keyword search runs
        in memory, on the question and, when it is short or points back ("which one?"), on
        the question with the company's names said in the last lines of the call. The
        detailed answer also gets the vector search's sections when they are in in time."""
        index, s = self.sections, self.settings
        want = s.knowledge_deep_sections if deep else s.knowledge_fast_sections
        if index is None or want <= 0:
            return []
        lists = [[c for c, _ in index.search(text, want)]]
        names = self._context_names()
        if knowledge.needs_context(text, names):
            said = knowledge.context_names((call or self.conv.context()).recent, names)
            if said:
                again = index.search(f"{text} {' '.join(said)}", want)
                lists.append([c for c, _ in again])
        if deep and self.company_memory is not None:
            ids = await self.company_memory.section_ids(
                self._vector(text), want, s.memory_deep_wait_ms
            )
            found = [index.get(i) for i in ids]
            lists.append([c for c in found if c is not None])
        chunks = knowledge.merge(*lists, limit=want)
        words = None if deep else knowledge.FAST_EXCERPT_WORDS
        return knowledge.memory_lines(chunks, self._refs, text, words)

    def _memory_on(self) -> bool:
        return self.company_memory is not None and self.info.mode in ("standup", "meeting")

    async def private_memory(self) -> list[str]:
        url = self.settings.personal_ai_api_url
        if not url or not self.account_token or not self.account_id:
            return []
        requested_account = self.account_id
        requested_profile = self.principal.user_id
        requested_scope = (self.info.employer_id, self.info.job_id)
        try:
            async with httpx2.AsyncClient(timeout=self.settings.personal_ai_timeout_s) as client:
                response = await client.get(
                    url.rstrip("/") + "/v1/personal-ai/live-context",
                    headers={
                        "Authorization": "Bearer " + self.account_token,
                        "x-copilot-profile": str(self.principal.user_id),
                    },
                    params={
                        "session_id": str(self.sid),
                        **(
                            {"employer_id": str(self.info.employer_id)}
                            if self.info.employer_id
                            else {}
                        ),
                        **({"job_id": str(self.info.job_id)} if self.info.job_id else {}),
                    },
                )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            briefs = cast(list[dict[str, Any]], data.get("briefs", []))
            if not briefs:
                return []
            holder = await self.svc.store.lease_holder(self.sid)
            # Account/profile can change during the HTTP await (shared-profile resume).
            # Never attach the old account's response to the new capture owner.
            if (
                self.ending
                or holder != self.svc.task_id
                or self.account_id != requested_account
                or self.principal.user_id != requested_profile
                or (self.info.employer_id, self.info.job_id) != requested_scope
                or self.private_context_account_id not in (None, str(requested_account))
            ):
                return []
            self.private_context_account_id = str(requested_account)
            self.writer.set_state(private_context_account_id=str(requested_account), sharing="0")
            self.emit(models.Sharing, on=False)
            versions = tuple(str(b["id"]) for b in briefs)
            if versions != self.private_context_versions:
                self.private_context_versions = versions
                self.set_status(
                    "memory", "ok", "Private AI context versions: " + ", ".join(versions)
                )
            return [personal_ai.context_text(briefs)]
        except (httpx2.HTTPError, OSError, ValueError, KeyError, *UNAVAILABLE):
            # The existing live path stays usable; no stale private brief cache is reused.
            return []

    async def memory_lines(self, text: str, pass_: str) -> tuple[list[str], dict[str, int]]:
        """The `<memory>` block for a pass: what search found in the chosen company's memory
        within the pass's wait, minus what the pack already shows. Each line cites a short id
        that the session's refs now map, so the answer's citations become memory_refs."""
        # Keep contextual query expansion tied to this question across lookup waits.
        call = self.call_context()
        mem = self.company_memory
        if mem is None or not self._memory_on():
            sections = await self.section_lines(text, pass_ == "deep", call=call)
            return sections, ({"memory_sections": len(sections)} if sections else {})
        deep = pass_ == "deep"
        s = self.settings
        wait = s.memory_deep_wait_ms if deep else s.memory_fast_wait_ms
        sections: list[str] | None = None
        if deep and s.answer_architecture != "legacy_dual":
            # Both lookups share the same preloaded embedding, but are independent.
            # Their 150 ms waits overlap instead of serially adding up to 300 ms.
            (hits, stats), sections = await asyncio.gather(
                mem.hits(text, self._vector(text), deep, wait),
                self.section_lines(text, deep, call=call),
            )
        else:
            hits, stats = await mem.hits(text, self._vector(text), deep, wait)
        if not deep:
            hits = hits[: s.memory_fast_hits]
        in_pack = {i.node_id for i in pack_items(self.pack)} if self.pack else set[UUID]()
        if self.pack is not None and self.pack.knowledge is not None:
            in_pack |= {r.node_id for r in self.pack.knowledge.refs.values()}
        lines = workplace.memory_lines(hits, self._refs, in_pack)
        stats["memory_hits"] = len(lines)
        if sections is None:
            sections = await self.section_lines(text, deep, call=call)
        if sections:
            stats["memory_sections"] = len(sections)
        return lines + sections, stats

    def answer_style(self) -> str:
        return self.info.answer_style

    def response_length(self) -> str:
        return self.info.response_length

    def cache_extra(self) -> dict[str, str]:
        """Stored with exact-repeat answers, so deleting a company deletes its answers."""
        return {"employer": str(self.info.employer_id)} if self.info.employer_id else {}

    def refs(self) -> dict[str, NodeRef]:
        return self._refs

    def llm_status(self, ok: bool) -> None:
        if ok:
            self.set_status("llm", "ok", "Answers back to normal speed")
        else:
            self.set_status("llm", "degraded", "Answers are slower than usual")

    def store_text(self) -> bool:
        return self.info.store_transcript

    def cache_version(self) -> str:
        digest = self.digest.version if self.digest else "-"
        pack = str(self.pack.pack_id) if self.pack else "-"
        return f"{digest}|{pack}|{self.info.mode}|{self.spoken_language}"

    async def qa_get(self, qhash: str) -> dict[str, Any] | None:
        return await self.svc.store.qa_get(self.principal.user_id, qhash)

    async def qa_put(self, qhash: str, value: Mapping[str, Any]) -> None:
        await self.svc.store.qa_put(self.principal.user_id, qhash, value)

    def _cost(self, done: LLMDone) -> float | None:
        s = self.settings
        u = done.usage
        cost = cost_usd(
            done.model, done.service_tier, u.input_tokens, u.cached_tokens, u.output_tokens,
            s.model_prices, s.tier_price_multipliers,
            cache_write_tokens=u.cache_write_tokens,
            cache_write_30m_tokens=u.cache_write_30m_tokens,
            cache_write_12h_tokens=u.cache_write_12h_tokens,
        )  # fmt: skip
        return None if cost is None else float(cost)

    def _log_detection(self, entry: dict[str, Any], utterance: str) -> None:
        """Every detector decision, so misses and false fires can be explained afterwards;
        the text only with transcript consent."""
        log.info(
            "detector %s: decision=%s type=%s latency=%sms cached=%s%s",
            entry["text_hash"][:12], entry["decision"], entry["type"], entry["latency_ms"],
            entry["cached"], " cancelled" if entry.get("cancelled") else "",
        )  # fmt: skip
        self.log_entry({**entry, **({"text": utterance} if self.store_text() else {})})

    # TurnHost.

    def detect(self, utterance: str) -> "asyncio.Future[Detection]":
        duplicate = self._duplicate_question(utterance)
        if duplicate is not None:
            self.log_entry({"kind": "question_duplicate", "question_id": str(duplicate.qid)})
            skipped: asyncio.Future[Detection] = asyncio.get_running_loop().create_future()
            skipped.set_result(Detection("duplicate", duplicate.qtype))
            return skipped
        self._snapshot_question(utterance)
        transcript = self.conv.recent(self.settings.detector_context_tokens)
        turns = ""
        if self.info.mode in prompts.GROUP_MODES:
            turns = prompts.turn_taking(
                self.principal.display_name,
                [(ln.channel, ln.start_ms / 1000, ln.end_ms / 1000) for ln in self.conv.lines],
                # The utterance being decided ended at the other side's last speech end.
                max(self.capture_now, self.them_spans[-1][1] if self.them_spans else 0) / 1000,
            )
        call = asyncio.ensure_future(
            self.detector.detect(utterance, transcript, self.info.mode, turns)
        )
        call.add_done_callback(self._remember_detection)
        grace = self.settings.name_address_grace_ms
        if (
            grace is not None
            and self.info.mode in prompts.GROUP_MODES
            and addressed_by_name(utterance, prompts.first_name(self.principal.display_name))
        ):
            named = asyncio.ensure_future(named_or_model(call, grace / 1000))
            self._named_detections[named] = call
            call.add_done_callback(lambda _: self._named_detections.pop(named, None))
            return named
        return call

    def _duplicate_question(self, utterance: str) -> Question | None:
        """Suppress a short repeat of the latest usable card, never a later follow-up.

        Compare the words before continuation joins fragments. A deliberate force bypasses
        this detector path; failed or withdrawn cards do not make retries disappear.
        """
        window_ms = min(self.settings.continuation_gap_ms, 10_000)
        if window_ms <= 0:
            return None
        latest = next((q for q in reversed(self.answers.questions.values()) if not q.held), None)
        if (
            latest is None
            or latest.forced
            or latest.cancelled
            or latest.discarded
            or latest.superseded
        ):
            return None
        automatic = self._automatic_questions.get(latest.qid)
        if automatic is None or normalize_question(utterance) != automatic.text:
            return None
        if time.monotonic() - latest.t0 > window_ms / 1000:
            return None
        capture_at = max(
            self.conv.them_end,
            self.them_open or 0,
            self.them_spans[-1][1] if self.them_spans else 0,
        )
        if capture_at - automatic.at_ms > window_ms:
            return None
        if any(
            line.channel == "me"
            and line.end_ms > automatic.at_ms
            and (not line.text or substantive_words(line.text) > 0)
            for line in self.conv.lines
        ):
            return None
        if any(
            line.channel == "them"
            and line.end_ms > automatic.at_ms
            and normalize_question(line.text) != automatic.text
            and substantive_words(line.text) > 0
            for line in self.conv.lines
        ):
            return None
        if automatic.completed or any(not task.done() for task in latest.tasks.values()):
            return latest
        return None

    def _snapshot_question(self, text: str) -> None:
        # Include the capture end in the key: two identical questions can both be waiting
        # on slow detector calls. The later one must not overwrite the earlier source.
        at_ms = max(self._latest_them_result_ms, self.conv.them_end)
        turn = self.turns.turn
        turn_at = turn.t0 if turn is not None and not self.turns.speaking else None
        key = (normalize_question(text), at_ms)
        self._question_sources[key] = QuestionSource(at_ms, time.monotonic(), turn_at)
        self._question_sources.move_to_end(key)
        while len(self._question_sources) > 100:
            self._question_sources.popitem(last=False)

    def _question_anchor(self, text: str, t0: float | None) -> int | None:
        if t0 is None:
            return None
        words = normalize_question(text)
        sources = [source for (said, _), source in self._question_sources.items() if said == words]
        exact = [source for source in sources if source.turn_at == t0]
        if exact:
            return exact[-1].at_ms
        # Early detection precedes the speech-end turn. A held pass records its source at
        # creation; without one, use only a source observed by that turn's start. The small
        # tolerance covers synchronous held creation just after the early timer was read.
        early = [
            source
            for source in sources
            if source.turn_at is None and source.observed_at <= t0 + 0.05
        ]
        return early[-1].at_ms if early else None

    def _remember_automatic(self, qid: UUID, text: str, *, t0: float | None = None) -> None:
        words = normalize_question(text)
        automatic = self._automatic_questions.get(qid)
        if automatic is not None and automatic.text == words:
            return  # A later release or confirmation cannot advance the source's clock.
        question = self.answers.questions.get(qid)
        anchor = self._question_anchor(
            text, t0 if t0 is not None else question.t0 if question else None
        )
        if automatic is None:
            self._automatic_questions[qid] = AutomaticQuestion(
                words, anchor if anchor is not None else self.conv.them_end
            )
        else:
            automatic.text = words
            # A materially changed final may have a new snapshot for this same turn. If
            # not, retain its original source instead of borrowing a later turn's end.
            if anchor is not None:
                automatic.at_ms = anchor
        while len(self._automatic_questions) > 100:
            self._automatic_questions.popitem(last=False)

    def _remember_detection(self, call: "asyncio.Future[Detection]") -> None:
        if not call.cancelled() and call.exception() is None:
            self._last_detection = call.result()

    def start_match(self, utterance: str) -> "asyncio.Task[object] | None":
        if self._memory_on() and self.company_memory is not None:
            # Memory search starts with the detector, on the same text.
            self.company_memory.lookup(
                utterance,
                self._vector(utterance),
                refresh_completed=self.settings.answer_architecture != "legacy_dual",
            )
        # Prepared answers have no language metadata; keep their evidence in the prompt,
        # but generate localized output instead of displaying unverified verbatim text.
        if self.spoken_language != "en" or not self.matcher.enabled:
            return None
        match = self.matcher.match(utterance, self.embedding(utterance))
        return asyncio.create_task(match)  # pyright: ignore[reportReturnType]

    def begin_question(
        self,
        text: str,
        detection: Detection,
        *,
        speculative: bool,
        t0: float,
        match: "asyncio.Task[object] | None",
    ) -> UUID:
        original = text
        text = self._continued(text)
        q = Question(
            qid=uuid7(),
            text=text,
            qtype=detection.qtype,
            confidence=DETECTED_CONFIDENCE,
            addressed=True,
            speculative=speculative,
            forced=False,
            t0=t0,
            detector_ms=detection.latency_ms,
            match=match,
            decided_ms=detection.decided_ms,
            depth=detection.depth,
        )
        self._remember_automatic(q.qid, original, t0=t0)
        self.emit(
            models.QuestionDetected,
            question_id=q.qid,
            text=text,
            question_type=q.qtype,
            confidence=q.confidence,
            speculative=speculative,
        )
        self.answers.add(q)
        return q.qid

    def _continued(self, text: str) -> str:
        # Only a question the other side was saying goes on: a forced one (the hotkey, Respond,
        # a typed question) carries what was heard earlier, and its end is not theirs.
        last = self.conv.asked[-1] if self.conv.asked else None
        shown = self.answers.questions.get(UUID(last.qid)) if last is not None else None
        if shown is not None and shown.forced:
            return text
        starts = [a for a, _ in self.them_spans]
        if self.them_open is not None:
            starts.append(self.them_open)
        return self.conv.continued(text, self.settings.continuation_gap_ms, starts)

    def confirm_question(
        self, qid: UUID, text: str, turn_end_ms: int, final_ms: int | None
    ) -> None:
        self._remember_automatic(qid, text)
        text = self._continued(text)
        self.answers.confirm(qid, text, turn_end_ms, final_ms)
        # Paired with what the user says after the question's own words, which may already
        # have started by the time the turn is confirmed.
        self.conv.question(str(qid), text, self.conv.them_end)

    def cancel_question(self, qid: UUID) -> None:
        self.answers.cancel_speculative(qid)

    def start_held(self, text: str, t0: float, match: "asyncio.Task[object] | None") -> UUID:
        q = Question(
            qid=uuid7(),
            text=self._continued(text),
            qtype="other",
            confidence=0.0,
            addressed=False,
            speculative=True,
            forced=False,
            t0=t0,
            detector_ms=None,
            match=match,
            held=True,
        )
        self._remember_automatic(q.qid, text, t0=t0)
        self.answers.add(q)
        return q.qid

    def release_question(
        self, qid: UUID, text: str, detection: Detection, *, speculative: bool
    ) -> None:
        q = self.answers.questions.get(qid)
        if q is None:
            return
        self._remember_automatic(qid, text)
        text = self._continued(text)
        q.text = text
        q.qtype = detection.qtype
        q.confidence = 1.0 if q.forced else DETECTED_CONFIDENCE
        q.addressed = True
        q.detector_ms = detection.latency_ms
        q.decided_ms = detection.decided_ms
        q.depth = detection.depth
        q.speculative = speculative
        q.detected_at = datetime.now(UTC)
        self.emit(
            models.QuestionDetected,
            question_id=q.qid,
            text=text,
            question_type=q.qtype,
            confidence=q.confidence,
            speculative=speculative,
        )
        self.answers.release(qid)

    def discard_question(self, qid: UUID) -> None:
        self.answers.discard(qid)

    def retime_held(self, qid: UUID, t0: float) -> None:
        q = self.answers.questions.get(qid)
        if q is not None:
            q.early_ms = max(0, int((t0 - q.t0) * 1000))
            q.t0 = t0

    def retype_question(self, qid: UUID, detection: Detection) -> None:
        # Shown before the detector decided (turn.py): its type shapes the detailed answer,
        # and the confirmation sends it to the client.
        q = self.answers.questions.get(qid)
        if q is not None:
            q.qtype = detection.qtype
            if detection.latency_ms:
                q.detector_ms = detection.latency_ms
            if detection.decided_ms is not None:
                q.decided_ms = detection.decided_ms
            if detection.depth is not None:
                q.depth = detection.depth
            q.first_shown = True

    # STT callbacks.

    async def _on_stt(self, r: TimedResult) -> None:
        if self.ending or self.lost:
            return
        self._latest_them_result_ms = max(self._latest_them_result_ms, r.end_ms)
        if r.text:
            if r.is_final:
                self.emit(
                    models.TranscriptFinal,
                    channel="them",
                    seg_id=self.seg_id,
                    text=r.text,
                    start_ms=r.start_ms,
                    end_ms=r.end_ms,
                )
                self.conv.add(Line("them", r.start_ms, r.end_ms, r.text))
                self.them_closing = is_closing(r.text)
                if self.info.store_transcript:
                    self.log_entry(
                        {
                            "kind": "segment",
                            "channel": "them",
                            "seg_id": self.seg_id,
                            "start_ms": r.start_ms,
                            "end_ms": r.end_ms,
                            "text": r.text,
                        }
                    )
                self.seg_id += 1
                self.writer.set_state(seg_them=str(self.seg_id))
            else:
                self.emit(models.TranscriptPartial, channel="them", seg_id=self.seg_id, text=r.text)
        self.turns.on_result(r)

    def _on_stt_status(self, state: str, message: str) -> None:
        if self.ending or self.lost:
            return
        self.set_status("stt", state, message)

    # The user's own words (live STT of the mic).

    def _them_near(self, start_ms: int, end_ms: int) -> bool:
        """The other side was speaking around this time (their speech flags)."""
        lo, hi = start_ms - ECHO_NEAR_MS, end_ms + ECHO_NEAR_MS
        if self.them_open is not None and self.them_open <= hi:
            return True
        return any(a <= hi and b >= lo for a, b in self.them_spans)

    async def _on_me_stt(self, r: TimedResult) -> None:
        if self.ending or self.lost:
            return
        if not r.text:
            return
        if not r.is_final:
            # Not while the other side speaks: that text may be their echo.
            if not self._them_near(r.start_ms, r.end_ms):
                self.emit(models.TranscriptPartial, channel="me", seg_id=self.seg_me, text=r.text)
                self._maybe_update(r.text, r.start_ms)
            return
        seg = self.seg_me
        self.seg_me += 1
        self.writer.set_state(seg_me=str(self.seg_me))
        line = Line("me", r.start_ms, r.end_ms, r.text)
        if not self._them_near(line.start_ms, line.end_ms):
            self._accept_me(seg, line)
            return
        task = asyncio.create_task(self._settle_me(seg, line))
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)

    async def _settle_me(self, seg: int, line: Line) -> None:
        """Echo check for a line of the user's said while the other side was speaking: once
        the other side's transcript covers that time (at most echo_wait_ms), a line whose
        words mostly appear in theirs is their voice leaking into the mic past echo
        cancellation. It is dropped: not shown, not in the conversation, never stored."""
        s = self.settings
        deadline = time.monotonic() + s.echo_wait_ms / 1000
        while time.monotonic() < deadline:
            covered = any(
                ln.channel == "them" and ln.end_ms >= line.end_ms - ECHO_NEAR_MS
                for ln in self.conv.lines[-20:]
            )
            if covered or (self.them_open is None and not self.turns.interim):
                break
            await asyncio.sleep(0.05)
        if self.ending or self.lost:
            return
        heard = self.conv.them_text_between(
            line.start_ms - ECHO_TEXT_MS, line.end_ms + ECHO_TEXT_MS
        )
        share = echo_share(line.text, [*heard, self.turns.interim])
        if share >= s.echo_min_overlap:
            self._echo(seg, line, share)
        else:
            self._accept_me(seg, line)

    def _accept_me(self, seg: int, line: Line) -> None:
        if self.ending or self.lost:
            return
        self.emit(
            models.TranscriptFinal,
            channel="me",
            seg_id=seg,
            text=line.text,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
        )
        self.conv.add(line)
        if substantive_words(line.text) >= 2:
            self.turns.drop_carry()  # the user spoke: an unfinished sentence of theirs is over
        self._maybe_update(line.text, line.start_ms)
        self._maybe_follow(line)
        if self.info.store_transcript:  # the user's words follow the same consent
            self.log_entry(
                {
                    "kind": "segment",
                    "channel": "me",
                    "seg_id": seg,
                    "start_ms": line.start_ms,
                    "end_ms": line.end_ms,
                    "text": line.text,
                }
            )
        if self.echo_warned and (
            not self.echo_at or time.monotonic() - self.echo_at[-1] > ECHO_WINDOW_S
        ):
            self.echo_warned = False
            self.set_status("stt", "ok", "Transcription back to normal")

    def _maybe_follow(self, line: Line) -> None:
        """Follow the user: once they have said `follow_min_words` real words in reply to the
        other side's last turn, a card continues from what they are saying when no answer
        was shown for that turn (2026-10-28: the user wanted the answer to follow them; since
        2026-10-29 only where nothing answered, so a turn is never paid for twice). Once per
        turn of the other side; not while the detector is still deciding it, not for small
        talk, and not for pleasantries."""
        s = self.settings
        if not s.follow_user or not line.text:
            return
        lines = self.conv.lines
        # A "mhmm" from the other side is not a new turn to follow (2026-10-30).
        them = [
            ln
            for ln in lines
            if ln.channel == "them"
            and ln.end_ms <= line.start_ms + 300
            and not is_backchannel(ln.text)
        ]
        if not them:
            return
        anchor = them[-1].end_ms
        if anchor in self._followed or line.start_ms - anchor > s.follow_window_ms:
            return
        task = self.turns.task
        if task is not None and not task.done():
            return  # the turn is still being decided; the next line looks again
        said = " ".join(
            ln.text for ln in lines if ln.channel == "me" and ln.start_ms >= anchor - 300
        )
        if substantive_words(said) < s.follow_min_words:
            return
        mine = [ln.end_ms for ln in lines if ln.channel == "me" and ln.end_ms < them[-1].start_ms]
        since = mine[-1] if mine else 0
        # The turn already has an answer on screen, and its deep pass continues it (in the
        # 2026-10-29 interviews 15 of 18 follow cards were on such turns and mostly restated
        # the answer, each with its own deep pass). Answered: a question fired since the user
        # last spoke, shortly before the other side's last line. A remark or an audio check
        # after the question does not make the turn a new one (2026-11-10: 2 of 4 follow
        # cards came that way).
        asked = next(
            (
                a
                for a in reversed(self.conv.asked)
                if a.at_ms >= anchor - 1500
                or (a.at_ms >= since - 1500 and anchor - a.at_ms <= FOLLOW_ANSWERED_MS)
            ),
            None,
        )
        if asked is not None:
            self._followed.add(anchor)
            return
        if asking(said):
            # The user is asking, not answering ("could you say that again?"): on 2026-11-04
            # and 2026-11-10 such a card told them what the other side should have said.
            self._followed.add(anchor)
            return
        last = self._last_detection
        if last is not None and last.qtype == "small_talk":
            return
        text = " ".join(self.conv.them_text_between(since, anchor))
        words = text.split()
        if len(words) < 3:
            return
        self._followed.add(anchor)
        text = " ".join(words[-80:])
        q = Question(
            qid=uuid7(),
            text=text,
            qtype="follow",
            confidence=1.0,
            addressed=True,
            speculative=False,
            forced=False,
            t0=time.monotonic(),
            detector_ms=None,
            match=None,
        )
        self.emit(
            models.QuestionDetected,
            question_id=q.qid,
            text=text,
            question_type=q.qtype,
            confidence=q.confidence,
            speculative=False,
        )
        self.answers.add(q)
        self.answers.confirm(q.qid, text, None, supersede=False)
        log.info("following the user after %d words (no question shown)", len(said.split()))

    def _maybe_update(self, text: str, start_ms: int) -> None:
        """A work call's update card: when the user opens their own update ("so for my
        updates, yesterday I...") and nothing answered the other side's last turn, the pack's
        prepared update shows at once, with no model call for the short answer. The hand-off
        is often a pause, not a question: in three of eight real standups the user gave
        yesterday and today with nothing on screen, and the prepared points had been shown
        once in nine (2026-11-11 analysis). Once per session."""
        if (
            self.spoken_language != "en"
            or self._update_shown
            or self.info.mode not in prompts.GROUP_MODES
            or self.pack is None
        ):
            return
        if not update_opening(text):
            return
        question = workplace.STANDUP_QUESTIONS["update"]
        answer = next((a for a in self.pack.prepared_answers if a.question == question), None)
        if answer is None or not answer.talking_points:
            return
        task = self.turns.task
        if task is not None and not task.done():
            return  # the other side's turn is still being decided: the next words look again
        anchor = self.conv.them_end
        if any(a.at_ms >= anchor - 1500 and start_ms - a.at_ms <= FOLLOW_ANSWERED_MS
               for a in self.conv.asked):  # fmt: skip
            return  # a card for that turn is on screen already
        self._update_shown = True
        self._followed.add(anchor)
        found: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        found.set_result(MatchResult(answer, 1.0, 0))
        q = Question(
            qid=uuid7(),
            text=question,
            qtype="standup_status",
            confidence=1.0,
            addressed=True,
            speculative=False,
            forced=False,
            t0=time.monotonic(),
            detector_ms=None,
            match=found,  # pyright: ignore[reportArgumentType]
        )
        self.emit(
            models.QuestionDetected,
            question_id=q.qid,
            text=question,
            question_type=q.qtype,
            confidence=q.confidence,
            speculative=False,
        )
        self.answers.add(q)
        self.answers.confirm(q.qid, question, None, supersede=False)
        self.conv.question(str(q.qid), question, anchor)
        log.info("standup update card shown as the user began their update")

    def _echo(self, seg: int, line: Line, share: float) -> None:
        self.emit(
            models.TranscriptFinal,
            channel="me",
            seg_id=seg,
            text="",
            start_ms=line.start_ms,
            end_ms=line.end_ms,
            echo=True,
        )
        self.log_entry(
            {
                "kind": "echo",
                "seg_id": seg,
                "start_ms": line.start_ms,
                "end_ms": line.end_ms,
                "share": round(share, 2),
            }
        )
        log.info("me segment %d taken for echo (%.0f%% of its words)", seg, share * 100)
        now = time.monotonic()
        self.echo_at.append(now)
        if (
            len(self.echo_at) == ECHO_WARN_COUNT
            and now - self.echo_at[0] <= ECHO_WINDOW_S
            and not self.echo_warned
        ):
            self.echo_warned = True
            self.set_status(
                "stt", "degraded", "Your mic is picking up the other side; headphones help"
            )

    def _check_mic(self) -> None:
        s = self.settings
        if self.me_heard or self.mic_warned or not s.mic_silent_warn_s:
            return
        if self.them_speech_ms >= s.mic_silent_warn_s * 1000:
            self.mic_warned = True
            log.info("no mic speech after %d s of the other side's", self.them_speech_ms // 1000)
            self.set_status(
                "stt", "degraded", "We can't hear your microphone: check it in Settings"
            )

    def _mic_heard(self) -> None:
        self.me_heard = True
        if self.mic_warned:
            self.mic_warned = False
            self.set_status("stt", "ok", "Your microphone is coming through")

    def _check_call_audio(self) -> None:
        s = self.settings
        if self.them_heard or self.call_warned or not s.call_silent_warn_s:
            return
        if self.me_speech_ms >= s.call_silent_warn_s * 1000:
            self.call_warned = True
            log.info("no audio from the call after %d s of the user's", self.me_speech_ms // 1000)
            self.set_status(
                "stt", "degraded", "We can't hear the other side: check the call audio in Settings"
            )

    def _call_heard(self) -> None:
        self.them_heard = True
        if self.call_warned:
            self.call_warned = False
            self.set_status("stt", "ok", "The call's audio is coming through")

    def totals(self) -> dict[str, Any]:
        """The end line's counts for the whole session, earlier gateway tasks included."""
        prior = self.prior_totals

        def add(mine: Mapping[str, int], before: Any) -> dict[str, int]:
            out = dict(mine)
            if isinstance(before, dict):
                for k, v in cast(dict[str, Any], before).items():
                    if isinstance(v, int):
                        out[k] = out.get(k, 0) + v
            return out

        return {
            "questions": len(self.answers.questions) + int(prior.get("questions") or 0),
            "stats": add(self.answers.stats, prior.get("stats")),
            "detector": add(self.detector.stats, prior.get("detector")),
            # Held passes are the price of not waiting for the detector (2026-10-30): how
            # many started, were shown, were thrown away, and how many turns were decided
            # before their speech end.
            "turns": add(
                {
                    "held_started": self.turns.held_starts,
                    "held_released": self.turns.held_released,
                    "held_discarded": self.turns.held_discarded,
                    "early_started": self.turns.early_starts,
                    "early_used": self.turns.early_used,
                    "carried": self.turns.carried,
                    "first_shown": self.turns.first_shown,
                    "first_withdrawn": self.turns.first_withdrawn,
                    "held_kept": self.turns.held_kept,
                },
                prior.get("turns"),
            ),
            "stt": add(
                {
                    f"{label}_{k}": v
                    for label, stt in (("them", self.stt), ("me", self.stt_me))
                    if stt is not None
                    for k, v in (
                        ("failures", stt.total_failures),
                        ("nudges", stt.nudges),
                        ("nudge_saves", stt.nudge_saves),
                    )
                },
                prior.get("stt"),
            ),
        }

    # Lifecycle.

    def deep_auto_types(self) -> list[str]:
        s = self.settings
        if s.answer_architecture != "legacy_dual":
            return []  # Candidate experts are chosen per question, never by UI type guesses.
        # The client knows no "follow": a follow card reaches it as "other". Nor the types
        # used at work, which reach it as the protocol types of prompts.WIRE_TYPES.
        types = [t for t in s.deep_pass_auto_types if t in prompts.QUESTION_TYPES]
        return types if s.deep_pass_auto else []

    def state_fields(self) -> dict[str, Any]:
        return {
            "user_id": str(self.principal.user_id),
            "mode": self.info.mode,
            "status": "active",
            "job_id": str(self.info.job_id) if self.info.job_id else "",
            "interview_id": str(self.info.interview_id) if self.info.interview_id else "",
            "title": self.info.title or "",
            "platform": self.info.platform,
            "client_version": self.info.client_version,
            "store_transcript": "1" if self.info.store_transcript else "0",
            "started_at": self.info.started_at.isoformat(),
            "pack_id": str(self.pack.pack_id) if self.pack else "",
            "digest_version": self.digest.version if self.digest else "",
            "resume_revision_id": str(self.digest.resume_revision_id)
            if self.digest and self.digest.resume_revision_id
            else "",
            "resume_content_sha256": self.digest.resume_content_sha256 or "" if self.digest else "",
            "employer_id": str(self.info.employer_id) if self.info.employer_id else "",
            "agenda": self.info.agenda or "",
            "material_ids": ",".join(str(i) for i in self.info.material_ids),
            "answer_style": self.info.answer_style,
            "response_length": self.info.response_length,
            "resume_version_id": str(self.info.resume_version_id)
            if self.info.resume_version_id
            else "",
            "resume_base": "1" if self.info.resume_is_base else "0",
            # The client sends the mic's speech audio (session.ready me_audio): a client that
            # takes the call over later (GET /sessions/live) needs to know.
            "me_audio": "1" if self.me_audio else "0",
            "no_call": "1" if self.info.no_call else "0",
            "spoken_language": self.spoken_language,
            "language_switching": "1",
            "account_id": str(self.account_id) if self.account_id else "",
            **self.live_price.state_fields(),
            "metering_version": self.metering_version,
            "usage_payer_account_id": str(self.usage_payer_account_id)
            if self.usage_payer_account_id
            else "",
            **(
                {"private_context_account_id": self.private_context_account_id}
                if self.private_context_account_id
                else {}
            ),
            "task_id": self.svc.task_id,
            "writer_generation": str(self.durable.generation) if self.durable else "0",
            "writer_id": str(self.durable.writer_id) if self.durable else "",
            "workspace_id": str(self.principal.user_id),
            "created_by_account_id": str(self.usage_payer_account_id or self.account_id or ""),
        }

    def start_core(self) -> None:
        self.core_task = asyncio.create_task(self._run_core())
        self.seatdesk.start()
        self.companiondesk.start()

    async def _run_core(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                self._core_group = tg
                self._start_stt_tasks()
                tg.create_task(self.writer.run())
                tg.create_task(self._lease_loop())
                tg.create_task(self._flush_loop())
                tg.create_task(self._watchdog())
                tg.create_task(self._usage_loop())
                if self.settings.llm_warm_interval_s:
                    tg.create_task(self._keep_warm(self.settings.llm_warm_interval_s))
                if self.matcher.stale:
                    tg.create_task(self._rebuild_stale_pack())
        except* asyncio.CancelledError:
            raise
        except* Exception as eg:
            log.error("session %s failed", self.sid, exc_info=eg)
            await self.abandon(CLOSE_INTERNAL, "internal error")
        finally:
            self._core_group = None
            self._stt_tasks.clear()

    def _start_stt_tasks(self) -> None:
        if self._core_group is not None:
            self._stt_tasks = [
                self._core_group.create_task(stream.run())
                for stream in (self.stt, self.stt_me)
                if stream is not None
            ]

    async def _replace_language_streams(self, selected: str) -> None:
        old_language = self.spoken_language
        incoming, microphone = self.stt is not None, self.stt_me is not None
        self._language_generation += 1

        def build(language: str) -> tuple[ResilientSTT | None, ResilientSTT | None]:
            return (
                self._stt_stream(self._on_stt, label="them", language=language)
                if incoming
                else None,
                self._stt_stream(
                    self._on_me_stt, self.svc.stt_me_providers, label="me", language=language
                )
                if microphone
                else None,
            )

        old_tasks = list(self._stt_tasks)
        for task in old_tasks:
            task.cancel()
        await asyncio.gather(*old_tasks, return_exceptions=True)
        self._stt_tasks.clear()
        candidates = build(selected)
        try:
            # Finish all old-language work while recognition is stopped. Committing the
            # prepared replacement below then contains no cancellation points.
            await self.turns.change_language(self.spoken_language)
            pending = [*self._settling, *self._forced]
            for task in pending:
                task.cancel()
            warming = list(self._warm_tasks)
            self._cancel_warm()
            await asyncio.gather(*pending, *warming, return_exceptions=True)
            await self.answers.change_language()
            await self.conv.cancel_fold()
            # Close both old channels before opening either replacement. If one fails,
            # close all prepared channels and restore the previous language supervisors.
            for stream in candidates:
                if stream is not None:
                    await stream.prepare()
            if self.ending or self.lost:
                raise ValueError("This call has ended.")
        except BaseException as exc:
            for stream in candidates:
                if stream is not None:
                    await stream.discard_prepared()
            self.stt, self.stt_me = build(old_language)
            if not self.ending and not self.lost:
                self._start_stt_tasks()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ValueError(
                "Could not connect transcription in that language. Please try again."
            ) from exc
        self.stt, self.stt_me = candidates

    async def _set_language(self, message: models.SessionSetLanguage) -> None:
        """Apply an explicit language boundary without replacing the call or its history."""
        async with self._language_lock:
            error: str | None = None
            try:
                selected = languages.validate_language(
                    message.spoken_language, self.settings.verified_call_languages
                )
                self.manager.validate_recognition_language(selected)
                if self.ending or self.lost:
                    raise ValueError("This call has ended.")
                previous = self._language_requests.get(message.request_id)
                if previous is not None and previous != selected:
                    raise ValueError("This language request ID has already been used.")
                if previous is None and selected != self.spoken_language:
                    await self._replace_language_streams(selected)
                    self.spoken_language = self.info.spoken_language = selected
                    self.turns.spoken_language = selected
                    self.detector.spoken_language = selected
                    self.detector.cache.clear()
                    self._prompt_ctx = self._build_prompt_ctx()
                    self._last_detection = None
                    self._automatic_questions.clear()
                    self._question_sources.clear()
                    self.them_open = self.me_since = None
                    self.me_audio_seen = False
                    self.them_closing = False
                    self.writer.set_state(spoken_language=selected)
                    self.log_entry({"kind": "language", "spoken_language": selected})
                    self._start_stt_tasks()
                    # Clear transient transcript text; completed transcript lines remain.
                    for channel, segment in (("them", self.seg_id), ("me", self.seg_me)):
                        self.emit(
                            models.TranscriptPartial, channel=channel, seg_id=segment, text=""
                        )
                    self.set_status("stt", "ok", "Call language updated")
                self._language_requests[message.request_id] = selected
                while len(self._language_requests) > 100:
                    self._language_requests.popitem(last=False)
            except ValueError as exc:
                error = str(exc)
            self.emit(
                models.SessionLanguage,
                spoken_language=self.spoken_language,
                request_id=message.request_id,
                **({"error": error} if error else {}),
            )

    async def _keep_warm(self, every_s: float) -> None:
        """Keep the model connections open through a quiet spell (llm_warm_interval_s). The
        warm-up request is free, and its failures only cost the head start."""
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.router.warm()
            except Exception as e:  # never ends the session's task group
                log.info("keeping the model connections warm failed: %s", type(e).__name__)

    async def _lease_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.lease_renew_s)
            if self.ending or self.lost:
                return
            try:
                if self.durable is not None:
                    await self.durable.renew()
                    self._durable_renewed_at = time.monotonic()
                held = await self.svc.store.renew_lease(self.sid, self.svc.task_id)
                if not held:
                    self.on_lease_lost()
                    return
                await self.svc.store.refresh_active(self.principal.user_id, self.sid)
            except WriterOwnershipLost:
                self.on_lease_lost()
                return
            except (*UNAVAILABLE, asyncpg.PostgresError):
                if self.durable is not None and time.monotonic() - self._durable_renewed_at >= 30:
                    self._on_retention_overflow(
                        RetainedLogOverflowError("Durable session ownership could not be renewed")
                    )
                    return
                continue  # Valkey down: keep the session in memory

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.flush_interval_s)
            await self.flush_job()

    async def _usage_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.usage_interval_s)
            if self.ending or self.lost or self._retention_failed:
                return
            self.usage_meter.flush()

    async def flush_job(self) -> None:
        upto = self.writer.last_log_id
        if not upto or upto == self.flushed_upto:
            return
        ok = await self._send_job(
            "session.flush",
            f"session.flush:{self.sid}:{upto}",
            {"session_id": str(self.sid), "upto_entry_id": upto},
            attempts=1,
        )
        if ok:
            self.flushed_upto = upto
            self.writer.set_state(flushed_upto=upto)

    def warm_models(self) -> None:
        """Open the model connections while the user gets ready, so the first question does
        not pay the handshake (see ModelRouter.warm), then write the prompt cache."""

        async def warm() -> None:
            if not self._warm_current():
                return
            await self.load_sections()
            if not self._warm_current():
                return
            await self.router.warm()
            if not self._warm_current():
                return
            await self.prewarm_prompts()

        self._start_warm(warm)

    def _start_warm(self, work: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if self.ending or self.lost or self._retention_failed:
            return
        task = asyncio.create_task(work())
        self._warm = task
        self._warm_tasks.add(task)
        task.add_done_callback(self._warm_tasks.discard)

    def _warm_current(self) -> bool:
        """Only the latest live warm task may publish context or start another request."""
        task = asyncio.current_task()
        return (
            task is not None
            and task is self._warm
            and not task.cancelling()
            and not self.ending
            and not self.lost
            and not self._retention_failed
        )

    def _cancel_warm(self) -> None:
        # Retain every overlapping mode/prewarm task until it finishes. Do not
        # interrupt cleanup a second time when more than one stop path runs.
        for task in self._warm_tasks:
            if not task.done() and not task.cancelling():
                task.cancel()

    async def prewarm_prompts(self) -> None:
        """Write the prompt cache for the session's prefix (rules, digest, pack) before the
        first question, so it is not answered cold: in the October 2026 live test the first
        question's fast pass was the only uncached one, 1.6 s to its first token against
        1.0 s, and a cold gpt-6-luna first token measured up to 5 s. Once per session and per
        mode change; the default cache lives 30 minutes on OpenAI and 5 on Claude, and the
        questions that follow keep it warm."""
        ctx = self._prompt_ctx
        adaptive = self.settings.answer_architecture != "legacy_dual"
        warmed: list[tuple[object, str, Mapping[str, Any], str]] = []
        for job in self.settings.prewarm_jobs:
            if not self._warm_current():
                return
            route_job = (
                "adaptive_expert"
                if adaptive and job == "deep" and self.settings.adaptive_expert_route is not None
                else job
            )
            target = None
            if adaptive:
                expert = job in ("deep", "deep_light")
                req = prompts.adaptive_pass(
                    ctx, "", "(warm-up)", "other", self.settings.adaptive_max_tokens,
                    depth="expert" if expert else "standard", expert=expert,
                )  # fmt: skip
                route = next(
                    (r for r in self.router.routes.get(route_job, ()) if not r.refusal_only), None
                )
                key = prompt_cache_key(req.system)
                if route is not None and key is not None:
                    # Keep configured targets/order, but do not pay twice for an identical
                    # prefix after a successful write in this invocation. A failed/unsupported
                    # warm does not prevent the next configured attempt. Snapshot nested params.
                    if any(
                        provider is route.provider
                        and model == route.model
                        and params == route.params
                        and prefix == key
                        for provider, model, params, prefix in warmed
                    ):
                        continue
                    target = (route.provider, route.model, deepcopy(route.params), key)
            else:
                req = prompts.fast_pass(
                    ctx, "", "(warm-up)", None, self.settings.fast_pass_max_tokens
                )
                if job in ("deep", "deep_light"):
                    req = prompts.deep_pass(
                        ctx, "", "(warm-up)", "other", self.settings.deep_pass_max_tokens
                    )
            started = time.monotonic()
            done = await self.router.prewarm(route_job, req)
            if done is None:
                continue
            if target is not None:
                warmed.append(target)
            ms = int((time.monotonic() - started) * 1000)
            log.info(
                "prewarmed %s on %s: %s tokens in %s ms",
                job,
                done.model,
                done.usage.input_tokens,
                ms,
            )
            self.log_entry(
                {
                    "kind": "prewarm",
                    "pass": job,
                    **({"request_job": route_job} if route_job != job else {}),
                    "model": done.model,
                    "tier": done.service_tier,
                    "input_tokens": done.usage.input_tokens,
                    "cached_tokens": done.usage.cached_tokens,
                    "reasoning_tokens": done.usage.reasoning_tokens,
                    "cache_write_tokens": done.usage.cache_write_tokens,
                    "cache_write_30m_tokens": done.usage.cache_write_30m_tokens,
                    "cache_write_12h_tokens": done.usage.cache_write_12h_tokens,
                    "cost_usd": self._cost(done),
                    "latency_ms": ms,
                }
            )

    async def _mode_changed(self, mode: str) -> None:
        """A switch between standup and meeting at a company loads that company's pack for the
        new mode; then the new prefix is prewarmed."""
        if not self._warm_current():
            return
        if self.info.employer_id is not None and mode in ("standup", "meeting"):
            pack = await self.manager.employer_pack(
                self.principal.user_id, self.info.employer_id, mode, self.info.title,
                self.info.agenda, self.info.material_ids,
            )  # fmt: skip
            if not self._warm_current():
                return
            if pack is not None:
                self.set_pack(pack)
                self._prompt_ctx = self._build_prompt_ctx()
            await self.load_sections()
        if not self._warm_current():
            return
        await self.prewarm_prompts()

    def set_pack(self, pack: SessionPack) -> None:
        self.pack = pack
        self._refs = ref_map(self.digest, pack)
        self.matcher = PreparedMatcher(
            self.svc.embedder,
            pack.prepared_answers,
            self.settings.prepared_threshold(
                self.svc.embedder.model_id if self.svc.embedder else None
            ),
            pack.embedding_model,
        )
        self.writer.set_state(pack_id=str(pack.pack_id))

    async def _rebuild_stale_pack(self) -> None:
        """The pack's vectors came from another embedding model (or it has none): matching is
        off for this session, and the pack is rebuilt for the next one. The key names the
        gateway's embedding model, so this runs once per pack and model."""
        pack, embedder = self.pack, self.svc.embedder
        if pack is None or embedder is None:
            return
        info = self.info
        if info.employer_id is not None and pack.memory_version:
            log.info("company pack %s has no %s vectors; rebuilding it", pack.pack_id,
                     embedder.model_id)  # fmt: skip
            day = pack.standup.day if pack.standup else datetime.now(UTC).date().isoformat()
            with contextlib.suppress(Exception):
                await self.svc.jobs.send(
                    jobs.employer_pack_build(
                        self.principal.user_id,
                        info.employer_id,
                        pack.mode,
                        pack.memory_version,
                        day,
                        embedder.model_id,
                    )
                )
            return
        if info.job_id is not None or info.interview_id is not None:
            log.info(
                "pack %s has %s vectors, not %s: prepared-answer matching off; rebuilding it",
                pack.pack_id, pack.embedding_model or "no", embedder.model_id,
            )  # fmt: skip
            await self.manager.request_application_packs(
                self.principal.user_id, info.job_id, info.interview_id, pack
            )

    async def _send_job(
        self, job_type: str, key: str, payload: Mapping[str, Any], attempts: int = 3
    ) -> bool:
        message = jobs.envelope(job_type, key, self.principal.user_id, payload)
        for attempt in range(attempts):
            try:
                await self.svc.jobs.send(message)
                return True
            except Exception as e:
                log.warning("enqueue %s failed (attempt %d): %s", key, attempt + 1, e)
                await asyncio.sleep(0.5 * (attempt + 1))
        return False

    async def _watchdog(self) -> None:
        s = self.settings
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if self.ending:
                return
            elapsed_min = (datetime.now(UTC) - self.info.started_at).total_seconds() / 60
            if elapsed_min >= s.max_session_minutes:
                self.error("no_minutes", "This session reached its minute limit", fatal=True)
                self.request_end("minutes", CLOSE_NO_MINUTES)
                return
            if now - self.last_speech > s.idle_end_s:
                self.request_end("idle", CLOSE_NORMAL)
                return
            after_close = s.idle_end_after_close_s
            if (
                after_close
                and self.info.mode == "interview"
                and self.them_closing
                and now - self.last_speech > after_close
            ):
                log.info("ending: quiet for %.0f s after the other side's goodbye", after_close)
                self.request_end("idle", CLOSE_NORMAL)
                return
            if now - self.totals_saved_at > TOTALS_SAVE_S:
                self.totals_saved_at = now
                self.writer.set_state(totals=json.dumps(self.totals()))
            gone_since = self.disconnected_at if self.ws is None else None
            if gone_since is not None and now - gone_since > s.resume_window_s:
                self.request_end("no_resume", CLOSE_NORMAL)
                return
            await self._check_auth()

    async def _check_auth(self) -> None:
        expires_at = self.principal.expires_at
        if expires_at is None or self.ws is None:
            return
        remaining = expires_at - time.time()
        if remaining <= 0:
            self.error("token_expired", "Access token expired; refresh and resume", fatal=True)
            ws = self.ws
            await self._flush_socket()
            await close_ws(ws, CLOSE_UNAUTHORIZED, "token expired")
        elif remaining <= self.settings.auth_expiring_lead_s and not self.auth_warned:
            self.auth_warned = True
            self.emit(models.AuthExpiring, expires_in_s=int(remaining))

    def request_end(self, reason: str, code: int = CLOSE_NORMAL) -> None:
        # Stop on the accepted end request, before any asynchronous drains or summaries.
        self.usage_meter.stop()
        if self.end_task is None:
            self.end_task = asyncio.create_task(self.end(reason, code))

    async def end(self, reason: str, code: int = CLOSE_NORMAL) -> None:
        """session.end, idle, or no resume: flush the log, enqueue session.ended, close."""
        if self.ending:
            return
        self.ending = True
        self.usage_meter.stop()
        self.turns.cancel()
        self._cancel_warm()
        if not await self._shutdown_answers(within_s=5):
            log.error("session %s answer cancellation exceeded the shutdown deadline", self.sid)
        self.log_entry({"kind": "session.ended", "reason": reason})
        totals = self.totals()
        log.info(
            "session ended: reason=%s minutes=%.1f questions=%d stats=%s detector=%s turns=%s"
            " stt=%s",
            reason, (datetime.now(UTC) - self.info.started_at).total_seconds() / 60,
            totals["questions"], totals["stats"], totals["detector"], totals["turns"],
            totals["stt"],
        )  # fmt: skip
        self.writer.set_state(status="ended", ended_at=datetime.now(UTC).isoformat())
        if not await self.writer.drain(within_s=10):
            log.error("session %s ended with unwritten log entries (valkey down)", self.sid)
        if self.durable is not None:
            try:
                await self.durable.close(reason)
            except asyncpg.SerializationError:
                # Takeover won while the final drain awaited transport. Never publish an
                # end job or release the new owner's active slot from this generation.
                await self._stop_lost(self.ws)
                return
            except (*UNAVAILABLE, asyncpg.PostgresError) as exc:
                log.warning("session %s close intent awaits database recovery: %s", self.sid, exc)
        end_key = f"session.ended:{self.sid}"
        end_payload: dict[str, Any] = {"session_id": str(self.sid)}
        if self.durable is not None:
            end_key += f":g{self.durable.generation}"
            end_payload["writer_generation"] = self.durable.generation
        await self._send_job("session.ended", end_key, end_payload)
        with contextlib.suppress(*UNAVAILABLE):
            await self.svc.store.end_session(self.sid, self.svc.task_id)
            await self.svc.store.release_active(self.principal.user_id, self.sid)
        if self.ws is not None:
            ws = self.ws
            await self._flush_socket()
            await close_ws(ws, code, reason)
        await self._stop()

    async def abandon(self, code: int, reason: str) -> None:
        """Stop serving this session on this task without ending it (it stays resumable)."""
        if self.ending:
            return
        self.ending = True
        self.usage_meter.stop()
        self.turns.cancel()
        self._cancel_warm()
        if not await self._shutdown_answers(within_s=2):
            log.error("session %s answer cancellation exceeded the abandon deadline", self.sid)
        await self.writer.drain(within_s=2)
        with contextlib.suppress(*UNAVAILABLE):
            await self.svc.store.release_lease(self.sid, self.svc.task_id)
        if self.ws is not None:
            await close_ws(self.ws, code, reason)
        await self._stop()

    def on_lease_lost(self) -> None:
        """Another task resumed this session: stop here, write nothing more."""
        if self.lost or self.ending:
            return
        self.lost = True
        self.ending = True
        # Ownership already moved: fencing rejects this task's unconfirmed tail. Never
        # infer it from the new attachment's wall clock or write after lease loss.
        self.usage_meter.stop(discard=True)
        log.info("session %s moved to another task", self.sid)
        self.turns.cancel()
        self.answers.cancel_all()
        self._cancel_warm()
        self.end_task = asyncio.create_task(self._stop_lost(self.ws))

    async def _stop_lost(self, ws: WebSocket | None) -> None:
        if ws is not None:
            await close_ws(ws, CLOSE_REPLACED, "replaced by a newer connection")
        await self._stop()

    def _cancel_forced(self) -> set[asyncio.Future[Any]]:
        pending: set[asyncio.Future[Any]] = set(self._forced)
        for detection, call in self._forced_detections.items():
            pending.update((detection, call))
            # Cancel the actual classifier once. Its named wrapper finishes when the
            # call settles; cancelling both can interrupt the child's provider cleanup,
            # while cancelling only a wrapper that has not started can orphan the call.
            if not call.done() and not (isinstance(call, asyncio.Task) and call.cancelling()):
                call.cancel()
        for task in self._forced:
            if not task.done() and not task.cancelling():
                task.cancel()
        return pending

    async def _drain_forced(self, pending: set[asyncio.Future[Any]], *, within_s: float) -> bool:
        if not pending:
            return True
        _, unfinished = await asyncio.wait(pending, timeout=max(0, within_s))
        return not unfinished

    async def _shutdown_answers(self, *, within_s: float) -> bool:
        deadline = time.monotonic() + within_s
        forced = self._cancel_forced()
        answers_stopped = await self.answers.shutdown(within_s=within_s)
        forced_stopped = await self._drain_forced(forced, within_s=deadline - time.monotonic())
        return answers_stopped and forced_stopped

    async def _stop(self) -> None:
        self._cancel_warm()
        # Also covers lease-loss/core-stop paths that do not call end or abandon.
        if not await self._drain_forced(self._cancel_forced(), within_s=0.25):
            log.warning("forced-question cancellation exceeded the stop drain bound")
        self.manager.forget(self)
        await self.seatdesk.stop()
        await self.companiondesk.stop()
        changing = list(self._language_changes)
        for task in changing:
            task.cancel()
        await asyncio.gather(*changing, return_exceptions=True)
        self.conv.close()
        if self.company_memory is not None:
            memory_tasks = {
                task
                for lookup in self.company_memory.lookups.values()
                for task in (lookup.fts, lookup.full)
                if not task.done()
            }
            self.company_memory.close()
            if memory_tasks:
                _, pending = await asyncio.wait(memory_tasks, timeout=0.25)
                if pending:
                    log.warning("company memory cancellation exceeded the shutdown drain bound")
        for task in list(self._settling):
            task.cancel()
        if self.send_q is not None:
            self.send_q.close()
        task = self.core_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        for stream in (self.stt, self.stt_me):
            if stream is not None:
                await stream.discard_prepared()

    async def _flush_socket(self) -> None:
        q = self.send_q
        if q is None:
            return
        for _ in range(100):
            if q.empty():
                return
            await asyncio.sleep(0.01)

    # Connections.

    async def attach(
        self, ws: WebSocket, last_seq: int, replay: Sequence[tuple[int, str]] | None = None
    ) -> None:
        # Serialize only handover, not the connection lifetime. A third attachment cannot
        # overtake an in-flight close and leave an older reader attached to the new meter.
        async with self._attach_lock:
            if self.ending or self._retention_failed:
                await close_ws(ws, CLOSE_NO_START, "session has ended")
                return
            if self.ws is not None:
                old = self.ws
                self.usage_meter.stop()
                if self.send_q is not None:
                    self.send_q.close()
                await close_ws(old, CLOSE_REPLACED, "replaced by a newer connection")
            q = HostSendQueue(
                self.settings.host_send_max_entries, self.settings.host_send_max_bytes
            )
            self.ws, self.send_q, self.disconnected_at = ws, q, None
            self.auth_warned = False
            self.writer.set_state(status="active")
            if replay is None:
                replay = [(s, m) for s, m in self.ring if s > last_seq]
            # Stream the bounded replay separately; copying it into the socket queue would
            # reject a valid resume merely because it had more history than queue slots.
            self.emit(models.SessionLanguage, spoken_language=self.spoken_language)
            sender = asyncio.create_task(self._sender(ws, q, replay))
            reader = asyncio.create_task(self._reader(ws))
        try:
            await asyncio.wait({sender, reader}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if self.ws is ws:
                self.usage_meter.stop()
            for task in (sender, reader):
                task.cancel()
            await asyncio.gather(sender, reader, return_exceptions=True)
            if self.ws is ws:
                self.ws, self.send_q = None, None
                self.disconnected_at = time.monotonic()
                if not self.ending:
                    self.writer.set_state(status="reconnecting")
                    self.log_entry({"kind": "disconnected"})

    async def _sender(
        self, ws: WebSocket, q: HostSendQueue, replay: Sequence[tuple[int, str]] = ()
    ) -> None:
        for _, message in replay:
            await ws.send_text(message)
        if (
            self.ws is ws
            and self.send_q is q
            and not self.ending
            and not self._retention_failed
            and not self.info.no_call
            and self.metering_version == METERING_VERSION
            and self.usage_payer_account_id is not None
        ):
            # Initial replay includes session.ready. Until it is sent successfully there
            # is no confirmed connected service time to meter.
            self.usage_meter.start(self.usage_payer_account_id)
        while True:
            message = await q.get()
            if message is None:
                return
            await ws.send_text(message)

    async def _reader(self, ws: WebSocket) -> None:
        s = self.settings
        while True:
            raw = await ws.receive()
            if raw["type"] == "websocket.disconnect":
                if self.ws is ws:
                    self.usage_meter.stop()
                return
            if self.ws is not ws or self.ending or self._retention_failed:
                return
            data_bytes: bytes | None = raw.get("bytes")
            data_text: str | None = raw.get("text")
            if data_bytes is not None:
                if len(data_bytes) > s.max_binary_bytes:
                    self.error(
                        "frame_too_large", f"binary frames are at most {s.max_binary_bytes} bytes"
                    )
                    continue
                await self.on_audio(data_bytes)
            elif data_text is not None:
                await self.on_text(data_text)

    async def on_audio(self, data: bytes) -> None:
        if self.ending or self.lost or self._retention_failed:
            return
        async with self._language_lock:
            if self.ending or self.lost or self._retention_failed:
                return
            await self._on_audio(data)

    async def _on_audio(self, data: bytes) -> None:
        try:
            frame = audio.parse(data)
        except ValueError as e:
            self.error("bad_frame", str(e))
            return
        if not self.dedupe.accept(frame):
            return
        self.writer.set_state(**{f"hw_{frame.channel}": str(frame.frame_seq)})
        if frame.speech_start or frame.audio:
            self.last_speech = time.monotonic()
        if frame.channel == "me":
            await self._on_me_frame(frame)
            return
        if frame.speech_start or (frame.audio and self.stt is not None and not self.stt.in_speech):
            self._call_heard()
            self.them_open = frame.capture_ms
            if self.stt is not None:
                self.stt.speech(True)
            self.turns.on_speech_start()
        if frame.audio and self.stt is not None:
            await self.stt.send(frame.audio, frame.capture_ms)
        if frame.speech_end:
            if self.them_open is not None:
                self.them_spans.append((self.them_open, frame.capture_ms))
                self.them_speech_ms += max(0, frame.capture_ms - self.them_open)
                self.them_open = None
                self._check_mic()
            if self.stt is not None:
                self.stt.speech(False)
            self.turns.on_speech_end()
            if self.stt is not None:
                await self.stt.finalize()

    async def _on_me_frame(self, frame: audio.Frame) -> None:
        """The user's mic: speech audio goes to its own STT stream when the client sends it
        (session.ready me_audio). A speech segment that came with no audio (an older client,
        or me_stt off) leaves only its timing, as "(name spoke for N s)"."""
        stt = self.stt_me
        if frame.speech_start or (frame.audio and stt is not None and not stt.in_speech):
            self._mic_heard()
            self.me_since = frame.capture_ms
            self.me_audio_seen = False
            if stt is not None:
                stt.speech(True)
        if frame.audio and stt is not None:
            self.me_audio_seen = True
            await stt.send(frame.audio, frame.capture_ms)
        if frame.speech_end:
            if self.me_since is not None:
                self.me_speech_ms += max(0, frame.capture_ms - self.me_since)
                self._check_call_audio()
            if stt is not None:
                stt.speech(False)
            if self.me_audio_seen and stt is not None:
                await stt.finalize()
            elif self.me_since is not None:
                self.conv.add(Line("me", self.me_since, frame.capture_ms, ""))
            self.me_since = None
            self.me_audio_seen = False

    async def on_text(self, text: str) -> None:
        s = self.settings
        if len(text) > s.max_screenshot_bytes:
            self.error("message_too_large", "message is over the size limit")
            return
        try:
            message = models.ClientMessage.model_validate_json(text).root
        except ValidationError as e:
            self.error(
                "bad_message", e.errors(include_url=False)[0]["msg"] if e.errors() else "invalid"
            )
            return
        if len(text) > s.max_json_bytes and not isinstance(message, models.Screenshot):
            self.error("message_too_large", f"JSON messages are at most {s.max_json_bytes} bytes")
            return
        await self.handle(message)

    async def handle(self, message: Any) -> None:
        if self.ending or self.lost or self._retention_failed:
            return
        if isinstance(message, models.SessionSetLanguage):
            # A transport disconnect must not leave the provider transaction half-applied.
            task = asyncio.create_task(self._set_language(message))
            self._language_changes.add(task)
            task.add_done_callback(self._language_changes.discard)
            await asyncio.shield(task)
            return
        async with self._language_lock:
            if self.ending or self.lost or self._retention_failed:
                return
            await self._handle(message)

    async def _handle(self, message: Any) -> None:
        match message:
            case models.SessionStart() | models.SessionResume():
                self.error("unexpected_message", f"{message.type} is only valid as a first message")
            case models.AuthRefresh():
                await self._auth_refresh(message.access_token)
            case models.QuestionForce():
                if message.note_id is not None:
                    # Ask AI, on a guest's note: the host chose to ask it. The note's text
                    # is the typed question, as any typed question.
                    self.log_entry({"kind": "seat.ask", "note_id": str(message.note_id)})
                self._force(message.text, respond=message.kind == "respond")
            case models.Screenshot():
                self._screenshot(message)
            case models.AnswerExpand():
                if not self.answers.expand(message.question_id):
                    self.error("unknown_question", f"no question {message.question_id}")
            case models.LatencyReport():
                # Measurement only: a report for a question this task no longer holds (after
                # a resume on another task) is dropped without an error.
                self.answers.client_latency(
                    message.question_id,
                    message.pass_,
                    message.first_word_ms,
                    message.anchor,
                )
            case models.AnswerFeedback():
                self.log_entry(
                    {
                        "kind": "feedback",
                        "question_id": str(message.question_id),
                        "rating": message.rating,
                        **({"reason": message.reason} if self.store_text() else {}),
                    }
                )
            case models.ModeSet():
                self.info.mode = message.mode
                self._prompt_ctx = self._build_prompt_ctx()
                self.writer.set_state(mode=message.mode)
                self.log_entry({"kind": "mode", "mode": message.mode})
                self._start_warm(lambda: self._mode_changed(message.mode))
            case models.AnswerStyleSet():
                self.info.answer_style = message.answer_style
                if message.response_length is not None:
                    self.info.response_length = message.response_length
                self.writer.set_state(
                    answer_style=message.answer_style, response_length=self.info.response_length
                )
                self.log_entry(
                    {
                        "kind": "answer_style",
                        "answer_style": message.answer_style,
                        "response_length": self.info.response_length,
                    }
                )
            case models.SharingSet():
                if message.on and self.private_context_account_id:
                    self.error(
                        "unexpected_message", "Calls using private AI context cannot be shared"
                    )
                    return
                # Guests see nothing from here until sharing is on again (seatview.py): the
                # marker sits in the event stream, so replays and snapshots honour it too.
                self.writer.set_state(sharing="1" if message.on else "0")
                self.log_entry({"kind": "sharing", "on": message.on})
                self.emit(models.Sharing, on=message.on)
            case models.SessionEnd():
                # The user may skip the post-call outputs and keep the transcript only; the
                # worker reads this from the state when the session ends.
                if message.summarize is False:
                    self.writer.set_state(summarize="0")
                self.request_end("user", CLOSE_NORMAL)
            case _:
                self.error("unexpected_message", "unsupported message")

    async def _auth_refresh(self, token: str) -> None:
        try:
            principal = await self.svc.verifier.verify(token)
        except AuthError as e:
            principal = None
            reason = str(e)
        else:
            reason = "token is for another user"
            if self.usage_payer_account_id not in (None, principal.user_id):
                self.error(
                    "unauthorized",
                    "This session belongs to another paying account. Start a new session.",
                    fatal=True,
                )
                self.usage_meter.stop()
                ws = self.ws
                if ws is not None:
                    await self._flush_socket()
                    await close_ws(ws, CLOSE_UNAUTHORIZED, "payer mismatch")
                return
            if (
                self.private_context_account_id
                and str(principal.user_id) != self.private_context_account_id
            ):
                self.error("unauthorized", "Private session belongs to another account", fatal=True)
                if self.ws is not None:
                    await close_ws(self.ws, CLOSE_UNAUTHORIZED, "unauthorized")
                return
            if principal.user_id != self.principal.user_id:
                # A session of a profile: the new token must be its account's.
                principal = await self.manager.as_profile(principal, str(self.principal.user_id))
        if principal is None or principal.user_id != self.principal.user_id:
            self.error("unauthorized", reason, fatal=True)
            ws = self.ws
            if ws is not None:
                await self._flush_socket()
                await close_ws(ws, CLOSE_UNAUTHORIZED, "unauthorized")
            return
        self.principal = principal
        self.account_token = token
        self.auth_warned = False

    @property
    def knows_seats(self) -> bool:
        return versions.knows_seats(self.info.client_version)

    @property
    def knows_respond(self) -> bool:
        return versions.knows_respond(self.info.client_version)

    def _force(self, text: str | None, respond: bool = False) -> None:
        """The hotkey, a typed question, or the Respond button (`respond`): a suggested reply
        to what was just said, for a remark that needs one but is not a question."""
        now = time.monotonic()
        wait_s = self.settings.force_min_interval_s - (now - self.last_force)
        if wait_s > 0:
            self.error(
                "rate_limited",
                "question.force is limited to once per second",
                retry_after_ms=max(1, math.ceil(wait_s * 1000)),
            )
            return
        self.last_force = now
        self.last_speech = now  # a typed question is activity: the session is in use
        if text and is_answer_command(text):
            text = None  # "answer" typed into the Ask box: the same as Enter on an empty box
        if not text and (taken := self.turns.take_over()) is not None:
            self._force_taken(*taken, respond=respond)
            return
        if not text:
            heard = self.transcript_text(self.settings.force_context_s)
            text = " ".join(p for p in (heard, self.turns.interim) if p).strip()
        if not text:
            self.error("nothing_to_answer", "no question text and nothing heard yet")
            return
        self.turns.cancel()
        self.turns.reset()
        q = Question(
            qid=uuid7(),
            text=text,
            qtype="respond" if respond else "other",
            confidence=1.0,
            addressed=True,
            speculative=False,
            forced=True,
            t0=now,
            detector_ms=None,
            match=self.start_match(text),
        )
        self.emit(
            models.QuestionDetected,
            question_id=q.qid,
            text=text,
            question_type=q.qtype,
            confidence=1.0,
            speculative=False,
        )
        self.answers.add(q)
        self.answers.confirm(q.qid, text, None)
        self.conv.question(str(q.qid), text, self.capture_now)

    def _screenshot(self, msg: models.Screenshot) -> None:
        """Coding mode: the tasks on the user's screen, from 1 to 6 shots of one problem,
        answered as a question (copilot_core.screen). A new shot replaces the answer before."""
        s = self.settings
        now = time.monotonic()
        wait_s = s.force_min_interval_s - (now - self.last_screenshot)
        if wait_s > 0:
            self.error(
                "rate_limited",
                "screenshot is limited to once per second",
                retry_after_ms=max(1, math.ceil(wait_s * 1000)),
            )
            return
        shots = tuple(
            screen.Shot(
                data=i.data, media_type=i.media_type, width=i.width, height=i.height, title=i.title
            )
            for i in msg.images
        )
        for n, shot in enumerate(shots, start=1):
            if not screen.image_ok(shot):
                self.error("bad_message", f"image {n} is not a valid {shot.media_type}")
                return
        qid = msg.question_id or uuid7()
        if qid in self.answers.questions:
            self.error("bad_message", f"question {qid} already exists")
            return
        self.last_screenshot = now
        # Activity, as speech is: a coding session may go 10 minutes without a word
        # (2026-11-18: one ended as idle mid-problem, between screenshots).
        self.last_speech = now
        ask = ScreenAsk(
            shots=shots,
            answer_mode=msg.answer_mode or "interview",
            language=msg.language,
            default_language=msg.default_language or s.screen_default_language,
        )
        q = Question(
            qid=qid,
            text=screen.summary(None),
            qtype="coding",
            confidence=1.0,
            addressed=True,
            speculative=False,
            forced=True,
            t0=now,
            detector_ms=None,
            match=None,
            screen=ask,
        )
        store = self.store_text()
        self.log_entry(
            {
                "kind": "screen",
                "question_id": str(qid),
                "answer_mode": ask.answer_mode,
                **({"language": ask.language} if ask.language else {}),
                # The images themselves are never stored; their window titles only with consent.
                "images": [
                    {
                        "media_type": i.media_type,
                        "width": i.width,
                        "height": i.height,
                        "bytes": i.size_bytes,
                        **({"title": i.title} if store and i.title else {}),
                    }
                    for i in shots
                ],
            }
        )
        self.emit(
            models.QuestionDetected,
            question_id=qid,
            text=q.text,
            question_type=q.qtype,
            confidence=1.0,
            speculative=False,
            answer_strategy="legacy_dual",
        )
        self.answers.add(q)
        self.answers.confirm(qid, q.text, None)

    def _force_taken(
        self, qid: UUID, text: str, detection: "asyncio.Future[Detection]", *, respond: bool
    ) -> None:
        """A forced question that took over the turn still deciding (`TurnDetector.take_over`):
        its held fast pass is shown at once, with its prepared answer if one matched, and the
        detector's type, if it comes within `force_type_wait_ms`, decides the deep pass's
        shape. In an interview a Respond press on a question counts as one, so it may take a
        prepared answer; in a standup or meeting it stays a reply."""
        q = self.answers.questions.get(qid)
        if q is None:
            return
        reply = respond and self.info.mode in prompts.GROUP_MODES
        # The first detection identifies the button anchor before held text is released.
        q.forced, q.confidence, q.detector_ms = True, 1.0, None
        self.release_question(qid, text, Detection("answer", "respond" if reply else "other"),
                              speculative=False)  # fmt: skip
        wait_s = self.settings.force_type_wait_ms / 1000
        call = self._named_detections.get(detection, detection)
        self._forced_detections[detection] = call

        def settled(future: asyncio.Future[Detection]) -> None:
            if not future.cancelled():
                future.exception()  # Observe failures even if the typing waiter was cancelled.
            if detection.done() and call.done():
                self._forced_detections.pop(detection, None)

        detection.add_done_callback(settled)
        if call is not detection:
            call.add_done_callback(settled)

        async def typed() -> None:
            try:
                await asyncio.wait({detection}, timeout=wait_s)
            except asyncio.CancelledError:
                if not call.done() and not (isinstance(call, asyncio.Task) and call.cancelling()):
                    call.cancel()
                raise
            if self.ending or self.lost or self._retention_failed:
                return
            ok = detection.done() and not detection.cancelled() and not detection.exception()
            found = detection.result() if ok else None
            if found is not None and found.fires() and not reply:
                q.qtype, q.detector_ms, q.depth = found.qtype, found.latency_ms, found.depth
            if not (q.cancelled or q.discarded):
                self.answers.confirm(qid, q.text, None)
                self.conv.question(str(qid), q.text, self.capture_now)

        task = asyncio.create_task(typed())
        self._forced.add(task)
        task.add_done_callback(self._forced.discard)

    def transcript_text(self, seconds: float) -> str:
        return self.conv.them_text_since(self.capture_now - int(seconds * 1000))


Handler = Callable[[WebSocket], Any]


class SessionManager:
    """Sessions on this gateway task, and the connection handshake."""

    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.sessions: dict[UUID, Session] = {}

    def forget(self, session: Session) -> None:
        if self.sessions.get(session.sid) is session:
            del self.sessions[session.sid]

    async def employer_pack(
        self,
        uid: UUID,
        employer_id: UUID,
        mode: str,
        title: str | None = None,
        agenda: str | None = None,
        materials: Sequence[UUID] = (),
    ) -> SessionPack | None:
        """The company's pack for this call, never waiting on a model.

        The worker's pack (built when the company was picked) is used when it was rendered
        from the company's memory as it is now, and for today. Otherwise the gateway renders
        the same pack from Postgres itself (tens of ms; prepared standup vectors carry over
        from the stored pack) and asks a worker to build it for next time. Without a database
        the stored pack is used as it is. None: the company is not this user's, or there is
        nothing to load.

        A meeting gets its title and agenda, and the company's memory that matches the agenda,
        and a standup or meeting the approved facts of the documents chosen for it (`materials`,
        read first; live search still covers the whole company), on a copy with its own pack
        id, kept in Valkey for a resume."""
        svc, s = self.svc, self.svc.settings
        stored: SessionPack | None = None
        try:
            stored = await svc.store.get_employer_pack(uid, employer_id, mode)
        except (*UNAVAILABLE, ValidationError, ValueError) as e:
            log.warning("could not load the company pack: %s", e)
        if stored is not None and stored.employer is not None and stored.employer.id != employer_id:
            stored = None
        pack, fresh, related = stored, False, list[PackItem]()
        focus: list[tuple[UUID, list[PackItem]]] = []
        if svc.pool is not None:
            try:
                async with asyncio.timeout(s.pack_render_timeout_ms / 1000):
                    async with user_tx(svc.pool, uid) as conn:
                        if await workplace.employer(conn, employer_id) is None:
                            return None
                        settings = await conn.fetchval(
                            "select settings from app.accounts where id = $1",
                            actor_account.get() or uid,
                        )
                        today = workplace.local_today(
                            json.loads(settings) if isinstance(settings, str) else settings
                        )
                        version = await workplace.memory_version(conn, uid, employer_id)
                        current = (
                            stored is not None
                            and stored.memory_version == version
                            # Both work modes include a dated daily brief. This also refreshes
                            # older meeting packs built before daily status was included.
                            and stored.standup is not None
                            and stored.standup.day == str(today)
                        )
                        if not current:
                            pack = await workplace.render_employer_pack(
                                conn, uid, employer_id, mode, today
                            )
                            fresh = pack is not None
                        if mode == "meeting" and agenda:
                            hits = await workplace.search(
                                conn, uid, employer_id, agenda, limit=6, hops=0
                            )
                            shown = {i.node_id for i in pack_items(pack)} if pack else set[UUID]()
                            related = [
                                PackItem(node_id=h.node_id, text=h.text, date=h.day)
                                for h in hits
                                if h.node_id not in shown
                            ]
                        if materials:
                            focus = await workplace.material_facts(
                                conn, uid, employer_id, materials
                            )
            except (TimeoutError, OSError, asyncpg.PostgresError) as e:
                log.warning("company pack render failed (%s); using the stored pack", e)
                pack = stored
        if pack is None:
            return None
        if fresh:
            workplace.carry_vectors(pack, stored)
            log.info("rendered the %s pack of employer %s at session start", mode, employer_id)
            embedding = svc.embedder.model_id if svc.embedder else None
            with contextlib.suppress(Exception):
                await svc.jobs.send(
                    jobs.employer_pack_build(
                        uid,
                        employer_id,
                        mode,
                        pack.memory_version or "",
                        pack.standup.day if pack.standup else str(datetime.now(UTC).date()),
                        embedding,
                    )
                )
        if mode == "meeting" and (title or agenda):
            pack = pack.model_copy(deep=True)
            refs = ref_map(None, pack)
            for item in related:
                item.short_id = assign_short_id(item.node_id, refs)
                refs[item.short_id] = NodeRef(node_id=item.node_id, label=item.text[:120])
            pack.meeting = MeetingContext(title=title or None, agenda=agenda, related=related)
            fresh = True
        if focus:
            # Every fact of the chosen documents, even one another section shows too: this is
            # what the user asked to be read first. An agenda match moves here.
            pack = pack.model_copy(deep=True)
            chosen = {i.node_id for _, items in focus for i in items}
            if pack.meeting is not None:
                pack.meeting.related = [i for i in pack.meeting.related if i.node_id not in chosen]
            refs = ref_map(None, pack)
            ids = {r.node_id: sid for sid, r in refs.items()}
            pack.focus = []
            for document_id, items in focus:
                for item in items:
                    item.short_id = ids.get(item.node_id) or assign_short_id(item.node_id, refs)
                    refs[item.short_id] = NodeRef(node_id=item.node_id, label=item.text[:120])
                pack.focus.append(FocusMaterial(document_id=document_id, items=items))
            fresh = True
        if fresh:
            pack.pack_id = uuid7()
            pack.user_id = uid
            with contextlib.suppress(*UNAVAILABLE):
                await svc.store.put_session_pack(uid, pack, s.state_ttl_s)
        return pack

    async def application_memory(
        self, uid: UUID, job_id: UUID | None, interview_id: UUID | None
    ) -> ApplicationMemory:
        """The digest and pack for an interview for an application: its paired resume version
        (from Postgres) and that version's digest; the application's (or interview's) pack only
        if it was built from that same version, otherwise none until a worker rebuilds it.

        Without a database the pairing cannot be checked: the indexed pack names its version,
        whose digest goes with it; with no pack, no digest (it could be another version's)."""
        svc, store = self.svc, self.svc.store
        index = await store.pack_index(uid)
        pack_id = (index.get(f"interview:{interview_id}") if interview_id else None) or (
            index.get(f"job:{job_id}") if job_id else None
        )
        indexed = await store.get_pack_by_id(uid, pack_id) if pack_id else None
        paired: resumes.Pairing | None = None
        known = False
        if svc.pool is not None:
            try:
                async with asyncio.timeout(svc.settings.pack_render_timeout_ms / 1000):
                    async with user_tx(svc.pool, uid) as conn:
                        app_id = await self._application_of(conn, job_id, interview_id)
                        paired = await resumes.pairing(conn, app_id) if app_id else None
                        known = True
            except (TimeoutError, OSError, asyncpg.PostgresError) as e:
                log.warning("could not read the application's resume pairing (%s)", e)
        if known and paired is None:
            log.info("interview for an application that is not this user's; no pack")
            return ApplicationMemory(None, None, None, False)
        if paired is None:  # no database
            if indexed is None:
                return ApplicationMemory(None, None, None, False)
            if indexed.resume_version_id is None:  # built before resume versions: the base
                return ApplicationMemory(await store.get_digest(uid), indexed, None, True)
            version = indexed.resume_version_id
            digest = await resumes.version_digest(store.client, uid, version, False)
            return ApplicationMemory(digest, indexed, version, False)
        version = paired.resume_version_id
        digest = await resumes.version_digest(store.client, uid, version, paired.is_base)
        pack = indexed
        if pack is not None and not (
            pack.resume_version_id == version or (pack.resume_version_id is None and paired.is_base)
        ):
            log.info("pack %s is another resume version's; not used", pack.pack_id)
            pack = None
        if pack is None or pack.pairing != resumes.pairing_tag(paired):
            await self.request_application_packs(uid, job_id, interview_id, None)
        return ApplicationMemory(digest, pack, version, paired.is_base)

    @staticmethod
    async def _application_of(
        conn: Any, job_id: UUID | None, interview_id: UUID | None
    ) -> UUID | None:
        if interview_id is not None:
            found = await conn.fetchval(
                "select job_id from app.interviews where id = $1", interview_id
            )
            if found is not None:
                return found
        return job_id

    async def request_application_packs(
        self,
        uid: UUID,
        job_id: UUID | None,
        interview_id: UUID | None,
        pack: SessionPack | None,
    ) -> None:
        """Ask a worker to build the application's packs for its current pairing. Without a
        database the pairing is not known here: the worker reads it when it builds."""
        svc = self.svc
        embedding = svc.embedder.model_id if svc.embedder else None
        if svc.pool is None:
            context = f"interview:{interview_id}" if interview_id else f"job:{job_id}"
            digest = pack.digest_version if pack is not None else None
            message = jobs.pack_build(
                uid, "interview", context, datetime.now(UTC), digest or None, embedding
            )
            with contextlib.suppress(Exception):
                await svc.jobs.send(message)
            return
        try:
            async with user_tx(svc.pool, uid) as conn:
                app_id = await self._application_of(conn, job_id, interview_id)
                if app_id is None:
                    return
                messages = await resumes.pack_messages(
                    conn, svc.store.client, uid, app_id, embedding
                )
            for message in messages:
                await svc.jobs.send(message)
        except Exception as e:
            log.warning("could not ask for the application's packs: %s", e)

    async def request_resume_digest(
        self,
        uid: UUID,
        actor: UUID,
        job_id: UUID | None,
        interview_id: UUID | None,
    ) -> None:
        """Repair missing or legacy derived context through the normal worker path.

        A short source-specific lease coalesces reconnects. Each new lease gets a new
        operation key so a completed rebuild cannot prevent recovery after cache loss.
        The worker reads durable memory under its source lock and proves its provenance;
        the gateway never stamps an old digest with the current source revision.
        """
        svc = self.svc
        if svc.pool is None:
            return
        async with asyncio.timeout(2):
            async with user_tx(svc.pool, uid, actor_account_id=actor) as conn:
                if job_id is not None or interview_id is not None:
                    application = await self._application_of(conn, job_id, interview_id)
                    paired = await resumes.pairing(conn, application) if application else None
                    version = paired.resume_version_id if paired else None
                else:
                    version = await resumes.base_version(conn, uid)
                if version is None:
                    return
                revision = await conn.fetchval(
                    "select current_revision_id from app.resume_versions where id=$1", version
                )
            if revision is None:
                return
            repair_key = keys.resume_digest_repair(uid, revision)
            repair_id = str(uuid7())
            if not await svc.store.client.set(repair_key, repair_id, nx=True, ex=30):
                return
            try:
                await svc.jobs.send(
                    jobs.digest_rebuild(
                        uid,
                        version,
                        f"cache-repair:{repair_id}",
                        resume_revision_id=revision,
                    )
                )
            except BaseException:
                with contextlib.suppress(*UNAVAILABLE, TimeoutError):
                    async with asyncio.timeout(0.2):
                        await svc.store.client.delete(repair_key)
                raise

    def live_for_user(self, uid: UUID) -> Session | None:
        for s in self.sessions.values():
            if s.principal.user_id == uid and not s.ending:
                return s
        return None

    async def reject(
        self, ws: WebSocket, close: int, code: str, message: str, **extra: Any
    ) -> None:
        with contextlib.suppress(Exception):
            await ws.send_text(_error_json(code, message, True, **extra))
        await close_ws(ws, close, code)

    async def reject_outdated(self, ws: WebSocket, client_version: str) -> None:
        """An app older than min_client_version: the `client_outdated` error, then 4426. A
        release from before 4426 existed would reconnect on a code it does not know, so it
        gets 4410 (stop, no reconnect) instead; it offers the update at its next start."""
        minimum = self.svc.settings.min_client_version
        body = models.ClientOutdated.model_validate(versions.outdated(client_version, minimum))
        close = CLOSE_CLIENT_OUTDATED if versions.knows_4426(client_version) else CLOSE_REPLACED
        log.info("refused %s: older than the minimum %s (close %s)", client_version, minimum, close)
        await self.reject(ws, close, body.code, body.message)

    async def handle(self, ws: WebSocket) -> None:
        context = actor_account.set(None)
        try:
            await self._handle(ws)
        finally:
            actor_account.reset(context)

    async def _handle(self, ws: WebSocket) -> None:
        s = self.svc.settings
        token = bearer_token(ws.headers.get("authorization"))
        await ws.accept()
        try:
            principal = await self.svc.verifier.verify(token or "")
        except AuthError as e:
            await self.reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", str(e))
            return
        actor_account.set(principal.user_id)
        if s.require_career_os and not await self.linked(principal):
            # 4410: the app stops and does not reconnect; the error says why.
            await self.reject(ws, CLOSE_REPLACED, careeros.REQUIRED_CODE, careeros.REQUIRED_MESSAGE)
            return
        account = principal
        profile = ws.query_params.get(profiles.QUERY)
        if profile or self.svc.pool is not None:
            # The session acts for one of the account's profiles, or for "Me", which for a
            # user link is the person's shared workspace (ARCHITECTURE.md, "Profiles").
            resolved = await self.as_profile(principal, profile or "me")
            if resolved is None:
                await self.reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", "unknown profile")
                return
            principal = resolved
        logs.set_ids(user_id=principal.user_id)
        try:
            raw = await asyncio.wait_for(ws.receive(), s.first_message_timeout_s)
        except TimeoutError:
            await self.reject(
                ws, CLOSE_NO_START, "no_start", "send session.start or session.resume first"
            )
            return
        if raw["type"] == "websocket.disconnect":
            return
        try:
            first = models.ClientMessage.model_validate_json(raw.get("text") or "").root
        except ValidationError:
            first = None
        if isinstance(first, models.SessionStart):
            if versions.is_outdated(first.client_version, s.min_client_version):
                await self.reject_outdated(ws, first.client_version)
                return
            await self.start(ws, principal, first, account=account, account_token=token)
        elif isinstance(first, models.SessionResume):
            await self.resume(ws, principal, first, account=account, account_token=token)
        else:
            await self.reject(
                ws,
                CLOSE_NO_START,
                "no_start",
                "the first message must be session.start or session.resume",
            )

    async def linked(self, account: Principal) -> bool:
        """The account has an active Career-OS link (the stage requires one). Without a
        database to ask, or with it down, the session is refused."""
        if self.svc.pool is None:
            return False
        try:
            async with user_tx(self.svc.pool, account.user_id) as conn:
                return await careeros.is_linked(conn)
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("could not check the HubRuby link: %s", e)
            return False

    async def as_profile(self, account: Principal, profile: str) -> Principal | None:
        """The account's profile named `profile` (or the account itself), or None when it is
        not the account's or cannot be checked (no database, or the database is down)."""
        if self.svc.pool is None:
            return None
        try:
            return await profiles.resolve(self.svc.pool, account, profile)
        except profiles.ProfileError:
            return None
        except (OSError, asyncpg.PostgresError) as e:
            log.warning("could not check the profile: %s", e)
            return None

    async def _stale(self, sid: str) -> bool:
        """True if an active-session slot names a session nobody can resume any more."""
        if await self.svc.store.lease_holder(sid):
            return False
        state = await self.svc.store.load_state(sid)
        if not state or state.get("status") == "ended":
            return True
        updated = float(state.get("updated_at", "0") or 0)
        return time.time() - updated > self.svc.settings.resume_window_s

    async def _end_abandoned(self, uid: UUID, sid: str) -> None:
        """A session whose task died and that was never resumed: end it for the workers."""
        await self.svc.store.mark_ended(sid, "abandoned")
        message = jobs.envelope("session.ended", f"session.ended:{sid}", uid, {"session_id": sid})
        with contextlib.suppress(Exception):
            await self.svc.jobs.send(message)

    def validate_recognition_language(self, language: str) -> None:
        if language == "en":
            return
        paths = list(self.svc.stt_providers) + list(self.svc.stt_me_providers or [])
        if (
            not paths
            or not languages.recognition_supported(
                language, self.svc.settings.deepgram_model, self.svc.settings.stt_providers
            )
            or any(name != "deepgram" for name, _ in paths)
            or any(name != "deepgram" for name in self.svc.settings.stt_providers)
        ):
            raise ValueError(
                "Selected language is unavailable on the configured recognition paths."
            )

    # Retain the old internal hook for existing embedders.
    _validate_recognition_language = validate_recognition_language

    async def start(
        self,
        ws: WebSocket,
        principal: Principal,
        msg: models.SessionStart,
        *,
        account: Principal | None = None,
        account_token: str | None = None,
    ) -> None:
        store = self.svc.store
        uid = principal.user_id
        if self.svc.pool is None and (
            self.svc.settings.database_url or self.svc.settings.env not in ("dev", "test")
        ):
            await self.reject(ws, CLOSE_INTERNAL, "unavailable", "session database is unavailable")
            return
        actor_account.set((account or principal).user_id)
        try:
            spoken_language = languages.validate_language(
                msg.spoken_language if msg.spoken_language is not None else "en",
                self.svc.settings.verified_call_languages,
            )
            self.validate_recognition_language(spoken_language)
        except ValueError as exc:
            await self.reject(ws, CLOSE_NO_START, "invalid_message", str(exc))
            return
        if self.live_for_user(uid) is not None:
            await self.reject(
                ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
            )
            return
        sid = uuid7()
        # Every line logged for this session from here on (and in the tasks it starts) names it.
        logs.set_ids(session_id=sid)
        try:
            other = await store.claim_active(uid, sid)
            if other is not None:
                if not await self._stale(other):
                    await self.reject(
                        ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
                    )
                    return
                await self._end_abandoned(uid, other)
                if not await store.replace_active(uid, other, sid):
                    await self.reject(
                        ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
                    )
                    return
        except UNAVAILABLE as e:
            log.warning("valkey unavailable at session start: %s", e)
            with contextlib.suppress(*UNAVAILABLE):
                await store.release_active(uid, sid)
            await self.reject(
                ws, CLOSE_INTERNAL, "unavailable", "session state is unavailable; retry"
            )
            return
        digest: Digest | None = None
        pack: SessionPack | None = None
        at_work = msg.employer_id is not None and msg.mode in ("standup", "meeting")
        employer_id = msg.employer_id if at_work else None
        agenda = (msg.agenda or "").strip() or None
        material_ids = (
            list(dict.fromkeys(msg.material_ids or []))[: workplace.MAX_FOCUS_DOCUMENTS]
            if employer_id is not None
            else []
        )
        resume_version_id: UUID | None = None
        resume_is_base = False
        for_application = (
            employer_id is None
            and msg.mode == "interview"
            and (msg.job_id is not None or msg.interview_id is not None)
        )
        try:
            if for_application:
                memory = await self.application_memory(uid, msg.job_id, msg.interview_id)
                digest, pack = memory.digest, memory.pack
                resume_version_id, resume_is_base = memory.resume_version_id, memory.is_base
            else:
                # The base resume's digest. An interview for no application gets no
                # application's pack: only a mode pack from before resume versions, which
                # came from the one (base) resume too.
                digest = await store.get_digest(uid)
                if employer_id is None:
                    pack = await store.get_pack(uid, None, msg.mode, None)
                    if pack is not None and pack.resume_version_id is not None:
                        pack = None
        except (*UNAVAILABLE, ValidationError, ValueError) as e:
            log.warning("could not load digest or pack for %s: %s", uid, e)
        if employer_id is not None:
            pack = await self.employer_pack(
                uid, employer_id, msg.mode, msg.meeting_title, agenda, material_ids
            )
            if pack is None:
                employer_id = None  # not this user's company, or nothing to load at all
        if (
            self.svc.pool is not None
            and msg.mode == "interview"
            and pack is not None
            and digest is not None
            and (
                pack.digest_version != digest.version
                or pack.resume_revision_id != digest.resume_revision_id
            )
        ):
            pack = None
        info = SessionInfo(
            mode=msg.mode,
            job_id=msg.job_id,
            interview_id=msg.interview_id,
            title=msg.meeting_title,
            platform=msg.platform,
            client_version=msg.client_version,
            store_transcript=msg.consent.store_transcript,
            started_at=datetime.now(UTC),
            employer_id=employer_id,
            agenda=agenda,
            material_ids=material_ids if employer_id is not None else [],
            resume_version_id=resume_version_id,
            resume_is_base=resume_is_base,
            answer_style=msg.answer_style or "bullets",
            response_length=msg.response_length or "default",
            no_call=bool(msg.no_call),
            spoken_language=spoken_language,
        )
        session = Session(self.svc, self, sid, principal, info, digest, pack)
        session.account_id = (account or principal).user_id
        session.usage_payer_account_id = session.account_id
        session.account_token = account_token
        try:
            if self.svc.pool is not None:
                session.durable = await DurableSession.acquire(
                    self.svc.pool,
                    sid,
                    uid,
                    session.account_id,
                    metadata=session.state_fields(),
                )
                session.writer.durable = session.durable
            if not await store.refresh_active(uid, sid):
                if session.durable is not None:
                    await session.durable.close("admission_failed")
                await self.reject(
                    ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
                )
                return
            await store.take_lease(sid, self.svc.task_id)
        except (*UNAVAILABLE, asyncpg.PostgresError, ContextNotReady, TimeoutError) as exc:
            cause = exc.__cause__ if isinstance(exc, ContextNotReady) else exc
            log.warning(
                "session admission failed: error=%s sqlstate=%s",
                type(exc).__name__,
                getattr(cause, "sqlstate", None),
            )
            if session.durable is not None:
                with contextlib.suppress(Exception):
                    await session.durable.close("admission_failed")
            with contextlib.suppress(*UNAVAILABLE):
                await store.release_lease(sid, self.svc.task_id)
                await store.release_active(uid, sid)
            if isinstance(exc, ContextNotReady):
                try:
                    await self.request_resume_digest(
                        uid, session.account_id, msg.job_id, msg.interview_id
                    )
                except Exception as repair_error:
                    log.warning(
                        "resume context repair request failed: error=%s sqlstate=%s",
                        type(repair_error).__name__,
                        getattr(repair_error, "sqlstate", None),
                    )
                await self.reject(
                    ws,
                    CLOSE_RATE_LIMITED,
                    "memory_not_ready",
                    "Preparing your resume context. Retrying in a few seconds.",
                    retry_after_ms=5000,
                )
                return
            await self.reject(
                ws,
                CLOSE_INTERNAL,
                "unavailable",
                "session state is unavailable; retry",
            )
            return
        self.sessions[sid] = session
        session.writer.set_state(**session.state_fields())
        session.log_entry({"kind": "session.started", **session.state_fields()})
        # Which pack was loaded, so an analysis can find it after the next rebuild overwrites
        # it (twice after the 2026-10-26 and 2026-10-27 standups).
        loaded = (
            f"{pack.pack_id} built={pack.built_at} memory={pack.memory_version}"
            f" keyterms={len(pack.keyterms)}"
            if pack is not None
            else "none"
        )
        log.info(
            "session started: mode=%s platform=%s client=%s consent=%s digest=%s pack=%s",
            info.mode, info.platform, info.client_version, info.store_transcript,
            digest is not None, loaded,
        )  # fmt: skip
        session.start_core()
        if session.durable is not None and not await session.writer.drain(within_s=2):
            await session.end("admission_failed", CLOSE_INTERNAL)
            await self.reject(
                ws, CLOSE_INTERNAL, "unavailable", "session state could not be saved; retry"
            )
            return
        session.warm_models()
        session.emit(
            models.SessionReady,
            session_id=sid,
            server_time=datetime.now(UTC),
            limits={"max_minutes": self.svc.settings.max_session_minutes},
            me_audio=session.me_audio,
            deep_auto_types=session.deep_auto_types(),
            spoken_language=session.spoken_language,
            language_switching=True,
        )
        if digest is None:
            session.set_status("memory", "degraded", "No career digest loaded")
        await session.attach(ws, last_seq=0)

    async def resume(
        self,
        ws: WebSocket,
        principal: Principal,
        msg: models.SessionResume,
        *,
        account: Principal | None = None,
        account_token: str | None = None,
    ) -> None:
        store = self.svc.store
        uid = principal.user_id
        sid = msg.session_id
        logs.set_ids(session_id=sid)
        log.info("session resume from seq %d", msg.last_seq)
        actor = (account or principal).user_id
        actor_account.set(actor)
        if self.svc.pool is None and (
            self.svc.settings.database_url or self.svc.settings.env not in ("dev", "test")
        ):
            await self.reject(ws, CLOSE_INTERNAL, "unavailable", "session database is unavailable")
            return
        local = self.sessions.get(sid)
        if local is not None and local.principal.user_id == uid and not local.ending:
            if local.usage_payer_account_id not in (None, actor):
                await self.reject(
                    ws,
                    CLOSE_UNAUTHORIZED,
                    "payer_mismatch",
                    "This session belongs to another paying account. Start a new session.",
                )
                return
            if local.private_context_account_id and local.private_context_account_id != str(actor):
                await self.reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", "Private session")
                return
            try:
                if local.durable is not None:
                    await local.durable.renew()
                if not await store.refresh_active(uid, sid):
                    await self.reject(
                        ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
                    )
                    return
                if not await store.take_resume_lease(sid, self.svc.task_id):
                    local.on_lease_lost()
                    await store.release_active(uid, sid)
                    await self.reject(
                        ws, CLOSE_NO_START, "session_not_found", "no resumable session with that id"
                    )
                    return
            except (*UNAVAILABLE, asyncpg.PostgresError, WriterOwnershipLost):
                await self.reject(
                    ws, CLOSE_INTERNAL, "unavailable", "session state is unavailable; retry"
                )
                return
            local.account_id = actor
            local.writer.set_state(account_id=str(actor))
            local.account_token = account_token
            local.principal = principal
            local.log_entry({"kind": "resumed", "task_id": self.svc.task_id})
            await local.attach(ws, msg.last_seq)
            return
        durable: DurableSession | None = None
        try:
            state = await store.load_state(sid)
            if not state or state.get("user_id") != str(uid) or state.get("status") == "ended":
                await self.reject(
                    ws, CLOSE_NO_START, "session_not_found", "no resumable session with that id"
                )
                return
            try:
                languages.validate_language(
                    state.get("spoken_language") or "en", self.svc.settings.verified_call_languages
                )
                self.validate_recognition_language(state.get("spoken_language") or "en")
            except ValueError as exc:
                await self.reject(ws, CLOSE_NO_START, "invalid_message", str(exc))
                return
            if state.get("private_context_account_id") not in (None, "", str(actor)):
                await self.reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", "Private session")
                return
            payer = state.get("usage_payer_account_id") or state.get("account_id")
            if payer and payer != str(actor):
                await self.reject(
                    ws,
                    CLOSE_UNAUTHORIZED,
                    "payer_mismatch",
                    "This session belongs to another paying account. Start a new session.",
                )
                return
            other = await store.claim_active(uid, sid)
            if other is not None and other != str(sid):
                if not await self._stale(other):
                    await self.reject(
                        ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
                    )
                    return
                await self._end_abandoned(uid, other)
                if not await store.replace_active(uid, other, sid):
                    await self.reject(
                        ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
                    )
                    return
            if not await store.refresh_active(uid, sid):
                await self.reject(
                    ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
                )
                return
            if self.svc.pool is not None and int(state.get("writer_generation") or 0) > 0:
                durable = await DurableSession.acquire(self.svc.pool, sid, uid, actor)
                for key, value in (durable.metadata or {}).items():
                    if value is not None:
                        state[key] = str(value)
                state["usage_payer_account_id"] = str(actor)
                if durable.private_account_id:
                    state["private_context_account_id"] = durable.private_account_id
            if not await store.take_resume_lease(sid, self.svc.task_id):
                if durable is not None:
                    with contextlib.suppress(Exception):
                        await durable.close("admission_failed")
                await store.release_active(uid, sid)
                await self.reject(
                    ws, CLOSE_NO_START, "session_not_found", "no resumable session with that id"
                )
                return
            replay = await store.events_after(sid, msg.last_seq)
            seq = max(
                int(state.get("last_seq", "0")), await store.last_event_seq(sid), msg.last_seq
            )
            rv = state.get("resume_version_id")
            if rv:
                digest = await resumes.version_digest(
                    store.client, uid, UUID(rv), state.get("resume_base") == "1"
                )
            elif (
                "resume_version_id" in state
                and state.get("mode") == "interview"
                and (state.get("job_id") or state.get("interview_id"))
            ):
                digest = None  # an application with no resume: no digest
            else:
                digest = await store.get_digest(uid)
            pack = None
            if state.get("pack_id"):
                pack = await store.get_pack_by_id(uid, state["pack_id"])
            if (
                durable is not None
                and state.get("resume_revision_id")
                and (
                    digest is None or str(digest.resume_revision_id) != state["resume_revision_id"]
                )
            ):
                raise ContextNotReady("Resume context changed. Start a new session.")
            if (
                pack is not None
                and state.get("mode") == "interview"
                and digest is not None
                and (
                    pack.digest_version != digest.version
                    or pack.resume_revision_id != digest.resume_revision_id
                )
            ):
                pack = None
        except (*UNAVAILABLE, asyncpg.PostgresError, ContextNotReady) as exc:
            if durable is not None:
                with contextlib.suppress(Exception):
                    await durable.close("admission_failed")
            await self.reject(
                ws,
                CLOSE_INTERNAL,
                "memory_not_ready" if isinstance(exc, ContextNotReady) else "unavailable",
                str(exc)
                if isinstance(exc, ContextNotReady)
                else "session state is unavailable; retry",
            )
            return
        info = SessionInfo(
            mode=state.get("mode", "interview"),
            job_id=UUID(state["job_id"]) if state.get("job_id") else None,
            interview_id=UUID(state["interview_id"]) if state.get("interview_id") else None,
            title=state.get("title") or None,
            platform=state.get("platform", ""),
            client_version=state.get("client_version", ""),
            store_transcript=state.get("store_transcript") == "1",
            started_at=datetime.fromisoformat(state["started_at"])
            if state.get("started_at")
            else datetime.now(UTC),
            employer_id=UUID(state["employer_id"]) if state.get("employer_id") else None,
            agenda=state.get("agenda") or None,
            material_ids=[UUID(i) for i in (state.get("material_ids") or "").split(",") if i],
            resume_version_id=UUID(rv) if rv else None,
            resume_is_base=state.get("resume_base") == "1",
            answer_style=state.get("answer_style") or "bullets",
            response_length=state.get("response_length") or "default",
            no_call=state.get("no_call") == "1",
            spoken_language=state.get("spoken_language") or "en",
        )
        session = Session(
            self.svc, self, sid, principal, info, digest, pack, seq=seq, ring=replay, restored=state
        )
        session.account_id = actor
        session.account_token = account_token
        session.durable = durable
        session.writer.durable = durable
        self.sessions[sid] = session
        session.writer.set_state(**session.state_fields())
        session.log_entry({"kind": "resumed", "task_id": self.svc.task_id})
        session.start_core()
        await session.attach(ws, msg.last_seq, replay=replay)

    async def shutdown(self) -> None:
        """Deploy or scale-in: close every socket with 1001; sessions stay resumable."""
        await asyncio.gather(
            *(
                s.abandon(CLOSE_GOING_AWAY, "server going away")
                for s in list(self.sessions.values())
            ),
            return_exceptions=True,
        )
