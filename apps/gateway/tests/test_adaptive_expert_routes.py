"""Dedicated adaptive expert settings leave legacy/screen routes unchanged; no network."""

import json
from collections.abc import AsyncGenerator, Mapping
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from copilot_core.llm import LLMDone, LLMRequest, OpenAIProvider, Route, SystemBlock
from copilot_gateway.config import AdaptiveExpertRoute
from copilot_gateway.router import JOBS, build_router, build_routes, job_params


class InertProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        raise AssertionError("Route construction must not call a provider")
        yield "unreachable"


def configured(settings_factory: Any, **changes: Any) -> Any:
    return settings_factory(
        **{
            "answer_architecture": "adaptive",
            "adaptive_expert_route": None,
            "adaptive_technical_legacy": False,
            "openai_detector_model": "detector-fixture",
            "openai_detector_reasoning": "none",
            "openai_fast_model": "gpt-6-luna",
            "openai_fast_reasoning": "none",
            "openai_deep_model": "gpt-6.1-sol",
            "openai_deep_reasoning": "low",
            "openai_first_token_timeout_ms": 3000,
            "openai_deep_first_token_timeout_ms": 6000,
            "openai_fast_service_tier": None,
            "openai_deep_service_tier": None,
            "openai_fast_retry_tier": "priority",
            "openai_deep_retry_tier": "priority",
            "fast_hedge_ms": 1200,
            "deep_hedge_ms": 4500,
            **changes,
        }
    )


def profile(**changes: Any) -> dict[str, Any]:
    # Authored opt-in for equivalence to the frozen final B route, not a new default.
    return {
        "model": "gpt-6.2-sol",
        "reasoning": "low",
        "service_tier": "fast",
        "retry_tier": "fast",
        "first_token_timeout_ms": 20000,
        "hedge_ms": None,
        **changes,
    }


def routes_value(routes: list[Route]) -> list[tuple[Any, ...]]:
    return [
        (r.provider, r.model, deepcopy(r.params), r.refusal_only, r.first_token_timeout_s)
        for r in routes
    ]


@pytest.mark.parametrize(
    "architecture", ["legacy_dual", "adaptive", "adaptive_hybrid", "adaptive_on_demand"]
)
def test_default_none_has_no_new_route_or_hedge(settings_factory: Any, architecture: str) -> None:
    settings = configured(settings_factory, answer_architecture=architecture)
    router = build_router(settings, [InertProvider("openai")])
    assert settings.adaptive_expert_route is None
    assert set(router.routes) == set(JOBS)
    assert "adaptive_expert" not in router.hedge_after_s
    assert router.routes["deep"][0].model == "gpt-6.1-sol"
    assert router.routes["deep"][0].first_token_timeout_s == 6
    assert router.routes["deep"][1].params["service_tier"] == "priority"
    assert router.hedge_after_s["deep"] == 4.5


@pytest.mark.parametrize("providers", [("openai",), ("openai", "bedrock", "anthropic")])
def test_opt_in_preserves_every_original_route_and_does_not_mutate_settings(
    settings_factory: Any, providers: tuple[str, ...]
) -> None:
    supplied = [InertProvider(name) for name in providers]
    baseline = configured(settings_factory, adaptive_failure_recovery=True)
    settings = configured(
        settings_factory, adaptive_failure_recovery=True, adaptive_expert_route=profile()
    )
    before = settings.model_dump()
    base_router, router = build_router(baseline, supplied), build_router(settings, supplied)
    assert set(router.routes) == {*base_router.routes, "adaptive_expert"}
    for job, routes in base_router.routes.items():
        assert routes_value(list(router.routes[job])) == routes_value(list(routes))
    assert {k: v for k, v in router.hedge_after_s.items() if k != "adaptive_expert"} == (
        base_router.hedge_after_s
    )
    assert "adaptive_expert" not in router.hedge_after_s
    assert settings.model_dump() == before
    assert baseline.adaptive_expert_route is None


def test_legacy_rollback_ignores_retained_opt_in_including_warm_targets(
    settings_factory: Any,
) -> None:
    provider = InertProvider("openai")
    baseline = configured(settings_factory, answer_architecture="legacy_dual")
    retained = configured(
        settings_factory,
        answer_architecture="legacy_dual",
        adaptive_expert_route=profile(hedge_ms=500),
    )
    first, second = build_router(baseline, [provider]), build_router(retained, [provider])
    assert first.routes == second.routes
    assert first.hedge_after_s == second.hedge_after_s
    assert "adaptive_expert" not in second.routes


