"""Offline real-session replay and request accounting; no provider credentials required."""

import asyncio

import pytest
from copilot_evals.live_answer_checkpoints import RecordDispatchJournal
from copilot_evals.live_answer_session_replay import (
    CONTROL,
    DispatchGate,
    FaultProvider,
    LimitedProvider,
    OfflineSessionProvider,
    SessionAuditProvider,
    SessionBudget,
    inspect_events,
    latency_summary,
    offline_reply,
    replay_session,
    replay_settings,
    scenarios,
    timing_rows,
)

from copilot_core.llm import LLMDone, LLMRequest
from copilot_gateway.testing import ScriptedProvider


def event(kind, qid="q1", **fields):
    return {
        "at_ms": fields.pop("at_ms", 0),
        "message": {"type": kind, "question_id": qid, **fields},
    }


def test_event_audit_allows_history_and_rejects_stale_or_incomplete_streams():
    valid = [
        event("question.detected", seq=1),
        event("answer.delta", seq=2, **{"pass": "fast", "sequence": 0}),
        event("question.detected", "q2", seq=3),
        event("answer.done", seq=4, **{"pass": "fast", "delta_count": 1}),
        event("question.cancelled", "q2", seq=5),
    ]
    assert inspect_events(valid) == []
    invalid = [
        *valid,
        event("answer.delta", "q2", seq=7, **{"pass": "fast", "sequence": 2}),
        event("answer.delta", "unknown", seq=8, **{"pass": "fast", "sequence": 0}),
        event("answer.done", "unknown", seq=9, **{"pass": "fast", "delta_count": 2}),
        event("answer.delta", seq=10, **{"pass": "fast", "sequence": 1}),
    ]
    violations = inspect_events(invalid)
    for prefix in (
        "wire_sequence",
        "chunk_sequence",
        "unknown_question",
        "completion_count",
        "output_after_terminal",
    ):
        assert any(value.startswith(prefix) for value in violations)


def test_text_origin_and_boundary_metric_exclude_filler_and_keep_unknown_origins():
    events = [
        event("question.detected", text="Where are you based?", at_ms=40),
        event("answer.delta", text="Great question. ", at_ms=50, **{"pass": "fast"}),
        event("answer.delta", text="I am based in Chicago. ", at_ms=80, **{"pass": "fast"}),
        event("answer.done", at_ms=100, **{"pass": "fast"}),
        event("question.detected", "q2", text="A rewritten question?", at_ms=110),
        event("answer.delta", "q2", text="An answer. ", at_ms=120, **{"pass": "fast"}),
    ]
    rows = timing_rows(events, [{"text": "Where are you based?", "speaker": "them", "at_ms": 10}])
    assert rows[0]["first_delta_from_text_ms"] == 40
    assert rows[0]["first_sentence_boundary_from_text_ms"] == 70
    assert rows[0]["complete_from_text_ms"] == 90
    assert rows[1]["first_delta_from_text_ms"] is None


@pytest.mark.parametrize(
    ("chunks", "expected_boundary"),
    [
        (["A complete opening. ", "Another sentence. ", "Final sentence."], 20),
        (["A complete opening."], 80),
    ],
)
def test_primary_boundary_is_first_only_and_final_flush_cannot_overwrite(chunks, expected_boundary):
    events = [event("question.detected", text="Question?", at_ms=10)]
    events.extend(
        event("answer.delta", text=text, at_ms=20 + index * 20, **{"pass": "fast"})
        for index, text in enumerate(chunks)
    )
    events.extend(
        [
            event("answer.delta", text="An expert sentence. ", at_ms=70, **{"pass": "deep"}),
            event("answer.done", at_ms=80, **{"pass": "fast"}),
            event("answer.done", at_ms=90, **{"pass": "deep"}),
        ]
    )
    rows = timing_rows(events, [{"text": "Question?", "speaker": "them", "at_ms": 0}])
    assert rows[0]["first_sentence_boundary_at_ms"] == expected_boundary
    assert rows[0]["first_sentence_boundary_from_text_ms"] == expected_boundary
    assert rows[0]["complete_at_ms"] == 80


