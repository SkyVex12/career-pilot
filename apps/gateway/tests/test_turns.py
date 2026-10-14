"""Turn handling against the problems the 2026-10-07 live test found (docs/analysis): the fast
pass waiting for the detector, the detector running twice in a row on a turn, the fixed wait
for a prepared match. A slow scripted detector stands in for the 1.3 s OpenAI one."""

import asyncio
import json
from typing import Any

from copilot_core import keys
from copilot_gateway.testing import GatewayServer, ProtocolClient, Reply, ScriptedProvider
from copilot_gateway.testing import default_reply as scripted_reply
from copilot_gateway.turn import likely_question

DETECTOR_S = 0.6  # the scripted detector's latency


def slow_detector(job: str, req: Any) -> Reply:
    reply = scripted_reply(job, req)
    if job == "detector":
        reply.first_delay_s = DETECTOR_S
        content = str(req.content)
        if "share your screen" in content.split("<utterance>")[-1]:
            # A request for an action: not a question that needs talking points.
            reply.chunks = [
                json.dumps(
                    {
                        "is_question": False,
                        "complete": True,
                        "addressed_to_user": True,
                        "type": "other",
                        "confidence": 0.9,
                    }
                )
            ]
    return reply


def use(env: Any, reply: Any = slow_detector) -> ScriptedProvider:
    provider = ScriptedProvider("anthropic", reply)
    env.providers = [provider]
    return provider


async def session_log(env: Any, sid: str) -> list[dict[str, Any]]:
    return [json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(sid))]


def test_likely_question_gates_the_speculative_start() -> None:
    for text in (
        "How does a database index actually speed up a query?",
        "Tell me about a time you had to deal with a production incident",
        "I'm curious how you handled schema changes in that pipeline",
        "What would you do differently if you built it again",
        "Walk me through what happens when you type a URL into the browser",
        "Have you ever disagreed with a decision your manager made",
        "Could you share your screen for a second?",  # held, then discarded by the detector
    ):
        assert likely_question(text), text
    for text in (
        "Thanks for joining today.",
        "Great.",
        "Okay, that makes sense. Let me just make a quick note of that.",
        "We run the payments platform, about 40 engineers across four squads.",
        "Why?",  # too short to be worth a pass
    ):
        assert not likely_question(text), text


async def test_fast_pass_starts_before_the_detector_returns(env) -> None:
    """Issue 1: the fast pass waited for the detector, so the first word was detector + TTFT.
    Now it starts at the speech end, held, and its first words go out as the detector fires."""
    use(env)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?")
        # The detector outlasts the 250 ms silence timer, so the question goes out confirmed.
        q = await c.wait_type("question.detected", speculative=False)
        first = await c.wait_type("answer.delta", question_id=q["question_id"], pass_="fast")
        fast = await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
    lat = fast["latency_ms"]
    assert lat["detector_ms"] >= DETECTOR_S * 1000 - 50
    assert lat["start_ms"] < 100, lat  # started with the detector, not after it
    # The buffered text went out right behind the question, at the release.
    assert first["seq"] == q["seq"] + 1
    assert lat["first_delta_ms"] >= lat["release_ms"] >= lat["detector_ms"]
    # The answer comes back typed: the type reached the deep pass and the log.
    assert q["question_type"] == "technical"


async def test_a_false_start_never_shows(env) -> None:
    """A held pass the detector rejects is cancelled without a message, and its cost is logged."""
    provider = use(env)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("Could you share your screen for a second?")
        await c.wait_type("transcript.final")
        await asyncio.sleep(DETECTOR_S + 0.5)
        session = next(iter(gw.manager.sessions.values()))
        assert session.turns.held_starts == 1
        assert session.turns.held_discarded == 1
        await c.send({"type": "session.end"})
        await c.closed()
    for kind in ("question.detected", "question.cancelled", "answer.delta", "answer.done"):
        assert c.of_type(kind) == [], kind
    assert ("fast", "claude-haiku-4-5") in provider.calls  # it did start
    log = await session_log(env, c.session_id)
    [discarded] = [e for e in log if e["kind"] == "pass_cancelled"]
    assert discarded["reason"] == "held_discarded"
    # The scripted pass finished while held, so its real usage and cost are known.
    assert discarded["cost_usd"] > 0
    assert discarded["output_tokens"] > 0


async def test_held_pass_is_dropped_silently_when_the_speaker_continues(env) -> None:
    use(env)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How would you design a rate limiter", capture_ms=0)
        await c.wait_type("transcript.final")
        await asyncio.sleep(0.2)  # before the detector returns
        await c.frame(0, 0b01, b" for a public API?".ljust(3200, b"\0"), 1000)
        await c.frame(0, 0b10, b"", 1200)
        q = await c.wait_type("question.detected", speculative=False, timeout=5)
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
    assert c.of_type("question.cancelled") == []
    assert len({m["question_id"] for m in c.of_type("question.detected")}) == 1
    assert q["text"] == "How would you design a rate limiter for a public API?"


async def test_text_that_fails_the_local_check_waits_for_the_detector(env) -> None:
    provider = use(env)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("Thanks for joining today.")
        await c.wait_type("transcript.final")
        await asyncio.sleep(DETECTOR_S + 0.3)
    assert [job for job, _ in provider.calls] == ["detector"]


async def test_holding_can_be_turned_off(env) -> None:
    use(env)
    async with GatewayServer(env.services(env.settings(speculative_hold=False))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?")
        fast = await c.wait_type("answer.done", pass_="fast")
    assert fast["latency_ms"]["start_ms"] >= fast["latency_ms"]["detector_ms"]


async def test_a_cut_short_interim_costs_one_detector_wait_not_two(env) -> None:
    """Issue 2: the interim lacked the last words, the detector said "incomplete", and only then
    did the gateway check the final with a second call: 1.3 to 2.1 s extra on 3 of 8
    questions, the p95. The final now replaces the interim's call the moment it arrives."""
    use(env)
    env.stt.final_suffix = " ledger?"  # the interim stops before the last word and the "?"
    env.stt.final_delay_s = 0.25  # the final lands while the interim's call is running
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the")
        q = await c.wait_type("question.detected")
        fast = await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    assert q["text"] == "How did you scale the ledger?"
    # Final arrival + one detector call, not two calls back to back.
    assert fast["latency_ms"]["turn_end_ms"] < 2 * DETECTOR_S * 1000 - 100, fast["latency_ms"]
    decisions = [e for e in await session_log(env, c.session_id) if e["kind"] == "detector"]
    assert [bool(e.get("cancelled")) for e in decisions] == [True, False]


async def test_a_final_that_changes_nothing_reuses_the_interim_decision(env) -> None:
    use(env)
    env.stt.final_delay_s = 0.25
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?")
        q = await c.wait_type("question.detected")
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    decisions = [e for e in await session_log(env, c.session_id) if e["kind"] == "detector"]
    assert len(decisions) == 1


def test_material_change() -> None:
    from copilot_gateway.turn import materially_changed

    assert not materially_changed("How did you scale it", "how did you scale it.")
    assert materially_changed("How did you scale it", "How did you scale it?")
    assert materially_changed("How did you scale the", "How did you scale the ledger?")
    assert not materially_changed("What's next?", "what's next?")
