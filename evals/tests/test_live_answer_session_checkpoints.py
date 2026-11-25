"""Atomic session audit persistence and resume behavior; no provider network calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
from argparse import Namespace
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from copilot_evals import live_answer_session_replay as harness
from copilot_evals.live_answer_checkpoints import POLICY, RecordStore
from copilot_evals.live_answer_replay import TRIAL, AdmissionBlocked

from copilot_core.llm import Decided, LLMDone, LLMUsage


def args(directory: Path) -> Namespace:
    return Namespace(
        out=directory,
        live=False,
        architectures=["D"],
        scenarios=["duplicate-partial", "prepared-context-change"],
        reps=1,
        cycles=1,
        concurrency=2,
        max_inflight=2,
        timeout=15,
        max_cost=None,
        seed=20261125,
        skip_connectivity_check=True,
    )


def stable_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    relative = "evals/src/copilot_evals/live_answer_session_replay.py"
    digest = hashlib.sha256(Path(harness.__file__).read_bytes()).hexdigest()

    def identity(cases, settings, reps, seed, concurrency, timeout):
        return {
            "code": {relative: digest},
            "cases": cases,
            "reps": reps,
            "seed": seed,
            "concurrency": concurrency,
            "timeout": timeout,
        }

    monkeypatch.setattr(harness, "experiment_identity", identity)


def completed(scenario, settings, trial_id):
    return {
        "id": scenario["id"],
        "architecture": settings.answer_architecture,
        "trial_id": trial_id,
        "status": "completed",
        "violations": [],
        "questions": [],
    }


class Decisions:
    name = "openai"

    def __init__(self):
        self.calls = 0

    async def decide(self, model, input, questions):
        self.calls += 1
        await asyncio.sleep(0)
        return Decided(
            [],
            LLMUsage(
                input_tokens=5,
                output_tokens=1,
                cache_write_tokens=0,
                cache_write_30m_tokens=0,
                cache_write_12h_tokens=0,
            ),
            provider=self.name,
            model=model,
        )


def test_purpose_is_durable_in_first_reservation_without_duplicate_save(tmp_path):
    settings = harness.replay_settings(False, "adaptive")
    assert settings.openai_detector_model is not None
    assert settings.openai_fast_model is not None
    store = RecordStore(tmp_path)
    budget = harness.SessionBudget(tmp_path, settings, 1, store=store)
    token = harness.PURPOSE.set("detector_decisions")
    trial = TRIAL.set("authored:adaptive:0")
    try:
        budget.reserve(settings.openai_detector_model, [], 0)
    finally:
        TRIAL.reset(trial)
        harness.PURPOSE.reset(token)
    assert len([row for row in store.write_samples if row["kind"] == "cost"]) == 1
    assert not (tmp_path / "cost.json").exists()
    stored = RecordStore(tmp_path).read("cost")
    assert stored[0]["purpose"] == "detector_decisions"
    assert stored[0]["trial_id"] == "authored:adaptive:0"


def test_unpriced_usage_and_cancel_reservation_survive_record_resume(tmp_path):
    settings = harness.replay_settings(False, "adaptive")
    assert settings.openai_detector_model is not None
    assert settings.openai_fast_model is not None
    store = RecordStore(tmp_path)
    budget = harness.SessionBudget(tmp_path, settings, 1, store=store)
    model = settings.openai_fast_model
    unpriced = budget.reserve(model, "unknown cache-write counters", 100)
    reserved = budget.entries[unpriced]["charged_usd"]
    budget.finish(unpriced, LLMDone(LLMUsage(5, 0, 1), "stop", model=model))
    budget.reserve(model, "cancelled with no terminal usage", 100)
    failed = budget.reserve(model, "connection never submitted", 100)
    provider = harness.SessionAuditProvider(Decisions(), budget, [], tmp_path, store=store)
    provider.connection_failure(failed, httpx2.ConnectError("synthetic"))
    expected = json.loads(json.dumps(budget.entries))
    resumed = harness.SessionBudget(tmp_path, settings, 1, store=RecordStore(tmp_path))
    assert resumed.entries == expected
    assert [row["status"] for row in resumed.entries] == [
        "usage_unpriced",
        "reserved",
        "failed_before_submission",
    ]
    assert resumed.entries[unpriced]["charged_usd"] == reserved
    assert resumed.entries[unpriced]["usage"]["input_tokens"] == 5
    assert resumed.entries[failed]["charged_usd"] == 0
    assert resumed.total == budget.total


async def test_decisions_have_durable_uuid_status_and_no_array_writes(tmp_path):
    settings = harness.replay_settings(False, "adaptive")
    assert settings.openai_detector_model is not None
    assert settings.openai_fast_model is not None
    store = RecordStore(tmp_path)
    budget = harness.SessionBudget(tmp_path, settings, 1, store=store)
    raw = Decisions()
    records = []
    provider = harness.SessionAuditProvider(raw, budget, records, tmp_path, store=store)
    await provider.decide(settings.openai_detector_model, [], [])
    await provider.decide(settings.openai_detector_model, [], [])
    saved = RecordStore(tmp_path).read("requests")
    assert len({row["request_id"] for row in saved}) == 2
    assert all(row["status"] == "completed" for row in saved)
    assert all(row["purpose"] == "detector_decisions" for row in saved)
    assert all(row["elapsed_s"] >= 0 for row in saved)
    assert raw.calls == 2
    assert not (tmp_path / "request-identities.json").exists()
    assert not (tmp_path / "cost.json").exists()
    assert harness.PURPOSE.get() == "answer"


async def test_checkpoint_failure_blocks_decisions_before_provider_and_restores_context(
    tmp_path, monkeypatch
):
    settings = harness.replay_settings(False, "adaptive")
    assert settings.openai_detector_model is not None
    assert settings.openai_fast_model is not None
    store = RecordStore(tmp_path)
    budget = harness.SessionBudget(tmp_path, settings, 1, store=store)
    gate = harness.DispatchGate(1)
    store.on_error = lambda: gate.stop("checkpoint_error")
    raw = Decisions()
    provider = harness.LimitedProvider(
        harness.SessionAuditProvider(raw, budget, [], tmp_path, store=store), gate
    )
    real_open = Path.open

    def broken_open(path, *positional, **keywords):
        if path.parent.name == "requests" and path.suffix == ".tmp":
            raise PermissionError("synthetic checkpoint failure")
        return real_open(path, *positional, **keywords)

    monkeypatch.setattr(Path, "open", broken_open)
    with pytest.raises(PermissionError):
        await provider.decide(settings.openai_detector_model, [], [])
    assert raw.calls == 0
    assert budget.entries == []
    assert gate.reason == "checkpoint_error"
    assert gate.active == 0
    assert harness.PURPOSE.get() == "answer"
    with pytest.raises(AdmissionBlocked):
        await provider.decide(settings.openai_detector_model, [], [])


async def test_active_session_writes_records_only_and_exports_after_all_workers_drain(
    tmp_path, monkeypatch
):
    stable_identity(monkeypatch)
    options = args(tmp_path / "record-run")
    active = 0
    exported = []
    original_export = RecordStore.export

    def export(store, kind, filename):
        assert active == 0
        exported.append(filename)
        original_export(store, kind, filename)

    async def replay(scenario, settings, providers, *, trial_id, attempt_store, **kwargs):
        nonlocal active
        active += 1
        token = TRIAL.set(trial_id)
        try:
            assert attempt_store is not None
            audit = providers[0].inner
            assert audit.store is attempt_store
            for filename in ("sessions.json", "cost.json", "request-identities.json"):
                assert not (options.out / filename).exists()
            record = {
                "request_id": str(uuid4()),
                "trial_id": trial_id,
                "purpose": "detector_decisions",
                "status": "completed",
            }
            audit.requests.append(record)
            audit.checkpoint(record)
            audit.budget.reserve(settings.openai_detector_model, [], 0)
            attempt_store.put("attempts", trial_id, {"trial_id": trial_id, "status": "completed"})
            await asyncio.sleep(0.01)
            return completed(scenario, settings, trial_id)
        finally:
            TRIAL.reset(token)
            active -= 1

    monkeypatch.setattr(RecordStore, "export", export)
    monkeypatch.setattr(harness, "replay_session", replay)
    result = await harness.execute(options)
    assert result["sessions"] == 2
    assert result["checkpoint_policy"] == POLICY
    assert set(exported) == {
        "sessions.json",
        "cost.json",
        "request-identities.json",
        "provider-attempts.json",
    }
    records = RecordStore(options.out)
    assert len(records.read("sessions")) == len(records.read("requests")) == 2
    assert json.loads((options.out / "cost.json").read_text("utf-8")) == records.read("cost")
    assert result["observed_cost_usd"] is None
    writes = json.loads((options.out / "checkpoint-write-telemetry.json").read_text("utf-8"))
    assert any(
        row["kind"] == "sessions" and row["phase"] == "active_workers" for row in writes["writes"]
    )
    assert (options.out / "event-loop-lag.json").exists()


async def test_resume_uses_records_and_preserves_measured_telemetry_when_exports_stale(
    tmp_path, monkeypatch
):
    stable_identity(monkeypatch)
    options = args(tmp_path / "record-run")
    calls = []

    async def replay(scenario, settings, providers, *, trial_id, **kwargs):
        calls.append(trial_id)
        return completed(scenario, settings, trial_id)

    monkeypatch.setattr(harness, "replay_session", replay)
    expected = await harness.execute(options)
    telemetry = (options.out / "checkpoint-write-telemetry.json").read_bytes()
    lag = (options.out / "event-loop-lag.json").read_bytes()
    (options.out / "sessions.json").write_text("[]", "utf-8")
    (options.out / "progress.json").write_text('{"completed":1,"in_flight":1}', "utf-8")
    assert await harness.execute(options) == expected
    assert len(calls) == 2
    assert len(json.loads((options.out / "sessions.json").read_text("utf-8"))) == 2
    progress = json.loads((options.out / "progress.json").read_text("utf-8"))
    assert progress["completed"] == 2
    assert progress["in_flight"] == 0
    assert (options.out / "checkpoint-write-telemetry.json").read_bytes() == telemetry
    assert (options.out / "event-loop-lag.json").read_bytes() == lag
    history = list((options.out / "instrumentation-invocations").glob("*.json"))
    assert len(history) == 2


async def test_corrupt_authoritative_record_fails_before_any_new_session(tmp_path, monkeypatch):
    stable_identity(monkeypatch)
    options = args(tmp_path / "record-run")
    calls = []

    async def replay(scenario, settings, providers, *, trial_id, **kwargs):
        calls.append(trial_id)
        return completed(scenario, settings, trial_id)

    monkeypatch.setattr(harness, "replay_session", replay)
    await harness.execute(options)
    record = next((options.out / "checkpoint-records" / "sessions").glob("*.json"))
    record.write_text('{"corrupt":true}', "utf-8")
    with pytest.raises(ValueError, match="Corrupt committed"):
        await harness.execute(options)
    assert len(calls) == 2


async def test_actual_session_exports_attempts_and_all_terminal_decisions(tmp_path, monkeypatch):
    stable_identity(monkeypatch)
    options = args(tmp_path / "real-offline-session")
    options.scenarios = ["duplicate-partial"]
    options.concurrency = 1
    result = await harness.execute(options)
    assert result["status"] == "completed"
    assert result["invariant_violations"] == 0
    records = RecordStore(options.out)
    requests = records.read("requests")
    attempts = records.read("attempts")
    assert requests
    assert attempts
    assert len({row["request_id"] for row in requests}) == len(requests)
    assert all(row["status"] != "started" for row in requests)
    assert any(row["purpose"] == "detector_decisions" for row in requests)
    assert json.loads((options.out / "provider-attempts.json").read_text("utf-8")) == attempts
