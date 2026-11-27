"""Zero-provider routing experiments through the real transcript and Decisions path.

The production policy remains unchanged. The comparison injects one synchronous hook
into an in-memory copy of the current answer pass, immediately after the EXISTING
memory lookup and before prompt/cache construction. No production module is rewritten.
Event gates establish order rather than pretending scripted sleeps measure model speed.

Options:
* frozen: current early plan;
* applied: reuse classification already applied to this exact question;
* ready: additionally read its already-completed, exact-text detector future.

All options retain the original bounded memory lookup. Results establish scheduling and
request-contract behavior, not answer quality, provider latency, or population coverage.
"""

import asyncio
import inspect
import json
import textwrap
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from copilot_core import prompts
from copilot_core.llm import Decided, LLMChunk, LLMDone, LLMRequest, LLMUsage
from copilot_gateway import answer as answer_module
from copilot_gateway import session as session_module
from copilot_gateway.adaptive import AnswerPlan, plan_answer
from copilot_gateway.answer import AnswerEngine, Question, question_hash
from copilot_gateway.conversation import Line
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    ScriptedProvider,
    text_frame_audio,
)
from copilot_gateway.turn import Detection

QUESTION = (
    "How do you preserve exactly-once effects when a worker crashes after commit "
    "but before acknowledgement?"
)
EVIDENCE = "[n:a123] Fictional source: the worker uses a durable operation key."
VARIANTS = ("frozen", "applied", "ready")


def ready_detection(session: Any, q: Question, original: str) -> Detection | None:
    """Read-only, no-await lookup; never release a question or reuse another revision."""
    if q.text != original or q.cancelled or q.discarded or q.superseded:
        return None
    turns = session.turns
    early, turn = turns.early, turns.turn
    candidates = []
    if early is not None and early.held == q.qid:
        candidates.append(early.cand)
    if (
        turn is not None
        and q.qid in (turn.held_qid, turn.first_qid, turn.speculative_qid)
        and turn.cand is not None
    ):
        candidates.append(turn.cand)
    for candidate in candidates:
        if candidate.text != original:
            continue
        future = candidate.detection
        if not future.done() or future.cancelled() or future.exception() is not None:
            continue
        result = future.result()
        if result.fires():
            return result
    return None


@dataclass
class RoutingLab:
    variant: str
    question_text: str = QUESTION
    detection_type: str = "technical"
    architecture: str = "adaptive"
    hold_before_request: bool = False
    request_gate_entered: asyncio.Event = field(default_factory=asyncio.Event)
    request_gate_release: asyncio.Event = field(default_factory=asyncio.Event)
    expert_entered: asyncio.Event = field(default_factory=asyncio.Event)
    expert_release: asyncio.Event = field(default_factory=asyncio.Event)
    closed_models: list[str] = field(default_factory=list)
    memory_entered: asyncio.Event = field(default_factory=asyncio.Event)
    memory_release: asyncio.Event = field(default_factory=asyncio.Event)
    cache_entered: asyncio.Event = field(default_factory=asyncio.Event)
    cache_release: asyncio.Event = field(default_factory=asyncio.Event)
    provider_entered: asyncio.Event = field(default_factory=asyncio.Event)
    provider_release: asyncio.Event = field(default_factory=asyncio.Event)
    detector_entered: asyncio.Event = field(default_factory=asyncio.Event)
    detector_release: asyncio.Event = field(default_factory=asyncio.Event)
    memory_calls: list[tuple[str, str]] = field(default_factory=list)
    requests: list[tuple[str, LLMRequest]] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    cache_writes: list[dict[str, Any]] = field(default_factory=list)
    cache_reads: list[str] = field(default_factory=list)
    snapshots: dict[tuple[UUID, str], tuple[Question, str]] = field(default_factory=dict)
    selections: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)
    session: Any = None

    async def before_request(self, pass_: str) -> None:
        if pass_ == "fast":
            self.timeline.append("request_gate_entered")
            self.request_gate_entered.set()
            if self.hold_before_request:
                await self.request_gate_release.wait()

    def replan(self, engine: AnswerEngine, q: Question, pass_: str, plan: AnswerPlan) -> AnswerPlan:
        self.timeline.append("pre_request")
        saved = self.snapshots[(q.qid, pass_)]
        if (
            self.variant == "frozen"
            or pass_ != "fast"
            or engine._closing
            or saved[0] is not q
            or saved[1] != q.text
            or engine.questions.get(q.qid) is not q
            or q.cancelled
            or q.discarded
            or q.superseded
        ):
            return plan
        detection = ready_detection(self.session, q, saved[1]) if self.variant == "ready" else None
        chosen = plan_answer(
            saved[1],
            detection.qtype if detection else q.qtype,
            detection.depth if detection else q.depth,
            engine.strategy(q),
        )
        self.selections.append(
            {
                "qid": q.qid,
                "from": plan,
                "to": chosen,
                "source": "completed_future" if detection else "applied_question",
            }
        )
        return chosen


