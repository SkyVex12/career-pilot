"""Selected technical exception: real request contracts and obsolete task ownership."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import keys, prompts, screen
from copilot_core.ids import uuid7
from copilot_core.llm import LLMDone, LLMUsage
from copilot_core.packs import PreparedAnswer
from copilot_gateway.adaptive import AnswerPlan
from copilot_gateway.answer import AnswerEngine, AnswerHost, MatchResult, Question, ScreenAsk
from copilot_gateway.auth import Principal
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.session import Session, SessionInfo, SessionManager
from copilot_gateway.testing import ScriptedProvider
from copilot_gateway.turn import FIRST, Detection
from copilot_protocol import models

TEXT = "Why do transaction retries need an idempotency key?"


@pytest.fixture
def clean_config_env(monkeypatch):
    for name in (
        "COPILOT_LLM_PROVIDERS",
        "COPILOT_ANSWER_ARCHITECTURE",
        "COPILOT_ADAPTIVE_EXPERT_ROUTE",
        "COPILOT_ADAPTIVE_TECHNICAL_LEGACY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "providers",
    [
        ["openai"],
        ["anthropic"],
        ["bedrock"],
        ["bedrock", "anthropic"],
        ["openai", "anthropic"],
        ["anthropic", "openai"],
        ["openai", "openai"],
    ],
)
def test_defaults_promote_only_evaluated_topology(clean_config_env, providers):
    s = Settings(_env_file=None, llm_providers=providers)  # pyright: ignore[reportCallIssue]
    selected = providers == ["openai"]
    assert s.answer_architecture == ("adaptive" if selected else "legacy_dual")
    assert s.adaptive_technical_legacy is selected
    assert (s.adaptive_expert_route is not None) is selected
    if s.adaptive_expert_route:
        assert s.adaptive_expert_route.model_dump() == {
            "model": "gpt-6.2-sol",
            "reasoning": "low",
            "service_tier": "ultrafast",
            "retry_tier": "fast",
            "first_token_timeout_ms": 20000,
            "hedge_ms": None,
        }


def test_environment_and_constructor_overrides_and_copy_snapshot(clean_config_env, monkeypatch):
    monkeypatch.setenv("COPILOT_LLM_PROVIDERS", "openai")
    monkeypatch.setenv("COPILOT_ANSWER_ARCHITECTURE", "legacy_dual")
    monkeypatch.setenv("COPILOT_ADAPTIVE_EXPERT_ROUTE", "null")
    monkeypatch.setenv("COPILOT_ADAPTIVE_TECHNICAL_LEGACY", "false")
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert (s.answer_architecture, s.adaptive_expert_route, s.adaptive_technical_legacy) == (
        "legacy_dual",
        None,
        False,
    )
    explicit = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        answer_architecture="adaptive",
        adaptive_technical_legacy=True,
        adaptive_expert_route={"model": "explicit-model", "hedge_ms": 900},
    )
    assert explicit.answer_architecture == "adaptive"
    assert explicit.adaptive_technical_legacy
    assert explicit.adaptive_expert_route
    assert explicit.adaptive_expert_route.model == "explicit-model"
    # model_copy is a resolved settings snapshot, not a request to recompute defaults.
    moved = explicit.model_copy(update={"llm_providers": ["anthropic"]})
    assert moved.answer_architecture == "adaptive"
    assert moved.adaptive_expert_route == explicit.adaptive_expert_route
    assert moved.adaptive_technical_legacy


class Provider:
    name = "openai"


def routes(s):
    router = build_router(s, [cast(Any, Provider())])
    return {
        job: [(r.model, r.params, r.first_token_timeout_s, r.refusal_only) for r in values]
        for job, values in router.routes.items()
    }, router.hedge_after_s


def test_explicit_legacy_and_null_profile_keep_baseline_routes(clean_config_env):
    kwargs = dict(
        llm_providers=["openai"],
        openai_fast_model="gpt-6-luna",
        openai_fast_reasoning="none",
        openai_deep_model="gpt-6.1-sol",
        openai_deep_reasoning="low",
        openai_detector_model="gpt-6-luna",
        openai_summary_model="gpt-6-luna",
    )
    legacy = Settings(_env_file=None, answer_architecture="legacy_dual", **kwargs)  # pyright: ignore[reportCallIssue]
    baseline = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        answer_architecture="legacy_dual",
        adaptive_expert_route=None,
        adaptive_technical_legacy=False,
        **kwargs,
    )
    assert legacy.adaptive_expert_route is not None
    assert routes(legacy) == routes(baseline)
    restored = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        answer_architecture="adaptive",
        adaptive_expert_route=None,
        adaptive_technical_legacy=False,
        **kwargs,
    )
    assert routes(restored) == routes(baseline)


class Router:
    def __init__(self, tail=None):
        self.routes = {}
        self.requests = []
        self.tail = tail
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release_old = asyncio.Event()
        self.completed = []

    async def stream(
        self, job, req, on_fallback, *, on_attempt=None
    ) -> AsyncGenerator[str | LLMDone]:
        index = len(self.requests)
        self.requests.append((job, req))
        if index == 0 and self.tail:
            if not self.tail.startswith("blank_"):
                yield "Obsolete buffered text. "
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                await self.release_old.wait()
            if self.tail.endswith("error"):
                raise RuntimeError("authored late dependency failure")
            if self.tail.endswith("delta_done"):
                yield "Obsolete late text. "
            yield LLMDone(LLMUsage(91, 0, 13), "end_turn", "test", "authored-old")
        else:
            yield "Current primary. " if job == "fast" else "Dependent detail. "
            yield LLMDone(LLMUsage(21, 0, 7), "end_turn", "test", "authored-current")
        self.completed.append(index)


def make_engine(settings, tail=None):
    events, logs, writes, memory_calls = [], [], [], []
    router = Router(tail)

    async def memory(text, pass_):
        memory_calls.append((text, pass_))
        return ["[n:ab12] Original verified evidence."], {"memory_hits": 1}

    async def get(key):
        return None

    async def put(key, value):
        writes.append(value)

    host = SimpleNamespace(
        settings=settings,
        router=router,
        emit=lambda cls, **fields: events.append((cls.__name__, fields)),
        log_entry=logs.append,
        store_text=lambda: True,
        call_context=lambda: prompts.CallContext(recent="Original speaker."),
        prompt_context=lambda: prompts.PromptContext("Alex", "interview", "Verified profile", None),
        refs=lambda: {},
        llm_status=lambda ok: None,
        cache_version=lambda: "policy-fixture",
        answer_style=lambda: "bullets",
        response_length=lambda: "default",
        cache_extra=lambda: {},
        memory_lines=memory,
        qa_get=get,
        qa_put=put,
    )
    engine = AnswerEngine(cast(AnswerHost, host))
    return engine, router, events, logs, writes, memory_calls


def question(**changes):
    return Question(
        **{
            "qid": uuid4(),
            "text": TEXT,
            "qtype": "technical",
            "confidence": 1,
            "addressed": True,
            "speculative": False,
            "forced": False,
            "t0": time.monotonic(),
            "detector_ms": 1,
            "match": None,
            **changes,
        }
    )


def configured(settings_factory, **changes):
    return settings_factory(
        **{
            "answer_architecture": "adaptive",
            "adaptive_expert_route": None,
            "adaptive_technical_legacy": True,
            **changes,
        }
    )


@pytest.mark.parametrize("depth", [None, "light"])
async def test_standard_technical_requests_equal_existing_legacy(settings_factory, depth):
    s = configured(settings_factory)
    result = []
    for settings in (s, s.model_copy(update={"answer_architecture": "legacy_dual"})):
        engine, router, events, _logs, _writes, memory_calls = make_engine(settings)
        q = question(depth=depth)
        engine.add(q)
        engine.confirm(q.qid, q.text, 0)
        await engine.wait_idle()
        await asyncio.gather(*engine._background)
        assert engine.strategy(q) == "legacy_dual"
        assert [req.max_tokens for _, req in router.requests] == [250, 800]
        assert "Current primary." in str(router.requests[1][1].content)
        assert len([x for x in events if x[0] == "AnswerDone"]) == 2
        result.append((router.requests, memory_calls))
    assert result[0] == result[1]


@pytest.mark.parametrize(
    ("qtype", "text", "depth", "expected"),
    [
        ("technical", TEXT, "deep", AnswerPlan("expert", "deep", 1200)),
        (
            "technical",
            "Where are you based, and how would you design a distributed notification system?",
            None,
            AnswerPlan("expert", "deep", 1200),
        ),
        (
            "technical",
            "Write a Python function to merge intervals.",
            None,
            AnswerPlan("expert", "deep", 1200),
        ),
        ("coding", "Merge these intervals.", None, AnswerPlan("expert", "deep", 1200)),
        (
            "system_design",
            "Build a notification service.",
            None,
            AnswerPlan("expert", "deep", 1200),
        ),
        ("other", "Where are you based?", None, AnswerPlan("brief", "fast", 100)),
    ],
)
async def test_expert_and_nontechnical_precedence(settings_factory, qtype, text, depth, expected):
    engine, router, _, _, _, _ = make_engine(configured(settings_factory))
    q = question(qtype=qtype, text=text, depth=depth)
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert engine.strategy(q) == "adaptive"
    assert q.plans["fast"] == expected
    assert len(router.requests) == 1
    assert router.requests[0][0] == expected.job


def test_screens_remain_legacy(settings_factory):
    engine, *_ = make_engine(configured(settings_factory))
    q = question(screen=ScreenAsk((), "interview", None, "python"))
    assert engine.strategy(q) == "legacy_dual"
    engine.problem = screen.Problem(uuid4(), (), None, "prior", "", time.monotonic())
    assert engine.strategy(question(qtype="technical")) == "legacy_dual"


@pytest.mark.parametrize(
    "tail",
    ["delta_done", "done_only", "error", "blank_delta_done", "blank_done_only", "blank_error"],
)
async def test_unexposed_replacement_owns_outputs_and_retains_cancelled_usage(
    settings_factory, tail
):
    engine, router, events, logs, writes, _ = make_engine(configured(settings_factory), tail)
    match_gate = asyncio.Event()

    async def matching():
        await match_gate.wait()
        return None

    match = asyncio.create_task(matching())
    old = question(qtype="other", held=True, speculative=True, match=match, early_ms=21)
    engine.add(old)
    await asyncio.wait_for(router.entered.wait(), 1)
    original_snapshot = old.snapshots["fast"]
    if tail.startswith("blank_"):
        engine.release(old.qid)
        assert old.exposed
        assert not old.answer_exposed
    released_at = old.released_at
    old.qtype = "technical"
    engine.host.call_context = lambda: prompts.CallContext(recent="Later unrelated speaker.")
    replacement = engine.reconcile_strategy(old)
    assert replacement is not old
    assert replacement.qid == old.qid
    assert replacement.t0 == old.t0
    assert replacement.early_ms == 21
    assert replacement.match is match
    assert old.match is None
    assert not match.cancelled()
    assert replacement.snapshots["fast"] is original_snapshot
    assert replacement.preferences == old.preferences
    assert replacement.held_text == []
    assert replacement.delta_counts == {}
    assert replacement.fast_text == ""
    assert not replacement.fast_ended
    assert replacement.plans == {}
    assert replacement.logged == set()
    assert replacement.released_at == released_at
    assert not replacement.released.is_set()
    engine.release(replacement.qid)
    engine.confirm(replacement.qid, replacement.text, 0)
    await asyncio.wait_for(router.cancelled.wait(), 1)
    await asyncio.wait_for(replacement.tasks["fast"], 1)
    router.release_old.set()
    match_gate.set()
    await engine.wait_idle()
    await asyncio.gather(match, *engine._background)
    assert [data["text"] for kind, data in events if kind == "AnswerDelta"] == [
        "Current primary. ",
        "Dependent detail. ",
    ]
    assert [data["sequence"] for kind, data in events if kind == "AnswerDelta"] == [0, 0]
    assert not [x for x in events if x[0] in {"QuestionCancelled", "AnswerError"}]
    assert len([x for x in events if x[0] == "AnswerDone"]) == 2
    assert len([x for x in logs if x["kind"] == "answer"]) == 2
    assert all("Obsolete" not in str(x) for x in writes)
    assert len(router.requests) == 3
    assert "Original speaker" in str(router.requests[1][1].content)
    assert "Later unrelated speaker" not in str(router.requests[1][1].content)
    cancelled = [x for x in logs if x["kind"] == "pass_cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["answer_strategy"] == "adaptive"
    if tail.endswith("done_only"):
        assert cancelled[0]["input_tokens"] == 91
        assert cancelled[0]["output_tokens"] == 13


async def test_late_lookup_cannot_dispatch_or_store_after_replacement(settings_factory):
    engine, router, events, logs, _writes, _ = make_engine(configured(settings_factory))
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_memory = engine.host.memory_lines
    calls = 0

    async def memory(text, pass_):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise RuntimeError("obsolete lookup error") from None
        return await old_memory(text, pass_)

    engine.host.memory_lines = memory
    old = question(qtype="other", held=True)
    engine.add(old)
    await asyncio.wait_for(entered.wait(), 1)
    old.qtype = "technical"
    new = engine.reconcile_strategy(old)
    engine.release(new.qid)
    engine.confirm(new.qid, new.text, 0)
    await asyncio.wait_for(cancelled.wait(), 1)
    release.set()
    await engine.wait_idle()
    await asyncio.gather(*engine._background)
    assert len(router.requests) == 2
    assert not [x for x in events if x[0] == "AnswerError"]
    assert len([x for x in logs if x["kind"] == "answer"]) == 2
    assert len([x for x in logs if x["kind"] == "pass_cancelled"]) == 1


@pytest.mark.parametrize("detection_only", [False, True])
async def test_visible_generation_is_stable_after_late_technical_type(
    settings_factory, detection_only
):
    engine, router, events, logs, _, _ = make_engine(configured(settings_factory))
    q = question(qtype="other", held=detection_only)
    engine.add(q)
    frozen = q.plans["fast"]
    q.exposed = True
    q.answer_exposed = True  # A prior answer event, even without readable text, is final.
    q.qtype = "technical"
    assert engine.reconcile_strategy(q) is q
    assert engine.reconcile_strategy(q) is q
    assert engine.strategy(q) == "adaptive"
    assert q.plans["fast"] == frozen
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 1
    assert router.requests[0][1].max_tokens == 400
    assert len([x for x in logs if x["kind"] == "answer_strategy_retained"]) == 1
    assert not [x for x in events if x[0] == "QuestionCancelled"]


@pytest.fixture
async def session(env) -> AsyncIterator[Session]:
    s = configured(env.settings, prewarm_jobs=[], llm_warm_interval_s=None, force_type_wait_ms=100)
    svc = env.services(s, stt_names=())
    manager = SessionManager(svc)
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
    obj = Session(svc, manager, uuid7(), Principal(env.user_a, "Alex", None), info, None, None)
    manager.sessions[obj.sid] = obj
    await svc.store.claim_active(env.user_a, obj.sid)
    await svc.store.take_lease(obj.sid, svc.task_id)
    obj.writer.set_state(**obj.state_fields())
    obj.start_core()
    try:
        yield obj
    finally:
        obj.ending = True
        obj.turns.cancel()
        await obj.answers.shutdown(within_s=1)
        await obj._stop()
        await svc.store.release_lease(obj.sid, svc.task_id)
        await svc.store.release_active(env.user_a, obj.sid)
        await svc.store.client.aclose()


async def wire(session):
    assert await session.writer.drain(within_s=1)
    rows = await session.svc.store.client.xrange(keys.sess_events(session.sid))
    return [json.loads(fields[b"m"]) for _, fields in rows]


async def test_actual_session_initial_legacy_metadata_and_auto_deep(session):
    qid = session.begin_question(
        TEXT, Detection("answer", "technical"), speculative=False, t0=time.monotonic(), match=None
    )
    session.confirm_question(qid, TEXT, 0, 0)
    await session.answers.wait_idle()
    messages = await wire(session)
    detected = [m for m in messages if m["type"] == "question.detected"]
    assert detected[0]["answer_strategy"] == "legacy_dual"
    assert {m["pass"] for m in messages if m["type"] == "answer.done"} == {"fast", "deep"}
    assert session.deep_auto_types() == ["technical"]


async def test_actual_session_held_replacement_release_and_confirmation(session):
    qid = session.start_held(TEXT, time.monotonic(), None)
    old = session.answers.questions[qid]
    session.release_question(qid, TEXT, Detection("answer", "technical"), speculative=False)
    new = session.answers.questions[qid]
    assert new is not old
    assert new.exposed
    assert not new.held
    session.confirm_question(qid, TEXT, 0, 0)
    await session.answers.wait_idle()
    messages = await wire(session)
    assert not [m for m in messages if m["type"] == "question.cancelled"]
    assert [m["answer_strategy"] for m in messages if m["type"] == "question.detected"] == [
        "legacy_dual"
    ]
    assert {m["question_id"] for m in messages if m["type"].startswith("answer.")} == {str(qid)}
    assert {m["pass"] for m in messages if m["type"] == "answer.done"} == {"fast", "deep"}


async def test_forced_takeover_retains_anchor_and_frozen_generation(session):
    buffered = asyncio.Event()
    original_stream = session.svc.router.stream

    async def observed(*args, **kwargs):
        async for chunk in original_stream(*args, **kwargs):
            yield chunk
            if isinstance(chunk, str) and chunk:
                buffered.set()

    session.svc.router.stream = observed
    qid = session.start_held(TEXT, time.monotonic(), None)
    q = session.answers.questions[qid]
    t0 = q.t0
    await asyncio.wait_for(buffered.wait(), 1)
    assert q.held_text
    detection = asyncio.get_running_loop().create_future()
    session._force_taken(qid, TEXT, detection, respond=False)
    detection.set_result(Detection("answer", "technical"))
    await asyncio.gather(*session._forced)
    await session.answers.wait_idle()
    assert session.answers.questions[qid] is q
    assert q.t0 == t0
    assert q.forced
    assert session.answers.strategy(q) == "adaptive"
    assert q.qtype == "technical"
    messages = await wire(session)
    first = next(m for m in messages if m["type"] == "question.detected")
    assert first["confidence"] == 1
    assert first["answer_strategy"] == "adaptive"
    assert len([m for m in messages if m["type"] == "answer.done"]) == 1


async def test_stop_after_hidden_replacement_drains_both_generations(settings_factory):
    engine, router, events, _, _, _ = make_engine(configured(settings_factory), "error")
    old = question(qtype="other", held=True)
    engine.add(old)
    await asyncio.wait_for(router.entered.wait(), 1)
    old.qtype = "technical"
    new = engine.reconcile_strategy(old)
    assert not await engine.shutdown(within_s=0.01)
    router.release_old.set()
    assert await engine.shutdown(within_s=1)
    assert engine._closing
    assert not any(not t.done() for t in engine._tasks)
    assert new.held
    assert not events


@pytest.mark.parametrize("forced", [False, True])
async def test_session_blank_detection_retypes_before_any_answer(session, forced):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def memory(text, pass_):
        calls.append((text, pass_))
        entered.set()
        await release.wait()
        return ["Verified original source."], {"memory_hits": 1}

    session.memory_lines = memory
    # Same call sequence as TurnDetector._first at its 300 ms threshold, with
    # event gates instead of assuming provider/scheduler speed.
    qid = session.start_held(TEXT, time.monotonic() - 0.3, None)
    old = session.answers.questions[qid]
    await asyncio.wait_for(entered.wait(), 1)
    t0 = old.t0
    detection = asyncio.get_running_loop().create_future()
    if forced:
        session._force_taken(qid, TEXT, detection, respond=False)
    else:
        session.release_question(qid, TEXT, FIRST, speculative=True)
        session.retype_question(qid, FIRST)
    initial = await wire(session)
    assert [m["answer_strategy"] for m in initial if m["type"] == "question.detected"] == [
        "adaptive"
    ]
    assert not [m for m in initial if m["type"].startswith("answer.")]
    assert old.exposed
    assert not old.answer_exposed
    released_at = old.released_at
    if forced:
        detection.set_result(Detection("answer", "technical", latency_ms=500))
        await asyncio.gather(*session._forced)
    else:
        session.retype_question(qid, Detection("answer", "technical", latency_ms=500))
        session.confirm_question(qid, TEXT, 500, 300)
    new = session.answers.questions[qid]
    assert new is not old
    assert new.qid == old.qid
    assert old.discarded
    assert new.t0 == t0
    assert new.released_at == released_at
    assert new.first_shown == old.first_shown
    assert new.forced is forced
    assert new.confidence == (1 if forced else old.confidence)
    assert session.answers.strategy(new) == "legacy_dual"
    before_body = await wire(session)
    detections = [m for m in before_body if m["type"] == "question.detected"]
    assert detections[0]["answer_strategy"] == "adaptive"
    assert all(m["answer_strategy"] == "legacy_dual" for m in detections[1:])
    assert not [m for m in before_body if m["type"].startswith("answer.")]
    if forced:
        assert all(m["speculative"] is False for m in detections)
    release.set()
    await session.answers.wait_idle()
    messages = await wire(session)
    assert not [m for m in messages if m["type"] == "question.cancelled"]
    first_output = next(m["seq"] for m in messages if m["type"].startswith("answer."))
    assert detections[1]["seq"] < first_output
    assert {m["pass"] for m in messages if m["type"] == "answer.done"} == {"fast", "deep"}
    assert all(
        m["latency_ms"].get("release_ms") is not None
        for m in messages
        if m["type"] == "answer.done"
    )
    assert {m["question_id"] for m in messages if m["type"].startswith("answer.")} == {str(qid)}


@pytest.mark.parametrize("boundary", ["whitespace", "done", "error", "expand"])
async def test_session_answer_or_expert_boundary_prevents_replacement(session, boundary):
    gate = asyncio.Event()

    async def memory(text, pass_):
        await gate.wait()
        return [], {}

    session.memory_lines = memory
    qid = session.start_held(TEXT, time.monotonic(), None)
    q = session.answers.questions[qid]
    session.release_question(qid, TEXT, FIRST, speculative=False)
    if boundary == "whitespace":
        session.emit(models.AnswerDelta, question_id=qid, **{"pass": "fast"}, text=" ", sequence=0)
    elif boundary == "done":
        session.emit(
            models.AnswerDone,
            question_id=qid,
            **{"pass": "fast"},
            source="model",
            memory_refs=[],
            usage={"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0},
            latency_ms={},
            delta_count=0,
        )
    elif boundary == "error":
        session.emit(models.AnswerError, question_id=qid, **{"pass": "fast"}, code="internal")
    else:
        assert session.answers.expand(qid)
    session.retype_question(qid, Detection("answer", "technical"))
    assert session.answers.questions[qid] is q
    assert session.answers.strategy(q) == "adaptive"
    assert q.answer_exposed is (boundary != "expand")
    assert q.expanded_at is not None if boundary == "expand" else q.expanded_at is None
    messages = await wire(session)
    assert [m["answer_strategy"] for m in messages if m["type"] == "question.detected"] == [
        "adaptive"
    ]
    assert not [m for m in messages if m["type"] == "question.cancelled"]
    assert await session.answers.shutdown(within_s=1)


@pytest.mark.parametrize("held", [False, True])
async def test_legacy_to_adaptive_replacement_only_while_unexposed(settings_factory, held):
    engine, router, events, _, _, _ = make_engine(configured(settings_factory))
    q = question(held=held)
    engine.add(q)
    q.depth = "deep"
    result = engine.reconcile_strategy(q)
    assert (result is not q) is held
    assert engine.strategy(result) == ("adaptive" if held else "legacy_dual")
    engine.release(result.qid)
    engine.confirm(result.qid, result.text, 0)
    await engine.wait_idle()
    assert not [x for x in events if x[0] == "QuestionCancelled"]
    if held:
        assert len(router.requests) == 1
        assert router.requests[0][1].max_tokens == 1200


async def test_replacement_revised_text_keeps_original_context_and_preferences(settings_factory):
    engine, router, _, _, _, _ = make_engine(configured(settings_factory))
    entered, release = asyncio.Event(), asyncio.Event()

    async def memory(text, pass_):
        entered.set()
        await release.wait()
        return ["Original evidence."], {}

    engine.host.memory_lines = memory
    old = question(qtype="other", held=True)
    engine.add(old)
    await asyncio.wait_for(entered.wait(), 1)
    snapshot = old.snapshots["fast"]
    old.text = "Why do transaction retries need an idempotency key after a timeout?"
    old.qtype = "technical"
    engine.host.call_context = lambda: prompts.CallContext(recent="Later unrelated speaker")
    engine.host.prompt_context = lambda: prompts.PromptContext(
        "Alex", "standup", "New profile", None
    )
    engine.host.answer_style = lambda: "paragraph"
    replacement = engine.reconcile_strategy(old)
    new_snapshot = replacement.snapshots["fast"]
    assert new_snapshot.text == old.text
    assert new_snapshot.text != snapshot.text
    assert new_snapshot.context is snapshot.context
    assert new_snapshot.transcript is snapshot.transcript
    engine.release(replacement.qid)
    engine.confirm(replacement.qid, replacement.text, 0)
    release.set()
    await engine.wait_idle()
    request = router.requests[0][1]
    assert old.text in str(request.content)
    assert "Original speaker." in str(request.content)
    assert "Later unrelated speaker" not in str(request.content)
    assert "Verified profile" in str(request.system)
    assert "New profile" not in str(request.system)
    assert replacement.preferences["fast"] == ("bullets", "default")


@pytest.mark.parametrize("change", ["version", "conversation", "suggested"])
async def test_category_legacy_keeps_prepared_freshness_guards(settings_factory, change):
    engine, router, events, _, _, _ = make_engine(configured(settings_factory))
    prepared = PreparedAnswer(
        question=TEXT,
        talking_points=["Previously verified source."],
        grounding_policy="fixture-policy",
        evidence_version="v1",
    )
    version = "v1"
    engine.host.prepared_source_version = lambda policy: version
    engine.host.call_context = lambda: prompts.CallContext(recent="Them: " + TEXT)

    async def match():
        return MatchResult(prepared, 1, 0)

    task = asyncio.create_task(match())
    await task
    q = question(held=True, match=task)
    engine.add(q)
    await asyncio.sleep(0)
    assert not q.held_text
    assert not events
    assert not router.requests
    if change == "version":
        version = "v2"
    elif change == "conversation":
        engine.host.call_context = lambda: prompts.CallContext(
            recent="Me: That migration was cancelled."
        )
    else:
        prepared.talking_points[0] = "[~] Unverified migration story."
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(router.requests) == 2
    assert all(data["source"] == "model" for kind, data in events if kind == "AnswerDone")
    assert all(
        "Previously verified" not in data["text"] for kind, data in events if kind == "AnswerDelta"
    )


async def test_category_cache_requires_current_facts_and_reuses_only_exact_request(
    settings_factory,
):
    engine, router, events, _, _, _ = make_engine(
        configured(settings_factory, deep_pass_auto=False)
    )
    cache = {}
    evidence = "Launch is blocked pending security review."

    async def memory(text, pass_):
        return [evidence], {}

    async def get(qhash):
        return cache.get(qhash)

    async def put(qhash, value):
        cache[qhash] = value

    engine.host.memory_lines, engine.host.qa_get, engine.host.qa_put = memory, get, put
    for index in range(3):
        if index == 1:
            evidence = "Security review approved the launch."
        engine.questions.clear()
        q = question(forced=True)
        engine.add(q)
        engine.confirm(q.qid, q.text, 0)
        await engine.wait_idle()
        await asyncio.gather(*engine._background)
    assert len(router.requests) == 2
    assert "blocked pending security" in str(router.requests[0][1].content)
    assert "approved the launch" in str(router.requests[1][1].content)
    assert [data["source"] for kind, data in events if kind == "AnswerDone"] == [
        "model",
        "model",
        "cache",
    ]


RUST = "In Rust, what happens when a future is dropped while waiting on an async operation?"


@pytest.mark.parametrize("stage", ["held", "shown_empty", "forced_empty"])
async def test_session_late_expert_upgrade_uses_isolated_primary(session, env, stage):
    settings = configured(
        env.settings,
        prewarm_jobs=[],
        llm_warm_interval_s=None,
        force_type_wait_ms=100,
        llm_providers=["openai"],
        openai_detector_model="gpt-6-luna",
        openai_detector_reasoning="none",
        openai_summary_model="gpt-6-luna",
        openai_fast_model="gpt-6-luna",
        openai_fast_reasoning="none",
        openai_deep_model="gpt-6.1-sol",
        openai_deep_reasoning="low",
        adaptive_expert_route={
            "model": "gpt-6.2-sol",
            "reasoning": "low",
            "service_tier": "fast",
            "retry_tier": "fast",
            "first_token_timeout_ms": 20000,
            "hedge_ms": None,
        },
    )
    provider = ScriptedProvider("openai")
    session.settings = session.svc.settings = settings
    session.router = session.svc.router = build_router(settings, [provider])
    entered, release = asyncio.Event(), asyncio.Event()

    async def memory(text, pass_):
        entered.set()
        await release.wait()
        return ["Verified original context."], {"memory_hits": 1}

    session.memory_lines = memory
    qid = session.start_held(RUST, time.monotonic() - 0.3, None)
    old = session.answers.questions[qid]
    await asyncio.wait_for(entered.wait(), 1)
    assert old.plans["fast"] == AnswerPlan("standard", "fast", 400)
    snapshot = old.snapshots["fast"]
    detection = asyncio.get_running_loop().create_future()
    if stage == "forced_empty":
        session._force_taken(qid, RUST, detection, respond=False)
    elif stage == "shown_empty":
        session.release_question(qid, RUST, FIRST, speculative=True)
        session.retype_question(qid, FIRST)
    before = await wire(session)
    assert not [m for m in before if m["type"].startswith("answer.")]
    assert len([m for m in before if m["type"] == "question.detected"]) == (
        0 if stage == "held" else 1
    )
    released_at = old.released_at
    revised = RUST + " Include resource cleanup."
    session.prompt_context = lambda: prompts.PromptContext("Changed", "interview", "Later", None)
    resolved = Detection("answer", "technical", depth="deep", latency_ms=500)
    if stage == "forced_empty":
        detection.set_result(resolved)
        await asyncio.gather(*session._forced)
    elif stage == "held":
        session.release_question(qid, revised, resolved, speculative=False)
    else:
        old.text = revised
        session.retype_question(qid, resolved)
    new = session.answers.questions[qid]
    assert new is not old
    assert new.qid == old.qid
    assert old.discarded
    assert new.plans["fast"] == AnswerPlan("expert", "deep", 1200)
    assert session.answers.strategy(new) == "adaptive"
    assert new.snapshots["fast"].context is snapshot.context
    assert new.snapshots["fast"].transcript is snapshot.transcript
    assert new.snapshots["fast"].text == new.text
    assert new.t0 == old.t0
    assert new.forced == old.forced
    if stage != "held":
        assert new.released_at == released_at
    session.retype_question(qid, resolved)
    assert session.answers.questions[qid] is new  # Already expert: never dispatch twice.
    session.confirm_question(qid, new.text, 500, 300)
    release.set()
    await session.answers.wait_idle()
    assert len(provider.calls) == 1
    assert provider.calls[0][1] == "gpt-6.2-sol"
    request = provider.requests[0][1]
    assert request.max_tokens == 1200
    assert new.text in str(request.content)
    assert "Later" not in str(request.system)
    route = session.router.routes["adaptive_expert"][0]
    assert route.first_token_timeout_s == 20
    assert route.params["reasoning"]["effort"] == "low"
    assert route.params["service_tier"] == "fast"
    after = await wire(session)
    assert not [m for m in after if m["type"] == "question.cancelled"]
    assert {m["pass"] for m in after if m["type"] == "answer.done"} == {"fast"}
    assert {m["question_id"] for m in after if m["type"].startswith("answer.")} == {str(qid)}
    detections = [m for m in after if m["type"] == "question.detected"]
    assert all(m["answer_strategy"] == "adaptive" for m in detections)
    first_output = next(m["seq"] for m in after if m["type"].startswith("answer."))
    assert all(m["seq"] < first_output for m in detections)
    if stage == "forced_empty":
        assert all(m["speculative"] is False for m in detections)


@pytest.mark.parametrize("boundary", ["whitespace", "done", "error", "expand"])
async def test_late_expert_upgrade_preserves_answer_boundary(session, boundary):
    gate = asyncio.Event()

    async def memory(text, pass_):
        await gate.wait()
        return [], {}

    session.memory_lines = memory
    qid = session.start_held(RUST, time.monotonic(), None)
    q = session.answers.questions[qid]
    session.release_question(qid, RUST, FIRST, speculative=False)
    if boundary == "whitespace":
        session.emit(models.AnswerDelta, question_id=qid, **{"pass": "fast"}, text=" ", sequence=0)
    elif boundary == "done":
        session.emit(
            models.AnswerDone,
            question_id=qid,
            **{"pass": "fast"},
            source="model",
            memory_refs=[],
            usage={"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0},
            latency_ms={},
            delta_count=0,
        )
    elif boundary == "error":
        session.emit(models.AnswerError, question_id=qid, **{"pass": "fast"}, code="internal")
    else:
        assert session.answers.expand(qid)
    session.retype_question(qid, Detection("answer", "technical", depth="deep"))
    assert session.answers.questions[qid] is q
    assert q.plans["fast"] == AnswerPlan("standard", "fast", 400)
    assert await session.answers.shutdown(within_s=1)


@pytest.mark.parametrize(
    ("architecture", "text", "initial_type", "initial_depth", "new_type", "new_depth"),
    [
        ("adaptive_hybrid", RUST, "other", None, "technical", "deep"),
        ("adaptive_on_demand", RUST, "other", None, "technical", "deep"),
        ("adaptive", RUST, "technical", "deep", "other", None),
        ("adaptive", "Where are you based?", "other", None, "technical", "deep"),
        (
            "adaptive",
            "Tell me about a time you fixed an outage.",
            "other",
            None,
            "technical",
            "deep",
        ),
    ],
)
async def test_late_expert_upgrade_has_no_other_replanning(
    settings_factory, architecture, text, initial_type, initial_depth, new_type, new_depth
):
    engine, _, _, _, _, _ = make_engine(
        configured(
            settings_factory, answer_architecture=architecture, adaptive_technical_legacy=False
        )
    )
    q = question(text=text, qtype=initial_type, depth=initial_depth, held=True)
    engine.add(q)
    frozen = q.plans["fast"]
    q.qtype, q.depth = new_type, new_depth
    assert engine.reconcile_strategy(q) is q
    assert q.plans["fast"] == frozen
    assert await engine.shutdown(within_s=1)


@pytest.mark.parametrize("tail", ["blank_delta_done", "blank_done_only", "blank_error"])
async def test_late_expert_upgrade_owns_retired_provider_tails(settings_factory, tail):
    engine, router, events, logs, writes, _ = make_engine(configured(settings_factory), tail)
    old = question(text=RUST, qtype="other", held=True)
    engine.add(old)
    await asyncio.wait_for(router.entered.wait(), 1)
    old.qtype, old.depth = "technical", "deep"
    new = engine.reconcile_strategy(old)
    assert new is not old
    assert old.discarded
    engine.release(new.qid)
    engine.confirm(new.qid, new.text, 0)
    await asyncio.wait_for(router.cancelled.wait(), 1)
    router.release_old.set()
    await engine.wait_idle()
    assert [req.max_tokens for _, req in router.requests] == [400, 1200]
    assert not any("Obsolete" in str(fields) for _, fields in events)
    assert len([kind for kind, _ in events if kind == "AnswerDone"]) == 1
    assert not [kind for kind, _ in events if kind in {"AnswerError", "QuestionCancelled"}]
    assert all("Obsolete" not in str(value) for value in writes)
    cancelled = [item for item in logs if item["kind"] == "pass_cancelled"]
    assert len(cancelled) == 1
    if tail == "blank_done_only":
        assert cancelled[0]["input_tokens"] == 91
        assert cancelled[0]["output_tokens"] == 13
    assert await engine.shutdown(within_s=1)
