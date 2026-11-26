"""Crash boundaries and replay equivalence for constant-size atomic checkpoints."""

import json
import time
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from copilot_evals.live_answer_checkpoints import LoopLagMonitor, RecordDispatchJournal, RecordStore
from copilot_evals.live_answer_replay import TRIAL, AuditBudget, AuditProvider, load_cases, trial
from copilot_evals.sensei_runtime import MeteredProvider

from copilot_core.llm import LLMDone, LLMUsage
from copilot_core.pricing import ModelPrice
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.testing import Reply, ScriptedProvider


def configuration(architecture="adaptive_on_demand"):
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] - BaseSettings runtime keyword.
        adaptive_expert_route=None,
        adaptive_technical_legacy=False,
        llm_providers=["openai"],
        answer_architecture=architecture,
        openai_detector_model="offline-fast",
        openai_detector_reasoning="none",
        openai_fast_reasoning="none",
        openai_deep_reasoning="low",
        openai_fast_model="offline-fast",
        openai_deep_model="offline-deep",
        openai_fast_retry_tier=None,
        openai_deep_retry_tier=None,
        fast_hedge_ms=None,
        deep_hedge_ms=None,
        model_prices={
            "offline-fast": ModelPrice(input=1, cached=0.1, output=2),
            "offline-deep": ModelPrice(input=2, cached=0.2, output=3),
        },
    )


def test_store_replaces_only_changed_record_and_exports_original_array(tmp_path):
    store = RecordStore(tmp_path)
    values = [{"id": "b", "text": "Original."}, {"id": "a", "text": "Second."}]
    for value in values:
        store.put("trials", value["id"], value)
    paths = {
        json.loads(path.read_text(encoding="utf-8"))["key"]: path
        for path in (store.root / "trials").glob("*.json")
    }
    untouched = paths["a"].read_bytes()
    values[0]["text"] = "Changed."
    store.put("trials", "b", values[0])
    assert paths["a"].read_bytes() == untouched
    assert len(list((store.root / "trials").glob("*.json"))) == 2
    assert not (tmp_path / "trials.json").exists()
    restored = RecordStore(tmp_path)
    assert restored.read("trials") == values
    restored.export("trials", "trials.json")
    assert json.loads((tmp_path / "trials.json").read_text()) == values
    assert len(store.write_samples) == 3
    assert all(sample["bytes"] < 512 for sample in store.write_samples)


def test_uncommitted_temporary_is_ignored_but_corrupt_committed_record_is_fatal(tmp_path):
    store = RecordStore(tmp_path)
    store.put("requests", "a", {"status": "started"})
    path = next((store.root / "requests").glob("*.json"))
    path.with_suffix(".tmp").write_text("incomplete temporary")
    assert RecordStore(tmp_path).read("requests") == [{"status": "started"}]
    data = json.loads(path.read_text())
    data["value"]["status"] = "completed"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Corrupt committed"):
        RecordStore(tmp_path).read("requests")


def test_missing_middle_record_and_unknown_version_fail_closed(tmp_path):
    store = RecordStore(tmp_path)
    for i in range(3):
        store.put("attempts", str(i), {"ordinal": i})
    sorted((store.root / "attempts").glob("*.json"))[1].unlink()
    with pytest.raises(ValueError, match="Missing or duplicate"):
        RecordStore(tmp_path).read("attempts")
    (store.root / "manifest.json").write_text('{"schema":999}')
    with pytest.raises(ValueError, match="Unsupported checkpoint"):
        RecordStore(tmp_path)


@pytest.mark.parametrize(
    "boundary", ["started", "attempted", "state_completed", "result_committed"]
)
def test_resume_never_reissues_an_ambiguous_started_request(tmp_path, boundary):
    store = RecordStore(tmp_path)
    journal = RecordDispatchJournal(store, ["a", "b"], set(), set())
    if boundary != "attempted":
        assert journal.start("a")
    if boundary == "state_completed":
        journal.finish("a")
    if boundary == "result_committed":
        store.put("trials", "a", {"id": "a"})
    completed = {row["id"] for row in store.read("trials")}
    restored = RecordDispatchJournal(RecordStore(tmp_path), ["a", "b"], completed, {"a"})
    assert restored.states["a"] == ("completed" if boundary == "result_committed" else "ambiguous")
    assert not restored.start("a")
    assert restored.start("b")
    restored.export()
    assert json.loads((tmp_path / "dispatch-state.json").read_text())["in_flight"] == ["b"]


