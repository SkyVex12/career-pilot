"""Scripted engine regressions for compound depth precedence; no SDK clients or network."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Mapping
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMChunk, LLMDone, LLMRequest, LLMUsage, ModelRouter, Route
from copilot_gateway import adaptive, answer
from copilot_gateway.answer import AnswerEngine, AnswerHost, Question
from copilot_gateway.config import Settings
from copilot_gateway.testing import ScriptedProvider

MODES = ("adaptive", "adaptive_hybrid", "adaptive_on_demand")
DESIGN = "Where are you based, and how would you design a distributed notification system?"
STORY = "Where are you based, and tell me about a time you handled a production incident?"
SIMPLE = "Where are you based?"
SOURCE = "[n:a123] Fictional source: the team used an operation key to deduplicate requests."


@pytest.fixture(scope="module")
def candidate():
    return adaptive


class GatedScriptedProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__("scripted")
        self.recorded: list[tuple[str, LLMRequest]] = []
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.released.set()
        self.closed = 0

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        self.recorded.append((model, req))
        self.started.set()
        try:
            yield "Use the recorded operation key to deduplicate requests. "
            await self.released.wait()
            yield "Confirm the ownership boundary before describing the recovery path."
            yield LLMDone(LLMUsage(20, 0, 12, cache_write_tokens=0), "end_turn", self.name, model)
        finally:
            self.closed += 1


def make_engine(mode: str):
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        env="test",
        database_url=None,
        personal_ai_api_url=None,
        llm_providers=["openai"],
        embedder="none",
        answer_architecture=mode,
        adaptive_max_tokens=1200,
        fast_pass_max_tokens=180,
        deep_pass_max_tokens=900,
        deep_pass_auto=True,
        deep_pass_auto_types=["system_design", "behavioral"],
        fast_hedge_ms=None,
        deep_hedge_ms=None,
    )
    provider = GatedScriptedProvider()
    router = ModelRouter(
        {job: [Route(provider, "fixture-" + job)] for job in ("fast", "deep", "deep_light")},
        first_token_timeout_s=1,
    )
    events: list[tuple[str, dict[str, Any]]] = []
    logs: list[dict[str, Any]] = []
    retrieval: list[tuple[str, str]] = []

    async def memory(text: str, pass_: str):
        retrieval.append((text, pass_))
        return [SOURCE], {"memory_hits": 1}

    async def get(key: str):
        return None

    async def put(key: str, value: dict[str, Any]):
        pass

    def emit(cls: type[Any], **fields: Any) -> int:
        events.append((cls.__name__, fields))
        return len(events)

    host = SimpleNamespace(
        settings=settings,
        router=router,
        emit=emit,
        log_entry=logs.append,
        store_text=lambda: True,
        call_context=lambda: prompts.CallContext(recent="Them: Please explain."),
        prompt_context=lambda: prompts.PromptContext(
            "Alex", "interview", "Fictional source profile", None
        ),
        refs=lambda: {},
        llm_status=lambda ok: None,
        cache_version=lambda: "authored-integration",
        answer_style=lambda: "paragraph",
        response_length=lambda: "default",
        cache_extra=lambda: {},
        memory_lines=memory,
        qa_get=get,
        qa_put=put,
        prepared_source_version=lambda policy: None,
    )
    return AnswerEngine(cast(AnswerHost, host)), provider, events, logs, retrieval, settings


def question(text: str, qtype: str = "other", **extra: Any) -> Question:
    values: dict[str, Any] = dict(
        qid=uuid4(),
        text=text,
        qtype=qtype,
        confidence=1.0,
        addressed=True,
        speculative=False,
        forced=False,
        t0=time.monotonic(),
        detector_ms=1,
        match=None,
    )
    values.update(extra)
    return Question(**values)


async def idle(engine: AnswerEngine) -> None:
    await asyncio.wait_for(engine.wait_idle(), 2)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("text", "qtype", "depth", "budget"),
    [
        (DESIGN, "system_design", "expert", 650),
        (STORY, "respond", "narrative", 600),
        (SIMPLE, "other", "brief", 100),
    ],
)
async def test_real_requests_preserve_compound_budget(
    candidate, monkeypatch, mode, text, qtype, depth, budget
):
    monkeypatch.setattr(answer, "plan_answer", candidate.plan_answer)
    engine, provider, events, logs, retrieval, _ = make_engine(mode)
    q = question(text, qtype)
    try:
        engine.add(q)
        engine.confirm(q.qid, text, 0)
        await idle(engine)
        expected_budget = 1200 if text == DESIGN and mode == "adaptive" else budget
        expected_job = "deep" if text == DESIGN and mode == "adaptive" else "fast"
        assert provider.recorded[0][0] == "fixture-" + expected_job
        req = provider.recorded[0][1]
        assert req.max_tokens == expected_budget
        assert text in str(req.content)
        assert SOURCE in str(req.content)
        assert q.plans["fast"].depth == depth
        primary = next(row for row in logs if row["kind"] == "answer" and row["pass"] == "fast")
        assert primary["request_job"] == expected_job
        assert primary["max_output_tokens"] == expected_budget
        expected_passes = 2 if mode == "adaptive_hybrid" and text == DESIGN else 1
        assert len(provider.recorded) == expected_passes
        if expected_passes == 2:
            expert = provider.recorded[1][1]
            assert expert.max_tokens == 1200
            assert "<shown_cues>" not in str(expert.content)
        assert retrieval[0][1] == ("deep" if depth in {"narrative", "expert"} else "fast")
        assert not any(kind == "AnswerError" for kind, _ in events)
    finally:
        assert await engine.shutdown(within_s=1)
    assert provider.closed == len(provider.recorded)


@pytest.mark.parametrize("mode", MODES)
async def test_held_compound_request_survives_confirmation_without_restart(
    candidate, monkeypatch, mode
):
    monkeypatch.setattr(answer, "plan_answer", candidate.plan_answer)
    engine, provider, events, logs, _, _ = make_engine(mode)
    provider.released.clear()
    q = question(DESIGN, held=True, speculative=True)
    try:
        engine.add(q)
        await asyncio.wait_for(provider.started.wait(), 1)
        assert provider.recorded[0][1].max_tokens == (1200 if mode == "adaptive" else 650)
        assert not any(kind == "AnswerDelta" for kind, _ in events)
        q.qtype, q.depth = "system_design", "deep"
        engine.release(q.qid)
        engine.confirm(q.qid, DESIGN, 0)
        provider.released.set()
        await idle(engine)
        assert len(provider.recorded) == (2 if mode == "adaptive_hybrid" else 1)
        assert q.was_speculative
        assert q.confirmed
        deltas = [data for kind, data in events if kind == "AnswerDelta"]
        assert deltas
        assert all(data["question_id"] == q.qid for data in deltas)
        for pass_ in {data["pass"] for data in deltas}:
            assert [d["sequence"] for d in deltas if d["pass"] == pass_] == list(
                range(q.delta_counts[pass_])
            )
        primary = next(row for row in logs if row["kind"] == "answer" and row["pass"] == "fast")
        assert primary["latency_ms"]["detector_depth_at_start"] is None
        assert primary["latency_ms"]["detector_depth_at_completion"] == "deep"
    finally:
        provider.released.set()
        assert await engine.shutdown(within_s=1)


@pytest.mark.parametrize("mode", MODES)
async def test_cancel_held_compound_drains_provider_and_prevents_late_start(
    candidate, monkeypatch, mode
):
    monkeypatch.setattr(answer, "plan_answer", candidate.plan_answer)
    engine, provider, events, logs, _, _ = make_engine(mode)
    provider.released.clear()
    q = question(DESIGN, held=True, speculative=True)
    try:
        engine.add(q)
        await asyncio.wait_for(provider.started.wait(), 1)
        engine.cancel_speculative(q.qid)
        provider.released.set()
        engine.confirm(q.qid, DESIGN, 0)
        assert not engine.expand(q.qid)
        await idle(engine)
        assert len(provider.recorded) == 1
        assert provider.closed == 1
        assert not any(kind == "AnswerDelta" for kind, _ in events)
        assert any(row["kind"] == "pass_cancelled" for row in logs)
        assert q.qid not in engine.questions
    finally:
        provider.released.set()
        assert await engine.shutdown(within_s=1)


async def test_legacy_requests_are_exactly_unchanged(candidate, monkeypatch):
    def forbidden_legacy_planner(*args: Any, **kwargs: Any):
        raise AssertionError("Legacy path must not consult adaptive depth policy")

    captured = []
    for planner in (forbidden_legacy_planner, candidate.plan_answer):
        monkeypatch.setattr(answer, "plan_answer", planner)
        engine, provider, events, _, _, settings = make_engine("legacy_dual")
        q = question(DESIGN, "system_design", depth="deep")
        try:
            engine.add(q)
            engine.confirm(q.qid, DESIGN, 0)
            await idle(engine)
            assert [req.max_tokens for _, req in provider.recorded] == [
                settings.fast_pass_max_tokens,
                settings.deep_pass_max_tokens,
            ]
            assert len(provider.recorded) == 2
            assert not q.plans
            assert not any(kind == "AnswerError" for kind, _ in events)
            captured.append([(model, asdict(req)) for model, req in provider.recorded])
        finally:
            assert await engine.shutdown(within_s=1)
    assert captured[0] == captured[1]
