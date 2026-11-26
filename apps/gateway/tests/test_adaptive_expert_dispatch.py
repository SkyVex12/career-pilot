"""Actual engine and Session prewarm isolation; authored providers never use a network."""

import asyncio
import time
from collections.abc import AsyncGenerator, Mapping
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts, screen
from copilot_core.llm import LLMDone, LLMRequest, LLMUsage, prompt_cache_key
from copilot_gateway.answer import AnswerEngine, AnswerHost, Question, ScreenAsk
from copilot_gateway.router import build_router
from copilot_gateway.session import Session


def profile() -> dict[str, Any]:
    return {
        "model": "gpt-6.2-sol",
        "reasoning": "low",
        "service_tier": "fast",
        "retry_tier": "fast",
        "first_token_timeout_ms": 20000,
        "hedge_ms": None,
    }


def settings_for(settings_factory: Any, **changes: Any) -> Any:
    return settings_factory(
        **{
            "answer_architecture": "adaptive",
            "adaptive_expert_route": None,
            "llm_providers": ["openai"],
            "openai_detector_model": "gpt-6-luna",
            "openai_detector_reasoning": "none",
            "openai_fast_model": "gpt-6-luna",
            "openai_fast_reasoning": "none",
            "openai_deep_model": "gpt-6.1-sol",
            "openai_deep_reasoning": "low",
            "openai_fast_service_tier": None,
            "openai_deep_service_tier": None,
            "openai_fast_retry_tier": "priority",
            "openai_deep_retry_tier": "priority",
            "openai_first_token_timeout_ms": 3000,
            "openai_deep_first_token_timeout_ms": 6000,
            "fast_hedge_ms": 1200,
            "deep_hedge_ms": 4500,
            "adaptive_failure_recovery": False,
            "deep_pass_auto": False,
            "model_prices": {
                "gpt-6.2-sol": {"input": 1, "cached": 0.1, "output": 2},
            },
            **changes,
        }
    )