def test_dispatch_schedule_changes_and_unexpected_results_are_rejected(tmp_path):
    store = RecordStore(tmp_path)
    RecordDispatchJournal(store, ["a"], set(), set())
    with pytest.raises(ValueError, match="schedule changed"):
        RecordDispatchJournal(store, ["b"], set(), set())
    with pytest.raises(ValueError, match="Unexpected dispatch"):
        RecordDispatchJournal(store, ["a"], {"b"}, set())


def test_budget_shards_match_legacy_costs_and_ignore_stale_array(tmp_path):
    legacy_dir, shard_dir = tmp_path / "legacy", tmp_path / "shards"
    legacy_dir.mkdir()
    shard_dir.mkdir()
    legacy = AuditBudget(legacy_dir, configuration(), 1)
    store = RecordStore(shard_dir)
    shard = AuditBudget(shard_dir, configuration(), 1, store=store)
    token = TRIAL.set("case:D:0")
    try:
        for budget in (legacy, shard):
            paid = budget.reserve("offline-fast", {"original": "question"}, 10)
            budget.finish(paid, LLMDone(LLMUsage(10, 0, 3), "stop", "openai", "offline-fast"))
            budget.reserve("offline-fast", {"cancelled": "unknown usage"}, 10)
            failed = budget.reserve("offline-fast", {"connection": "never submitted"}, 10)
            MeteredProvider(SimpleNamespace(name="openai"), budget).connection_failure(
                failed, httpx2.ConnectError("offline")
            )
    finally:
        TRIAL.reset(token)
    assert shard.entries == legacy.entries
    assert shard.total == legacy.total
    assert [e["status"] for e in shard.entries] == ["usage", "reserved", "failed_before_submission"]
    (shard_dir / "cost.json").write_text("broken stale compatibility export")
    restored = AuditBudget(shard_dir, configuration(), 1, store=RecordStore(shard_dir))
    assert restored.entries == legacy.entries
    assert all(e["trial_id"] == "case:D:0" for e in restored.entries)


async def test_failed_reservation_checkpoint_prevents_actual_provider_dispatch(
    tmp_path, monkeypatch
):
    store = RecordStore(tmp_path)
    budget = AuditBudget(tmp_path, configuration(), 1, store=store)
    raw = ScriptedProvider("openai")
    provider = AuditProvider(raw, budget, [], tmp_path, store=store)
    original = store.put

    def reject_cost(kind, key, value):
        if kind == "cost":
            raise OSError("offline disk failure")
        original(kind, key, value)

    monkeypatch.setattr(store, "put", reject_cost)
    result = await trial(
        load_cases()[0], "D", 0, configuration(), build_router(configuration(), [provider]), 5
    )
    assert result["failure"]
    assert not raw.calls


@pytest.mark.parametrize(
    "architecture", ["legacy_dual", "adaptive", "adaptive_hybrid", "adaptive_on_demand"]
)
@pytest.mark.parametrize("depth", ["brief", "deep"])
async def test_scripted_actual_engine_replay_matches_legacy_storage(tmp_path, architecture, depth):
    case = {**load_cases()[0], "depth": depth}
    config = configuration(architecture)
    observations = []
    for name, sharded in (("legacy", False), ("records", True)):
        directory = tmp_path / name
        directory.mkdir()
        store = RecordStore(directory) if sharded else None
        budget = AuditBudget(directory, config, 1, store=store)
        raw = ScriptedProvider(
            "openai", lambda *args: Reply(["A useful complete sentence. ", "A grounded detail."])
        )
        provider = AuditProvider(raw, budget, [], directory, store=store)
        result = await trial(case, architecture, 0, config, build_router(config, [provider]), 5)
        text = {}
        terminal = []
        for event in result["events"]:
            message = event["message"]
            kind = message.get("pass")
            if message["type"] == "answer.delta":
                text[kind] = text.get(kind, "") + message["text"]
            if message["type"] in ("answer.done", "answer.error"):
                terminal.append((message["type"], kind, message.get("code")))
        observations.append(
            {
                "failure": result["failure"],
                "text": text,
                "terminal": terminal,
                "calls": raw.calls,
                "requests": [(job, asdict(request)) for job, request in raw.requests],
                "ledger": budget.entries,
            }
        )
    assert observations[0] == observations[1]


