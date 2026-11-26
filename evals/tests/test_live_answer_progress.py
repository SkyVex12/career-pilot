"""Progress readers cannot cancel durable replay work; failures retain safe diagnostics."""

import asyncio
import json
import logging
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from copilot_evals import live_answer_async_checkpoints as async_checkpoints
from copilot_evals import live_answer_replay as replay
from copilot_evals.live_answer_async_checkpoints import BufferedRecordStore
from copilot_evals.live_answer_checkpoints import RecordStore
from copilot_evals.sensei_runtime import save as save_artifact

from copilot_core.pricing import ModelPrice
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.testing import Reply, ScriptedProvider


def configuration():
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] - BaseSettings runtime keyword.
        adaptive_expert_route=None,
        adaptive_technical_legacy=False,
        llm_providers=["openai"],
        answer_architecture="adaptive_on_demand",
        openai_detector_model="offline-fast",
        openai_detector_reasoning="none",
        openai_fast_model="offline-fast",
        openai_fast_reasoning="none",
        openai_deep_model="offline-deep",
        openai_deep_reasoning="low",
        openai_fast_retry_tier=None,
        openai_deep_retry_tier=None,
        fast_hedge_ms=None,
        deep_hedge_ms=None,
        model_prices={
            "offline-fast": ModelPrice(input=1, cached=0.1, output=2),
            "offline-deep": ModelPrice(input=2, cached=0.2, output=3),
        },
    )


def sharing_error():
    error = PermissionError(13, "PRIVATE payload or filename must not be exported")
    # Simulate the Windows-only attribute on Linux CI too.
    error.winerror = 5  # pyright: ignore[reportAttributeAccessIssue]
    return error


def test_active_snapshot_errors_are_bounded_without_retry(tmp_path, monkeypatch):
    snapshots = replay.ProgressSnapshots(tmp_path)
    calls = []

    def fail(path, value):
        calls.append(path)
        raise sharing_error()

    monkeypatch.setattr(replay, "save", fail)
    for i in range(20):
        snapshots.active({"completed": i})
    diagnostics = snapshots.diagnostics()
    assert len(calls) == diagnostics["failures"] == 20
    assert len(diagnostics["samples"]) == 16
    assert diagnostics["omitted_samples"] == 4
    assert "PRIVATE" not in json.dumps(diagnostics)


def test_active_programming_error_is_not_suppressed(tmp_path, monkeypatch):
    snapshots = replay.ProgressSnapshots(tmp_path)

    def fail(path, value):
        raise ValueError("invalid value")

    monkeypatch.setattr(replay, "save", fail)
    with pytest.raises(ValueError, match="invalid value"):
        snapshots.active({})
    assert snapshots.failure_count == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read handles deny atomic replacement")
async def test_real_windows_progress_read_lock_does_not_abort(tmp_path):
    snapshots = replay.ProgressSnapshots(tmp_path)
    save_artifact(snapshots.path, {"completed": 0})
    with snapshots.path.open("rb"):
        snapshots.active({"completed": 1})
    assert snapshots.failure_count == 1
    assert json.loads(snapshots.path.read_text())["completed"] == 0
    await snapshots.final(lambda: {"completed": 1})
    assert json.loads(snapshots.path.read_text())["completed"] == 1


@pytest.mark.parametrize("succeeds", [True, False])
async def test_final_snapshot_has_bounded_windows_retry(tmp_path, monkeypatch, succeeds):
    snapshots = replay.ProgressSnapshots(tmp_path)
    calls, delays = [], []

    def fail(path, value):
        calls.append(value)
        if not succeeds or len(calls) < 4:
            raise sharing_error()

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(replay, "save", fail)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    if succeeds:
        await snapshots.final(lambda: {"failures": snapshots.failure_count})
        assert snapshots.failure_count == 3
    else:
        with pytest.raises(PermissionError):
            await snapshots.final(lambda: {})
        assert snapshots.failure_count == 4
    assert len(calls) == 4
    assert delays == [0.01, 0.03, 0.1]


