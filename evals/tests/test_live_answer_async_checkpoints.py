"""No providers: filesystem, cancellation, ownership and authored dispatch probes only."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from copilot_evals.live_answer_artifacts import run_lock
from copilot_evals.live_answer_async_checkpoints import (
    AnchoredLoopLagMonitor,
    CheckpointDrainTimeout,
    CheckpointUnavailable,
    DurableWriter,
    StaleDurabilityBarrier,
)
from copilot_evals.live_answer_checkpoints import RecordDispatchJournal, RecordStore


class GatedStore(RecordStore):
    def __init__(self, directory, entered, release, failed_kind=None):
        super().__init__(directory)
        self.entered, self.release, self.failed_kind = entered, release, failed_kind

    def put(self, kind, key, value):
        self.entered.set()
        if not self.release.wait(5):
            raise OSError("Authored test write did not release")
        if kind == self.failed_kind:
            raise OSError("Authored disk failure")
        return super().put(kind, key, value)


async def wait_thread(event):
    deadline = time.perf_counter() + 3
    while not event.is_set():
        assert time.perf_counter() < deadline
        await asyncio.sleep(0.001)


def lock_is_owned(directory):
    with pytest.raises(ValueError, match="Another runner owns"), run_lock(directory):
        pytest.fail("Writer released its lock prematurely")


async def finish_failed(writer):
    with pytest.raises(CheckpointUnavailable):
        await writer.close(timeout_s=3)
    assert not writer.lock_owned


@pytest.mark.parametrize("failure_stage", ["guard", "store"])
async def test_close_preserves_startup_error_after_releasing_ownership(tmp_path, failure_stage):
    original = ValueError("Authored invalid checkpoint identity or record")

    def fail_startup(directory):
        raise original

    writer = DurableWriter(
        tmp_path,
        initialization_guard=fail_startup if failure_stage == "guard" else None,
        store_factory=fail_startup if failure_stage == "store" else RecordStore,
    )
    with pytest.raises(ValueError, match="Authored invalid checkpoint") as ready_failure:
        await writer.ready()
    with pytest.raises(ValueError, match="Authored invalid checkpoint") as close_failure:
        await writer.close(exports=(("requests", "requests.json"),))
    assert ready_failure.value is close_failure.value is original
    assert not writer.thread.is_alive()
    assert not writer.lock_owned
    assert not (tmp_path / "requests.json").exists()
    with run_lock(tmp_path):
        pass


async def test_fifo_immutable_replacement_and_export_after_drain(tmp_path):
    writer = DurableWriter(tmp_path)
    await writer.ready()
    source = {"values": ["original"]}
    first = writer.submit("requests", "a", source)
    source["values"][0] = "mutated"
    writer.submit("requests", "b", {"values": ["second"]})
    writer.submit("requests", "a", {"values": ["final"]})
    permit = await writer.barrier()
    assert first.future.done()
    writer.assert_dispatch_ready(permit)
    telemetry = await writer.close(exports=(("requests", "request-identities.json"),))
    assert json.loads((tmp_path / "request-identities.json").read_text()) == [
        {"values": ["final"]},
        {"values": ["second"]},
    ]
    assert [r["sequence"] for r in telemetry["writes"]] == [1, 2, 3]
    assert not writer.thread.is_alive()
    with run_lock(tmp_path):
        pass


async def test_cancelled_waiter_does_not_cancel_snapshot_or_enable_dispatch(tmp_path):
    entered, release = threading.Event(), threading.Event()
    writer = DurableWriter(tmp_path, store_factory=lambda p: GatedStore(p, entered, release))
    await writer.ready()
    value = {"text": ["original"]}
    receipt = writer.submit("requests", "a", value)
    value["text"][0] = "changed"
    waiter = asyncio.create_task(writer.barrier())
    await wait_thread(entered)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not receipt.future.cancelled()
    lock_is_owned(tmp_path)
    release.set()
    await writer.barrier()
    await writer.close()
    assert RecordStore(tmp_path).read("requests") == [{"text": ["original"]}]


async def test_new_write_rejects_a_previously_valid_dispatch_permit(tmp_path):
    writer = DurableWriter(tmp_path)
    await writer.ready()
    writer.submit("requests", "a", {"status": "started"})
    prior = await writer.barrier()
    writer.submit("cost", "0", {"status": "reserved"})
    with pytest.raises(StaleDurabilityBarrier):
        writer.assert_dispatch_ready(prior)
    writer.assert_dispatch_ready(await writer.barrier())
    await writer.close()


async def test_barrier_refreshes_for_writes_submitted_while_it_waited(tmp_path):
    entered, release = threading.Event(), threading.Event()
    writer = DurableWriter(tmp_path, store_factory=lambda p: GatedStore(p, entered, release))
    await writer.ready()
    writer.submit("requests", "a", {"status": "started"})
    waiter = asyncio.create_task(writer.barrier())
    await wait_thread(entered)
    last = writer.submit("cost", "0", {"status": "reserved"})
    release.set()
    permit = await waiter
    assert permit.sequence == last.sequence
    writer.assert_dispatch_ready(permit)
    await writer.close()


@pytest.mark.parametrize("kind", ["dispatch", "requests", "cost", "attempts"])
async def test_any_failed_intent_prevents_authored_paid_dispatch(tmp_path, kind):
    entered, release = threading.Event(), threading.Event()
    release.set()
    notices = []

    def swallowed(reason):
        notices.append(reason)
        raise RuntimeError("Observer failure must not bypass sticky gate")

    writer = DurableWriter(
        tmp_path,
        on_failure=swallowed,
        store_factory=lambda p: GatedStore(p, entered, release, kind),
    )
    await writer.ready()
    raw_calls = []
    for name in ("dispatch", "requests", "cost", "attempts"):
        try:
            writer.submit(name, name, {"status": "in_flight" if name == "dispatch" else "reserved"})
        except CheckpointUnavailable:
            break

    async def invoke():
        permit = await writer.barrier()
        writer.assert_dispatch_ready(permit)
        raw_calls.append("billed")

    with pytest.raises(CheckpointUnavailable):
        await invoke()
    assert not raw_calls
    with pytest.raises(CheckpointUnavailable):
        writer.submit("requests", "later", {})
    await finish_failed(writer)
    assert notices


async def test_queue_full_is_sticky_local_failure_and_never_silently_drops(tmp_path):
    entered, release = threading.Event(), threading.Event()
    writer = DurableWriter(
        tmp_path, capacity=1, store_factory=lambda p: GatedStore(p, entered, release)
    )
    await writer.ready()
    receipt = writer.submit("requests", "a", {"status": "started"})
    await wait_thread(entered)
    with pytest.raises(CheckpointUnavailable, match="checkpoint_queue_full"):
        writer.submit("requests", "b", {"status": "started"})
    with pytest.raises(CheckpointUnavailable, match="checkpoint_queue_full"):
        await writer.barrier()
    release.set()
    await finish_failed(writer)
    assert receipt.future.done()
    assert not receipt.future.cancelled()
    assert writer._reason == "checkpoint_queue_full"


async def test_byte_bound_rejects_oversize_before_acceptance(tmp_path):
    writer = DurableWriter(tmp_path, byte_capacity=16)
    await writer.ready()
    with pytest.raises(CheckpointUnavailable, match="checkpoint_queue_full"):
        writer.submit("requests", "a", {"long": "x" * 100})
    assert writer._last is None
    await finish_failed(writer)


async def test_shutdown_timeout_retains_lock_forbids_exports_and_new_starts(tmp_path):
    entered, release = threading.Event(), threading.Event()
    writer = DurableWriter(tmp_path, store_factory=lambda p: GatedStore(p, entered, release))
    await writer.ready()
    writer.submit("requests", "a", {"status": "started"})
    await wait_thread(entered)
    with pytest.raises(CheckpointDrainTimeout):
        await writer.close(timeout_s=0.02, exports=(("requests", "requests.json"),))
    assert writer.thread.is_alive()
    assert writer.lock_owned
    lock_is_owned(tmp_path)
    assert not (tmp_path / "requests.json").exists()
    with pytest.raises(CheckpointUnavailable, match="checkpoint_shutdown_timeout"):
        writer.submit("requests", "late", {})
    release.set()
    await finish_failed(writer)
    assert not writer.thread.is_alive()
    assert not (tmp_path / "requests.json").exists()
    with run_lock(tmp_path):
        pass


async def test_cancelled_close_keeps_ownership_until_accepted_writes_finish(tmp_path):
    entered, release = threading.Event(), threading.Event()
    writer = DurableWriter(tmp_path, store_factory=lambda p: GatedStore(p, entered, release))
    await writer.ready()
    writer.submit("requests", "a", {"status": "started"})
    await wait_thread(entered)
    closer = asyncio.create_task(writer.close())
    await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    lock_is_owned(tmp_path)
    release.set()
    await writer.close()
    assert not writer.thread.is_alive()


async def test_durable_start_without_result_is_ambiguous_after_restart(tmp_path):
    writer = DurableWriter(tmp_path)
    await writer.ready()
    writer.submit("dispatch-plan", "planned", ["case:D:0"])
    writer.submit("dispatch", "case:D:0", {"id": "case:D:0", "state": "in_flight"})
    writer.submit("requests", "request", {"trial_id": "case:D:0", "status": "started"})
    writer.submit("cost", "0", {"status": "reserved", "trial_id": "case:D:0", "charged_usd": 0.1})
    writer.assert_dispatch_ready(await writer.barrier())
    await writer.close()
    restored = RecordDispatchJournal(RecordStore(tmp_path), ["case:D:0"], set(), {"case:D:0"})
    assert restored.states["case:D:0"] == "ambiguous"
    assert not restored.start("case:D:0")


async def test_slow_fsync_path_does_not_block_unrelated_stream_ticks(tmp_path):
    entered, release = threading.Event(), threading.Event()
    writer = DurableWriter(tmp_path, store_factory=lambda p: GatedStore(p, entered, release))
    await writer.ready()
    writer.submit("cost", "0", {"status": "reserved"})
    await wait_thread(entered)
    progress = []
    for _ in range(10):
        progress.append(time.perf_counter())
        await asyncio.sleep(0.005)
    assert len(progress) == 10
    assert writer._last is not None
    assert not writer._last.future.done()
    release.set()
    result = await writer.close()
    assert result["writes"][0]["worker_ms"] >= 40
    assert result["anchor"]["utc"]


def test_lag_spikes_are_timestamped_bounded_and_not_subtracted():
    monitor = AnchoredLoopLagMonitor(max_spikes=2)
    for value in (10, 60, 70, 80):
        monitor.observe(value)
    summary = monitor.summary()
    assert summary["samples"] == 4
    assert summary["max_ms"] == 80
    assert len(summary["spikes"]) == 2
    assert summary["omitted_spikes"] == 1
    assert all("observed_at_s" in row for row in summary["spikes"])
    assert summary["anchor"]["utc"]
