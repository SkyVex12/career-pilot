"""Prepared provenance/freshness checks, not semantic approval of generated claims."""

import asyncio
import time
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.llm import LLMDone, LLMUsage
from copilot_core.packs import (
    PREPARED_DIGEST_POLICY,
    PREPARED_STANDUP_POLICY,
    Digest,
    PackItem,
    PreparedAnswer,
    SessionPack,
    StandupBrief,
    render_pack,
)
from copilot_core.workplace import standup_answers
from copilot_gateway.answer import AnswerEngine, AnswerHost, MatchResult, Question
from copilot_gateway.session import Session

QUESTION = "Where are you based?"
PROFILE = "Alex is based in Chicago."


class Router:
    def __init__(self):
        self.routes = {}
        self.requests = []
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.closed = False

    async def stream(self, job, request, on_fallback, *, on_attempt=None):
        self.requests.append(request)
        try:
            self.started.set()
            yield "I am based in Austin now."
            if self.gate is not None:
                await self.gate.wait()
            yield LLMDone(LLMUsage(50, 10), "end_turn", "test", "gpt-6-luna")
        finally:
            self.closed = True


def engine_for(settings_factory, *, architecture="adaptive", call=None, profile=PROFILE):
    events, logs = [], []
    router = Router()
    digest = Digest.model_validate({"version": Digest.version_of(profile), "text": profile})

    async def memory(*args):
        return [], {}

    async def get(*args):
        return None

    async def put(*args):
        return None

    host = SimpleNamespace(
        settings=settings_factory(answer_architecture=architecture, deep_pass_auto=False),
        router=router,
        digest=digest,
        pack=None,
        company_memory=None,
        emit=lambda cls, **fields: events.append((cls.__name__, fields)),
        log_entry=logs.append,
        store_text=lambda: True,
        call_context=lambda: call or prompts.CallContext(recent=f"[00:01] Them: {QUESTION}"),
        prompt_context=lambda: prompts.PromptContext("Alex", "interview", host.digest.text, None),
        refs=lambda: {},
        llm_status=lambda ok: None,
        cache_version=lambda: "test",
        cache_extra=lambda: {},
        answer_style=lambda: "bullets",
        response_length=lambda: "default",
        memory_lines=memory,
        qa_get=get,
        qa_put=put,
    )
    host.prepared_source_version = lambda policy: Session.prepared_source_version(
        cast(Session, host), policy
    )
    return AnswerEngine(cast(AnswerHost, host)), host, events, logs


def prepared(**overrides):
    return PreparedAnswer(
        **{
            "question": QUESTION,
            "talking_points": ["I am based in Chicago."],
            "grounding_policy": PREPARED_DIGEST_POLICY,
            "evidence_version": Digest.version_of(PROFILE),
            **overrides,
        }
    )


async def matched(answer):
    async def result():
        return MatchResult(answer, 1.0, 0)

    task = asyncio.create_task(result())
    await task
    return Question(
        qid=uuid4(),
        text=QUESTION,
        qtype="other",
        confidence=1,
        addressed=True,
        speculative=False,
        forced=False,
        t0=time.monotonic(),
        detector_ms=0,
        match=task,
    )


@pytest.mark.parametrize("architecture", ["adaptive", "adaptive_hybrid", "adaptive_on_demand"])
async def test_current_versioned_prepared_answer_keeps_zero_model_fast_path(
    settings_factory, architecture
):
    engine, host, events, logs = engine_for(settings_factory, architecture=architecture)
    answer = prepared()
    q = await matched(answer)
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert host.router.requests == []
    assert engine.stats["prepared_hits"] == 1
    done = next(value for kind, value in events if kind == "AnswerDone")
    assert done["source"] == "prepared"
    assert done["delta_count"] == 1
    assert next(row for row in logs if row["kind"] == "answer")["source"] == "prepared"