async def test_nonsharing_terminal_error_is_immediately_fatal(tmp_path, monkeypatch):
    snapshots = replay.ProgressSnapshots(tmp_path)

    def fail(path, value):
        raise OSError(28, "PRIVATE disk detail")

    monkeypatch.setattr(replay, "save", fail)
    with pytest.raises(OSError, match="PRIVATE disk detail"):
        await snapshots.final(lambda: {})
    assert snapshots.failure_count == 1


@pytest.mark.parametrize("terminal_failure", [False, True])
async def test_progress_failure_does_not_cancel_provider_work(
    tmp_path, monkeypatch, terminal_failure
):
    raw = ScriptedProvider("openai", lambda *args: Reply(["A complete answer."]))
    monkeypatch.setattr(replay, "make_providers", lambda *args, **kwargs: [raw])
    monkeypatch.setattr(replay, "experiment_identity", lambda *args: {"code": {}, "test": "fixed"})
    drained = False

    async def close(providers):
        nonlocal drained
        drained = True

    monkeypatch.setattr(replay, "close_providers", close)
    original_save = save_artifact
    active_failures = 0

    def save(path, value):
        nonlocal active_failures
        if path.name == "progress.json":
            if value.get("finalized") and terminal_failure:
                raise sharing_error()
            if not value.get("finalized") and active_failures == 0:
                active_failures += 1
                raise sharing_error()
        original_save(path, value)

    monkeypatch.setattr(replay, "save", save)
    monkeypatch.setattr(async_checkpoints, "save", save)
    args = Namespace(
        architectures=["D"],
        reps=2,
        seed=1,
        concurrency=2,
        timeout=5,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=4,
    )
    task = replay.execute_live(args, replay.load_cases()[:2], configuration(), None, None, tmp_path)
    if terminal_failure:
        with pytest.raises(PermissionError):
            await task
    else:
        await task
    assert drained
    assert active_failures == 1
    trials = json.loads((tmp_path / "trials.json").read_text())
    assert len(trials) == len(raw.calls) == 4
    assert all(row["status"] == "completed" for row in trials)
    requests = json.loads((tmp_path / "request-identities.json").read_text())
    assert all(row["status"] == "completed" for row in requests)
    telemetry = json.loads((tmp_path / "checkpoint-write-telemetry.json").read_text())
    assert len(telemetry["snapshot_errors"]) == 1
    if terminal_failure:
        failure = json.loads((tmp_path / "finalization-failure.json").read_text())
        assert failure["status"] == "finalization_failed"
        assert failure["finalized"] is False
        assert failure["error_type"] == "PermissionError"
        assert not (tmp_path / "progress-write-errors.json").exists()
    else:
        errors = json.loads((tmp_path / "progress-write-errors.json").read_text())
        assert errors["failures"] == 0
        assert len(errors["async_snapshot_errors"]) == 1
        progress = json.loads((tmp_path / "progress.json").read_text())
        assert progress["completed"] == progress["total"] == 4
        assert progress["in_flight"] == progress["not_started"] == 0
        assert progress["finalized"] is True
        assert len(progress["progress_snapshot_errors"]["async_snapshot_errors"]) == 1


async def test_durable_record_replace_error_still_stops_before_dispatch(tmp_path, monkeypatch):
    store = RecordStore(tmp_path)
    gate = replay.ProviderAdmission(2)
    store.on_error = lambda: gate.stop("checkpoint_error")
    budget = replay.AuditBudget(tmp_path, configuration(), 1, store=store)
    raw = ScriptedProvider("openai")
    provider = replay.AuditProvider(raw, budget, [], tmp_path, admission=gate, store=store)
    original_replace = Path.replace

    def replace(path, target):
        if path.parent.name == "cost":
            raise sharing_error()
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    result = await replay.trial(
        replay.load_cases()[0],
        "D",
        0,
        configuration(),
        build_router(configuration(), [provider]),
        5,
    )
    assert result["failure"]
    assert gate.reason == "checkpoint_error"
    assert not raw.calls