def experimental_engine(lab: RoutingLab) -> type[AnswerEngine]:
    """Compile the CURRENT implementation with an exact, fail-closed test hook.

    This avoids a second handwritten engine. The baseline also traverses this hook,
    which returns its original frozen plan unchanged.
    """
    source = textwrap.dedent(inspect.getsource(AnswerEngine._run_pass))
    anchor = "            if not host.settings.personal_ai_api_url:\n"
    assert source.count(anchor) == 1, (
        "Re-audit the safe request/cache boundary after source changes."
    )
    source = source.replace(
        anchor,
        "            await self._routing_lab.before_request(pass_)\n"
        "            plan = self._routing_lab.replan(self, q, pass_, plan)\n"
        "            q.plans[pass_] = plan\n" + anchor,
    )
    namespace = dict(vars(answer_module))
    exec(compile(source, "<offline-routing-experiment>", "exec"), namespace)

    class ExperimentalEngine(AnswerEngine):
        _routing_lab = lab

        def reconcile_strategy(self, q: Question, *, restore_provisional: bool = False) -> Question:
            # This historical lab isolates its in-flight request-plan hook. Production
            # pre-answer generation replacement is covered by the actual Session tests
            # in test_technical_legacy_policy, without this experimental subclass.
            return q

        def start_pass(self, q: Question, pass_: str) -> None:
            if pass_ not in q.tasks:
                lab.snapshots[(q.qid, pass_)] = (q, q.text)
            super().start_pass(q, pass_)

    ExperimentalEngine._run_pass = namespace["_run_pass"]
    return ExperimentalEngine


class RoutingProvider(ScriptedProvider):
    def __init__(self, lab: RoutingLab) -> None:
        super().__init__("openai")
        self.lab = lab

    async def decide(self, model: str, input: Any, questions: Any) -> Decided:
        lab = self.lab
        utterance = str(input[1]["content"][0]["text"]).split("<utterance>")[-1]
        utterance = utterance.split("</utterance>")[0].strip()
        lab.decisions.append(utterance)
        lab.timeline.append("detector_started")
        lab.detector_entered.set()
        await lab.detector_release.wait()
        lab.timeline.append("detector_completed")
        return Decided(
            [
                {"type": "choice", "name": name, "choice": value, "confidence": 0.99}
                for name, value in (
                    ("turn", "answer"),
                    ("type", lab.detection_type),
                    ("depth", "deep"),
                )
            ],
            LLMUsage(10, cache_write_tokens=0, reasoning_tokens=0),
            "openai",
            model,
        )

    async def stream(
        self, model: str, req: LLMRequest, params: Mapping[str, Any]
    ) -> AsyncGenerator[LLMChunk]:
        lab = self.lab
        lab.requests.append((model, req))
        lab.timeline.append("provider_dispatched")
        expert = lab.architecture == "adaptive_hybrid" and model == "fixture-deep"
        (lab.expert_entered if expert else lab.provider_entered).set()
        try:
            await (lab.expert_release if expert else lab.provider_release).wait()
            yield "Use a durable operation key to make retries idempotent. "
            yield LLMDone(
                LLMUsage(10, 0, 12, cache_write_tokens=0, reasoning_tokens=0),
                "end_turn",
                self.name,
                model,
            )
        finally:
            lab.closed_models.append(model)


