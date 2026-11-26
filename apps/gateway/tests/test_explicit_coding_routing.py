"""Explicit coding requests use the existing expert route before detector typing."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest

from copilot_core import prompts
from copilot_core.llm import Decided, LLMChunk, LLMDone, LLMRequest, LLMUsage
from copilot_gateway import session as session_module
from copilot_gateway.adaptive import plan_answer
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    ScriptedProvider,
    text_frame_audio,
)

ARCHITECTURES = ("adaptive", "adaptive_hybrid", "adaptive_on_demand")
QUESTION = "Write a Python function to merge overlapping intervals."
EVIDENCE = "[n:1234] Authored constraint: intervals may arrive in arbitrary order."


@pytest.mark.parametrize("architecture", ARCHITECTURES)
@pytest.mark.parametrize("qtype", [None, "other", "coding"])
@pytest.mark.parametrize(
    "text",
    [
        QUESTION,
        "Please implement a bounded async map in Python.",
        "Could you write an async function that limits active work?",
        "Write code to deduplicate these records.",
        "Implement merge_intervals(intervals).",
        "Write a SQL query for the latest event per user.",
        "Where are you based, and write a Python function to merge intervals?",
    ],
)
def test_explicit_code_route_before_typing(architecture, qtype, text):
    plan = plan_answer(text, qtype, None, architecture)
    assert plan.depth == "expert"
    assert plan.job == ("deep" if architecture == "adaptive" else "fast")
    assert plan.max_tokens == (1200 if architecture == "adaptive" else 650)
    assert plan.auto_expert == (architecture == "adaptive_hybrid")


@pytest.mark.parametrize(
    "text",
    [
        "Write a status update about the rollout.",
        "Implement a rollout plan.",
        "What is the function of the payments team?",
        "Explain how a Python function works.",
        "Write an explanation of the function of this team.",
        "We wrote a Python function last week.",
        "Do not write code; explain the choice.",
        "Please don't implement this in Python.",
        "What is your favorite programming language?",
        "Where are you based?",
    ],
)
def test_ordinary_language_does_not_become_coding(text):
    assert plan_answer(text, "other", None, "adaptive").depth in ("brief", "standard")


class CodingProvider(ScriptedProvider):
    def __init__(self):
        super().__init__("openai")
        self.requests: list[tuple[str, LLMRequest]] = []
        self.decisions = 0
        self.detected = asyncio.Event()
        self.allow_detection = asyncio.Event()
        self.primary_started = asyncio.Event()
        self.allow_primary = asyncio.Event()
        self.closed_streams = 0

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        self.decisions += 1
        self.detected.set()
        await self.allow_detection.wait()
        return Decided(
            [
                {"type": "choice", "name": key, "choice": value, "confidence": 0.99}
                for key, value in (("turn", "answer"), ("type", "coding"), ("depth", "deep"))
            ],
            LLMUsage(10, cache_write_tokens=0),
            "openai",
            model,
        )

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        self.requests.append((model, req))
        expert = "independent expert response" in str(req.content)
        try:
            yield "Sort the intervals before merging them. "
            if not expert:
                self.primary_started.set()
                await self.allow_primary.wait()
            yield LLMDone(LLMUsage(20, 0, 10, cache_write_tokens=0), "end_turn", self.name, model)
        finally:
            self.closed_streams += 1


@asynccontextmanager
async def live_session(env, monkeypatch, architecture, length="default"):
    provider = CodingProvider()
    memory_calls = []

    async def memory(self, text, pass_):
        memory_calls.append((text, pass_))
        return [EVIDENCE], {"memory_hits": 1}

    monkeypatch.setattr(session_module.Session, "memory_lines", memory)
    env.providers = [provider]
    settings = env.settings(
        answer_architecture=architecture,
        adaptive_expert_route=None,
        adaptive_technical_legacy=False,
        llm_providers=["openai"],
        detector_api="decisions",
        openai_detector_model="fixture-detector",
        openai_fast_model="fixture-fast",
        openai_deep_model="fixture-deep",
        early_decision_interval_ms=1,
        early_decision_all_modes=True,
        first_show_min_words=None,
        decisions_hedge_ms=None,
        fast_hedge_ms=None,
        deep_hedge_ms=None,
        prewarm_jobs=[],
        deep_pass_auto=True,
        deep_pass_auto_types=[],
        fast_pass_max_tokens=180,
    )
    async with GatewayServer(env.services(settings)) as gateway:
        client = ProtocolClient(gateway.url)
        await client.start(response_length=length)
        session = next(iter(gateway.manager.sessions.values()))
        try:
            yield session, client, provider, memory_calls
        finally:
            provider.allow_detection.set()
            provider.allow_primary.set()
            await client.close()


@pytest.mark.parametrize("architecture", ARCHITECTURES)
@pytest.mark.parametrize("length", ["default", "concise"])
async def test_typed_explicit_request_uses_expert_without_classifier(
    env, monkeypatch, architecture, length
):
    async with live_session(env, monkeypatch, architecture, length) as (session, client, p, memory):
        await client.send({"type": "question.force", "text": QUESTION})
        await asyncio.wait_for(p.primary_started.wait(), 2)
        q = next(iter(session.answers.questions.values()))
        assert q.qtype == "other"  # Local routing never pretends the detector returned.
        assert p.decisions == 0
        assert q.plans["fast"].depth == "expert"
        model, req = p.requests[0]
        assert model == ("fixture-deep" if architecture == "adaptive" else "fixture-fast")
        assert req.max_tokens == (1200 if architecture == "adaptive" else 650)
        assert QUESTION in str(req.content)
        assert EVIDENCE in str(req.content)
        p.allow_primary.set()
        await session.answers.wait_idle()
        assert len(p.requests) == (2 if architecture == "adaptive_hybrid" else 1)
        assert len(memory) == len(p.requests)
        assert all(text == QUESTION for text, _ in memory)
        assert p.closed_streams == len(p.requests)
        if architecture == "adaptive_hybrid":
            expert = p.requests[1][1]
            assert expert.max_tokens == 1200
            assert "Sort the intervals before merging them" not in str(expert.content)
            assert "<shown_cues>" not in str(expert.content)


@pytest.mark.parametrize("architecture", ARCHITECTURES)
@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_held_code_keeps_plan_and_confirmation_ownership(
    env, monkeypatch, architecture, cancel
):
    async with live_session(env, monkeypatch, architecture, "concise") as (session, client, p, _):
        await client.frame(
            0,
            0b01,
            text_frame_audio("Could you write a Python function to merge overlapping intervals?"),
            0,
        )
        await asyncio.wait_for(p.detected.wait(), 2)
        await asyncio.wait_for(p.primary_started.wait(), 2)
        q = next(iter(session.answers.questions.values()))
        assert q.held
        assert q.qtype == "other"
        assert q.depth is None
        frozen = q.plans["fast"]
        assert frozen.depth == "expert"
        assert p.requests[0][1].max_tokens == (1200 if architecture == "adaptive" else 650)
        assert not client.of_type("answer.delta")
        assert len(p.requests) == 1  # Hybrid waits for confirmation, not just local text.
        if cancel:
            session.turns.cancel()
            await session.answers.wait_idle()
            assert p.closed_streams == 1
            assert q.discarded or q.cancelled
            assert not client.of_type("answer.delta")
            assert len(p.requests) == 1
        else:
            p.allow_detection.set()
            await client.frame(0, 0b10, b"", 100)
            await client.wait_type("question.detected", question_id=str(q.qid), speculative=False)
            assert q.qtype == "coding"
            assert q.depth == "deep"
            assert q.plans["fast"] is frozen
            p.allow_primary.set()
            await session.answers.wait_idle()
            assert p.decisions == 1
            assert len(p.requests) == (2 if architecture == "adaptive_hybrid" else 1)
            assert p.closed_streams == len(p.requests)
            assert all(EVIDENCE in str(req.content) for _, req in p.requests)


@pytest.mark.parametrize(("length", "budget"), [("default", 180), ("concise", 108)])
async def test_legacy_untyped_coding_request_preserves_existing_budget(
    env, monkeypatch, length, budget
):
    async with live_session(env, monkeypatch, "legacy_dual", length) as (session, client, p, _):
        await client.send({"type": "question.force", "text": QUESTION})
        await asyncio.wait_for(p.primary_started.wait(), 2)
        q = next(iter(session.answers.questions.values()))
        assert not q.plans
        assert p.requests[0][0] == "fixture-fast"
        assert p.requests[0][1].max_tokens == budget
        snapshot = session.answers._request_snapshot(q)
        expected = prompts.fast_pass(
            snapshot.context, snapshot.transcript, QUESTION, "other", 180, [EVIDENCE]
        )
        if length == "concise":
            expected = prompts.with_response_length(expected, "concise", max_tokens=108)
        assert p.requests[0][1] == expected
        p.allow_primary.set()
        await session.answers.wait_idle()
        assert len(p.requests) == 1
        assert p.decisions == 0
