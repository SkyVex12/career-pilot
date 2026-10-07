"""Model provider clients from settings: Claude on Bedrock (Mantle) and the direct Anthropic
API, and OpenAI.

The gateway and the workers build their routes on top of these (copilot_gateway.router and
copilot_worker.llm); any provider can be used alone.
"""

import logging
from typing import Any

import anthropic
import openai
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle

from copilot_core.config import CoreSettings
from copilot_core.llm import REASONING_HEADROOM, AnthropicProvider, LLMProvider, OpenAIProvider

log = logging.getLogger(__name__)


def openai_client(settings: CoreSettings, timeout_s: float = 30.0) -> openai.AsyncOpenAI:
    assert settings.openai_api_key is not None
    return openai.AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        max_retries=0,  # the router moves on to the next route instead
        timeout=openai.Timeout(timeout_s, connect=2.0),
    )


def openai_params(settings: CoreSettings, job: str) -> dict[str, Any]:
    """An OpenAI route's request fields: the job's reasoning effort, if set, and output-token
    headroom for the reasoning."""
    _, effort = settings.openai_route(job)
    if effort is None:
        return {}
    params: dict[str, Any] = {"reasoning": {"effort": effort}}
    if effort != "none":
        params[REASONING_HEADROOM] = settings.openai_reasoning_headroom_tokens
    return params


def make_providers(settings: CoreSettings, timeout_s: float = 30.0) -> list[LLMProvider]:
    timeout = anthropic.Timeout(timeout_s, connect=2.0)
    providers: list[LLMProvider] = []
    for name in settings.llm_providers:
        if name == "bedrock":
            key = settings.bedrock_api_key.get_secret_value() if settings.bedrock_api_key else None
            client = AsyncAnthropicBedrockMantle(
                aws_region=settings.aws_region, api_key=key, max_retries=0, timeout=timeout
            )
            providers.append(AnthropicProvider("bedrock", client))
        elif name == "openai":
            if settings.openai_api_key is None:
                log.warning("OPENAI_API_KEY is not set; skipping the OpenAI route")
                continue
            providers.append(OpenAIProvider("openai", openai_client(settings, timeout_s)))
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
