"""Claude provider clients from settings: the direct API and Bedrock (Mantle).

The gateway and the workers build their routes on top of these (copilot_gateway.router and
copilot_worker.llm); either provider can be used alone.
"""

import logging

import anthropic
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle

from copilot_core.config import CoreSettings
from copilot_core.llm import AnthropicProvider, LLMProvider

log = logging.getLogger(__name__)


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