@asynccontextmanager
async def live_lab(
    env,
    monkeypatch,
    variant,
    architecture="adaptive",
    *,
    question_text=QUESTION,
    detection_type="technical",
    automatic_expert=False,
    hold_before_request=False,
    **preferences,
):
    lab = RoutingLab(
        variant,
        question_text=question_text,
        detection_type=detection_type,
        architecture=architecture,
        hold_before_request=hold_before_request,
    )
    monkeypatch.setattr(session_module, "AnswerEngine", experimental_engine(lab))

    async def memory(self, text, pass_):
        lab.memory_calls.append((text, pass_))
        lab.timeline.append("memory_started")
        lab.memory_entered.set()
        await lab.memory_release.wait()
        lab.timeline.append("memory_completed")
        return [EVIDENCE], {"memory_hits": 1}

    async def cache(self, key):
        lab.cache_reads.append(key)
        lab.cache_entered.set()
        await lab.cache_release.wait()
        return None

    async def put(self, key, value):
        lab.cache_writes.append(value)

    monkeypatch.setattr(session_module.Session, "memory_lines", memory)
    monkeypatch.setattr(session_module.Session, "qa_get", cache)
    monkeypatch.setattr(session_module.Session, "qa_put", put)
    env.providers = [RoutingProvider(lab)]
    settings = env.settings(
        llm_providers=["openai"],
        answer_architecture=architecture,
        adaptive_expert_route=None,
        adaptive_technical_legacy=False,
        detector_api="decisions",
        openai_detector_model="fixture-detector",
        openai_fast_model="fixture-fast",
        openai_deep_model="fixture-deep",
        early_decision_interval_ms=1,
        early_decision_all_modes=True,
        first_show_min_words=None,
        decisions_hedge_ms=None,
        fast_hedge_ms=None,
        deep_hedge_ms=None,
        prewarm_jobs=[],
        deep_pass_auto=automatic_expert,
    )
    async with GatewayServer(env.services(settings)) as gateway:
        client = ProtocolClient(gateway.url)
        await client.start(**preferences)
        lab.session = next(iter(gateway.manager.sessions.values()))
        try:
            yield lab, client
        finally:
            for gate in (
                lab.memory_release,
                lab.cache_release,
                lab.detector_release,
                lab.provider_release,
                lab.expert_release,
                lab.request_gate_release,
            ):
                gate.set()
            await client.close()


async def start_partial(lab, client):
    await client.frame(0, 0b01, text_frame_audio(lab.question_text), 0)
    await asyncio.wait_for(lab.memory_entered.wait(), 2)
    await asyncio.wait_for(lab.detector_entered.wait(), 2)
    q = next(iter(lab.session.answers.questions.values()))
    assert q.held
    assert q.qtype == "other"
    assert q.depth is None
    assert q.plans["fast"] == AnswerPlan("standard", "fast", 400)
    return q


async def finish(lab, client, q, *, ended=False):
    lab.detector_release.set()
    lab.memory_release.set()
    lab.cache_release.set()
    lab.provider_release.set()
    if not ended:
        await client.frame(0, 0b10, b"", 100)
    await client.wait_type("question.detected", question_id=str(q.qid), speculative=False)
    await client.wait_type("answer.done", question_id=str(q.qid), pass_="fast")
    await lab.session.answers.wait_idle()
    await asyncio.gather(*lab.session.answers._background)
    assert len(lab.requests) == 1
    assert lab.decisions == [QUESTION]
    assert lab.memory_calls == [(QUESTION, "deep")]  # One rich lookup; no extra memory call.
    assert q.qtype == "technical"
    assert q.depth == "deep"
    assert q.delta_counts == {"fast": 1}
    assert len(client.of_type("answer.done")) == 1


