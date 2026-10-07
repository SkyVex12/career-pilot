"""Query embeddings (ARCHITECTURE.md, "Models and routing"): Cohere Embed v4 on Bedrock, or
OpenAI embeddings; always 1024 dimensions, the size of `memory_nodes.embedding`.

The gateway embeds the live question and compares it with the prepared answers of the session
pack; the pack builder embeds those prepared questions the same way, so the vectors compare.
Vectors only compare when the same model made them: every embedder has a `model_id`
(`provider:model@dimensions`, as `CoreSettings.embedding_model_id` names it), packs and
memory nodes store it next to their vectors, and vectors with another id are never compared.
"""

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any, Protocol

import openai

from copilot_core.aws import boto3_client
from copilot_core.config import EMBED_DIMENSIONS, CoreSettings
from copilot_core.providers import openai_client

log = logging.getLogger(__name__)


class Embedder(Protocol):
    model_id: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class BedrockCohereEmbedder:
    def __init__(self, model_id: str, region: str, client: Any = None) -> None:
        self.client: Any = client or boto3_client("bedrock-runtime", region_name=region)
        self.model = model_id
        self.model_id = f"bedrock:{model_id}@{EMBED_DIMENSIONS}"

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        body = {
            "texts": list(texts),
            # Question to question similarity: both sides use the same input type.
            "input_type": "search_query",
            "embedding_types": ["float"],
            "output_dimension": EMBED_DIMENSIONS,
        }
        response = self.client.invoke_model(
            modelId=self.model,
            body=json.dumps(body),
            accept="application/json",
            contentType="application/json",
        )
        data = json.loads(response["body"].read())
        return [list(map(float, v)) for v in data["embeddings"]["float"]]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed, texts)


class OpenAIEmbedder:
    """OpenAI embeddings shortened to 1024 dimensions with the `dimensions` parameter (the
    text-embedding-3 models support it)."""

    def __init__(self, model: str, client: openai.AsyncOpenAI) -> None:
        self.client = client
        self.model = model
        self.model_id = f"openai:{model}@{EMBED_DIMENSIONS}"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self.client.embeddings.create(
            model=self.model,
            input=list(texts),
            dimensions=EMBED_DIMENSIONS,
            encoding_format="float",
        )
        data = sorted(response.data, key=lambda d: d.index)
        vectors = [list(map(float, d.embedding)) for d in data]
        if any(len(v) != EMBED_DIMENSIONS for v in vectors):
            raise ValueError(f"{self.model} did not return {EMBED_DIMENSIONS} dimensions")
        return vectors


def make_embedder(settings: CoreSettings, timeout_s: float = 10.0) -> Embedder | None:
    """The configured embedder, or None (prepared-answer matching off)."""
    provider = settings.embedding_provider
    if provider == "bedrock":
        return BedrockCohereEmbedder(settings.embed_model, settings.aws_region)
    if provider == "openai":
        if settings.openai_api_key is None:
            log.warning("OPENAI_API_KEY is not set; no OpenAI embeddings")
            return None
        assert settings.openai_embed_model is not None
        return OpenAIEmbedder(settings.openai_embed_model, openai_client(settings, timeout_s))
    return None
