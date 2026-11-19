"""Clear settled asks survive detector hesitation/failure without overriding rejections."""

import asyncio
import json

import pytest

from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)
from copilot_gateway.turn import recoverable_question


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Hello!", "small_talk"),
        ("How are you?", "small_talk"),
        ("How are you deploying services?", "other"),
        ("Hi. How do you deploy services?", "other"),
        ("What tools do you work with?", "other"),
        ("Where are you?", "other"),
        ("What's your approach to testing?", "other"),
        ("Okay, what questions do you have for us?", "other"),
        ("Can you hear the call clearly?", "other"),
        ("How did you tune the dashboard queries", "other"),
        ("Tell me about the monitoring you implemented", "other"),
        ("Why?", "other"),
        ("We use SQL to join the data.", None),
        ("I explained how the dashboard works.", None),
        ("What we do is process device telemetry.", None),
        ("How we deploy is documented in the runbook.", None),
        ("What we're building is a reporting dashboard.", None),
        ("What do you do if the service is down?", "other"),
        ("How do you know the deployment is healthy?", "other"),
        ("How are you validating the rollout?", "other"),
        ("Morgan, could you introduce yourself?", None),
        ("Can you explain the", None),
        ("And what about databases that you're", None),
        ("Thanks, okay.", None),
    ],
)
def test_recovery_requires_an_identifiable_direct_ask(text, kind) -> None:
    assert recoverable_question(text) == kind


@pytest.mark.parametrize("decision", ["wait", "error", "small_talk"])
async def test_settled_question_or_greeting_gets_one_card(env, decision) -> None:
    def reply(job, req):
        if job == "detector":
            if decision == "error":
                return Reply([], error=TimeoutError())
            return Reply([json.dumps({"answer": decision})])
        return default_reply(job, req)

    env.providers = [ScriptedProvider("anthropic", reply)]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        text = "Hello!" if decision == "small_talk" else "How did you tune the queries?"
        await c.say(text)
        q = await c.wait_type("question.detected", speculative=False, timeout=5)
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        session = next(iter(gw.manager.sessions.values()))
        assert session.turns.recovered == (decision != "small_talk")
        assert {m["question_id"] for m in c.of_type("question.detected")} == {q["question_id"]}
        assert q["text"] == text
        if decision == "small_talk":
            assert q["question_type"] == "small_talk"


@pytest.mark.parametrize(
    ("mode", "decision", "text"),
    [
        ("interview", "none", "Could you explain the migration?"),
        ("interview", "wait", "Could you explain the"),
        ("interview", "error", "We use SQL to join the data."),
        ("meeting", "error", "Could you explain the migration?"),
    ],
)
async def test_recovery_preserves_rejections_fragments_and_group_addressees(
    env, mode, decision, text
) -> None:
    def reply(job, req):
        if job == "detector":
            return (
                Reply([], error=TimeoutError())
                if decision == "error"
                else Reply([json.dumps({"answer": decision})])
            )
        return default_reply(job, req)

    env.providers = [ScriptedProvider("anthropic", reply)]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(mode=mode)
        await c.say(text)
        await c.wait_type("transcript.final")
        session = next(iter(gw.manager.sessions.values()))
        if session.turns.task is not None:
            await asyncio.wait_for(asyncio.shield(session.turns.task), 5)
        assert not c.of_type("question.detected")
        assert session.turns.recovered == 0


async def test_a_late_final_during_wait_recovery_replaces_stale_held_cues(env) -> None:
    """A final after the normal deadline but inside recovery can add a separate ask."""
    env.stt.final_delay_s = 0.9
    env.stt.final_suffix = " And why choose a queue?"

    def reply(job, req):
        if job == "detector":
            return Reply([json.dumps({"answer": "wait"})])
        return default_reply(job, req)

    provider = ScriptedProvider("anthropic", reply)
    env.providers = [provider]
    settings = env.settings(deep_pass_auto=False, silence_timer_ms=250, final_wait_max_ms=500)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("What does the service do?")
        q = await c.wait_type("question.detected", speculative=False, timeout=5)
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        assert q["text"] == "What does the service do? And why choose a queue?"
        assert {m["question_id"] for m in c.of_type("question.detected")} == {q["question_id"]}
        assert {m["question_id"] for m in c.of_type("answer.delta")} == {q["question_id"]}
        session = next(iter(gw.manager.sessions.values()))
        assert session.turns.recovered == 1
    fast = [str(req.content) for job, req in provider.requests if job == "fast"]
    assert len(fast) == 2
    assert "And why choose a queue?" not in fast[0]
    assert "And why choose a queue?" in fast[1]
    assert sum(job == "detector" for job, _ in provider.requests) == 2


@pytest.mark.parametrize("decision", ["wait", "error", "technical"])
@pytest.mark.parametrize(
    "text",
    [
        "What we do is process device telemetry.",
        "How we deploy is documented in the runbook.",
    ],
)
async def test_recovery_leaves_leading_declarative_wh_clauses_to_the_detector(
    env, decision: str, text: str
) -> None:
    def reply(job, req):
        if job == "detector":
            return (
                Reply([], error=TimeoutError())
                if decision == "error"
                else Reply([json.dumps({"answer": decision})])
            )
        return default_reply(job, req)

    env.providers = [ScriptedProvider("anthropic", reply)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say(text)
        await client.wait_type("transcript.final")
        session = next(iter(gw.manager.sessions.values()))
        if session.turns.task is not None:
            await asyncio.wait_for(asyncio.shield(session.turns.task), 5)
        assert session.turns.recovered == 0
        if decision == "technical":
            # Heuristics must not veto a detector-positive interpretation of the turn.
            question = await client.wait_type("question.detected", speculative=False)
            assert question["text"] == text
            await client.wait_type("answer.done", question_id=question["question_id"], pass_="fast")
        else:
            assert client.of_type("question.detected") == []
            assert client.of_type("answer.delta") == []