@pytest.mark.parametrize("architecture", ["adaptive", "adaptive_on_demand"])
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("arrival", ["future_ready", "applied", "after_dispatch"])
async def test_actual_session_late_route_comparison(
    env, monkeypatch, architecture, variant, arrival
):
    async with live_lab(env, monkeypatch, variant, architecture) as (lab, client):
        q = await start_partial(lab, client)
        lab.cache_release.set()
        ended = False
        if arrival != "after_dispatch":
            lab.detector_release.set()
            early = lab.session.turns.early
            assert early is not None
            assert early.held == q.qid
            await asyncio.wait_for(asyncio.shield(early.cand.detection), 2)
            assert q.depth is None  # Completion alone does not apply the result while speaking.
            if arrival == "applied":
                await client.frame(0, 0b10, b"", 100)
                ended = True
                await client.wait_type("question.detected", question_id=str(q.qid))
                assert q.depth == "deep"
        lab.memory_release.set()
        await asyncio.wait_for(lab.provider_entered.wait(), 2)
        if arrival == "after_dispatch":
            # The experimental path neither waits for classification nor starts a new one.
            assert not lab.detector_release.is_set()
            assert "detector_completed" not in lab.timeline
        upgraded = (arrival == "applied" and variant in {"applied", "ready"}) or (
            arrival == "future_ready" and variant == "ready"
        )
        expected = (
            plan_answer(QUESTION, "technical", "deep", architecture)
            if upgraded
            else AnswerPlan("standard", "fast", 400)
        )
        assert q.plans["fast"] == expected
        model, req = lab.requests[0]
        expected_model = lab.session.settings.openai_route(expected.job)[0]
        assert model == expected_model
        assert req.max_tokens == expected.max_tokens
        assert EVIDENCE in str(req.content)
        if arrival == "future_ready":
            assert q.held
            assert not client.of_type("answer.delta")
            assert q.qtype == "other"
            assert q.depth is None  # Routing never pretends to confirm.
        await finish(lab, client, q, ended=ended)
        key = next(key for key in lab.cache_writes[-1] if key not in {"v"})
        assert f":{expected.depth}:{expected.job}:" in key


@pytest.mark.parametrize("architecture", ["adaptive", "adaptive_on_demand"])
@pytest.mark.parametrize("variant", VARIANTS)
async def test_result_during_cache_read_cannot_mutate_frozen_request(
    env, monkeypatch, variant, architecture
):
    async with live_lab(env, monkeypatch, variant, architecture) as (lab, client):
        lab.session.call_context = lambda: prompts.CallContext(recent="Frozen original context.")
        q = await start_partial(lab, client)
        lab.memory_release.set()
        await asyncio.wait_for(lab.cache_entered.wait(), 2)
        lab.detector_release.set()
        early = lab.session.turns.early
        assert early is not None
        await asyncio.wait_for(asyncio.shield(early.cand.detection), 2)
        lab.session.call_context = lambda: prompts.CallContext(recent="Later unrelated context.")
        lab.cache_release.set()
        await asyncio.wait_for(lab.provider_entered.wait(), 2)
        req = lab.requests[0][1]
        assert q.plans["fast"] == AnswerPlan("standard", "fast", 400)
        assert "Frozen original context" in str(req.content)
        assert "Later unrelated context" not in str(req.content)
        await finish(lab, client, q)


@pytest.mark.parametrize("style", ["bullets", "paragraph"])
@pytest.mark.parametrize("length", ["default", "concise", "detailed"])
async def test_ready_route_keeps_initial_preferences_and_single_memory_snapshot(
    env, monkeypatch, style, length
):
    async with live_lab(
        env,
        monkeypatch,
        "ready",
        "adaptive_on_demand",
        answer_style=style,
        response_length=length,
    ) as (lab, client):
        q = await start_partial(lab, client)
        lab.detector_release.set()
        early = lab.session.turns.early
        assert early is not None
        await asyncio.wait_for(asyncio.shield(early.cand.detection), 2)
        lab.session.info.answer_style = "paragraph" if style == "bullets" else "bullets"
        lab.session.info.response_length = "detailed" if length != "detailed" else "concise"
        lab.memory_release.set()
        lab.cache_release.set()
        await asyncio.wait_for(lab.provider_entered.wait(), 2)
        assert q.preferences["fast"] == (style, length)
        expected_cap = {"default": 650, "concise": 390, "detailed": 975}[length]
        req = lab.requests[0][1]
        assert req.max_tokens == expected_cap
        policy = "\n".join(block.text for block in req.system) + str(req.content)
        selected_style = (
            "Use connected paragraphs."
            if style == "paragraph"
            else "Write the answer as complete-sentence bullets."
        )
        assert selected_style in policy
        if length != "default":
            assert f"User response-length preference: {length}." in policy
        await finish(lab, client, q)


