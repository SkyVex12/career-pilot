"""Session stress replay through a localhost gateway, synthetic STT, and real turn handling.

Offline by default. --live enables configured providers with full attempt and cost ledgers.
Timings start at text-frame injection; audio recognition, diarization and physical UI paint
are explicitly outside this measurement. Artifacts must be private and outside the repo.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import math
import time
from collections import Counter
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
from fakeredis import FakeAsyncValkey, FakeServer

from copilot_core import keys
from copilot_core.jobs import MemoryJobQueue
from copilot_core.llm import Decided, LLMChunk, LLMDone, LLMRequest, LLMUsage, error_chain
from copilot_core.packs import (
    PREPARED_DIGEST_POLICY,
    Digest,
    PreparedAnswer,
    SessionPack,
    encode_embedding,
)
from copilot_core.pricing import ModelPrice
from copilot_core.providers import close_providers, make_providers
from copilot_evals.live_answer_artifacts import DispatchJournal, immutable, source_archive
from copilot_evals.live_answer_async_checkpoints import (
    ASYNC_POLICY,
    AnchoredLoopLagMonitor,
    BufferedRecordStore,
    DurableWriter,
    require_async_directory,
)
from copilot_evals.live_answer_checkpoints import (
    LoopLagMonitor,
    RecordDispatchJournal,
    RecordStore,
)
from copilot_evals.live_answer_eval import distribution, fingerprint
from copilot_evals.live_answer_replay import (
    STRATEGIES,
    TRIAL,
    AuditBudget,
    AuditProvider,
    BufferedProgressSnapshots,
    ProviderAdmission,
    budget_limit,
    connectivity_check,
    experiment_identity,
    record_finalization_failure,
    root_directory,
)
from copilot_evals.live_answer_schedule import FailureCircuit, case_blocks
from copilot_evals.sensei_runtime import private_directory, save
from copilot_gateway.auth import Principal, StaticVerifier
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.session import Services
from copilot_gateway.store import GatewayStore
from copilot_gateway.stt import STTResult
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    TextSTT,
    TextSTTControl,
    default_reply,
    job_of,
)

PURPOSE = contextvars.ContextVar("session_eval_purpose", default="answer")
CONTROL: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("session_eval_control")
PROFILE = (
    "Alex is based in Chicago and is a backend engineer. Alex uses Python and PostgreSQL. "
    "Alex built a Redis rate limiter and added bounded retries and idempotency keys to "
    "a fictional notification service. No production incident outcomes have been supplied."
)


class PreparedReplayEmbedder:
    """Exact synthetic vectors exercise PreparedMatcher without a billed embedding call."""

    model_id = "session-replay:exact@2"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [1.0, 0.0] if text.strip() == "Where are you based?" else [0.0, 1.0] for text in texts
        ]


class RevisionSTT(TextSTT):
    """Replaceable interim text and delayed final corrections over the existing test frames."""

    def __init__(self, control: TextSTTControl, name: str = "text") -> None:
        super().__init__(control, name)
        self.final_text = ""

    async def send_audio(self, pcm: bytes) -> None:
        value = json.loads(pcm.rstrip(b"\0 ").decode())
        self.ms += len(pcm) // 32
        self.final_text = value.get("final_text", value["text"])
        self.words = [self.final_text]
        self.queue.put_nowait(STTResult(value["text"], False, self.seg_start, self.ms))


class SessionBudget(AuditBudget):
    def reserve(self, model: str, payload: Any, output: int, tier: str = "standard") -> int:
        # Anthropic request tier names differ from response billing tier names.
        tier = {"standard_only": "standard", "auto": "priority"}.get(tier, tier)
        return super().reserve(model, payload, output, tier)

    def checkpoint(self, ticket: int) -> None:
        # Persist purpose in the initial reservation, without another full-array save.
        self.entries[ticket].setdefault("purpose", PURPOSE.get())
        super().checkpoint(ticket)


class SessionAuditProvider(AuditProvider):
    def checkpoint(self, record: dict[str, Any]) -> None:
        record.setdefault("purpose", PURPOSE.get())
        super().checkpoint(record)

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        purpose = (
            "detector"
            if req.json_schema is not None
            else "summary"
            if job_of(req) == "summary"
            else "answer"
        )
        stream = super().stream(model, req, params)
        try:
            while True:
                # The router opens an async generator in one task and consumes it in another.
                # A ContextVar token must be reset in the task/context that created it.
                token = PURPOSE.set(purpose)
                try:
                    chunk = await anext(stream)
                except StopAsyncIteration:
                    return
                finally:
                    PURPOSE.reset(token)
                yield chunk
        finally:
            await stream.aclose()

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        token = PURPOSE.set("detector_decisions")
        if isinstance(self.store, BufferedRecordStore):
            try:
                return await super().decide(model, input, questions)
            finally:
                PURPOSE.reset(token)
        started = time.monotonic()
        record: dict[str, Any] = {
            "request_id": str(uuid4()),
            "trial_id": TRIAL.get(),
            "purpose": "detector_decisions",
            "model": model,
            "request_sha256": fingerprint([input, questions]),
            "status": "started",
            "started_epoch_s": time.time(),
        }
        self.requests.append(record)
        try:
            self.checkpoint(record)
            result = await super().decide(model, input, questions)
            record["status"] = "completed"
            return result
        except BaseException as error:
            record.update(status=type(error).__name__, error_types=error_chain(error))
            raise
        finally:
            record["elapsed_s"] = time.monotonic() - started
            try:
                self.checkpoint(record)
            finally:
                PURPOSE.reset(token)


class FaultProvider:
    """Synthetic failure points are explicit; successful requests still use the real provider."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.name = inner, inner.name

    async def warm(self, model: str) -> None:
        method = getattr(self.inner, "warm", None)
        if method is not None:
            await method(model)

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        return await self.inner.decide(model, input, questions)

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        control = CONTROL.get({})
        is_answer = req.json_schema is None and job_of(req) != "summary"
        if is_answer and control.get("outage"):
            control["injected_failures"] = control.get("injected_failures", 0) + 1
            control["connection_outages"] = control.get("connection_outages", 0) + 1
            raise httpx2.ConnectError("synthetic session replay connection outage")
        stream = self.inner.stream(model, req, params)
        try:
            async for chunk in stream:
                yield chunk
                if is_answer and isinstance(chunk, str) and control.get("stream_timeout"):
                    await asyncio.sleep(control.get("timeout_delay_s", 0.2))
                    control["injected_failures"] = control.get("injected_failures", 0) + 1
                    control["stream_timeouts"] = control.get("stream_timeouts", 0) + 1
                    raise TimeoutError("synthetic session replay stream timeout")
        finally:
            await stream.aclose()


