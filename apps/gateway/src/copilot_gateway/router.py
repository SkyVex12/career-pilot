"""Model routes for the gateway's jobs (ARCHITECTURE.md, "Models and routing", "Failure modes").

Failure row "Bedrock throttling or errors": retry once through the `us.` cross-region profile,
then the direct Anthropic API. Generalised: the first configured provider gets two routes (the
primary model id, then its retry id), each later provider one. Any provider can be used
alone, so Stage 0 can start with only one set of credentials.

OpenAI routes take their model and reasoning effort per job from settings
(`COPILOT_OPENAI_<JOB>_MODEL`, `..._REASONING`); the Claude-only settings (between_tools
thinking, effort, `fallbacks: "default"`, Priority) never reach them.
The detector's and the fast pass's second OpenAI route (the hedge and the retry) runs on
OpenAI's own Priority tier (`openai_detector_retry_tier`, `openai_fast_retry_tier`), so a slow
spell on the first does not stall both. Both jobs hedge (`detector_hedge_ms`,
`fast_hedge_ms`): with no first token by then, the second route starts alongside the first.
"""

from collections.abc import Sequence
from typing import Any, Literal

from copilot_core.llm import FallbackHook, LLMProvider, ModelRouter, Route
from copilot_core.providers import make_providers, openai_params
from copilot_gateway.config import Settings

Family = Literal["haiku", "sonnet"]
# "summary" is the call's rolling summary (conversation memory): background, on the cheapest
# family, like the detector.
JOBS: dict[str, Family] = {
    "detector": "haiku", "fast": "haiku", "deep": "sonnet", "summary": "haiku",
}  # fmt: skip
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _model_ids(settings: Settings, provider: str, job: str, family: Family) -> tuple[str, str]:
    if provider == "openai":
        model, _ = settings.openai_route(job)
        return model, model
    if provider == "bedrock":
        if family == "haiku":
            primary = settings.bedrock_haiku_model
            return primary, settings.bedrock_haiku_retry_model or primary
        primary = settings.bedrock_sonnet_model
        return primary, settings.bedrock_sonnet_retry_model or primary
    model = settings.haiku_model if family == "haiku" else settings.sonnet_model
    return model, model


def job_params(settings: Settings, provider: str, job: str) -> dict[str, Any]:
    if provider == "openai":
        return openai_params(settings, job)
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
    retry_tiers = {
        "detector": settings.openai_detector_retry_tier,
        "fast": settings.openai_fast_retry_tier,
    }
    for job, family in JOBS.items():
        job_routes: list[Route] = []
        for i, provider in enumerate(providers):
            primary, retry = _model_ids(settings, provider.name, job, family)
            params = job_params(settings, provider.name, job)
            timeout = None
            if provider.name == "openai":
                timeout = settings.openai_first_token_timeout_s(job)
            job_routes.append(Route(provider, primary, params, first_token_timeout_s=timeout))
            if i == 0:
                retry_params = params
                tier = retry_tiers.get(job)
                if provider.name == "openai" and tier:
                    retry_params = {**params, "service_tier": tier}
                job_routes.append(
                    Route(provider, retry, retry_params, first_token_timeout_s=timeout)
                )
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
    hedges = {"detector": settings.detector_hedge_ms, "fast": settings.fast_hedge_ms}
    return ModelRouter(
        build_routes(settings, providers),
        first_token_timeout_s=settings.first_token_timeout_ms / 1000,
        on_fallback=on_fallback,
        hedge_after_s={job: ms / 1000 for job, ms in hedges.items() if ms} or None,
    )
