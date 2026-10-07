"""The failure-mode rows the gateway owns (ARCHITECTURE.md, "Failure modes").

- Bedrock throttling or errors: retry, then the next route; a "slow" status.
- STT stream drops: reconnect; switch provider after two failures; "Reconnecting transcription".
- Gateway task dies or is replaced: the client resumes on another task (test_resume.py covers
  a planned move; here the first task dies without cleaning up).
- Valkey unavailable: the session carries on from memory and the log is written when Valkey
  returns.
"""

import asyncio
import contextlib
import time
from typing import Any

import anthropic
import httpx2

from copilot_core import keys
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)


def rate_limit_error() -> anthropic.RateLimitError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(429, request=request)
    return anthropic.RateLimitError("rate limited", response=response, body=None)


async def test_model_fallback_on_errors(env) -> None:
    bedrock = ScriptedProvider("bedrock")

    def throttled(job: str, req: Any) -> Reply:
        reply = default_reply(job, req)
        if job == "fast":
            reply.error = rate_limit_error()
        return reply

    bedrock.reply = throttled
    direct = ScriptedProvider("anthropic")
    env.providers = [bedrock, direct]
    settings = env.settings(llm_providers=["bedrock", "anthropic"])
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "How do you deploy?"})
        status = await c.wait_type("status", component="llm")
        assert status["state"] == "degraded"
        done = await c.wait_type("answer.done", pass_="fast")
        assert done["source"] == "model"
    # Bedrock twice (the primary and its one retry), then the direct API.
    assert [m for j, m in bedrock.calls if j == "fast"] == [
        "anthropic.claude-haiku-4-5",
        "anthropic.claude-haiku-4-5",
    ]
    assert [m for j, m in direct.calls if j == "fast"] == ["claude-haiku-4-5"]


async def test_model_fallback_on_first_token_timeout(env) -> None:
    slow = ScriptedProvider("bedrock")

    def stuck(job: str, req: Any) -> Reply:
        reply = default_reply(job, req)
        if job == "fast":
            reply.first_delay_s = 5
        return reply

    slow.reply = stuck
    direct = ScriptedProvider("anthropic")
    env.providers = [slow, direct]
    settings = env.settings(llm_providers=["bedrock", "anthropic"], first_token_timeout_ms=200)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        started = time.monotonic()
        await c.send({"type": "question.force", "text": "How do you deploy?"})
        done = await c.wait_type("answer.done", pass_="fast")
        elapsed = time.monotonic() - started
    assert done["source"] == "model"
    assert slow.cancelled == 2  # both bedrock attempts abandoned at the timeout
    # Two 200 ms timeouts; Windows timers can fire a few ms early by time.monotonic().
    assert 0.35 <= elapsed < 2.0
    assert [m for j, m in direct.calls if j == "fast"] == ["claude-haiku-4-5"]


async def test_all_routes_failing_sends_answer_error(env) -> None:
    broken = ScriptedProvider("anthropic")

    def fail(job: str, req: Any) -> Reply:
        reply = default_reply(job, req)
        if job != "detector":
            reply.error = rate_limit_error()
        return reply

    broken.reply = fail
    env.providers = [broken]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "How do you deploy?"})
        err = await c.wait_type("answer.error")
        assert err["code"] == "llm_unavailable"
        await c.wait_type("status", component="llm", state="degraded")


