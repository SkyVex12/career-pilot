"""Operational timeout/stop guards: fake providers, durable accounting, no network."""

import asyncio
import json
import signal
import threading
from argparse import Namespace
from contextlib import suppress
from typing import Any

import pytest
from copilot_evals import live_answer_replay as replay
from copilot_evals.live_answer_schedule import FailureCircuit

from copilot_core.llm import LLMDone, LLMUsage
from copilot_core.pricing import ModelPrice
from copilot_gateway.config import Settings


def timeout_result():
    return {
        "failure": "llm_unavailable",
        "metadata_logs": [
            {
                "kind": "provider_attempt",
                "status": "failed",
                "error_category": "first_token_timeout",
            }
        ],
        "events": [],
    }


def test_short_deadline_failures_need_separate_streak_and_success_resets():
    circuit = FailureCircuit()
    for _ in range(7):
        circuit.observe(timeout_result(), [{"status": "CancelledError", "provider_invoked": True}])
    assert circuit.reason is None
    assert circuit.consecutive == 0
    assert circuit.zero_output_timeouts == 7
    circuit.observe({"failure": None}, [{"status": "completed"}])
    assert circuit.zero_output_timeouts == 0
    for _ in range(8):
        circuit.observe(timeout_result(), [{"status": "CancelledError", "provider_invoked": True}])
    assert circuit.reason == "zero_output_timeout_streak"
    circuit.observe({"failure": None}, [{"status": "completed"}])
    assert circuit.reason == "zero_output_timeout_streak"
    assert circuit.snapshot()["zero_output_timeouts_since_progress"] == 8


@pytest.mark.parametrize("progress", ["released", "held", "first_chunk", "done"])
def test_partial_or_held_response_progress_does_not_count_as_zero_output(progress):
    circuit = FailureCircuit(zero_output_timeout_threshold=2)
    circuit.observe(timeout_result(), [])
    result, attempts = timeout_result(), []
    if progress == "released":
        result["events"] = [{"message": {"type": "answer.delta", "text": "Primary answer."}}]
    elif progress == "held":
        attempts = [{"status": "CancelledError", "streamed_chars": 4}]
    elif progress == "first_chunk":
        result["metadata_logs"].append({"kind": "provider_attempt", "status": "first_chunk"})
    else:
        attempts = [{"status": "completed"}]
    circuit.observe(result, attempts)
    assert circuit.zero_output_timeouts == 0
    assert circuit.reason is None


def test_quality_only_and_expected_injected_failures_are_not_timeout_evidence():
    circuit = FailureCircuit(zero_output_timeout_threshold=2)
    circuit.observe(timeout_result(), [])
    circuit.observe({"failure": "validation_failure"}, [])
    assert circuit.zero_output_timeouts == 1
    circuit.observe({**timeout_result(), "expected_injected_failure": True}, [])
    assert circuit.zero_output_timeouts == 1
    assert circuit.reason is None


@pytest.mark.parametrize("value", [True, 0, -1, 101, 1.5])
def test_timeout_limit_rejects_unbounded_or_noninteger_controls(value):
    with pytest.raises(ValueError, match="Zero-output timeout threshold"):
        FailureCircuit(zero_output_timeout_threshold=value)


@pytest.mark.parametrize("value", [True, 0, -1, 301, float("nan"), float("inf")])
def test_stop_drain_bound_is_finite(value):
    with pytest.raises(ValueError, match="Stop drain timeout"):
        replay.stop_policy(Namespace(stop_drain_timeout=value))


async def test_stop_file_is_payload_free_idempotent_and_handlers_are_restored(
    tmp_path, monkeypatch
):
    handlers: dict[int, Any] = {signal.SIGINT: object(), signal.SIGTERM: object()}
    original = handlers.copy()

    def install(number, callback):
        previous = handlers[number]
        handlers[number] = callback
        return previous

    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(signal, "getsignal", lambda number: handlers[number])
    stops, records = [], []
    flag = tmp_path / "stop.request"
    stop = replay.ExternalStop(flag, stops.append, records.append)
    await stop.start()
    try:
        flag.write_text("private request contents must never be read")
        await stop.check_file()
        handlers[signal.SIGINT](signal.SIGINT, None)
        await asyncio.sleep(0)
        assert stops == ["external_stop"]
        assert len(records) == 1
        assert records[0]["source"] == "file"
        assert "private request" not in json.dumps(records)
    finally:
        await stop.close()
    assert handlers == original
    assert flag.exists()


