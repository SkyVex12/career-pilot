"""Session behaviour through a real server with scripted STT and Claude (no network, no stack)."""

import asyncio
import json
import time
from typing import Any

from copilot_core import keys
from copilot_core.ids import uuid7
from copilot_core.jobs import embedding_tag
from copilot_core.packs import Digest, NodeRef, PreparedAnswer, SessionPack, encode_embedding
from copilot_core.pricing import DEFAULT_PRICES, DEFAULT_TIERS, cost_usd
from copilot_gateway.auth import Principal
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)

NODE = uuid7()
STORY = uuid7()


async def seed_memory(
    env: Any, prepared: list[PreparedAnswer] | None = None, embedding_model: str | None = None
) -> SessionPack:
    vk = env.client()
    digest_text = "# Career digest\n[n:7c1e] Senior Backend Engineer, Acme."
    digest = Digest.model_validate(
        {
            "version": Digest.version_of(digest_text),
            "text": digest_text,
            "refs": {"n:7c1e": NodeRef(node_id=NODE, label="Senior Backend Engineer, Acme")},
        }
    )
    pack = SessionPack.model_validate(
        {
            "pack_id": str(uuid7()),
            "user_id": str(env.user_a),
            "mode": "interview",
            "built_at": "2026-10-05T14:00:00Z",
            "digest_version": digest.version,
            "ranked_stories": [
                {
                    "node_id": str(STORY),
                    "short_id": "n:3f2a",
                    "title": "Payments migration",
                    "one_liner": "Moved the ledger",
                }
            ],
            "prepared_answers": [p.model_dump(mode="json") for p in prepared or []],
            "embedding_model": embedding_model,
        }
    )
    await vk.set(keys.digest(env.user_a), digest.model_dump_json(by_alias=True))
    await vk.set(keys.pack(env.user_a, pack.pack_id), pack.model_dump_json(by_alias=True))
    await vk.hset(keys.pack_index(env.user_a), "mode:interview", str(pack.pack_id))
    return pack


async def test_question_flow_streams_fast_and_deep(env) -> None:
    await seed_memory(env)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        ready = await c.start()
        assert ready["seq"] == 1
        assert ready["limits"] == {"max_minutes": 240}
        await c.say("How did you scale the ledger?")
        final = await c.wait_type("transcript.final")
        assert final["text"] == "How did you scale the ledger?"
        spec = await c.wait_type("question.detected", speculative=True)
        confirmed = await c.wait_type("question.detected", speculative=False)
        assert confirmed["question_id"] == spec["question_id"]
        assert confirmed["question_type"] == "technical"
        fast = await c.wait_type("answer.done", pass_="fast")
        assert fast["source"] == "model"
        # [n:3f2a] in the scripted answer maps to the pack's ranked story.
        assert fast["memory_refs"] == [{"node_id": str(STORY), "label": "Payments migration"}]
        assert fast["usage"] == {"input_tokens": 1000, "cached_tokens": 900, "output_tokens": 16}
        assert "first_delta_ms" in fast["latency_ms"]
        await c.wait_type("answer.done", pass_="deep")
        seqs = [m["seq"] for m in c.messages]
        assert seqs == list(range(1, len(seqs) + 1))
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000

    ended = [j for j in env.jobs.sent if j["type"] == "session.ended"]
    assert len(ended) == 1
    job = ended[0]
    assert job["idempotency_key"] == f"session.ended:{c.session_id}"
    assert job["user_id"] == str(env.user_a)
    assert job["payload"] == {"session_id": c.session_id}
    assert job["version"] == 1
    vk = env.client()
    assert await vk.get(keys.user_active_session(env.user_a)) is None
    assert await vk.hget(keys.sess_state(c.session_id), "status") == b"ended"
    # The session log holds the start, the segment, the question, both answers and the end.
    log = [json.loads(f[b"e"]) for _, f in await vk.xrange(keys.sess_log(c.session_id))]
    kinds = [e["kind"] for e in log]
    for kind in ("session.started", "segment", "question", "answer", "session.ended"):
        assert kind in kinds
    question = next(e for e in log if e["kind"] == "question")
    assert question["speculative_kept"] is True
    # Each answer is priced from its usage, model and tier (answers.cost_usd).
    for entry in (e for e in log if e["kind"] == "answer"):
        expected = cost_usd(
            entry["model"], entry["tier"], entry["input_tokens"], entry["cached_tokens"],
            entry["output_tokens"], DEFAULT_PRICES, DEFAULT_TIERS,
        )  # fmt: skip
        assert entry["tier"] == "standard"
        assert expected is not None
        assert entry["cost_usd"] == float(expected) > 0
    # Every server message is in the events stream, with its seq as the stream id.
    events = await vk.xrange(keys.sess_events(c.session_id))
    assert [int(i.split(b"-")[0]) for i, _ in events] == seqs