@pytest.mark.parametrize("action", ["discard", "cancel", "supersede"])
async def test_cancelled_question_never_dispatches_from_a_completed_late_result(
    env, monkeypatch, action
):
    async with live_lab(env, monkeypatch, "ready") as (lab, client):
        q = await start_partial(lab, client)
        lab.detector_release.set()
        early = lab.session.turns.early
        assert early is not None
        await asyncio.wait_for(asyncio.shield(early.cand.detection), 2)
        if action == "discard":
            lab.session.answers.discard(q.qid)
        elif action == "cancel":
            lab.session.answers.cancel_speculative(q.qid)
        else:
            lab.session.answers.supersede(keep=None)
        await lab.session.answers.wait_idle()
        lab.memory_release.set()
        lab.cache_release.set()
        assert not lab.requests
        assert not client.of_type("answer.delta")


@pytest.mark.parametrize("variant", ["applied", "ready"])
@pytest.mark.parametrize("detector_completed", [False, True])
async def test_final_revision_gets_a_new_question_and_does_not_reuse_old_route_future(
    env, monkeypatch, variant, detector_completed, record_property
):
    env.stt.final_suffix = " when the downstream provider has no idempotency API?"
    env.stt.final_delay_s = 0.06
    async with live_lab(env, monkeypatch, variant) as (lab, client):
        old = await start_partial(lab, client)
        old_task = old.tasks["fast"]
        if detector_completed:
            lab.detector_release.set()
            early = lab.session.turns.early
            assert early is not None
            await asyncio.wait_for(asyncio.shield(early.cand.detection), 2)
        await client.frame(0, 0b10, b"", 100)
        final = await client.wait_type("transcript.final")
        assert "downstream provider" in final["text"]
        if not detector_completed:
            await asyncio.wait_for(asyncio.gather(old_task, return_exceptions=True), 2)
            lab.detector_release.set()
        detected = await client.wait_type("question.detected", speculative=False)
        lab.memory_release.set()
        lab.cache_release.set()
        lab.provider_release.set()
        await client.wait_type("answer.done", question_id=detected["question_id"], pass_="fast")
        await lab.session.answers.wait_idle()
        observation = {
            "same_question_id": detected["question_id"] == str(old.qid),
            "memory_queries": lab.memory_calls,
            "cache_read_hashes": list(dict.fromkeys(lab.cache_reads)),
            "old_question_hash": question_hash(QUESTION),
            "final_question_hash": question_hash(final["text"]),
            "request_contains_final_constraint": "downstream provider"
            in str(lab.requests[0][1].content),
            "detector_calls": len(lab.decisions),
            "old_task_cancelled": old_task.cancelled(),
        }
        record_property("material_revision_observation", json.dumps(observation))
        assert detected["question_id"] != str(old.qid), observation
        assert old.discarded
        assert old_task.cancelled()
        assert len(lab.requests) == 1
        assert "downstream provider" in str(lab.requests[0][1].content)
        assert len(lab.decisions) == 2  # Existing material-revision redetection, no new classifier.
        assert {m["question_id"] for m in client.of_type("answer.delta")} == {
            detected["question_id"]
        }


@pytest.mark.parametrize("variant", ["frozen", "ready"])
async def test_old_question_keeps_generation_start_conversation(
    env, monkeypatch, variant, record_property
):
    async with live_lab(env, monkeypatch, variant) as (lab, client):
        lab.session.conv.add(Line("them", 0, 1, "Original question context."))
        q = await start_partial(lab, client)
        # The question is still the same object/id; a later speaker contributes context
        # while its first bounded evidence lookup is pending.
        lab.session.conv.add(Line("them", 200, 300, "Blair owns the unrelated billing rollout."))
        lab.memory_release.set()
        lab.cache_release.set()
        await asyncio.wait_for(lab.provider_entered.wait(), 2)
        content = str(lab.requests[0][1].content)
        observation = {
            "same_question_object": lab.session.answers.questions[q.qid] is q,
            "original_context_present": "Original question context" in content,
            "later_speaker_present": "Blair owns the unrelated billing rollout" in content,
            "memory_queries": lab.memory_calls,
            "detector_pending_at_dispatch": not lab.detector_release.is_set(),
        }
        record_property("conversation_snapshot_observation", json.dumps(observation))
        await finish(lab, client, q)
        assert "Blair owns the unrelated billing rollout" not in content, observation


