"""Cache usage survives SDK parsing and prices only observed billing categories."""

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from openai.types.responses.response_usage import InputTokensDetails, ResponseUsage

from copilot_core.llm import (
    LLMDone,
    LLMRequest,
    LLMUsage,
    OpenAIProvider,
    SystemBlock,
    _openai_usage,
)


def usage_payload() -> dict[str, Any]:
    return {
        "input_tokens": 12000,
        "input_tokens_details": {
            "cached_tokens": 4000,
            "cache_write_tokens": 7000,
            "cache_write_tokens_details": {"ttl_30m_tokens": 3000, "ttl_12h_tokens": 4000},
        },
        "output_tokens": 20,
        "output_tokens_details": {"reasoning_tokens": 7},
        "total_tokens": 12020,
    }


def test_sdk_extra_ttl_fields_survive_without_sdk_upgrade() -> None:
    raw = usage_payload()
    sdk = ResponseUsage.model_validate(raw)
    assert isinstance(sdk.input_tokens_details, InputTokensDetails)
    assert (
        _openai_usage(sdk)
        == _openai_usage(raw)
        == LLMUsage(
            input_tokens=12000,
            cached_tokens=4000,
            output_tokens=20,
            cache_write_tokens=7000,
            cache_write_30m_tokens=3000,
            cache_write_12h_tokens=4000,
            reasoning_tokens=7,
        )
    )
    # A future SDK may turn the extra dict into a typed object; the parser accepts both.
    typed = SimpleNamespace(
        input_tokens=12000,
        output_tokens=20,
        output_tokens_details=SimpleNamespace(reasoning_tokens=7),
        input_tokens_details=SimpleNamespace(
            cached_tokens=4000,
            cache_write_tokens=7000,
            cache_write_tokens_details=SimpleNamespace(ttl_30m_tokens=3000, ttl_12h_tokens=4000),
        ),
    )
    assert _openai_usage(typed) == _openai_usage(raw)


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": 100}])
def test_absent_write_usage_remains_unknown(details: dict[str, Any] | None) -> None:
    usage = _openai_usage({"input_tokens": 1000, "input_tokens_details": details})
    assert usage.cache_write_tokens is None
    assert usage.cache_write_30m_tokens is None
    assert usage.cache_write_12h_tokens is None
    assert _openai_usage(None) == LLMUsage()


def test_aggregate_write_usage_does_not_invent_lifetime_from_default() -> None:
    usage = _openai_usage(
        {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1000},
        }
    )
    assert usage.cache_write_tokens == 1000
    assert usage.cache_write_30m_tokens is None
    assert usage.cache_write_12h_tokens is None


@pytest.mark.parametrize("invalid", [-1, True, "7000", 7000.5])
def test_invalid_optional_counters_do_not_become_observed_usage(invalid: Any) -> None:
    raw = usage_payload()
    raw["input_tokens_details"]["cache_write_tokens"] = invalid
    assert _openai_usage(raw).cache_write_tokens is None


@pytest.mark.parametrize("endpoint", ["stream", "prewarm", "decide"])
async def test_each_openai_entry_point_preserves_cache_write_usage(endpoint: str) -> None:
    sent: list[httpx2.Request] = []
    raw = usage_payload()
    if endpoint == "prewarm":
        raw["output_tokens"] = 0
        raw["total_tokens"] = raw["input_tokens"]
    response = {
        "id": "resp_cache_test",
        "object": "response",
        "created_at": 0,
        "model": "gpt-6-luna",
        "status": "completed",
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "service_tier": "fast",
        "usage": raw,
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if endpoint == "decide":
            return httpx2.Response(200, json={"answers": [], "model": "gpt-6-luna", "usage": raw})
        if endpoint == "prewarm":
            return httpx2.Response(200, json=response)
        event = {"type": "response.completed", "sequence_number": 0, "response": response}
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(event)}\n\n".encode(),
        )

    client = openai.AsyncOpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    async with client:
        provider = OpenAIProvider("openai", client)
        request = LLMRequest([SystemBlock("stable", cache=True)], "question", 50)
        if endpoint == "stream":
            chunks = [chunk async for chunk in provider.stream("gpt-6-luna", request, {})]
            done = chunks[-1]
            assert isinstance(done, LLMDone)
            assert done.service_tier == "fast"
            usage = done.usage
        elif endpoint == "prewarm":
            done = await provider.prewarm("gpt-6-luna", request, {})
            assert done is not None
            assert done.service_tier == "fast"
            usage = done.usage
        else:
            usage = (await provider.decide("gpt-6-luna", [], [])).usage
    assert len(sent) == 1
    assert usage == LLMUsage(
        input_tokens=12000,
        cached_tokens=4000,
        output_tokens=20 if endpoint == "stream" else 0,
        cache_write_tokens=7000,
        cache_write_30m_tokens=3000,
        cache_write_12h_tokens=4000,
        reasoning_tokens=7,
    )


def test_hidden_output_tokens_are_observed_without_increasing_billable_output() -> None:
    usage = _openai_usage(usage_payload())
    assert usage.output_tokens == 20
    assert usage.reasoning_tokens == 7
    raw = usage_payload()
    del raw["output_tokens_details"]
    assert _openai_usage(raw).reasoning_tokens is None
