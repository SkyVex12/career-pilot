"""Fastest supported deep tier, explicit rollback and bounded fallback."""

import os
from typing import Any, cast

import pytest

from copilot_core.llm import AllRoutesFailedError, LLMDone, LLMRequest, LLMUsage, OpenAIProvider
from copilot_gateway.config import Settings, fastest_deep_tier
from copilot_gateway.router import build_router
from copilot_gateway.testing import ScriptedProvider


@pytest.fixture
def config(monkeypatch):
    for key in os.environ:
        if key.startswith("COPILOT_"):
            monkeypatch.delenv(key)

    def make(**changes: Any) -> Settings:
        return Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            **{
                "llm_providers": ["openai"],
                "openai_detector_model": "gpt-6-luna",
                "openai_fast_model": "gpt-6-luna",
                "openai_fast_reasoning": "none",
                "openai_deep_model": "gpt-6.1-sol",
                "openai_deep_reasoning": "low",
                **changes,
            },
        )

    return make


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6.2-sol",
        "gpt-6.2-astra",
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6.2-sol-2026-11-01",
    ],
)
def test_supported_models_request_ultrafast_without_reducing_reasoning(config, model):
    router = build_router(config(openai_deep_model=model), [ScriptedProvider("openai")])
    for job in ("deep", "screen_code", "adaptive_expert"):
        route = router.routes[job][0]
        assert route.params["service_tier"] == "ultrafast"
        assert route.params["reasoning"] == {"effort": "low"}
        # Wire request must preserve the exact service tier through the provider adapter.
        provider = OpenAIProvider("openai", cast(Any, None))
        request = LLMRequest(system=[], content="Explain transaction isolation.", max_tokens=200)
        assert provider.request(route.model, request, route.params)["service_tier"] == "ultrafast"
        assert len(router.routes[job]) == 2
        assert router.routes[job][1].params["service_tier"] in ("priority", "fast")
    for job in ("fast", "deep_light", "screen", "detector", "summary"):
        assert router.routes[job][0].params.get("service_tier") != "ultrafast"


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "custom-model", None])
def test_unknown_or_unsupported_models_are_not_opted_into_ultrafast(model):
    assert fastest_deep_tier(model) is None


@pytest.mark.parametrize("tier", [None, "default", "fast", "ultrafast"])
def test_explicit_deep_tier_overrides_and_rollback(config, tier):
    settings = config(answer_architecture="legacy_dual", openai_deep_service_tier=tier)
    router = build_router(settings, [ScriptedProvider("openai")])
    assert "adaptive_expert" not in router.routes
    for job in ("deep", "screen_code"):
        assert router.routes[job][0].params.get("service_tier") == tier


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-6-luna", None),
        ("custom-screen", None),
        ("gpt-6.2-sol", "ultrafast"),
    ],
)
def test_screen_override_resolves_actual_model_capability(config, model, expected):
    router = build_router(config(screen_code_model=model), [ScriptedProvider("openai")])
    assert router.routes["screen_code"][0].model == model
    assert router.routes["screen_code"][0].params.get("service_tier") == expected


def test_explicit_adaptive_profile_can_restore_fast(config):
    settings = config(
        adaptive_expert_route={
            "model": "gpt-6.2-sol",
            "reasoning": "low",
            "service_tier": "fast",
            "retry_tier": "priority",
        }
    )
    router = build_router(settings, [ScriptedProvider("openai")])
    assert router.routes["adaptive_expert"][0].params["service_tier"] == "fast"


@pytest.mark.parametrize("fallback_fails", [False, True])
async def test_ultrafast_failure_has_only_one_fallback_and_records_served_tier(
    config, fallback_fails
):
    tiers = []

    class CapacityProvider:
        name = "openai"

        async def stream(self, model, req, params):
            tier = params["service_tier"]
            tiers.append(tier)
            if tier == "ultrafast" or fallback_fails:
                raise RuntimeError("Capacity unavailable")
            yield "A complete answer."
            yield LLMDone(LLMUsage(), "end_turn", model=model, service_tier=tier)

    router = build_router(config(), [CapacityProvider()])
    request = LLMRequest(system=[], content="Explain transaction isolation.", max_tokens=200)
    chunks = []

    async def consume():
        async for chunk in router.stream("adaptive_expert", request):
            chunks.append(chunk)

    if fallback_fails:
        with pytest.raises(AllRoutesFailedError):
            await consume()
        assert chunks == []
    else:
        await consume()
        assert isinstance(chunks[-1], LLMDone)
        assert chunks[-1].service_tier == "fast"
        assert chunks[-1].attempt == 1
    assert tiers == ["ultrafast", "fast"]
