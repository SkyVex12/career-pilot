"""Model routes for the gateway's jobs (ARCHITECTURE.md, "Models and routing", "Failure modes").

Failure row "Bedrock throttling or errors": retry once through the `us.` cross-region profile,
then the direct Anthropic API. Generalised: the first configured provider gets two routes (the
primary model id, then its retry id), each later provider one. Either provider can be used
alone, so Stage 0 can start with only one set of credentials.
"""

import logging
from collections.abc import Sequence
from typing import Any, Literal

import anthropic
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle

from copilot_core.llm import AnthropicProvider, FallbackHook, LLMProvider, ModelRouter, Route
from copilot_gateway.config import Settings

log = logging.getLogger(__name__)

Family = Literal["haiku", "sonnet"]
JOBS: dict[str, Family] = {"detector": "haiku", "fast": "haiku", "deep": "sonnet"}
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_providers(settings: Settings) -> list[LLMProvider]:
    timeout = anthropic.Timeout(30.0, connect=2.0)
    providers: list[LLMProvider] = []
    for name in settings.llm_providers:
        if name == "bedrock":
            key = settings.bedrock_api_key.get_secret_value() if settings.bedrock_api_key else None
            client = AsyncAnthropicBedrockMantle(
                aws_region=settings.aws_region, api_key=key, max_retries=0, timeout=timeout
            )
            providers.append(AnthropicProvider("bedrock", client))
        elif settings.anthropic_api_key is None:
            log.warning("ANTHROPIC_API_KEY is not set; skipping the direct Anthropic API route")
        else:
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                max_retries=0,
                timeout=timeout,
            )
            providers.append(AnthropicProvider("anthropic", client))
    return providers


def _model_ids(settings: Settings, provider: str, family: Family) -> tuple[str, str]:
    if provider == "bedrock":
        if family == "haiku":
            primary = settings.bedrock_haiku_model
            return primary, settings.bedrock_haiku_retry_model or primary
        primary = settings.bedrock_sonnet_model
        return primary, settings.bedrock_sonnet_retry_model or primary
    model = settings.haiku_model if family == "haiku" else settings.sonnet_model
    return model, model


def job_params(settings: Settings, provider: str, job: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if job == "deep":
        # Sonnet 5.5 rejects thinking {type: "disabled"}; `between_tools` is its lowest
        # setting. Sonnet 5.5 has no Priority Tier, so the deep pass never sets service_tier.
        params["thinking"] = {"type": settings.deep_pass_thinking}
        params["output_config"] = {"effort": settings.deep_pass_effort}
        if provider == "anthropic":
            # Server-side refusal fallback; Claude API only (Bedrock: see build_routes).
            params["extra_headers"] = {"anthropic-beta": SERVER_FALLBACK_BETA}
            params["extra_body"] = {"fallbacks": "default"}
    if job == "fast" and provider == "anthropic":
        # Priority Tier is a direct-API flag for the Haiku fast pass only.
        params["service_tier"] = "auto" if settings.fast_pass_priority else "standard_only"
    return params


def build_routes(settings: Settings, providers: Sequence[LLMProvider]) -> dict[str, list[Route]]:
    routes: dict[str, list[Route]] = {}
    for job, family in JOBS.items():
        job_routes: list[Route] = []
        for i, provider in enumerate(providers):
            primary, retry = _model_ids(settings, provider.name, family)
            params = job_params(settings, provider.name, job)
            job_routes.append(Route(provider, primary, params))
            if i == 0:
                job_routes.append(Route(provider, retry, params))
            if job == "deep" and provider.name == "bedrock" and settings.bedrock_refusal_model:
                # What `fallbacks: "default"` does on the direct API: a refused Sonnet 5.5
                # request reruns on Sonnet 5 with thinking off.
                refusal_params = {
                    "thinking": {"type": "disabled"},
                    "output_config": {"effort": settings.deep_pass_effort},
                }
                job_routes.append(
                    Route(provider, settings.bedrock_refusal_model, refusal_params, True)
                )
        routes[job] = job_routes
    return routes


def build_router(
    settings: Settings,
    providers: Sequence[LLMProvider] | None = None,
    on_fallback: FallbackHook | None = None,
) -> ModelRouter:
    providers = make_providers(settings) if providers is None else providers
    return ModelRouter(
        build_routes(settings, providers),
        first_token_timeout_s=settings.first_token_timeout_ms / 1000,
        on_fallback=on_fallback,
    )