def test_explicit_profile_has_same_model_wire_params_and_deadline_as_final_b_deep(
    settings_factory: Any,
) -> None:
    provider = InertProvider("openai")
    scoped = build_router(configured(settings_factory, adaptive_expert_route=profile()), [provider])
    prior = build_router(
        configured(
            settings_factory,
            openai_deep_model="gpt-6.2-sol",
            openai_deep_reasoning="low",
            openai_deep_service_tier="fast",
            openai_deep_retry_tier="fast",
            openai_deep_first_token_timeout_ms=20000,
            deep_hedge_ms=None,
        ),
        [provider],
    )
    assert routes_value(list(scoped.routes["adaptive_expert"])) == routes_value(
        list(prior.routes["deep"])
    )
    serializer = object.__new__(OpenAIProvider)
    request = LLMRequest([SystemBlock("Authored fixed prefix", cache=True)], "Question", 1200)
    for actual, expected in zip(
        scoped.routes["adaptive_expert"], prior.routes["deep"], strict=True
    ):
        assert serializer.request(actual.model, request, actual.params) == serializer.request(
            expected.model, request, expected.params
        )
    assert "adaptive_expert" not in scoped.hedge_after_s
    assert "deep" not in prior.hedge_after_s
    # The old global deep and screenshot routes were not reconfigured for this opt-in.
    assert scoped.routes["deep"][0].model == scoped.routes["screen_code"][0].model == "gpt-6.1-sol"


def test_null_knobs_are_explicit_not_global_deep_inheritance(settings_factory: Any) -> None:
    settings = configured(
        settings_factory,
        adaptive_expert_route={"model": "authored-expert"},
        openai_deep_service_tier="priority",
        openai_deep_reasoning="high",
        openai_deep_retry_tier="fast",
        deep_hedge_ms=400,
    )
    router = build_router(settings, [InertProvider("openai")])
    primary, retry = router.routes["adaptive_expert"]
    assert primary.params == retry.params == {}
    assert primary.first_token_timeout_s is retry.first_token_timeout_s is None
    assert router.first_token_timeout_s == settings.first_token_timeout_ms / 1000
    assert "adaptive_expert" not in router.hedge_after_s
    assert router.routes["deep"][0].params["service_tier"] == "priority"


def test_explicit_hedge_and_retry_tier_do_not_alter_primary(settings_factory: Any) -> None:
    settings = configured(
        settings_factory,
        adaptive_expert_route=profile(
            reasoning="none", service_tier="default", retry_tier="priority", hedge_ms=900
        ),
    )
    router = build_router(settings, [InertProvider("openai")])
    primary, retry = router.routes["adaptive_expert"]
    assert primary.params == {"reasoning": {"effort": "none"}, "service_tier": "default"}
    assert retry.params == {"reasoning": {"effort": "none"}, "service_tier": "priority"}
    assert router.hedge_after_s["adaptive_expert"] == 0.9
    assert router.hedge_after_s["deep"] == 4.5


@pytest.mark.parametrize("order", [("bedrock", "openai", "anthropic"), ("anthropic", "openai")])
def test_nonopenai_fallback_policy_and_provider_order_are_preserved(
    settings_factory: Any, order: tuple[str, ...]
) -> None:
    providers = [InertProvider(name) for name in order]
    routes = build_routes(configured(settings_factory, adaptive_expert_route=profile()), providers)
    actual = routes["adaptive_expert"]
    expected = routes["deep"]
    assert [r.provider for r in actual] == [r.provider for r in expected]
    assert [r.refusal_only for r in actual] == [r.refusal_only for r in expected]
    for candidate, original in zip(actual, expected, strict=True):
        if candidate.provider.name != "openai":
            assert candidate == original
        else:
            assert candidate.model == "gpt-6.2-sol"
            assert candidate.params["service_tier"] == "fast"


def test_nested_environment_config_is_explicit_and_roundtrips(
    settings_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPILOT_ADAPTIVE_EXPERT_ROUTE", json.dumps(profile()))
    settings = settings_factory()
    assert settings.adaptive_expert_route == AdaptiveExpertRoute.model_validate(profile())
    assert settings.model_dump()["adaptive_expert_route"] == profile()
    # An explicit rollback wins over an environment opt-in.
    assert settings_factory(adaptive_expert_route=None).adaptive_expert_route is None


@pytest.mark.parametrize(
    "changes",
    [
        {"model": ""},
        {"model": " "},
        {"model": " padded"},
        {"model": 1},
        {"reasoning": "unexpected"},
        {"service_tier": "standard"},
        {"retry_tier": "auto"},
        {"first_token_timeout_ms": 0},
        {"first_token_timeout_ms": True},
        {"first_token_timeout_ms": 1.5},
        {"hedge_ms": -1},
        {"hedge_ms": True},
        {"hedge_ms": float("nan")},
        {"extra": "not allowed"},
    ],
)
def test_malformed_opt_in_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AdaptiveExpertRoute.model_validate(profile(**changes))


def test_missing_opt_in_cannot_silently_serialize_a_deep_override(settings_factory: Any) -> None:
    with pytest.raises(ValueError, match="not configured"):
        job_params(configured(settings_factory), "openai", "adaptive_expert")
