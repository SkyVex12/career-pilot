"""Generated cues remain separate from speech and do not repeat across follow-ups."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.packs import PreparedAnswer
from copilot_gateway.answer import (
    AnswerEngine,
    AnswerHost,
    MatchResult,
    Question,
    ScreenAsk,
    question_hash,
)
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    Reply,
    ScriptedProvider,
    default_reply,
)

PRIOR = (
    "- I would ship the schema change behind a feature flag, validate the backfill against "
    "the source rows, and watch the error rate before shifting reads."
)
FRESH = "- Start with row counts and reconcile a sample of records before enabling new reads."


def question(at: int, text: str = "How would you roll out the change?", **kwargs: Any) -> Question:
    q = Question(
        qid=uuid4(),
        text=text,
        qtype="technical",
        confidence=1,
        addressed=True,
        speculative=False,
        forced=False,
        t0=float(at),
        detector_ms=None,
        match=None,
        confirmed=True,
        fast_ended=True,
        fast_text=PRIOR,
    )
    return replace(q, **kwargs)


def engine_for(settings: Any, call: prompts.CallContext, mode: str = "interview") -> AnswerEngine:
    host = SimpleNamespace(
        settings=settings,
        prompt_context=lambda: prompts.PromptContext("Alex", mode, "Verified profile", None),
        call_context=lambda: call,
        answer_style=lambda: "bullets",
    )
    return AnswerEngine(cast(AnswerHost, host))


def block(content: str, tag: str) -> str:
    return content.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0]


@pytest.mark.parametrize("pass_", ["fast", "deep"])
def test_prior_cues_are_separate_from_user_speech_and_current_cues(settings_factory, pass_) -> None:
    call = prompts.CallContext(
        recent="Them: How would you validate the backfill?",
        summary="The rollout is under discussion.",
        answers="Q: What did you check?\nAlex said: I compared row counts.",
    )
    engine = engine_for(settings_factory(), call)
    old = question(1, superseded=True)
    current = question(20, "How would you validate the backfill?", fast_text=FRESH)
    invalid = [
        question(2, fast_text="hidden", held=True),
        question(3, fast_text="cancelled", cancelled=True),
        question(4, fast_text="discarded", discarded=True),
        question(5, fast_text=" "),
        question(6, fast_text="unfinished", fast_ended=False),
        question(7, fast_text="unconfirmed", confirmed=False),
        question(8, fast_text="screen-only", screen=ScreenAsk((), "interview", None, "Python")),
        question(21, fast_text="future cue"),
    ]
    for q in [old, *invalid, current]:
        engine.questions[q.qid] = q
    req = engine._request(current, pass_)
    content = str(req.content)
    suggestions = block(content, "earlier_suggestions")
    assert PRIOR in suggestions
    assert all(q.fast_text not in suggestions for q in invalid if q.fast_text.strip())
    assert FRESH not in suggestions
    assert block(content, "earlier_answers") == call.answers
    assert block(content, "transcript") == call.recent
    assert block(content, "call_summary") == call.summary
    assert call.suggestions == ""
    if pass_ == "deep":
        assert block(content, "shown_cues") == FRESH


def test_prior_cues_have_a_small_bound_and_apply_to_regular_meeting_answers(
    settings_factory,
) -> None:
    engine = engine_for(settings_factory(), prompts.CallContext())
    old = [
        question(i, text=f"Question {i} " + "x" * 400, fast_text=f"Cue {i} " + "y" * 1500)
        for i in range(5)
    ]
    current = question(10)
    for q in [*old, current]:
        engine.questions[q.qid] = q
    content = str(engine._request(current, "fast").content)
    suggestions = block(content, "earlier_suggestions")
    assert len(suggestions) < 2400
    assert "Cue 0" not in suggestions
    assert "Cue 1" not in suggestions
    assert all(f"Cue {i}" in suggestions for i in (2, 3, 4))
    workplace = engine_for(settings_factory(), prompts.CallContext(), "meeting")
    workplace.questions = engine.questions
    assert (
        block(str(workplace._request(current, "fast").content), "earlier_suggestions")
        == suggestions
    )


@pytest.mark.parametrize(
    ("forced", "short", "expected"),
    [(False, False, True), (True, False, False), (False, True, False)],
)
def test_direct_reuse_guard_preserves_explicit_repeats_and_brief_acknowledgements(
    settings_factory, forced, short, expected
) -> None:
    engine = engine_for(settings_factory(), prompts.CallContext())
    old = question(1, fast_text="Yes, please." if short else PRIOR)
    current = question(2, forced=forced)
    engine.questions[old.qid] = old
    assert engine._repeats_cues(current, "  " + old.fast_text.replace(" ", "\n") + "  ") is expected
    assert not engine._repeats_cues(current, FRESH)


async def test_prepared_repeat_uses_new_generation_but_forced_repeat_keeps_the_match(
    settings_factory,
) -> None:
    engine = engine_for(settings_factory(), prompts.CallContext())
    old = question(1)
    current = question(2)
    engine.questions[old.qid] = old
    prepared = PreparedAnswer(question=old.text, talking_points=[PRIOR.removeprefix("- ")])

    async def matched() -> MatchResult:
        return MatchResult(prepared, 1, 0)

    current.match = asyncio.create_task(matched())
    await current.match
    assert engine._prepared(current) is None
    current.forced = True
    assert engine._prepared(current) == prepared


async def test_cached_repeated_answer_is_regenerated_with_separate_prior_cues(env) -> None:
    fast_calls = 0

    def reply(job: str, req: Any) -> Reply:
        nonlocal fast_calls
        if job == "fast":
            fast_calls += 1
            return Reply([PRIOR if fast_calls == 1 else FRESH])
        return default_reply(job, req)

    provider = ScriptedProvider("anthropic", reply=reply)
    env.providers = [provider]
    async with GatewayServer(env.services(env.settings(deep_pass_auto=False))) as gw:
        c = ProtocolClient(gw.url)
        await c.start()
        await c.say("How would you roll out the schema change?", capture_ms=0)
        first = await c.wait_type("answer.done", pass_="fast")
        await c.wait_type("question.detected", question_id=first["question_id"], speculative=False)
        await c.say_me("I compared row counts.", capture_ms=3000, end_ms=3500)
        await c.wait(lambda m: m["type"] == "transcript.final" and m["channel"] == "me")
        session = next(iter(gw.manager.sessions.values()))
        followup = "How would you validate the backfill?"
        await session.qa_put(
            question_hash(followup),
            {
                "v": session.cache_version(),
                "fast": {"text": PRIOR, "refs": []},
            },
        )
        await c.say(followup, capture_ms=6000)
        second = await c.wait(
            lambda m: (
                m["type"] == "answer.done"
                and m["pass"] == "fast"
                and m["question_id"] != first["question_id"]
            )
        )
        assert second["source"] == "model"
        fast_requests = [req for job, req in provider.requests if job == "fast"]
        content = str(fast_requests[-1].content)
        assert PRIOR in block(content, "earlier_suggestions")
        assert PRIOR not in block(content, "transcript")
        assert "Alex: I compared row counts." in block(content, "transcript")
        assert PRIOR not in session.call_context().answers
        assert fast_calls == 2
