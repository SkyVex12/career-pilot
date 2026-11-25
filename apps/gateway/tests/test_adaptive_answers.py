"""Candidate architectures exercised without paid providers or historical-answer leakage."""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMDone, LLMUsage
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

    async def stream(self, job, req, on_fallback):
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
    policy = req.system[0].text
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
    assert versions == ["prior-candidate", "adaptive-v2-grounding"]


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
