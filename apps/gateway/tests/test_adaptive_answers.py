"""Candidate architectures exercised without paid providers or historical-answer leakage."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMDone, LLMUsage, ModelRouter, Route
from copilot_gateway.adaptive import SentenceBoundary, plan_answer
from copilot_gateway.answer import AnswerEngine, AnswerHost, Question, ScreenAsk
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)


class RecordingRouter:
    def __init__(self) -> None:
        self.requests: list[tuple[str, Any]] = []
        self.routes: dict[str, Any] = {}
        self.primary_gate: asyncio.Event | None = None
        self.primary_started = asyncio.Event()
        self.expert_started = asyncio.Event()
        self.fail = False

    async def stream(
        self, job, req, on_fallback, *, on_attempt=None
    ) -> AsyncGenerator[str | LLMDone]:
        self.requests.append((job, req))
        if self.fail:
            raise RuntimeError("provider stopped")
        expert = "independent expert response" in str(req.content)
        if expert:
            self.expert_started.set()
            yield "Use the verified incident record. "
        else:
            self.primary_started.set()
            yield "I chose Redis for low latency. "
            if self.primary_gate is not None:
                await self.primary_gate.wait()
            yield "The cache can be rebuilt from the source."
        yield LLMDone(LLMUsage(100, 20, 15), "end_turn", "test", "gpt-6-luna")


def make_engine(settings):
    events: list[tuple[str, dict]] = []
    logs: list[dict] = []
    cache: dict[str, dict] = {}
    router = RecordingRouter()

    async def memory(text, pass_):
        return ["[n:ab12] Verified source: Redis cached read results."], {"memory_hits": 1}

    async def get(key):
        return cache.get(key)

    async def put(key, value):
        cache[key] = value

    host = SimpleNamespace(
        settings=settings,
        router=router,
        emit=lambda cls, **fields: events.append((cls.__name__, fields)),
        log_entry=logs.append,
        store_text=lambda: True,
        call_context=lambda: prompts.CallContext(recent="Them: Please explain the decision."),
        prompt_context=lambda: prompts.PromptContext("Alex", "interview", "Verified profile", None),
        refs=lambda: {},
        llm_status=lambda ok: None,
        cache_version=lambda: "test-v1",
        answer_style=lambda: "paragraph",
        response_length=lambda: "default",
        cache_extra=lambda: {},
        memory_lines=memory,
        qa_get=get,
        qa_put=put,
    )
    return AnswerEngine(cast(AnswerHost, host)), router, events, logs, cache


def question(text="Why did you choose Redis?", qtype="technical", **extra):
    fields: dict[str, Any] = dict(
        qid=uuid4(),
        text=text,
        qtype=qtype,
        confidence=1,
        addressed=True,
        speculative=False,
        forced=False,
        t0=time.monotonic(),
        detector_ms=1,
        match=None,
    )
    fields.update(extra)
    return Question(**fields)


@pytest.mark.parametrize("strategy", ["adaptive", "adaptive_hybrid", "adaptive_on_demand"])
async def test_simple_question_is_one_bounded_grounded_stream(settings_factory, strategy):
    engine, router, events, logs, _ = make_engine(settings_factory(answer_architecture=strategy))
    q = question("Where are you based?", "other")
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 1
    job, req = router.requests[0]
    assert job == "fast"
    assert req.max_tokens == 100
    assert "<shown_cues>" not in str(req.content)
    assert "Never invent personal actions" in req.system[0].text
    assert "suggest specific details that are probably true" not in req.system[0].text
    deltas = [data for kind, data in events if kind == "AnswerDelta"]
    done = next(data for kind, data in events if kind == "AnswerDone")
    assert [d["sequence"] for d in deltas] == list(range(len(deltas)))
    assert done["delta_count"] == len(deltas)
    assert 0 <= done["latency_ms"]["first_complete_sentence_ms"] <= done["latency_ms"]["total_ms"]
    assert next(row for row in logs if row["kind"] == "answer")["answer_strategy"] == strategy


async def test_single_adaptive_routes_complex_question_directly(settings_factory):
    engine, router, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    q = question("Design a distributed notification system?", "system_design", depth="deep")
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 1
    assert router.requests[0][0] == "deep"
    assert router.requests[0][1].max_tokens == 1200
    assert set(q.tasks) == {"fast"}


async def test_hybrid_expert_is_independent_and_does_not_wait_for_primary(settings_factory):
    engine, router, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive_hybrid"))
    router.primary_gate = asyncio.Event()
    q = question("Design a distributed notification system?", "system_design", depth="deep")
    q.fast_text = "WRONG PRIMARY SAYS WE USED KAFKA"
    engine.add(q)
    await asyncio.wait_for(router.primary_started.wait(), 1)
    engine.confirm(q.qid, q.text, 0)
    await asyncio.wait_for(router.expert_started.wait(), 1)
    assert not q.fast_ended
    expert = router.requests[1][1]
    assert "WRONG PRIMARY" not in str(expert.content)
    assert "<shown_cues>" not in str(expert.content)
    assert q.text in str(expert.content)
    assert "Verified source" in str(expert.content)
    router.primary_gate.set()
    await engine.wait_idle()
    assert len(router.requests) == 2


async def test_on_demand_expert_is_idempotent_and_independent(settings_factory):
    engine, router, _, _, _ = make_engine(
        settings_factory(answer_architecture="adaptive_on_demand")
    )
    q = question("Describe a production incident", "behavioral")
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 1
    assert engine.expand(q.qid)
    assert engine.expand(q.qid)
    await engine.wait_idle()
    assert len(router.requests) == 2
    assert q.fast_text not in str(router.requests[-1][1].content)


async def test_superseded_stream_cannot_emit_or_restart_expert(settings_factory):
    engine, router, events, logs, _ = make_engine(
        settings_factory(answer_architecture="adaptive_on_demand")
    )
    router.primary_gate = asyncio.Event()
    old = question()
    engine.add(old)
    engine.confirm(old.qid, old.text, 0)
    await asyncio.wait_for(router.primary_started.wait(), 1)
    newer = question("Where are you based?", "other")
    engine.add(newer)
    engine.confirm(newer.qid, newer.text, 0)
    count = len([d for kind, d in events if kind == "AnswerDelta" and d["question_id"] == old.qid])
    router.primary_gate.set()
    await engine.wait_idle()
    assert (
        len([d for kind, d in events if kind == "AnswerDelta" and d["question_id"] == old.qid])
        == count
    )
    assert not engine.expand(old.qid)
    assert any(row["kind"] == "pass_cancelled" for row in logs)


async def test_held_stream_is_sequence_counted_only_when_released(settings_factory):
    engine, _, events, _, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    q = question(held=True)
    engine.add(q)
    await asyncio.sleep(0.01)
    assert not events
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    deltas = [d for kind, d in events if kind == "AnswerDelta"]
    assert len(deltas) == 1
    assert deltas[0]["sequence"] == 0
    done = next(d for kind, d in events if kind == "AnswerDone")
    assert done["delta_count"] == 1
    assert done["latency_ms"]["first_complete_sentence_ms"] >= done["latency_ms"]["release_ms"]


def test_candidate_cache_does_not_read_legacy_fragment_or_leak_current_answer(settings_factory):
    engine, _, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    q = question()
    q.fast_text = "UNSUPPORTED_CURRENT_ANSWER"
    req = engine._request(q, "deep")
    assert q.fast_text not in str(req.content)
    assert "independent expert response" in str(req.content)
    assert (
        engine.strategy(question(screen=ScreenAsk((), "interview", None, "Python")))
        == "legacy_dual"
    )


@pytest.mark.parametrize(
    ("text", "qtype", "depth", "expected"),
    [
        ("Where are you based?", "other", "light", "brief"),
        ("Tell me about yourself", "other", "light", "narrative"),
        ("Why did you choose Redis?", "technical", "light", "standard"),
        ("Describe a production incident", "behavioral", "light", "narrative"),
        ("Design a distributed notification system", "system_design", "deep", "expert"),
        ("Thanks, explain the trade-offs in the retry design", "technical", "deep", "expert"),
    ],
)
def test_depth_uses_existing_detection_without_model(text, qtype, depth, expected):
    assert plan_answer(text, qtype, depth, "adaptive").depth == expected


def test_sentence_boundary_split_decimal_filler_and_abbreviation():
    boundary = SentenceBoundary()
    assert not boundary.feed("That's a great question. ")
    assert not boundary.feed("Dr. Jones measured 3.")
    assert not boundary.feed("5 seconds")
    assert not boundary.feed(".")
    assert boundary.feed(" Next")
    assert not boundary.feed(" sentence.")
    final = SentenceBoundary()
    assert not final.feed("I'm based in Chicago.")
    assert final.feed("", final=True)


async def test_real_gateway_candidate_strategy_and_single_completion(env):
    def reply(job, req):
        if req.system[0].text.startswith("You help Alex answer live"):
            return Reply(["I would verify the source. ", "Then check the cache."])
        return default_reply(job, req)

    env.providers = [ScriptedProvider("anthropic", reply=reply)]
    async with GatewayServer(env.services(env.settings(answer_architecture="adaptive"))) as gw:
        client = ProtocolClient(gw.url)
        ready = await client.start()
        assert ready["deep_auto_types"] == []
        await client.say("Where are you based?")
        detected = await client.wait_type("question.detected", speculative=False)
        done = await client.wait_type("answer.done", pass_="fast")
        assert detected["answer_strategy"] == "adaptive"
        deltas = [
            m for m in client.of_type("answer.delta") if m["question_id"] == done["question_id"]
        ]
        assert done["delta_count"] == len(deltas)
        assert "".join(d["text"] for d in deltas) == (
            "I would verify the source. Then check the cache."
        )
        assert "first_complete_sentence_ms" in done["latency_ms"]
        session = next(iter(gw.manager.sessions.values()))
        await session.answers.wait_idle()
        assert not any(m["pass"] == "deep" for m in client.of_type("answer.done"))


async def test_stalled_partial_stream_times_out_without_retry_or_late_chunks(settings_factory):
    engine, router, events, logs, _ = make_engine(
        settings_factory(
            answer_architecture="adaptive",
            adaptive_stream_idle_timeout_s=0.02,
        )
    )
    router.primary_gate = asyncio.Event()
    q = question()
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await asyncio.wait_for(engine.wait_idle(), 1)
    assert len(router.requests) == 1
    deltas = [d for kind, d in events if kind == "AnswerDelta"]
    assert len(deltas) == 1
    assert deltas[0]["text"] == "I chose Redis for low latency. "
    assert any(kind == "AnswerError" and d["code"] == "timeout" for kind, d in events)
    assert not any(kind == "AnswerDone" for kind, _ in events)
    assert any(row["kind"] == "pass_cancelled" and row["reason"] == "timeout" for row in logs)
    router.primary_gate.set()
    await asyncio.sleep(0)
    assert len([d for kind, d in events if kind == "AnswerDelta"]) == 1


async def test_candidate_context_sources_are_fetched_concurrently(settings_factory):
    from copilot_gateway.session import Session

    hits_started, sections_started = asyncio.Event(), asyncio.Event()

    async def hits(text, vector, deep, wait):
        hits_started.set()
        await sections_started.wait()
        return [], {"memory_wait_ms": 0}

    async def sections(text, deep):
        sections_started.set()
        await hits_started.wait()
        return ["Verified source section"]

    host = SimpleNamespace(
        settings=settings_factory(answer_architecture="adaptive"),
        company_memory=SimpleNamespace(hits=hits),
        _memory_on=lambda: True,
        _vector=lambda text: None,
        section_lines=sections,
        pack=None,
        _refs={},
    )
    lines, _ = await asyncio.wait_for(
        Session.memory_lines(cast(Session, host), "Explain the incident", "deep"), 1
    )
    assert lines == ["Verified source section"]


@pytest.mark.parametrize(
    "suffix", [" without passwords?", " not Kafka?", " in production?", " Redis?"]
)
def test_candidate_partial_extensions_invalidate_unseen_constraints(suffix):
    from copilot_gateway.turn import TurnDetector

    detector = TurnDetector(
        cast(Any, object()),
        silence_timer_ms=250,
        speculative_start_ms=100,
        final_wait_max_ms=500,
        keep_added_words=3,
        strict_extensions=True,
    )
    assert not detector._extends(
        "How would you configure this", "How would you configure this" + suffix
    )
    assert detector._extends("How would you configure this", "How would you configure this?")
    assert detector._extends("How would you configure this", "How would you configure this please?")
    detector.strict_extensions = False
    assert detector._extends(
        "How would you configure this", "How would you configure this" + suffix
    )


async def test_adaptive_final_revision_restarts_held_generation(env):
    env.stt.final_suffix = " without passwords?"
    env.stt.final_delay_s = 0.05
    requests = []

    def reply(job, req):
        if req.system[0].text.startswith("You help Alex answer live"):
            requests.append(str(req.content))
            return Reply(["Use a verified authentication flow. "], first_delay_s=0.2)
        return default_reply(job, req)

    env.providers = [ScriptedProvider("anthropic", reply=reply)]
    async with GatewayServer(env.services(env.settings(answer_architecture="adaptive"))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("How do you authenticate")
        q = await client.wait_type("question.detected", speculative=False)
        await client.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        assert len(requests) == 2
        assert "without passwords" in requests[1]
        assert len({m["question_id"] for m in client.of_type("answer.delta")}) == 1
        session = next(iter(gw.manager.sessions.values()))
        assert session.turns.held_kept == 0


async def test_adaptive_cache_ignores_legacy_fragments(settings_factory):
    from copilot_gateway.answer import question_hash

    engine, router, _, _, cache = make_engine(settings_factory(answer_architecture="adaptive"))
    q = question()
    cache[question_hash(q.text)] = {
        "v": "test-v1",
        "fast": {"text": "LEGACY_FRAGMENT", "refs": []},
    }
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 1
    assert "LEGACY_FRAGMENT" not in q.fast_text
    await asyncio.gather(*engine._background)
    assert any(
        key.startswith("adaptive:standard:fast:fast:") for key in cache[question_hash(q.text)]
    )


async def test_wait_idle_includes_dependent_legacy_deep_pass(settings_factory):
    engine, router, _, _, _ = make_engine(settings_factory())
    q = question()
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 2
    assert all(task.done() for task in q.tasks.values())


async def test_hybrid_late_classification_starts_independent_expert(settings_factory):
    engine, router, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive_hybrid"))
    router.primary_gate = asyncio.Event()
    q = question("How do partitions rebalance?", "other", held=True)
    engine.add(q)
    await asyncio.wait_for(router.primary_started.wait(), 1)
    assert q.plans["fast"].job == "fast"
    assert q.plans["fast"].depth == "standard"
    q.qtype, q.depth = "technical", "deep"
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    await asyncio.wait_for(router.expert_started.wait(), 1)
    assert not q.fast_ended
    expert = router.requests[-1][1]
    assert "I chose Redis for low latency" not in str(expert.content)
    assert q.text in str(expert.content)
    router.primary_gate.set()
    await engine.wait_idle()
    assert len(router.requests) == 2


async def test_single_stream_records_late_depth_without_restarting_visible_answer(settings_factory):
    engine, router, _, logs, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    router.primary_gate = asyncio.Event()
    q = question("How do partitions rebalance?", "other", held=True)
    engine.add(q)
    await asyncio.wait_for(router.primary_started.wait(), 1)
    q.qtype, q.depth = "technical", "deep"
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    router.primary_gate.set()
    await engine.wait_idle()
    assert len(router.requests) == 1
    row = next(row for row in logs if row["kind"] == "answer")
    assert row["latency_ms"]["request_job"] == "fast"
    assert row["latency_ms"]["detector_depth_at_start"] is None
    assert row["latency_ms"]["detector_depth_at_completion"] == "deep"


async def test_hybrid_honors_explicit_automatic_depth_disable(settings_factory):
    engine, router, _, _, _ = make_engine(
        settings_factory(
            answer_architecture="adaptive_hybrid",
            deep_pass_auto=False,
        )
    )
    q = question("Design a distributed notification system", "system_design", depth="deep")
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 1
    assert engine.expand(q.qid)
    await engine.wait_idle()
    assert len(router.requests) == 2


async def test_candidate_cache_identity_changes_with_conversation_and_evidence(settings_factory):
    engine, router, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    for context, evidence in [
        ("First topic", "Source A"),
        ("Second topic", "Source A"),
        ("Second topic", "Source B"),
    ]:
        engine.questions.clear()
        engine.host.call_context = lambda value=context: prompts.CallContext(recent=value)
        engine.host.prompt_context = lambda value=evidence: prompts.PromptContext(
            "Alex", "interview", value, None
        )
        q = question("Why did you choose that design?", forced=True)
        engine.add(q)
        engine.confirm(q.qid, q.text, 0)
        await engine.wait_idle()
        await asyncio.gather(*engine._background)
    assert len(router.requests) == 3


async def test_candidate_rejects_speculative_prepared_cues_but_baseline_is_unchanged(
    settings_factory,
):
    from copilot_core.packs import PreparedAnswer
    from copilot_gateway.answer import MatchResult

    prepared = PreparedAnswer(
        question="Tell me about a time", talking_points=["[~] I led an invented incident."]
    )

    async def matched():
        return MatchResult(prepared, 1.0, 0)

    for strategy in ["legacy_dual", "adaptive"]:
        engine, _, _, _, _ = make_engine(settings_factory(answer_architecture=strategy))
        q = question()
        q.match = asyncio.create_task(matched())
        await q.match
        assert (engine._prepared(q) is None) == (strategy == "adaptive")


@pytest.mark.parametrize(
    ("text", "depth"),
    [
        ("Please design a distributed notification system", "light"),
        ("Explain quorum safety under partitions", "deep"),
    ],
)
def test_complex_requests_classified_as_respond_keep_expert_reasoning(text, depth):
    plan = plan_answer(text, "respond", depth, "adaptive_hybrid")
    assert plan.depth == "expert"
    assert plan.auto_expert
    assert (
        plan_answer("Thanks, send the numbers by Friday", "respond", "light", "adaptive").depth
        == "brief"
    )


@pytest.mark.parametrize("strategy", ["legacy_dual", "adaptive"])
def test_default_length_keeps_prompt_and_request_budget_identical(settings_factory, strategy):
    engine, _, _, _, _ = make_engine(settings_factory(answer_architecture=strategy))
    q = question()
    for pass_ in ["fast", "deep"]:
        baseline = engine._base_request(q, pass_, ["Verified fact"])
        assert engine._request(q, pass_, ["Verified fact"]) == baseline
        assert prompts.with_response_length(baseline, "default") is baseline


@pytest.mark.parametrize("strategy", ["legacy_dual", "adaptive"])
@pytest.mark.parametrize("length", ["concise", "detailed"])
async def test_response_length_changes_only_next_generation_and_cache(
    settings_factory, strategy, length
):
    from copilot_gateway.answer import question_hash

    engine, router, _, logs, cache = make_engine(
        settings_factory(answer_architecture=strategy, deep_pass_auto=False)
    )
    first = question(forced=True)
    engine.add(first)
    engine.confirm(first.qid, first.text, 0)
    await engine.wait_idle()
    await asyncio.gather(*engine._background)
    initial = router.requests[0][1]
    engine.questions.clear()  # Keep evidence identical, so only length changes the cache identity.
    engine.host.response_length = lambda: length
    second = question(forced=True)
    engine.add(second)
    engine.confirm(second.qid, second.text, 0)
    await engine.wait_idle()
    await asyncio.gather(*engine._background)
    assert len(router.requests) == 2
    req = router.requests[-1][1]
    assert initial.content == req.content
    assert initial.system == req.system[:-1]
    assert f"preference: {length}." in req.system[-1].text
    if length == "concise":
        assert req.max_tokens < initial.max_tokens
    elif strategy == "adaptive":
        assert req.max_tokens > initial.max_tokens
    assert any(f"length={length}" in slot for slot in cache[question_hash(second.text)])
    answers = [row for row in logs if row["kind"] == "answer"]
    assert answers[0]["latency_ms"]["response_length"] == "default"
    assert answers[1]["latency_ms"]["response_length"] == length
    assert first.preferences["fast"][1] == "default"


async def test_preference_is_frozen_before_async_retrieval(settings_factory):
    engine, router, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    retrieval_started, release = asyncio.Event(), asyncio.Event()

    async def memory(text, pass_):
        retrieval_started.set()
        await release.wait()
        return [], {}

    engine.host.memory_lines = memory
    engine.host.response_length = lambda: "concise"
    q = question()
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await retrieval_started.wait()
    engine.host.response_length = lambda: "detailed"
    engine.host.answer_style = lambda: "bullets"
    release.set()
    await engine.wait_idle()
    req = router.requests[0][1]
    assert "preference: concise." in req.system[-1].text
    assert "Use connected paragraphs." in str(req.content)
    assert q.preferences["fast"] == ("paragraph", "concise")


async def test_response_length_session_start_update_and_resume(env):
    settings = env.settings(answer_architecture="adaptive")
    async with GatewayServer(env.services(settings)) as first_gateway:
        client = ProtocolClient(first_gateway.url)
        await client.start(answer_style="paragraph", response_length="concise")
        session = next(iter(first_gateway.manager.sessions.values()))
        assert session.response_length() == "concise"
        assert session.state_fields()["response_length"] == "concise"
        await client.send({"type": "answer_style.set", "answer_style": "bullets"})
        await client.say("Where are you based?")
        await client.wait_type("answer.done", pass_="fast")
        assert session.response_length() == "concise"  # Older clients omit the new field.
        await client.send(
            {"type": "answer_style.set", "answer_style": "paragraph", "response_length": "detailed"}
        )
        await client.say("What is your notice period?", capture_ms=5000)
        await client.wait(
            lambda event: event["type"] == "question.detected" and "notice" in event["text"]
        )
        await session.answers.wait_idle()
        assert session.response_length() == "detailed"
        assert session.state_fields()["response_length"] == "detailed"
        await client.ws.close()
    async with GatewayServer(env.services(settings)) as second_gateway:
        resumed = ProtocolClient(second_gateway.url)
        resumed.session_id = client.session_id
        resumed.last_seq = client.last_seq
        await resumed.resume()
        await resumed.wait_type("session.language")
        restored = next(iter(second_gateway.manager.sessions.values()))
        assert restored.answer_style() == "paragraph"
        assert restored.response_length() == "detailed"


@pytest.mark.parametrize("strategy", ["adaptive", "adaptive_hybrid", "adaptive_on_demand"])
@pytest.mark.parametrize("pass_", ["fast", "deep"])
def test_candidate_grounding_contract_preserves_missing_facts_and_task_provenance(
    settings_factory, strategy, pass_
):
    engine, _, _, _, _ = make_engine(settings_factory(answer_architecture=strategy))
    engine.host.prompt_context = lambda: prompts.PromptContext(
        "Alex", "interview", "[n:ab12] Alex implemented payments reconciliation at LedgerCo.", None
    )
    engine.host.call_context = lambda: prompts.CallContext(
        recent=(
            "Jamie: I checked retry logging on staging today.\n"
            "Alex: The carrier report preview is still in development."
        )
    )
    q = question("Have you worked with an EMR such as Epic?", "technical")
    q.fast_text = "UNSUPPORTED: I have never worked with an EMR."
    evidence = [
        "[n:cd34] Jamie owns retry logging; staging checks passed today.",
        "[n:ef56] Alex owns the carrier report preview; "
        "still in development; no validation recorded.",
    ]
    req = engine._request(q, pass_, evidence)
    policy = " ".join(req.system[0].text.split())
    assert "Missing evidence never establishes either experience or lack of experience." in policy
    assert "unsupported positive or negative personal experience" in policy
    assert "keep that private note separate from the speakable answer" in policy
    assert "supported adjacent experience and a clearly hypothetical approach" in policy
    assert "bind every claim to its own project, task, speaker, time and environment" in policy
    assert (
        "Do not transfer ownership, implementation status, tests, deployments or outcomes" in policy
    )
    assert (
        "A validation result for one task does not establish "
        "that another task was validated in the same environment" in policy
    )
    assert "Epic" not in policy
    assert "carrier report preview" not in policy
    content = str(req.content)
    assert q.text in content
    assert "\n".join(evidence) in content  # Source boundaries and attribution remain intact.
    assert "Jamie: I checked retry logging" in content
    assert "Alex: The carrier report preview is still in development." in content
    assert "UNSUPPORTED:" not in content
    # Baseline compatibility: candidate-specific correction is not inserted into A.
    assert "Missing evidence never establishes" not in prompts.FAST_SYSTEM
    assert "Preserve source provenance" not in prompts.DEEP_SYSTEM


async def test_candidate_prompt_revision_invalidates_prior_cached_answer(
    settings_factory, monkeypatch
):
    engine, router, _, logs, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    for revision in ["prior-candidate", prompts.ADAPTIVE_PROMPT_VERSION]:
        monkeypatch.setattr(prompts, "ADAPTIVE_PROMPT_VERSION", revision)
        engine.questions.clear()
        q = question("Have you worked with an EMR such as Epic?", "technical", forced=True)
        engine.add(q)
        engine.confirm(q.qid, q.text, 0)
        await engine.wait_idle()
        await asyncio.gather(*engine._background)
    assert len(router.requests) == 2
    versions = [row["latency_ms"]["prompt_version"] for row in logs if row["kind"] == "answer"]
    assert versions == ["prior-candidate", "adaptive-v3-direct-opening"]


@pytest.mark.parametrize("style", ["bullets", "paragraph"])
@pytest.mark.parametrize("pass_", ["fast", "deep"])
def test_candidate_requests_require_the_selected_answer_format(settings_factory, style, pass_):
    engine, _, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive_hybrid"))
    engine.host.answer_style = lambda: style
    req = engine._request(question("Where are you based?", "other"), pass_)
    if style == "bullets":
        assert "Write the answer as complete-sentence bullets." in str(req.content)
        assert "Use one bullet for a brief answer" in str(req.content)
        assert "Use connected paragraphs." not in str(req.content)
    else:
        assert "Use connected paragraphs." in str(req.content)
        assert "Write the answer as complete-sentence bullets." not in str(req.content)


async def test_candidate_exact_cache_uses_current_retrieved_evidence(settings_factory):
    engine, router, _, logs, cache = make_engine(settings_factory(answer_architecture="adaptive"))
    evidence = ["[n:ab12] Carrier report preview is in development."]
    retrievals = 0

    async def memory(text, pass_):
        nonlocal retrievals
        retrievals += 1
        return list(evidence), {"memory_hits": len(evidence)}

    engine.host.memory_lines = memory
    for next_evidence in [
        "[n:ab12] Carrier report preview is in development.",
        "[n:ab12] Carrier report preview is in development.",
        "[n:ab12] Carrier report preview passed staging validation today.",
    ]:
        evidence[:] = [next_evidence]
        engine.questions.clear()  # Same digest, pack, question, and call context throughout.
        q = question("What is the carrier report preview status?", "status_follow_up", forced=True)
        engine.add(q)
        engine.confirm(q.qid, q.text, 0)
        await engine.wait_idle()
        await asyncio.gather(*engine._background)
    assert retrievals == 3  # Exactly once per generation, including a cache hit.
    assert len(router.requests) == 2
    answers = [entry for entry in logs if entry["kind"] == "answer"]
    assert [entry["source"] for entry in answers] == ["model", "cache", "model"]
    assert "in development" in str(router.requests[0][1].content)
    assert "passed staging validation today" in str(router.requests[1][1].content)
    assert "in development" not in str(router.requests[1][1].content)
    assert all("Carrier report" not in key for value in cache.values() for key in value)


async def test_candidate_request_stays_bound_to_context_hashed_before_cache_read(settings_factory):
    engine, router, _, _, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    lookup_started, release = asyncio.Event(), asyncio.Event()

    async def get(qhash):
        lookup_started.set()
        await release.wait()
        return None

    engine.host.qa_get = get
    engine.host.call_context = lambda: prompts.CallContext(recent="Alex: Original context.")
    q = question(forced=True)
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await lookup_started.wait()
    engine.host.call_context = lambda: prompts.CallContext(recent="Alex: Later context.")
    release.set()
    await engine.wait_idle()
    assert "Original context" in str(router.requests[0][1].content)
    assert "Later context" not in str(router.requests[0][1].content)


@pytest.mark.parametrize("strategy", ["legacy_dual", "adaptive"])
async def test_candidate_question_preload_refreshes_completed_memory_and_shares_inflight(
    settings_factory, monkeypatch, strategy
):
    from copilot_gateway.retrieval import CompanyMemory
    from copilot_gateway.session import Session

    retrieval = CompanyMemory(
        cast(Any, None), uuid4(), uuid4(), embedding_model=None, budget_ms=100, limit=5
    )
    release = asyncio.Event()
    calls = 0

    async def query(text, vector, name, started):
        nonlocal calls
        calls += 1
        await release.wait()
        return []

    monkeypatch.setattr(retrieval, "_query", query)
    host = cast(
        Session,
        SimpleNamespace(
            settings=settings_factory(answer_architecture=strategy),
            company_memory=retrieval,
            _memory_on=lambda: True,
            _vector=lambda text: None,
            spoken_language="en",
            matcher=SimpleNamespace(enabled=False),
        ),
    )
    Session.start_match(host, "What is the carrier report status?")
    first = retrieval.lookups["What is the carrier report status?"]
    Session.start_match(host, "What is the carrier report status?")
    assert retrieval.lookups["What is the carrier report status?"] is first
    release.set()
    await asyncio.gather(first.fts, first.full)
    Session.start_match(host, "What is the carrier report status?")
    second = retrieval.lookups["What is the carrier report status?"]
    assert (second is first) == (strategy == "legacy_dual")
    await asyncio.gather(second.fts, second.full)
    assert calls == (1 if strategy == "legacy_dual" else 2)
    await retrieval.hits("What is the carrier report status?", None, True, 100)
    assert retrieval.lookups["What is the carrier report status?"] is second
    retrieval.close()


async def test_stored_timing_separates_preparation_first_chunk_and_sentence(settings_factory):
    engine, _, events, logs, _ = make_engine(settings_factory(answer_architecture="adaptive"))

    async def memory(text, pass_):
        await asyncio.sleep(0.012)
        return ["[n:ab12] Redis cached reads."], {"memory_hits": 1}

    async def cache(qhash):
        await asyncio.sleep(0.012)
        return None

    provider = ScriptedProvider(
        "openai",
        lambda job, req: Reply(
            ["I chose Redis", " for low latency. ", "It cached reads."],
            first_delay_s=0.012,
            chunk_delay_s=0.012,
        ),
    )
    engine.host.memory_lines = memory
    engine.host.qa_get = cache
    engine.host.router = ModelRouter({"fast": [Route(provider, "gpt-6-luna")]}, 1)
    q = question(forced=True)
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    answer = next(row for row in logs if row["kind"] == "answer")
    lat = answer["latency_ms"]
    for field in (
        "context_wait_ms",
        "cache_lookup_ms",
        "router_first_chunk_ms",
        "stream_sentence_ms",
    ):
        assert lat[field] >= 8
    assert lat["answer_prepare_ms"] >= lat["context_wait_ms"] + lat["cache_lookup_ms"] - 2
    assert lat["first_complete_sentence_ms"] >= lat["model_dispatch_ms"]
    wire = next(fields for name, fields in events if name == "AnswerDone")
    assert "context_wait_ms" not in wire["latency_ms"]
    attempts = [row for row in logs if row["kind"] == "provider_attempt"]
    assert [row["status"] for row in attempts] == ["started", "first_chunk", "completed"]
    assert {row["question_id"] for row in attempts} == {str(q.qid)}
    assert len({row["attempt_id"] for row in attempts}) == 1
    assert all("content" not in row and "question" not in row for row in attempts)
    assert attempts[-1]["usage_observed"] is True
    assert attempts[-1]["cost_usd"] == answer["cost_usd"]


async def test_cancelled_attempt_has_no_invented_usage_or_cost(settings_factory):
    engine, _, _, logs, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    started = asyncio.Event()

    def reply(job, req):
        started.set()
        return Reply(["An answer."], first_delay_s=30)

    provider = ScriptedProvider("openai", reply)
    engine.host.router = ModelRouter({"fast": [Route(provider, "gpt-6-luna")]}, 60)
    q = question(forced=True)
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await started.wait()
    engine.discard(q.qid)
    await asyncio.gather(*q.tasks.values(), return_exceptions=True)
    terminals = [
        row
        for row in logs
        if row["kind"] == "provider_attempt" and row["status"] in {"closed", "cancelled", "failed"}
    ]
    assert len(terminals) == 1
    assert terminals[0]["error_category"] == "caller_cancelled"
    assert terminals[0]["usage_observed"] is False
    assert terminals[0]["usage"] is None
    assert "cost_usd" not in terminals[0]


async def test_shutdown_awaits_discarded_pass_cleanup_without_recancelling(settings_factory):
    engine, _, _, logs, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowCloseRouter(RecordingRouter):
        async def stream(self, job, req, on_fallback, *, on_attempt=None):
            self.primary_started.set()
            try:
                yield "An initial answer. "
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                closed.set()

    router = SlowCloseRouter()
    engine.host.router = cast(ModelRouter, router)
    q = question(forced=True)
    engine.add(q)
    await router.primary_started.wait()
    engine.discard(q.qid)
    await cleaning.wait()
    draining = asyncio.create_task(engine.shutdown())
    await asyncio.sleep(0)
    assert not draining.done()
    assert q.qid not in engine.questions
    release.set()
    assert await draining is True
    assert closed.is_set()
    assert all(task.done() for task in q.tasks.values())
    assert any(row["kind"] == "pass_cancelled" for row in logs)
    next_q = question()
    engine.add(next_q)
    assert next_q.tasks == {}  # No dependent pass can restart during shutdown.


async def test_shutdown_deadline_reports_unfinished_cleanup(settings_factory):
    engine, _, _, _, _ = make_engine(settings_factory())
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def cleanup():
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    task = asyncio.create_task(cleanup())
    engine._background.add(task)
    await asyncio.sleep(0)
    task.cancel()
    await cleaning.wait()
    assert await engine.shutdown(within_s=0.01) is False
    assert not task.done()
    release.set()
    await asyncio.gather(task, return_exceptions=True)


class RecoveryStatusError(Exception):
    def __init__(self, status: int):
        super().__init__("Synthetic typed HTTP failure")
        self.status_code = status


def recovery_engine(settings_factory, *, primary=None, expert=None, **settings):
    settings.setdefault("answer_architecture", "adaptive_on_demand")
    settings.setdefault("adaptive_failure_recovery", True)
    settings.setdefault("deep_pass_auto", False)
    engine, _, events, logs, cache = make_engine(settings_factory(**settings))
    primary = primary or ScriptedProvider(
        "openai", lambda job, req: Reply([], error=TimeoutError())
    )
    expert = expert or ScriptedProvider(
        "openai", lambda job, req: Reply(["A grounded answer. ", "Supporting detail."])
    )
    engine.host.router = ModelRouter(
        {
            "fast": [Route(primary, "gpt-6-luna")],
            "deep": [Route(expert, "gpt-6.2-sol", {"reasoning": {"effort": "low"}})],
            "answer_recovery": [Route(expert, "gpt-6.2-sol", {"reasoning": {"effort": "low"}})],
        },
        1,
    )
    return engine, primary, expert, events, logs, cache


@pytest.mark.parametrize("strategy", ["adaptive", "adaptive_on_demand"])
async def test_failure_recovery_is_one_frozen_request_in_same_visible_pass(
    settings_factory, strategy
):
    primary = ScriptedProvider(
        "openai", lambda job, req: Reply([], first_delay_s=0.015, error=TimeoutError())
    )
    engine, primary, expert, events, logs, cache = recovery_engine(
        settings_factory, primary=primary, answer_architecture=strategy
    )
    # Exhaust every primary route before the one independent expert attempt.
    cast(dict[str, list[Route]], engine.host.router.routes)["fast"] = [
        Route(primary, "gpt-6-luna"),
        Route(primary, "gpt-6-luna"),
    ]
    q = question(forced=True)
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    await asyncio.gather(*engine._background)
    assert len(primary.calls) == 2
    assert len(expert.calls) == 1
    assert expert.requests[0][1] is primary.requests[0][1]
    assert expert.requests[0][1].max_tokens == primary.requests[0][1].max_tokens
    assert "<shown_cues>" not in str(expert.requests[0][1].content)
    assert cache == {}
    assert set(q.tasks) == {"fast"}
    assert not any(kind == "AnswerError" for kind, _ in events)
    deltas = [row for kind, row in events if kind == "AnswerDelta"]
    assert [row["sequence"] for row in deltas] == list(range(len(deltas)))
    done = [row for kind, row in events if kind == "AnswerDone"]
    assert len(done) == 1
    assert done[0]["pass"] == "fast"
    answer = next(row for row in logs if row["kind"] == "answer")
    latency = answer["latency_ms"]
    assert latency["failure_recovery"] is True
    assert latency["request_job"] == "answer_recovery"
    assert latency["configured_reasoning"] == {"reasoning": {"effort": "low"}}
    assert latency["failure_recovery_start_ms"] >= 20
    assert latency["model_dispatch_ms"] < latency["failure_recovery_start_ms"]
    assert latency["router_first_chunk_ms"] >= 20
    assert latency["first_complete_sentence_ms"] >= latency["failure_recovery_start_ms"]
    attempts = [row for row in logs if row["kind"] == "provider_attempt"]
    failed = [row for row in attempts if row["status"] == "failed"]
    assert len(failed) == 2
    assert all(row["usage"] is None for row in failed)
    completed = [row for row in attempts if row["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["job"] == "answer_recovery"
    assert completed[0]["failure_recovery"] is True
    assert completed[0]["usage_observed"] is True


async def test_failed_recovery_has_one_terminal_error_and_no_extra_routes(settings_factory):
    expert = ScriptedProvider("openai", lambda job, req: Reply([], error=TimeoutError()))
    engine, primary, expert, events, logs, _ = recovery_engine(settings_factory, expert=expert)
    q = question()
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(primary.calls) == len(expert.calls) == 1
    assert [row["code"] for kind, row in events if kind == "AnswerError"] == ["llm_unavailable"]
    assert not any(kind in {"AnswerDelta", "AnswerDone"} for kind, _ in events)
    error = next(row for row in logs if row["kind"] == "answer_error")
    assert error["failure_recovery"] is True
    assert error["failure_recovery_job"] == "answer_recovery"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 422, 429, 501])
async def test_http_failure_outside_transient_allowlist_never_recovers(settings_factory, status):
    primary = ScriptedProvider(
        "openai", lambda job, req: Reply([], error=RecoveryStatusError(status))
    )
    engine, primary, expert, events, _, _ = recovery_engine(settings_factory, primary=primary)
    q = question()
    engine.add(q)
    await engine.wait_idle()
    assert len(primary.calls) == 1
    assert expert.calls == []
    assert [row["code"] for kind, row in events if kind == "AnswerError"] == ["llm_unavailable"]


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
async def test_typed_transient_server_failure_can_recover(settings_factory, status):
    primary = ScriptedProvider(
        "openai", lambda job, req: Reply([], error=RecoveryStatusError(status))
    )
    engine, _, expert, events, _, _ = recovery_engine(settings_factory, primary=primary)
    engine.add(question())
    await engine.wait_idle()
    assert len(expert.calls) == 1
    assert sum(kind == "AnswerDone" for kind, _ in events) == 1


@pytest.mark.parametrize("mixed", [False, True])
async def test_refusal_including_mixed_refusal_and_timeout_never_recovers(settings_factory, mixed):
    refusal = ScriptedProvider("openai", lambda job, req: Reply([], stop_reason="refusal"))
    engine, primary, expert, events, logs, _ = recovery_engine(settings_factory, primary=refusal)
    transient = ScriptedProvider("openai", lambda job, req: Reply([], error=TimeoutError()))
    if mixed:
        cast(dict[str, list[Route]], engine.host.router.routes)["fast"] = [
            Route(primary, "gpt-6-luna"),
            Route(transient, "gpt-6-sol"),
        ]
    engine.add(question())
    await engine.wait_idle()
    assert expert.calls == []
    assert sum(kind == "AnswerError" for kind, _ in events) == 1
    assert any(row["status"] == "completed" for row in logs if row["kind"] == "provider_attempt")
    if mixed:
        assert len(transient.calls) == 1


@pytest.mark.parametrize("case", ["flag_off", "unknown", "no_routes", "multiple_recovery_routes"])
async def test_unknown_unconfigured_or_disabled_recovery_never_calls_expert(settings_factory, case):
    engine, _, expert, events, _, _ = recovery_engine(
        settings_factory, adaptive_failure_recovery=case != "flag_off"
    )
    if case == "unknown":
        cast(ScriptedProvider, engine.host.router.routes["fast"][0].provider).reply = (
            lambda job, req: Reply([], error=RuntimeError("Unknown provider failure"))
        )
    elif case == "no_routes":
        cast(dict[str, list[Route]], engine.host.router.routes)["fast"] = []
    elif case == "multiple_recovery_routes":
        cast(dict[str, list[Route]], engine.host.router.routes)["answer_recovery"] *= 2
    engine.add(question())
    await engine.wait_idle()
    assert expert.calls == []
    assert sum(kind == "AnswerError" for kind, _ in events) == 1


@pytest.mark.parametrize("text", ["A partial answer. ", "[n:"])
@pytest.mark.parametrize("held", [False, True])
async def test_partial_or_citation_buffered_output_never_recovers(settings_factory, text, held):
    from copilot_core.llm import AllRoutesFailedError

    failed = asyncio.Event()

    class PartialProvider(ScriptedProvider):
        async def stream(self, model, req, params):
            yield text
            failed.set()
            raise AllRoutesFailedError("fast", ["synthetic stream failure"])

    engine, _, expert, events, _, _ = recovery_engine(
        settings_factory, primary=PartialProvider("openai")
    )
    q = question(held=held)
    engine.add(q)
    await failed.wait()
    if held:
        assert not events
        engine.release(q.qid)
    await engine.wait_idle()
    assert expert.calls == []
    assert sum(kind == "AnswerError" for kind, _ in events) == 1


@pytest.mark.parametrize("strategy", ["legacy_dual", "adaptive_hybrid"])
async def test_other_architectures_do_not_gain_failure_recovery(settings_factory, strategy):
    engine, _, expert, _, _, _ = recovery_engine(settings_factory, answer_architecture=strategy)
    q = question()
    engine.questions[q.qid] = q
    engine.start_pass(q, "fast")  # Unconfirmed: no unrelated automatic legacy/hybrid deep pass.
    await engine.wait_idle()
    assert expert.calls == []


async def test_adaptive_direct_expert_plan_does_not_retry_as_recovery(settings_factory):
    engine, primary, expert, _, _, _ = recovery_engine(
        settings_factory, answer_architecture="adaptive"
    )
    cast(dict[str, list[Route]], engine.host.router.routes)["deep"] = [
        Route(primary, "gpt-6.2-sol")
    ]
    engine.add(question("Design a distributed notification system", "system_design", depth="deep"))
    await engine.wait_idle()
    assert len(primary.calls) == 1
    assert expert.calls == []


@pytest.mark.parametrize("action", ["cancel", "discard", "supersede", "shutdown"])
async def test_cancellation_during_recovery_never_publishes_stale_answer(settings_factory, action):
    entered = asyncio.Event()

    def reply(job, req):
        entered.set()
        return Reply(["Must never be displayed."], first_delay_s=30)

    engine, primary, expert, events, logs, cache = recovery_engine(
        settings_factory, expert=ScriptedProvider("openai", reply)
    )
    q = question(held=action == "discard")
    engine.add(q)
    await asyncio.wait_for(entered.wait(), 1)
    if action == "cancel":
        engine.cancel_speculative(q.qid)
    elif action == "discard":
        engine.discard(q.qid)
    elif action == "supersede":
        engine.supersede(keep=None)
    else:
        assert await engine.shutdown()
    await engine.wait_idle()
    assert len(primary.calls) == len(expert.calls) == 1
    assert expert.cancelled == 1
    assert not any(kind in {"AnswerDelta", "AnswerDone"} for kind, _ in events)
    assert cache == {}
    cancelled = next(row for row in logs if row["kind"] == "pass_cancelled")
    assert cancelled["failure_recovery"] is True
    assert cancelled["request_job"] == "answer_recovery"
    assert all(task.done() for task in q.tasks.values())


async def test_held_recovery_releases_one_monotonic_stream(settings_factory):
    engine, _, expert, events, logs, _ = recovery_engine(settings_factory)
    q = question(held=True)
    engine.add(q)
    await asyncio.sleep(0.02)
    assert len(expert.calls) == 1
    assert q.held_text
    assert not events
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    deltas = [row for kind, row in events if kind == "AnswerDelta"]
    assert len(deltas) == 1
    assert deltas[0]["sequence"] == 0
    done = next(row for kind, row in events if kind == "AnswerDone")
    assert done["delta_count"] == 1
    assert done["latency_ms"]["first_complete_sentence_ms"] >= done["latency_ms"]["release_ms"]
    assert next(row for row in logs if row["kind"] == "answer")["latency_ms"]["failure_recovery"]


async def test_recovery_uses_remaining_total_budget_not_a_new_deadline(
    settings_factory, monkeypatch
):
    import copilot_gateway.answer as answer_module
    from copilot_gateway.adaptive import bounded_chunks as original_bound

    budgets = []

    def record_bound(chunks, idle_s, total_s):
        budgets.append((idle_s, total_s))
        return original_bound(chunks, idle_s=idle_s, total_s=total_s)

    monkeypatch.setattr(answer_module, "bounded_chunks", record_bound)
    primary = ScriptedProvider(
        "openai", lambda job, req: Reply([], first_delay_s=0.1, error=TimeoutError())
    )
    expert = ScriptedProvider("openai", lambda job, req: Reply(["Too late."], first_delay_s=5))
    engine, _, expert, events, logs, _ = recovery_engine(
        settings_factory,
        primary=primary,
        expert=expert,
        adaptive_stream_total_timeout_s=0.5,
        adaptive_stream_idle_timeout_s=1,
    )
    engine.add(question())
    await engine.wait_idle()
    assert len(expert.calls) == 1
    assert expert.cancelled == 1
    assert budgets == [(1, 0.5)]  # One outer budget spans primary and recovery.
    assert [row["code"] for kind, row in events if kind == "AnswerError"] == ["timeout"]
    assert not any(kind in {"AnswerDelta", "AnswerDone"} for kind, _ in events)
    assert next(row for row in logs if row["kind"] == "pass_cancelled")["failure_recovery"]


async def test_primary_first_token_timeout_can_recover_with_remaining_budget(settings_factory):
    primary = ScriptedProvider(
        "openai", lambda job, req: Reply(["Never received."], first_delay_s=30)
    )
    engine, _, expert, events, logs, _ = recovery_engine(settings_factory, primary=primary)
    cast(dict[str, list[Route]], engine.host.router.routes)["fast"] = [
        Route(primary, "gpt-6-luna", first_token_timeout_s=0.01)
    ]
    engine.add(question())
    await engine.wait_idle()
    assert primary.cancelled == 1
    assert len(expert.calls) == 1
    assert sum(kind == "AnswerDone" for kind, _ in events) == 1
    recovery = next(row for row in logs if row["kind"] == "answer_recovery")
    assert recovery["failure_recovery_categories"] == ["first_token_timeout"]


def test_recovery_routes_are_off_by_default_and_only_first_deep_route(settings_factory):
    from copilot_gateway.router import build_router

    providers = [ScriptedProvider("openai"), ScriptedProvider("anthropic")]
    model_settings = {
        "openai_detector_model": "gpt-6-luna",
        "openai_fast_model": "gpt-6-luna",
        "openai_deep_model": "gpt-6.2-sol",
    }
    off = build_router(settings_factory(**model_settings), providers)
    assert "answer_recovery" not in off.routes
    enabled = build_router(
        settings_factory(adaptive_failure_recovery=True, **model_settings), providers
    )
    assert enabled.routes["answer_recovery"] == enabled.routes["deep"][:1]
    assert len(enabled.routes["answer_recovery"]) == 1
    assert "answer_recovery" not in enabled.hedge_after_s
    assert len(enabled.routes["deep"]) > 1


def test_recovery_does_not_opt_into_server_side_refusal_fallback(settings_factory):
    from copilot_gateway.router import build_router

    router = build_router(
        settings_factory(adaptive_failure_recovery=True), [ScriptedProvider("anthropic")]
    )
    primary = router.routes["deep"][0]
    recovery = router.routes["answer_recovery"][0]
    assert primary.params["extra_body"]["fallbacks"] == "default"
    assert "fallbacks" not in recovery.params.get("extra_body", {})
    assert "server-side-fallback" not in str(recovery.params.get("extra_headers", {}))
    assert recovery.model == primary.model
    assert recovery.params["thinking"] == primary.params["thinking"]
    assert recovery.params["output_config"] == primary.params["output_config"]


@pytest.mark.parametrize(
    "case",
    [
        "first_chunk",
        "completed",
        "cancelled",
        "closed",
        "missing_terminal",
        "duplicate_terminal",
        "unknown_category",
        "untyped_connection",
        "stream_failure",
        "mixed_auth",
        "mixed_rate_limit",
    ],
)
def test_recovery_eligibility_requires_complete_transient_attempt_evidence(case):
    from dataclasses import replace

    from copilot_core.llm import AttemptEvent
    from copilot_gateway.answer import _transient_before_text

    started = AttemptEvent("r", "a", "fast", 0, "openai", "m", "started", "first_chunk", 0, 0)
    failed = replace(started, status="failed", error_category="read_timeout")
    events = [started, failed]
    assert _transient_before_text(events)
    if case in {"first_chunk", "completed", "cancelled", "closed"}:
        events.insert(1, replace(started, status=case))
    elif case == "missing_terminal":
        events.pop()
    elif case == "duplicate_terminal":
        events.append(failed)
    elif case == "unknown_category":
        events[-1] = replace(failed, error_category="provider_error")
    elif case == "untyped_connection":
        events[-1] = replace(failed, error_category="connection_error")
    elif case == "stream_failure":
        events[-1] = replace(failed, stage="stream")
    else:
        status = 401 if case == "mixed_auth" else 429
        other = replace(started, attempt_id="b", attempt=1)
        events = [
            other,
            replace(other, status="failed", http_status=status, error_category="http_error"),
            *events,
        ]
    assert not _transient_before_text(events)


@pytest.mark.parametrize(
    ("cause_name", "eligible"),
    [
        ("SSLZeroReturnError", True),
        ("ConnectionResetError", True),
        ("ReadError", True),
        ("WriteError", True),
        ("RemoteProtocolError", True),
        ("SSLError", False),
        ("SSLCertVerificationError", False),
        (None, False),
    ],
)
async def test_connection_recovery_requires_an_explicit_abrupt_transport_cause(
    settings_factory, cause_name, eligible
):
    import ssl

    import httpx2
    from openai import APIConnectionError

    causes = {
        "SSLZeroReturnError": ssl.SSLZeroReturnError,
        "ConnectionResetError": ConnectionResetError,
        "ReadError": httpx2.ReadError,
        "WriteError": httpx2.WriteError,
        "RemoteProtocolError": httpx2.RemoteProtocolError,
        "SSLError": ssl.SSLError,
        "SSLCertVerificationError": ssl.SSLCertVerificationError,
    }
    failure = APIConnectionError(request=httpx2.Request("GET", "https://fixture.invalid"))
    if cause_name is not None:
        failure.__cause__ = causes[cause_name]("Synthetic transport cause")
    primary = ScriptedProvider("openai", lambda job, req: Reply([], error=failure))
    engine, _, expert, events, logs, _ = recovery_engine(settings_factory, primary=primary)
    engine.add(question())
    await engine.wait_idle()
    assert len(expert.calls) == int(eligible)
    assert sum(kind == "AnswerDone" for kind, _ in events) == int(eligible)
    assert sum(kind == "AnswerError" for kind, _ in events) == int(not eligible)
    failed = next(
        row
        for row in logs
        if row["kind"] == "provider_attempt" and row["job"] == "fast" and row["status"] == "failed"
    )
    assert failed["error_category"] == "connection_error"
    if cause_name is not None:
        assert cause_name in failed["cause_types"]


@pytest.mark.parametrize("category", ["connection_error", "connect_error", "read_timeout"])
@pytest.mark.parametrize("certificate", ["SSLCertVerificationError", "CertificateError"])
def test_certificate_verification_vetoes_otherwise_eligible_transport_failure(
    category, certificate
):
    from dataclasses import replace

    from copilot_core.llm import AttemptEvent
    from copilot_gateway.answer import _transient_before_text

    started = AttemptEvent("r", "a", "fast", 0, "openai", "m", "started", "first_chunk", 0, 0)
    failed = replace(
        started,
        status="failed",
        error_category=category,
        cause_types=("APIConnectionError", "SSLZeroReturnError", certificate),
    )
    assert not _transient_before_text([started, failed])


@pytest.mark.parametrize(("start_type", "start_depth"), [("other", None), ("behavioral", "light")])
@pytest.mark.parametrize(("held", "streaming"), [(True, True), (True, False), (False, True)])
async def test_cancellation_records_frozen_and_latest_classification_without_text(
    settings_factory, start_type, start_depth, held, streaming
):
    engine, router, events, logs, _ = make_engine(
        settings_factory(answer_architecture="adaptive_on_demand")
    )
    engine.host.store_text = lambda: False
    if streaming:
        router.primary_gate = asyncio.Event()
    q = question("PRIVATE_QUESTION_CONTENT", start_type, depth=start_depth, held=held)
    engine.add(q)
    await asyncio.wait_for(router.primary_started.wait(), 1)
    q.qtype, q.depth = "technical", "deep"
    if held:
        engine.discard(q.qid)
    else:
        engine.cancel_speculative(q.qid)
    await engine.wait_idle()
    [entry] = [row for row in logs if row["kind"] == "pass_cancelled"]
    assert entry["question_type_at_start"] == start_type
    assert entry["detector_depth_at_start"] == start_depth
    assert entry["question_type_at_cancellation"] == "technical"
    assert entry["detector_depth_at_cancellation"] == "deep"
    assert entry["model_started"] is True
    assert entry["request_job"] == "fast"
    assert len(router.requests) == 1
    assert not any(kind == "AnswerDone" for kind, _ in events)
    if held:
        assert events == []
    if not streaming:
        assert entry["output_tokens"] > 0  # Completed provider output remained held.
    assert "PRIVATE_QUESTION_CONTENT" not in json.dumps(logs)
    assert "I chose Redis" not in json.dumps(logs)
    assert "text" not in entry


async def test_cancellation_before_request_keeps_unknown_routing_depth(settings_factory):
    engine, router, _, logs, _ = make_engine(settings_factory(answer_architecture="adaptive"))
    entered = asyncio.Event()

    async def memory(text, pass_):
        entered.set()
        await asyncio.Event().wait()
        return [], {}

    engine.host.memory_lines = memory
    q = question(qtype="other", held=True)
    engine.add(q)
    await asyncio.wait_for(entered.wait(), 1)
    q.qtype = "technical"
    engine.discard(q.qid)
    await engine.wait_idle()
    [entry] = [row for row in logs if row["kind"] == "pass_cancelled"]
    assert entry["model_started"] is False
    assert entry["question_type_at_start"] == "other"
    assert entry["question_type_at_cancellation"] == "technical"
    assert entry["detector_depth_at_start"] is None
    assert entry["detector_depth_at_cancellation"] is None
    assert router.requests == []
