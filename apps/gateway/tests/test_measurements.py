"""The measurements the 2026-10-07 live test lacked (docs/analysis, issue 9): per-stage timings
in latency_ms, the route that answered, prepared-answer similarity on a miss, the expand tap,
and the client's first word from latency.report, all kept in the session log for the flush."""

import json
import logging
from typing import Any

from copilot_core import keys
from copilot_core.logs import ContextFilter
from copilot_core.packs import PreparedAnswer, encode_embedding
from copilot_gateway.testing import GatewayServer, ProtocolClient

from .test_session_flow import STORY, FixedEmbedder, seed_memory


async def stored_latency(env: Any, sid: str) -> dict[str, dict[str, Any]]:
    """Each pass's answers.latency_ms as the flush builds it: the answer entry's, plus any
    latency_report entries logged after it."""
    log = [json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(sid))]
    out: dict[str, dict[str, Any]] = {}
    for e in log:
        if e["kind"] == "answer":
            out[e["pass"]] = dict(e["latency_ms"])
        elif e["kind"] == "latency_report":
            assert e["pass"] in out, "a report is logged only after its answer"
            skip = ("kind", "question_id", "pass", "at")
            out[e["pass"]].update({k: v for k, v in e.items() if k not in skip})
    return out


async def test_latency_ms_has_every_stage_and_the_route(env) -> None:
    await seed_memory(env)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?")
        fast = await c.wait_type("answer.done", pass_="fast")
        deep = await c.wait_type("answer.done", pass_="deep")
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
    lat = fast["latency_ms"]
    assert lat["start_ms"] <= lat["first_delta_ms"]
    for field in ("turn_end_ms", "final_ms", "start_ms", "first_token_ms", "first_delta_ms"):
        assert field in deep["latency_ms"], field
    # The route is stored with the answer, not sent to the client.
    assert "provider" not in lat
    stored = await stored_latency(env, c.session_id)
    assert stored["fast"]["provider"] == stored["deep"]["provider"] == "anthropic"
    assert stored["fast"]["fallback_attempt"] == 0
    # The speculative fast pass finished before confirmation; its row still gets the
    # confirmation's timings.
    for field in ("turn_end_ms", "final_ms"):
        assert stored["fast"][field] == deep["latency_ms"][field], field


async def test_prepared_similarity_is_kept_on_a_miss(env) -> None:
    far = PreparedAnswer(
        question="Why do you want this job?",
        embedding_b64=encode_embedding([0.0, 1.0] + [0.0] * 6),
        talking_points=["Mission"],
        node_ids=[STORY],
    )
    await seed_memory(env, [far], embedding_model=FixedEmbedder.model_id)
    async with GatewayServer(env.services(embedder=FixedEmbedder())) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale your system?")
        fast = await c.wait_type("answer.done", pass_="fast")
    assert fast["source"] == "model"
    assert fast["latency_ms"]["prepared_similarity"] == 0.0
    assert fast["latency_ms"]["prepared_ms"] >= 0


async def test_client_first_word_is_stored_with_the_answer(env) -> None:
    await seed_memory(env)
    # The deep pass only on expand, so its tap is measured.
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "Why us?"})
        qid = (await c.wait_type("question.detected"))["question_id"]
        # Sent at the first delta, so it may land before or after the answer is logged; it
        # ends up with the answer either way.
        await c.wait_type("answer.delta", pass_="fast")
        await c.send(
            {"type": "latency.report", "question_id": qid, "pass": "fast",
             "first_word_ms": 812, "anchor": "forced"}
        )  # fmt: skip
        await c.wait_type("answer.done", pass_="fast")
        # The deep pass after expand records the tap.
        await c.send({"type": "answer.expand", "question_id": qid})
        deep = await c.wait_type("answer.done", pass_="deep")
        await c.send(
            {"type": "latency.report", "question_id": qid, "pass": "deep",
             "first_word_ms": 2400, "anchor": "expand"}
        )  # fmt: skip
        # A report for an unknown question is dropped silently: measurement only.
        await c.send(
            {"type": "latency.report", "question_id": "0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210",
             "pass": "fast", "first_word_ms": 1, "anchor": "forced"}
        )  # fmt: skip
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
    assert not c.of_type("error")
    assert "expand_ms" in deep["latency_ms"]
    stored = await stored_latency(env, c.session_id)
    assert stored["fast"]["client_first_word_ms"] == 812
    assert stored["fast"]["client_anchor"] == "forced"
    assert stored["deep"]["client_first_word_ms"] == 2400
    assert stored["deep"]["client_anchor"] == "expand"