async def test_not_a_question_shows_nothing(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("Thanks for joining today.")
        await c.wait_type("transcript.final")
        await asyncio.sleep(0.5)
        assert c.of_type("question.detected") == []


async def test_speculative_cancel_when_speech_resumes(env) -> None:
    provider = ScriptedProvider("anthropic")

    def slow_fast(job: str, req: Any) -> Reply:
        reply = default_reply(job, req)
        if job == "fast":
            reply.chunk_delay_s = 0.3
        return reply

    provider.reply = slow_fast
    env.providers = [provider]
    settings = env.settings(speculative_start_ms=50, silence_timer_ms=600)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("What is your biggest strength?", capture_ms=0)
        spec = await c.wait_type("question.detected", speculative=True)
        # The speaker keeps talking before the 600 ms silence timer confirms.
        await c.frame(0, 0b01, b" and how do you use it?".ljust(3200, b"\0"), 1000)
        cancelled = await c.wait_type("question.cancelled")
        assert cancelled["question_id"] == spec["question_id"]
        await c.frame(0, 0b10, b"", 1200)
        confirmed = await c.wait_type("question.detected", speculative=False)
        assert confirmed["question_id"] != spec["question_id"]
        assert confirmed["text"] == "What is your biggest strength? and how do you use it?"
        # The cancelled question's fast pass stopped mid-stream.
        await c.wait_type("answer.done", question_id=confirmed["question_id"], pass_="fast")
        assert provider.cancelled >= 1
        assert not [m for m in c.of_type("answer.done") if m["question_id"] == spec["question_id"]]


async def test_new_question_cancels_previous_generation(env) -> None:
    provider = ScriptedProvider("anthropic")

    def slow_deep(job: str, req: Any) -> Reply:
        if job == "deep":
            return Reply([f"part {i}. " for i in range(40)], chunk_delay_s=0.05)
        return default_reply(job, req)

    provider.reply = slow_deep
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How does the ledger work?", capture_ms=0)
        q1 = await c.wait_type("question.detected", speculative=False)
        await c.wait(lambda m: m["type"] == "answer.delta" and m["pass"] == "deep")
        await c.say("What would you change?", capture_ms=5000)
        q2 = await c.wait(
            lambda m: (
                m["type"] == "question.detected"
                and not m["speculative"]
                and m["question_id"] != q1["question_id"]
            )
        )
        err = await c.wait_type("answer.error", question_id=q1["question_id"])
        assert err["code"] == "cancelled"
        assert err["pass"] == "deep"
        await c.wait_type("answer.done", question_id=q2["question_id"], pass_="fast")
        await asyncio.sleep(0.2)
        q1_deltas_after = [
            m
            for m in c.messages
            if m["type"] == "answer.delta"
            and m["question_id"] == q1["question_id"]
            and m["seq"] > err["seq"]
        ]
        assert q1_deltas_after == []
        assert not c.of_type("answer.done") or all(
            not (m["question_id"] == q1["question_id"] and m["pass"] == "deep")
            for m in c.of_type("answer.done")
        )


async def test_one_live_session_per_user(env) -> None:
    async with GatewayServer(env.services()) as gw:
        first = ProtocolClient(gw.url)
        await first.start()
        second = ProtocolClient(gw.url, token="token-a2")
        await second.connect()
        await second.send(start_message())
        err = await second.wait_type("error")
        assert err["code"] == "session_active"
        assert err["fatal"] is True
        assert await second.closed() == 4409
        # Another user is unaffected.
        other = ProtocolClient(gw.url, token="token-b")
        await other.start()


async def test_one_live_session_per_user_across_tasks(env) -> None:
    async with GatewayServer(env.services()) as a, GatewayServer(env.services()) as b:
        first = ProtocolClient(a.url)
        await first.start()
        second = ProtocolClient(b.url)
        await second.connect()
        await second.send(start_message())
        assert await second.closed() == 4409


async def test_stale_active_session_is_ended_and_replaced(env) -> None:
    vk = env.client()
    stale = str(uuid7())
    await vk.set(keys.user_active_session(env.user_a), stale)
    await vk.hset(
        keys.sess_state(stale),
        mapping={"user_id": str(env.user_a), "status": "active", "updated_at": "0"},
    )
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
    assert any(
        j["type"] == "session.ended" and j["payload"]["session_id"] == stale for j in env.jobs.sent
    )


async def test_auth_and_first_message(env) -> None:
    async with GatewayServer(env.services()) as gw:
        no_token = ProtocolClient(gw.url, token="")
        await no_token.connect()
        assert await no_token.closed() == 4401
        bad = ProtocolClient(gw.url, token="nope")
        await bad.connect()
        assert await bad.closed() == 4401
        silent = ProtocolClient(gw.url)
        await silent.connect()
        assert await silent.closed() == 4408  # nothing within first_message_timeout_s
        wrong = ProtocolClient(gw.url)
        await wrong.connect()
        await wrong.send({"type": "answer.expand", "question_id": str(uuid7())})
        assert await wrong.closed() == 4408
        unknown = ProtocolClient(gw.url)
        await unknown.connect()
        await unknown.send({"type": "session.resume", "session_id": str(uuid7()), "last_seq": 0})
        assert (await unknown.wait_type("error"))["code"] == "session_not_found"
        assert await unknown.closed() == 4408


async def test_auth_refresh_and_expiry(env) -> None:
    env.verifier.principals["short"] = Principal(env.user_a, "Alex", time.time() + 1.5)
    settings = env.settings(auth_expiring_lead_s=1)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url, token="short")
        await c.start()
        expiring = await c.wait_type("auth.expiring", timeout=3)
        assert expiring["expires_in_s"] <= 1
        await c.send({"type": "auth.refresh", "access_token": "token-a"})
        await asyncio.sleep(1.5)
        assert c.close_code is None  # refreshed in time
        await c.send({"type": "auth.refresh", "access_token": "token-b"})  # another user
        assert (await c.wait_type("error", code="unauthorized"))["fatal"] is True
        assert await c.closed() == 4401


