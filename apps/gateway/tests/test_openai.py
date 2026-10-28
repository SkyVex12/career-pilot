"""The OpenAI provider replayed from recorded Responses API streams (fixtures/openai_*.sse):
streaming, the detector's structured output, a refusal, a failure mid-stream that falls back
to the other provider, and usage and cost through a real session."""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from copilot_core import keys, prompts
from copilot_core.llm import (
    AllRoutesFailedError,
    AnthropicProvider,
    LLMDone,
    ModelRouter,
    OpenAIProvider,
    RefusedError,
    Route,
)
from copilot_core.pricing import cost_usd
from copilot_core.providers import openai_params
from copilot_gateway.config import Settings
from copilot_gateway.detector import Detector, parse_detection
from copilot_gateway.router import build_routes, job_params
from copilot_gateway.testing import GatewayServer, ProtocolClient

FIXTURES = Path(__file__).parent / "fixtures"

OPENAI_SETTINGS: dict[str, Any] = {
    "llm_providers": ["openai"],
    "openai_detector_model": "gpt-6-luna",
    "openai_detector_reasoning": "none",
    "openai_fast_model": "gpt-6-luna",
    "openai_fast_reasoning": "none",
    "openai_deep_model": "gpt-6.1-sol",
    "openai_deep_reasoning": "low",
    "model_prices": {
        "gpt-6-luna": {"input": 0.1, "cached": 0.01, "output": 0.5},
        "gpt-6.1-sol": {"input": 2, "cached": 0.1, "output": 10},
    },
}


def openai_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**OPENAI_SETTINGS, **overrides})  # pyright: ignore[reportCallIssue]


def events(name: str) -> list[dict[str, Any]]:
    text = (FIXTURES / name).read_text("utf-8")
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def expected(name: str) -> dict[str, Any]:
    """What a recorded stream says: its text and its final usage."""
    evs = events(name)
    usage = evs[-1]["response"]["usage"]
    return {
        "text": "".join(e["delta"] for e in evs if e["type"] == "response.output_text.delta"),
        "input": usage["input_tokens"],
        "cached": usage["input_tokens_details"]["cached_tokens"],
        "output": usage["output_tokens"],
    }


def fixture_for(body: dict[str, Any]) -> str:
    if body.get("text", {}).get("format", {}).get("type") == "json_schema":
        return "openai_detector.sse"
    return "openai_deep.sse" if body["model"] == "gpt-6.1-sol" else "openai_fast.sse"


def openai_client(
    captured: list[dict[str, Any]], pick: Callable[[dict[str, Any]], str] = fixture_for
) -> openai.AsyncOpenAI:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        captured.append({"headers": dict(request.headers), "json": body})
        content = (FIXTURES / pick(body)).read_bytes()
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return openai.AsyncOpenAI(api_key="test-key", http_client=http, max_retries=0)


def claude_text() -> str:
    text = ""
    for line in (FIXTURES / "anthropic_fast.sse").read_text("utf-8").splitlines():
        if line.startswith("data:"):
            event = json.loads(line[5:])
            if event["type"] == "content_block_delta" and event["delta"]["type"] == "text_delta":
                text += event["delta"]["text"]
    return text


def claude() -> AnthropicProvider:
    body = (FIXTURES / "anthropic_fast.sse").read_bytes()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    client = anthropic.AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)
    return AnthropicProvider("anthropic", client)


def ctx() -> prompts.PromptContext:
    return prompts.PromptContext("Alex", "interview", "# Career digest", "Mode: interview")