class DispatchGate(ProviderAdmission):
    """Shared fatal-status circuit; outer session admission also covers Decisions calls."""


class LimitedProvider:
    def __init__(self, inner: Any, gate: DispatchGate) -> None:
        self.inner, self.name, self.gate = inner, inner.name, gate

    async def warm(self, model: str) -> None:
        await self.gate.enter()
        try:
            await self.inner.warm(model)
        except BaseException as error:
            self.gate.observe_error(error)
            raise
        finally:
            self.gate.leave()

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        await self.gate.enter()
        try:
            return await self.inner.decide(model, input, questions)
        except BaseException as error:
            self.gate.observe_error(error)
            raise
        finally:
            self.gate.leave()

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        # Queueing before the audit wrapper never reserves a request that was not dispatched.
        await self.gate.enter()
        stream = self.inner.stream(model, req, params)
        try:
            async for chunk in stream:
                yield chunk
        except BaseException as error:
            self.gate.observe_error(error)
            raise
        finally:
            try:
                await stream.aclose()
            finally:
                self.gate.leave()


def offline_reply(job: str, req: LLMRequest) -> Reply:
    if req.json_schema is not None:
        return default_reply("detector", req)
    if job == "summary":
        return default_reply(job, req)
    # A long enough stream to deterministically overlap the next confirmed question.
    return Reply(
        ["I would make each operation idempotent. "]
        + [f"Step {i} adds a bounded retry and checks the original evidence. " for i in range(8)],
        first_delay_s=0.03,
        chunk_delay_s=0.04,
    )


class OfflineSessionProvider(ScriptedProvider):
    """Fictional, deterministic Decisions results through the genuine detector API path.

    Delays and answer text are test controls, not measurements of provider quality or speed.
    The delayed decision lets held generation start naturally before depth is available.
    """

    def __init__(self) -> None:
        super().__init__("openai", offline_reply)
        self.decisions: list[dict[str, Any]] = []
        self.routed_requests: list[tuple[str, LLMRequest]] = []

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        text = str(input[1]["content"][0]["text"])
        utterance = text.split("<utterance>")[-1].split("</utterance>")[0].strip()
        lowered = utterance.lower()
        turn = "answer" if utterance.endswith("?") else "none"
        qtype = (
            "system_design"
            if "design" in lowered
            else "standup_status"
            if "alex, what is the status" in lowered
            else "behavioral"
            if "incident" in lowered
            else "technical"
            if lowered.startswith(("how", "why", "what is the current"))
            else "other"
        )
        depth = (
            "deep"
            if any(term in lowered for term in ("design", "exactly-once", "incident"))
            else "light"
        )
        self.decisions.append({"utterance": utterance, "turn": turn, "type": qtype, "depth": depth})
        self.calls.append(("detector_decisions", model))
        await asyncio.sleep(0.06)
        return Decided(
            [
                {"type": "choice", "name": name, "choice": value, "confidence": 0.9}
                for name, value in (("turn", turn), ("type", qtype), ("depth", depth))
            ],
            LLMUsage(1300, cache_write_tokens=0, reasoning_tokens=0),
            self.name,
            model,
        )

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        self.routed_requests.append((model, req))
        stream = super().stream(model, req, params)
        try:
            async for chunk in stream:
                if isinstance(chunk, LLMDone):
                    chunk = replace(
                        chunk, usage=replace(chunk.usage, cache_write_tokens=0, reasoning_tokens=0)
                    )
                yield chunk
        finally:
            await stream.aclose()


def question_routes(session: Any) -> list[dict[str, Any]]:
    """Read-only runtime snapshots also cover canceled passes absent from completed logs."""
    return [
        {
            "question_id": str(q.qid),
            "text": q.text,
            "question_type": q.qtype,
            "detector_depth": q.depth,
            "held": q.held,
            "confirmed": q.confirmed,
            "cancelled": q.cancelled,
            "discarded": q.discarded,
            "early_ms": q.early_ms,
            "started_passes": sorted(q.started_passes),
            "plans": {name: asdict(plan) for name, plan in q.plans.items()},
        }
        for q in session.answers.questions.values()
    ]


async def wait_started(session: Any, action: dict[str, Any], timeout_s: float) -> None:
    """Observe a real held request plan before ending its synthetic utterance."""
    poll_s = max(0.01, 2 * time.get_clock_info("monotonic").resolution)
    async with asyncio.timeout(timeout_s):
        # Production question state has no start-event hook; polling is bounded by timeout.
        while not any(  # noqa: ASYNC110
            q.text == action["text"] and action.get("pass", "fast") in q.started_passes
            for q in session.answers.questions.values()
        ):
            await asyncio.sleep(poll_s)