async def test_limits_and_bad_input(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.ws.send(b"\x00" * (64 * 1024 + 1))
        assert (await c.wait_type("error", code="frame_too_large"))["fatal"] is False
        await c.ws.send(b"\x07\x00")
        await c.wait_type("error", code="bad_frame")
        await c.ws.send("{not json")
        await c.wait_type("error", code="bad_message")
        await c.send({"type": "answer.feedback", "question_id": "x", "rating": "up"})
        await c.wait(lambda m: m["type"] == "error" and m["code"] == "bad_message", start=3)
        big = {"type": "mode.set", "mode": "standup", "pad": "x" * (256 * 1024)}
        await c.send(big)
        await c.wait_type("error", code="message_too_large")
        await c.send({"type": "screenshot", "image": "aGVsbG8="})
        await c.wait_type("error", code="not_implemented")
        await c.send({"type": "answer.expand", "question_id": str(uuid7())})
        await c.wait_type("error", code="unknown_question")
        # Still alive after all of that.
        await c.send({"type": "question.force", "text": "How do you test?"})
        await c.wait_type("answer.done", pass_="fast")


async def test_question_force_and_rate_limit(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "Why this company?"})
        q = await c.wait_type("question.detected")
        assert q["speculative"] is False
        assert q["text"] == "Why this company?"
        await c.send({"type": "question.force"})
        limited = await c.wait_type("error", code="rate_limited")
        assert limited["fatal"] is False
        assert 0 < limited["retry_after_ms"] <= 1000
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        # Hotkey without text uses what was heard.
        await asyncio.sleep(1.0)
        await c.say("We use Kafka for events.")
        await c.wait_type("transcript.final")
        await c.send({"type": "question.force"})
        forced = await c.wait(
            lambda m: m["type"] == "question.detected" and m["question_id"] != q["question_id"]
        )
        assert "Kafka" in forced["text"]
        # Expand starts the deep pass for a type that does not get it automatically.
        await c.wait_type("answer.done", question_id=forced["question_id"], pass_="fast")
        await c.send({"type": "answer.expand", "question_id": forced["question_id"]})
        await c.wait_type("answer.done", question_id=forced["question_id"], pass_="deep")
        await c.send(
            {"type": "answer.feedback", "question_id": forced["question_id"], "rating": "up"}
        )
        await c.send({"type": "mode.set", "mode": "meeting"})
        await c.send({"type": "session.end"})
        await c.closed()
    log = [json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(c.session_id))]
    kinds = [e["kind"] for e in log]
    assert {"feedback", "expand", "mode"} <= set(kinds)
    forced_log = [e for e in log if e["kind"] == "question" and e["forced_by_hotkey"]]
    assert len(forced_log) == 2


