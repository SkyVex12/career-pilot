"""Generation identity and evidence snapshot regressions; fake providers only."""

import asyncio
import time
from types import MethodType, SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from apps.gateway.tests import test_late_answer_routing as lab_test
from apps.gateway.tests import test_prepared_grounding as prepared_test
from apps.gateway.tests.conftest import Env
from copilot_core import prompts
from copilot_gateway.session import Session
from copilot_gateway.turn import Detection, TurnDetector, TurnHost, _Candidate, _Turn


@pytest.fixture
def env():
    return Env()


async def test_material_final_after_completed_detection(env, monkeypatch, record_property):
    env.stt.final_delay_s = 0.06
    await lab_test.test_final_revision_gets_a_new_question_and_does_not_reuse_old_route_future(
        env, monkeypatch, "frozen", True, record_property
    )


async def test_generation_snapshot_excludes_later_speaker(env, monkeypatch, record_property):
    await lab_test.test_old_question_keeps_generation_start_conversation(
        env, monkeypatch, "frozen", record_property
    )


@pytest.mark.parametrize("style", ["bullets", "paragraph"])
@pytest.mark.parametrize("length", ["default", "concise", "detailed"])
async def test_preferences_remain_bound_to_current_generation(env, monkeypatch, style, length):
    await lab_test.test_ready_route_keeps_initial_preferences_and_single_memory_snapshot(
        env, monkeypatch, style, length
    )


async def test_benign_polite_final_keeps_existing_generation(env, monkeypatch):
    env.stt.final_suffix = " please"
    async with lab_test.live_lab(env, monkeypatch, "frozen") as (lab, client):
        q = await lab_test.start_partial(lab, client)
        lab.detector_release.set()
        await asyncio.wait_for(asyncio.shield(lab.session.turns.early.cand.detection), 2)
        await client.frame(0, 0b10, b"", 100)
        final = await client.wait_type("transcript.final")
        detected = await client.wait_type("question.detected", speculative=False)
        assert detected["question_id"] == str(q.qid)
        assert final["text"].endswith("please")
        lab.memory_release.set()
        lab.cache_release.set()
        lab.provider_release.set()
        await client.wait_type("answer.done", question_id=str(q.qid), pass_="fast")
        await lab.session.answers.wait_idle()
        assert len(lab.decisions) == 1
        assert len(lab.requests) == 1
        assert lab.memory_calls == [(lab_test.QUESTION, "deep")]
        assert lab_test.QUESTION in str(lab.requests[0][1].content)
        assert lab_test.QUESTION + " please" not in str(lab.requests[0][1].content)


@pytest.mark.parametrize("visible", [False, True])
async def test_material_final_replaces_buffered_or_visible_old_generation(
    env, monkeypatch, visible
):
    env.stt.final_suffix = " when the downstream provider has no idempotency API?"
    env.stt.final_delay_s = 0.16 if visible else 0.02
    async with lab_test.live_lab(env, monkeypatch, "frozen") as (lab, client):
        streamed = asyncio.Event()
        provider = env.providers[0]
        original_stream = provider.stream

        async def observe_stream(*args, **kwargs):
            async for chunk in original_stream(*args, **kwargs):
                if not isinstance(chunk, str):
                    streamed.set()
                yield chunk

        monkeypatch.setattr(provider, "stream", observe_stream)
        old = await lab_test.start_partial(lab, client)
        lab.detector_release.set()
        await asyncio.wait_for(asyncio.shield(lab.session.turns.early.cand.detection), 2)
        lab.memory_release.set()
        lab.cache_release.set()
        lab.provider_release.set()
        await asyncio.wait_for(lab.provider_entered.wait(), 2)
        await asyncio.wait_for(streamed.wait(), 2)
        assert old.held_text
        assert not client.of_type("answer.delta")
        await client.frame(0, 0b10, b"", 100)
        if visible:
            await client.wait_type("answer.delta", question_id=str(old.qid))
        final = await client.wait_type("transcript.final")
        q = await client.wait(
            lambda message: (
                message["type"] == "question.detected"
                and not message["speculative"]
                and message["question_id"] != str(old.qid)
            )
        )
        await client.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await lab.session.answers.wait_idle()
        assert len(lab.decisions) == 2
        assert len(lab.requests) == 2
        assert "downstream provider" not in str(lab.requests[0][1].content)
        assert "downstream provider" in str(lab.requests[1][1].content)
        assert lab.memory_calls[-1] == (final["text"], "deep")
        if visible:
            cancelled = next(
                i
                for i, message in enumerate(client.messages)
                if message["type"] == "question.cancelled"
                and message["question_id"] == str(old.qid)
            )
            assert not [
                message
                for message in client.messages[cancelled + 1 :]
                if message["type"] in {"answer.delta", "answer.done"}
                and message["question_id"] == str(old.qid)
            ]
        else:
            assert {m["question_id"] for m in client.of_type("answer.delta")} == {q["question_id"]}