async def test_openai_streams_fixture_with_the_same_prompt_blocks() -> None:
    captured: list[dict[str, Any]] = []
    provider = OpenAIProvider("openai", openai_client(captured))
    s = openai_settings()
    req = prompts.fast_pass(ctx(), "[00:01] Them: hi", "How did you scale?", "technical", 250)
    chunks = [c async for c in provider.stream("gpt-6-luna", req, job_params(s, "openai", "fast"))]
    deltas = [c for c in chunks if isinstance(c, str)]
    done = chunks[-1]
    assert isinstance(done, LLMDone)
    want = expected("openai_fast.sse")
    assert len(deltas) > 10  # streamed, not one block
    assert "".join(deltas) == want["text"]
    assert done.stop_reason == "end_turn"
    assert done.service_tier == "standard"  # OpenAI's "default" tier
    # Usage: input counts cached tokens, as LLMUsage expects; the recording hit the cache.
    assert done.usage.input_tokens == want["input"]
    assert done.usage.cached_tokens == want["cached"] > 0
    assert done.usage.output_tokens == want["output"]

    sent = captured[0]["json"]
    assert captured[0]["headers"]["authorization"] == "Bearer test-key"
    assert sent["model"] == "gpt-6-luna"
    assert sent["stream"] is True
    assert sent["store"] is False
    assert sent["max_output_tokens"] == 250
    assert sent["reasoning"] == {"effort": "none"}
    # No Claude-only settings reach OpenAI.
    for claude_only in ("thinking", "output_config", "service_tier", "fallbacks", "system"):
        assert claude_only not in sent
    # Block order as for Claude: rules, <digest>, <session_pack> first, byte for byte, then
    # the user turn; the prefix key is a hash of the cached blocks.
    developer, user = sent["input"]
    assert developer["role"] == "developer"
    assert [p["text"] for p in developer["content"]] == [b.text for b in req.system]
    assert developer["content"][1]["text"].startswith("<digest>")
    assert developer["content"][2]["text"].startswith("<session_pack>")
    assert user["content"] == [{"type": "input_text", "text": req.content}]
    assert sent["prompt_cache_key"].startswith("pfx-")
    other = prompts.fast_pass(ctx(), "[00:09] Them: so", "Why Go?", "technical", 250)
    assert provider.request("gpt-6-luna", other, {})["prompt_cache_key"] == sent["prompt_cache_key"]


async def test_openai_deep_pass_reasoning_gets_output_headroom() -> None:
    captured: list[dict[str, Any]] = []
    provider = OpenAIProvider("openai", openai_client(captured))
    s = openai_settings()
    req = prompts.deep_pass(ctx(), "", "How did you scale?", "technical", 800)
    params = job_params(s, "openai", "deep")
    assert params == {"reasoning": {"effort": "low"}, "max_output_tokens_extra": 2048}
    chunks = [c async for c in provider.stream("gpt-6.1-sol", req, params)]
    sent = captured[0]["json"]
    assert sent["reasoning"] == {"effort": "low"}
    assert sent["max_output_tokens"] == 800 + 2048
    assert "max_output_tokens_extra" not in sent
    assert "".join(c for c in chunks if isinstance(c, str)) == expected("openai_deep.sse")["text"]


async def test_openai_detector_structured_output() -> None:
    captured: list[dict[str, Any]] = []
    s = openai_settings()
    router = ModelRouter(
        build_routes(s, [OpenAIProvider("openai", openai_client(captured))]),
        first_token_timeout_s=1,
    )
    detection = await Detector(router, "Alex", 80).detect("How did you scale the ledger?", "")
    # The same parsing and validation as for Claude's JSON output.
    parsed = parse_detection(expected("openai_detector.sse")["text"])
    assert detection == replace(parsed, latency_ms=detection.latency_ms)
    assert detection.error is None
    assert detection.fires()
    fmt = captured[0]["json"]["text"]["format"]
    assert fmt == {
        "type": "json_schema",
        "name": "output",
        "schema": prompts.DETECTOR_SCHEMA,
        "strict": True,
    }
    assert captured[0]["json"]["max_output_tokens"] == 80