@pytest.mark.parametrize(
    "case",
    [
        "pending",
        "cancelled_future",
        "failed_future",
        "none",
        "wait",
        "wrong_qid",
        "wrong_candidate_text",
        "changed_question",
        "discarded",
    ],
)
async def test_completed_future_lookup_rejects_unknown_or_unrelated_routing(case):
    future = asyncio.get_running_loop().create_future()
    if case == "cancelled_future":
        future.cancel()
    elif case == "failed_future":
        future.set_exception(RuntimeError("scripted detector failure"))
    elif case != "pending":
        future.set_result(
            Detection(case if case in {"none", "wait"} else "answer", "technical", depth="deep")
        )
    q = SimpleNamespace(
        qid=UUID(int=1), text=QUESTION, cancelled=False, discarded=False, superseded=False
    )
    candidate = SimpleNamespace(text=QUESTION, detection=future)
    early = SimpleNamespace(held=q.qid, cand=candidate)
    session = SimpleNamespace(turns=SimpleNamespace(early=early, turn=None))
    if case == "wrong_qid":
        early.held = UUID(int=2)
    elif case == "wrong_candidate_text":
        candidate.text += " changed"
    elif case == "changed_question":
        q.text += " changed"
    elif case == "discarded":
        q.discarded = True
    assert ready_detection(session, cast(Question, q), QUESTION) is None


WORK_QUESTION = "How does Boreal retry delivery?"


async def complete_existing_detector(lab):
    early = lab.session.turns.early
    assert early is not None
    future = early.cand.detection
    lab.detector_release.set()
    await asyncio.wait_for(asyncio.shield(future), 2)
    assert future.result().qtype == "how_it_works"
    assert future.result().depth == "deep"


async def confirm_existing_question(lab, client, q):
    await client.frame(0, 0b10, b"", 100)
    detected = await client.wait_type(
        "question.detected", question_id=str(q.qid), speculative=False
    )
    assert detected["question_id"] == str(q.qid)
    assert lab.session.answers.questions[q.qid] is q
    assert q.qtype == "how_it_works"
    assert q.depth == "deep"


