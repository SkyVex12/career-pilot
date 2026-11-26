"""Actual replay integration with fake providers only; no network credentials used."""

import asyncio
import json
import shutil
import threading
import time
from argparse import Namespace
from contextlib import suppress

import pytest
from copilot_evals import live_answer_replay as replay
from copilot_evals import live_answer_session_replay as session
from copilot_evals.live_answer_async_checkpoints import (
    BufferedRecordStore,
    CheckpointUnavailable,
    DurableWriter,
)
from copilot_evals.live_answer_checkpoints import RecordDispatchJournal, RecordStore

from copilot_core.llm import Decided, LLMDone, LLMRequest, LLMUsage
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


async def finish(writer):
    with suppress(CheckpointUnavailable):
        await writer.close()


class Probe:
    name = "openai"

    def __init__(self, directory):
        self.directory = directory
        self.calls = []
        self.entered = asyncio.Event()
        self.hold: asyncio.Event | None = None

    async def check(self, operation):
        store = RecordStore(self.directory)
        assert store.read("requests")[-1]["status"] == "started"
        assert store.read("cost")[-1]["status"] == "reserved"
        assert store.read("dispatch")[0]["state"] == "in_flight"
        self.calls.append(operation)
        self.entered.set()
        if self.hold:
            await self.hold.wait()

    async def stream(self, model, req, params):
        await self.check("stream")
        yield "A complete answer."
        yield LLMDone(LLMUsage(10, 0, 3), "stop", "openai", model)

    async def decide(self, model, input, questions):
        await self.check("decide")
        return Decided([], LLMUsage(10, 0, 3), "openai", model)

    async def prewarm(self, model, req, params):
        await self.check("prewarm")
        return LLMDone(LLMUsage(10, 0, 3), "stop", "openai", model)


async def invoke(provider, operation):
    if operation == "stream":
        return [
            chunk
            async for chunk in provider.stream("offline-fast", LLMRequest([], "Question", 100), {})
        ]
    if operation == "decide":
        return await provider.decide("offline-fast", [], [])
    return await provider.prewarm("offline-fast", LLMRequest([], "Question", 100), {})


async def setup(directory, *, factory=RecordStore):
    writer = DurableWriter(directory, store_factory=factory)
    await writer.ready()
    store = BufferedRecordStore(writer)
    journal = RecordDispatchJournal(store, ["case:D:0"], set(), set())
    assert journal.start("case:D:0")
    budget = replay.AuditBudget(directory, configuration(), 1, store=store)
    raw = Probe(directory)
    records = []
    provider = replay.AuditProvider(raw, budget, records, directory, store=store)
    return writer, store, budget, raw, records, provider


@pytest.mark.parametrize("operation", ["stream", "decide", "prewarm"])
async def test_actual_provider_entry_requires_durable_request_cost_and_dispatch(
    tmp_path, operation
):
    writer, store, budget, raw, records, provider = await setup(tmp_path)
    try:
        await invoke(provider, operation)
        await store.flush()
        assert raw.calls == [operation]
        assert budget.entries[0]["status"] == "usage"
        assert records[0]["provider_invoked"] is True
        assert records[0]["durability_wait_ms"] >= 0
        assert records[0]["elapsed_s"] >= records[0]["provider_elapsed_s"]
        assert RecordStore(tmp_path).read("requests")[0]["status"] == "completed"
    finally:
        await finish(writer)


@pytest.mark.parametrize("operation", ["stream", "decide", "prewarm"])
async def test_failed_cost_write_never_invokes_provider_even_if_observer_swallows(
    tmp_path, operation
):
    class Failed(RecordStore):
        def put(self, kind, key, value):
            if kind == "cost":
                raise OSError("authored disk failure")
            return super().put(kind, key, value)

    writer, store, _budget, raw, records, provider = await setup(tmp_path, factory=Failed)
    store.on_error = lambda: (_ for _ in ()).throw(RuntimeError("observer swallowed"))
    try:
        with pytest.raises(CheckpointUnavailable):
            await invoke(provider, operation)
        assert not raw.calls
        assert not records[0]["provider_invoked"]
        with pytest.raises(CheckpointUnavailable):
            await invoke(provider, operation)
        assert not raw.calls
    finally:
        await finish(writer)