@pytest.mark.parametrize(
    "overrides",
    [
        {"grounding_policy": None},
        {"grounding_policy": "human_verified"},  # No approval workflow exists to establish this.
        {"evidence_version": None},
        {"evidence_version": "old-version"},
        {"talking_points": ["I have Epic experience [~]"]},
    ],
)
async def test_candidate_rejects_unversioned_unknown_or_stale_provenance(
    settings_factory, overrides
):
    engine, _, _, _ = engine_for(settings_factory)
    assert engine._prepared(await matched(prepared(**overrides))) is None


async def test_legacy_prepared_eligibility_does_not_require_new_metadata(settings_factory):
    engine, _, _, _ = engine_for(
        settings_factory,
        architecture="legacy_dual",
        call=prompts.CallContext(recent="Alex: I moved to Austin."),
    )
    answer = prepared(grounding_policy=None, evidence_version=None)
    assert engine._prepared(await matched(answer)) is answer


@pytest.mark.parametrize(
    "call",
    [
        prompts.CallContext(recent=f"[00:01] Alex: I moved to Austin.\n[00:02] Them: {QUESTION}"),
        prompts.CallContext(
            recent=f"[00:01] Them: We changed the location to Austin.\nThem: {QUESTION}"
        ),
        prompts.CallContext(summary="Alex now lives in Austin.", recent=f"Them: {QUESTION}"),
        prompts.CallContext(answers="Earlier answer: Austin.", recent=f"Them: {QUESTION}"),
    ],
)
async def test_live_conversation_evidence_bypasses_prepared_without_semantic_guess(
    settings_factory, call
):
    engine, host, events, _ = engine_for(settings_factory, call=call)
    q = await matched(prepared())
    engine.add(q)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(host.router.requests) == 1
    assert "Austin" in str(host.router.requests[0].content)
    assert engine.stats["prepared_hits"] == 0
    assert all(row["source"] != "prepared" for kind, row in events if kind == "AnswerDone")


async def test_changed_digest_text_rejects_old_prepared_even_with_stale_declared_version(
    settings_factory,
):
    engine, host, _, _ = engine_for(settings_factory)
    host.digest.text = "Alex moved to Austin."
    assert host.digest.version == Digest.version_of(PROFILE)
    assert engine._prepared(await matched(prepared())) is None


async def test_generated_suggestions_are_not_conversational_evidence(settings_factory):
    engine, _, _, _ = engine_for(
        settings_factory,
        call=prompts.CallContext(recent=f"Them: {QUESTION}", suggestions="An old AI suggestion."),
    )
    assert engine._prepared(await matched(prepared())) is not None


def pack_with(brief, answer):
    return SessionPack(  # pyright: ignore[reportCallIssue]
        pack_id=uuid4(),
        user_id=uuid4(),
        mode="standup",
        built_at="2026-11-25T00:00:00Z",
        digest_version=Digest.version_of(PROFILE),
        standup=brief,
        prepared_answers=[answer],
    )


def test_compiled_standup_source_version_changes_with_status_and_requires_freshness_proof():
    brief = StandupBrief(today=[PackItem(node_id=uuid4(), text="Release", state="planned")])
    answer = standup_answers(brief)[0]
    host = SimpleNamespace(company_memory=None, pack=pack_with(brief, answer))
    assert answer.grounding_policy == PREPARED_STANDUP_POLICY
    assert (
        Session.prepared_source_version(cast(Session, host), PREPARED_STANDUP_POLICY)
        == answer.evidence_version
    )
    brief.today[0].state = "blocked"
    assert (
        Session.prepared_source_version(cast(Session, host), PREPARED_STANDUP_POLICY)
        != answer.evidence_version
    )
    host.company_memory = object()
    assert Session.prepared_source_version(cast(Session, host), PREPARED_STANDUP_POLICY) is None


