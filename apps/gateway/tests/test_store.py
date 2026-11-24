"""Retention backpressure, append acknowledgement, and distributed admission invariants."""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

from copilot_core import keys
from copilot_gateway.config import Settings
from copilot_gateway.store import GatewayStore, RetainedLogOverflowError, SessionWriter


class ControlledStore:
    """Let tests decide exactly when an append fails or acknowledges its captured batch."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: asyncio.Queue[tuple[list[tuple[int, str]], list[str]]] = asyncio.Queue()
        self.results: asyncio.Queue[str | Exception] = asyncio.Queue()

    async def append(
        self,
        sid: UUID,
        task_id: str,
        events: list[tuple[int, str]],
        log_entries: list[str],
        state: Mapping[str, str],
        *,
        batch_id: str | None = None,
    ) -> str:
        self.calls.put_nowait((list(events), list(log_entries)))
        result = await self.results.get()
        if isinstance(result, Exception):
            raise result
        return result


def writer_for(store: ControlledStore, **options: Any) -> SessionWriter:
    return SessionWriter(cast(GatewayStore, store), uuid4(), "task-a", lambda: None, **options)


def serialized(entry: Mapping[str, Any]) -> str:
    return json.dumps(entry, separators=(",", ":"), default=str)


def test_retained_entry_limit_preserves_accepted_history_and_fails_once(settings_factory) -> None:
    store = ControlledStore(settings_factory(retained_log_max_entries=2))
    failures: list[RetainedLogOverflowError] = []
    writer = writer_for(store, on_overflow=failures.append)
    entries = [{"kind": "transcript.final", "text": value} for value in ("one", "two")]
    for entry in entries:
        writer.log_entry(entry)

    with pytest.raises(RetainedLogOverflowError) as raised:
        writer.log_entry({"kind": "answer.done", "text": "three"})
    with pytest.raises(RetainedLogOverflowError) as repeated:
        writer.log_entry({"kind": "session.ended"})

    assert list(writer.log) == [serialized(entry) for entry in entries]
    assert writer.retained_log_bytes == sum(len(value.encode()) for value in writer.log)
    assert failures == [raised.value]
    assert repeated.value is raised.value
    assert writer.retention_error is raised.value


def test_retained_byte_limit_counts_serialized_payload_at_exact_boundary(settings_factory) -> None:
    entry = {"kind": "transcript.final", "text": 'こんにちは "quoted"'}
    value = serialized(entry)
    size = len(value.encode("utf-8"))
    store = ControlledStore(settings_factory(retained_log_max_bytes=size))
    writer = writer_for(store)
    writer.log_entry(entry)
    assert writer.retained_log_bytes == size
    with pytest.raises(RetainedLogOverflowError):
        writer.log_entry({})
    assert list(writer.log) == [value]

    smaller = writer_for(ControlledStore(settings_factory(retained_log_max_bytes=size - 1)))
    with pytest.raises(RetainedLogOverflowError):
        smaller.log_entry(entry)
    assert list(smaller.log) == []
    assert smaller.retained_log_bytes == 0


async def test_retry_keeps_retention_budget_until_acknowledged_and_drain_recovers(
    settings_factory,
) -> None:
    store = ControlledStore(settings_factory(retained_log_max_entries=2))
    availability: list[bool] = []
    writer = writer_for(store, on_availability=availability.append)
    writer.log_entry({"text": "first"})
    runner = asyncio.create_task(writer.run())
    try:
        _, first_batch = await asyncio.wait_for(store.calls.get(), 1)
        first_size = writer.retained_log_bytes
        assert first_size == len(first_batch[0].encode())
        store.results.put_nowait(ConnectionError("offline"))
        _, retry_batch = await asyncio.wait_for(store.calls.get(), 1)
        assert retry_batch == first_batch
        assert writer.retained_log_bytes == first_size
        assert not await writer.drain(within_s=0.01)

        # An entry arriving during the retry is still pending after that batch succeeds.
        writer.log_entry({"text": "second"})
        both_size = writer.retained_log_bytes
        store.results.put_nowait("1-0")
        _, next_batch = await asyncio.wait_for(store.calls.get(), 1)
        assert next_batch == [serialized({"text": "second"})]
        assert writer.retained_log_bytes == both_size - first_size
        assert not await writer.drain(within_s=0.01)

        store.results.put_nowait("2-0")
        assert await writer.drain(within_s=1)
        assert not writer.pending()
        assert writer.retained_log_bytes == 0
        assert writer.last_log_id == "2-0"
        assert availability == [False, True]
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


async def test_inflight_entries_count_toward_limit_and_overflow_never_reports_saved(
    settings_factory,
) -> None:
    store = ControlledStore(settings_factory(retained_log_max_entries=1))
    failures: list[RetainedLogOverflowError] = []
    writer = writer_for(store, on_overflow=failures.append)
    writer.log_entry({"text": "accepted"})
    runner = asyncio.create_task(writer.run())
    try:
        _, batch = await asyncio.wait_for(store.calls.get(), 1)
        with pytest.raises(RetainedLogOverflowError):
            writer.log_entry({"text": "cannot accept while append is in flight"})
        assert list(writer.log) == batch
        assert len(failures) == 1

        store.results.put_nowait("1-0")
        assert not await writer.drain(within_s=1)
        assert not writer.pending()
        assert writer.retained_log_bytes == 0
        assert writer.last_log_id == "1-0"
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


async def test_event_eviction_during_append_preserves_newer_unacknowledged_events(
    settings_factory,
) -> None:
    store = ControlledStore(settings_factory())
    writer = writer_for(store, buffer_max=2)
    writer.event(1, "one")
    writer.event(2, "two")
    runner = asyncio.create_task(writer.run())
    try:
        first, _ = await asyncio.wait_for(store.calls.get(), 1)
        assert first == [(1, "one"), (2, "two")]
        writer.event(3, "three")
        writer.event(4, "four")
        store.results.put_nowait("")
        next_events, _ = await asyncio.wait_for(store.calls.get(), 1)
        assert next_events == [(3, "three"), (4, "four")]
        store.results.put_nowait("")
        assert await writer.drain(within_s=1)
        assert not writer.pending()
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


async def test_concurrent_active_claims_have_one_owner_and_failed_replace_preserves_it(env) -> None:
    store = env.services().store
    first, second, replacement = uuid4(), uuid4(), uuid4()
    try:
        claims = await asyncio.gather(
            store.claim_active(env.user_a, first), store.claim_active(env.user_a, second)
        )
        assert claims.count(None) == 1
        winner = first if claims[0] is None else second
        loser = second if winner == first else first
        assert claims[1 if claims[0] is None else 0] == str(winner)
        assert not await store.replace_active(env.user_a, str(loser), replacement)
        assert await store.client.get(keys.user_active_session(env.user_a)) == str(winner).encode()
        assert await store.replace_active(env.user_a, str(winner), replacement)
        assert not await store.refresh_active(env.user_a, winner)
        assert await store.refresh_active(env.user_a, replacement)
        assert await store.claim_active(env.user_a, first) == str(replacement)
        assert await store.client.ttl(keys.user_active_session(env.user_a)) > 0
    finally:
        await store.client.aclose()


@pytest.mark.parametrize("status", ["active", "detached", "ended", None])
async def test_resume_lease_requires_existing_unended_state(env, status: str | None) -> None:
    store = env.services().store
    sid = uuid4()
    try:
        await store.take_lease(sid, "old-task")
        if status is not None:
            await store.client.hset(keys.sess_state(sid), mapping={"status": status})
        allowed = status not in ("ended", None)
        assert await store.take_resume_lease(sid, "new-task") is allowed
        assert await store.lease_holder(sid) == ("new-task" if allowed else "old-task")
        assert await store.client.pttl(keys.sess_lease(sid)) > 0
        if not allowed:
            await store.release_lease(sid, "old-task")
            assert not await store.take_resume_lease(sid, "new-task")
            assert await store.lease_holder(sid) is None
    finally:
        await store.client.aclose()


@pytest.mark.parametrize("status", ["active", "detached", "ended", None])
async def test_expired_lease_renewal_cannot_recreate_ended_or_deleted_session(
    env, status: str | None
) -> None:
    store = env.services().store
    sid = uuid4()
    try:
        # Initial admission may renew its current lease before its first state flush.
        await store.take_lease(sid, "task-a")
        assert await store.renew_lease(sid, "task-a")
        if status is not None:
            await store.client.hset(keys.sess_state(sid), mapping={"status": status})
        # Simulate the missing key after an outage/expiry deterministically; the
        # real-Valkey integration test exercises PEXPIRE itself.
        await store.client.delete(keys.sess_lease(sid))
        assert await store.lease_holder(sid) is None
        allowed = status not in ("ended", None)
        assert await store.renew_lease(sid, "task-a") is allowed
        assert await store.lease_holder(sid) == ("task-a" if allowed else None)
        if allowed:
            assert await store.client.pttl(keys.sess_lease(sid)) > 0
    finally:
        await store.client.aclose()


async def test_lost_append_response_retries_same_batch_without_duplicate_events_or_logs(
    env,
    monkeypatch,
) -> None:
    store = env.services().store
    sid = uuid4()
    await store.take_lease(sid, "task-a")
    append = store._append
    calls = 0
    failed_response = asyncio.Event()

    async def commit_then_timeout(**kwargs):
        nonlocal calls
        result = await append(**kwargs)
        calls += 1
        if calls == 1:
            raise ValkeyTimeoutError("the commit succeeded but its response was lost")
        return result

    monkeypatch.setattr(store, "_append", commit_then_timeout)
    writer = SessionWriter(
        store,
        sid,
        "task-a",
        lambda: None,
        on_availability=lambda available: failed_response.set() if not available else None,
    )
    writer.event(1, "first")
    writer.log_entry({"kind": "usage.interval", "event_id": "one"})
    writer.set_state(marker="first")
    runner = asyncio.create_task(writer.run())
    try:
        await asyncio.wait_for(failed_response.wait(), 1)
        # New output arrives before the old batch's receipt can be recovered.
        writer.event(2, "second")
        writer.log_entry({"kind": "usage.interval", "event_id": "two"})
        writer.set_state(marker="second")
        assert await writer.drain(within_s=2)
        assert calls == 3  # committed batch, its retry, then the queued second batch
        assert await store.events_after(sid, 0) == [(1, "first"), (2, "second")]
        entries = await store.client.xrange(keys.sess_log(sid))
        assert [json.loads(fields[b"e"])["event_id"] for _, fields in entries] == ["one", "two"]
        assert writer.last_log_id == entries[-1][0].decode()
        assert (await store.load_state(sid))["marker"] == "second"
        assert writer.retained_log_bytes == 0
        assert not runner.done()
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        await store.client.aclose()