VEC = [1.0] + [0.0] * 7


class FixedEmbedder:
    model_id = "test:fixed@8"

    async def embed(self, texts: Any) -> list[list[float]]:
        return [VEC for _ in texts]


def scaled_answer() -> PreparedAnswer:
    return PreparedAnswer(
        question="Tell me about a system you scaled.",
        embedding_b64=encode_embedding(VEC),
        talking_points=["Scaled the ledger 10x [n:3f2a]", "Zero downtime cutover"],
        node_ids=[STORY],
    )


async def test_prepared_answer_skips_the_model(env) -> None:
    await seed_memory(env, [scaled_answer()], embedding_model=FixedEmbedder.model_id)
    provider = env.providers[0]
    async with GatewayServer(env.services(embedder=FixedEmbedder())) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale your system?")
        done = await c.wait_type("answer.done", pass_="fast")
        assert done["source"] == "prepared"
        assert done["memory_refs"] == [{"node_id": str(STORY), "label": "Payments migration"}]
        delta = next(m for m in c.of_type("answer.delta") if m["pass"] == "fast")
        assert delta["text"].startswith("- Scaled the ledger 10x")
    assert ("fast", "claude-haiku-4-5") not in provider.calls


async def test_vectors_from_another_embedding_model_are_never_compared(env) -> None:
    # The same vectors, but the pack says another model made them: no match, the model
    # answers, and the pack is rebuilt for the gateway's embedding model.
    pack = await seed_memory(
        env, [scaled_answer()], embedding_model="bedrock:cohere.embed-v4:0@1024"
    )
    job_id = uuid7()
    await env.client().hset(keys.pack_index(env.user_a), f"job:{job_id}", str(pack.pack_id))
    async with GatewayServer(env.services(embedder=FixedEmbedder())) as gw:
        c = ProtocolClient(gw.url)
        await c.start(job_id=str(job_id))
        await c.say("How did you scale your system?")
        done = await c.wait_type("answer.done", pass_="fast")
        assert done["source"] == "model"
    rebuilds = [m for m in env.jobs.sent if m["type"] == "pack.build"]
    assert len(rebuilds) == 1
    assert rebuilds[0]["payload"]["context"] == f"job:{job_id}"
    assert rebuilds[0]["idempotency_key"].endswith(":" + embedding_tag(FixedEmbedder.model_id))


async def test_exact_repeat_served_from_cache(env) -> None:
    await seed_memory(env)
    provider = env.providers[0]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "How do you handle on-call?"})
        first = await c.wait_type("answer.done", pass_="fast")
        assert first["source"] == "model"
        await asyncio.sleep(1.1)
        await c.send({"type": "question.force", "text": "how do you handle on call"})
        second = await c.wait(
            lambda m: (
                m["type"] == "answer.done"
                and m["pass"] == "fast"
                and m["question_id"] != first["question_id"]
            )
        )
        assert second["source"] == "cache"
        assert second["memory_refs"] == first["memory_refs"]
    assert sum(1 for job, _ in provider.calls if job == "fast") == 1
    assert await env.client().keys("qa:*")


