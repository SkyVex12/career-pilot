"""Automatic-card repetition and follow-up boundaries through the live gateway."""

import asyncio
import json

import pytest

from copilot_core.llm import LLMRequest
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
    text_frame_audio,
)


def replying(job: str, req: LLMRequest) -> Reply:
    if job == "detector":
        utterance = str(req.content).split("<utterance>")[-1].split("</utterance>")[0]
        return Reply(
            [json.dumps({"answer": "small_talk" if "hello" in utterance.lower() else "other"})]
        )
    return default_reply(job, req)


def cards(client: ProtocolClient) -> set[str]:
    return {m["question_id"] for m in client.of_type("question.detected")}


@pytest.mark.parametrize(
    ("question", "repeat"),
    [
        ("Hello!", "HELLO."),
        ("How do you handle on-call?", "how do you handle on call"),
    ],
)
async def test_an_immediate_repeat_keeps_one_automatic_card(
    env, question: str, repeat: str
) -> None:
    provider = ScriptedProvider("anthropic", replying)
    env.providers = [provider]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say(question)
        first = await client.wait_type("answer.done", pass_="fast")
        await client.wait_type(
            "question.detected", question_id=first["question_id"], speculative=False
        )
        await client.say(repeat, capture_ms=1000)
        await client.wait(lambda m: m["type"] == "transcript.final" and m["text"] == repeat)
        await asyncio.sleep(0.45)
        assert cards(client) == {first["question_id"]}
        assert client.of_type("question.cancelled") == []
    assert sum(job == "fast" for job, _ in provider.calls) == 1


async def test_a_repeat_does_not_cancel_the_answer_still_streaming(env) -> None:
    def delayed(job: str, req: LLMRequest) -> Reply:
        return (
            Reply(["First point. ", "Second point."], chunk_delay_s=0.8)
            if job == "fast"
            else replying(job, req)
        )

    provider = ScriptedProvider("anthropic", delayed)
    env.providers = [provider]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("What do you build?")
        first = await client.wait_type("question.detected", speculative=False)
        await client.say("what do you build", capture_ms=1000)
        await client.wait_type("answer.done", question_id=first["question_id"], pass_="fast")
        assert cards(client) == {first["question_id"]}
        assert client.of_type("answer.error") == []
    assert sum(job == "fast" for job, _ in provider.calls) == 1