async def test_audit_generator_can_be_opened_and_consumed_in_different_router_tasks(tmp_path):
    settings = replay_settings(False, "adaptive_on_demand")
    budget = SessionBudget(tmp_path, settings, 1)
    records = []
    provider = SessionAuditProvider(ScriptedProvider("anthropic"), budget, records, tmp_path)
    stream = provider.stream(settings.haiku_model, LLMRequest([], "Question", 100), {})
    assert isinstance(await asyncio.create_task(anext(stream)), str)
    while True:
        chunk = await asyncio.create_task(anext(stream))
        if isinstance(chunk, LLMDone):
            break
    await stream.aclose()
    assert records[0]["status"] == "completed"
    assert budget.entries[0]["status"] == "usage"
    assert budget.entries[0]["purpose"] == "answer"


async def test_gate_cancels_queued_work_without_dispatch_or_reserving_cost(tmp_path):
    settings = replay_settings(False, "adaptive_on_demand")
    budget = SessionBudget(tmp_path, settings, 1)
    records = []
    raw = ScriptedProvider("anthropic", offline_reply)
    gate = DispatchGate(1)
    provider = LimitedProvider(SessionAuditProvider(raw, budget, records, tmp_path), gate)
    request = LLMRequest([], "Question", 100)
    first = provider.stream(settings.haiku_model, request, {})
    second = provider.stream(settings.haiku_model, request, {})
    assert isinstance(await anext(first), str)
    queued = asyncio.create_task(anext(second))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert len(raw.calls) == len(records) == len(budget.entries) == 1
    assert gate.active == gate.peak == 1
    await first.aclose()
    await second.aclose()
    assert gate.active == 0
    assert budget.entries[0]["status"] == "reserved"