def test_empty_worklog_blockers_does_not_assert_absence_of_real_blockers():
    brief = StandupBrief(today=[PackItem(node_id=uuid4(), text="Release", state="planned")])
    answers = {a.question: a for a in standup_answers(brief)}
    assert answers["Is anything blocking you?"].talking_points == [
        "No blockers are recorded in the work log"
    ]


def test_candidate_pack_render_omits_generated_points_but_keeps_original_evidence():
    brief = StandupBrief(today=[PackItem(node_id=uuid4(), text="Original evidence: planned")])
    pack = pack_with(brief, prepared(talking_points=["Stale generated claim: completed"]))
    assert "Stale generated claim" in render_pack(pack)
    candidate = render_pack(pack, include_prepared=False)
    assert "Stale generated claim" not in candidate
    assert "## Prepared answers" not in candidate
    assert "Original evidence: planned" in candidate


async def test_valid_held_prepared_answer_waits_and_revalidates_without_a_model(settings_factory):
    engine, host, events, _ = engine_for(settings_factory)
    q = await matched(prepared())
    q.held = True
    engine.add(q)
    await asyncio.sleep(0)
    assert not q.held_text
    assert not events
    assert not host.router.requests
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert not host.router.requests
    assert [value["source"] for kind, value in events if kind == "AnswerDone"] == ["prepared"]


@pytest.mark.parametrize("change", ["conversation", "digest"])
async def test_held_prepared_fact_changed_before_release_never_flashes_old_text(
    settings_factory, change
):
    engine, host, events, _ = engine_for(settings_factory)
    q = await matched(prepared())
    q.held = True
    engine.add(q)
    await asyncio.sleep(0)
    if change == "conversation":
        host.call_context = lambda: prompts.CallContext(
            recent=f"[00:01] Alex: I moved to Austin.\n[00:02] Them: {QUESTION}"
        )
    else:
        host.digest.text = "Alex moved to Austin."
    engine.release(q.qid)
    assert not events
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    deltas = [row["text"] for kind, row in events if kind == "AnswerDelta"]
    assert deltas == ["I am based in Austin now."]
    assert len(host.router.requests) == 1
    assert engine.stats["prepared_hits"] == 0
    assert "Austin" in str(host.router.requests[0])


async def test_late_prepared_match_keeps_active_candidate_model_progress(settings_factory):
    engine, host, events, logs = engine_for(settings_factory)
    answer = prepared()
    match_ready = asyncio.Event()

    async def match_later():
        await match_ready.wait()
        return MatchResult(answer, 1.0, 0)

    q = await matched(None)
    q.match = asyncio.create_task(match_later())
    q.held = True
    host.router.gate = asyncio.Event()
    engine.add(q)
    await host.router.started.wait()
    match_ready.set()
    await q.match
    assert engine._prepared(q) is answer
    assert not host.router.closed
    host.router.gate.set()
    engine.release(q.qid)
    engine.confirm(q.qid, q.text, 0)
    await engine.wait_idle()
    assert len(host.router.requests) == 1
    assert engine.stats["prepared_hits"] == 0
    assert all(row["source"] == "model" for kind, row in events if kind == "AnswerDone")
    assert not any(row["kind"] == "pass_cancelled" for row in logs)


@pytest.mark.parametrize("architecture", ["legacy_dual", "adaptive_on_demand"])
def test_session_prompt_uses_source_evidence_without_rejected_prepared_claims(
    settings_factory, architecture
):
    _, host, _, _ = engine_for(settings_factory, architecture=architecture)
    host.pack = pack_with(StandupBrief(), prepared(talking_points=["Old generated location"]))
    host.info = SimpleNamespace(employer_id=None, mode="interview")
    host.principal = SimpleNamespace(display_name="Alex")
    host.spoken_language = "en"
    context = Session._build_prompt_ctx(cast(Session, host))
    assert context.digest_text == PROFILE
    assert ("Old generated location" in (context.pack_text or "")) == (
        architecture == "legacy_dual"
    )