async def test_typed_answer_command_answers_what_was_just_said(env) -> None:
    # 2026-10-26 standup: "answer" typed into the Ask box was answered as the question
    # "answer", and the second time from the exact-repeat cache, six minutes stale.
    await seed_memory(env)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How do our label retries back off", capture_ms=0)
        await c.wait(lambda m: m["type"] == "transcript.final" and "retries" in m["text"])
        await c.send({"type": "question.force", "text": "answer"})
        first = await c.wait_type("question.detected")
        assert "label retries" in first["text"]
        await c.wait_type("answer.done", question_id=first["question_id"], pass_="fast")
        await asyncio.sleep(1.1)
        await c.say("Which queue takes the carrier webhooks", capture_ms=60_000)
        await c.wait(lambda m: m["type"] == "transcript.final" and "webhooks" in m["text"])
        await c.send({"type": "question.force", "text": "Answer every question now."})
        second = await c.wait(
            lambda m: m["type"] == "question.detected" and m["question_id"] != first["question_id"]
        )
        assert "carrier webhooks" in second["text"]
        done = await c.wait_type("answer.done", question_id=second["question_id"], pass_="fast")
        assert done["source"] == "model"  # not the first answer replayed from the cache


async def test_short_questions_skip_the_repeat_cache(env) -> None:
    await seed_memory(env)
    provider = env.providers[0]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        for i in range(2):
            await c.send({"type": "question.force", "text": "Why that?"})
            q = await c.wait(
                lambda m, i=i: (
                    m["type"] == "question.detected"
                    and len(c.of_type("question.detected")) == i + 1
                )
            )
            done = await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
            assert done["source"] == "model"
            await asyncio.sleep(1.1)
    assert sum(1 for job, _ in provider.calls if job == "fast") == 2
    assert not await env.client().keys("qa:*")


async def test_flush_job_every_interval(env) -> None:
    settings = env.settings(flush_interval_s=0.3)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("We shipped it last week.")
        await c.wait_type("transcript.final")
        deadline = time.monotonic() + 3
        while not [j for j in env.jobs.sent if j["type"] == "session.flush"]:
            assert time.monotonic() < deadline, "no session.flush job"
            await asyncio.sleep(0.05)
    flush = next(j for j in env.jobs.sent if j["type"] == "session.flush")
    upto = flush["payload"]["upto_entry_id"]
    assert flush["payload"]["session_id"] == c.session_id
    assert flush["idempotency_key"] == f"session.flush:{c.session_id}:{upto}"
    assert flush["user_id"] == str(env.user_a)
    # Nothing new, no new flush job.
    keys_sent = [j["idempotency_key"] for j in env.jobs.sent if j["type"] == "session.flush"]
    assert len(keys_sent) == len(set(keys_sent))


async def test_session_ends_without_resume(env) -> None:
    settings = env.settings(resume_window_s=0.5)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.drop()
        deadline = time.monotonic() + 5
        while not [j for j in env.jobs.sent if j["type"] == "session.ended"]:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        assert not gw.manager.sessions


async def test_idle_session_ends(env) -> None:
    settings = env.settings(idle_end_s=0.5)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        assert await c.closed(timeout=5) == 1000
    assert [j for j in env.jobs.sent if j["type"] == "session.ended"]


async def test_minute_limit_closes_4403(env) -> None:
    settings = env.settings(max_session_minutes=0)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        assert (await c.wait_type("error", code="no_minutes"))["fatal"] is True
        assert await c.closed() == 4403


def start_message() -> dict[str, Any]:
    return {
        "type": "session.start",
        "mode": "interview",
        "platform": "zoom",
        "client_version": "test",
        "consent": {"store_transcript": False},
        "audio": {"sample_rate": 16000, "encoding": "pcm16"},
    }


async def test_final_text_is_rechecked_when_the_interim_was_cut_short(env) -> None:
    env.stt.final_suffix = " ledger?"  # the interim stops before the last word and the "?"
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the")
        q = await c.wait_type("question.detected")
        # The final restarted the check as soon as it arrived, so the question can go out
        # before the silence timer, speculatively; the interim's words were never shown.
        assert q["text"] == "How did you scale the ledger?"
        await c.wait_type("question.detected", question_id=q["question_id"], speculative=False)
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        session = next(iter(gw.manager.sessions.values()))
        assert session.turns.final_rechecks == 1


