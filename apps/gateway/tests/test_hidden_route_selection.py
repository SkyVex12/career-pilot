"""Earlier hidden route selection keeps the normal-release request contract."""

import asyncio
import time
from copy import deepcopy
from types import MethodType, SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from copilot_core import prompts
from copilot_core.packs import PreparedAnswer
from copilot_gateway.answer import AnswerEngine, AnswerHost, MatchResult, Question
from copilot_gateway.router import build_router
from copilot_gateway.session import Session
from copilot_gateway.testing import Reply, ScriptedProvider
from copilot_gateway.turn import Detection

TEXT = "Why do transaction retries need an idempotency key?"
EVIDENCE = "[n:ab12] Original verified evidence."
METADATA = ("qtype", "confidence", "addressed", "detector_ms", "decided_ms", "depth", "detected_at")


class Provider(ScriptedProvider):
    def __init__(self):
        super().__init__(
            "openai", lambda _job, _req: Reply(["Use a durable idempotency key for every retry."])
        )
        self.text_consumed = asyncio.Event()
        self.params = []

    async def stream(self, model, req, params):
        self.params.append(deepcopy(params))
        async for chunk in super().stream(model, req, params):
            yield chunk
            if isinstance(chunk, str):
                self.text_consumed.set()


@pytest.fixture
async def harness(settings_factory):
    created = []

    def make(*, mode: str = "interview", **changes: Any):
        settings = settings_factory(
            **{
                "llm_providers": ["openai"],
                "answer_architecture": "adaptive",
                "adaptive_technical_legacy": True,
                "adaptive_expert_route": {
                    "model": "gpt-6.2-sol",
                    "reasoning": "low",
                    "service_tier": "fast",
                    "retry_tier": "fast",
                    "first_token_timeout_ms": 20000,
                    "hedge_ms": None,
                },
                "openai_detector_model": "gpt-6-luna",
                "openai_deep_model": "gpt-6.2-sol",
                "openai_fast_model": "gpt-6-luna",
                "openai_fast_reasoning": "none",
                "prewarm_jobs": [],
                "fast_hedge_ms": None,
                "deep_hedge_ms": None,
                "deep_pass_auto": False,
                **changes,
            }
        )
        provider = Provider()
        events, logs, writes = [], [], []
        entered, release = asyncio.Event(), asyncio.Event()

        async def memory(_text, _pass):
            entered.set()
            await release.wait()
            return [EVIDENCE], {"memory_hits": 1}

        async def get(_key):
            return None

        async def put(_key, value):
            writes.append(value)

        host = SimpleNamespace(
            settings=settings,
            router=build_router(settings, [provider]),
            ending=False,
            lost=False,
            emit=lambda cls, **fields: events.append((cls.__name__, fields)),
            log_entry=logs.append,
            store_text=lambda: True,
            call_context=lambda: prompts.CallContext(recent="Original speaker."),
            prompt_context=lambda: prompts.PromptContext("Alex", mode, "Verified profile", None),
            refs=lambda: {},
            llm_status=lambda _ok: None,
            cache_version=lambda: "hidden-route-fixture",
            answer_style=lambda: "bullets",
            response_length=lambda: "default",
            cache_extra=lambda: {},
            memory_lines=memory,
            qa_get=get,
            qa_put=put,
            _continued=lambda text: text,
            _remember_automatic=lambda *args, **kwargs: None,
        )
        engine = AnswerEngine(cast(AnswerHost, host))
        host.answers = engine
        host.info = SimpleNamespace(mode=mode)
        host._retention_failed = False
        host._named_detections = {}
        host._forced_detections = {}
        host._forced = set()
        host.conv = SimpleNamespace(question=lambda *args: None)
        host.capture_now = 1000
        host.release_question = MethodType(Session.release_question, host)
        lab = SimpleNamespace(
            session=cast(Session, host),
            engine=engine,
            provider=provider,
            events=events,
            logs=logs,
            writes=writes,
            entered=entered,
            release=release,
        )
        created.append(lab)
        return lab

    yield make
    for lab in created:
        lab.release.set()
        assert await lab.engine.shutdown(within_s=1)
        await asyncio.gather(*lab.engine._background)


def metadata(q: Question) -> tuple[Any, ...]:
    return tuple(getattr(q, field) for field in METADATA)