async def test_openai_refusal_moves_to_the_next_model_or_answers_refused() -> None:
    def refuse(body: dict[str, Any]) -> str:
        return "openai_refusal.sse"

    refusing = OpenAIProvider("openai", openai_client([], refuse))
    req = prompts.fast_pass(ctx(), "", "How did you scale?", "technical", 250)
    [done] = [c async for c in refusing.stream("gpt-6-luna", req, {})]
    assert isinstance(done, LLMDone)  # the refusal text is never streamed as an answer
    assert done.stop_reason == "refusal"

    # OpenAI first, Claude next: the refusal falls back to the other model.
    s = openai_settings(llm_providers=["openai", "anthropic"])
    hooks: list[str] = []
    router = ModelRouter(
        build_routes(s, [refusing, claude()]),
        first_token_timeout_s=1,
        on_fallback=lambda job, route, reason: hooks.append(f"{route.label} {reason}"),
    )
    text, final = await router.complete("fast", req)
    assert text == claude_text()
    assert (final.provider, final.model) == ("anthropic", "claude-haiku-4-5")
    # The retry route runs the same model, so it is skipped after the refusal.
    assert hooks == ["openai:gpt-6-luna refusal"]

    # OpenAI alone: every route refused, so the caller answers `refused`.
    alone = ModelRouter(build_routes(openai_settings(), [refusing]), first_token_timeout_s=1)
    with pytest.raises(AllRoutesFailedError) as info:
        await alone.complete("fast", req)
    assert info.value.refused
    with pytest.raises(AllRoutesFailedError):
        await alone.complete("deep", req)  # another model, but it refuses too