def scenarios(cycles: int = 8) -> list[dict[str, Any]]:
    def ask(text: str, **extra: Any) -> dict[str, Any]:
        return {"op": "say", "text": text, **extra}

    technical = (
        "How do you preserve exactly-once effects when a worker crashes after commit "
        "but before acknowledgement?"
    )
    design = (
        "How would you design a distributed notification system with per-recipient ordering "
        "and regional failover?"
    )
    return [
        *[
            {
                "id": identifier,
                "actions": [
                    {"op": "partial", "text": question, "start": True},
                    {"op": "wait_started", "text": question},
                    {"op": "end"},
                    {"op": "settle"},
                ],
                "routing_fixture": "synthetic deep decision arrives after initial held plan",
            }
            for identifier, question in (
                ("early-technical-depth", technical),
                ("early-system-design", design),
            )
        ],
        {
            "id": "missing-personal-context",
            "profile": "Alex is an interview candidate. No employers or incident stories supplied.",
            "actions": [
                ask("Can you describe a production incident you handled at Northwind?"),
                {"op": "settle"},
            ],
        },
        {
            "id": "prepared-source-mismatch",
            "prepared_fixture": True,
            "stale_prepared_source": True,
            "actions": [ask("Where are you based?"), {"op": "settle"}],
        },
        {
            "id": "standup-source-ownership",
            "mode": "standup",
            "max_confirmed": 1,
            "actions": [
                ask("Priya here. I deployed the carrier preview to production.", speaker="priya"),
                {"op": "settle"},
                ask(
                    "My retry logging passed staging. Morgan's review is pending; "
                    "I have not deployed it to production.",
                    speaker="me",
                ),
                {"op": "wait", "type": "transcript.final", "channel": "me"},
                ask("Alex, what is the status of your retry logging?"),
                {"op": "settle"},
            ],
        },
        {
            "id": "partial-revision",
            "actions": [
                ask(
                    "How would you scale the service with Redis?",
                    final_text="How would you scale the service without Redis?",
                ),
                {"op": "settle"},
            ],
            "must_question": "without Redis",
        },
        {
            "id": "duplicate-partial",
            "actions": [
                {"op": "partial", "text": "Where are you based?", "start": True},
                {"op": "partial", "text": "Where are you based?"},
                {"op": "end"},
                {"op": "settle"},
            ],
            "max_confirmed": 1,
        },
        {
            "id": "rapid-speakers",
            "min_questions": 2,
            "actions": [
                ask(
                    "How would you design a distributed notification system?",
                    speaker="interviewer_a",
                ),
                {"op": "wait", "type": "answer.delta"},
                ask("Why did you choose Redis for rate limiting?", speaker="interviewer_b"),
                {"op": "settle"},
            ],
        },
        {
            "id": "session-interrupt",
            "actions": [
                ask("How would you design a distributed notification system?"),
                {"op": "wait", "type": "answer.delta"},
                {"op": "stop"},
            ],
        },
        {
            "id": "changing-context",
            "min_questions": 2,
            "actions": [
                ask("What is the current notification retry policy?"),
                {"op": "settle"},
                ask("The notification service now uses three retries with jitter.", speaker="me"),
                {"op": "wait", "type": "transcript.final", "channel": "me"},
                ask("What is the current notification retry policy?"),
                {"op": "settle"},
            ],
        },
        {
            "id": "prepared-context-change",
            "min_questions": 2,
            "prepared_fixture": True,
            "actions": [
                ask("Where are you based?"),
                {"op": "settle"},
                ask("I have moved to Austin and I am based there now.", speaker="me"),
                {"op": "wait", "type": "transcript.final", "channel": "me"},
                ask("Where are you based?"),
                {"op": "settle"},
            ],
        },
        {
            "id": "outage-recovery",
            "min_questions": 2,
            "actions": [
                {"op": "fault", "outage": True},
                ask("Where are you based?"),
                {"op": "wait", "type": "answer.error"},
                {"op": "fault", "outage": False},
                ask("Which programming languages do you use?"),
                {"op": "settle"},
            ],
            "expect_error": True,
        },
        {
            "id": "stream-timeout",
            "min_questions": 2,
            "actions": [
                {"op": "fault", "stream_timeout": True},
                ask("How would you design a distributed notification system?"),
                {"op": "wait", "type": "answer.error"},
                {"op": "fault", "stream_timeout": False},
                ask("Where are you based?"),
                {"op": "settle"},
            ],
            "expect_error": True,
        },
        {
            "id": "standup-context",
            "mode": "standup",
            "actions": [
                ask(
                    "Yesterday I added retry logging. The staging check passed today.", speaker="me"
                ),
                {"op": "wait", "type": "transcript.final", "channel": "me"},
                ask("Alex, what is the status of the retry logging?"),
                {"op": "settle"},
            ],
        },
        {
            "id": "sustained-load",
            "min_questions": cycles,
            "actions": [
                action
                for i in range(cycles)
                for action in (
                    ask(
                        (
                            "Which programming languages do you use?",
                            "Why did you choose Redis?",
                            "How would you prevent duplicate notifications?",
                            "Where are you based?",
                        )[i % 4],
                        speaker=f"interviewer_{i % 2}",
                    ),
                    {"op": "settle"},
                )
            ],
        },
    ]


def inspect_events(events: list[dict[str, Any]]) -> list[str]:
    """Protocol safety, without assuming a different question invalidates completed history."""
    violations: list[str] = []
    known: set[str] = set()
    cancelled: set[str] = set()
    terminal: set[tuple[str, str]] = set()
    next_chunk: dict[tuple[str, str], int] = {}
    last_wire = 0
    for event in events:
        message = event["message"]
        kind = message["type"]
        wire = message.get("seq", 0)
        if wire and wire != last_wire + 1:
            violations.append(f"wire_sequence:{last_wire}->{wire}")
        last_wire = wire or last_wire
        qid = message.get("question_id", "")
        slot = (qid, message.get("pass", ""))
        if event.get("after_close"):
            violations.append(f"event_after_socket_close:{kind}")
        if kind == "question.detected":
            if qid in cancelled:
                violations.append(f"detection_after_cancelled:{qid}")
            known.add(qid)
        elif kind == "question.cancelled":
            cancelled.add(qid)
        elif kind in ("answer.delta", "answer.done"):
            if qid not in known:
                violations.append(f"unknown_question:{qid}")
            if qid in cancelled or slot in terminal:
                violations.append(f"output_after_terminal:{qid}:{slot[1]}")
            if kind == "answer.delta" and "sequence" in message:
                expected = next_chunk.get(slot, 0)
                if message["sequence"] != expected:
                    violations.append(f"chunk_sequence:{qid}:{expected}->{message['sequence']}")
                next_chunk[slot] = message.get("sequence_end", message["sequence"] + 1)
            if kind == "answer.done":
                if "delta_count" in message and message["delta_count"] != next_chunk.get(slot, 0):
                    violations.append(f"completion_count:{qid}")
                terminal.add(slot)
        elif kind == "answer.error":
            if qid not in known:
                violations.append(f"unknown_question:{qid}")
            if (qid in cancelled or slot in terminal) and message.get("code") != "cancelled":
                violations.append(f"error_after_terminal:{qid}:{slot[1]}")
            terminal.add(slot)
    return violations


async def settle(session: Any, timeout_s: float) -> None:
    poll_s = max(0.01, 2 * time.get_clock_info("monotonic").resolution)
    async with asyncio.timeout(timeout_s):
        quiet_since: float | None = None
        while True:
            turns = session.turns
            busy = bool(turns._detached) or (turns.task is not None and not turns.task.done())
            busy = busy or any(
                not task.done()
                for q in session.answers.questions.values()
                for task in q.tasks.values()
            )
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since > 0.1:
                return
            await asyncio.sleep(poll_s)


