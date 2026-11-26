"""Adaptive primary evidence uses existing bounded deep retrieval, with no extra model pass."""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.llm import ModelRouter, Route
from copilot_core.workplace import Hit
from copilot_gateway.answer import AnswerEngine, AnswerHost, Question, ScreenAsk
from copilot_gateway.retrieval import CompanyMemory
from copilot_gateway.session import Session
from copilot_gateway.testing import Reply, ScriptedProvider

ARCHITECTURES = ("adaptive", "adaptive_hybrid", "adaptive_on_demand")


def make_engine(settings):
    provider = ScriptedProvider(reply=lambda job, req: Reply(["The current approval is pending."]))
    router = ModelRouter(
        {job: [Route(provider, job)] for job in ("fast", "deep", "deep_light", "screen")}, 1
    )
    events, logs, cache, memory_calls = [], [], {}, []
    evidence = ["[n:ab12] Authored current source: approval is pending."]

    async def memory(text, pass_):
        memory_calls.append((text, pass_))
        return list(evidence), {}

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
        call_context=lambda: prompts.CallContext(recent="Them: Original question context."),
        prompt_context=lambda: prompts.PromptContext("Alex", "interview", "Authored profile", None),
        refs=lambda: {},
        llm_status=lambda ok: None,
        cache_version=lambda: "unchanged-profile",
        answer_style=lambda: "bullets",
        response_length=lambda: "default",
        cache_extra=lambda: {},
        memory_lines=memory,
        qa_get=get,
        qa_put=put,
    )
    return AnswerEngine(cast(AnswerHost, host)), provider, events, logs, memory_calls, evidence


def question(text="Why did you choose Redis?", qtype="technical", **extra):
    return Question(
        qid=uuid4(),
        text=text,
        qtype=qtype,
        confidence=1,
        addressed=True,
        speculative=False,
        forced=True,
        t0=time.monotonic(),
        detector_ms=1,
        match=None,
        **extra,
    )


async def generate(engine, q):
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await asyncio.wait_for(engine.wait_idle(), 1)
    await asyncio.gather(*engine._background)


@pytest.mark.parametrize("architecture", ARCHITECTURES)
@pytest.mark.parametrize(
    ("text", "qtype", "depth"),
    [
        ("Where are you based?", "other", "brief"),
        ("Why did you choose Redis?", "technical", "standard"),
        ("Describe a production incident", "behavioral", "narrative"),
        ("Design a distributed notification system", "system_design", "expert"),
    ],
)
async def test_all_adaptive_depths_use_rich_evidence_without_changing_routes(
    settings_factory, architecture, text, qtype, depth
):
    engine, provider, _, _, calls, _ = make_engine(
        settings_factory(answer_architecture=architecture)
    )
    q = question(text, qtype)
    try:
        await generate(engine, q)
        assert q.plans["fast"].depth == depth
        expected_calls = 2 if architecture == "adaptive_hybrid" and depth == "expert" else 1
        assert len(provider.requests) == len(calls) == expected_calls
        assert calls == [(text, "deep")] * expected_calls
        expected_job = "deep" if architecture == "adaptive" and depth == "expert" else "fast"
        assert provider.calls[0][1] == expected_job
        assert all("Authored current source" in str(req.content) for _, req in provider.requests)
        assert set(q.tasks) == ({"fast", "deep"} if expected_calls == 2 else {"fast"})
    finally:
        assert await engine.shutdown(within_s=1)


@pytest.mark.parametrize("architecture", ARCHITECTURES)
async def test_empty_deep_evidence_is_one_model_request_not_a_fallback(
    settings_factory, architecture
):
    engine, provider, _, _, calls, evidence = make_engine(
        settings_factory(answer_architecture=architecture)
    )
    evidence.clear()
    try:
        await generate(engine, question())
        assert calls == [("Why did you choose Redis?", "deep")]
        assert len(provider.requests) == 1
        assert provider.calls[0][1] == "fast"
    finally:
        assert await engine.shutdown(within_s=1)


async def test_current_rich_evidence_still_keys_exact_cache_and_freezes_context(settings_factory):
    engine, provider, _, logs, calls, evidence = make_engine(
        settings_factory(answer_architecture="adaptive")
    )
    try:
        for source in (
            "Original approval pending",
            "Original approval pending",
            "Approval granted",
        ):
            evidence[:] = ["[n:ab12] " + source]
            engine.questions.clear()
            await generate(engine, question())
        assert [row["source"] for row in logs if row["kind"] == "answer"] == [
            "model",
            "cache",
            "model",
        ]
        assert len(provider.requests) == 2
        assert len(calls) == 3
        assert all(pass_ == "deep" for _, pass_ in calls)
        assert "Approval granted" in str(provider.requests[-1][1].content)
        assert "Original approval pending" not in str(provider.requests[-1][1].content)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_cache(qhash):
            entered.set()
            await release.wait()
            return None

        engine.host.qa_get = blocked_cache
        engine.questions.clear()
        q = question()
        engine.add(q)
        await asyncio.wait_for(entered.wait(), 1)
        engine.host.call_context = lambda: prompts.CallContext(recent="Them: Later context.")
        release.set()
        await asyncio.wait_for(engine.wait_idle(), 1)
        assert "Original question context" in str(provider.requests[-1][1].content)
        assert "Later context" not in str(provider.requests[-1][1].content)
    finally:
        assert await engine.shutdown(within_s=1)