async def test_graceful_drain_preserves_success_before_deadline():
    stop, release = asyncio.Event(), asyncio.Event()

    async def work():
        await release.wait()

    task = asyncio.create_task(replay.drain_on_stop(work(), stop, 0.3, 0.02))
    stop.set()
    await asyncio.sleep(0)
    release.set()
    assert await task == {"status": "drained_after_stop", "workers_drained": True}


async def test_drain_timeout_cancels_cooperative_worker():
    stop = asyncio.Event()
    stop.set()
    closed = asyncio.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    result = await replay.drain_on_stop(work(), stop, 0.01, 0.1)
    assert result == {"status": "cancelled_after_grace", "workers_drained": True}
    assert closed.is_set()


async def test_ignored_cancellation_is_explicitly_unconfirmed_and_test_owns_cleanup():
    stop, release = asyncio.Event(), asyncio.Event()
    stop.set()
    state = {}

    async def work():
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()

    worker = asyncio.create_task(work())
    try:
        with pytest.raises(replay.ReplayDrainTimeout, match="ignored cancellation"):
            await replay.drain_on_stop(worker, stop, 0.01, 0.01, state)
        assert state["workers_drained"] is False
        assert state["status"] == "unconfirmed"
        assert not worker.done()
    finally:
        release.set()
        await asyncio.wait_for(worker, 1)


async def test_worker_error_is_not_misreported_as_an_unconfirmed_drain():
    async def work():
        raise RuntimeError("authored failure")

    state = {}
    with pytest.raises(RuntimeError, match="authored failure"):
        await replay.drain_on_stop(work(), asyncio.Event(), 1, state=state)
    assert state == {"status": "failed", "workers_drained": True}


def config():
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
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
        openai_first_token_timeout_ms=50,
        openai_fast_retry_tier=None,
        openai_deep_retry_tier=None,
        fast_hedge_ms=None,
        deep_hedge_ms=None,
        model_prices={
            "offline-fast": ModelPrice(input=1, cached=0.1, output=2),
            "offline-deep": ModelPrice(input=2, cached=0.2, output=3),
        },
    )


def arguments(**changes):
    return Namespace(
        **{
            "architectures": ["D"],
            "reps": 1,
            "seed": 1,
            "concurrency": 1,
            "timeout": 5,
            "max_cost": None,
            "skip_connectivity_check": True,
            "max_provider_inflight": 1,
            "zero_output_timeout_limit": 2,
            "stop_drain_timeout": 0.02,
            **changes,
        }
    )


class HeldProvider:
    name = "openai"

    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.closed = 0

    async def stream(self, model, req, params):
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
            yield "A complete answer."
            yield LLMDone(LLMUsage(10, 0, 4), "stop", "openai", model)
        finally:
            self.closed += 1


def install(monkeypatch, provider):
    monkeypatch.setattr(replay, "make_providers", lambda *a, **k: [provider])
    monkeypatch.setattr(replay, "experiment_identity", lambda *a: {"code": {}, "test": "stop"})


async def test_uncapped_run_stops_timeout_streak_and_keeps_unknown_cost(tmp_path, monkeypatch):
    provider = HeldProvider()
    install(monkeypatch, provider)
    await replay.execute_live(arguments(), replay.load_cases()[:4], config(), None, None, tmp_path)
    accounting = json.loads((tmp_path / "accounting.json").read_text())
    assert accounting["budget_limit_usd"] is None
    assert accounting["circuit_reason"] == "zero_output_timeout_streak"
    assert accounting["completed_trials"] == 2
    assert accounting["not_started"] == 2
    assert accounting["failure_circuit"]["zero_output_timeouts_since_progress"] == 2
    assert accounting["actual_billed_cost_usd"] is None
    entries = json.loads((tmp_path / "cost.json").read_text())
    assert any(e["status"] == "reserved" for e in entries)
    assert provider.calls == provider.closed
    assert json.loads((tmp_path / "progress.json").read_text())["finalized"] is True


