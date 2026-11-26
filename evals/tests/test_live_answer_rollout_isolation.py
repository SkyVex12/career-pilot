"""Evaluation conditions must not silently inherit production rollout policies."""

import argparse
import json
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Any

import pytest
from copilot_evals import live_answer_replay as replay
from copilot_evals.live_answer_variants import load_variants

from copilot_core.llm import LLMDone, LLMRequest
from copilot_gateway.config import AdaptiveExpertRoute, Settings
from copilot_gateway.router import build_router


class InertProvider:
    name = "openai"

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[str | LLMDone]:
        raise AssertionError("Configuration validation must not call a provider")
        yield "unreachable"


def product_settings() -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] - BaseSettings runtime keyword.
        llm_providers=["openai"],
        answer_architecture="adaptive",
        openai_detector_model="detector-fixture",
        openai_fast_model="fast-fixture",
        openai_deep_model="global-deep-fixture",
        openai_deep_reasoning="medium",
        adaptive_expert_route=AdaptiveExpertRoute(
            model="isolated-expert-fixture",
            reasoning="low",
            service_tier="fast",
            retry_tier="fast",
            first_token_timeout_ms=20000,
        ),
        adaptive_technical_legacy=True,
    )


def manifest(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    path = tmp_path / "variants.json"
    path.write_text(json.dumps({"schema": 1, "variants": entries}), encoding="utf-8")
    return path


def test_cli_conditions_use_explicit_global_models_not_product_profile() -> None:
    base = product_settings()
    args = argparse.Namespace(
        architectures=["A", "B"],
        expert_model="cli-expert-fixture",
        expert_reasoning="high",
        expert_tier="priority",
    )
    configured = base.model_copy(update=replay.settings_overrides(args))
    conditions = replay.configured_conditions(args, configured, None)
    for variant in conditions.values():
        assert variant.settings.adaptive_expert_route is None
        assert not variant.settings.adaptive_technical_legacy
        router = build_router(variant.settings, [InertProvider()])
        replay.tier_overrides(router, args)
        assert "adaptive_expert" not in router.routes
        assert router.routes["deep"][0].model == "cli-expert-fixture"
        assert router.routes["deep"][0].params["reasoning"]["effort"] == "high"
        assert router.routes["deep"][0].params["service_tier"] == "priority"
        identity = variant.identity(replay.CONFIG_FIELDS)
        assert identity["settings"]["adaptive_expert_route"] is None
        assert identity["settings"]["adaptive_technical_legacy"] is False
    assert base.adaptive_expert_route is not None
    assert base.adaptive_technical_legacy


def test_schema_one_policy_is_per_condition_and_is_bound(tmp_path: Path) -> None:
    base = product_settings()
    assert base.adaptive_expert_route is not None
    path = manifest(
        tmp_path,
        [
            {
                "label": "baseline",
                "architecture": "adaptive",
                "settings": {
                    "openai_deep_model": "manifest-expert-fixture",
                    "openai_deep_reasoning": "high",
                },
            },
            {
                "label": "product",
                "architecture": "adaptive",
                "settings": {
                    "adaptive_expert_route": base.adaptive_expert_route.model_dump(mode="json"),
                    "adaptive_technical_legacy": True,
                },
            },
            {
                "label": "explicit-null",
                "architecture": "adaptive",
                "settings": {
                    "adaptive_expert_route": None,
                    "adaptive_technical_legacy": False,
                },
            },
        ],
    )
    variants = load_variants(path, base, tmp_path)
    baseline = variants["baseline"]
    router = build_router(baseline.settings, [InertProvider()])
    assert "adaptive_expert" not in router.routes
    assert router.routes["deep"][0].model == "manifest-expert-fixture"
    assert router.routes["deep"][0].params["reasoning"]["effort"] == "high"
    assert baseline.settings.adaptive_expert_route is None
    assert not baseline.settings.adaptive_technical_legacy
    assert variants["explicit-null"].settings.adaptive_expert_route is None
    promoted = variants["product"]
    router = build_router(promoted.settings, [InertProvider()])
    assert router.routes["adaptive_expert"][0].model == "isolated-expert-fixture"
    assert router.routes["adaptive_expert"][0].params["reasoning"]["effort"] == "low"
    assert router.routes["deep"][0].model == "global-deep-fixture"
    identity = promoted.identity(replay.CONFIG_FIELDS)["settings"]
    assert identity["adaptive_expert_route"]["model"] == "isolated-expert-fixture"
    assert identity["adaptive_technical_legacy"] is True
    assert base.adaptive_expert_route.model == "isolated-expert-fixture"


def test_cli_tier_overlay_revalidates_profiles_without_mutating_input(tmp_path: Path) -> None:
    base = product_settings()
    assert base.adaptive_expert_route is not None
    path = manifest(
        tmp_path,
        [
            {
                "label": "product",
                "architecture": "adaptive",
                "settings": {
                    "adaptive_expert_route": base.adaptive_expert_route.model_dump(mode="json"),
                },
            }
        ],
    )
    variants = load_variants(path, base, tmp_path)
    with pytest.raises(ValueError, match="profile cannot use eval expert route overrides"):
        replay.variants_with_cli_tiers(variants, argparse.Namespace(expert_tier="priority"))
    assert variants["product"].expert_tier is None
    updated = replay.variants_with_cli_tiers(variants, argparse.Namespace(fast_tier="fast"))
    assert updated["product"].fast_tier == "fast"
    assert variants["product"].fast_tier is None
    assert updated["product"] is not variants["product"]


async def test_execute_rejects_cli_profile_conflict_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = product_settings()
    assert base.adaptive_expert_route is not None
    path = manifest(
        tmp_path,
        [
            {
                "label": "product",
                "architecture": "adaptive",
                "settings": {
                    "adaptive_expert_route": base.adaptive_expert_route.model_dump(mode="json"),
                },
            }
        ],
    )
    seen: dict[str, Any] = {}

    def configuration(**kwargs: Any) -> Settings:
        seen.update(kwargs)
        return base.model_copy(update=kwargs)

    def no_providers(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Conflict must fail before provider creation")

    monkeypatch.setattr(replay, "Settings", configuration)
    monkeypatch.setattr(replay, "make_providers", no_providers)
    monkeypatch.setattr(replay, "load_cases", lambda: [])
    args = argparse.Namespace(timeout=90, cases=None, variants=path, expert_tier="priority")
    with pytest.raises(ValueError, match="profile cannot use eval expert route overrides"):
        await replay.execute(args)
    assert seen["adaptive_expert_route"] is None
    assert seen["adaptive_technical_legacy"] is False


async def test_offline_cli_preflight_uses_the_same_policy_as_live_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_build = build_router
    seen = []

    def checked_router(settings: Settings, providers: list[Any]) -> Any:
        assert settings.adaptive_expert_route is None
        assert settings.adaptive_technical_legacy is False
        router = actual_build(settings, providers)
        assert "adaptive_expert" not in router.routes
        assert router.routes["deep"][0].model == "offline-expert"
        seen.append(settings.answer_architecture)
        return router

    monkeypatch.setattr(replay, "build_router", checked_router)
    case = next(case for case in replay.load_cases() if case["type"] == "technical")
    await replay.preflight([case])
    assert seen == list(replay.STRATEGIES.values())
