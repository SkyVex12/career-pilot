"""Accepted work survives process/queue failures; expired generations cannot commit."""

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from copilot_core import jobs, keys, operations
from copilot_core.db import create_pool, user_tx
from copilot_core.teststack import as_role
from copilot_worker.runtime import Consumer, JobRuns, Message, SqsQueue
from copilot_worker.testing import Stack

pytestmark = pytest.mark.stack


async def test_domain_and_outbox_roll_back_together(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:rollback:{uuid4()}"

    async def interrupted_commit() -> None:
        async with stack.ctx.user(uid) as conn:
            await conn.execute(
                "update app.users set settings=$2 where id=$1", uid, {"changed": True}
            )
            await operations.enqueue(conn, jobs.envelope("digest.rebuild", key, uid, {}))
            raise RuntimeError("crash before commit")

    with pytest.raises(RuntimeError, match="crash before commit"):
        await interrupted_commit()
    assert not await stack.admin.fetchval(
        "select 1 from app.operations where idempotency_key=$1", key
    )
    assert not await stack.admin.fetchval(
        "select 1 from app.outbox_events where operation_key=$1", key
    )
    assert await stack.admin.fetchval("select settings from app.users where id=$1", uid) == "{}"


async def test_dispatcher_restart_retries_ambiguous_send_with_same_event_id(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:outbox:{uuid4()}"
    async with stack.ctx.user(uid) as conn:
        await operations.enqueue(conn, jobs.envelope("digest.rebuild", key, uid, {}))
    delivered: list[dict[str, Any]] = []

    class AmbiguousQueue:
        async def send(self, message, delay_s=0):
            delivered.append(dict(message))
            if len(delivered) == 1:
                raise ConnectionError("accepted by queue but acknowledgement lost")

    queue = AmbiguousQueue()
    first = operations.OutboxDispatcher(stack.ctx.system_pool, queue)
    assert await first.once() == 0
    row = await stack.admin.fetchrow(
        "select published_at,last_error from app.outbox_events where operation_key=$1", key
    )
    assert row is not None
    assert row["published_at"] is None
    assert row["last_error"]
    await stack.admin.execute(
        "update app.outbox_events set available_at=now() where operation_key=$1", key
    )
    restarted = operations.OutboxDispatcher(stack.ctx.system_pool, queue)
    assert await restarted.once() == 1
    assert len(delivered) == 2
    assert delivered[0]["event_id"] == delivered[1]["event_id"]
    assert await stack.admin.fetchval(
        "select published_at is not null from app.outbox_events where operation_key=$1", key
    )


async def test_published_but_lost_delivery_is_recovered(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:recover:{uuid4()}"
    async with stack.ctx.user(uid) as conn:
        await operations.enqueue(conn, jobs.envelope("digest.rebuild", key, uid, {}))
    queue = jobs.MemoryJobQueue()
    dispatch = operations.OutboxDispatcher(stack.ctx.system_pool, queue)
    await dispatch.once()
    original = next(m for m in queue.sent if m["idempotency_key"] == key)
    await stack.admin.execute(
        "update app.outbox_events set published_at=now()-interval '1 hour' where operation_key=$1",
        key,
    )
    assert await dispatch.recover() >= 1
    await dispatch.once()
    again = [m for m in queue.sent if m["idempotency_key"] == key]
    assert len(again) == 2
    assert again[-1]["event_id"] == original["event_id"]


async def test_replacement_generation_rejects_stale_results_and_status(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:fence:{uuid4()}"
    async with stack.ctx.user(uid) as conn:
        await operations.enqueue(conn, jobs.envelope("digest.rebuild", key, uid, {}))
        old = await operations.claim(conn, key, uuid4(), 30)
    assert isinstance(old, operations.Lease)
    await stack.admin.execute(
        "update app.operations set lease_expires_at=now()-interval '1 second'"
        " where idempotency_key=$1",
        key,
    )
    async with stack.ctx.user(uid) as conn:
        new = await operations.claim(conn, key, uuid4(), 30)
    assert isinstance(new, operations.Lease)
    assert new.generation == old.generation + 1
    token = operations.current_lease.set(old)
    try:
        with pytest.raises(operations.LeaseLost):
            async with stack.ctx.user(uid) as conn:
                await conn.execute(
                    "update app.users set settings=$2 where id=$1", uid, {"stale": True}
                )
        with pytest.raises(operations.LeaseLost):
            async with stack.ctx.cache_commit(uid):
                await stack.ctx.valkey.set(keys.digest(uid), "stale")
    finally:
        operations.current_lease.reset(token)
    assert await stack.admin.fetchval("select settings from app.users where id=$1", uid) == "{}"
    assert await stack.ctx.valkey.get(keys.digest(uid)) is None
    async with stack.ctx.system() as conn:
        assert not await operations.renew(conn, old, 30)
        assert not await operations.finish(conn, old)
        assert not await operations.fail(conn, old, "old error")
        assert await operations.finish(conn, new)


async def test_heartbeat_extends_durable_lease_independent_of_original_start(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:heartbeat:{uuid4()}"
    async with stack.ctx.user(uid) as conn:
        await operations.enqueue(conn, jobs.envelope("digest.rebuild", key, uid, {}))
        lease = await operations.claim(conn, key, uuid4(), 30)
    assert isinstance(lease, operations.Lease)
    await stack.admin.execute(
        "update app.operations set started_at=now()-interval '1 day' where idempotency_key=$1", key
    )
    async with stack.ctx.system() as conn:
        assert await operations.renew(conn, lease, 30)
        assert await operations.claim(conn, key, uuid4(), 30) == "busy"
        assert await operations.finish(conn, lease)


async def test_api_claims_only_local_interactive_work(stack: Stack) -> None:
    uid = await stack.new_user()
    api_pool = await create_pool(as_role(stack.url, "app_api"), 1, 1)
    key = f"ask.generate:local:{uuid4()}"
    try:
        async with user_tx(api_pool, uid) as conn:
            await operations.enqueue(
                conn,
                jobs.envelope("ask.generate", key, uid, {"turn_id": str(uuid4())}),
                account_id=uid,
                transport="local",
            )
        async with api_pool.acquire() as conn, conn.transaction():
            accepted = await operations.claim_next(conn, uuid4(), 30)
            assert accepted is not None
            message, lease = accepted
            assert message["idempotency_key"] == key
            await operations.guard(conn, lease)
            assert await operations.renew(conn, lease, 30)
            assert await operations.finish(conn, lease)
        assert not await stack.admin.fetchval(
            "select 1 from app.outbox_events where operation_key=$1", key
        )
    finally:
        await api_pool.close()


async def test_persistence_budget_survives_saturated_expensive_work(stack: Stack) -> None:
    class Queue(SqsQueue):
        def __init__(self):
            self.visibility = []
            self.deleted = []

        async def set_visibility(self, receipt, seconds):
            self.visibility.append((receipt, seconds))

        async def delete(self, receipt):
            self.deleted.append(receipt)

    started, release = asyncio.Event(), asyncio.Event()

    async def expensive(job, ctx):
        started.set()
        await release.wait()

    async def persist(job, ctx):
        return

    queue = Queue()
    consumer = Consumer(
        queue,
        JobRuns(stack.ctx.pool),
        stack.ctx,
        concurrency=2,
        persistence_concurrency=1,
        handlers={"digest.rebuild": expensive, "session.flush": persist},
    )

    def message(kind, receipt):
        return Message(json.dumps(jobs.envelope(kind, f"{kind}:{uuid4()}", None, {})), receipt)

    first = asyncio.create_task(consumer.process(message("digest.rebuild", "first")))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await consumer.process(message("digest.rebuild", "second"))
        await consumer.process(message("session.flush", "persistence"))
        assert ("second", 5) in queue.visibility
        assert "persistence" in queue.deleted
        assert "first" not in queue.deleted
    finally:
        release.set()
        await first


async def test_failed_legacy_delivery_can_be_explicitly_retried_durably(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:legacy:{uuid4()}"
    message = jobs.envelope("digest.rebuild", key, uid, {})
    async with stack.ctx.system() as conn:
        await operations.adopt(conn, message)
        lease = await operations.claim(conn, key, uuid4(), 30)
        assert isinstance(lease, operations.Lease)
        assert await operations.fail(conn, lease, "old queue failure")
        await operations.enqueue(conn, message, account_id=uid)
    row = await stack.admin.fetchrow(
        "select status,account_id from app.operations where idempotency_key=$1", key
    )
    assert row is not None
    assert row["status"] == "queued"
    assert row["account_id"] is None  # preserve the accepted creator attribution
    assert await stack.admin.fetchval(
        "select published_at is null from app.outbox_events where operation_key=$1", key
    )


async def test_accepted_key_rejects_new_input_and_preserves_attribution(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:immutable:{uuid4()}"
    message = jobs.envelope("digest.rebuild", key, uid, {"resume_version_id": str(uuid4())})
    async with stack.ctx.system() as conn:
        first = await operations.enqueue(conn, message, account_id=uid)
        assert await operations.enqueue(conn, message) == first
    bad = {**message, "payload": {"resume_version_id": str(uuid4())}}
    async with stack.ctx.system() as conn:
        with pytest.raises(ValueError, match="different operation input"):
            await operations.enqueue(conn, bad)
        with pytest.raises(ValueError, match="conflicts with accepted"):
            await operations.adopt(conn, bad)


async def test_flush_metadata_retention_is_bounded_and_preserves_pending_work(stack: Stack) -> None:
    uid = await stack.new_user()
    candidates = {}
    async with stack.ctx.system() as conn:
        for kind, label, state, age in (
            ("session.flush", "old", "done", 31),
            ("session.flush", "recent", "done", 29),
            ("session.flush", "pending", "queued", 31),
            ("session.ended", "audit", "done", 31),
        ):
            key = f"{kind}:{label}:{uuid4()}"
            candidates[label] = key
            await operations.enqueue(conn, jobs.envelope(kind, key, uid, {}))
            await conn.execute(
                "update app.operations set status=$2,finished_at=now()-make_interval(days=>$3)"
                " where idempotency_key=$1",
                key,
                state,
                age,
            )
        assert await operations.purge_terminal_flushes(conn) == 1
    assert not await stack.admin.fetchval(
        "select 1 from app.outbox_events where operation_key=$1", candidates["old"]
    )
    for label in ("recent", "pending", "audit"):
        assert await stack.admin.fetchval(
            "select 1 from app.operations where idempotency_key=$1", candidates[label]
        )


async def test_abandoned_admission_uses_durable_expiry_without_cache(stack: Stack) -> None:
    uid = await stack.new_user()
    sessions = [uuid4(), uuid4(), uuid4()]
    for sid, generation, expiry in zip(sessions, (0, 2, 3), (None, -600, 600), strict=True):
        await stack.admin.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at,metering_version,"
            " admission_state,writer_generation,writer_expires_at,payer_account_id,"
            " billing_price_version,billing_currency,billing_rate_minor_per_hour)"
            " values($1,$2,'interview',false,now()-interval '10 minutes',"
            " 'connected-v1','active',$3,"
            " case when $4::int is null then null else now()+make_interval(secs=>$4) end,"
            " $2,'live-usd-v1','USD',500)",
            sid,
            uid,
            generation,
            expiry,
        )
    await stack.run(jobs.envelope("sessions.sweep", f"sweep:{uuid4()}", None, {}))
    accepted = [m for m in stack.take("session.ended") if m.get("user_id") == str(uid)]
    assert {m["payload"]["session_id"] for m in accepted} == {str(s) for s in sessions[:2]}
    assert {m["payload"]["writer_generation"] for m in accepted} == {0, 2}
    for message in accepted:
        assert (
            await stack.admin.fetchval(
                "select admission_state from app.sessions where id=$1",
                UUID(message["payload"]["session_id"]),
            )
            == "closing"
        )
        await stack.run(message)
    assert (
        await stack.admin.fetchval(
            "select admission_state from app.sessions where id=$1", sessions[2]
        )
        == "active"
    )
    assert await stack.admin.fetchval(
        "select ended_at=started_at from app.sessions where id=$1", sessions[1]
    )


async def test_heartbeat_cancels_work_after_database_lease_expiry(stack: Stack) -> None:
    started, stopped = asyncio.Event(), asyncio.Event()

    class Queue(SqsQueue):
        def __init__(self):
            self.deleted = []

        async def set_visibility(self, receipt, seconds):
            return

        async def delete(self, receipt):
            self.deleted.append(receipt)

    async def held(job, ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    key = f"digest.rebuild:lost:{uuid4()}"
    queue = Queue()
    consumer = Consumer(
        queue,
        JobRuns(stack.ctx.pool),
        stack.ctx,
        heartbeat_s=0.02,
        handlers={"digest.rebuild": held},
    )
    message = Message(json.dumps(jobs.envelope("digest.rebuild", key, None, {})), "lost")
    task = asyncio.create_task(consumer.process(message))
    await asyncio.wait_for(started.wait(), 2)
    await stack.admin.execute(
        "update app.operations set lease_expires_at=now()-interval '1 second'"
        " where idempotency_key=$1",
        key,
    )
    await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    assert queue.deleted == []
    assert (
        await stack.admin.fetchval(
            "select status from app.operations where idempotency_key=$1", key
        )
        == "running"
    )  # recoverable, never falsely acknowledged as complete


async def test_expensive_failure_limit_requires_explicit_retry(stack: Stack) -> None:
    uid = await stack.new_user()
    key = f"digest.rebuild:dead:{uuid4()}"
    message = jobs.envelope("digest.rebuild", key, uid, {})
    async with stack.ctx.system() as conn:
        await operations.enqueue(conn, message)
        last_generation = 0
        for _ in range(5):
            lease = await operations.claim(conn, key, uuid4(), 30)
            assert isinstance(lease, operations.Lease)
            last_generation = lease.generation
            assert await operations.fail(conn, lease, "provider rejected output")
        assert await operations.claim(conn, key, uuid4(), 30) == "dead"
        await operations.enqueue(conn, message)
        assert await operations.claim(conn, key, uuid4(), 30) == "dead"
        assert await operations.retry(conn, key)
        again = await operations.claim(conn, key, uuid4(), 30)
        assert isinstance(again, operations.Lease)
        assert again.generation > last_generation
        assert await operations.finish(conn, again)


async def test_persistence_failure_remains_recoverable_after_five_attempts(stack: Stack) -> None:
    key = f"session.flush:recoverable:{uuid4()}"
    async with stack.ctx.system() as conn:
        await operations.enqueue(conn, jobs.envelope("session.flush", key, None, {}))
        for _ in range(6):
            lease = await operations.claim(conn, key, uuid4(), 30)
            assert isinstance(lease, operations.Lease)
            assert await operations.fail(conn, lease, "storage unavailable")
        assert (
            await conn.fetchval("select status from app.operations where idempotency_key=$1", key)
            == "failed"
        )


async def test_outbox_bounds_concurrency_and_retries_timed_out_send(stack: Stack) -> None:
    targets = {f"session.flush:parallel:{uuid4()}" for _ in range(4)}
    async with stack.ctx.system() as conn:
        for key in targets:
            await operations.enqueue(conn, jobs.envelope("session.flush", key, None, {}))
    active = maximum = 0
    two_started, release = asyncio.Event(), asyncio.Event()

    class Queue:
        async def send(self, message, delay_s=0):
            nonlocal active, maximum
            if message["idempotency_key"] not in targets:
                return
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_started.set()
            try:
                await release.wait()
            finally:
                active -= 1

    dispatcher = operations.OutboxDispatcher(
        stack.ctx.system_pool,
        Queue(),
        batch_size=100,
        publish_concurrency=2,
        publish_timeout_s=2,
    )
    task = asyncio.create_task(dispatcher.once())
    try:
        await asyncio.wait_for(two_started.wait(), 2)
        assert maximum == 2
    finally:
        release.set()
        await task
    assert maximum == 2

    key = f"session.flush:timeout:{uuid4()}"
    async with stack.ctx.system() as conn:
        await operations.enqueue(conn, jobs.envelope("session.flush", key, None, {}))

    class StalledQueue:
        async def send(self, message, delay_s=0):
            await asyncio.Event().wait()

    stalled = operations.OutboxDispatcher(
        stack.ctx.system_pool,
        StalledQueue(),
        publish_timeout_s=0.02,
    )
    assert await stalled.once() == 0
    row = await stack.admin.fetchrow(
        "select published_at,last_error from app.outbox_events where operation_key=$1", key
    )
    assert row is not None
    assert row["published_at"] is None
    assert "TimeoutError" in row["last_error"]


async def test_api_operations_respect_membership_and_keep_interactive_input_private(stack: Stack):
    owner, member = await stack.new_user(), await stack.new_user()
    await stack.admin.execute(
        "insert into app.workspace_memberships(workspace_id,account_id,role,status)"
        " values($1,$2,'editor','active')",
        owner,
        member,
    )
    api_pool = await create_pool(as_role(stack.url, "app_api"), 1, 1)
    shared, private = f"digest.rebuild:{uuid4()}", f"ask.generate:{uuid4()}"
    try:
        async with user_tx(api_pool, owner) as conn:
            await operations.enqueue(conn, jobs.envelope("digest.rebuild", shared, owner, {}))
            await operations.enqueue(
                conn,
                jobs.envelope("ask.generate", private, owner, {"workspace_id": str(owner)}),
                transport="local",
            )
        async with user_tx(api_pool, owner, actor_account_id=member) as conn:
            assert (
                await conn.fetchval(
                    "select count(*) from app.operations where idempotency_key=any($1)",
                    [shared, private],
                )
                == 1
            )
            assert await conn.fetchval(
                "select 1 from app.operations where idempotency_key=$1", shared
            )
        await stack.admin.execute(
            "update app.workspace_memberships set status='revoked'"
            " where workspace_id=$1 and account_id=$2",
            owner,
            member,
        )
        async with user_tx(api_pool, owner, actor_account_id=member) as conn:
            assert not await conn.fetchval(
                "select 1 from app.operations where idempotency_key=$1", shared
            )
            assert not await conn.fetchval(
                "select 1 from app.outbox_events where operation_key=$1", shared
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with user_tx(api_pool, owner, actor_account_id=member) as conn:
                await operations.enqueue(
                    conn, jobs.envelope("digest.rebuild", f"new:{uuid4()}", owner, {})
                )
    finally:
        await api_pool.close()
