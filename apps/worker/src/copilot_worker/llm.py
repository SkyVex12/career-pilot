"""Model routes for background jobs (ARCHITECTURE.md, "Models and routing" and "Workers").

Opus for memory extraction; Sonnet for job descriptions, session packs and post-call outputs.
On Bedrock each job tries the Flex tier first and then Standard; the direct API, which has no
Flex tier, comes last on Standard. Thinking is adaptive (neither model accepts it off).

OpenAI routes take their model and reasoning effort per job from settings
(`COPILOT_OPENAI_<JOB>_MODEL`, `..._REASONING`), on the default tier; the Claude-only
settings (adaptive thinking, effort, Flex) never reach them.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from copilot_core.llm import LLMProvider, LLMRequest, ModelRouter, RefusedError, Route
from copilot_core.providers import openai_params
from copilot_worker.config import WorkerSettings

Family = Literal["sonnet", "opus"]
JOBS: dict[str, Family] = {
    "extract": "opus",  # resume to career facts
    "job": "sonnet",  # job description to company, title, summary
    "pack": "sonnet",  # session pack: ranked stories and prepared answers
    "postcall": "sonnet",  # summary, feedback, follow-up email, standup report
}


def _model(settings: WorkerSettings, provider: str, family: Family) -> str:
    if provider == "bedrock":
        return settings.bedrock_opus_model if family == "opus" else settings.bedrock_sonnet_model
    return settings.opus_model if family == "opus" else settings.sonnet_model


def build_routes(
    settings: WorkerSettings, providers: Sequence[LLMProvider]
) -> dict[str, list[Route]]:
    routes: dict[str, list[Route]] = {}
    for job, family in JOBS.items():
        effort = settings.extract_effort if job == "extract" else settings.background_effort
        params: dict[str, Any] = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
        job_routes: list[Route] = []
        for provider in providers:
            if provider.name == "openai":
                model, _ = settings.openai_route(job)
                job_routes.append(Route(provider, model, openai_params(settings, job)))
                continue
            model = _model(settings, provider.name, family)
            if provider.name == "bedrock" and settings.bedrock_background_tier:
                tier = {"extra_body": {"service_tier": settings.bedrock_background_tier}}
                job_routes.append(Route(provider, model, {**params, **tier}))
            job_routes.append(Route(provider, model, params))
        routes[job] = job_routes
    return routes


def build_router(settings: WorkerSettings, providers: Sequence[LLMProvider]) -> ModelRouter:
    return ModelRouter(
        build_routes(settings, providers),
        first_token_timeout_s=settings.llm_first_token_timeout_s,
    )


class ModelOutputError(Exception):
    pass


async def complete_json(router: ModelRouter, job: str, req: LLMRequest) -> dict[str, Any]:
    """One structured-output call; the text must be the JSON object the schema describes."""
    try:
        text, done = await router.complete(job, req)
    except RefusedError as e:
        raise ModelOutputError(str(e)) from e
    if done.stop_reason == "max_tokens":
        raise ModelOutputError(f"{job}: output hit max_tokens")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ModelOutputError(f"{job}: output is not JSON ({e})") from e
    if not isinstance(data, dict):
        raise ModelOutputError(f"{job}: output is not a JSON object")
    return cast(dict[str, Any], data)


def as_list(value: object) -> list[Any]:
    """A list from model output, or empty (the schema was checked, but stay defensive)."""
    return cast(list[Any], value) if isinstance(value, list) else []


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def obj(props: Mapping[str, Any]) -> dict[str, Any]:
    """A strict JSON schema object: every property required, nothing else allowed."""
    return {
        "type": "object",
        "properties": dict(props),
        "required": list(props),
        "additionalProperties": False,
    }


STR: dict[str, Any] = {"type": "string"}
STRS: dict[str, Any] = {"type": "array", "items": STR}


def data_block(tag: str, body: str) -> str:
    """Untrusted text in a labeled block that it cannot close early."""
    return f"<{tag}>\n{body.replace(f'</{tag}>', f'</ {tag}>')}\n</{tag}>"
