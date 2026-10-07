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
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, get_args
from uuid import UUID

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketState

from copilot_core import jobs, prompts
from copilot_core.embed import Embedder
from copilot_core.ids import uuid7
from copilot_core.llm import ModelRouter
from copilot_core.packs import Digest, NodeRef, SessionPack, ref_map, render_pack
from copilot_gateway import audio
from copilot_gateway.answer import AnswerEngine, PreparedMatcher, Question
from copilot_gateway.auth import AuthError, Principal, TokenVerifier, bearer_token
from copilot_gateway.config import Settings
from copilot_gateway.detector import Detector
from copilot_gateway.store import UNAVAILABLE, GatewayStore, SessionWriter
from copilot_gateway.stt import ResilientSTT, STTFactory, TimedResult
from copilot_gateway.turn import Detection, TurnDetector, TurnModel
from copilot_protocol import models

log = logging.getLogger(__name__)

# Close codes (ARCHITECTURE.md, "Close codes, reconnect policy and limits").
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_UNAUTHORIZED = 4401
CLOSE_NO_MINUTES = 4403
CLOSE_NO_START = 4408
CLOSE_SESSION_ACTIVE = 4409
CLOSE_REPLACED = 4410  # a newer connection took this session; the client must not reconnect
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
    turn_model: TurnModel | None = None
    task_id: str = field(default_factory=lambda: str(uuid7()))


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


@dataclass(frozen=True)
class TailItem:
    channel: str
    start_ms: int
    end_ms: int
    text: str


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
        if svc.stt_providers:
            self.stt = ResilientSTT(
                svc.stt_providers,
                on_result=self._on_stt,
                on_status=self._on_stt_status,
                stall_s=self.settings.stt_stall_s,
                failures_before_switch=self.settings.stt_failures_before_switch,
                keepalive_s=self.settings.stt_keepalive_s,
            )
        self.detector = Detector(
            svc.router, principal.display_name, self.settings.detector_max_tokens
        )
        self.matcher = PreparedMatcher(
            svc.embedder,
            pack.prepared_answers if pack else [],
            self.settings.prepared_answer_min_similarity,
            pack.embedding_model if pack else None,
        )
        self.turns = TurnDetector(
            self,
            silence_timer_ms=self.settings.silence_timer_ms,
            speculative_start_ms=self.settings.speculative_start_ms,
            final_wait_max_ms=self.settings.final_wait_max_ms,
            min_confidence=self.settings.detector_min_confidence,
            turn_model=svc.turn_model,
        )
        self.answers = AnswerEngine(self)
        self.dedupe = audio.FrameDedupe(
            {ch: int(restored[f"hw_{ch}"]) for ch in ("them", "me") if f"hw_{ch}" in restored}
        )
        self.seg_id = int(restored.get("seg_them", "0"))
        self.tail: deque[TailItem] = deque(
            TailItem(**item) for item in json.loads(restored.get("tail", "[]"))
        )
        self.capture_now = max((t.end_ms for t in self.tail), default=0)
        self.me_since: int | None = None
        self._refs = ref_map(digest, pack)
        self._prompt_ctx = self._build_prompt_ctx()

        self.last_speech = time.monotonic()
        self.last_force = 0.0
        self.disconnected_at: float | None = None
        self.auth_warned = False
        self.flushed_upto = restored.get("flushed_upto", "")
        self.ending = False
        self.lost = False
        self.core_task: asyncio.Task[None] | None = None
        self.end_task: asyncio.Task[None] | None = None

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

    # AnswerHost.

    def transcript(self, seconds: float) -> str:
        since = self.capture_now - int(seconds * 1000)
        lines: list[str] = []
        for item in self.tail:
            if item.end_ms < since:
                continue
            stamp = f"[{item.start_ms // 60000:02d}:{item.start_ms // 1000 % 60:02d}]"
            if item.channel == "them":
                lines.append(f"{stamp} Them: {item.text}")
            else:
                secs = max(1, (item.end_ms - item.start_ms) // 1000)
                lines.append(f"{stamp} ({self.principal.display_name} spoke for {secs} s)")
        return "\n".join(lines)

    def prompt_context(self) -> prompts.PromptContext:
        return self._prompt_ctx

    def _build_prompt_ctx(self) -> prompts.PromptContext:
        return prompts.PromptContext(
            user_name=self.principal.display_name,
            mode=self.info.mode,
            digest_text=self.digest.text if self.digest else None,
            pack_text=render_pack(self.pack) if self.pack else None,
        )

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

    # TurnHost.

    def detect(self, utterance: str) -> "asyncio.Future[Detection]":
        transcript = self.transcript(self.settings.transcript_context_s)
        return asyncio.ensure_future(self.detector.detect(utterance, transcript))

    def start_match(self, utterance: str) -> "asyncio.Task[object] | None":
        if not self.matcher.enabled:
            return None
        return asyncio.create_task(self.matcher.match(utterance))  # pyright: ignore[reportReturnType]

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
            confidence=detection.confidence,
            addressed=detection.addressed_to_user,
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

    def confirm_question(self, qid: UUID, text: str, turn_end_ms: int) -> None:
        self.answers.confirm(qid, text, turn_end_ms)

    def cancel_question(self, qid: UUID) -> None:
        self.answers.cancel_speculative(qid)

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
                self._add_tail(TailItem("them", r.start_ms, r.end_ms, r.text))
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

    def _add_tail(self, item: TailItem) -> None:
        self.tail.append(item)
        self.capture_now = max(self.capture_now, item.end_ms)
        keep_from = self.capture_now - 5 * 60_000
        while self.tail and self.tail[0].end_ms < keep_from:
            self.tail.popleft()
        recent = [
            t.__dict__
            for t in self.tail
            if t.end_ms >= self.capture_now - self.settings.transcript_context_s * 1000
        ]
        self.writer.set_state(tail=recent)

    # Lifecycle.

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
            "task_id": self.svc.task_id,
        }

    def start_core(self) -> None:
        self.core_task = asyncio.create_task(self._run_core())

    async def _run_core(self) -> None:
        try:
            async with asyncio.TaskGroup() as tg:
                if self.stt is not None:
                    tg.create_task(self.stt.run())
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

    async def _rebuild_stale_pack(self) -> None:
        """The pack's vectors came from another embedding model (or it has none): matching is
        off for this session, and the pack is rebuilt for the next one. The key names the
        gateway's embedding model, so this runs once per pack and model."""
        pack, embedder = self.pack, self.svc.embedder
        if pack is None or embedder is None:
            return
        info = self.info
        if info.interview_id is not None:
            context = f"interview:{info.interview_id}"
        elif info.job_id is not None:
            context = f"job:{info.job_id}"
        else:
            return
        log.info(
            "pack %s has %s vectors, not %s: prepared-answer matching off; rebuilding it",
            pack.pack_id, pack.embedding_model or "no", embedder.model_id,
        )  # fmt: skip
        message = jobs.pack_build(
            self.principal.user_id, pack.mode, context, datetime.now(UTC),
            pack.digest_version or None, embedder.model_id,
        )  # fmt: skip
        try:
            await self.svc.jobs.send(message)
        except Exception as e:
            log.warning("enqueue %s failed: %s", message["idempotency_key"], e)

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
            if frame.speech_start:
                self.me_since = frame.capture_ms
            if frame.speech_end and self.me_since is not None:
                self._add_tail(TailItem("me", self.me_since, frame.capture_ms, ""))
                self.me_since = None
            return
        if frame.speech_start:
            if self.stt is not None:
                self.stt.speech(True)
            self.turns.on_speech_start()
        if frame.audio and self.stt is not None:
            await self.stt.send(frame.audio, frame.capture_ms)
        if frame.speech_end:
            if self.stt is not None:
                self.stt.speech(False)
            self.turns.on_speech_end()
            if self.stt is not None:
                await self.stt.finalize()

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

    def transcript_text(self, seconds: float) -> str:
        since = self.capture_now - int(seconds * 1000)
        return " ".join(t.text for t in self.tail if t.channel == "them" and t.end_ms >= since)


