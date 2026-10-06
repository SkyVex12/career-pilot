"""Provider adapters replayed from recorded wire fixtures: the Anthropic SDK stream and the
Deepgram WebSocket."""

import asyncio
import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
from websockets.asyncio.server import ServerConnection, serve

from copilot_core import prompts
from copilot_core.llm import AnthropicProvider, LLMDone, Route
from copilot_gateway.config import Settings
from copilot_gateway.router import build_routes, job_params
from copilot_gateway.stt import ResilientSTT, TimedResult
from copilot_gateway.stt.deepgram import DeepgramSTT, parse_message

FIXTURES = Path(__file__).parent / "fixtures"


def anthropic_client(captured: list[dict[str, Any]]) -> anthropic.AsyncAnthropic:
    body = (FIXTURES / "anthropic_fast.sse").read_bytes()

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append({"headers": dict(request.headers), "json": json.loads(request.content)})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return anthropic.AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)


async def test_anthropic_provider_streams_fixture_and_sends_cached_prefix() -> None:
    captured: list[dict[str, Any]] = []
    provider = AnthropicProvider("anthropic", anthropic_client(captured))
    ctx = prompts.PromptContext("Alex", "interview", "# Career digest", "Mode: interview")
    req = prompts.fast_pass(ctx, "[00:01] Them: hi", "How did you scale?", "technical", 250)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    params = job_params(settings, "anthropic", "fast")
    chunks = [c async for c in provider.stream("claude-haiku-4-5", req, params)]
    text = "".join(c for c in chunks if isinstance(c, str))
    done = chunks[-1]
    assert isinstance(done, LLMDone)
    assert text.startswith("- Led the payments migration")
    assert done.stop_reason == "end_turn"
    assert done.usage.cached_tokens == 4180
    assert done.usage.input_tokens == 214 + 4180
    assert done.usage.output_tokens == 27

    sent = captured[0]["json"]
    assert sent["model"] == "claude-haiku-4-5"
    assert sent["max_tokens"] == 250
    assert sent["stream"] is True
    assert sent["service_tier"] == "standard_only"
    system = sent["system"]
    # Block order: rules, <digest> (breakpoint), <session_pack> (breakpoint).
    assert system[0]["text"].startswith("You help Alex answer a question live in a interview")
    assert "cache_control" not in system[0]
    assert system[1]["text"].startswith("<digest>")
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert system[2]["text"].startswith("<session_pack>")
    assert system[2]["cache_control"] == {"type": "ephemeral"}
    user = sent["messages"][0]["content"]
    assert user.index("<transcript>") < user.index("Question (technical)")


async def test_deep_route_params() -> None:
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    direct = job_params(s, "anthropic", "deep")
    assert direct["thinking"] == {"type": "between_tools"}
    assert direct["output_config"] == {"effort": "low"}
    assert direct["extra_body"] == {"fallbacks": "default"}
    bedrock = job_params(s, "bedrock", "deep")
    assert "extra_body" not in bedrock
    assert job_params(s, "bedrock", "fast") == {}


async def test_route_order_for_each_provider_setup() -> None:
    class P:
        def __init__(self, name: str) -> None:
            self.name = name

    s = Settings(_env_file=None, bedrock_haiku_retry_model="us.anthropic.claude-haiku-4-5")  # pyright: ignore[reportCallIssue]
    routes = build_routes(s, [P("bedrock"), P("anthropic")])  # pyright: ignore[reportArgumentType]
    assert [r.label for r in routes["fast"]] == [
        "bedrock:anthropic.claude-haiku-4-5",
        "bedrock:us.anthropic.claude-haiku-4-5",
        "anthropic:claude-haiku-4-5",
    ]
    assert [r.label for r in routes["deep"]][-1] == "anthropic:claude-sonnet-5-5"
    only_direct = build_routes(s, [P("anthropic")])  # pyright: ignore[reportArgumentType]
    assert [r.label for r in only_direct["detector"]] == [
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-haiku-4-5",
    ]
    assert isinstance(routes["fast"][0], Route)


def test_deepgram_parse_message() -> None:
    fixture = json.loads((FIXTURES / "deepgram_session.json").read_text())
    assert parse_message(fixture["on_audio"][0]) is None  # Metadata
    interim = parse_message(fixture["on_audio"][2])
    assert interim is not None
    assert (interim.text, interim.is_final, interim.end_ms) == (
        "how did you scale the ledger",
        False,
        2100,
    )
    final = parse_message(fixture["on_finalize"][0])
    assert final is not None
    assert final.is_final
    assert final.flushed


async def test_deepgram_stream_against_replayed_server() -> None:
    fixture = json.loads((FIXTURES / "deepgram_session.json").read_text())
    seen: dict[str, Any] = {"audio": 0, "control": []}

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["path"] = ws.request.path
        sent_audio_replies = False
        async for message in ws:
            if isinstance(message, bytes):
                seen["audio"] += len(message)
                if not sent_audio_replies:
                    sent_audio_replies = True
                    for m in fixture["on_audio"]:
                        await ws.send(json.dumps(m))
                continue
            control = json.loads(message)
            seen["control"].append(control["type"])
            if control["type"] == "Finalize":
                for m in fixture["on_finalize"]:
                    await ws.send(json.dumps(m))
            if control["type"] == "CloseStream":
                await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        results: list[TimedResult] = []
        statuses: list[str] = []

        async def on_result(r: TimedResult) -> None:
            results.append(r)

        def factory() -> DeepgramSTT:
            return DeepgramSTT("dg-key", url=f"ws://127.0.0.1:{port}/v1/listen", model="nova-3")

        stt = ResilientSTT([("deepgram", factory)], on_result, lambda s, m: statuses.append(s))
        task = asyncio.create_task(stt.run())
        await asyncio.sleep(0.2)
        stt.speech(True)
        # Speech starts 5 s into the call: provider times map back to capture times.
        await stt.send(b"\0" * 3200 * 10, capture_ms=5000)
        stt.speech(False)
        await stt.finalize()
        for _ in range(100):
            if any(r.flushed for r in results):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert seen["auth"] == "Token dg-key"
    assert "model=nova-3" in seen["path"]
    assert "encoding=linear16" in seen["path"]
    assert "interim_results=true" in seen["path"]
    assert seen["audio"] == 32000
    assert "Finalize" in seen["control"]
    final = next(r for r in results if r.flushed)
    assert final.text == "How did you scale the ledger?"
    assert (final.start_ms, final.end_ms) == (5000, 7400)
    assert [r.text for r in results if not r.is_final] == [
        "how did you",
        "how did you scale the ledger",
    ]
    assert statuses == []