async def wait_thread(event):
    deadline = time.monotonic() + 3
    while not event.is_set():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.001)


@pytest.mark.parametrize("operation", ["stream", "decide", "prewarm"])
async def test_cancellation_during_durability_wait_is_known_not_invoked(tmp_path, operation):
    entered, release = threading.Event(), threading.Event()

    class Delayed(RecordStore):
        def put(self, kind, key, value):
            if kind == "cost" and value["status"] == "reserved":
                entered.set()
                assert release.wait(4)
            return super().put(kind, key, value)

    writer, store, budget, raw, records, provider = await setup(tmp_path, factory=Delayed)
    try:
        pending = asyncio.create_task(invoke(provider, operation))
        await wait_thread(entered)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not raw.calls
        release.set()
        await store.flush()
        assert RecordStore(tmp_path).read("cost")[0]["status"] == "failed_before_submission"
        assert budget.total == 0
        assert records[0]["provider_elapsed_s"] is None
    finally:
        release.set()
        await finish(writer)


@pytest.mark.parametrize("operation", ["stream", "decide", "prewarm"])
async def test_cancellation_after_provider_entry_keeps_unknown_usage_reservation(
    tmp_path, operation
):
    writer, store, budget, raw, records, provider = await setup(tmp_path)
    raw.hold = asyncio.Event()
    try:
        pending = asyncio.create_task(invoke(provider, operation))
        await raw.entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await store.flush()
        assert budget.entries[0]["status"] == "reserved"
        assert budget.total > 0
        assert records[0]["provider_invoked"] is True
    finally:
        await finish(writer)


async def test_actual_engine_request_deadline_includes_durable_wait(tmp_path):
    class Slow(RecordStore):
        def put(self, kind, key, value):
            if kind == "cost" and value["status"] == "reserved":
                time.sleep(0.12)
            return super().put(kind, key, value)

    writer, store, budget, raw, _records, provider = await setup(tmp_path, factory=Slow)
    config = configuration().model_copy(update={"openai_first_token_timeout_ms": 20})
    try:
        result = await replay.trial(
            replay.load_cases()[0], "D", 0, config, build_router(config, [provider]), 5
        )
        await store.flush()
        assert result["failure"]
        assert not raw.calls
        assert all(e["status"] == "failed_before_submission" for e in budget.entries)
    finally:
        await finish(writer)


async def test_generation_replay_exports_drains_and_resume_calls_no_provider(tmp_path, monkeypatch):
    runs = []

    def scripted(*args, **kwargs):
        raw = ScriptedProvider("openai", lambda *args: Reply(["A complete answer."]))
        runs.append(raw)
        return [raw]

    monkeypatch.setattr(replay, "make_providers", scripted)
    monkeypatch.setattr(replay, "experiment_identity", lambda *args: {"code": {}, "test": "fixed"})
    args = Namespace(
        architectures=["A", "D"],
        reps=1,
        seed=1,
        concurrency=2,
        timeout=5,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=4,
    )
    await replay.execute_live(args, replay.load_cases()[:2], configuration(), None, None, tmp_path)
    rows = json.loads((tmp_path / "trials.json").read_text())
    assert len(rows) == 4
    assert all(row["status"] == "completed" for row in rows)
    assert runs[0].calls
    first_cost = json.loads((tmp_path / "cost.json").read_text())
    assert first_cost
    assert all(row["status"] == "usage" for row in first_cost)
    assert (
        json.loads((tmp_path / "checkpoint-write-telemetry.json").read_text())["pending_records"]
        == 0
    )
    (tmp_path / "trials.json").write_text("stale export")
    await replay.execute_live(args, replay.load_cases()[:2], configuration(), None, None, tmp_path)
    assert not runs[1].calls
    assert json.loads((tmp_path / "trials.json").read_text()) == rows
    assert json.loads((tmp_path / "cost.json").read_text()) == first_cost
    assert json.loads((tmp_path / "progress.json").read_text())["in_flight"] == 0