Handler = Callable[[WebSocket], Any]


class SessionManager:
    """Sessions on this gateway task, and the connection handshake."""

    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.sessions: dict[UUID, Session] = {}

    def forget(self, session: Session) -> None:
        if self.sessions.get(session.sid) is session:
            del self.sessions[session.sid]

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

    async def handle(self, ws: WebSocket) -> None:
        s = self.svc.settings
        token = bearer_token(ws.headers.get("authorization"))
        await ws.accept()
        try:
            principal = await self.svc.verifier.verify(token or "")
        except AuthError as e:
            await self.reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", str(e))
            return
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
        if valkey_ok:
            try:
                digest = await store.get_digest(uid)
                pack = await store.get_pack(uid, msg.job_id, msg.mode, msg.interview_id)
            except (*UNAVAILABLE, ValidationError, ValueError) as e:
                log.warning("could not load digest or pack for %s: %s", uid, e)
        info = SessionInfo(
            mode=msg.mode,
            job_id=msg.job_id,
            interview_id=msg.interview_id,
            title=msg.meeting_title,
            platform=msg.platform,
            client_version=msg.client_version,
            store_transcript=msg.consent.store_transcript,
            started_at=datetime.now(UTC),
        )
        session = Session(self.svc, self, sid, principal, info, digest, pack)
        self.sessions[sid] = session
        with contextlib.suppress(*UNAVAILABLE):
            await store.take_lease(sid, self.svc.task_id)
        session.writer.set_state(**session.state_fields())
        session.log_entry({"kind": "session.started", **session.state_fields()})
        session.start_core()
        session.emit(
            models.SessionReady,
            session_id=sid,
            server_time=datetime.now(UTC),
            limits={"max_minutes": self.svc.settings.max_session_minutes},
        )
        if digest is None:
            session.set_status("memory", "degraded", "No career digest loaded")
        await session.attach(ws, last_seq=0)

    async def resume(self, ws: WebSocket, principal: Principal, msg: models.SessionResume) -> None:
        store = self.svc.store
        uid = principal.user_id
        sid = msg.session_id
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
