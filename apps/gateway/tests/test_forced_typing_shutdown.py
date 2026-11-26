"""Forced takeover shutdown races: real Session/AnswerEngine/writer, authored classifiers."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from copilot_core import keys
from copilot_core.ids import uuid7
from copilot_core.llm import LLMDone, LLMUsage
from copilot_gateway.answer import Question
from copilot_gateway.auth import Principal
from copilot_gateway.session import Session, SessionInfo, SessionManager
from copilot_gateway.turn import Detection

pytestmark = pytest.mark.asyncio

TEXT = "Alex, how should we design this queue?"


@pytest.fixture
async def session(env: Any) -> AsyncIterator[Session]:
    settings = env.settings(
        force_type_wait_ms=60_000,
        deep_pass_auto=False,
        prewarm_jobs=[],
        llm_warm_interval_s=None,
        lease_ttl_s=10,
    )
    services = env.services(settings, stt_names=())
    manager = SessionManager(services)
    info = SessionInfo(
        mode="interview",
        job_id=None,
        title=None,
        platform="authored",
        client_version="test",
        store_transcript=True,
        started_at=datetime.now(UTC),
        no_call=True,
    )
    obj = Session(services, manager, uuid7(), Principal(env.user_a, "Alex", None), info, None, None)
    manager.sessions[obj.sid] = obj
    await services.store.claim_active(env.user_a, obj.sid)
    await services.store.take_lease(obj.sid, services.task_id)
    obj.writer.set_state(**obj.state_fields())
    obj.start_core()
    assert await obj.writer.drain(within_s=1)
    try:
        yield obj
    finally:
        obj.ending = True
        obj.turns.cancel()
        await obj.answers.shutdown(within_s=1)
        await obj._stop()
        await services.store.release_lease(obj.sid, services.task_id)
        await services.store.release_active(env.user_a, obj.sid)
        await services.store.client.aclose()


def held_question(session: Session) -> Question:
    # The held pass has no live provider task in these tests. Actual AnswerEngine release,
    # confirmation, question logging and conversation persistence are exercised unchanged.
    question = Question(
        qid=uuid7(),
        text=TEXT,
        qtype="other",
        confidence=0,
        addressed=False,
        speculative=True,
        forced=False,
        t0=time.monotonic(),
        detector_ms=None,
        match=None,
        held=True,
    )
    session.answers.questions[question.qid] = question
    return question


async def stored_log(session: Session) -> list[dict[str, Any]]:
    rows = await session.svc.store.client.xrange(keys.sess_log(session.sid))
    return [json.loads(fields[b"e"]) for _, fields in rows]


class Classifier:
    """Returns real Detector usage only after cancellation cleanup has been released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelling = asyncio.Event()
        self.release = asyncio.Event()
        self.owner: asyncio.Task[Any] | None = None
        self.calls = 0
        self.recancels = 0

    async def complete(self, job: str, request: Any) -> tuple[str, LLMDone]:
        assert job == "detector"
        self.calls += 1
        self.owner = asyncio.current_task()
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelling.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.recancels += 1
                raise
        return (
            '{"answer":"technical"}',
            LLMDone(LLMUsage(input_tokens=17, output_tokens=3), "end_turn", "authored", "local"),
        )

    async def finish(self) -> None:
        self.release.set()
        task = self.owner
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("path", ["end", "abandon"])
@pytest.mark.parametrize("wrapper", ["direct", "pending_name", "finished_name"])
async def test_stop_drains_taken_classifier_before_persistent_writer(
    session: Session, monkeypatch: pytest.MonkeyPatch, path: str, wrapper: str
) -> None:
    classifier = Classifier()
    monkeypatch.setattr(session.router, "complete", classifier.complete)
    if wrapper != "direct":
        session.info.mode = "standup"
        session.settings.name_address_grace_ms = 0 if wrapper == "finished_name" else 60_000
    detection = session.detect(TEXT)
    await asyncio.wait_for(classifier.entered.wait(), 1)
    if wrapper == "finished_name":
        await asyncio.wait_for(asyncio.shield(detection), 1)
    q = held_question(session)
    session._force_taken(q.qid, TEXT, detection, respond=False)
    typing = list(session._forced)
    if wrapper == "finished_name":
        await asyncio.gather(*typing)
    else:
        await asyncio.sleep(0)
    assert await session.writer.drain(within_s=1)
    before_asked = session.conv.to_state()
    before_questions = sum(row["kind"] == "question" for row in await stored_log(session))
    drain_observations: list[bool] = []
    drain = session.writer.drain

    async def checked_drain(within_s: float) -> bool:
        drain_observations.append(bool(classifier.owner and classifier.owner.done()))
        return await drain(within_s)

    monkeypatch.setattr(session.writer, "drain", checked_drain)
    stop = asyncio.create_task(
        session.end("authored") if path == "end" else session.abandon(4500, "authored")
    )
    try:
        await asyncio.wait_for(classifier.cancelling.wait(), 1)
        assert not stop.done(), "Stop must retain the classifier while accepted usage settles"
        assert drain_observations == [], "The final writer drain must follow classifier cleanup"
        classifier.release.set()
        await asyncio.wait_for(stop, 2)
        await asyncio.gather(*typing, return_exceptions=True)
        assert classifier.owner is not None
        assert classifier.owner.done()
        assert classifier.calls == 1
        assert classifier.recancels == 0
        assert drain_observations
        assert all(drain_observations)
        assert not session.writer.pending()
        assert session.conv.to_state() == before_asked
        records = await stored_log(session)
        assert sum(row["kind"] == "question" for row in records) == before_questions
        [decision] = [row for row in records if row["kind"] == "detector"]
        assert decision["input_tokens"] == 17
        assert decision["output_tokens"] == 3
        assert not decision.get("estimated", False)
        if path == "end":
            assert records.index(decision) < next(
                index for index, row in enumerate(records) if row["kind"] == "session.ended"
            )
        state = await session.svc.store.load_state(session.sid)
        if "conversation" in state:
            assert json.loads(state["conversation"]) == before_asked
        assert session.sid not in session.manager.sessions
    finally:
        await classifier.finish()
        await asyncio.gather(stop, *typing, return_exceptions=True)