async def test_real_session_deep_wait_and_hit_limit_reach_brief_primary(settings_factory):
    settings = settings_factory(
        answer_architecture="adaptive", memory_deep_wait_ms=35, memory_fast_hits=4
    )
    engine, provider, _, _, _, _ = make_engine(settings)
    hits_started, sections_started = asyncio.Event(), asyncio.Event()
    seen = []
    hits = [Hit(uuid4(), "work_item", f"Authored task {i}", None, 1) for i in range(5)]
    hits.append(Hit(uuid4(), "work_item", "Material sixth-item approval gate", None, 1))

    async def lookup(text, vector, deep, wait):
        seen.append((deep, wait))
        hits_started.set()
        if deep:
            await sections_started.wait()
        return hits, {}

    async def sections(text, deep, *, call):
        sections_started.set()
        if deep:
            await hits_started.wait()
        return ["Full section qualifier"] if deep else []

    session = cast(
        Session,
        SimpleNamespace(
            settings=settings,
            company_memory=SimpleNamespace(hits=lookup),
            _memory_on=lambda: True,
            _vector=lambda text: None,
            section_lines=sections,
            call_context=engine.host.call_context,
            pack=None,
            _refs={},
        ),
    )
    engine.host.memory_lines = lambda text, pass_: Session.memory_lines(session, text, pass_)
    try:
        await generate(engine, question("Where are you based?", "other"))
        assert seen == [(True, 35)]
        assert len(provider.requests) == 1
        content = str(provider.requests[0][1].content)
        assert "Material sixth-item approval gate" in content
        assert "Full section qualifier" in content
    finally:
        assert await engine.shutdown(within_s=1)


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_shared_lookup_is_bounded_and_survives_answer_cancellation(
    settings_factory, monkeypatch, cancel
):
    settings = settings_factory(
        answer_architecture="adaptive", memory_deep_wait_ms=5000 if cancel else 10
    )
    engine, provider, events, _, _, _ = make_engine(settings)
    memory = CompanyMemory(
        cast(Any, None), uuid4(), uuid4(), embedding_model=None, budget_ms=200, limit=6
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def query(text, vector, name, started):
        entered.set()
        await release.wait()
        return []

    async def sections(text, deep, *, call):
        assert deep
        return []

    monkeypatch.setattr(memory, "_query", query)
    session = cast(
        Session,
        SimpleNamespace(
            settings=settings,
            company_memory=memory,
            _memory_on=lambda: True,
            _vector=lambda text: None,
            section_lines=sections,
            call_context=engine.host.call_context,
            pack=None,
            _refs={},
        ),
    )
    engine.host.memory_lines = lambda text, pass_: Session.memory_lines(session, text, pass_)
    q = question()
    tasks = []
    try:
        engine.add(q)
        await asyncio.wait_for(entered.wait(), 1)
        item = memory.lookups[q.text]
        tasks = [item.fts, item.full]
        if cancel:
            engine.discard(q.qid)
            assert await engine.shutdown(within_s=1)
            assert not provider.requests
            assert not any(kind in {"AnswerDelta", "AnswerDone"} for kind, _ in events)
        else:
            await asyncio.wait_for(engine.wait_idle(), 1)
            assert (
                len(provider.requests) == 1
            )  # Pending lookup does not hold the answer indefinitely.
        assert all(not task.done() for task in tasks)  # Shared retrieval belongs to Session.
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert all(not task.cancelled() for task in tasks)
    finally:
        release.set()
        assert await engine.shutdown(within_s=1)
        memory.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_legacy_keeps_fast_then_deep_memory_and_two_requests(settings_factory):
    engine, provider, _, _, calls, _ = make_engine(
        settings_factory(answer_architecture="legacy_dual")
    )
    try:
        await generate(engine, question())
        assert calls == [
            ("Why did you choose Redis?", "fast"),
            ("Why did you choose Redis?", "deep"),
        ]
        assert len(provider.requests) == 2
    finally:
        assert await engine.shutdown(within_s=1)


@pytest.mark.parametrize("architecture", ["legacy_dual", *ARCHITECTURES])
async def test_screens_keep_legacy_strategy_and_never_fetch_memory(settings_factory, architecture):
    engine, provider, _, _, calls, _ = make_engine(
        settings_factory(answer_architecture=architecture)
    )
    q = question(screen=ScreenAsk((), "interview", None, "Python"))
    try:
        assert engine.strategy(q) == "legacy_dual"
        await generate(engine, q)
        assert calls == []
        assert len(provider.requests) == 1
        assert provider.calls[0][1] == "screen"
    finally:
        assert await engine.shutdown(within_s=1)
