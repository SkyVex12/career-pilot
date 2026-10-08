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
    async with GatewayServer(env.services()) as gw:
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