@pytest.mark.parametrize(
    "architecture", ["legacy_dual", "adaptive", "adaptive_hybrid", "adaptive_on_demand"]
)
@pytest.mark.parametrize("scenario", scenarios(cycles=3), ids=lambda row: row["id"])
async def test_real_session_scenarios_preserve_question_and_terminal_invariants(
    architecture, scenario, tmp_path
):
    settings = replay_settings(False, architecture)
    budget = SessionBudget(tmp_path, settings, 10)
    records, attempts = [], []
    raw = OfflineSessionProvider()
    gate = DispatchGate(4)
    providers = [
        LimitedProvider(SessionAuditProvider(FaultProvider(raw), budget, records, tmp_path), gate)
    ]
    result = await replay_session(
        scenario,
        settings,
        providers,
        trial_id=f"{scenario['id']}:{architecture}:0",
        timeout_s=15,
        attempt_events=attempts,
    )
    assert result["status"] == "completed", {
        key: result.get(key)
        for key in ("status", "violations", "error_type", "answer_error_counts")
    }
    assert result["violations"] == []
    assert result["close_code"] == 1000
    assert result["pending_after_close"] == 0
    assert not result["writer_pending_after_close"]
    assert gate.active == 0
    assert 0 < gate.peak <= 4
    assert records
    assert attempts
    assert budget.entries
    assert all(record["status"] != "started" for record in records)
    assert all(entry.get("trial_id") == result["trial_id"] for entry in budget.entries)
    assert result["fixture_provenance"].startswith("checked-in fictional")
    assert raw.decisions
    assert not any(job == "detector" for job, _ in raw.calls)
    if scenario["id"].startswith("early-"):
        before = result["route_observations"][0]["questions"][0]
        assert before["detector_depth"] is None
        assert before["held"]
        assert not before["confirmed"]
        route = next(row for row in result["routing"] if row["confirmed"])
        assert route["detector_depth"] == "deep"
        complete = [r for r in result["metadata_logs"] if r["kind"] == "answer"]
        fast = next(r for r in complete if r["pass"] == "fast")["latency_ms"]
        upgraded = architecture == "adaptive" and scenario["id"] == "early-technical-depth"
        assert fast.get("detector_depth_at_start") == ("deep" if upgraded else None)
        assert fast["detector_depth_at_completion"] == "deep"
        if architecture == "adaptive":
            # Explicit design starts expert immediately. Standard technical can replace
            # its provisional generation only before any answer is exposed.
            assert fast["request_job"] == "deep"
            assert fast["answer_depth"] == "expert"
            assert route["started_passes"] == ["fast"]
            if upgraded:
                assert any(
                    row["kind"] == "answer_strategy_replaced"
                    and row["reason"] == "pre_answer_expert_upgrade"
                    for row in result["metadata_logs"]
                )
        if architecture == "adaptive_hybrid":
            assert route["started_passes"] == ["deep", "fast"]
            assert {r["pass"] for r in complete} == {"fast", "deep"}
            expert = next(
                r for model, r in raw.routed_requests if model == settings.openai_deep_model
            )
            assert "shown_cues" not in str(expert.content)
            assert "I would make each operation idempotent." not in str(expert.content)
        if architecture == "legacy_dual":
            expert = next(
                r for model, r in raw.routed_requests if model == settings.openai_deep_model
            )
            assert "shown_cues" in str(expert.content)
            assert "I would make each operation idempotent." in str(expert.content)
        if architecture == "adaptive_on_demand":
            assert route["started_passes"] == ["fast"]
    if scenario["id"] == "missing-personal-context":
        requests = [request for _, request in raw.requests if request.json_schema is None]
        assert requests
        assert all("No employers or incident stories supplied" in str(r.system) for r in requests)
        assert all("Chicago" not in str(r.system) for r in requests)
        assert all("production incident" in str(r.content) for r in requests)
    if scenario["id"] == "standup-source-ownership":
        requests = [request for _, request in raw.requests if request.json_schema is None]
        content = str(requests[-1].content)
        assert "Priya here. I deployed the carrier preview to production." in content
        assert "Morgan's review is pending" in content
        assert "I have not deployed it to production" in content
        assert "Them:" in content
        assert "Alex:" in content
    if scenario["id"] == "prepared-source-mismatch" and architecture != "legacy_dual":
        assert result["prepared_sources"] == ["model"]
    if scenario["id"] == "changing-context":
        assert any(
            "three retries with jitter" in str(request.content)
            for _, request in raw.requests
            if request.json_schema is None
        )
    if scenario["id"] == "prepared-context-change":
        assert result["prepared_sources"][0] == "prepared"
        if settings.answer_architecture != "legacy_dual":
            assert result["prepared_sources"][-1] != "prepared"
            assert any("Austin" in str(request.content) for _, request in raw.requests)
    if scenario["id"] == "outage-recovery":
        assert result["synthetic_faults"] > 0
        assert any(entry["status"] == "failed_before_submission" for entry in budget.entries)
    if scenario["id"] == "stream-timeout":
        assert result["synthetic_faults"] > 0
        assert result["synthetic_fault_types"]["stream_timeouts"] > 0
        assert result["synthetic_fault_types"]["connection_outages"] == 0
    if scenario["id"] == "session-interrupt":
        assert any(entry["kind"] == "pass_cancelled" for entry in result["metadata_logs"])
        assert any(entry["status"] == "reserved" for entry in budget.entries)


async def test_synthetic_outage_does_not_fault_detector_or_leak_control():
    raw = ScriptedProvider("anthropic", offline_reply)
    provider = FaultProvider(raw)
    token = CONTROL.set({"outage": True})
    try:
        request = LLMRequest(
            [], "<utterance>Where are you based?</utterance>", 100, json_schema={"type": "object"}
        )
        stream = provider.stream("offline", request, {})
        assert isinstance(await anext(stream), str)
        await stream.aclose()
    finally:
        CONTROL.reset(token)
    assert len(raw.calls) == 1


