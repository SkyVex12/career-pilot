"""Authored scoped-fence safety/anti-starvation tests; no external providers."""

import asyncio
import threading
from dataclasses import replace

import pytest
from copilot_evals import live_answer_replay as replay
from copilot_evals.live_answer_async_checkpoints import (
    ASYNC_POLICY,
    REQUEST_FENCE_POLICY,
    BufferedRecordStore,
    CheckpointUnavailable,
    DurableWriter,
    RequestDispatchPermit,
    StaleDurabilityBarrier,
)
from copilot_evals.live_answer_checkpoints import RecordStore

from evals.tests.test_live_answer_async_replay import configuration, finish, invoke, setup

pytestmark = pytest.mark.asyncio


async def signal(event):
    assert await asyncio.to_thread(event.wait, 5), "Authored synchronization timed out"


async def records(directory, factory=RecordStore):
    directory.mkdir(parents=True, exist_ok=True)
    writer = DurableWriter(directory, store_factory=factory)
    await writer.ready()
    store = BufferedRecordStore(writer)
    store.put("requests", "req", {"status": "started", "provider_invoked": None})
    store.put("cost", "0", {"status": "reserved"})
    return writer, store


@pytest.mark.parametrize("operation", ["stream", "decide", "prewarm"])
async def test_unrelated_pending_write_cannot_extend_own_dispatch(tmp_path, operation):
    cost_entered, cost_release = threading.Event(), threading.Event()
    other_entered, other_release = threading.Event(), threading.Event()

    class Controlled(RecordStore):
        def put(self, kind, key, value):
            if kind == "cost" and value["status"] == "reserved":
                cost_entered.set()
                assert cost_release.wait(5)
            if kind == "attempts" and key == "unrelated":
                other_entered.set()
                assert other_release.wait(5)
            return super().put(kind, key, value)

    writer, store, _budget, raw, requests, provider = await setup(tmp_path, factory=Controlled)
    pending = None
    try:
        pending = asyncio.create_task(invoke(provider, operation))
        await signal(cost_entered)
        store.put("attempts", "unrelated", {"other_request": True})
        cost_release.set()
        await signal(other_entered)
        await asyncio.wait_for(raw.entered.wait(), 3)
        assert not other_release.is_set()
        assert raw.calls == [operation]
        await pending
        assert requests[0]["durability_fence"]["policy"] == REQUEST_FENCE_POLICY
        assert writer.snapshot()["request_barriers"][0]["outcome"] == "ready"
        # Export/full-drain is still forbidden while the unrelated accepted write is pending.
        with pytest.raises(CheckpointUnavailable, match="durable drain"):
            writer.assert_drained()
    finally:
        cost_release.set()
        other_release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await finish(writer)


@pytest.mark.parametrize(("kind", "key"), [("requests", "req"), ("cost", "0")])
async def test_bound_keyupdate_during_wait_revokes_fence(tmp_path, kind, key):
    entered, release = threading.Event(), threading.Event()

    class Controlled(RecordStore):
        def put(self, kind, key, value):
            if kind == "cost" and value.get("status") == "reserved":
                entered.set()
                assert release.wait(5)
            return super().put(kind, key, value)

    writer, store = await records(tmp_path, Controlled)
    pending = None
    try:
        fence = writer.capture_request_dispatch("req", "0")
        pending = asyncio.create_task(writer.wait_request_dispatch(fence))
        await signal(entered)
        store.put(kind, key, {"status": "changed"})
        release.set()
        with pytest.raises(StaleDurabilityBarrier, match="version changed"):
            await pending
        await store.flush()
        permit = await writer.wait_request_dispatch(writer.capture_request_dispatch("req", "0"))
        writer.assert_request_dispatch_ready(permit)
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await finish(writer)


@pytest.mark.parametrize(("kind", "key"), [("requests", "req"), ("cost", "0")])
async def test_bound_keyupdate_after_wait_revokes_permit_immediately(tmp_path, kind, key):
    writer, store = await records(tmp_path)
    try:
        permit = await writer.wait_request_dispatch(writer.capture_request_dispatch("req", "0"))
        store.put(kind, key, {"status": "newer"})
        with pytest.raises(StaleDurabilityBarrier, match="version changed"):
            writer.assert_request_dispatch_ready(permit)
    finally:
        await finish(writer)


async def test_foreign_writer_and_foreign_receipt_rejected(tmp_path):
    writer, _store = await records(tmp_path / "first")
    other, _other_store = await records(tmp_path / "second")
    try:
        fence = writer.capture_request_dispatch("req", "0")
        foreign = other.capture_request_dispatch("req", "0")
        with pytest.raises(StaleDurabilityBarrier, match="another writer"):
            await writer.wait_request_dispatch(foreign)
        with pytest.raises(StaleDurabilityBarrier, match="version changed"):
            await writer.wait_request_dispatch(
                replace(fence, request_receipt=foreign.request_receipt)
            )
        with pytest.raises(StaleDurabilityBarrier, match="another writer"):
            writer.assert_request_dispatch_ready(RequestDispatchPermit(foreign))
    finally:
        await finish(writer)
        await finish(other)