def test_main_exports_exception_leaves_without_messages_or_payloads(tmp_path, monkeypatch, capsys):
    async def fail(args):
        try:
            raise sharing_error()
        except OSError as cause:
            raise ExceptionGroup(
                "PRIVATE group message", [cause, ValueError("PRIVATE prompt")]
            ) from cause

    monkeypatch.setattr(replay, "execute", fail)
    threshold = logging.root.manager.disable
    assert replay.main(["--out", str(tmp_path)]) == 1
    assert logging.root.manager.disable == threshold
    console = json.loads(capsys.readouterr().out)
    artifact = json.loads((tmp_path / "failure.json").read_text())
    assert console["failure_artifact_written"] is True
    assert {row["type"] for row in console["errors"]} == {"PermissionError", "ValueError"}
    leaf = artifact["error"]["children"][0]
    assert leaf["errno"] == 13
    assert leaf["winerror"] == 5
    assert leaf["frames"][-1]["file"] == "test_live_answer_progress.py"
    encoded = json.dumps([console, artifact])
    assert "PRIVATE" not in encoded
    assert str(tmp_path) not in encoded


def test_failure_artifact_error_keeps_original_leaf_in_console(tmp_path, monkeypatch):
    def fail(path, value):
        raise OSError(28, "PRIVATE storage payload")

    monkeypatch.setattr(replay, "save", fail)
    report = replay.write_failure_report(tmp_path, ExceptionGroup("PRIVATE", [sharing_error()]))
    assert report["failure_artifact_written"] is False
    assert report["errors"][0]["type"] == "PermissionError"
    assert report["errors"][0]["winerror"] == 5
    assert report["artifact_error"]["errno"] == 28
    assert "PRIVATE" not in json.dumps(report)


def test_exception_diagnostics_bound_groups_and_detect_cycles():
    error = ValueError("PRIVATE")
    error.__cause__ = error
    tree = replay.sanitized_exception_tree(ExceptionGroup("PRIVATE", [error] * 50))
    assert len(tree["children"]) == 8
    assert tree["omitted_children"] == 42
    assert tree["children"][0]["cause"]["truncated"] is True
    assert "PRIVATE" not in json.dumps(tree)


async def test_final_progress_does_not_claim_finalized_after_export_error(tmp_path, monkeypatch):
    raw = ScriptedProvider("openai", lambda *args: Reply(["A complete answer."]))
    monkeypatch.setattr(replay, "make_providers", lambda *args, **kwargs: [raw])
    monkeypatch.setattr(replay, "experiment_identity", lambda *args: {"code": {}, "test": "fixed"})
    original_export = BufferedRecordStore.export

    def export(store, kind, filename):
        assert store.phase == "drained"
        if kind == "trials":
            raise OSError(28, "authored final export failure")
        original_export(store, kind, filename)

    monkeypatch.setattr(BufferedRecordStore, "export", export)
    args = Namespace(
        architectures=["D"],
        reps=1,
        seed=1,
        concurrency=1,
        timeout=5,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=4,
    )
    with pytest.raises(OSError, match="authored final export failure"):
        await replay.execute_live(
            args, replay.load_cases()[:1], configuration(), None, None, tmp_path
        )
    progress = json.loads((tmp_path / "progress.json").read_text())
    assert progress["completed"] == progress["total"] == 1
    assert progress["provider_admission"]["active"] == 0
    assert progress["finalized"] is False
    assert not (tmp_path / "progress-write-errors.json").exists()
    failure = json.loads((tmp_path / "finalization-failure.json").read_text())
    assert failure["status"] == "finalization_failed"
    assert failure["finalized"] is False
    assert len(RecordStore(tmp_path).read("trials")) == 1
