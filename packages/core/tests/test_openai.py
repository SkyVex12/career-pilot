"""OpenAI pieces of the core: request content mapping, the prefix cache key, embeddings at
1024 dimensions, model names in pricing and routing."""

import base64
import json
from typing import Any

import httpx2
import openai
import pytest

from copilot_core.config import CoreSettings
from copilot_core.embed import OpenAIEmbedder, make_embedder
from copilot_core.llm import SystemBlock, model_family, openai_content, prompt_cache_key
from copilot_core.pricing import ModelPrice, cost_usd, missing_prices, model_key


def settings(**values: Any) -> CoreSettings:
    return CoreSettings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_content_blocks_map_to_responses_input() -> None:
    pdf = base64.standard_b64encode(b"%PDF-1.7 fake").decode()
    parts = openai_content(
        [
            {
                "type": "document",
                "title": "Resume",
                "source": {"type": "base64", "media_type": "application/pdf", "data": pdf},
            },
            {
                "type": "document",
                "title": "Job description",
                "source": {"type": "text", "media_type": "text/plain", "data": "Go </document>"},
            },
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
            },
            {"type": "text", "text": "Extract the facts."},
        ]
    )
    assert parts[0] == {
        "type": "input_file",
        "filename": "Resume.pdf",
        "file_data": f"data:application/pdf;base64,{pdf}",
    }
    # A text document is a labeled block it cannot close early.
    assert parts[1] == {
        "type": "input_text",
        "text": '<document title="Job description">\nGo </ document>\n</document>',
    }
    assert parts[2] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,AA==",
        "detail": "auto",
    }
    assert parts[3] == {"type": "input_text", "text": "Extract the facts."}
    assert openai_content("hi") == [{"type": "input_text", "text": "hi"}]
    with pytest.raises(ValueError, match="not supported"):
        openai_content([{"type": "tool_result"}])


def test_prompt_cache_key_covers_the_cached_prefix_only() -> None:
    rules, digest = SystemBlock("rules"), SystemBlock("<digest>a</digest>", cache=True)
    key = prompt_cache_key([rules, digest, SystemBlock("tail")])
    assert key is not None
    assert "digest" not in key
    assert prompt_cache_key([rules, digest]) == key  # blocks after the breakpoint don't count
    assert prompt_cache_key([rules, SystemBlock("<digest>b</digest>", cache=True)]) != key
    assert prompt_cache_key([rules]) is None


async def test_openai_embedder_asks_for_1024_dimensions() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        data = [
            {"object": "embedding", "index": i, "embedding": [float(i)] * 1024}
            for i in reversed(range(len(body["input"])))  # out of order on purpose
        ]
        usage = {"prompt_tokens": 4, "total_tokens": 4}
        return httpx2.Response(
            200, json={"object": "list", "data": data, "model": body["model"], "usage": usage}
        )

    client = openai.AsyncOpenAI(
        api_key="k",
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    embedder = OpenAIEmbedder("text-embedding-3-large", client)
    assert embedder.model_id == "openai:text-embedding-3-large@1024"
    vectors = await embedder.embed(["a", "b"])
    assert [v[0] for v in vectors] == [0.0, 1.0]
    assert sent[0]["dimensions"] == 1024
    assert sent[0]["model"] == "text-embedding-3-large"


def test_make_embedder_picks_openai_with_only_an_openai_key() -> None:
    s = settings(
        llm_providers=["openai"], openai_api_key="k", openai_embed_model="text-embedding-3-small"
    )
    embedder = make_embedder(s)
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model_id == s.embedding_model_id == "openai:text-embedding-3-small@1024"
    # Idle connections stay open past the SDK's 5 s, so live questions skip a TLS handshake.
    pool = embedder.client._client._transport._pool  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue, reportUnknownMemberType]
    assert pool._keepalive_expiry == 120.0  # pyright: ignore[reportUnknownMemberType]
    no_key = s.model_copy(update={"openai_api_key": None})
    assert make_embedder(no_key) is None
    assert make_embedder(s.model_copy(update={"embedder": "none"})) is None


def test_openai_model_names_in_pricing_and_routing() -> None:
    assert model_key("gpt-6-luna-2026-09-01") == "gpt-6-luna"
    assert model_key("gpt-6.1-sol") == "gpt-6.1-sol"
    # Dots inside a name are kept, so another model is never mistaken for the refused one.
    assert model_family("gpt-6.1-sol") == "gpt-6.1-sol"
    assert model_family("gpt-6-luna") != model_family("gpt-6.1-sol")
    assert model_family("us.anthropic.claude-sonnet-5-5") == "claude-sonnet-5-5"
    prices = {"gpt-6-luna": ModelPrice(input=0.1, cached=0.01, output=0.5)}
    assert missing_prices(["gpt-6-luna", "gpt-6.1-sol"], prices) == ["gpt-6.1-sol"]
    # 1,568 input of which 1,565 cached, 71 output (the recorded fast pass).
    cost = cost_usd("gpt-6-luna", "standard", 1568, 1565, 71, prices, {"standard": 1.0})
    assert cost is not None
    assert float(cost) == pytest.approx((3 * 0.1 + 1565 * 0.01 + 71 * 0.5) / 1e6, abs=1e-6)


def test_openai_settings_need_models_and_drop_empty_values() -> None:
    s = settings(
        llm_providers="openai,anthropic",
        openai_fast_model="gpt-6-luna",
        openai_fast_reasoning="",
        openai_deep_model=" ",
    )
    assert s.llm_providers == ["openai", "anthropic"]
    assert s.openai_route("fast") == ("gpt-6-luna", None)
    with pytest.raises(ValueError, match="COPILOT_OPENAI_DEEP_MODEL"):
        s.openai_route("deep")