class TimedMessages(list[dict[str, Any]]):
    def __init__(self, events: list[dict[str, Any]], origin: float) -> None:
        super().__init__()
        self.events, self.origin = events, origin
        self.closed = False

    def append(self, message: dict[str, Any]) -> None:
        self.events.append(
            {
                "at_ms": round((time.monotonic() - self.origin) * 1000),
                "message": message,
                "after_close": self.closed,
            }
        )
        super().append(message)


def timing_rows(
    events: list[dict[str, Any]], injected: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    from copilot_gateway.adaptive import SentenceBoundary

    questions: dict[str, dict[str, Any]] = {}
    boundaries: dict[str, SentenceBoundary] = {}
    for event in events:
        message, at = event["message"], event["at_ms"]
        qid = message.get("question_id")
        if message["type"] == "question.detected":
            matches = [
                item
                for item in injected
                if item["at_ms"] <= at
                and item.get("speaker") != "me"
                and message["text"] in (item.get("text"), item.get("final_text"))
            ]
            origin = matches[-1]["at_ms"] if matches else None
            questions.setdefault(
                qid,
                {
                    "question_id": qid,
                    "text": message["text"],
                    "injected_at_ms": origin,
                    "detected_at_ms": at,
                    "first_delta_at_ms": None,
                    "first_sentence_boundary_at_ms": None,
                    "complete_at_ms": None,
                },
            )
            questions[qid]["text"] = message["text"]
            boundaries.setdefault(qid, SentenceBoundary())
        if qid not in questions or message.get("pass") != "fast":
            continue
        row = questions[qid]
        if message["type"] == "answer.delta":
            if row["first_delta_at_ms"] is None:
                row["first_delta_at_ms"] = at
            if boundaries[qid].feed(message["text"]):
                row["first_sentence_boundary_at_ms"] = at
        elif message["type"] == "answer.done":
            row["complete_at_ms"] = at
            if boundaries[qid].feed("", final=True):
                row["first_sentence_boundary_at_ms"] = at
        elif message["type"] == "answer.error":
            row["error"] = message["code"]
    for row in questions.values():
        origin = row["injected_at_ms"]
        for field in ("first_delta", "first_sentence_boundary", "complete"):
            at = row[field + "_at_ms"]
            row[field + "_from_text_ms"] = (
                at - origin if at is not None and origin is not None else None
            )
    return list(questions.values())


def latency_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed text-injection latency; canceled/missing completions stay missing."""
    return {
        strategy: {
            scenario: {
                metric: distribution(
                    [
                        question.get(metric)
                        for result in results
                        if result["architecture"] == strategy and result["id"] == scenario
                        for question in result.get("questions", [])
                    ]
                )
                for metric in (
                    "first_delta_from_text_ms",
                    "first_sentence_boundary_from_text_ms",
                    "complete_from_text_ms",
                )
            }
            for scenario in sorted({result["id"] for result in results})
        }
        for strategy in sorted({result["architecture"] for result in results})
    }


async def replay_session(
    scenario: dict[str, Any],
    settings: Settings,
    providers: list[Any],
    *,
    trial_id: str,
    timeout_s: float = 30,
    attempt_events: list[dict[str, Any]] | None = None,
    attempt_store: RecordStore | None = None,
) -> dict[str, Any]:
    token = TRIAL.set(trial_id)
    control: dict[str, Any] = {"timeout_delay_s": 0.2, "injected_failures": 0}
    control_token = CONTROL.set(control)
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    injected: list[dict[str, Any]] = []
    attempt_events = attempt_events if attempt_events is not None else []
    user: UUID = uuid4()
    server = FakeServer()
    client_db = FakeAsyncValkey(server=server)
    profile = scenario.get("profile", PROFILE)
    digest = Digest.model_validate(
        {"version": Digest.version_of(profile), "text": profile, "refs": {}}
    )
    await client_db.set(keys.digest(user), digest.model_dump_json(by_alias=True))
    them, me = TextSTTControl(final_delay_s=0.12), TextSTTControl(final_delay_s=0.01)

    def on_attempt(event: Any) -> None:
        entry = {"trial_id": TRIAL.get(), **asdict(event)}
        attempt_events.append(entry)
        if attempt_store is not None:
            attempt_store.put("attempts", f"{event.attempt_id}:{event.status}", entry)

    router = build_router(settings, providers, on_attempt=on_attempt)
    services = Services(
        settings=settings,
        store=GatewayStore(FakeAsyncValkey(server=server), settings),
        jobs=MemoryJobQueue(),
        router=router,
        verifier=StaticVerifier({"token-a": Principal(user, "Alex", None)}),
        stt_providers=[("text", lambda _: RevisionSTT(them))],
        stt_me_providers=[("text", lambda _: RevisionSTT(me))],
        embedder=PreparedReplayEmbedder() if scenario.get("prepared_fixture") else None,
    )
    result: dict[str, Any] = {
        "id": scenario["id"],
        "trial_id": trial_id,
        "architecture": settings.answer_architecture,
        "status": "completed",
        "events": events,
        "injections": injected,
        "route_observations": [],
        "profile_sha256": fingerprint(profile),
        "fixture_provenance": "checked-in fictional controlled scenario; no historical audio",
        "scope": "text injection through synthetic STT, real Session/TurnDetector/AnswerEngine",
    }
    session = None
    protocol = None
    try:
        async with GatewayServer(services) as gateway:
            protocol = ProtocolClient(gateway.url)
            timed_messages = TimedMessages(events, started)
            protocol.messages = timed_messages
            await protocol.start(mode=scenario.get("mode", "interview"))
            result["session_id"] = protocol.session_id
            session = next(iter(gateway.manager.sessions.values()))
            if scenario.get("prepared_fixture"):
                session.set_pack(
                    SessionPack(  # pyright: ignore[reportCallIssue]
                        pack_id=uuid4(),
                        user_id=user,
                        mode="interview",
                        built_at="2026-11-25T00:00:00Z",
                        digest_version=digest.version,
                        embedding_model=PreparedReplayEmbedder.model_id,
                        prepared_answers=[
                            PreparedAnswer(
                                question="Where are you based?",
                                talking_points=["I am based in Chicago."],
                                embedding_b64=encode_embedding([1.0, 0.0]),
                                grounding_policy=PREPARED_DIGEST_POLICY,
                                evidence_version=(
                                    "stale-fictional-source"
                                    if scenario.get("stale_prepared_source")
                                    else digest.version
                                ),
                            )
                        ],
                    )
                )
                session._prompt_ctx = session._build_prompt_ctx()
                result["prepared_fixture"] = {
                    "source": "synthetic versioned pack; exact fake question embeddings",
                    "initial_fact": "Chicago",
                    "changed_conversation_fact": "Austin",
                }
            capture_ms = 0
            wait_start = 0
            async with asyncio.timeout(timeout_s):
                for action in scenario["actions"]:
                    op = action["op"]
                    if op in ("say", "partial"):
                        wait_start = len(protocol.messages)
                        speaker = action.get("speaker", "them")
                        channel = 1 if speaker == "me" else 0
                        entry = {
                            **action,
                            "at_ms": round((time.monotonic() - started) * 1000),
                            "speaker": speaker,
                        }
                        injected.append(entry)
                        payload = json.dumps(
                            {key: action[key] for key in ("text", "final_text") if key in action}
                        ).encode()
                        payload = payload.ljust(max(3200, -(-len(payload) // 3200) * 3200), b"\0")
                        await protocol.frame(
                            channel,
                            1 if op == "say" or action.get("start") else 0,
                            payload,
                            capture_ms,
                        )
                        capture_ms += 100
                        if op == "say":
                            await protocol.frame(channel, 2, b"", capture_ms)
                            capture_ms += 6000
                    elif op == "wait_started":
                        await wait_started(session, action, timeout_s)
                        result["route_observations"].append(
                            {
                                "stage": "partial_before_end",
                                "at_ms": round((time.monotonic() - started) * 1000),
                                "questions": question_routes(session),
                            }
                        )
                    elif op == "end":
                        await protocol.frame(0, 2, b"", capture_ms)
                        capture_ms += 6000
                    elif op == "wait":
                        match = {key: value for key, value in action.items() if key != "op"}
                        await protocol.wait(
                            lambda message, match=match: all(
                                message.get(k) == v for k, v in match.items()
                            ),
                            timeout=timeout_s,
                            start=wait_start,
                        )
                    elif op == "settle":
                        # Wait for frame processing before testing asynchronous turn/answer tasks.
                        await protocol.wait(
                            lambda m: m["type"] == "transcript.final",
                            timeout=timeout_s,
                            start=wait_start,
                        )
                        await settle(session, timeout_s)
                    elif op == "fault":
                        control.update({k: v for k, v in action.items() if k != "op"})
                        injected.append(
                            {**action, "at_ms": round((time.monotonic() - started) * 1000)}
                        )
                    elif op == "pause":
                        seconds = float(action["seconds"])
                        if not math.isfinite(seconds) or not 0 <= seconds <= 1:
                            raise ValueError("Replay pauses must be finite and at most one second")
                        await asyncio.sleep(seconds)
                    elif op == "stop":
                        break
                    else:
                        raise ValueError(f"Unknown replay operation: {op}")
            await protocol.send({"type": "session.end", "summarize": False})
            result["close_code"] = await protocol.closed(timeout=10)
            timed_messages.closed = True
            result["socket_closed_at_ms"] = round((time.monotonic() - started) * 1000)
    except Exception as error:
        result.update(
            status="harness_or_session_failure",
            error_type=type(error).__name__,
            error_types=error_chain(error),
        )
        # Preserve failure artifacts; never silently drop a failed session.
    finally:
        if protocol is not None:
            await protocol.close()
        if protocol is not None and result.get("session_id"):
            result["metadata_logs"] = [
                json.loads(fields[b"e"])
                for _, fields in await client_db.xrange(keys.sess_log(protocol.session_id))
            ]
        result["elapsed_s"] = round(time.monotonic() - started, 4)
        result["synthetic_faults"] = control["injected_failures"]
        result["synthetic_fault_types"] = {
            name: control.get(name, 0) for name in ("connection_outages", "stream_timeouts")
        }
        result["violations"] = inspect_events(events)
        result["questions"] = timing_rows(events, injected)
        result["late_events_after_close"] = sum(bool(event.get("after_close")) for event in events)
        if session is not None:
            result["routing"] = question_routes(session)
            result["pending_after_close"] = sum(
                not task.done()
                for q in session.answers.questions.values()
                for task in q.tasks.values()
            )
            result["writer_pending_after_close"] = bool(session.writer.pending())
            result["turn_counters"] = {
                field: getattr(session.turns, field)
                for field in (
                    "final_rechecks",
                    "held_starts",
                    "held_discarded",
                    "speculative_cancels",
                    "early_starts",
                )
            }
        confirmed = {
            e["message"]["question_id"]: e["message"]["text"]
            for e in events
            if e["message"]["type"] == "question.detected" and not e["message"].get("speculative")
        }
        if scenario.get("prepared_fixture"):
            sources = [
                event["message"].get("source")
                for event in events
                if event["message"]["type"] == "answer.done"
                and event["message"].get("pass") == "fast"
            ]
            result["prepared_sources"] = sources
            if not scenario.get("stale_prepared_source") and (
                not sources or sources[0] != "prepared"
            ):
                result["violations"].append("missing_initial_prepared_fast_path")
            if settings.answer_architecture != "legacy_dual" and sources[-1:] == ["prepared"]:
                result["violations"].append("stale_prepared_after_source_or_context_update")
        if scenario.get("must_question") and not any(
            scenario["must_question"] in text for text in confirmed.values()
        ):
            result["violations"].append("missing_corrected_question")
        if len(confirmed) > scenario.get("max_confirmed", math.inf):
            result["violations"].append("duplicate_confirmed_question")
        result["answer_error_counts"] = dict(
            Counter(e["message"]["code"] for e in events if e["message"]["type"] == "answer.error")
        )
        allowed = {"cancelled"}
        if scenario["id"] == "outage-recovery":
            allowed.add("llm_unavailable")
        if scenario["id"] == "stream-timeout":
            allowed.add("timeout")
        if set(result["answer_error_counts"]) - allowed:
            result["status"] = "provider_or_engine_failure"
        if len(result["questions"]) < scenario.get("min_questions", 1):
            result["status"] = "missing_question"
        if result.get("close_code") not in (None, 1000):
            result["status"] = "unexpected_socket_close"
        if scenario["id"] != "session-interrupt" and confirmed:
            last_qid = list(confirmed)[-1]
            if not any(
                e["message"]["type"] == "answer.done"
                and e["message"].get("question_id") == last_qid
                and e["message"].get("pass") == "fast"
                for e in events
            ):
                result["status"] = "missing_final_answer"
        if scenario["id"] == "session-interrupt":
            interrupted = {
                e["message"]["question_id"]
                for e in events
                if e["message"]["type"] == "answer.delta" and e["message"].get("pass") == "fast"
            } - {
                e["message"]["question_id"]
                for e in events
                if e["message"]["type"] in {"answer.done", "answer.error"}
                and e["message"].get("pass") == "fast"
            }
            retained = {
                e.get("question_id")
                for e in result.get("metadata_logs", [])
                if e.get("kind") == "pass_cancelled" and e.get("pass") == "fast"
            }
            if interrupted - retained:
                result["violations"].append("missing_retained_cancellation")
        if result.get("writer_pending_after_close"):
            result["violations"].append("retained_writes_pending_after_close")
        if result["violations"] or result.get("pending_after_close"):
            result["status"] = "invariant_failure"
        await client_db.aclose()
        await services.store.client.aclose()
        CONTROL.reset(control_token)
        TRIAL.reset(token)
    return result


def replay_settings(live: bool, strategy: str) -> Settings:
    settings = Settings() if live else Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    overrides: dict[str, Any] = {
        "env": "test",
        "database_url": "",
        "answer_architecture": strategy,
        "embedder": "none",
        "prewarm_jobs": [],
        "llm_warm_interval_s": None,
        "personal_ai_api_url": None,
        "flush_interval_s": 600.0,
        "stt_stall_s": 30,
        "follow_user": False,
        "first_message_timeout_s": 5,
    }
    if not live:
        overrides.update(
            llm_providers=["openai"],
            openai_detector_model="gpt-6-luna",
            openai_detector_reasoning="none",
            openai_fast_model="gpt-6-luna",
            openai_fast_reasoning="none",
            openai_deep_model="gpt-6.1-sol",
            openai_deep_reasoning="low",
            detector_api="decisions",
            model_prices={
                **settings.model_prices,
                "gpt-6-luna": ModelPrice(input=0.1, cached=0.01, output=0.5),
                "gpt-6.1-sol": ModelPrice(input=2.0, cached=0.1, output=10.0),
            },
            speculative_start_ms=50,
            silence_timer_ms=150,
            final_wait_max_ms=500,
            early_decision_interval_ms=50,
            first_token_timeout_ms=1000,
            detector_hedge_ms=None,
            fast_hedge_ms=None,
            deep_hedge_ms=None,
            continuation_gap_ms=0,
            first_show_min_words=None,
        )
    return settings.model_copy(update=overrides)


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    if not (
        1 <= args.reps <= 100
        and 1 <= args.concurrency <= 8
        and 1 <= args.max_inflight <= 32
        and 1 <= args.cycles <= 100
        and 1 <= args.timeout <= 600
    ):
        raise ValueError(
            "Session replay requires finite concurrency, repetitions, cycles and timeout"
        )
    directory = private_directory(args.out)
    return await execute_locked(args, directory)


def validate_directory(directory: Path) -> None:
    if not (directory / "identity.json").exists() and any(
        path.name not in {"runner.lock", "checkpoint-records"} for path in directory.iterdir()
    ):
        raise ValueError("Existing artifacts lack a resumable identity; use a fresh directory")
    if (
        not (directory / "identity.json").exists()
        and (directory / "checkpoint-records").exists()
        and any(
            path.name != "manifest.json" for path in (directory / "checkpoint-records").iterdir()
        )
    ):
        raise ValueError("Existing artifacts lack a resumable identity; use a fresh directory")


async def execute_locked(args: argparse.Namespace, directory: Path) -> dict[str, Any]:
    writer = DurableWriter(directory, initialization_guard=require_async_directory)
    try:
        await writer.ready()
        return await execute_owned(args, directory, BufferedRecordStore(writer))
    finally:
        await writer.close(timeout_s=10)


async def execute_owned(
    args: argparse.Namespace, directory: Path, buffered_store: BufferedRecordStore
) -> dict[str, Any]:
    validate_directory(directory)
    chosen = [s for s in scenarios(args.cycles) if not args.scenarios or s["id"] in args.scenarios]
    if not chosen or (args.scenarios and set(args.scenarios) - {s["id"] for s in chosen}):
        raise ValueError("Unknown or empty scenario selection")
    if not args.architectures or len(set(args.architectures)) != len(args.architectures):
        raise ValueError("Select distinct architectures")
    settings = replay_settings(args.live, STRATEGIES[args.architectures[0]])
    identity = experiment_identity(
        chosen, settings, args.reps, args.seed, args.concurrency, args.timeout
    )
    identity.update(
        scope="text -> synthetic STT -> production Session/TurnDetector -> websocket answer",
        origin="individual text frame injection",
        live=args.live,
        max_inflight=args.max_inflight,
        strategies={k: STRATEGIES[k] for k in args.architectures},
        budget_limit_usd=args.max_cost,
        currency_cutoff_disabled=args.max_cost is None,
        connectivity_check=not args.skip_connectivity_check,
        session_settings=settings.model_dump(
            mode="json",
            include={
                "silence_timer_ms",
                "speculative_start_ms",
                "speculative_hold",
                "speculative_min_words",
                "early_decision_interval_ms",
                "early_decision_all_modes",
                "detector_api",
                "detector_max_tokens",
                "detector_hedge_ms",
                "detector_context_tokens",
                "openai_detector_model",
                "openai_detector_reasoning",
                "openai_detector_retry_tier",
                "openai_summary_model",
                "openai_summary_reasoning",
                "name_address_grace_ms",
                "final_wait_max_ms",
                "follow_user",
                "continuation_gap_ms",
                "conversation_recent_tokens",
                "conversation_summary_tokens",
                "conversation_summary_chunk_tokens",
                "conversation_summary_retry_s",
                "flush_interval_s",
                "stt_stall_s",
            },
        ),
        limitations=[
            "Synthetic scenarios and fictional profile, not original historical audio.",
            "No STT recognition, remote-speaker diarization, or physical UI paint measurement.",
            "In-memory storage and fictional digest; external retrieval disabled; "
            "fake prepared vectors.",
            "Offline Decisions labels and answer text are scripted, not quality evaluations.",
            "One shared admission gate covers detector and answer calls before cost reservation.",
            "Session admission waits count toward router deadlines; per-request gate waits are "
            "not recorded by the inner audit wrapper. Rate/auth/config errors stop new dispatches.",
            "Follow-user and prewarming disabled; summaries retain configured behavior.",
            "Offline timers are accelerated; live mode retains configured production turn timers.",
            "First complete sentence is a boundary metric; human usefulness is not established.",
            "Fault scenarios inject labeled failures; they are not spontaneous provider incidents.",
        ],
    )
    identity_path = directory / "identity.json"
    use_records = (
        directory / "checkpoint-records" / "manifest.json"
    ).exists() or not identity_path.exists()
    if use_records:
        identity["checkpoint_policy"] = ASYNC_POLICY
    if identity_path.exists():
        previous = json.loads(identity_path.read_text("utf-8"))
        if (
            not previous.get("source_archive")
            or {key: value for key, value in previous.items() if key != "source_archive"}
            != identity
        ):
            raise ValueError("Run source or settings changed; use a fresh private output")
    identity["source_archive"] = source_archive(
        directory, root_directory(), identity["code"], identity
    )
    immutable(identity_path, identity)
    immutable(directory / "scenarios.json", chosen)

    circuit = FailureCircuit(3)
    gate_path = directory / "session-admission-stop.json"
    gate = DispatchGate(
        args.max_inflight,
        on_stop=lambda reason: (
            buffered_store.snapshot_file(gate_path.name, {"reason": reason})
            if not buffered_store.writer.failed
            else None
        ),
    )
    if gate_path.exists():
        gate.stop(json.loads(gate_path.read_text("utf-8"))["reason"])
    store = buffered_store if use_records else None
    if store is not None:
        store.on_error = lambda: gate.stop("checkpoint_error")

    def read_list(kind: str, name: str) -> list[dict[str, Any]]:
        if store is not None:
            return store.read(kind)
        path = directory / name
        return json.loads(path.read_text("utf-8")) if path.exists() else []

    requests = read_list("requests", "request-identities.json")
    attempts = read_list("attempts", "provider-attempts.json")
    results = read_list("sessions", "sessions.json")
    budget = SessionBudget(
        directory, settings, math.inf if args.max_cost is None else args.max_cost, store=store
    )
    schedule = [
        item
        for block in case_blocks(chosen, args.architectures, args.reps, args.seed)
        for item in block
    ]
    scheduled: list[dict[str, Any]] = [
        {
            "scenario": scenario["id"],
            "architecture": label,
            "rep": rep,
            "trial_id": f"{scenario['id']}:{STRATEGIES[label]}:{rep}",
        }
        for scenario, label, rep in schedule
    ]
    immutable(directory / "schedule.json", scheduled)
    planned = [row["trial_id"] for row in scheduled]
    completed: set[str] = set()
    for result in results:
        expected = f"{result['id']}:{result['architecture']}:{result['rep']}"
        if result.get("trial_id") != expected or expected not in planned or expected in completed:
            raise ValueError("Duplicate or mismatched completed session checkpoint")
        completed.add(expected)
    attempted = (
        {str(row["trial_id"]) for row in requests if row.get("trial_id")}
        | {str(row["trial_id"]) for row in budget.entries if row.get("trial_id")}
        | {str(row["trial_id"]) for row in attempts if row.get("trial_id")}
    )
    journal = (
        RecordDispatchJournal(store, planned, completed, attempted)
        if store is not None
        else DispatchJournal(directory, planned, completed, attempted)
    )
    progress = BufferedProgressSnapshots(directory, buffered_store)
    started_trials: list[str] = []
    invocation_id = str(uuid4())
    invocation_started = time.time()

    def progress_value() -> dict[str, Any]:
        states = Counter(journal.states.values())
        return {
            "planned_sessions": len(planned),
            "completed": len(results),
            "in_flight": states["in_flight"],
            "not_started": states["not_started"],
            "ambiguous": states["ambiguous"],
            "circuit_reason": gate.reason or circuit.reason,
            "provider_admission": gate.snapshot(),
            "progress_snapshot_errors": progress.diagnostics(),
        }

    def checkpoint() -> None:
        journal.checkpoint()
        if store is None:
            save(directory / "sessions.json", results)
            save(directory / "provider-attempts.json", attempts)
        progress.active(progress_value())

    async def export_after_drain(monitor: LoopLagMonitor | None = None) -> None:
        try:
            await export_drained(monitor)
        except BaseException as error:
            record_finalization_failure(directory, error)
            raise

    async def export_drained(monitor: LoopLagMonitor | None = None) -> None:
        if store is not None:
            store.phase = "drained"
            await store.flush()
            for kind, filename in (
                ("sessions", "sessions.json"),
                ("requests", "request-identities.json"),
                ("attempts", "provider-attempts.json"),
                ("cost", "cost.json"),
            ):
                store.export(kind, filename)
            if isinstance(journal, RecordDispatchJournal):
                journal.export()
            telemetry = {
                "invocation_id": invocation_id,
                "started_epoch_s": invocation_started,
                "finished_epoch_s": time.time(),
                "started_trials": started_trials,
                "checkpoint_writes": store.telemetry(),
                "event_loop_lag": monitor.summary() if monitor is not None else None,
            }
            # Preserve measured earlier invocations when a no-dispatch resume refreshes exports.
            if started_trials or not (directory / "checkpoint-write-telemetry.json").exists():
                save(directory / "checkpoint-write-telemetry.json", store.telemetry())
                if monitor is not None:
                    save(directory / "event-loop-lag.json", monitor.summary())
            history = directory / "instrumentation-invocations"
            history.mkdir(exist_ok=True)
            save(history / f"{invocation_id}.json", telemetry)
        elif monitor is not None:
            save(directory / "event-loop-lag.json", monitor.summary())
        save(
            directory / "not-started.json",
            [
                {
                    **row,
                    "dispatch_state": journal.states[row["trial_id"]],
                    "reason": "prior_session_dispatch_has_no_durable_result"
                    if journal.states[row["trial_id"]] == "ambiguous"
                    else gate.reason
                    or circuit.reason
                    or ("budget_exhausted" if budget.blocked else "not_dispatched"),
                }
                for row in scheduled
                if journal.states[row["trial_id"]] != "completed"
            ],
        )
        try:
            await progress.final(progress_value)
        finally:
            save(directory / "progress-snapshot-errors.json", progress.diagnostics())

    checkpoint()
    pending = [row for row in scheduled if journal.states[row["trial_id"]] == "not_started"]
    if pending and args.live and not args.skip_connectivity_check:
        check = await connectivity_check(settings)
        save(directory / "connectivity.json", check)
        if not check["ready"]:
            await export_after_drain()
            return {
                "status": "transport_unavailable",
                "provider_calls_this_invocation": 0,
                "remaining_sessions": len(pending),
                "ambiguous_sessions": sum(
                    state == "ambiguous" for state in journal.states.values()
                ),
            }
    raw = (
        (
            make_providers(settings, timeout_s=args.timeout)
            if args.live
            else [OfflineSessionProvider()]
        )
        if pending
        else []
    )
    providers = [
        LimitedProvider(
            SessionAuditProvider(
                FaultProvider(p),
                budget,
                requests,
                directory,
                store=store,
                dispatch_guard=gate.check_open,
            ),
            gate,
        )
        for p in raw
    ]
    semaphore = asyncio.Semaphore(args.concurrency)
    aborting = False

    async def one(scenario: dict[str, Any], label: str, rep: int) -> None:
        nonlocal aborting
        async with semaphore:
            try:
                trial = f"{scenario['id']}:{STRATEGIES[label]}:{rep}"
                if (
                    aborting
                    or budget.blocked
                    or gate.reason
                    or circuit.reason
                    or not journal.start(trial)
                ):
                    return
                if isinstance(store, BufferedRecordStore):
                    await store.flush()
                started_trials.append(trial)
                result = await replay_session(
                    scenario,
                    replay_settings(args.live, STRATEGIES[label]),
                    providers,
                    trial_id=trial,
                    timeout_s=args.timeout,
                    attempt_events=attempts,
                    attempt_store=store,
                )
                result["rep"] = rep
                trial_requests = [r for r in requests if r["trial_id"] == trial]
                # Storage failure is local instrumentation, not a model/network failure.
                if buffered_store.writer.failed:
                    result["instrumentation_failure"] = buffered_store.writer.failure_reason
                    result["status"] = "checkpoint_failure"
                    gate.stop("checkpoint_error")
                # Successful detector/primary calls cannot mask an exhausted answer failure.
                if not buffered_store.writer.failed:
                    circuit.observe(
                        {
                            "failure": result["status"] != "completed",
                            "metadata_logs": result.get("metadata_logs", []),
                        },
                        [row for row in trial_requests if row.get("status") != "completed"],
                    )
                # Durable results precede journal completion. A crash between these writes
                # is recovered from the completed result and cannot repeat billed work.
                if store is not None:
                    store.put("sessions", trial, result)
                    results.append(result)
                else:
                    results.append(result)
                    save(directory / "sessions.json", results)
                journal.finish(trial)
                checkpoint()
            except BaseException:
                aborting = True
                raise

    remaining = iter(schedule)

    async def worker() -> None:
        for item in remaining:
            if aborting or budget.blocked or gate.reason or circuit.reason:
                return
            await one(*item)

    monitor = AnchoredLoopLagMonitor()
    if store is not None:
        store.phase = "active_workers"
    await monitor.start()
    tasks = [asyncio.create_task(worker()) for _ in range(min(args.concurrency, len(schedule)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        try:
            await close_providers(raw)
        finally:
            await monitor.stop()
        checkpoint()
        await export_after_drain(monitor)
    previous_summary_path = directory / "summary.json"
    previous_summary: dict[str, Any] = (
        json.loads(previous_summary_path.read_text("utf-8"))
        if previous_summary_path.exists()
        else {}
    )
    states = Counter(journal.states.values())
    unresolved = [e for e in budget.entries if e["status"] in {"reserved", "usage_unpriced"}]
    known = round(sum(float(e["charged_usd"]) for e in budget.entries if e["status"] == "usage"), 8)
    summary = {
        "status": "completed"
        if len(results) == len(schedule) and all(row["status"] == "completed" for row in results)
        else "partial",
        "live": args.live,
        "checkpoint_policy": ASYNC_POLICY if store is not None else "legacy-aggregate",
        "planned_sessions": len(schedule),
        "sessions": len(results),
        "dispatch_states": dict(states),
        "remaining_sessions": states["not_started"],
        "ambiguous_sessions": states["ambiguous"],
        "circuit_reason": gate.reason or circuit.reason,
        "statuses": dict(Counter(r["status"] for r in results)),
        "invariant_violations": sum(len(r.get("violations", [])) for r in results),
        "provider_dispatches": sum(bool(row.get("provider_invoked")) for row in requests),
        "request_intents": len(requests),
        "peak_provider_inflight": max(gate.peak, previous_summary.get("peak_provider_inflight", 0)),
        "known_usage_usd": known,
        "observed_cost_usd": None if unresolved or states["ambiguous"] else known,
        "unresolved_reserved_usd": round(sum(float(e["charged_usd"]) for e in unresolved), 8),
        "usage_source": "provider-reported" if args.live else "synthetic mock usage; no API spend",
        "completion_scope": (
            "Replay protocol completion; not semantic answer completeness or quality"
        ),
        "admission_scope": {
            "max_sessions": args.concurrency,
            "max_provider_inflight": args.max_inflight,
            "includes": ["answer streams", "Responses detector", "Decisions detector", "retries"],
            "cost_reserved_after_admission": True,
            "queued_time_counts_toward_router_deadline": True,
            "immediate_rate_auth_stop": True,
            "per_request_queue_wait_ms": None,
            "circuit_scope": "completed sessions; bounded existing provider retry routes",
        },
        "cost_statuses": dict(Counter(e["status"] for e in budget.entries)),
        "text_injection_latency_ms": latency_summary(results),
    }
    save(directory / "summary.json", summary)
    return summary


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--architectures", choices=list(STRATEGIES), nargs="+", default=["A", "D"])
    parser.add_argument("--scenarios", nargs="+")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--cycles", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--max-inflight", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-cost", type=budget_limit, default=3.0)
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--skip-connectivity-check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(execute(args))))


if __name__ == "__main__":
    run()