async def test_openai_refusal_after_text_reaches_the_caller() -> None:
    # A refusal output item after streamed text: the router passes the stop reason on.
    evs = events("openai_fast.sse")
    final = evs[-1]
    final["response"]["output"][0]["content"].append({"type": "refusal", "refusal": "No."})
    body = "".join(
        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in [*evs[:-1], final]
    ).encode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    client = openai.AsyncOpenAI(
        api_key="k",
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    router = ModelRouter(
        {"fast": [Route(OpenAIProvider("openai", client), "gpt-6-luna")]}, first_token_timeout_s=1
    )
    with pytest.raises(RefusedError):
        await router.complete("fast", prompts.fast_pass(ctx(), "", "q?", "other", 250))


async def test_openai_failure_mid_stream_falls_back_to_claude() -> None:
    # The response opens (created, in_progress) and then fails before any text: the next
    # route runs. A failure after text had streamed would be final (the user saw the text).
    def failing(body: dict[str, Any]) -> str:
        return "openai_failed.sse"

    calls: list[dict[str, Any]] = []
    s = openai_settings(llm_providers=["openai", "anthropic"])
    hooks: list[str] = []
    router = ModelRouter(
        build_routes(
            s,
            [
                OpenAIProvider("openai", openai_client(calls, failing)),
                claude(),
            ],
        ),
        first_token_timeout_s=1,
        on_fallback=lambda job, route, reason: hooks.append(f"{route.label} {reason}"),
    )
    req = prompts.fast_pass(ctx(), "", "How did you scale?", "technical", 250)
    chunks = [c async for c in router.stream("fast", req)]
    done = chunks[-1]
    assert isinstance(done, LLMDone)
    assert "".join(c for c in chunks if isinstance(c, str)) == claude_text()
    assert (done.provider, done.attempt) == ("anthropic", 2)  # after OpenAI and its retry
    assert len(calls) == 2
    assert hooks == [
        "openai:gpt-6-luna OpenAIResponseError: response failed: server_error",
        "openai:gpt-6-luna OpenAIResponseError: response failed: server_error",
    ]


def test_openai_routes_need_their_models() -> None:
    class P:
        name = "openai"

    s = openai_settings(openai_fast_model=None)
    with pytest.raises(ValueError, match="COPILOT_OPENAI_FAST_MODEL"):
        build_routes(s, [P()])  # pyright: ignore[reportArgumentType]
    routes = build_routes(openai_settings(), [P()])  # pyright: ignore[reportArgumentType]
    assert [r.label for r in routes["deep"]] == ["openai:gpt-6.1-sol", "openai:gpt-6.1-sol"]
    assert openai_params(openai_settings(openai_fast_reasoning=None), "fast") == {}
    assert all(r.first_token_timeout_s is None for rs in routes.values() for r in rs)
    s = openai_settings(openai_deep_first_token_timeout_ms=6000)
    assert [r.first_token_timeout_s for r in build_routes(s, [P()])["deep"]] == [6.0, 6.0]  # pyright: ignore[reportArgumentType]
    assert all(r.first_token_timeout_s is None for r in build_routes(s, [P()])["fast"])  # pyright: ignore[reportArgumentType]
    s = s.model_copy(update={"openai_first_token_timeout_ms": 3000})
    for job in ("detector", "fast"):
        assert [r.first_token_timeout_s for r in build_routes(s, [P()])[job]] == [3.0, 3.0]  # pyright: ignore[reportArgumentType]


async def test_openai_session_records_model_tokens_and_cost(env) -> None:
    # The whole gateway on OpenAI only: a typed question streams the fast pass, expand the
    # deep pass; the log carries provider, model, tier, tokens and cost from config prices.
    captured: list[dict[str, Any]] = []
    env.providers = [OpenAIProvider("openai", openai_client(captured))]
    settings = env.settings(**OPENAI_SETTINGS)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "How did you scale the ledger?"})
        q = await c.wait_type("question.detected")
        fast = await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await c.send({"type": "answer.expand", "question_id": q["question_id"]})
        deep = await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
        await c.send({"type": "session.end"})
        await c.closed()
    for done, name in ((fast, "openai_fast.sse"), (deep, "openai_deep.sse")):
        want = expected(name)
        assert done["source"] == "model"
        assert done["usage"] == {
            "input_tokens": want["input"],
            "cached_tokens": want["cached"],
            "output_tokens": want["output"],
        }
        deltas = [
            m["text"]
            for m in c.of_type("answer.delta")
            if m["question_id"] == q["question_id"] and m["pass"] == done["pass"]
        ]
        assert "".join(deltas) == want["text"]

    def prewarm(body: dict[str, Any]) -> bool:
        return bool(body["json"].get("prompt_cache_options", {}).get("prewarm"))

    # The session wrote the fast pass's prompt cache first, then the two passes ran.
    [warm] = [b["json"] for b in captured if prewarm(b)]
    assert warm["model"] == "gpt-6-luna"
    assert any("prompt_cache_breakpoint" in p for p in warm["input"][0]["content"])
    streamed = [b["json"] for b in captured if not prewarm(b)]
    assert [b["model"] for b in streamed] == ["gpt-6-luna", "gpt-6.1-sol"]
    # Both requests mark the end of the cached prefix the same way the prewarm did.
    assert streamed[0]["input"][0] == warm["input"][0]
    log = [json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(c.session_id))]
    answers = {e["pass"]: e for e in log if e["kind"] == "answer"}
    assert answers["fast"]["provider"] == "openai"
    assert answers["fast"]["model"] == "gpt-6-luna"
    assert answers["deep"]["model"] == "gpt-6.1-sol"
    for entry in answers.values():
        assert entry["tier"] == "standard"
        price = cost_usd(
            entry["model"], entry["tier"], entry["input_tokens"], entry["cached_tokens"],
            entry["output_tokens"], settings.model_prices, settings.tier_price_multipliers,
        )  # fmt: skip
        assert price is not None
        assert entry["cost_usd"] == float(price) > 0


def test_the_detector_hedge_runs_on_the_priority_tier() -> None:
    # 2026-10-27: the hedge and the retry went to the same model and tier, so a slow spell on
    # gpt-6-luna failed 10 detector decisions in 50 s.
    provider = OpenAIProvider("openai", openai_client([]))
    routes = build_routes(openai_settings(), [provider])
    first, second = routes["detector"]
    assert "service_tier" not in first.params
    assert second.params["service_tier"] == "priority"
    req = prompts.detector("Alex", "", "Any blockers?", 80, "standup")
    assert provider.request(second.model, req, second.params)["service_tier"] == "priority"
    assert all("service_tier" not in r.params for r in routes["fast"])
    off = build_routes(openai_settings(openai_detector_retry_tier=None), [provider])
    assert all("service_tier" not in r.params for r in off["detector"])