class Provider:
    name = "openai"

    def __init__(self) -> None:
        self.calls: list[tuple[str, LLMRequest, Mapping[str, Any]]] = []
        self.warms: list[tuple[str, LLMRequest, Mapping[str, Any]]] = []
        self.warm_outcomes: list[Any] = []
        self.entered = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.closed = 0

    @staticmethod
    def done(model: str, params: Mapping[str, Any]) -> LLMDone:
        return LLMDone(
            LLMUsage(input_tokens=100, output_tokens=10, cache_write_tokens=0),
            "end_turn",
            "openai",
            model,
            service_tier=str(params.get("service_tier", "standard")),
        )

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        self.calls.append((model, req, deepcopy(params)))
        self.entered.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            yield "Use a durable queue with idempotent consumers."
            yield self.done(model, params)
        finally:
            self.closed += 1

    async def prewarm(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> LLMDone | None:
        self.warms.append((model, req, deepcopy(params)))
        value = self.warm_outcomes.pop(0) if self.warm_outcomes else self.done(model, params)
        if isinstance(value, Exception):
            raise value
        return value


def context(mode: str = "interview") -> prompts.PromptContext:
    return prompts.PromptContext("Alex", mode, "Verified profile", "Verified pack")


def make_engine(settings: Any, provider: Provider) -> tuple[AnswerEngine, list[Any], list[Any]]:
    events: list[Any] = []
    logs: list[Any] = []

    async def memory(text: str, pass_: str) -> tuple[list[str], dict[str, Any]]:
        return ["[n:ab12] Verified project uses durable storage."], {"memory_hits": 1}

    async def get(key: str) -> None:
        return None

    async def put(key: str, value: Any) -> None:
        return None

    host = SimpleNamespace(
        settings=settings,
        router=build_router(settings, [provider]),
        emit=lambda cls, **fields: events.append((cls.__name__, fields)),
        log_entry=logs.append,
        store_text=lambda: True,
        call_context=lambda: prompts.CallContext(recent="Them: Explain the design."),
        prompt_context=context,
        refs=lambda: {},
        llm_status=lambda ok: None,
        cache_version=lambda: "authored-v1",
        answer_style=lambda: "paragraph",
        response_length=lambda: "default",
        cache_extra=lambda: {},
        memory_lines=memory,
        qa_get=get,
        qa_put=put,
    )
    return AnswerEngine(cast(AnswerHost, host)), events, logs


def question(**changes: Any) -> Question:
    return Question(
        **{
            "qid": uuid4(),
            "text": "Design a distributed notification system?",
            "qtype": "system_design",
            "confidence": 1,
            "addressed": True,
            "speculative": False,
            "forced": False,
            "t0": time.monotonic(),
            "detector_ms": 1,
            "match": None,
            "depth": "deep",
            **changes,
        }
    )


async def run_pass(engine: AnswerEngine, q: Question, pass_: str = "fast") -> None:
    engine.questions[q.qid] = q
    engine.start_pass(q, pass_)
    await engine.wait_idle()


async def test_isolated_expert_matches_final_b_request_and_actual_usage_telemetry(
    settings_factory: Any,
) -> None:
    common = settings_for(settings_factory, adaptive_expert_route=profile())
    old_b = settings_for(
        settings_factory,
        openai_deep_model="gpt-6.2-sol",
        openai_deep_reasoning="low",
        openai_deep_service_tier="fast",
        openai_deep_retry_tier="fast",
        openai_deep_first_token_timeout_ms=20000,
        deep_hedge_ms=None,
    )
    first, second = Provider(), Provider()
    isolated, events, logs = make_engine(common, first)
    previous, _, _ = make_engine(old_b, second)
    q, prior_q = question(), question()
    await run_pass(isolated, q)
    await run_pass(previous, prior_q)
    assert first.calls == second.calls
    assert len(first.calls) == first.closed == 1
    model, req, params = first.calls[0]
    assert model == "gpt-6.2-sol"
    assert req.max_tokens == 1200
    assert params == {
        "reasoning": {"effort": "low"},
        "max_output_tokens_extra": 2048,
        "service_tier": "fast",
    }
    assert prompt_cache_key(req.system) == prompt_cache_key(second.calls[0][1].system)
    assert q.plans["fast"] == prior_q.plans["fast"]
    assert q.plans["fast"].job == "deep"
    assert set(q.tasks) == {"fast"}
    answer = next(row for row in logs if row["kind"] == "answer")
    assert (answer["pass"], answer["request_job"], answer["model"], answer["tier"]) == (
        "fast",
        "adaptive_expert",
        "gpt-6.2-sol",
        "fast",
    )
    assert answer["latency_ms"]["configured_reasoning"] == {"reasoning": {"effort": "low"}}
    assert answer["cost_usd"] == 0.00024  # Authored rates, returned actual model and fast tier.
    assert {row["pass"] for kind, row in events if kind == "AnswerDone"} == {"fast"}
    assert common.adaptive_stream_idle_timeout_s == old_b.adaptive_stream_idle_timeout_s
    assert common.adaptive_stream_total_timeout_s == old_b.adaptive_stream_total_timeout_s


@pytest.mark.parametrize("architecture", ["adaptive", "adaptive_hybrid", "adaptive_on_demand"])
async def test_simple_primary_stays_fast_and_explicit_expert_keeps_deep_public_pass(
    settings_factory: Any,
    architecture: str,
) -> None:
    provider = Provider()
    engine, _, logs = make_engine(
        settings_for(
            settings_factory,
            answer_architecture=architecture,
            adaptive_expert_route=profile(),
        ),
        provider,
    )
    q = question(text="Where are you based?", qtype="other", depth="light")
    await run_pass(engine, q)
    await run_pass(engine, q, "deep")
    assert [model for model, _, _ in provider.calls] == ["gpt-6-luna", "gpt-6.2-sol"]
    assert [(row["pass"], row["request_job"]) for row in logs if row["kind"] == "answer"] == [
        ("fast", "fast"),
        ("deep", "adaptive_expert"),
    ]
    assert q.plans["deep"].job == "deep"


@pytest.mark.parametrize("scenario", ["legacy", "screen", "screen_followup"])
async def test_legacy_and_screenshot_requests_are_identical_with_retained_opt_in(
    settings_factory: Any,
    scenario: str,
) -> None:
    records = []
    for enabled in (False, True):
        provider = Provider()
        engine, _, logs = make_engine(
            settings_for(
                settings_factory,
                answer_architecture="legacy_dual" if scenario == "legacy" else "adaptive",
                adaptive_expert_route=profile() if enabled else None,
            ),
            provider,
        )
        q = question(qtype="coding")
        if scenario == "screen":
            q.screen = ScreenAsk((), "interview", None, "Python")
            engine.add(q)  # Both original screenshot passes; no valid reading suppresses either.
            await engine.wait_idle()
            expected_jobs = {"screen", "screen_code"}
        else:
            if scenario == "screen_followup":
                engine.problem = screen.Problem(
                    uuid4(), (), None, "Merge intervals", "def merge(xs): ...", time.monotonic()
                )
            await run_pass(engine, q, "deep")
            expected_jobs = {"deep"}
        assert q.plans == {}
        assert len(provider.calls) == len(expected_jobs)
        assert {row["request_job"] for row in logs if row["kind"] == "answer"} == expected_jobs
        records.append(provider.calls)
    assert records[0] == records[1]


@pytest.mark.parametrize("finish", ["release", "cancel"])
async def test_held_expert_keeps_frozen_plan_and_owns_cancelled_provider(
    settings_factory: Any,
    finish: str,
) -> None:
    provider = Provider()
    provider.gate = asyncio.Event()
    engine, events, logs = make_engine(
        settings_for(settings_factory, adaptive_expert_route=profile()), provider
    )
    q = question(held=True)
    try:
        engine.add(q)
        await asyncio.wait_for(provider.entered.wait(), 1)
        frozen = q.plans["fast"]
        q.qtype, q.depth = "other", "light"
        assert not events
        if finish == "release":
            engine.release(q.qid)
            engine.confirm(q.qid, q.text, 0)
            provider.gate.set()
            await engine.wait_idle()
            assert (
                next(row for row in logs if row["kind"] == "answer")["request_job"]
                == "adaptive_expert"
            )
        else:
            assert await engine.shutdown(within_s=1)
            provider.gate.set()
            assert not [row for kind, row in events if kind in {"AnswerDelta", "AnswerDone"}]
        assert q.plans["fast"] is frozen
        assert len(provider.calls) == provider.closed == 1
    finally:
        provider.gate.set()
        await engine.shutdown(within_s=1)


def warm_session(settings: Any, provider: Provider) -> Any:
    obj = cast(Any, object.__new__(Session))
    obj.settings, obj.router = settings, build_router(settings, [provider])
    obj.ending = obj.lost = obj._retention_failed = False
    obj._warm, obj._warm_tasks = None, set()
    obj._prompt_ctx, obj.info = context(), SimpleNamespace(mode="interview", employer_id=None)
    obj.records = []
    obj.log_entry, obj._cost = obj.records.append, lambda _: 0.001
    return obj


async def warm(obj: Any) -> None:
    obj._start_warm(obj.prewarm_prompts)
    await obj._warm


@pytest.mark.parametrize("first", ["success", "none", "failure"])
async def test_prewarm_uses_real_expert_prefix_and_dedups_only_success(
    settings_factory: Any,
    first: str,
) -> None:
    provider = Provider()
    if first != "success":
        provider.warm_outcomes = [None if first == "none" else RuntimeError("authored")]
    settings = settings_for(
        settings_factory, adaptive_expert_route=profile(), prewarm_jobs=["deep", "deep", "fast"]
    )
    obj = warm_session(settings, provider)
    await warm(obj)
    assert [model for model, _, _ in provider.warms] == (
        ["gpt-6.2-sol", "gpt-6-luna"]
        if first == "success"
        else ["gpt-6.2-sol", "gpt-6.2-sol", "gpt-6-luna"]
    )
    engine, _, _ = make_engine(settings, provider)
    req = engine._base_request(question(), "deep", ["[n:ab12] Retrieved source"])
    assert prompt_cache_key(req.system) == prompt_cache_key(provider.warms[0][1].system)
    assert [(row["pass"], row.get("request_job")) for row in obj.records] == [
        ("deep", "adaptive_expert"),
        ("fast", None),
    ]
    obj.info.mode, obj._prompt_ctx = "standup", replace(obj._prompt_ctx, mode="standup")
    obj._start_warm(lambda: obj._mode_changed("standup"))
    await obj._warm
    assert prompt_cache_key(provider.warms[-2][1].system) != prompt_cache_key(
        provider.warms[0][1].system
    )


async def test_prewarm_legacy_rollback_is_exact(settings_factory: Any) -> None:
    snapshots = []
    for enabled in (False, True):
        provider = Provider()
        settings = settings_for(
            settings_factory,
            answer_architecture="legacy_dual",
            adaptive_expert_route=profile() if enabled else None,
            prewarm_jobs=["fast", "deep_light", "deep", "deep"],
        )
        obj = warm_session(settings, provider)
        await warm(obj)
        assert all("request_job" not in row for row in obj.records)
        snapshots.append(provider.warms)
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("stop", ["superseded", "cancelled"])
async def test_stale_prewarm_keeps_returned_usage_and_stops_remaining_jobs(
    settings_factory: Any, stop: str
) -> None:
    provider = Provider()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(model: str, req: LLMRequest, params: Mapping[str, Any]) -> LLMDone:
        provider.warms.append((model, req, deepcopy(params)))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return provider.done(model, params)

    provider.prewarm = held  # type: ignore[method-assign]
    obj = warm_session(
        settings_for(
            settings_factory, adaptive_expert_route=profile(), prewarm_jobs=["deep", "fast"]
        ),
        provider,
    )
    obj._start_warm(obj.prewarm_prompts)
    old = obj._warm
    try:
        await asyncio.wait_for(entered.wait(), 1)

        async def noop() -> None:
            return None

        if stop == "superseded":
            obj._start_warm(noop)
            await obj._warm
        else:
            old.cancel()
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(old, 1)
        assert len(provider.warms) == len(obj.records) == 1
        assert obj.records[0]["request_job"] == "adaptive_expert"
        assert obj.records[0]["model"] == "gpt-6.2-sol"
        assert obj.records[0]["tier"] == "fast"
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)