async def test_missing_bindings_and_invalid_fifo_target_rejected(tmp_path):
    writer, _store = await records(tmp_path)
    try:
        with pytest.raises(StaleDurabilityBarrier, match="accepted writes"):
            writer.capture_request_dispatch("missing", "0")
        with pytest.raises(StaleDurabilityBarrier, match="accepted writes"):
            writer.capture_request_dispatch("req", "missing")
        with pytest.raises(StaleDurabilityBarrier, match="keys are required"):
            writer.capture_request_dispatch("req", "")
        fence = writer.capture_request_dispatch("req", "0")
        assert fence.target is fence.reservation_receipt
        with pytest.raises(StaleDurabilityBarrier, match="invalid FIFO target"):
            await writer.wait_request_dispatch(replace(fence, target=fence.request_receipt))
    finally:
        await finish(writer)


async def test_uncommitted_and_closed_permits_rejected(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Controlled(RecordStore):
        def put(self, kind, key, value):
            if kind == "cost":
                entered.set()
                assert release.wait(5)
            return super().put(kind, key, value)

    writer, _store = await records(tmp_path, Controlled)
    try:
        fence = writer.capture_request_dispatch("req", "0")
        await signal(entered)
        with pytest.raises(StaleDurabilityBarrier, match="not durably committed"):
            writer.assert_request_dispatch_ready(RequestDispatchPermit(fence))
        release.set()
        permit = await writer.wait_request_dispatch(fence)
        await writer.close()
        with pytest.raises(CheckpointUnavailable, match="closing"):
            writer.assert_request_dispatch_ready(permit)
        with pytest.raises(CheckpointUnavailable, match="closing"):
            writer.capture_request_dispatch("req", "0")
    finally:
        release.set()
        await finish(writer)


async def test_full_drain_guard_stays_global_and_strict(tmp_path):
    writer, store = await records(tmp_path)
    try:
        strict = await store.flush()
        scoped = await writer.wait_request_dispatch(writer.capture_request_dispatch("req", "0"))
        store.put("attempts", "later", {"unrelated": True})
        with pytest.raises(StaleDurabilityBarrier, match="New records"):
            writer.assert_dispatch_ready(strict)
        writer.assert_request_dispatch_ready(scoped)
        await store.flush()
        writer.assert_drained()
        assert ASYNC_POLICY == "atomic-records-async-request-fence-v2"
    finally:
        await finish(writer)


async def test_sticky_later_storage_failure_revokes_existing_permit(tmp_path):
    class Failed(RecordStore):
        def put(self, kind, key, value):
            if kind == "attempts":
                raise OSError("authored unrelated failure")
            return super().put(kind, key, value)

    writer, store = await records(tmp_path, Failed)
    try:
        permit = await writer.wait_request_dispatch(writer.capture_request_dispatch("req", "0"))
        store.put("attempts", "later", {})
        with pytest.raises(CheckpointUnavailable):
            await store.flush()
        with pytest.raises(CheckpointUnavailable):
            writer.assert_request_dispatch_ready(permit)
    finally:
        await finish(writer)


@pytest.mark.parametrize("operation", ["stream", "decide", "prewarm"])
async def test_budget_in_another_store_never_invokes_provider(tmp_path, operation):
    (tmp_path / "request").mkdir()
    (tmp_path / "cost").mkdir()
    writer, _store, _budget, raw, records_, provider = await setup(tmp_path / "request")
    other = DurableWriter(tmp_path / "cost")
    await other.ready()
    other_store = BufferedRecordStore(other)
    provider.budget = replay.AuditBudget(tmp_path / "cost", configuration(), 1, store=other_store)
    try:
        with pytest.raises(CheckpointUnavailable, match="share one durable store"):
            await invoke(provider, operation)
        assert raw.calls == []
        assert records_[0]["provider_invoked"] is False
    finally:
        await finish(writer)
        await finish(other)


async def test_malformed_binding_cannot_use_missing_keynone_equality(tmp_path):
    writer, _store = await records(tmp_path)
    try:
        fence = writer.capture_request_dispatch("req", "0")
        malformed = replace(fence, request_key="missing", request_receipt=None)
        with pytest.raises(StaleDurabilityBarrier, match="version changed"):
            await writer.wait_request_dispatch(malformed)
        with pytest.raises(StaleDurabilityBarrier, match="permit is required"):
            writer.assert_request_dispatch_ready(object())  # pyright: ignore[reportArgumentType] - Malformed caller probe.
    finally:
        await finish(writer)
