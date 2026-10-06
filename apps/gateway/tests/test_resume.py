"""Resume after a dropped socket: replay after last_seq, and no duplicate audio frames."""

import asyncio
import time
from typing import Any

from copilot_core import keys
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)


def assert_contiguous(c: ProtocolClient) -> None:
    seqs = [m["seq"] for m in c.messages]
    assert seqs == list(range(1, len(seqs) + 1)), seqs


async def resend_all(c: ProtocolClient) -> None:
    """What the client's ring buffer does after a resume: resend frames, oldest first."""
    for frame in c.sent_frames:
        await c.ws.send(frame)


def slow_deep_provider() -> ScriptedProvider:
    provider = ScriptedProvider("anthropic")

    def reply(job: str, req: Any) -> Reply:
        if job == "deep":
            return Reply([f"part {i}. " for i in range(10)], chunk_delay_s=0.05)
        return default_reply(job, req)

    provider.reply = reply
    return provider


async def test_resume_same_task_replays_after_last_seq(env) -> None:
    env.providers = [slow_deep_provider()]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How does the ledger work?", capture_ms=0)
        await c.wait(lambda m: m["type"] == "answer.delta" and m["pass"] == "deep")
        seen_before_drop = c.last_seq
        await c.drop()
        # The answer keeps streaming into the session while the socket is gone.
        await asyncio.sleep(0.6)
        session = next(iter(gw.manager.sessions.values()))
        assert session.seq > seen_before_drop
        await c.resume()
        await c.wait_type("answer.done", pass_="deep")
        assert_contiguous(c)  # replay started at last_seq + 1, nothing twice
        texts_before = list(env.stt.sent_text)
        await resend_all(c)
        await asyncio.sleep(0.3)
        assert env.stt.sent_text == texts_before  # every resent frame was dropped
        assert session.dedupe.dropped == len(c.sent_frames)
        # New audio after the resend still goes through.
        await c.say("What would you change?", capture_ms=9000)
        await c.wait(lambda m: m["type"] == "question.detected" and "change" in m["text"])
        assert_contiguous(c)


async def test_resume_on_another_task(env) -> None:
    env.providers = [slow_deep_provider()]
    async with GatewayServer(env.services()) as a, GatewayServer(env.services()) as b:
        c = ProtocolClient(a.url)
        await c.start()
        await c.say("How does the ledger work?", capture_ms=0)
        await c.wait_type("answer.done", pass_="deep")
        await c.drop()
        await asyncio.sleep(0.3)  # let task A's writer catch up
        last = c.last_seq
        c.last_seq = last - 3  # pretend the last three messages were never processed
        c.messages = c.messages[: last - 3]
        c.url = b.url
        await c.resume()
        await c.wait(lambda m: m["seq"] == last)
        assert_contiguous(c)
        # Task B now holds the lease; task A stops serving the session.
        vk = env.client()
        holder = await vk.get(keys.sess_lease(c.session_id))
        assert holder.decode() == b.services.task_id
        deadline = time.monotonic() + 3
        while a.manager.sessions:
            assert time.monotonic() < deadline, "task A kept the session"
            await asyncio.sleep(0.05)
        # Frames task A already had are dropped on task B too.
        texts_before = list(env.stt.sent_text)
        await resend_all(c)
        await asyncio.sleep(0.3)
        assert env.stt.sent_text == texts_before
        await c.say("What would you change?", capture_ms=9000)
        q = await c.wait(lambda m: m["type"] == "question.detected" and "change" in m["text"])
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        assert_contiguous(c)
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
    assert len([j for j in env.jobs.sent if j["type"] == "session.ended"]) == 1


async def test_newer_connection_replaces_older(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        stale = c.ws
        c2 = ProtocolClient(gw.url)
        c2.session_id, c2.last_seq = c.session_id, c.last_seq
        await c2.resume()
        await c.closed()
        assert c.close_code == 4409
        assert stale.close_code == 4409
        await c2.send({"type": "question.force", "text": "Why us?"})
        await c2.wait_type("answer.done", pass_="fast")


async def test_resume_rejects_another_users_session(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.drop()
        other = ProtocolClient(gw.url, token="token-b")
        other.session_id = c.session_id
        await other.resume()
        assert await other.closed() == 4408