async def test_stt_reconnects_after_a_drop(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        env.stt.drop_after_chunks = 0  # the next audio chunk kills the connection
        await c.say("How do you deploy?")
        await c.wait_type("status", component="stt", state="reconnecting")
        await c.wait_type("status", component="stt", state="ok")
        # The audio sent before the drop was replayed on the new connection.
        final = await c.wait_type("transcript.final")
        assert final["text"] == "How do you deploy?"
        await c.wait_type("answer.done", pass_="fast")
    assert env.stt.connects == 2


async def test_stt_stall_while_speaking_reconnects(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        env.stt.stall = True
        await c.frame(0, 0b01, b" hello".ljust(3200, b"\0"), 0)  # speech starts, never ends
        await c.wait_type("status", component="stt", state="reconnecting", timeout=3)
        env.stt.stall = False
        await c.wait_type("status", component="stt", state="ok", timeout=3)
        partial = await c.wait_type("transcript.partial")
        assert partial["text"] == "hello"


async def test_stt_switches_provider_after_two_failures(env) -> None:
    async with GatewayServer(env.services(stt_names=("deepgram", "assemblyai"))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await asyncio.sleep(0.1)
        env.stt.connect_failures = 1
        env.stt.drop_after_chunks = 0  # failure 1: drop; failure 2: reconnect fails
        await c.say("Can you walk me through it?")
        final = await c.wait_type("transcript.final", timeout=5)
        assert final["text"] == "Can you walk me through it?"
        session = next(iter(gw.manager.sessions.values()))
        assert session.stt.switches == 1
        assert session.stt.provider == "assemblyai"
    assert env.stt.names == ["deepgram", "deepgram", "assemblyai"]


async def test_task_dies_and_client_resumes_elsewhere(env) -> None:
    async with GatewayServer(env.services()) as b:
        a = GatewayServer(env.services())
        await a.__aenter__()
        c = ProtocolClient(a.url)
        await c.start()
        await c.say("How does the ledger work?", capture_ms=0)
        await c.wait_type("answer.done", pass_="fast")
        await asyncio.sleep(0.3)
        # Task A dies: its event loop work stops without any cleanup or lease release.
        for session in list(a.manager.sessions.values()):
            if session.core_task:
                session.core_task.cancel()
        a.server.force_exit = True
        a.server.should_exit = True
        with contextlib.suppress(Exception):
            await c.drop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(a.task, 5)  # pyright: ignore[reportArgumentType]
        c.url = b.url
        await c.resume()
        await c.wait(lambda m: m["seq"] == c.last_seq and m["seq"] > 1)
        await c.send({"type": "question.force", "text": "What would you change?"})
        await c.wait(
            lambda m: m["type"] == "answer.done" and m["pass"] == "fast", start=len(c.messages) - 1
        )
        seqs = [m["seq"] for m in c.messages]
        assert seqs == list(range(1, len(seqs) + 1))


async def test_valkey_unavailable_keeps_the_session_going(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await asyncio.sleep(0.2)
        env.valkey_server.connected = False
        await c.say("How do you handle incidents?", capture_ms=0)
        await c.wait_type("answer.done", pass_="fast")
        await c.wait_type("answer.done", pass_="deep")
        # A drop and resume on the same task still works from memory.
        await c.drop()
        await c.resume()
        await c.send({"type": "question.force", "text": "What would you change?"})
        await c.wait(lambda m: m["type"] == "question.detected" and "change" in m["text"])
        session = next(iter(gw.manager.sessions.values()))
        assert session.writer.available is False
        assert session.writer.pending()
        env.valkey_server.connected = True
        deadline = time.monotonic() + 5
        while session.writer.pending() or not session.writer.available:
            assert time.monotonic() < deadline, "buffer never flushed"
            await asyncio.sleep(0.05)
        vk = env.client()
        events = await vk.xrange(keys.sess_events(c.session_id))
        stored = [int(i.split(b"-")[0]) for i, _ in events]
        assert stored == list(range(1, session.seq + 1))
        log = await vk.xrange(keys.sess_log(c.session_id))
        assert len(log) >= 5


async def test_valkey_down_at_session_start(env) -> None:
    env.valkey_server.connected = False
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.wait_type("status", component="memory", state="degraded")
        await c.send({"type": "question.force", "text": "Why us?"})
        await c.wait_type("answer.done", pass_="fast")
        # The in-memory check still enforces one live session per user on this task.
        second = ProtocolClient(gw.url)
        await second.connect()
        await second.send(
            {
                "type": "session.start",
                "mode": "interview",
                "platform": "zoom",
                "client_version": "test",
                "consent": {"store_transcript": False},
                "audio": {"sample_rate": 16000, "encoding": "pcm16"},
            }
        )
        assert await second.closed() == 4409
        env.valkey_server.connected = True
