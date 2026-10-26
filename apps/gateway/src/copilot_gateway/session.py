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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, get_args
from uuid import UUID

import asyncpg
from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketState

from copilot_core import jobs, logs, profiles, prompts, resumes, versions, workplace
from copilot_core.db import user_tx
from copilot_core.embed import Embedder
from copilot_core.ids import uuid7
from copilot_core.llm import LLMDone, ModelRouter
from copilot_core.packs import (
    Digest,
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
from copilot_gateway.answer import AnswerEngine, PreparedMatcher, Question, is_answer_command
from copilot_gateway.auth import AuthError, Principal, TokenVerifier, bearer_token
from copilot_gateway.config import Settings
from copilot_gateway.conversation import Budget, Conversation, Line, echo_share
from copilot_gateway.detector import Detector
from copilot_gateway.retrieval import CompanyMemory
from copilot_gateway.store import UNAVAILABLE, GatewayStore, SessionWriter
from copilot_gateway.stt import ResilientSTT, STTFactory, TimedResult
from copilot_gateway.turn import Detection, TurnDetector, TurnModel
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
    # An interview for an application: the resume version its digest and pack come from.
    resume_version_id: UUID | None = None
    resume_is_base: bool = False


@dataclass
class ApplicationMemory:
    """What an interview for an application loads: its paired resume version's digest and the
    application's pack, never another version's."""

    digest: Digest | None
    pack: SessionPack | None
    resume_version_id: UUID | None
    is_base: bool


# Echo (see Session._settle_me): this many lines taken for echo within ECHO_WINDOW_S turn the
# status badge to a headphones hint, until ECHO_WINDOW_S passes without one.
ECHO_WARN_COUNT = 3
ECHO_WINDOW_S = 120.0
# The other side's speech counts as "around the same time" as a line of the user's this close.
ECHO_NEAR_MS = 500
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
        self.digest = digest
        self.pack = pack
        restored = restored or {}

        self.seq = seq
        self.ring: deque[tuple[int, str]] = deque(ring, maxlen=self.settings.events_maxlen)
        self.ws: WebSocket | None = None
        self.send_q: asyncio.Queue[str | None] | None = None
        self.status: dict[str, str] = {"stt": "ok", "llm": "ok", "memory": "ok"}

        self.writer = SessionWriter(
            svc.store,
            sid,
            svc.task_id,
            on_lost=self._on_lease_lost,
            buffer_max=self.settings.store_buffer_max,
        )
        self.stt: ResilientSTT | None = None
        # The user's own microphone, transcribed live as a second stream (me_stt).
        self.stt_me: ResilientSTT | None = None
        if svc.stt_providers:
            self.stt = self._stt_stream(self._on_stt)
            if self.settings.me_stt:
                self.stt_me = self._stt_stream(self._on_me_stt, svc.stt_me_providers)
        self.detector = Detector(
            svc.router,
            principal.display_name,
            self.settings.detector_max_tokens,
            on_decision=self._log_detection,
            cost=self._cost,
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
            hold_min_words=self.settings.speculative_min_words,
        )
        self.answers = AnswerEngine(self)
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
        self._settling: set[asyncio.Task[None]] = set()
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

        self.last_speech = time.monotonic()
        self.last_force = 0.0
        self.disconnected_at: float | None = None
        self.auth_warned = False
        self.flushed_upto = restored.get("flushed_upto", "")
        self.ending = False
        self.lost = False
        self.core_task: asyncio.Task[None] | None = None
        self.end_task: asyncio.Task[None] | None = None
        self._warm: asyncio.Task[None] | None = None

    # Messages to the client.

    def emit(self, cls: type[Any], **fields: Any) -> int:
        self.seq += 1
        message = cls.model_validate({"type": _type_of(cls), "seq": self.seq, **fields})
        data = message.model_dump_json(exclude_none=True, by_alias=True)
        self.ring.append((self.seq, data))
        self.writer.event(self.seq, data)
        self.writer.set_state(last_seq=str(self.seq), updated_at=str(time.time()))
        if self.send_q is not None:
            self.send_q.put_nowait(data)
        return self.seq

    def error(self, code: str, message: str, fatal: bool = False, **extra: Any) -> None:
        self.emit(models.ServerError, code=code, message=message, fatal=fatal, **extra)

    def set_status(self, component: str, state: str, message: str) -> None:
        if self.status.get(component) == state:
            return
        self.status[component] = state
        self.emit(models.Status, component=component, state=state, message=message)

    def log_entry(self, entry: Mapping[str, Any]) -> None:
        self.writer.log_entry({**entry, "at": datetime.now(UTC).isoformat()})

    def _stt_stream(
        self,
        on_result: Callable[[TimedResult], Awaitable[None]],
        providers: Sequence[tuple[str, STTFactory]] | None = None,
    ) -> ResilientSTT:
        s = self.settings
        return ResilientSTT(
            providers or self.svc.stt_providers,
            on_result=on_result,
            on_status=self._on_stt_status,
            stall_s=s.stt_stall_s,
            failures_before_switch=s.stt_failures_before_switch,
            keepalive_s=s.stt_keepalive_s,
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
            s.conversation_summary_tokens,
        )  # fmt: skip
        started = time.monotonic()
        text, done = await self.router.complete("summary", req)
        ms = int((time.monotonic() - started) * 1000)
        log.info(
            "conversation summary on %s: %s in / %s out tokens in %s ms",
            done.model, done.usage.input_tokens, done.usage.output_tokens, ms,
        )  # fmt: skip
        self.log_entry(
            {
                "kind": "summary",
                "model": done.model,
                "input_tokens": done.usage.input_tokens,
                "cached_tokens": done.usage.cached_tokens,
                "output_tokens": done.usage.output_tokens,
                "cost_usd": self._cost(done),
                "latency_ms": ms,
            }
        )
        return text

    # AnswerHost.

    def call_context(self) -> prompts.CallContext:
        return self.conv.context()

    def prompt_context(self) -> prompts.PromptContext:
        return self._prompt_ctx

    def _build_prompt_ctx(self) -> prompts.PromptContext:
        employer = self.pack.employer if self.pack is not None else None
        at_work = employer is not None and employer.id == self.info.employer_id
        return prompts.PromptContext(
            user_name=self.principal.display_name,
            mode=self.info.mode,
            digest_text=self.digest.text if self.digest else None,
            pack_text=render_pack(self.pack) if self.pack else None,
            company=employer.name if at_work and employer else None,
            role=(employer.title or None) if at_work and employer else None,
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

    def _memory_on(self) -> bool:
        return self.company_memory is not None and self.info.mode in ("standup", "meeting")

    async def memory_lines(self, text: str, pass_: str) -> tuple[list[str], dict[str, int]]:
        """The `<memory>` block for a pass: what search found in the chosen company's memory
        within the pass's wait, minus what the pack already shows. Each line cites a short id
        that the session's refs now map, so the answer's citations become memory_refs."""
        mem = self.company_memory
        if mem is None or not self._memory_on():
            return [], {}
        deep = pass_ == "deep"
        s = self.settings
        wait = s.memory_deep_wait_ms if deep else s.memory_fast_wait_ms
        hits, stats = await mem.hits(text, self._vector(text), deep, wait)
        if not deep:
            hits = hits[: s.memory_fast_hits]
        in_pack = {i.node_id for i in pack_items(self.pack)} if self.pack else set[UUID]()
        lines = workplace.memory_lines(hits, self._refs, in_pack)
        stats["memory_hits"] = len(lines)
        return lines, stats

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
        return f"{digest}|{pack}|{self.info.mode}"

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
        transcript = self.conv.recent(self.settings.detector_context_tokens)
        return asyncio.ensure_future(self.detector.detect(utterance, transcript, self.info.mode))

    def start_match(self, utterance: str) -> "asyncio.Task[object] | None":
        if self._memory_on() and self.company_memory is not None:
            # Memory search starts with the detector, on the same text.
            self.company_memory.lookup(utterance, self._vector(utterance))
        if not self.matcher.enabled:
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
        )
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

    def confirm_question(
        self, qid: UUID, text: str, turn_end_ms: int, final_ms: int | None
    ) -> None:
        self.answers.confirm(qid, text, turn_end_ms, final_ms)
        # Paired with what the user says after the question's own words, which may already
        # have started by the time the turn is confirmed.
        self.conv.question(str(qid), text, self.conv.them_end)

    def cancel_question(self, qid: UUID) -> None:
        self.answers.cancel_speculative(qid)

    def start_held(self, text: str, t0: float, match: "asyncio.Task[object] | None") -> UUID:
        q = Question(
            qid=uuid7(),
            text=text,
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
        self.answers.add(q)
        return q.qid

    def release_question(
        self, qid: UUID, text: str, detection: Detection, *, speculative: bool
    ) -> None:
        q = self.answers.questions.get(qid)
        if q is None:
            return
        q.text = text
        q.qtype = detection.qtype
        q.confidence = DETECTED_CONFIDENCE
        q.addressed = True
        q.detector_ms = detection.latency_ms
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

    # STT callbacks.

    async def _on_stt(self, r: TimedResult) -> None:
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
        self.set_status("stt", state, message)

    # The user's own words (live STT of the mic).

    def _them_near(self, start_ms: int, end_ms: int) -> bool:
        """The other side was speaking around this time (their speech flags)."""
        lo, hi = start_ms - ECHO_NEAR_MS, end_ms + ECHO_NEAR_MS
        if self.them_open is not None and self.them_open <= hi:
            return True
        return any(a <= hi and b >= lo for a, b in self.them_spans)

    async def _on_me_stt(self, r: TimedResult) -> None:
        if not r.text:
            return
        if not r.is_final:
            # Not while the other side speaks: that text may be their echo.
            if not self._them_near(r.start_ms, r.end_ms):
                self.emit(models.TranscriptPartial, channel="me", seg_id=self.seg_me, text=r.text)
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
        heard = self.conv.them_text_between(
            line.start_ms - ECHO_TEXT_MS, line.end_ms + ECHO_TEXT_MS
        )
        share = echo_share(line.text, [*heard, self.turns.interim])
        if share >= s.echo_min_overlap:
            self._echo(seg, line, share)
        else:
            self._accept_me(seg, line)

    def _accept_me(self, seg: int, line: Line) -> None:
        self.emit(
            models.TranscriptFinal,
            channel="me",
            seg_id=seg,
            text=line.text,
            start_ms=line.start_ms,
            end_ms=line.end_ms,
        )
        self.conv.add(line)
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

    # Lifecycle.

    def deep_auto_types(self) -> list[str]:
        s = self.settings
        return list(s.deep_pass_auto_types) if s.deep_pass_auto else []

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
            "employer_id": str(self.info.employer_id) if self.info.employer_id else "",
            "agenda": self.info.agenda or "",
            "resume_version_id": str(self.info.resume_version_id)
            if self.info.resume_version_id
            else "",
            "resume_base": "1" if self.info.resume_is_base else "0",
            "task_id": self.svc.task_id,
        }

    def start_core(self) -> None:
        self.core_task = asyncio.create_task(self._run_core())

    async def _run_core(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                if self.stt is not None:
                    tg.create_task(self.stt.run())
                if self.stt_me is not None:
                    tg.create_task(self.stt_me.run())
                tg.create_task(self.writer.run())
                tg.create_task(self._lease_loop())
                tg.create_task(self._flush_loop())
                tg.create_task(self._watchdog())
                if self.matcher.stale:
                    tg.create_task(self._rebuild_stale_pack())
        except* asyncio.CancelledError:
            raise
        except* Exception as eg:
            log.error("session %s failed", self.sid, exc_info=eg)
            await self.abandon(CLOSE_INTERNAL, "internal error")

    async def _lease_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.lease_renew_s)
            try:
                held = await self.svc.store.renew_lease(self.sid, self.svc.task_id)
                if not held:
                    self._on_lease_lost()
                    return
                await self.svc.store.refresh_active(self.principal.user_id, self.sid)
            except UNAVAILABLE:
                continue  # Valkey down: keep the session in memory

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.flush_interval_s)
            await self.flush_job()

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
            await self.router.warm()
            await self.prewarm_prompts()

        task = asyncio.create_task(warm())
        self._warm = task  # keep a reference until it finishes

    async def prewarm_prompts(self) -> None:
        """Write the prompt cache for the session's prefix (rules, digest, pack) before the
        first question, so it is not answered cold: in the October 2026 live test the first
        question's fast pass was the only uncached one, 1.6 s to its first token against
        1.0 s, and a cold gpt-6-luna first token measured up to 5 s. Once per session and per
        mode change; the default cache lives 30 minutes on OpenAI and 5 on Claude, and the
        questions that follow keep it warm."""
        ctx = self._prompt_ctx
        for job in self.settings.prewarm_jobs:
            req = prompts.fast_pass(ctx, "", "(warm-up)", None, self.settings.fast_pass_max_tokens)
            if job == "deep":
                req = prompts.deep_pass(
                    ctx, "", "(warm-up)", "other", self.settings.deep_pass_max_tokens
                )
            started = time.monotonic()
            done = await self.router.prewarm(job, req)
            if done is None:
                continue
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
                    "model": done.model,
                    "input_tokens": done.usage.input_tokens,
                    "cached_tokens": done.usage.cached_tokens,
                    "cost_usd": self._cost(done),
                    "latency_ms": ms,
                }
            )

    async def _mode_changed(self, mode: str) -> None:
        """A switch between standup and meeting at a company loads that company's pack for the
        new mode; then the new prefix is prewarmed."""
        if self.info.employer_id is not None and mode in ("standup", "meeting"):
            pack = await self.manager.employer_pack(
                self.principal.user_id, self.info.employer_id, mode, self.info.title,
                self.info.agenda,
            )  # fmt: skip
            if pack is not None:
                self.set_pack(pack)
                self._prompt_ctx = self._build_prompt_ctx()
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
        if self.end_task is None:
            self.end_task = asyncio.create_task(self.end(reason, code))

    async def end(self, reason: str, code: int = CLOSE_NORMAL) -> None:
        """session.end, idle, or no resume: flush the log, enqueue session.ended, close."""
        if self.ending:
            return
        self.ending = True
        self.turns.cancel()
        self.answers.cancel_all()
        self.log_entry({"kind": "session.ended", "reason": reason})
        log.info(
            "session ended: reason=%s minutes=%.1f questions=%d stats=%s",
            reason, (datetime.now(UTC) - self.info.started_at).total_seconds() / 60,
            len(self.answers.questions), self.answers.stats,
        )  # fmt: skip
        self.writer.set_state(status="ended", ended_at=datetime.now(UTC).isoformat())
        if not await self.writer.drain(within_s=10):
            log.error("session %s ended with unwritten log entries (valkey down)", self.sid)
        await self._send_job(
            "session.ended", f"session.ended:{self.sid}", {"session_id": str(self.sid)}
        )
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
        self.turns.cancel()
        self.answers.cancel_all()
        await self.writer.drain(within_s=2)
        with contextlib.suppress(*UNAVAILABLE):
            await self.svc.store.release_lease(self.sid, self.svc.task_id)
        if self.ws is not None:
            await close_ws(self.ws, code, reason)
        await self._stop()

    def _on_lease_lost(self) -> None:
        """Another task resumed this session: stop here, write nothing more."""
        if self.lost or self.ending:
            return
        self.lost = True
        self.ending = True
        log.info("session %s moved to another task", self.sid)
        self.turns.cancel()
        self.answers.cancel_all()
        self.end_task = asyncio.create_task(self._stop_lost(self.ws))

    async def _stop_lost(self, ws: WebSocket | None) -> None:
        if ws is not None:
            await close_ws(ws, CLOSE_REPLACED, "replaced by a newer connection")
        await self._stop()

    async def _stop(self) -> None:
        self.manager.forget(self)
        self.conv.close()
        for task in list(self._settling):
            task.cancel()
        if self.send_q is not None:
            self.send_q.put_nowait(None)
        task = self.core_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

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
        if self.ws is not None:
            old = self.ws
            if self.send_q is not None:
                self.send_q.put_nowait(None)
            await close_ws(old, CLOSE_REPLACED, "replaced by a newer connection")
        q: asyncio.Queue[str | None] = asyncio.Queue()
        self.ws, self.send_q, self.disconnected_at = ws, q, None
        self.auth_warned = False
        if replay is None:
            replay = [(s, m) for s, m in self.ring if s > last_seq]
        for _, message in replay:
            q.put_nowait(message)
        sender = asyncio.create_task(self._sender(ws, q))
        reader = asyncio.create_task(self._reader(ws))
        try:
            await asyncio.wait({sender, reader}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sender, reader):
                task.cancel()
            await asyncio.gather(sender, reader, return_exceptions=True)
            if self.ws is ws:
                self.ws, self.send_q = None, None
                self.disconnected_at = time.monotonic()
                if not self.ending:
                    self.writer.set_state(status="reconnecting")
                    self.log_entry({"kind": "disconnected"})

    async def _sender(self, ws: WebSocket, q: "asyncio.Queue[str | None]") -> None:
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
        if frame.speech_start:
            self.them_open = frame.capture_ms
            if self.stt is not None:
                self.stt.speech(True)
            self.turns.on_speech_start()
        if frame.audio and self.stt is not None:
            await self.stt.send(frame.audio, frame.capture_ms)
        if frame.speech_end:
            if self.them_open is not None:
                self.them_spans.append((self.them_open, frame.capture_ms))
                self.them_open = None
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
        if frame.speech_start:
            self.me_since = frame.capture_ms
            self.me_audio_seen = False
            if stt is not None:
                stt.speech(True)
        if frame.audio and stt is not None:
            self.me_audio_seen = True
            await stt.send(frame.audio, frame.capture_ms)
        if frame.speech_end:
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
        match message:
            case models.SessionStart() | models.SessionResume():
                self.error("unexpected_message", f"{message.type} is only valid as a first message")
            case models.AuthRefresh():
                await self._auth_refresh(message.access_token)
            case models.QuestionForce():
                self._force(message.text)
            case models.Screenshot():
                self.error("not_implemented", "screenshots are not supported yet")
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
                self._warm = asyncio.create_task(self._mode_changed(message.mode))
            case models.SessionEnd():
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
        self.auth_warned = False

    def _force(self, text: str | None) -> None:
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
        if text and is_answer_command(text):
            text = None  # "answer" typed into the Ask box: the same as Enter on an empty box
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
            qtype="other",
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
    ) -> SessionPack | None:
        """The company's pack for this call, never waiting on a model.

        The worker's pack (built when the company was picked) is used when it was rendered
        from the company's memory as it is now, and for today. Otherwise the gateway renders
        the same pack from Postgres itself (tens of ms; prepared standup vectors carry over
        from the stored pack) and asks a worker to build it for next time. Without a database
        the stored pack is used as it is. None: the company is not this user's, or there is
        nothing to load.

        A meeting gets its title and agenda, and the company's memory that matches the agenda,
        on a copy with its own pack id, kept in Valkey for a resume."""
        svc, s = self.svc, self.svc.settings
        stored: SessionPack | None = None
        try:
            stored = await svc.store.get_employer_pack(uid, employer_id, mode)
        except (*UNAVAILABLE, ValidationError, ValueError) as e:
            log.warning("could not load the company pack: %s", e)
        if stored is not None and stored.employer is not None and stored.employer.id != employer_id:
            stored = None
        pack, fresh, related = stored, False, list[PackItem]()
        if svc.pool is not None:
            try:
                async with asyncio.timeout(s.pack_render_timeout_ms / 1000):
                    async with user_tx(svc.pool, uid) as conn:
                        if await workplace.employer(conn, employer_id) is None:
                            return None
                        settings = await conn.fetchval(
                            "select settings from app.users where id = $1", uid
                        )
                        today = workplace.local_today(
                            json.loads(settings) if isinstance(settings, str) else settings
                        )
                        version = await workplace.memory_version(conn, uid, employer_id)
                        current = (
                            stored is not None
                            and stored.memory_version == version
                            and (
                                mode != "standup"
                                or (stored.standup is not None and stored.standup.day == str(today))
                            )
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
        s = self.svc.settings
        token = bearer_token(ws.headers.get("authorization"))
        await ws.accept()
        try:
            principal = await self.svc.verifier.verify(token or "")
        except AuthError as e:
            await self.reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", str(e))
            return
        profile = ws.query_params.get(profiles.QUERY)
        if profile:
            # The session acts for one of the account's profiles (ARCHITECTURE.md, "Profiles").
            resolved = await self.as_profile(principal, profile)
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
            await self.start(ws, principal, first)
        elif isinstance(first, models.SessionResume):
            await self.resume(ws, principal, first)
        else:
            await self.reject(
                ws,
                CLOSE_NO_START,
                "no_start",
                "the first message must be session.start or session.resume",
            )

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

    async def start(self, ws: WebSocket, principal: Principal, msg: models.SessionStart) -> None:
        store = self.svc.store
        uid = principal.user_id
        if self.live_for_user(uid) is not None:
            await self.reject(
                ws, CLOSE_SESSION_ACTIVE, "session_active", "another session is active"
            )
            return
        sid = uuid7()
        # Every line logged for this session from here on (and in the tasks it starts) names it.
        logs.set_ids(session_id=sid)
        valkey_ok = True
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
            log.warning("valkey unavailable at session start (%s); continuing in memory", e)
            valkey_ok = False
        digest: Digest | None = None
        pack: SessionPack | None = None
        at_work = msg.employer_id is not None and msg.mode in ("standup", "meeting")
        employer_id = msg.employer_id if at_work else None
        agenda = (msg.agenda or "").strip() or None
        resume_version_id: UUID | None = None
        resume_is_base = False
        for_application = (
            employer_id is None
            and msg.mode == "interview"
            and (msg.job_id is not None or msg.interview_id is not None)
        )
        if valkey_ok:
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
            pack = await self.employer_pack(uid, employer_id, msg.mode, msg.meeting_title, agenda)
            if pack is None:
                employer_id = None  # not this user's company, or nothing to load at all
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
            resume_version_id=resume_version_id,
            resume_is_base=resume_is_base,
        )
        session = Session(self.svc, self, sid, principal, info, digest, pack)
        self.sessions[sid] = session
        with contextlib.suppress(*UNAVAILABLE):
            await store.take_lease(sid, self.svc.task_id)
        session.writer.set_state(**session.state_fields())
        session.log_entry({"kind": "session.started", **session.state_fields()})
        log.info(
            "session started: mode=%s platform=%s client=%s consent=%s digest=%s pack=%s",
            info.mode, info.platform, info.client_version, info.store_transcript,
            digest is not None, pack is not None,
        )  # fmt: skip
        session.start_core()
        session.warm_models()
        session.emit(
            models.SessionReady,
            session_id=sid,
            server_time=datetime.now(UTC),
            limits={"max_minutes": self.svc.settings.max_session_minutes},
            me_audio=session.me_audio,
            deep_auto_types=session.deep_auto_types(),
        )
        if digest is None:
            session.set_status("memory", "degraded", "No career digest loaded")
        await session.attach(ws, last_seq=0)

    async def resume(self, ws: WebSocket, principal: Principal, msg: models.SessionResume) -> None:
        store = self.svc.store
        uid = principal.user_id
        sid = msg.session_id
        logs.set_ids(session_id=sid)
        log.info("session resume from seq %d", msg.last_seq)
        local = self.sessions.get(sid)
        if local is not None and local.principal.user_id == uid and not local.ending:
            local.principal = principal
            local.log_entry({"kind": "resumed", "task_id": self.svc.task_id})
            await local.attach(ws, msg.last_seq)
            return
        try:
            state = await store.load_state(sid)
            if not state or state.get("user_id") != str(uid) or state.get("status") == "ended":
                await self.reject(
                    ws, CLOSE_NO_START, "session_not_found", "no resumable session with that id"
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
                await store.replace_active(uid, other, sid)
            await store.set_active(uid, sid)
            await store.take_lease(sid, self.svc.task_id)
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
        except UNAVAILABLE:
            await self.reject(
                ws, CLOSE_INTERNAL, "unavailable", "session state is unavailable; retry"
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
            resume_version_id=UUID(rv) if rv else None,
            resume_is_base=state.get("resume_base") == "1",
        )
        session = Session(
            self.svc, self, sid, principal, info, digest, pack, seq=seq, ring=replay, restored=state
        )
        self.sessions[sid] = session
        session.writer.set_state(status="active", task_id=self.svc.task_id)
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