@pytest.mark.parametrize("action", ["discard", "cancel", "supersede"])
async def test_pending_snapshot_does_not_outlive_cancellation(env, monkeypatch, action):
    await lab_test.test_cancelled_question_never_dispatches_from_a_completed_late_result(
        env, monkeypatch, action
    )


async def test_prompt_profile_stays_with_generation_snapshot(settings_factory):
    engine, host, _, _ = prepared_test.engine_for(settings_factory)
    entered, released = asyncio.Event(), asyncio.Event()

    async def memory(text, pass_):
        entered.set()
        await released.wait()
        return [], {}

    host.memory_lines = memory
    q = await prepared_test.matched(None)
    engine.add(q)
    await asyncio.wait_for(entered.wait(), 2)
    host.digest.text = "Blair is based in Austin."
    host.call_context = lambda: prompts.CallContext(recent="Blair owns the unrelated rollout.")
    released.set()
    await engine.wait_idle()
    request = str(host.router.requests[0])
    assert prepared_test.PROFILE in request
    assert "Blair" not in request


async def test_retrieval_names_use_snapshot_but_next_question_gets_new_context(settings_factory):
    entered, released = asyncio.Event(), asyncio.Event()
    searches = []
    recent = "Them: How does Atlas work?"

    async def hits(text, vector, deep, wait):
        entered.set()
        await released.wait()
        return [], {}

    def search(text, want):
        searches.append(text)
        return []

    host = SimpleNamespace(
        settings=settings_factory(answer_architecture="adaptive"),
        company_memory=SimpleNamespace(hits=hits),
        _memory_on=lambda: True,
        _vector=lambda text: None,
        _context_names=lambda: ["Atlas", "Beacon"],
        sections=SimpleNamespace(search=search),
        call_context=lambda: prompts.CallContext(recent=recent),
        conv=SimpleNamespace(context=lambda: prompts.CallContext(recent=recent)),
        pack=None,
        _refs={},
    )
    host.section_lines = MethodType(Session.section_lines, host)
    old = asyncio.create_task(
        Session.memory_lines(cast(Session, host), "How does it retry?", "fast")
    )
    await asyncio.wait_for(entered.wait(), 2)
    recent = "Blair: Beacon uses an unrelated retry policy."
    released.set()
    await old
    assert searches == ["How does it retry?", "How does it retry? Atlas"]
    searches.clear()
    await Session.memory_lines(cast(Session, host), "What about its latency?", "fast")
    assert searches == ["What about its latency?", "What about its latency? Beacon"]


async def test_material_revision_withdraws_card_shown_before_detector():
    original = "How do you handle retries safely?"
    corrected = original + " Without an idempotency key?"
    old_id, new_id = uuid4(), uuid4()
    lifecycle = []

    async def detect(text):
        return Detection("answer", "technical", depth="deep")

    def hold(text, t0, match):
        lifecycle.append(("held", text))
        return new_id

    host = SimpleNamespace(
        detect=detect,
        start_match=lambda text: None,
        start_held=hold,
        cancel_question=lambda qid: lifecycle.append(("cancel", qid)),
        discard_question=lambda qid: lifecycle.append(("discard", qid)),
    )
    detector = TurnDetector(
        cast(TurnHost, host),
        silence_timer_ms=100,
        speculative_start_ms=50,
        final_wait_max_ms=100,
        keep_added_words=4,
        strict_extensions=True,
    )
    future = asyncio.get_running_loop().create_future()
    future.set_result(Detection("answer", "technical"))
    candidate = _Candidate(original, future, None)
    turn = _Turn(
        t0=time.monotonic(),
        text=corrected,
        cand=candidate,
        first_qid=old_id,
        speculative_qid=old_id,
    )
    turn.flushed.set()
    newest, detection = await detector._decide(turn, candidate)
    assert newest.text == corrected
    assert detection.fires()
    assert lifecycle == [("cancel", old_id), ("held", corrected)]
    assert turn.first_qid is None
    assert turn.speculative_qid is None
    assert turn.held_qid == new_id