def test_latency_summary_keeps_missing_cancellation_completions_in_denominator():
    rows = [
        {
            "architecture": "adaptive",
            "id": "interrupt",
            "questions": [
                {
                    "first_delta_from_text_ms": 10,
                    "first_sentence_boundary_from_text_ms": 20,
                    "complete_from_text_ms": None,
                },
                {
                    "first_delta_from_text_ms": 30,
                    "first_sentence_boundary_from_text_ms": 40,
                    "complete_from_text_ms": 50,
                },
            ],
        }
    ]
    metrics = latency_summary(rows)["adaptive"]["interrupt"]
    assert metrics["first_delta_from_text_ms"]["p50"] == 20
    assert metrics["complete_from_text_ms"]["n"] == 1
    assert metrics["complete_from_text_ms"]["missing"] == 1


def test_audit_detects_late_errors_and_events_after_socket_close():
    from copilot_evals.live_answer_session_replay import TimedMessages

    events = [
        event("question.detected"),
        event("answer.done", **{"pass": "fast"}),
        event("answer.error", code="timeout", **{"pass": "fast"}),
        event("answer.error", "unknown", code="cancelled", **{"pass": "fast"}),
    ]
    violations = inspect_events(events)
    assert any(value.startswith("error_after_terminal") for value in violations)
    assert any(value.startswith("unknown_question") for value in violations)
    timed = TimedMessages(events, 0)
    timed.closed = True
    timed.append({"type": "answer.error", "question_id": "q1", "pass": "fast", "code": "cancelled"})
    assert any(value.startswith("event_after_socket_close") for value in inspect_events(events))
    # Idempotent terminal cancellation is harmless; it cannot overwrite an answer.
    assert (
        inspect_events(
            [
                event("question.detected"),
                event("question.cancelled"),
                event("answer.error", code="cancelled", **{"pass": "fast"}),
            ]
        )
        == []
    )


def session_args(tmp_path):
    from argparse import Namespace

    return Namespace(
        out=tmp_path / "session-resume",
        live=False,
        architectures=["D"],
        scenarios=["duplicate-partial", "prepared-context-change"],
        reps=1,
        cycles=1,
        concurrency=1,
        max_inflight=2,
        timeout=15,
        max_cost=None,
        seed=20261125,
        skip_connectivity_check=True,
    )


def freeze_session_test_identity(monkeypatch):
    import hashlib
    from pathlib import Path

    from copilot_evals import live_answer_session_replay as harness

    # Test-only narrow source snapshot prevents unrelated concurrent team edits from
    # changing the fixture identity. Production hashes the complete relevant source tree.
    relative = "evals/src/copilot_evals/live_answer_session_replay.py"
    content_hash = hashlib.sha256(Path(harness.__file__).read_bytes()).hexdigest()

    def identity(cases, settings, reps, seed, concurrency, timeout):
        return {
            "code": {relative: content_hash},
            "cases": cases,
            "reps": reps,
            "seed": seed,
            "concurrency": concurrency,
            "timeout": timeout,
        }

    monkeypatch.setattr(harness, "experiment_identity", identity)


def completed_session(scenario, settings, trial_id):
    return {
        "id": scenario["id"],
        "architecture": settings.answer_architecture,
        "trial_id": trial_id,
        "status": "completed",
        "violations": [],
        "questions": [],
    }


