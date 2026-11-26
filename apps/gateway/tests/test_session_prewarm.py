"""Architecture-aware prompt warming through real Session/ModelRouter methods; no network."""

import asyncio
import time
from collections.abc import AsyncGenerator, Mapping
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMDone, LLMRequest, LLMUsage, ModelRouter, Route, prompt_cache_key
from copilot_gateway.answer import AnswerEngine, AnswerHost, Question
from copilot_gateway.session import Session


class WarmProvider:
    name = "authored"

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, LLMRequest, Mapping[str, Any]]] = []
        self.outcomes = list(outcomes or [])

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        raise AssertionError("prewarming must not request an answer stream")
        yield "unreachable"

    async def prewarm(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> LLMDone | None:
        self.calls.append((model, req, deepcopy(params)))
        result = self.outcomes.pop(0) if self.outcomes else returned(model)
        if isinstance(result, Exception):
            raise result
        return result


def returned(model: str = "fast-model") -> LLMDone:
    return LLMDone(LLMUsage(input_tokens=12, cache_write_tokens=12), "prewarm", "authored", model)


def context(mode: str = "interview") -> prompts.PromptContext:
    return prompts.PromptContext(
        "Alex",
        mode,
        "[n:profile] Verified profile",
        "[n:pack] Verified session pack",
        company="Authored company",
        role="Engineer",
        company_digest="[n:company] Release facts",
    )


def session(settings: Any, routes: dict[str, list[Route]], mode: str = "interview") -> Any:
    obj = cast(Any, object.__new__(Session))
    obj.settings = settings
    obj.router = ModelRouter(routes, first_token_timeout_s=1)
    obj.ending = obj.lost = obj._retention_failed = False
    obj._warm = None
    obj._warm_tasks = set()
    obj._prompt_ctx = context(mode)
    obj.records = []
    obj.log_entry = obj.records.append
    obj._cost = lambda _: 0.001
    obj.info = SimpleNamespace(mode=mode, employer_id=None)
    return obj


async def warm(obj: Any) -> None:
    obj._start_warm(obj.prewarm_prompts)
    await obj._warm


def cached_prefix(req: LLMRequest) -> tuple[str, ...]:
    last = max(i for i, block in enumerate(req.system) if block.cache)
    return tuple(block.text for block in req.system[: last + 1])


@pytest.mark.parametrize("architecture", ["adaptive", "adaptive_hybrid", "adaptive_on_demand"])
@pytest.mark.parametrize("mode", ["interview", "standup", "meeting"])
async def test_warm_prefix_matches_actual_primary_and_expert_requests(
    settings_factory,
    architecture: str,
    mode: str,
) -> None:
    provider = WarmProvider()
    settings = settings_factory(answer_architecture=architecture, prewarm_jobs=["fast", "deep"])
    obj = session(
        settings,
        {
            "fast": [Route(provider, "fast-model")],
            "deep": [Route(provider, "deep-model")],
        },
        mode,
    )
    await warm(obj)
    host = SimpleNamespace(
        settings=settings,
        prompt_context=lambda: obj._prompt_ctx,
        call_context=lambda: prompts.CallContext(recent="Them: How would you design this service?"),
        answer_style=lambda: "paragraph",
        response_length=lambda: "detailed",
    )
    engine = AnswerEngine(cast(AnswerHost, host))
    q = Question(
        uuid4(),
        "How would you design this service?",
        "system_design",
        1,
        True,
        False,
        False,
        time.monotonic(),
        None,
        None,
        depth="deep",
    )
    assert len(provider.calls) == 2
    for (_, warmed, _), pass_ in zip(provider.calls, ["fast", "deep"], strict=True):
        real = engine._base_request(q, pass_, ["[n:memory] Retrieved fact"])
        assert cached_prefix(warmed) == cached_prefix(real)
        assert prompt_cache_key(warmed.system) == prompt_cache_key(real.system)
        # Preferences append an uncached suffix; they do not change the cache being warmed.
        preferred = prompts.with_response_length(real, "detailed")
        assert prompt_cache_key(warmed.system) == prompt_cache_key(preferred.system)


@pytest.mark.parametrize("jobs", [[], ["deep"], ["deep_light", "fast", "deep", "fast"]])
async def test_legacy_requests_and_configured_calls_remain_exact(settings_factory, jobs) -> None:
    provider = WarmProvider()
    obj = session(
        settings_factory(answer_architecture="legacy_dual", prewarm_jobs=jobs),
        {
            job: [Route(provider, "same-model", {"reasoning": {"effort": "low"}})]
            for job in ("fast", "deep", "deep_light")
        },
    )
    await warm(obj)
    expected = [
        prompts.fast_pass(obj._prompt_ctx, "", "(warm-up)", None, obj.settings.fast_pass_max_tokens)
        if job == "fast"
        else prompts.deep_pass(
            obj._prompt_ctx, "", "(warm-up)", "other", obj.settings.deep_pass_max_tokens
        )
        for job in jobs
    ]
    assert [req for _, req, _ in provider.calls] == expected
    assert [record["pass"] for record in obj.records] == jobs


@pytest.mark.parametrize("difference", ["same", "provider", "model", "params"])
async def test_dedup_only_identical_successful_cache_targets(
    settings_factory, difference: str
) -> None:
    first = WarmProvider()
    second = WarmProvider() if difference == "provider" else first
    other_model = "other-model" if difference == "model" else "same-model"
    other_params = {"reasoning": {"effort": "high" if difference == "params" else "low"}}
    obj = session(
        settings_factory(answer_architecture="adaptive", prewarm_jobs=["deep_light", "fast"]),
        {
            "deep_light": [Route(first, "same-model", {"reasoning": {"effort": "low"}})],
            "fast": [Route(second, other_model, other_params)],
        },
    )
    await warm(obj)
    calls = first.calls + (second.calls if second is not first else [])
    assert len(calls) == (1 if difference == "same" else 2)
    assert [record["pass"] for record in obj.records] == (
        ["deep_light"] if difference == "same" else ["deep_light", "fast"]
    )


@pytest.mark.parametrize("first_result", [None, RuntimeError("authored warm failure")])
async def test_failed_or_unsupported_first_warm_does_not_skip_next_attempt(
    settings_factory,
    first_result: Any,
) -> None:
    provider = WarmProvider([first_result])
    route = Route(provider, "same-model")
    obj = session(
        settings_factory(answer_architecture="adaptive", prewarm_jobs=["fast", "deep_light"]),
        {"fast": [route], "deep_light": [route]},
    )
    await warm(obj)
    assert len(provider.calls) == 2
    assert [record["pass"] for record in obj.records] == ["deep_light"]


async def test_dedup_uses_first_non_refusal_route_and_keeps_distinct_targets(
    settings_factory,
) -> None:
    provider = WarmProvider()
    unused = WarmProvider()
    fast = Route(provider, "fast-model")
    obj = session(
        settings_factory(answer_architecture="adaptive"),
        {
            "fast": [Route(unused, "refusal-only", refusal_only=True), fast],
            "deep_light": [fast, Route(unused, "unselected-fallback")],
        },
    )
    await warm(obj)
    assert len(provider.calls) == 1
    assert unused.calls == []


async def test_success_dedup_is_local_to_each_invocation_and_mode(settings_factory) -> None:
    provider = WarmProvider()
    route = Route(provider, "same-model")
    obj = session(
        settings_factory(answer_architecture="adaptive"), {"fast": [route], "deep_light": [route]}
    )
    await warm(obj)
    await warm(obj)
    for mode in ("standup", "meeting"):
        obj.info.mode = mode
        obj._prompt_ctx = replace(obj._prompt_ctx, mode=mode)
        obj._start_warm(lambda mode=mode: obj._mode_changed(mode))
        await obj._warm
    keys = [prompt_cache_key(req.system) for _, req, _ in provider.calls]
    assert len(keys) == 4
    assert keys[0] == keys[1]
    assert len(set(keys)) == 3


@pytest.mark.parametrize("stop", ["ending", "lost", "_retention_failed", "cancelled", "superseded"])
async def test_adaptive_warm_ownership_keeps_returned_usage_without_more_jobs(
    settings_factory,
    stop: str,
) -> None:
    provider = WarmProvider()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(model: str, req: LLMRequest, params: Mapping[str, Any]) -> LLMDone:
        provider.calls.append((model, req, params))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return returned(model)

    provider.prewarm = held
    obj = session(
        settings_factory(answer_architecture="adaptive", prewarm_jobs=["fast", "deep"]),
        {
            "fast": [Route(provider, "fast-model")],
            "deep": [Route(provider, "deep-model")],
        },
    )
    obj._start_warm(obj.prewarm_prompts)
    task = obj._warm
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if stop == "cancelled":
            task.cancel()
            await asyncio.sleep(0)
        elif stop == "superseded":

            async def noop() -> None:
                return None

            obj._start_warm(noop)
            await obj._warm
        else:
            setattr(obj, stop, True)
        release.set()
        await asyncio.wait_for(task, 1)
        assert len(provider.calls) == 1
        assert [record["input_tokens"] for record in obj.records] == [12]
        assert [record["pass"] for record in obj.records] == ["fast"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("jobs", [[], ["deep"], ["deep", "fast"]])
async def test_adaptive_uses_only_configured_targets_in_order(settings_factory, jobs) -> None:
    provider = WarmProvider()
    obj = session(
        settings_factory(answer_architecture="adaptive", prewarm_jobs=jobs),
        {job: [Route(provider, job + "-model")] for job in ("fast", "deep", "deep_light")},
    )
    await warm(obj)
    assert [model for model, _, _ in provider.calls] == [job + "-model" for job in jobs]
    assert [record["pass"] for record in obj.records] == jobs