async def held(lab, text: str = TEXT) -> Question:
    qid = Session.start_held(lab.session, text, time.monotonic(), None)
    await asyncio.wait_for(lab.entered.wait(), 1)
    q = lab.engine.questions[qid]
    assert q.held
    assert not lab.events
    assert not lab.provider.requests
    return q


@pytest.mark.parametrize("depth", [None, "light", "deep"])
async def test_hidden_selection_matches_normal_release_request(harness, depth):
    results = []
    detection = Detection("answer", "technical", latency_ms=37, decided_ms=41, depth=depth)
    for select_early in (False, True):
        lab = harness()
        old = await held(lab)
        snapshot = old.snapshots["fast"]
        lab.session.call_context = lambda: prompts.CallContext(recent="Later unrelated speaker.")
        if select_early:
            assert Session.classify_held(lab.session, old.qid, TEXT, detection)
            current = lab.engine.questions[old.qid]
            assert current is not old
            assert old.discarded
            assert current.snapshots["fast"] is snapshot
            assert current.held
            assert current.hidden_classified
            assert not current.exposed
            assert not current.answer_exposed
            assert not current.released.is_set()
            assert not current.confirmed
            assert current.t0 == old.t0
            assert current.qtype == "technical"
            assert current.depth == depth
            assert current.detector_ms == 37
            assert current.decided_ms == 41
            assert not lab.events
            # Reapplying a decision never starts a second replacement or changes metadata.
            frozen = metadata(current)
            assert not Session.classify_held(lab.session, old.qid, TEXT, detection)
            assert lab.engine.questions[old.qid] is current
            assert metadata(current) == frozen
            lab.release.set()
            # The real router may complete in the background; the answer must remain held.
            await asyncio.wait_for(lab.provider.text_consumed.wait(), 1)
            assert current.held_text
            assert not lab.events
        Session.release_question(lab.session, old.qid, TEXT, detection, speculative=False)
        lab.release.set()
        await asyncio.wait_for(lab.engine.wait_idle(), 1)
        await asyncio.gather(*lab.engine._background)
        current = lab.engine.questions[old.qid]
        assert len(lab.provider.requests) == len(lab.provider.calls) == 1
        model = lab.provider.calls[0][1]
        request = lab.provider.requests[0][1]
        assert model == ("gpt-6.2-sol" if depth == "deep" else "gpt-6-luna")
        assert request.max_tokens == (1200 if depth == "deep" else 250)
        assert EVIDENCE in str(request.content)
        assert "Original speaker." in str(request.content)
        assert "Later unrelated speaker." not in str(request.content)
        assert sum(kind == "AnswerDone" for kind, _ in lab.events) == 1
        assert sum(kind == "QuestionDetected" for kind, _ in lab.events) == 1
        assert not any(kind in {"QuestionCancelled", "AnswerError"} for kind, _ in lab.events)
        results.append(
            (model, request, lab.provider.params, lab.engine.strategy(current), current.plans)
        )
    assert results[0] == results[1]


@pytest.mark.parametrize(
    ("qtype", "depth"), [("small_talk", None), ("behavioral", "light"), ("other", None)]
)
async def test_unchanged_route_restores_metadata_and_frozen_request(harness, qtype, depth):
    results = []
    for classify in (False, True):
        lab = harness()
        old = await held(lab, "Can you hear me?")
        original = metadata(old)
        if classify:
            assert not Session.classify_held(
                lab.session,
                old.qid,
                old.text,
                Detection("answer", qtype, latency_ms=29, decided_ms=31, depth=depth),
            )
            assert metadata(old) == original
            assert not old.hidden_classified
            assert lab.engine.questions[old.qid] is old
            assert not lab.events
        lab.release.set()
        await asyncio.wait_for(lab.provider.text_consumed.wait(), 1)
        assert old.held_text
        request = lab.provider.requests[0][1]
        assert lab.provider.calls[0][1] == lab.session.settings.openai_fast_model
        assert "Question (unclassified)" in str(request.content)
        assert not lab.events
        lab.engine.release(old.qid)
        await asyncio.wait_for(lab.engine.wait_idle(), 1)
        results.append((lab.provider.calls, request, lab.provider.params))
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "stale_text",
        "case_changed",
        "wait",
        "none",
        "duplicate",
        "error",
        "exposed",
        "answer_exposed",
        "not_held",
        "confirmed",
        "forced",
        "cancelled",
        "discarded",
        "superseded",
        "released",
        "ending",
        "lost",
        "closing",
    ],
)
async def test_hidden_selection_ignores_ineligible_questions(harness, case):
    lab = harness()
    q = await held(lab)
    qid, text = q.qid, q.text
    detection = Detection("answer", "technical", depth="light")
    if case == "missing":
        qid = uuid4()
    elif case == "stale_text":
        text += " Without retries."
    elif case == "case_changed":
        text = text.lower()
    elif case in {"wait", "none", "duplicate"}:
        detection = Detection(case, "technical", depth="light")
    elif case == "error":
        detection = Detection("answer", "technical", error="provider_error", depth="light")
    elif case == "not_held":
        q.held = False
    elif case == "released":
        q.released.set()
    elif case in {"ending", "lost"}:
        setattr(lab.session, case, True)
    elif case == "closing":
        lab.engine._closing = True
    else:
        setattr(q, case, True)
    original = metadata(q)
    assert not Session.classify_held(lab.session, qid, text, detection)
    assert lab.engine.questions[q.qid] is q
    assert metadata(q) == original
    assert not lab.events
    assert not lab.provider.requests