async def test_preexisting_stop_makes_no_provider_request_and_retains_all_cases(
    tmp_path, monkeypatch
):
    provider = HeldProvider()
    install(monkeypatch, provider)
    (tmp_path / "stop.request").write_text("stop")
    await replay.execute_live(arguments(), replay.load_cases()[:3], config(), None, None, tmp_path)
    assert provider.calls == 0
    accounting = json.loads((tmp_path / "accounting.json").read_text())
    assert accounting["completed_trials"] == 0
    assert accounting["not_started"] == 3
    assert accounting["circuit_reason"] == "external_stop"
    assert json.loads((tmp_path / "cost.json").read_text()) == []
    assert len(json.loads((tmp_path / "not-started.json").read_text())) == 3


async def test_external_stop_cancels_started_request_as_ambiguous_not_free(tmp_path, monkeypatch):
    provider = HeldProvider()
    install(monkeypatch, provider)
    settings = config().model_copy(update={"openai_first_token_timeout_ms": 5000})
    task = asyncio.create_task(
        replay.execute_live(arguments(), replay.load_cases()[:3], settings, None, None, tmp_path)
    )
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        (tmp_path / "stop.request").write_text("stop")
        await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    accounting = json.loads((tmp_path / "accounting.json").read_text())
    assert provider.calls == provider.closed == 1
    assert accounting["completed_trials"] == 0
    assert accounting["interrupted_outcome_unknown"] == 1
    assert accounting["not_started"] == 2
    assert accounting["external_stop"]["drain"]["status"] == "cancelled_after_grace"
    ledger = json.loads((tmp_path / "cost.json").read_text())
    assert ledger[0]["status"] == "reserved"
    assert ledger[0]["charged_usd"] > 0
    journal = json.loads((tmp_path / "dispatch-state.json").read_text())
    assert not journal["in_flight"]
    assert len(journal["ambiguous"]) == 1
    pending = json.loads((tmp_path / "not-started.json").read_text())
    assert sum(row["status"] == "interrupted_outcome_unknown" for row in pending) == 1


async def test_stop_file_stat_does_not_block_loop_and_timeout_stops_once(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    calls, stops, records = [], [], []
    stop = replay.ExternalStop(tmp_path / "stop.request", stops.append, records.append)
    loop_thread = threading.get_ident()

    def slow_stat(_path):
        calls.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return False

    def record(value):
        assert threading.get_ident() == loop_thread
        records.append(value)

    stop.record = record
    monkeypatch.setattr(type(stop.path), "is_file", slow_stat)
    monkeypatch.setattr(stop, "FILE_STAT_TIMEOUT_S", 0.02)
    pending = asyncio.create_task(stop.check_file())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # This coroutine can run while the filesystem call is blocked off-loop.
        assert not pending.done()
        await asyncio.wait_for(pending, 0.5)
        assert stops == ["stop_control_error"]
        assert records[0]["file_stat_failure"] == "timeout"
        assert calls[0] != loop_thread
        await stop.check_file()
        assert len(calls) == 1
    finally:
        release.set()
        await pending


async def test_stop_remains_sticky_when_inflight_answer_finishes_successfully(
    tmp_path, monkeypatch
):
    provider = HeldProvider()
    install(monkeypatch, provider)
    settings = config().model_copy(update={"openai_first_token_timeout_ms": 5000})
    acknowledged = asyncio.Event()
    original_request = replay.ExternalStop.request

    def request(stop, source):
        original_request(stop, source)
        acknowledged.set()

    monkeypatch.setattr(replay.ExternalStop, "request", request)
    task = asyncio.create_task(
        replay.execute_live(
            arguments(stop_drain_timeout=1), replay.load_cases()[:3], settings, None, None, tmp_path
        )
    )
    try:
        await asyncio.wait_for(provider.entered.wait(), 2)
        (tmp_path / "stop.request").write_text("stop")
        await asyncio.wait_for(acknowledged.wait(), 2)
        provider.release.set()
        await asyncio.wait_for(task, 3)
    finally:
        provider.release.set()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    accounting = json.loads((tmp_path / "accounting.json").read_text())
    results = json.loads((tmp_path / "trials.json").read_text())
    assert provider.calls == provider.closed == 1
    assert len(results) == 1
    assert results[0]["failure"] is None
    assert accounting["completed_trials"] == 1
    assert accounting["not_started"] == 2
    assert accounting["interrupted_outcome_unknown"] == 0
    assert accounting["circuit_reason"] == "external_stop"
    assert accounting["external_stop"]["drain"]["status"] == "drained_after_stop"