@pytest.mark.parametrize("architecture", ["adaptive", "adaptive_hybrid", "adaptive_on_demand"])
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize(
    "arrival",
    ["before_retrieval_completion", "before_request_construction", "after_dispatch"],
)
async def test_workplace_late_route_keeps_primary_and_hybrid_contract(
    env, monkeypatch, architecture, variant, arrival
):
    """No provider/model timing claim: gates establish the three exact causal orders."""
    async with live_lab(
        env,
        monkeypatch,
        variant,
        architecture,
        question_text=WORK_QUESTION,
        detection_type="how_it_works",
        automatic_expert=True,
        hold_before_request=True,
        mode="standup",
    ) as (lab, client):
        lab.session.call_context = lambda: prompts.CallContext(recent="Frozen Boreal context.")
        q = await start_partial(lab, client)
        original_task = q.tasks["fast"]
        assert lab.memory_calls == [(WORK_QUESTION, "deep")]
        lab.session.call_context = lambda: prompts.CallContext(
            recent="Later unrelated Atlas context."
        )
        lab.cache_release.set()

        if arrival == "before_retrieval_completion":
            await complete_existing_detector(lab)
            assert q.held
            assert q.depth is None
            assert "memory_completed" not in lab.timeline
            lab.memory_release.set()
            lab.request_gate_release.set()
        elif arrival == "before_request_construction":
            lab.memory_release.set()
            await asyncio.wait_for(lab.request_gate_entered.wait(), 2)
            assert "memory_completed" in lab.timeline
            assert not lab.provider_entered.is_set()
            await complete_existing_detector(lab)
            await confirm_existing_question(lab, client, q)
            # A hybrid may start the independent expert before this primary dispatch.
            if architecture == "adaptive_hybrid":
                await asyncio.wait_for(lab.expert_entered.wait(), 2)
                assert not lab.provider_entered.is_set()
            lab.request_gate_release.set()
        else:
            lab.memory_release.set()
            lab.request_gate_release.set()

        await asyncio.wait_for(lab.provider_entered.wait(), 2)
        upgraded = (arrival == "before_retrieval_completion" and variant == "ready") or (
            arrival == "before_request_construction" and variant != "frozen"
        )
        expected = (
            plan_answer(WORK_QUESTION, "how_it_works", "deep", architecture)
            if upgraded
            else AnswerPlan("standard", "fast", 400)
        )
        assert q.plans["fast"] == expected
        primary = [
            (model, req)
            for model, req in lab.requests
            if architecture != "adaptive_hybrid" or model != "fixture-deep"
        ]
        assert len(primary) == 1
        model, request = primary[0]
        assert model == lab.session.settings.openai_route(expected.job)[0]
        assert request.max_tokens == expected.max_tokens
        assert EVIDENCE in str(request.content)
        assert WORK_QUESTION in str(request.content)
        assert "Frozen Boreal context" in str(request.content)
        assert "Later unrelated Atlas context" not in str(request.content)
        assert q.tasks["fast"] is original_task
        assert not client.of_type("answer.delta")
        if arrival == "after_dispatch":
            assert "detector_completed" not in lab.timeline
            await complete_existing_detector(lab)
        if arrival != "before_request_construction":
            await confirm_existing_question(lab, client, q)

        if architecture == "adaptive_hybrid":
            await asyncio.wait_for(lab.expert_entered.wait(), 2)
            assert not q.fast_ended
            assert not original_task.done()
            assert len(lab.requests) == 2
            expert = next(req for model, req in lab.requests if model == "fixture-deep")
            assert expert.max_tokens == 1200
            assert WORK_QUESTION in str(expert.content)
            assert EVIDENCE in str(expert.content)
            assert "Use a durable operation key to make retries idempotent." not in str(
                expert.content
            )
            lab.expert_release.set()
            await client.wait_type("answer.done", question_id=str(q.qid), pass_="deep")
            assert not q.fast_ended
        else:
            assert len(lab.requests) == 1
            assert not lab.expert_entered.is_set()

        lab.provider_release.set()
        await client.wait_type("answer.done", question_id=str(q.qid), pass_="fast")
        await lab.session.answers.wait_idle()
        await asyncio.gather(*lab.session.answers._background)
        assert lab.decisions == [WORK_QUESTION]
        assert lab.memory_calls == (
            [(WORK_QUESTION, "deep"), (WORK_QUESTION, "deep")]
            if architecture == "adaptive_hybrid"
            else [(WORK_QUESTION, "deep")]
        )  # The primary keeps its original evidence lookup, even when routing upgrades.
        assert len(lab.requests) == (2 if architecture == "adaptive_hybrid" else 1)
        assert q.delta_counts == (
            {"deep": 1, "fast": 1} if architecture == "adaptive_hybrid" else {"fast": 1}
        )
        assert {m["question_id"] for m in client.of_type("answer.delta")} == {str(q.qid)}
        assert all(m["sequence"] == 0 for m in client.of_type("answer.delta"))
        assert q.tasks["fast"] is original_task
        assert len(lab.closed_models) == len(lab.requests)


@pytest.mark.parametrize("action", ["discard", "cancel", "supersede"])
@pytest.mark.parametrize("phase", ["before_request", "after_confirmation"])
async def test_workplace_hybrid_cancellation_owns_primary_and_expert(
    env, monkeypatch, action, phase
):
    async with live_lab(
        env,
        monkeypatch,
        "ready",
        "adaptive_hybrid",
        question_text=WORK_QUESTION,
        detection_type="how_it_works",
        automatic_expert=True,
        hold_before_request=True,
        mode="standup",
    ) as (lab, client):
        q = await start_partial(lab, client)
        await complete_existing_detector(lab)
        lab.cache_release.set()
        lab.memory_release.set()
        await asyncio.wait_for(lab.request_gate_entered.wait(), 2)
        if phase == "after_confirmation":
            lab.request_gate_release.set()
            await asyncio.wait_for(lab.provider_entered.wait(), 2)
            await confirm_existing_question(lab, client, q)
            await asyncio.wait_for(lab.expert_entered.wait(), 2)
            assert len(lab.requests) == 2
        else:
            assert q.held
            assert not lab.requests
        if action == "discard":
            lab.session.answers.discard(q.qid)
        elif action == "cancel":
            lab.session.answers.cancel_speculative(q.qid)
        else:
            lab.session.answers.supersede(keep=None)
        await lab.session.answers.wait_idle()
        assert all(task.done() for task in q.tasks.values())
        assert len(lab.requests) == (2 if phase == "after_confirmation" else 0)
        assert len(lab.closed_models) == len(lab.requests)
        assert not client.of_type("answer.delta")
        assert not client.of_type("answer.done")
        assert lab.decisions == [WORK_QUESTION]