async def test_hidden_selection_matches_continued_question_text(harness):
    lab = harness()
    lab.session._continued = lambda text: "Earlier context. " + text
    q = await held(lab)
    assert q.text == "Earlier context. " + TEXT
    assert Session.classify_held(lab.session, q.qid, TEXT, Detection("answer", "technical"))
    assert lab.engine.questions[q.qid].text == q.text
    assert not lab.events


async def test_reconcile_failure_restores_metadata(harness, monkeypatch):
    lab = harness()
    q = await held(lab)
    original = metadata(q)

    def fail(_q):
        raise RuntimeError("authored reconcile failure")

    monkeypatch.setattr(lab.engine, "reconcile_strategy", fail)
    with pytest.raises(RuntimeError, match="authored reconcile failure"):
        Session.classify_held(lab.session, q.qid, TEXT, Detection("answer", "technical"))
    assert metadata(q) == original
    assert not lab.events


@pytest.mark.parametrize(("providers", "expected"), [(["openai"], True), (["anthropic"], False)])
def test_early_route_selection_default_and_override(
    settings_factory, monkeypatch, providers, expected
):
    monkeypatch.delenv("COPILOT_EARLY_ROUTE_SELECTION", raising=False)
    assert settings_factory(llm_providers=providers).early_route_selection is expected
    assert not settings_factory(
        llm_providers=providers, early_route_selection=False
    ).early_route_selection


@pytest.mark.parametrize(
    ("mode", "respond", "depth"),
    [
        ("standup", True, "light"),
        ("standup", True, "deep"),
        ("interview", False, "light"),
        ("interview", False, "deep"),
    ],
)
async def test_forced_takeover_restores_original_request_contract(harness, mode, respond, depth):
    results = []
    detection = Detection("answer", "technical", depth=depth, latency_ms=23)
    for early in (False, True):
        lab = harness(mode=mode)
        old = await held(lab)
        snapshot, preferences = old.snapshots["fast"], dict(old.preferences)
        if early:
            assert Session.classify_held(lab.session, old.qid, TEXT, detection)
            selected = lab.engine.questions[old.qid]
            assert selected.hidden_original == ("adaptive", old.plans["fast"])
            assert not lab.events
        future = asyncio.get_running_loop().create_future()
        future.set_result(detection)
        Session._force_taken(lab.session, old.qid, TEXT, future, respond=respond)
        await asyncio.gather(*lab.session._forced)
        current = lab.engine.questions[old.qid]
        assert current.snapshots["fast"] is snapshot
        assert current.preferences == preferences
        assert not current.hidden_classified
        assert current.hidden_original is None
        assert current.forced
        assert current.confirmed
        assert current.t0 == old.t0
        if respond:
            assert current.qtype == "respond"
            assert current.plans["fast"] == old.plans["fast"]
        lab.release.set()
        await asyncio.wait_for(lab.engine.wait_idle(), 1)
        await asyncio.gather(*lab.engine._background)
        assert len(lab.provider.requests) == len(lab.provider.params) == 1
        assert not any(kind in {"QuestionCancelled", "AnswerError"} for kind, _ in lab.events)
        request = lab.provider.requests[0][1]
        if respond:
            assert request.max_tokens == 400  # Keep the provisional plan, not a new brief reply.
        results.append((lab.provider.calls, request, lab.provider.params, current.plans))
    assert results[0] == results[1]