async def test_real_session_offline_path_exports_and_resume_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session, "experiment_identity", lambda *args: {"code": {}, "test": "fixed-session"}
    )
    monkeypatch.setattr(
        session,
        "make_providers",
        lambda *args, **kwargs: pytest.fail("No live providers permitted"),
    )
    args = Namespace(
        out=tmp_path,
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
    result = await session.execute(args)
    assert result["sessions"] == 2
    assert result["invariant_violations"] == 0
    saved = json.loads((tmp_path / "sessions.json").read_text())
    assert all(row["status"] == "completed" for row in saved)
    assert all(row["violations"] == [] for row in saved)
    assert (
        json.loads((tmp_path / "checkpoint-write-telemetry.json").read_text())["pending_records"]
        == 0
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("Completed session cannot be rerun")

    monkeypatch.setattr(session, "replay_session", forbidden)
    assert await session.execute(args) == result


async def test_legacy_output_rejection_does_not_create_or_mutate_shards(tmp_path, monkeypatch):
    (tmp_path / "identity.json").write_text('{"checkpoint_policy":"atomic-records-v1"}')
    (tmp_path / "trials.json").write_text('[{"old":"preserved"}]')
    original = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(
        replay, "make_providers", lambda *a, **k: pytest.fail("No provider allowed")
    )
    args = Namespace(
        architectures=["D"],
        reps=1,
        seed=1,
        concurrency=1,
        timeout=5,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=1,
    )
    with pytest.raises(ValueError, match="Existing run uses another checkpoint policy"):
        await replay.execute_live(
            args, replay.load_cases()[:1], configuration(), None, None, tmp_path
        )
    assert not (tmp_path / "checkpoint-records").exists()
    assert all((tmp_path / name).read_bytes() == content for name, content in original.items())
    from copilot_evals.live_answer_artifacts import run_lock

    with run_lock(tmp_path):
        pass


@pytest.mark.parametrize("filename", ["provider-admission-stop.json", "admission-stop.json"])
async def test_admission_stop_snapshot_failure_is_sticky_and_blocks_raw_dispatch(
    tmp_path, monkeypatch, filename
):
    from copilot_evals import live_answer_async_checkpoints as persistence

    writer, store, _budget, raw, _records, provider = await setup(tmp_path)
    real = persistence.write_snapshot

    def fail(path, payload, *, durable):
        if path.name == filename:
            raise OSError("authored stop-file failure")
        return real(path, payload, durable=durable)

    monkeypatch.setattr(persistence, "write_snapshot", fail)
    try:
        store.snapshot_file(filename, {"reason": "authentication"})
        with pytest.raises(CheckpointUnavailable):
            await store.flush()
        with pytest.raises(CheckpointUnavailable):
            await invoke(provider, "stream")
        assert not raw.calls
        assert writer.failed
    finally:
        await finish(writer)


async def test_only_progress_snapshot_error_remains_best_effort(tmp_path, monkeypatch):
    from copilot_evals import live_answer_async_checkpoints as persistence

    writer, store, _budget, raw, _records, provider = await setup(tmp_path)
    real = persistence.write_snapshot

    def fail(path, payload, *, durable):
        if path.name == "progress.json":
            raise OSError("authored progress failure")
        return real(path, payload, durable=durable)

    monkeypatch.setattr(persistence, "write_snapshot", fail)
    try:
        with pytest.raises(ValueError, match="Only progress"):
            store.snapshot_file("admission-stop.json", {}, best_effort=True)
        store.snapshot_file("progress.json", {}, best_effort=True)
        await invoke(provider, "stream")
        await store.flush()
        assert raw.calls == ["stream"]
        assert len(writer.snapshot_errors) == 1
        assert not writer.failed
    finally:
        await finish(writer)


async def test_router_swallowed_attempt_observer_failure_cannot_dispatch(tmp_path):
    class Failed(RecordStore):
        def put(self, kind, key, value):
            if kind == "attempts":
                raise OSError("authored attempt failure")
            return super().put(kind, key, value)

    writer, store, _budget, raw, _records, provider = await setup(tmp_path, factory=Failed)
    config = configuration()

    def observer(event):
        store.put("attempts", f"{event.attempt_id}:{event.status}", {"status": event.status})

    try:
        result = await replay.trial(
            replay.load_cases()[0],
            "D",
            0,
            config,
            build_router(config, [provider], on_attempt=observer),
            5,
        )
        assert result["failure"]
        assert not raw.calls
        assert writer.failed
    finally:
        await finish(writer)


async def test_interrupted_durable_intent_does_not_claim_zero_provider_invocations(tmp_path):
    writer, _store, _budget, raw, records, provider = await setup(tmp_path)
    raw.hold = asyncio.Event()
    pending = asyncio.create_task(invoke(provider, "stream"))
    try:
        await raw.entered.wait()
        # This is the actual disk view at a possible process crash, before cancellation cleanup.
        saved = RecordStore(tmp_path)
        assert saved.read("requests")[0]["status"] == "started"
        assert saved.read("requests")[0]["provider_invoked"] is None
        assert saved.read("cost")[0]["status"] == "reserved"
        assert records[0]["provider_invoked"] is True
        crash = tmp_path / "crash-view"
        crash.mkdir()
        shutil.copytree(tmp_path / "checkpoint-records", crash / "checkpoint-records")
        restored = RecordDispatchJournal(RecordStore(crash), ["case:D:0"], set(), {"case:D:0"})
        assert restored.states["case:D:0"] == "ambiguous"
        assert not restored.start("case:D:0")
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await finish(writer)


async def test_progress_error_telemetry_is_bounded_and_counts_omissions(tmp_path, monkeypatch):
    from copilot_evals import live_answer_async_checkpoints as persistence

    writer = DurableWriter(tmp_path)
    await writer.ready()
    store = BufferedRecordStore(writer)

    def fail(*args, **kwargs):
        raise OSError("authored progress-only failure")

    monkeypatch.setattr(persistence, "write_snapshot", fail)
    try:
        for start in range(0, 1005, 64):
            for ordinal in range(start, min(start + 64, 1005)):
                store.snapshot_file("progress.json", {"ordinal": ordinal}, best_effort=True)
            await store.flush()
        assert not writer.failed
        assert len(writer.snapshot_errors) == 1000
        assert writer.snapshot()["omitted_snapshot_errors"] == 5
    finally:
        await finish(writer)


@pytest.mark.parametrize("operation", ["stream", "decide", "prewarm"])
@pytest.mark.parametrize("outer_gate", [False, True])
@pytest.mark.parametrize(
    "stop_reason", ["authentication", "external_stop", "zero_output_timeout_streak"]
)
async def test_gate_stop_during_durability_wait_prevents_new_raw_entry(
    tmp_path, operation, outer_gate, stop_reason
):
    entered, release = threading.Event(), threading.Event()

    class Delayed(RecordStore):
        def put(self, kind, key, value):
            if kind == "cost" and value["status"] == "reserved":
                entered.set()
                assert release.wait(4)
            return super().put(kind, key, value)

    writer, store, budget, raw, records, provider = await setup(tmp_path, factory=Delayed)
    gate = replay.ProviderAdmission(1)
    if outer_gate:
        # Session owns this admission outside its audit wrapper; never acquire it twice.
        provider.dispatch_guard = gate.check_open
        await gate.enter()
    else:
        provider.admission = gate
    try:
        pending = asyncio.create_task(invoke(provider, operation))
        await wait_thread(entered)
        gate.stop(stop_reason)
        release.set()
        with pytest.raises(replay.AdmissionBlocked):
            await pending
        await store.flush()
        assert not raw.calls
        assert records[0]["provider_invoked"] is False
        assert budget.entries[0]["status"] == "failed_before_submission"
        assert budget.total == 0
        assert RecordStore(tmp_path).read("requests")[0]["status"] == "AdmissionBlocked"
    finally:
        release.set()
        if outer_gate:
            gate.leave()
        await finish(writer)
    assert gate.active == 0


@pytest.mark.parametrize("kind", ["generation", "session"])
async def test_failed_final_drain_never_emits_success_compatibility_progress(
    tmp_path, monkeypatch, kind
):
    final_calls = []
    real_flush = BufferedRecordStore.flush

    async def failed_final(store):
        if store.phase == "drained":
            raise OSError("authored final flush failure")
        return await real_flush(store)

    async def forbidden_final(*args):
        final_calls.append(True)

    monkeypatch.setattr(BufferedRecordStore, "flush", failed_final)
    monkeypatch.setattr(replay.BufferedProgressSnapshots, "final", forbidden_final)
    if kind == "generation":
        monkeypatch.setattr(
            replay,
            "make_providers",
            lambda *a, **k: [ScriptedProvider("openai", lambda *a: Reply(["Done."]))],
        )
        monkeypatch.setattr(
            replay, "experiment_identity", lambda *a: {"code": {}, "test": "drain-generation"}
        )
        args = Namespace(
            architectures=["D"],
            reps=1,
            seed=1,
            concurrency=1,
            timeout=5,
            max_cost=None,
            skip_connectivity_check=True,
            max_provider_inflight=1,
        )
        run = replay.execute_live(
            args, replay.load_cases()[:1], configuration(), None, None, tmp_path
        )
    else:
        monkeypatch.setattr(
            session, "experiment_identity", lambda *a: {"code": {}, "test": "drain-session"}
        )
        monkeypatch.setattr(
            session, "make_providers", lambda *a, **k: pytest.fail("No live providers")
        )
        args = Namespace(
            out=tmp_path,
            live=False,
            architectures=["D"],
            scenarios=["duplicate-partial"],
            reps=1,
            cycles=1,
            concurrency=1,
            max_inflight=2,
            timeout=15,
            max_cost=None,
            seed=20261125,
            skip_connectivity_check=True,
        )
        run = session.execute(args)
    with pytest.raises(OSError, match="authored final flush"):
        await run
    assert not final_calls
    failure = json.loads((tmp_path / "finalization-failure.json").read_text())
    assert failure["status"] == "finalization_failed"
    assert failure["finalized"] is False
    if (tmp_path / "progress.json").exists():
        assert json.loads((tmp_path / "progress.json").read_text())["finalized"] is False
    assert not (tmp_path / "summary.json").exists()


async def test_cancelled_final_drain_keeps_lock_and_never_exports_success(tmp_path, monkeypatch):
    from copilot_evals.live_answer_artifacts import run_lock

    entered, release = threading.Event(), threading.Event()
    in_drain = asyncio.Event()

    class Held(RecordStore):
        def put(self, kind, key, value):
            if kind == "trials":
                entered.set()
                assert release.wait(5)
            return super().put(kind, key, value)

    monkeypatch.setattr(
        replay, "DurableWriter", lambda *a, **k: DurableWriter(*a, **k, store_factory=Held)
    )
    real_flush = BufferedRecordStore.flush

    async def observe_flush(store):
        if store.phase == "drained":
            in_drain.set()
        return await real_flush(store)

    monkeypatch.setattr(BufferedRecordStore, "flush", observe_flush)
    monkeypatch.setattr(
        replay,
        "make_providers",
        lambda *a, **k: [ScriptedProvider("openai", lambda *a: Reply(["Done."]))],
    )
    monkeypatch.setattr(
        replay, "experiment_identity", lambda *a: {"code": {}, "test": "blocked-drain"}
    )
    args = Namespace(
        architectures=["D"],
        reps=1,
        seed=1,
        concurrency=1,
        timeout=5,
        max_cost=None,
        skip_connectivity_check=True,
        max_provider_inflight=1,
    )
    pending = asyncio.create_task(
        replay.execute_live(args, replay.load_cases()[:1], configuration(), None, None, tmp_path)
    )
    try:
        await wait_thread(entered)
        await in_drain.wait()
        pending.cancel()
        await asyncio.sleep(0.02)
        assert not pending.done()
        with pytest.raises(ValueError, match="Another runner owns"), run_lock(tmp_path):
            pytest.fail("Run lock released while writer blocked")
        assert not (tmp_path / "trials.json").exists()
        assert (
            json.loads((tmp_path / "finalization-failure.json").read_text())["finalized"] is False
        )
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    with run_lock(tmp_path):
        pass