async def test_session_resume_skips_completed_and_preserves_source_snapshot(tmp_path, monkeypatch):
    import json

    from copilot_evals import live_answer_session_replay as harness

    freeze_session_test_identity(monkeypatch)
    args = session_args(tmp_path)
    calls = []

    async def replay(scenario, settings, providers, *, trial_id, **kwargs):
        calls.append(trial_id)
        return completed_session(scenario, settings, trial_id)

    monkeypatch.setattr(harness, "replay_session", replay)
    summary = await harness.execute(args)
    assert summary["planned_sessions"] == summary["sessions"] == 2
    assert summary["remaining_sessions"] == summary["ambiguous_sessions"] == 0
    assert (args.out / "source-archive.zip").exists()
    frozen = (args.out / "source-archive.zip").read_bytes()
    assert await harness.execute(args) == summary
    assert len(calls) == 2
    assert (args.out / "source-archive.zip").read_bytes() == frozen
    stored = json.loads((args.out / "scenarios.json").read_text("utf-8"))
    assert any(row["id"] == "prepared-context-change" for row in stored)
    args.timeout += 1
    with pytest.raises(ValueError, match="settings changed"):
        await harness.execute(args)
    assert len(calls) == 2


async def test_interrupted_session_stays_ambiguous_and_only_remaining_session_runs(
    tmp_path, monkeypatch
):
    import json

    from copilot_evals import live_answer_session_replay as harness

    freeze_session_test_identity(monkeypatch)
    args = session_args(tmp_path)
    calls = []

    async def interrupt(scenario, settings, providers, *, trial_id, **kwargs):
        calls.append(trial_id)
        raise asyncio.CancelledError

    monkeypatch.setattr(harness, "replay_session", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await harness.execute(args)
    states = json.loads((args.out / "dispatch-state.json").read_text("utf-8"))
    assert len(states["in_flight"]) == 1
    assert len(states["remaining"]) == 1

    async def replay(scenario, settings, providers, *, trial_id, **kwargs):
        calls.append(trial_id)
        return completed_session(scenario, settings, trial_id)

    monkeypatch.setattr(harness, "replay_session", replay)
    summary = await harness.execute(args)
    assert summary["status"] == "partial"
    assert summary["sessions"] == summary["ambiguous_sessions"] == 1
    assert summary["observed_cost_usd"] is None
    assert len(set(calls)) == len(calls) == 2
    assert await harness.execute(args) == summary
    assert len(calls) == 2
    skipped = json.loads((args.out / "not-started.json").read_text("utf-8"))
    assert len(skipped) == 1
    assert skipped[0]["dispatch_state"] == "ambiguous"


async def test_durable_result_recovers_crash_before_journal_finish(tmp_path, monkeypatch):
    from copilot_evals import live_answer_session_replay as harness

    freeze_session_test_identity(monkeypatch)
    args = session_args(tmp_path)
    calls = []
    original_finish = RecordDispatchJournal.finish

    async def replay(scenario, settings, providers, *, trial_id, **kwargs):
        calls.append(trial_id)
        return completed_session(scenario, settings, trial_id)

    def crash_after_result(self, identifier):
        raise RuntimeError("synthetic crash between durable result and finish")

    monkeypatch.setattr(harness, "replay_session", replay)
    monkeypatch.setattr(RecordDispatchJournal, "finish", crash_after_result)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        await harness.execute(args)
    monkeypatch.setattr(RecordDispatchJournal, "finish", original_finish)
    summary = await harness.execute(args)
    assert summary["sessions"] == 2
    assert summary["ambiguous_sessions"] == 0
    assert len(set(calls)) == len(calls) == 2


@pytest.mark.parametrize("value", [0, -1, 33])
def test_provider_admission_rejects_unbounded_or_invalid_limits(value):
    with pytest.raises(ValueError, match="between one and 32"):
        DispatchGate(value)


async def test_eight_workers_bound_sessions_without_scheduling_every_trial(tmp_path, monkeypatch):
    from copilot_evals import live_answer_session_replay as harness

    freeze_session_test_identity(monkeypatch)
    args = session_args(tmp_path)
    args.architectures = ["A", "B", "C", "D"]
    args.reps = 3
    args.concurrency = 8
    args.max_inflight = 4
    active = peak = 0
    calls = []
    first_wave = asyncio.Event()

    async def replay(scenario, settings, providers, *, trial_id, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(trial_id)
        if active == args.concurrency:
            first_wave.set()
        try:
            # Durable dispatch barriers can stagger admissions beyond a short sleep.
            # Hold the first wave until all bounded workers enter, independently of disk speed.
            async with asyncio.timeout(5):
                await first_wave.wait()
            return completed_session(scenario, settings, trial_id)
        finally:
            active -= 1

    monkeypatch.setattr(harness, "replay_session", replay)
    summary = await harness.execute(args)
    assert summary["planned_sessions"] == summary["sessions"] == 24
    assert peak == 8
    assert active == 0
    assert len(calls) == len(set(calls)) == 24
    args.concurrency = 9
    with pytest.raises(ValueError, match="finite concurrency"):
        await harness.execute(args)


async def test_shared_gate_covers_decisions_and_answers_before_budget_reservation(tmp_path):
    settings = replay_settings(False, "adaptive")
    budget = SessionBudget(tmp_path, settings, 1)
    records = []
    raw = OfflineSessionProvider()
    gate = DispatchGate(1)
    provider = LimitedProvider(SessionAuditProvider(raw, budget, records, tmp_path), gate)
    request = LLMRequest([], "Question", 100)
    assert settings.openai_fast_model is not None
    assert settings.openai_detector_model is not None
    stream = provider.stream(settings.openai_fast_model, request, {})
    await anext(stream)
    input = [{}, {"content": [{"text": "<utterance>Where are you based?</utterance>"}]}]
    queued = asyncio.create_task(provider.decide(settings.openai_detector_model, input, []))
    await asyncio.sleep(0)
    assert len(budget.entries) == 1
    assert not raw.decisions
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await stream.aclose()
    assert gate.peak == 1
    assert gate.active == 0
    assert len(records) == 1


async def test_session_gate_stops_decisions_retry_after_rate_limit_before_new_reservation(tmp_path):
    from copilot_evals.live_answer_replay import AdmissionBlocked

    class RateLimitError(Exception):
        pass

    class FailingDecisions(OfflineSessionProvider):
        async def decide(self, model, input, questions):
            raise RateLimitError("synthetic rate limit")

    settings = replay_settings(False, "adaptive")
    budget = SessionBudget(tmp_path, settings, 1)
    records = []
    gate = DispatchGate(1)
    provider = LimitedProvider(
        SessionAuditProvider(FailingDecisions(), budget, records, tmp_path), gate
    )
    assert settings.openai_detector_model is not None
    with pytest.raises(RateLimitError):
        await provider.decide(settings.openai_detector_model, [], [])
    assert gate.reason == "rate_limit"
    assert len(records) == len(budget.entries) == 1
    with pytest.raises(AdmissionBlocked):
        await provider.decide(settings.openai_detector_model, [], [])
    assert len(records) == len(budget.entries) == 1
    assert gate.active == 0


async def test_successful_detector_cannot_mask_answer_outage_circuit(tmp_path, monkeypatch):
    from copilot_evals import live_answer_session_replay as harness

    freeze_session_test_identity(monkeypatch)
    args = session_args(tmp_path)
    args.reps = 5
    calls = []

    async def replay(scenario, settings, providers, *, trial_id, **kwargs):
        calls.append(trial_id)
        audit = providers[0].inner
        audit.requests.extend(
            [
                {"trial_id": trial_id, "status": "completed", "purpose": "detector_decisions"},
                {
                    "trial_id": trial_id,
                    "status": "APIConnectionError",
                    "error_types": ["ConnectError"],
                },
            ]
        )
        return {
            **completed_session(scenario, settings, trial_id),
            "status": "provider_or_engine_failure",
        }

    monkeypatch.setattr(harness, "replay_session", replay)
    summary = await harness.execute(args)
    assert len(calls) == 3
    assert summary["remaining_sessions"] == 7
    assert summary["circuit_reason"] == "transport"
