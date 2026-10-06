"""Query embeddings (ARCHITECTURE.md, "Models and routing"): Cohere Embed v4 on Bedrock.

The gateway embeds the live question and compares it with the prepared answers of the session
pack; the pack builder embeds those prepared questions the same way, so the vectors compare.
"""

import asyncio
import json
from collections.abc import Sequence
from typing import Any, Protocol

from copilot_core.aws import boto3_client

EMBED_DIMENSIONS = 1024


class Embedder(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class BedrockCohereEmbedder:
    def __init__(self, model_id: str, region: str, client: Any = None) -> None:
        self.client: Any = client or boto3_client("bedrock-runtime", region_name=region)
        self.model_id = model_id

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        body = {
            "texts": list(texts),
            # Question to question similarity: both sides use the same input type.
            "input_type": "search_query",
            "embedding_types": ["float"],
            "output_dimension": EMBED_DIMENSIONS,
        }
        response = self.client.invoke_model(
            modelId=self.model_id,
            body=json.dumps(body),
            accept="application/json",
            contentType="application/json",
        )
        data = json.loads(response["body"].read())
        return [list(map(float, v)) for v in data["embeddings"]["float"]]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed, texts)