@pytest.mark.parametrize("terminal", ["ending", "lost", "_retention_failed"])
async def test_taken_typing_rechecks_terminal_before_any_late_mutation(
    session: Session, terminal: str
) -> None:
    q = held_question(session)
    detection: asyncio.Future[Detection] = asyncio.get_running_loop().create_future()
    session._force_taken(q.qid, TEXT, detection, respond=False)
    typing = list(session._forced)
    await asyncio.sleep(0)
    before = (q.qtype, q.depth, q.detector_ms, session.conv.to_state())
    setattr(session, terminal, True)
    detection.set_result(Detection("answer", "technical", latency_ms=25, depth="deep"))
    await asyncio.gather(*typing)
    assert (q.qtype, q.depth, q.detector_ms, session.conv.to_state()) == before
    assert not q.confirmed
    assert await session.writer.drain(within_s=1)
    assert not any(row["kind"] == "question" for row in await stored_log(session))


async def test_typing_timeout_keeps_classifier_owned_until_later_stop(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = Classifier()
    monkeypatch.setattr(session.router, "complete", classifier.complete)
    session.settings.force_type_wait_ms = 0
    detection = session.detect(TEXT)
    await asyncio.wait_for(classifier.entered.wait(), 1)
    q = held_question(session)
    session._force_taken(q.qid, TEXT, detection, respond=False)
    await asyncio.gather(*session._forced)
    assert q.confirmed
    assert classifier.owner is not None
    assert not classifier.owner.done()
    session.ending = True
    shutdown = asyncio.create_task(session._shutdown_answers(within_s=1))
    try:
        await asyncio.wait_for(classifier.cancelling.wait(), 1)
        classifier.release.set()
        assert await asyncio.wait_for(shutdown, 1)
        assert classifier.recancels == 0
        assert not session._forced_detections
    finally:
        await classifier.finish()
        await asyncio.gather(shutdown, return_exceptions=True)


async def test_shutdown_before_typing_task_starts_still_owns_classifier(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = Classifier()
    monkeypatch.setattr(session.router, "complete", classifier.complete)
    detection = session.detect(TEXT)
    await asyncio.wait_for(classifier.entered.wait(), 1)
    q = held_question(session)
    session._force_taken(q.qid, TEXT, detection, respond=False)
    session.ending = True
    pending = session._cancel_forced()  # no scheduling point after creating the typing task
    try:
        await asyncio.wait_for(classifier.cancelling.wait(), 1)
        classifier.release.set()
        assert await session._drain_forced(pending, within_s=1)
        assert classifier.recancels == 0
        assert not q.confirmed
    finally:
        await classifier.finish()


async def test_forced_drain_timeout_retains_owner_and_does_not_recancel_cleanup(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = Classifier()
    monkeypatch.setattr(session.router, "complete", classifier.complete)
    detection = session.detect(TEXT)
    await asyncio.wait_for(classifier.entered.wait(), 1)
    q = held_question(session)
    session._force_taken(q.qid, TEXT, detection, respond=False)
    session.ending = True
    try:
        assert not await asyncio.wait_for(session._shutdown_answers(within_s=0.02), 1)
        assert classifier.cancelling.is_set()
        assert classifier.owner is not None
        assert not classifier.owner.done()
        assert detection in session._forced_detections
        session._cancel_forced()
        await asyncio.sleep(0)
        assert classifier.recancels == 0
    finally:
        await classifier.finish()
    assert not q.confirmed
    assert not session._forced_detections


async def test_direct_stop_drains_taken_classifier_without_requiring_end(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = Classifier()
    classifier.release.set()
    monkeypatch.setattr(session.router, "complete", classifier.complete)
    detection = session.detect(TEXT)
    await asyncio.wait_for(classifier.entered.wait(), 1)
    q = held_question(session)
    session._force_taken(q.qid, TEXT, detection, respond=False)
    session.ending = session.lost = True
    try:
        await session._stop()
        assert classifier.owner is not None
        assert classifier.owner.done()
        assert classifier.recancels == 0
        assert not q.confirmed
        assert not session._forced
        assert not session._forced_detections
    finally:
        await classifier.finish()


async def test_cancelled_typing_waiter_does_not_cancel_shared_embedding(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedding_release = asyncio.Event()

    async def embed(texts: Any) -> list[list[float]]:
        await embedding_release.wait()
        return [[1.0, 0.0]]

    embedder = AsyncMock()
    embedder.embed.side_effect = embed
    monkeypatch.setattr(session.svc, "embedder", embedder)
    shared = session.embedding(TEXT)
    vector = session._vector(TEXT)
    assert shared is not None
    assert vector is not None
    consumer = asyncio.ensure_future(vector())
    detection: asyncio.Future[Detection] = asyncio.get_running_loop().create_future()
    q = held_question(session)
    session._force_taken(q.qid, TEXT, detection, respond=False)
    try:
        assert await session._drain_forced(session._cancel_forced(), within_s=1)
        assert not shared.done()
        assert not consumer.done()
        embedding_release.set()
        assert await consumer == [1.0, 0.0]
        assert session.embedding(TEXT) is shared
        embedder.embed.assert_awaited_once()
    finally:
        embedding_release.set()
        await asyncio.gather(consumer, shared, return_exceptions=True)


async def test_live_typing_still_confirms_once_with_classifier_type(session: Session) -> None:
    q = held_question(session)
    detection: asyncio.Future[Detection] = asyncio.get_running_loop().create_future()
    session._force_taken(q.qid, TEXT, detection, respond=False)
    typing = list(session._forced)
    detection.set_result(Detection("answer", "technical", latency_ms=25, depth="deep"))
    await asyncio.gather(*typing)
    assert q.confirmed
    assert q.qtype == "technical"
    assert q.depth == "deep"
    assert len(session.conv.asked) == 1
    assert session.conv.asked[0].qid == str(q.qid)
    assert await session.writer.drain(within_s=1)
    assert sum(row["kind"] == "question" for row in await stored_log(session)) == 1


async def test_takeover_before_named_wrapper_starts_does_not_orphan_its_call(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    classifier = Classifier()
    monkeypatch.setattr(session.router, "complete", classifier.complete)
    session.info.mode = "standup"
    session.settings.name_address_grace_ms = 60_000
    detection = session.detect(TEXT)
    q = held_question(session)
    session._force_taken(q.qid, TEXT, detection, respond=False)
    session.ending = True
    pending = session._cancel_forced()  # both wrapper and classifier have yet to start
    assert await session._drain_forced(pending, within_s=1)
    assert classifier.calls == 0
    assert not session._forced
    assert not session._forced_detections
    assert not q.confirmed