async def test_forced_respond_never_releases_retired_classified_text(harness):
    lab = harness(mode="standup")
    lab.provider.reply = lambda _job, _req: Reply(["Obsolete classified answer."])
    old = await held(lab)
    detection = Detection("answer", "technical", depth="deep")
    assert Session.classify_held(lab.session, old.qid, TEXT, detection)
    selected = lab.engine.questions[old.qid]
    lab.release.set()
    await asyncio.wait_for(lab.provider.text_consumed.wait(), 1)
    assert selected.held_text == ["Obsolete classified answer."]
    assert not lab.events
    lab.provider.reply = lambda _job, _req: Reply(["The current forced reply."])
    future = asyncio.get_running_loop().create_future()
    future.set_result(detection)
    Session._force_taken(lab.session, old.qid, TEXT, future, respond=True)
    await asyncio.gather(*lab.session._forced)
    await asyncio.wait_for(lab.engine.wait_idle(), 1)
    await asyncio.gather(*lab.engine._background)
    assert selected.discarded
    assert [data["text"] for kind, data in lab.events if kind == "AnswerDelta"] == [
        "The current forced reply."
    ]
    assert lab.provider.requests[-1][1].max_tokens == 400
    assert len(lab.provider.requests) == 2
    assert not any("Obsolete" in str(write) for write in lab.writes)


@pytest.mark.parametrize(
    "boundary", ["exposed", "answer_exposed", "released", "confirmed", "retired"]
)
async def test_provisional_restore_cannot_cross_exposure_or_retirement(harness, boundary):
    lab = harness()
    old = await held(lab)
    assert Session.classify_held(lab.session, old.qid, TEXT, Detection("answer", "technical"))
    q = lab.engine.questions[old.qid]
    if boundary == "released":
        lab.engine.release(q.qid)
    elif boundary == "retired":
        lab.engine.discard(q.qid)
    else:
        setattr(q, boundary, True)
    assert lab.engine.reconcile_strategy(q, restore_provisional=True) is q
    assert q.hidden_classified


async def test_prepared_candidate_without_snapshot_keeps_normal_release_context(harness):
    lab = harness(mode="standup")
    checked = asyncio.Event()

    def source_version(_policy):
        checked.set()
        return "v1"

    prepared = PreparedAnswer(
        question=TEXT,
        talking_points=["Verified prepared note."],
        grounding_policy="fixture-policy",
        evidence_version="v1",
    )

    async def match():
        return MatchResult(prepared, 1, 0)

    task = asyncio.create_task(match())
    await task
    lab.session.prepared_source_version = source_version
    lab.session.call_context = lambda: prompts.CallContext(recent="Them: " + TEXT)
    qid = Session.start_held(lab.session, TEXT, time.monotonic(), task)
    await asyncio.wait_for(checked.wait(), 1)
    q = lab.engine.questions[qid]
    assert not q.snapshots
    assert not lab.events
    assert not lab.provider.requests
    detection = Detection("answer", "how_it_works", depth="deep")
    assert not Session.classify_held(lab.session, qid, TEXT, detection)
    assert lab.engine.questions[qid] is q
    assert q.qtype == "other"
    lab.session.call_context = lambda: prompts.CallContext(recent="Current context at release.")
    Session.release_question(lab.session, qid, TEXT, detection, speculative=False)
    lab.release.set()
    await asyncio.wait_for(lab.engine.wait_idle(), 1)
    await asyncio.gather(*lab.engine._background)
    assert len(lab.provider.requests) == 1
    assert "Current context at release." in str(lab.provider.requests[0][1].content)
    assert lab.provider.requests[0][1].max_tokens == 1200


async def test_hidden_selection_requires_frozen_preferences(harness):
    lab = harness()
    q = await held(lab)
    q.preferences.clear()
    original = metadata(q)
    assert not Session.classify_held(lab.session, q.qid, TEXT, Detection("answer", "technical"))
    assert metadata(q) == original
    assert lab.engine.questions[q.qid] is q
    assert not lab.events