async def test_loop_monitor_observes_blocking_without_adjusting_answer_timings():
    monitor = LoopLagMonitor(interval_s=0.001)
    await monitor.start()
    time.sleep(0.025)  # noqa: ASYNC251 - Deliberately verify loop-blocking measurement.
    await monitor.stop()
    report = monitor.summary()
    assert report["samples"] > 0
    assert report["max_ms"] >= 15
    assert sum(report["histogram_ms"].values()) == report["samples"]
    assert "no latency subtraction" in report["scope"]


async def test_new_run_exports_after_drain_and_resume_ignores_stale_arrays(tmp_path, monkeypatch):
    from copilot_evals import live_answer_replay as replay
    from copilot_evals.live_answer_async_checkpoints import ASYNC_POLICY, BufferedRecordStore

    raw_runs = []

    def scripted(*args, **kwargs):
        raw = ScriptedProvider("openai", lambda *args: Reply(["A complete answer."]))
        raw_runs.append(raw)
        return [raw]

    monkeypatch.setattr(replay, "make_providers", scripted)
    monkeypatch.setattr(replay, "experiment_identity", lambda *args: {"code": {}, "test": "fixed"})
    exported = []
    original_export = BufferedRecordStore.export

    def export(store, kind, filename):
        assert store.phase == "drained"
        exported.append(filename)
        original_export(store, kind, filename)

    monkeypatch.setattr(BufferedRecordStore, "export", export)
    args = Namespace(
        architectures=["A", "B", "C", "D"],
        reps=1,
        seed=1,
        concurrency=2,
        timeout=5,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=4,
    )
    cases = load_cases()[:2]
    await replay.execute_live(args, cases, configuration(), None, None, tmp_path)
    first_trials = json.loads((tmp_path / "trials.json").read_text())
    first_cost = json.loads((tmp_path / "cost.json").read_text())
    assert len(first_trials) == 8
    assert raw_runs[0].calls
    assert set(exported) >= {
        "trials.json",
        "cost.json",
        "attempt-events.json",
        "request-identities.json",
    }
    assert json.loads((tmp_path / "identity.json").read_text())["checkpoint_policy"] == ASYNC_POLICY
    for name in ("trials.json", "cost.json", "attempt-events.json", "request-identities.json"):
        (tmp_path / name).write_text("stale incomplete export")
    (tmp_path / "progress.json").write_text('{"completed":7,"in_flight":1}')
    await replay.execute_live(args, cases, configuration(), None, None, tmp_path)
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["completed"] == progress["total"] == 8
    assert progress["in_flight"] == progress["not_started"] == 0
    assert not raw_runs[1].calls
    assert json.loads((tmp_path / "trials.json").read_text()) == first_trials
    assert json.loads((tmp_path / "cost.json").read_text()) == first_cost
    assert not json.loads((tmp_path / "not-started.json").read_text())
    assert (tmp_path / "checkpoint-write-telemetry.json").exists()
    assert (tmp_path / "event-loop-lag.json").exists()


@pytest.mark.parametrize("failure", ["read", "mkdir"])
async def test_observer_checkpoint_preparation_error_stops_before_provider_dispatch(
    tmp_path, monkeypatch, failure
):
    from copilot_evals.live_answer_replay import ProviderAdmission

    store = RecordStore(tmp_path)
    gate = ProviderAdmission(2)
    store.on_error = lambda: gate.stop("checkpoint_error")
    budget = AuditBudget(tmp_path, configuration(), 1, store=store)
    raw = ScriptedProvider("openai")
    provider = AuditProvider(raw, budget, [], tmp_path, admission=gate, store=store)
    original_read, original_mkdir = store.read, Path.mkdir
    if failure == "read":

        def reject_read(kind):
            if kind == "attempts":
                raise OSError("offline observer read failure")
            return original_read(kind)

        monkeypatch.setattr(store, "read", reject_read)
    else:

        def reject_mkdir(path, *args, **kwargs):
            if path.name == "attempts":
                raise OSError("offline observer mkdir failure")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", reject_mkdir)
    router = build_router(
        configuration(),
        [provider],
        on_attempt=lambda event: store.put(
            "attempts", event.attempt_id + event.status, asdict(event)
        ),
    )
    await trial(load_cases()[0], "D", 0, configuration(), router, 5)
    assert gate.reason == "checkpoint_error"
    assert gate.admitted == 0
    assert not raw.calls
    assert not budget.entries