async def test_every_log_line_of_a_session_names_it(env, caplog) -> None:
    await seed_memory(env)
    caplog.set_level(logging.INFO, logger="copilot_gateway")
    # In the services, logs.configure puts this filter on the handlers; here, on caplog's.
    caplog.handler.addFilter(ContextFilter())
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.send({"type": "question.force", "text": "Why us?"})
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
    lines = [r for r in caplog.records if r.name.startswith("copilot_gateway")]
    messages = [r.getMessage() for r in lines]
    for prefix in ("session started", "answer ", "session ended"):
        assert any(m.startswith(prefix) for m in messages), prefix
    assert {getattr(r, "session_id", None) for r in lines} == {c.session_id}
    assert {getattr(r, "user_id", None) for r in lines} == {str(env.user_a)}


async def session_log(env: Any, sid: str) -> list[dict[str, Any]]:
    return [json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(sid))]


async def test_every_detector_decision_is_logged(env) -> None:
    """The live test could not explain its misses: only the detection that fired was visible
    (docs/analysis, section 1). Every decision is now logged, the text only with consent."""
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(consent={"store_transcript": False})
        await c.say("Thanks for joining today.", capture_ms=0)
        await c.wait_type("transcript.final")
        await c.say("How did you scale the ledger?", capture_ms=3000)
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        assert await c.closed() == 1000
    decisions = [e for e in await session_log(env, c.session_id) if e["kind"] == "detector"]
    assert [e["decision"] for e in decisions] == ["none", "answer"]
    for e in decisions:
        assert "text" not in e
        assert len(e["text_hash"]) == 32
        assert e["model"] == "claude-haiku-4-5"
        assert e["cost_usd"] > 0
        assert e["latency_ms"] >= 0
        for field in ("type", "first_token_ms", "input_tokens"):
            assert field in e, field
    assert decisions[1]["type"] == "technical"


async def test_detector_decision_keeps_the_text_with_consent(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("Thanks for joining today.")
        await c.wait_type("transcript.final")
        await c.send({"type": "session.end"})
        await c.closed()
    decisions = [e for e in await session_log(env, c.session_id) if e["kind"] == "detector"]
    assert decisions[0]["text"] == "Thanks for joining today."


async def test_a_cancelled_pass_is_logged_with_its_estimated_cost(env) -> None:
    """A pass thrown away mid-stream still costs money; without an entry, speculative waste
    and its cost could not be measured."""
    from copilot_gateway.testing import Reply, ScriptedProvider, default_reply

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
        await c.wait_type("answer.delta", pass_="fast")
        await c.frame(0, 0b01, b" and how do you use it?".ljust(3200, b"\0"), 1000)
        await c.wait_type("question.cancelled")
        await c.send({"type": "session.end"})
        await c.closed()
    cancelled = [e for e in await session_log(env, c.session_id) if e["kind"] == "pass_cancelled"]
    assert len(cancelled) == 1
    e = cancelled[0]
    assert (e["pass"], e["reason"], e["model_started"]) == ("fast", "speculative", True)
    assert e["streamed_chars"] > 0
    assert e["input_tokens_est"] > 0
    assert e["cost_usd_est"] > 0