async def test_a_duplicate_interim_with_a_new_final_question_is_answered(env) -> None:
    env.providers = [ScriptedProvider("anthropic", replying)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("How do you deploy services?")
        first = await client.wait_type("answer.done", pass_="fast")
        await client.wait_type(
            "question.detected", question_id=first["question_id"], speculative=False
        )
        env.stt.final_suffix = " And how do you monitor them?"
        # More than the STT finalize-overlap slack: a real question lasts longer than
        # TextSTT's usual single 100ms frame, which can look like an earlier flush.
        payload = text_frame_audio("How do you deploy services?") + b"\0" * 6400
        await client.frame(0, 0b01, payload, 1000)
        await client.frame(0, 0b10, b"", 1300)
        second = await client.wait(
            lambda m: m["type"] == "question.detected" and m["question_id"] != first["question_id"]
        )
        assert second["text"].endswith("And how do you monitor them?")
        await client.wait_type("answer.done", question_id=second["question_id"], pass_="fast")


@pytest.mark.parametrize("empty", [False, True])
async def test_a_failed_or_empty_answer_does_not_suppress_a_retry(env, empty: bool) -> None:
    attempts = 0

    def failing_once(job: str, req: LLMRequest) -> Reply:
        nonlocal attempts
        if job == "fast":
            attempts += 1
            if attempts == 1:
                return Reply([], stop_reason="end_turn" if empty else "refusal")
        return replying(job, req)

    env.providers = [ScriptedProvider("anthropic", failing_once)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("How do you deploy services?")
        first = await client.wait_type("answer.done" if empty else "answer.error", pass_="fast")
        await client.wait_type("question.detected", speculative=False)
        await client.say("How do you deploy services?", capture_ms=1000)
        second = await client.wait(
            lambda m: m["type"] == "answer.done" and m["question_id"] != first["question_id"]
        )
        assert second["question_id"] != first["question_id"]
    assert attempts == 2


async def test_distinct_followups_inside_the_window_keep_their_own_questions(env) -> None:
    provider = ScriptedProvider("anthropic", replying)
    env.providers = [provider]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        for index, text in enumerate(
            ["How do you deploy services?", "And how do you monitor them", "Hello!"]
        ):
            await client.say(text, capture_ms=index * 1000)
            q = await client.wait(
                lambda m, text=text: (
                    m["type"] == "question.detected" and not m["speculative"] and m["text"] == text
                )
            )
            await client.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        assert len(cards(client)) == 3


async def test_a_short_repeat_after_a_substantive_user_reply_is_a_new_followup(env) -> None:
    env.providers = [ScriptedProvider("anthropic", replying)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("Why?")
        first = await client.wait_type("answer.done", pass_="fast")
        await client.wait_type(
            "question.detected", question_id=first["question_id"], speculative=False
        )
        await client.say_me(
            "The database supports serializable transactions", capture_ms=1000, end_ms=1600
        )
        await client.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await client.say("Why?", capture_ms=2000)
        await client.wait(
            lambda m: m["type"] == "answer.done" and m["question_id"] != first["question_id"]
        )
        assert len(cards(client)) == 2


async def test_a_deliberate_forced_repeat_bypasses_automatic_suppression(env) -> None:
    env.providers = [ScriptedProvider("anthropic", replying)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("How do you deploy services?")
        first = await client.wait_type("answer.done", pass_="fast")
        await client.wait_type(
            "question.detected", question_id=first["question_id"], speculative=False
        )
        await client.send({"type": "question.force", "text": "How do you deploy services?"})
        await client.wait(
            lambda m: m["type"] == "answer.done" and m["question_id"] != first["question_id"]
        )
        assert len(cards(client)) == 2


async def test_the_same_short_question_after_a_different_question_is_new(env) -> None:
    env.providers = [ScriptedProvider("anthropic", replying)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        seen: set[str] = set()
        for index, text in enumerate(["Why?", "What changed?", "Why?"]):
            await client.say(text, capture_ms=index * 1000)
            question = await client.wait(
                lambda m: (
                    m["type"] == "question.detected"
                    and not m["speculative"]
                    and m["question_id"] not in seen
                )
            )
            seen.add(question["question_id"])
            await client.wait_type("answer.done", question_id=question["question_id"], pass_="fast")
        assert len(cards(client)) == 3


async def test_a_question_outside_the_capture_window_is_not_suppressed(env) -> None:
    env.providers = [ScriptedProvider("anthropic", replying)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("How do you deploy services?")
        first = await client.wait_type("answer.done", pass_="fast")
        await client.wait_type(
            "question.detected", question_id=first["question_id"], speculative=False
        )
        await client.say("How do you deploy services?", capture_ms=12_000)
        await client.wait(
            lambda m: m["type"] == "answer.done" and m["question_id"] != first["question_id"]
        )


async def test_a_successful_deep_answer_is_usable_when_the_fast_answer_failed(env) -> None:
    def failed_fast(job: str, req: LLMRequest) -> Reply:
        return Reply([], stop_reason="refusal") if job == "fast" else replying(job, req)

    provider = ScriptedProvider("anthropic", failed_fast)
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("How do you deploy services?")
        first = await client.wait_type("answer.done", pass_="deep")
        await client.say("How do you deploy services?", capture_ms=1000)
        await client.wait(lambda m: m["type"] == "transcript.final" and m["start_ms"] >= 1000)
        await asyncio.sleep(0.45)
        assert cards(client) == {first["question_id"]}


async def test_a_short_repeat_after_new_interviewer_context_is_a_new_question(env) -> None:
    def contextual(job: str, req: LLMRequest) -> Reply:
        utterance = str(req.content).split("<utterance>")[-1].split("</utterance>")[0]
        if job == "detector" and "priority changed" in utterance:
            return Reply([json.dumps({"answer": "none"})])
        return replying(job, req)

    env.providers = [ScriptedProvider("anthropic", contextual)]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("Why?")
        first = await client.wait_type("answer.done", pass_="fast")
        await client.wait_type(
            "question.detected", question_id=first["question_id"], speculative=False
        )
        statement = "The priority changed to compliance."
        payload = text_frame_audio(statement) + b"\0" * 6400
        await client.frame(0, 0b01, payload, 1000)
        await client.frame(0, 0b10, b"", 1300)
        await client.wait(lambda m: m["type"] == "transcript.final" and m["text"] == statement)
        await asyncio.sleep(0.3)
        assert cards(client) == {first["question_id"]}
        await client.say("Why?", capture_ms=2000)
        second = await client.wait(
            lambda m: m["type"] == "answer.done" and m["question_id"] != first["question_id"]
        )
        assert second["question_id"] != first["question_id"]
        assert len(cards(client)) == 2


@pytest.mark.parametrize("overlap", [False, True])
async def test_a_slow_detector_keeps_the_original_question_context_anchor(
    env, overlap: bool
) -> None:
    why_calls = 0

    def delayed(job: str, req: LLMRequest) -> Reply:
        nonlocal why_calls
        if job == "detector":
            utterance = str(req.content).split("<utterance>")[-1].split("</utterance>")[0].strip()
            if utterance == "Why?":
                why_calls += 1
                return Reply(
                    [json.dumps({"answer": "other"})], first_delay_s=0.8 if why_calls == 1 else 1.0
                )
            return Reply([json.dumps({"answer": "none"})])
        return default_reply(job, req)

    env.providers = [ScriptedProvider("anthropic", delayed)]
    settings = env.settings(deep_pass_auto=False, first_token_timeout_ms=2000)
    async with GatewayServer(env.services(settings)) as gw:
        client = ProtocolClient(gw.url)
        await client.start()

        async def say(text: str, at_ms: int) -> None:
            payload = text_frame_audio(text) + b"\0" * 6400
            await client.frame(0, 0b01, payload, at_ms)
            await client.frame(0, 0b10, b"", at_ms + 300)

        await say("Why?", 0)
        await client.wait(lambda m: m["type"] == "transcript.final" and m["text"] == "Why?")
        await asyncio.sleep(0.3)
        statement = "The priority changed to compliance."
        await say(statement, 500)
        await client.wait(lambda m: m["type"] == "transcript.final" and m["text"] == statement)
        if overlap:
            await asyncio.sleep(0.3)
            await say("Why?", 1000)
        first = await client.wait_type("question.detected", speculative=False)
        await client.wait_type("answer.done", question_id=first["question_id"], pass_="fast")
        session = next(iter(gw.manager.sessions.values()))
        question = next(
            q for q in session.answers.questions.values() if str(q.qid) == first["question_id"]
        )
        assert session._automatic_questions[question.qid].at_ms == 300
        if not overlap:
            await say("Why?", 1000)
        await client.wait(
            lambda m: m["type"] == "answer.done" and m["question_id"] != first["question_id"]
        )
        assert len(cards(client)) == 2
        assert session.turns.detached_turns >= 1


@pytest.mark.parametrize(
    ("text", "qtype"),
    [
        ("What's your monitoring approach?", "technical"),
        ("Agreed?", "respond"),
        ("Thanks. Take care!", "small_talk"),
    ],
)
async def test_a_fresh_brief_reply_or_contracted_ask_does_not_repeat_the_prior_question(
    env, text: str, qtype: str
) -> None:
    def reply(job: str, req: LLMRequest) -> Reply:
        if job == "detector":
            utterance = str(req.content).split("<utterance>")[-1].split("</utterance>")[0].strip()
            return Reply([json.dumps({"answer": qtype if utterance == text else "technical"})])
        return default_reply(job, req)

    provider = ScriptedProvider("anthropic", reply)
    env.providers = [provider]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        client = ProtocolClient(gw.url)
        await client.start()
        await client.say("How do you deploy services?")
        first = await client.wait_type("question.detected", speculative=False)
        await client.wait_type("answer.done", question_id=first["question_id"], pass_="fast")
        await client.say(text, capture_ms=1000)
        second = await client.wait(
            lambda m: m["type"] == "question.detected" and m["question_id"] != first["question_id"]
        )
        assert second["text"] == text
        await client.wait_type("answer.done", question_id=second["question_id"], pass_="fast")
        assert len(cards(client)) == 2
    request = [req for job, req in provider.requests if job == "fast"][-1]
    question = str(request.content).splitlines()[-1]
    assert question.startswith("Question")
    assert question.split(": ", 1)[1] == text
