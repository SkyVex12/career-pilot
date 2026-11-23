"""The fixes of the 2026-11-11 analysis of every session (docs/analysis): a question left
unfinished keeps its first half, follow cards stay off answered turns and off the user's own
questions, a standup's update card, a plain question shown before the detector in a
one-on-one call, and the badge when nothing comes from the call."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from copilot_core import keys, prompts, workplace
from copilot_core.llm import LLMRequest
from copilot_core.packs import EmployerRef
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)
from copilot_gateway.turn import asking, plain_question, update_opening


def utterance_of(req: LLMRequest) -> str:
    return str(req.content).split("<utterance>")[-1].split("</utterance>")[0].strip()


def requests_for(provider: ScriptedProvider, job: str) -> list[LLMRequest]:
    return [req for j, req in provider.requests if j == job]


async def decided(gw: GatewayServer) -> Any:
    """The session, once the turn in flight has been decided."""
    session = next(iter(gw.manager.sessions.values()))
    for _ in range(300):
        task = session.turns.task
        if (task is None or task.done()) and not session.turns._detached:
            break
        await asyncio.sleep(0.01)
    return session


def detected(c: ProtocolClient) -> list[dict[str, Any]]:
    return [m for m in c.of_type("question.detected") if not m["speculative"]]


def test_a_plain_question_is_told_from_one_the_detector_must_decide() -> None:
    for text in (
        "So how would you design the retry logic for a payment that times out twice?",
        "Okay, and then what about databases, which ones have you used the most?",
        "Can you describe a project where you owned both the API and the data model?",
        "Tell me about a time you had to push back on a product decision, would you?",
    ):
        assert plain_question(text), text
    for text in (
        "How did you scale it?",  # too short to be sure
        "Are you authorized to work in the United States without any sponsorship at all?",
        "And what is your eligibility to work in the US at the moment, please?",
        "What salary range are you looking for in your next role, roughly speaking?",
        "Do you have any questions for me about the team or the role today?",
        "So that is how the team is set up and how we split the on-call work.",
        "I was wondering how you would design the retry logic for a payment",
    ):
        assert not plain_question(text), text


def test_the_start_of_an_update_and_a_question_of_the_users_are_recognized() -> None:
    for text in (
        "Okay. So for my updates, yesterday I finished the retry fix",
        "Yeah, from my side, the report is nearly done",
        "So yesterday I fixed two tickets",
        "Today I'm going to start on the export",
    ):
        assert update_opening(text), text
    for text in (
        "Yes, I went through it myself.",
        "I think the setup page is better than the preview, like we said in yesterday's call "
        "when you showed me your update",
    ):
        assert not update_opening(text), text
    assert asking("Sorry, could you say that date again? The audio cut out.")
    assert asking("Yeah. I was going to ask, what is the planned date for the report?")
    assert not asking("We split the ledger by account, right? And then moved the reads.")
    assert not asking("Mostly PostgreSQL and some MySQL.")


def waits_on(first_half_end: str) -> Any:
    def reply(job: str, req: LLMRequest) -> Reply:
        if job == "detector" and utterance_of(req).endswith(first_half_end):
            return Reply([json.dumps({"answer": "wait"})])
        return default_reply(job, req)

    return reply


async def test_a_question_left_unfinished_keeps_its_first_half(env) -> None:
    # 2026-11-10: two cards carried only a question's last words; the first half had been
    # judged unfinished and was dropped when the speaker went on after a pause.
    provider = ScriptedProvider("anthropic", waits_on("that you're"))
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("And what about databases, do you have any SQL that you're", capture_ms=0)
        await c.wait_type("transcript.final")
        session = await decided(gw)
        assert not c.of_type("question.detected")
        await c.say("that you're experienced with?", capture_ms=3000)
        q = await c.wait_type("question.detected", speculative=False)
        assert q["text"] == (
            "And what about databases, do you have any SQL that you're "
            "that you're experienced with?"
        )
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        assert session.turns.carried == 1
        # Both passes answer the whole question.
        fast = [r for r in requests_for(provider, "fast") if "experienced with?" in str(r.content)]
        assert all("what about databases" in str(r.content).split("Question")[-1] for r in fast)
        # The next question stands on its own.
        await decided(gw)
        await c.say("What about caching?", capture_ms=9000)
        q2 = await c.wait(lambda m: m["type"] == "question.detected" and "caching" in m["text"])
        assert q2["text"] == "What about caching?"


async def test_an_unfinished_sentence_is_dropped_once_the_user_speaks_or_time_passes(env) -> None:
    env.providers = [ScriptedProvider("anthropic", waits_on("that you're"))]
    async with GatewayServer(env.services(env.settings(continuation_gap_ms=400))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("And what about databases, any SQL that you're", capture_ms=0)
        await c.wait_type("transcript.final")
        await decided(gw)
        await c.say_me("Sorry, I did not catch the end of that", capture_ms=2000, end_ms=4000)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await c.say("What about caching?", capture_ms=6000)
        q = await c.wait(lambda m: m["type"] == "question.detected" and "caching" in m["text"])
        assert q["text"] == "What about caching?"
        await decided(gw)
        # And after a pause longer than the gap, with nobody speaking in between.
        await c.say("And for the queues, any tool that you're", capture_ms=9000)
        await c.wait(lambda m: m["type"] == "transcript.final" and "queues" in m["text"])
        await decided(gw)
        await asyncio.sleep(0.6)
        await c.say("How do you test it?", capture_ms=15000)
        q = await c.wait(lambda m: m["type"] == "question.detected" and "test it" in m["text"])
        assert q["text"] == "How do you test it?"


async def test_no_follow_card_after_a_remark_that_follows_the_question(env) -> None:
    # 2026-11-10: a trailing remark and an audio check after a question that had its card
    # made the turn look unanswered, and a second card restated the first.
    env.providers = [ScriptedProvider("anthropic")]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?", capture_ms=0)
        q1 = await c.wait_type("question.detected", speculative=False)
        await c.wait_type("answer.done", question_id=q1["question_id"], pass_="fast")
        await c.say("On the platform side in particular.", capture_ms=2000)
        await c.wait(lambda m: m["type"] == "transcript.final" and "platform" in m["text"])
        await decided(gw)
        await c.say_me(
            "We split the ledger by account and moved the reads onto replicas behind a cache",
            capture_ms=4000, end_ms=8000,
        )  # fmt: skip
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await asyncio.sleep(0.6)
        assert [m["question_id"] for m in detected(c)] == [q1["question_id"]]


async def test_no_follow_card_when_the_user_is_the_one_asking(env) -> None:
    # 2026-11-04 and 2026-11-10: the user asked for a date and for a repeat, and a card
    # "followed" them.
    env.providers = [ScriptedProvider("anthropic")]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("We moved the release of that report to a later date.", capture_ms=0)
        await c.wait_type("transcript.final")
        await decided(gw)
        await c.say_me(
            "Sorry, could you say that date again? The audio cut out for a second on my side",
            capture_ms=2000, end_ms=6000,
        )  # fmt: skip
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await asyncio.sleep(0.6)
        assert not c.of_type("question.detected")
        # Still followed when they answer a turn nothing answered.
        await c.say("And the one after it goes out the week after.", capture_ms=8000)
        await c.wait(lambda m: m["type"] == "transcript.final" and "week after" in m["text"])
        await decided(gw)
        await c.say_me(
            "That works because the export for it needs the new columns from the first one",
            capture_ms=10000, end_ms=14000,
        )  # fmt: skip
        await c.wait_type("question.detected", timeout=3.0)


async def seed_standup_pack(env: Any, mode: str = "standup") -> None:
    today = datetime.now(UTC).date()
    employer = EmployerRef(id=uuid4(), name="Fernhill Logistics", title="Backend engineer")

    def node(title: str, summary: str, state: str) -> dict[str, Any]:
        return {
            "id": uuid4(), "kind": "work_item", "title": title, "summary": summary,
            "body": {"state": state}, "valid_from": today - timedelta(days=1),
        }  # fmt: skip

    nodes = [
        node("Label retry logging", "Fixed the missing log line for the second retry.", "done"),
        node("Carrier status report export", "Add CSV and PDF export.", "planned"),
    ]
    build = workplace.standup_pack if mode == "standup" else workplace.meeting_pack
    pack = build(env.user_a, employer, nodes, today, "test")
    vk = env.client()
    await vk.set(keys.pack(env.user_a, pack.pack_id), pack.model_dump_json(by_alias=True))
    await vk.hset(keys.pack_index(env.user_a), f"mode:{mode}", str(pack.pack_id))


@pytest.mark.parametrize("mode", ["standup", "meeting"])
async def test_a_work_update_gets_the_prepared_card_as_the_user_starts(env, mode: str) -> None:
    # In 3 of 8 real standups the hand-off was a pause, the user gave yesterday and today
    # with nothing on screen, and a follow card came 5 to 9 s later on the teammate's topic.
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    await seed_standup_pack(env, mode)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(mode=mode)
        await c.say("So that is it from my side.", capture_ms=0)
        await c.wait_type("transcript.final")
        await decided(gw)
        await c.say_me(
            "Okay so for my updates yesterday I finished the retry logging fix",
            capture_ms=2000, end_ms=5000,
        )  # fmt: skip
        q = await c.wait_type("question.detected")
        assert (q["question_type"], q["text"]) == ("standup_status", "What's your update?")
        fast = await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        assert fast["source"] == "prepared"
        shown = "".join(
            m["text"] for m in c.of_type("answer.delta") if m["question_id"] == q["question_id"]
        )
        assert "Label retry logging" in shown
        assert "Carrier status report export" in shown
        # No model wrote the short answer; the detailed one develops it.
        assert not [r for r in requests_for(provider, "fast") if "your update?" in str(r.content)]
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
        # Once per call, and no follow card on the same turn.
        await c.say_me(
            "and today I am on the carrier status report where the main table is done already",
            capture_ms=6000, end_ms=10000,
        )  # fmt: skip
        await c.wait(lambda m: m["type"] == "transcript.final" and "carrier" in m["text"])
        await asyncio.sleep(0.6)
        assert len(c.of_type("question.detected")) == 1


@pytest.mark.parametrize("mode", ["standup", "meeting"])
async def test_no_update_card_over_a_card_already_shown(env, mode: str) -> None:
    env.providers = [ScriptedProvider("anthropic")]
    await seed_standup_pack(env, mode)
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(mode=mode)
        # The teammate asked, and that question has its card: the update card stays away.
        await c.say("What is your update on the retry work?", capture_ms=0)
        q1 = await c.wait_type("question.detected", speculative=False)
        await c.wait_type("answer.done", question_id=q1["question_id"], pass_="fast")
        await c.say_me(
            "So for my updates yesterday I finished the retry logging fix",
            capture_ms=2000, end_ms=5000,
        )  # fmt: skip
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await asyncio.sleep(0.6)
        assert [m["question_id"] for m in detected(c)] == [q1["question_id"]]


def slow_detector(answer: str, delay_s: float = 0.6) -> Any:
    def reply(job: str, req: LLMRequest) -> Reply:
        if job == "detector":
            return Reply([json.dumps({"answer": answer})], first_delay_s=delay_s)
        return default_reply(job, req)

    return reply


PLAIN = "So how would you design the retry logic for a payment that times out twice?"


async def test_a_plain_interview_question_shows_before_the_detector_decides(env) -> None:
    # Across 151 real questions the card's first word came 1.9 s after the question (p50),
    # about 1 s of it the detector, and the user had started answering first on 56% of
    # interview questions. A plain question now shows as soon as its final is in.
    env.providers = [ScriptedProvider("anthropic", slow_detector("technical"))]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        loop = asyncio.get_running_loop()
        started = loop.time()
        await c.say(PLAIN, capture_ms=0)
        first = await c.wait_type("question.detected")
        assert first["speculative"] is True
        assert loop.time() - started < 0.5  # the detector takes 0.6 s
        await c.wait_type("answer.delta", question_id=first["question_id"], pass_="fast")
        confirmed = await c.wait_type("question.detected", speculative=False)
        assert confirmed["question_id"] == first["question_id"]
        assert confirmed["question_type"] == "technical"
        await c.wait_type("answer.done", question_id=first["question_id"], pass_="fast")
        assert not c.of_type("question.cancelled")
        session = await decided(gw)
        assert (session.turns.first_shown, session.turns.first_withdrawn) == (1, 0)
        # The stored answer says so, for the next analysis.
        log = [
            json.loads(f[b"e"]) for _, f in await env.client().xrange(keys.sess_log(c.session_id))
        ]
        fast = next(e for e in log if e.get("kind") == "answer" and e["pass"] == "fast")
        assert fast["latency_ms"]["first_shown"] == 1


async def test_a_card_shown_first_is_withdrawn_when_the_detector_says_no(env) -> None:
    env.providers = [ScriptedProvider("anthropic", slow_detector("none"))]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say(PLAIN, capture_ms=0)
        first = await c.wait_type("question.detected")
        gone = await c.wait_type("question.cancelled")
        assert gone["question_id"] == first["question_id"]
        session = await decided(gw)
        assert session.turns.first_withdrawn == 1
        assert not detected(c)


async def test_nothing_shows_before_the_detector_in_a_standup_or_when_turned_off(env) -> None:
    for settings, mode in (
        (env.settings(), "standup"),
        (env.settings(first_show_min_words=None), "interview"),
    ):
        env.providers = [ScriptedProvider("anthropic", slow_detector("technical"))]
        async with GatewayServer(env.services(settings)) as gw:
            c = ProtocolClient(gw.url)
            await c.start(mode=mode)
            loop = asyncio.get_running_loop()
            started = loop.time()
            await c.say(PLAIN, capture_ms=0)
            await c.wait_type("question.detected")
            assert loop.time() - started >= 0.55, mode
            session = await decided(gw)
            assert session.turns.first_shown == 0
            await c.send({"type": "session.end"})
            await c.closed()


async def test_the_badge_says_when_nothing_comes_from_the_call(env) -> None:
    # 2026-11-05: a two-hour interview heard nine fragments of the other side; 2026-11-09:
    # one heard none. The mic had a warning; the call's side had not.
    settings = env.settings(call_silent_warn_s=0.15)
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say_me("Hello, can you hear me", capture_ms=0, end_ms=2000)
        warn = await c.wait(lambda m: m["type"] == "status" and "other side" in m["message"])
        assert (warn["component"], warn["state"]) == ("stt", "degraded")
        await c.say("Yes, sorry, I was on mute", capture_ms=4000)
        ok = await c.wait(lambda m: m["type"] == "status" and m["state"] == "ok"
                          and "call" in m["message"])  # fmt: skip
        assert ok["component"] == "stt"


def test_the_prompts_leave_the_users_choices_to_them_and_read_amounts_sensibly() -> None:
    ctx = prompts.PromptContext("Sam Lee", "interview", "digest", None)
    system = prompts.fast_pass(ctx, "", "q", None, 100).system[0].text
    assert "never made for them" in system
    assert '"my preference is..."' in system
    assert "never ask what it means" in system
    detector = prompts.detector("Sam Lee", "", "u", 80).system[0].text
    assert "are you working at the" in detector
    assert "fact from their resume or pay" in detector


def test_a_standup_is_told_about_the_hand_off_and_to_leave_out_tool_bookkeeping() -> None:
    ctx = prompts.PromptContext("Sam Lee", "standup", None, "pack", company="Fernhill", role="")
    system = prompts.fast_pass(ctx, "", "q", None, 100).system[0].text
    assert "asks for that update" in system
    assert "counts of files and tests" in system
    # Interview prompts stay as they were at work-only rules.
    interview = prompts.PromptContext("Sam Lee", "interview", "digest", None)
    assert "counts of files" not in prompts.fast_pass(interview, "", "q", None, 100).system[0].text
    detector = prompts.detector("Sam Lee", "", "u", 80, "standup", "turns").system[0].text
    assert "an open hand-off to Sam Lee" in detector
    assert "open hand-off" not in prompts.detector("Sam Lee", "", "u", 80).system[0].text


def test_the_detector_hedges_as_early_as_the_fast_pass(settings_factory) -> None:
    s = settings_factory()
    assert s.detector_hedge_ms == s.fast_hedge_ms == 1200
    assert s.first_show_min_words == 8