async def test_no_text_stored_without_transcript_consent(env) -> None:
    await seed_memory(env)
    question = "How did you scale the ledger?"
    typed = "What would you change next?"
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(consent={"store_transcript": False})
        await c.say(question)
        q = await c.wait_type("question.detected", speculative=False)
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
        await c.send({"type": "question.force", "text": typed})
        q2 = await c.wait(lambda m: m["type"] == "question.detected" and m["text"] == typed)
        await c.wait_type("answer.done", question_id=q2["question_id"], pass_="fast")
        await c.send(
            {
                "type": "answer.feedback",
                "question_id": q["question_id"],
                "rating": "down",
                "reason": "too long winded",
            }
        )
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
    answers = [m["text"] for m in c.of_type("answer.delta")]
    vk = env.client()
    raw = [f[b"e"].decode() for _, f in await vk.xrange(keys.sess_log(c.session_id))]
    log = [json.loads(e) for e in raw]
    kinds = [e["kind"] for e in log]
    assert "segment" not in kinds
    for entry in log:
        if entry["kind"] in ("question", "answer", "feedback"):
            assert "text" not in entry
            assert "reason" not in entry
    # Nothing textual anywhere in the log: not the words, not the answers.
    blob = "\n".join(raw)
    for text in (question, "scale the ledger", typed, "too long winded", *answers):
        assert text.strip() not in blob, text
    # Metadata stays.
    answer_entries = [e for e in log if e["kind"] == "answer"]
    assert {e["pass"] for e in answer_entries} == {"fast", "deep"}
    assert all("latency_ms" in e and "output_tokens" in e for e in answer_entries)
    assert any(e["memory_node_ids"] for e in answer_entries)
    questions = [e for e in log if e["kind"] == "question"]
    assert {e["type"] for e in questions} == {"technical", "other"}
    assert next(e for e in log if e["kind"] == "feedback")["rating"] == "down"
    # The exact-repeat cache holds answer text, so it is not written either.
    assert await vk.keys("qa:*") == []


async def test_text_is_logged_with_consent(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "Why us?"})
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    log = [json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(c.session_id))]
    assert next(e for e in log if e["kind"] == "question")["text"] == "Why us?"
    assert next(e for e in log if e["kind"] == "answer")["text"]


async def test_refusal_mid_answer_sends_answer_error(env) -> None:
    provider = ScriptedProvider("anthropic")

    def refuse(job: str, req: Any) -> Reply:
        reply = default_reply(job, req)
        if job == "fast":
            reply.stop_reason = "refusal"
        return reply

    provider.reply = refuse
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "Why us?"})
        err = await c.wait_type("answer.error")
        assert err["code"] == "refused"
        assert not c.of_type("answer.done")


async def test_an_interview_loads_its_own_pack(env) -> None:
    """`interview_id` in session.start picks the interview's pack over the job's newest one."""
    job, interview = uuid7(), uuid7()
    job_pack = await seed_memory(env)
    interview_pack = SessionPack.model_validate(
        {**job_pack.model_dump(mode="json", by_alias=True), "pack_id": str(uuid7())}
    )
    vk = env.client()
    await vk.set(
        keys.pack(env.user_a, interview_pack.pack_id), interview_pack.model_dump_json(by_alias=True)
    )
    await vk.hset(
        keys.pack_index(env.user_a),
        mapping={f"job:{job}": str(job_pack.pack_id),
                 f"interview:{interview}": str(interview_pack.pack_id)},
    )  # fmt: skip
    async with GatewayServer(env.services()) as gw:
        for extra, expected in (
            ({"job_id": str(job), "interview_id": str(interview)}, interview_pack.pack_id),
            ({"job_id": str(job)}, job_pack.pack_id),
            ({"job_id": str(job), "interview_id": str(uuid7())}, job_pack.pack_id),
        ):
            c = ProtocolClient(gw.url)
            await c.start(**extra)
            state = await vk.hgetall(keys.sess_state(c.session_id))
            assert state[b"pack_id"].decode() == str(expected)
            assert state[b"interview_id"].decode() == extra.get("interview_id", "")
            await c.send({"type": "session.end"})
            assert await c.closed() == 1000
