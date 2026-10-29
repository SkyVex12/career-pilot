"""The whole conversation, live, through a real server with scripted STT and models: the user's
own words from the mic stream, echo, consent, the conversation in the prompts, the automatic
deep pass and its cancellation, and resume. No network, no stack."""

import asyncio
import json
from typing import Any

from copilot_core import keys
from copilot_core.llm import LLMRequest
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)


async def session_log(env: Any, sid: str) -> list[dict[str, Any]]:
    raw = await env.client().xrange(keys.sess_log(sid))
    return [json.loads(f[b"e"]) for _, f in raw]


def requests_for(provider: ScriptedProvider, job: str) -> list[LLMRequest]:
    return [req for j, req in provider.requests if j == job]


def finals(c: ProtocolClient, channel: str) -> list[dict[str, Any]]:
    return [m for m in c.of_type("transcript.final") if m["channel"] == channel]


async def test_ready_tells_the_client_to_send_mic_audio(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        ready = await c.start()
        assert ready["me_audio"] is True
        assert "other" in ready["deep_auto_types"]
        assert "small_talk" not in ready["deep_auto_types"]
        await c.send({"type": "session.end"})
        await c.closed()
    async with GatewayServer(env.services(env.settings(me_stt=False, deep_pass_auto=False))) as gw:
        c = ProtocolClient(gw.url)
        ready = await c.start()
        assert ready["me_audio"] is False
        assert ready["deep_auto_types"] == []


async def test_the_users_words_are_transcribed_and_reach_both_passes(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?", capture_ms=0)
        q1 = await c.wait_type("question.detected", speculative=False)
        await c.wait_type("answer.done", question_id=q1["question_id"], pass_="deep")
        await c.say_me("We sharded it by account", capture_ms=3000, end_ms=3500)
        mine = await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        assert mine["text"] == "We sharded it by account"
        assert "echo" not in mine
        await c.say("Why by account and not by region?", capture_ms=6000)
        q2 = await c.wait(
            lambda m: (
                m["type"] == "question.detected"
                and not m["speculative"]
                and m["question_id"] != q1["question_id"]
            )
        )
        await c.wait_type("answer.done", question_id=q2["question_id"], pass_="deep")
        await c.send({"type": "session.end"})
        await c.closed()
    # The follow-up's detector, fast and deep requests all see what the user said.
    detector = requests_for(provider, "detector")[-1]
    fast = requests_for(provider, "fast")[-1]
    deep = requests_for(provider, "deep")[-1]
    for req in (detector, fast, deep):
        assert "Alex: We sharded it by account" in str(req.content)
        assert "Them: How did you scale the ledger?" in str(req.content)
    # Stored with consent, as a "me" segment.
    log = await session_log(env, c.session_id)
    me_segments = [e for e in log if e["kind"] == "segment" and e["channel"] == "me"]
    assert [e["text"] for e in me_segments] == ["We sharded it by account"]


async def test_the_users_words_are_not_stored_without_consent(env) -> None:
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(consent={"store_transcript": False})
        await c.say_me("My salary today is ninety", capture_ms=1000, end_ms=1500)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await c.send({"type": "session.end"})
        await c.closed()
    raw = [f[b"e"].decode() for _, f in await env.client().xrange(keys.sess_log(c.session_id))]
    assert not [e for e in raw if '"segment"' in e]
    assert "salary" not in "\n".join(raw)


async def test_the_other_sides_voice_in_the_mic_is_dropped_as_echo(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        # The mic picks up the question while the other side asks it.
        await c.frame(0, 0b01, b"", 0)
        await c.say_me("Tell me about your design", capture_ms=50, end_ms=300)
        from copilot_gateway.testing import text_frame_audio

        await c.frame(0, 0, text_frame_audio("Tell me about your design"), 100)
        await c.frame(0, 0b10, b"", 400)
        echo = await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        assert echo["echo"] is True
        assert echo["text"] == ""
        session = next(iter(gw.manager.sessions.values()))
        assert all(ln.channel == "them" for ln in session.conv.lines)
        # Speech of the user's own, away from the other side's, is kept.
        await c.say_me("I would start with the data model", capture_ms=5000, end_ms=5600)
        kept = await c.wait(
            lambda m: m["type"] == "transcript.final" and m["channel"] == "me" and m["text"]
        )
        assert kept["text"] == "I would start with the data model"
        await c.send({"type": "session.end"})
        await c.closed()
    # No partial of the echo was shown: the other side was speaking.
    assert not [
        m for m in c.of_type("transcript.partial") if m["channel"] == "me" and "design" in m["text"]
    ]
    log = await session_log(env, c.session_id)
    assert [e["kind"] for e in log if e["kind"] == "echo"] == ["echo"]
    assert "Tell me" not in json.dumps([e for e in log if e["kind"] in ("echo", "segment")
                                        and e.get("channel", "me") == "me"])  # fmt: skip


async def test_an_older_client_without_mic_audio_still_shows_turns(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.frame(1, 0b01, b"", 1000)
        await c.frame(1, 0b10, b"", 4500)
        await c.say("What would you change?", capture_ms=6000)
        await c.wait_type("answer.done", pass_="fast")
    assert not finals(c, "me")
    assert "(Alex spoke for 3 s)" in str(requests_for(provider, "fast")[-1].content)


async def test_deep_pass_starts_on_its_own_after_the_fast_pass(env) -> None:
    provider = ScriptedProvider("anthropic")
    order: list[str] = []

    def reply(job: str, req: Any) -> Reply:
        order.append(job)
        if job == "fast":
            return Reply(["- one\n", "- two\n"], chunk_delay_s=0.1)
        return default_reply(job, req)

    provider.reply = reply
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        # "other" (no longer only behavioral, technical, system design) gets a deep answer too.
        await c.send({"type": "question.force", "text": "Do you have questions for us?"})
        q = await c.wait_type("question.detected")
        assert q["question_type"] == "other"
        deep_done = await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
    fast_done = next(
        m for m in c.of_type("answer.done") if m["pass"] == "fast"
    )  # the deep pass starts only after this
    first_deep = next(m for m in c.of_type("answer.delta") if m["pass"] == "deep")
    assert first_deep["seq"] > fast_done["seq"]
    assert deep_done["seq"] > fast_done["seq"]
    assert order.index("deep") > order.index("fast")
    assert not c.of_type("error")


async def test_off_the_deep_pass_waits_for_expand(env) -> None:
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How does the ledger work?")
        q = await c.wait_type("question.detected", speculative=False)
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await asyncio.sleep(0.3)
        assert not [m for m in c.of_type("answer.delta") if m["pass"] == "deep"]
        await c.send({"type": "answer.expand", "question_id": q["question_id"]})
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")


async def test_a_newer_question_means_no_deep_tokens_for_the_old_one(env) -> None:
    provider = ScriptedProvider("anthropic")

    def slow_fast(job: str, req: Any) -> Reply:
        if job == "fast" and "ledger" in str(req.content).split("Question")[-1]:
            # Long enough (6 s) that the follow-up is confirmed while it still streams, however
            # the event loop schedules the turns.
            return Reply([f"- cue {i}\n" for i in range(60)], chunk_delay_s=0.1)
        return default_reply(job, req)

    provider.reply = slow_fast
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How does the ledger work?", capture_ms=0)
        q1 = await c.wait_type("question.detected", speculative=False)
        await c.wait(
            lambda m: m["type"] == "answer.delta" and m["question_id"] == q1["question_id"]
        )
        # Confirmed while the first question's fast pass still streams.
        await c.say("What would you change?", capture_ms=5000)
        q2 = await c.wait(
            lambda m: (
                m["type"] == "question.detected"
                and not m["speculative"]
                and m["question_id"] != q1["question_id"]
            )
        )
        err = await c.wait_type("answer.error", question_id=q1["question_id"])
        await c.wait_type("answer.done", question_id=q2["question_id"], pass_="deep")
        await asyncio.sleep(0.2)
    assert (err["pass"], err["code"]) == ("fast", "cancelled")
    # The first question's deep pass never started: one deep call, the second question's.
    assert [j for j, _ in provider.calls].count("deep") == 1
    assert not [
        m
        for m in c.messages
        if m["type"] in ("answer.delta", "answer.done")
        and m["question_id"] == q1["question_id"]
        and m["pass"] == "deep"
    ]


async def test_the_conversation_survives_a_resume_on_another_task(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw1:
        c = ProtocolClient(gw1.url)
        await c.start()
        await c.say_me("I led the payments migration", capture_ms=1000, end_ms=1500)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await asyncio.sleep(0.3)  # the state reaches Valkey
        await c.drop()
        await gw1.manager.shutdown()
    async with GatewayServer(env.services()) as gw2:
        c.url = gw2.url
        await c.resume()
        await asyncio.sleep(0.2)
        await c.send({"type": "question.force", "text": "What did that migration change?"})
        q = await c.wait_type("question.detected")
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
    assert "Alex: I led the payments migration" in str(requests_for(provider, "fast")[-1].content)


async def test_a_long_call_is_summarized_in_the_background(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    settings = env.settings(
        conversation_budget_tokens=400,
        conversation_summary_tokens=100,
        conversation_answers_tokens=100,
        conversation_summary_chunk_tokens=60,
    )
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        for i in range(8):
            words = " ".join(f"point{i}x{k}" for k in range(12))
            await c.say_me(f"So {words}", capture_ms=i * 2000, end_ms=i * 2000 + 500)
            await c.wait(
                lambda m, i=i: m["type"] == "transcript.final" and f"point{i}x0" in m["text"]
            )
        session = next(iter(gw.manager.sessions.values()))
        await session.conv.wait_folded()
        calls = [j for j, _ in provider.calls].count("summary")
        assert 1 <= calls < 8  # in chunks, never per line
        await c.send({"type": "question.force", "text": "Anything else?"})
        await c.wait_type("answer.done", pass_="fast")
        await c.send({"type": "session.end"})
        await c.closed()
    fast = str(requests_for(provider, "fast")[-1].content)
    assert "<call_summary>\n- Earlier:" in fast
    assert "point0x0" not in fast.split("<transcript>")[-1]  # summarized, not verbatim
    log = await session_log(env, c.session_id)
    summaries = [e for e in log if e["kind"] == "summary"]
    assert len(summaries) == calls
    assert all("text" not in e and e["input_tokens"] > 0 for e in summaries)


async def test_a_standup_detector_is_told_when_the_user_last_spoke(env) -> None:
    # 2026-10-27: in a two-person standup the detector turned down "you're still on X, right?"
    # seconds after the user spoke, guessing that "Them" were several people talking among
    # themselves. It now gets the turn-taking as a fact.
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(mode="standup")
        await c.say("Morning. Anything to raise before I share my screen?", capture_ms=0)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "them")
        await c.say_me("I finished the retry fix yesterday", capture_ms=3000, end_ms=3500)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await c.say("So you're still on the label migration, right?", capture_ms=8000)
        await c.wait(lambda m: m["type"] == "transcript.final" and "migration" in m["text"])
        await asyncio.sleep(0.3)
        await c.send({"type": "session.end"})
        await c.closed()
    first, *_, last = requests_for(provider, "detector")
    assert "<turn_taking>\nAlex has not spoken yet.\n</turn_taking>" in str(first.content)
    assert "Alex last spoke less than 20 s ago." in str(last.content)
    assert "changed between Them and Alex once" in str(last.content)
    assert "may be one person or several" in last.system[0].text


async def test_a_summary_cut_off_at_its_cap_is_not_kept(env) -> None:
    # 2026-10-27: the 7th summary call ended at exactly its 500-token cap, so its newest
    # notes were probably cut off and stored that way.
    def reply(job: str, req: Any) -> Reply:
        if job == "summary":
            return Reply(["- Earlier: the first half of a summa"], stop_reason="max_tokens")
        return default_reply(job, req)

    provider = ScriptedProvider("anthropic", reply=reply)
    env.providers = [provider]
    settings = env.settings(
        conversation_budget_tokens=400,
        conversation_summary_tokens=100,
        conversation_answers_tokens=100,
        conversation_summary_chunk_tokens=60,
    )
    async with GatewayServer(env.services(settings)) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        for i in range(8):
            words = " ".join(f"point{i}x{k}" for k in range(12))
            await c.say_me(f"So {words}", capture_ms=i * 2000, end_ms=i * 2000 + 500)
            await c.wait(
                lambda m, i=i: m["type"] == "transcript.final" and f"point{i}x0" in m["text"]
            )
        session = next(iter(gw.manager.sessions.values()))
        await session.conv.wait_folded()
        assert [j for j, _ in provider.calls].count("summary") >= 1
        assert session.conv.summary == ""
        await c.send({"type": "session.end"})
        await c.closed()
    log = await session_log(env, c.session_id)
    assert {e["stop_reason"] for e in log if e["kind"] == "summary"} == {"max_tokens"}


# Following the user (2026-10-28, the user's call): once the user is answering, the answer
# follows them, whether or not a question was detected.


async def test_the_answer_follows_the_user_when_no_question_fired(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        # A statement: the scripted detector says none, so nothing is shown.
        await c.say("So each client pod has one integrations engineer on it", capture_ms=0)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "them")
        await asyncio.sleep(0.6)
        assert c.of_type("question.detected") == []
        await c.say_me(
            "Right, at my last job I owned the CRM sync for four client accounts myself",
            capture_ms=3000, end_ms=6000,
        )  # fmt: skip
        q = await c.wait_type("question.detected", timeout=3.0)
        assert q["question_type"] == "other"  # "follow" on the gateway; the protocol has none
        assert q["text"] == "So each client pod has one integrations engineer on it"
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="fast")
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
        # One card per turn of the other side, however long the user keeps talking.
        await c.say_me(
            "and I wrote the field mappings and the retry jobs for every one of those accounts",
            capture_ms=6500, end_ms=9000,
        )  # fmt: skip
        await asyncio.sleep(0.6)
        assert len(c.of_type("question.detected")) == 1
    fast = requests_for(provider, "fast")[-1]
    content = str(fast.content)
    assert "Question (follow): So each client pod has one integrations engineer" in content
    assert "I owned the CRM sync for four client accounts" in content
    assert "type follow" in fast.system[0].text
    deep = requests_for(provider, "deep")[-1]
    assert "continue from where their lines in the transcript" in str(deep.content)


async def test_no_follow_for_small_talk_or_a_goodbye(env) -> None:
    def reply(job: str, req: LLMRequest) -> Reply:
        if job == "detector" and "how are you" in str(req.content).lower():
            return Reply([json.dumps({"answer": "small_talk"})])
        return default_reply(job, req)

    env.providers = [ScriptedProvider("anthropic", reply=reply)]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("Hi Alex, how are you doing today", capture_ms=0)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "them")
        await asyncio.sleep(0.6)
        await c.say_me(
            "Doing really well thanks, busy week with the migration work and two releases",
            capture_ms=2500, end_ms=5000,
        )  # fmt: skip
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        await c.say("Well it was great talking with you today", capture_ms=8000)
        await c.wait(lambda m: m["type"] == "transcript.final" and "great talking" in m["text"])
        await asyncio.sleep(0.6)
        await c.say_me(
            "Yeah thank you so much, it was great talking with you too, have a great day",
            capture_ms=11000, end_ms=13000,
        )  # fmt: skip
        await asyncio.sleep(1.0)
        assert c.of_type("question.detected") == []


async def test_a_follow_after_an_answered_question_keeps_its_answer(env) -> None:
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How did you scale the ledger?", capture_ms=0)
        q1 = await c.wait_type("question.detected", speculative=False)
        await c.wait_type("answer.done", question_id=q1["question_id"], pass_="fast")
        await c.say_me(
            "We split the ledger by account and moved the reads onto replicas behind a cache",
            capture_ms=2000, end_ms=5000,
        )  # fmt: skip
        q2 = await c.wait(
            lambda m: m["type"] == "question.detected" and m["question_id"] != q1["question_id"]
        )
        assert q2["text"] == "How did you scale the ledger?"
        await c.wait_type("answer.done", question_id=q2["question_id"], pass_="fast")
        # The follow does not cut off the answer the user was reading.
        await c.wait_type("answer.done", question_id=q1["question_id"], pass_="deep")
        cancelled = [m for m in c.of_type("answer.error") if m["question_id"] == q1["question_id"]]
        assert cancelled == []
    # Never served from the exact-repeat cache: it continues the user, not the question.
    fast = requests_for(provider, "fast")
    assert any("Question (follow): How did you scale the ledger?" in str(r.content) for r in fast)


async def test_the_detailed_answer_follows_the_chosen_style_and_the_shown_cues(env) -> None:
    # The Session tab's choice: paragraph from the start, then bullets mid-call. The deep pass
    # sees the talking points already on screen, so it goes beyond them.
    provider = ScriptedProvider("anthropic")
    env.providers = [provider]
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.start(answer_style="paragraph")
        await c.say("How did you scale the ledger?", capture_ms=0)
        q1 = await c.wait_type("question.detected", speculative=False)
        await c.wait_type("answer.done", question_id=q1["question_id"], pass_="deep")
        await c.send({"type": "answer_style.set", "answer_style": "bullets"})
        await c.say("What did you change in the payout service?", capture_ms=8000)
        q2 = await c.wait(
            lambda m: (
                m["type"] == "question.detected"
                and not m["speculative"]
                and m["question_id"] != q1["question_id"]
            )
        )
        await c.wait_type("answer.done", question_id=q2["question_id"], pass_="deep")
        await c.send({"type": "session.end"})
        await c.closed()
    first, second = [str(r.content) for r in requests_for(provider, "deep")][:2]
    assert "Style: paragraph" in first
    assert "Style: bullets" in second
    assert "<shown_cues>\n- Led the payments migration [n:3f2a]" in first
    log = await session_log(env, c.session_id)
    assert {"kind": "answer_style", "answer_style": "bullets"}.items() <= next(
        e for e in log if e["kind"] == "answer_style"
    ).items()


async def test_a_reply_gets_its_detailed_answer_without_a_tap(env) -> None:
    # 2026-10-28: the HubSpot question went in as a Respond, which was not an automatic deep
    # type, so the detailed answer waited behind "Get the detailed answer".
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        ready = await c.start()
        assert "respond" in ready["deep_auto_types"]
        assert "follow" not in ready["deep_auto_types"]  # the client knows no such type
        await c.say("Remember to send the numbers by Friday", capture_ms=0)
        await c.wait(lambda m: m["type"] == "transcript.final" and "Friday" in m["text"])
        await c.send({"type": "question.force", "kind": "respond"})
        q = await c.wait_type("question.detected")
        await c.wait_type("answer.done", question_id=q["question_id"], pass_="deep")
