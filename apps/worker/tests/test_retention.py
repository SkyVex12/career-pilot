"""Retention preserves accounting and retries storage deletion without reference races."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from copilot_core import keys, usage
from copilot_worker import retention
from copilot_worker.testing import Stack

pytestmark = pytest.mark.stack


async def test_expiry_only_removes_drained_history_and_preserves_accounting(stack: Stack):
    uid = await stack.new_user()
    await stack.admin.execute(
        "update app.workspaces set settings=settings||'{\"retention_days\":1}' where id=$1", uid
    )
    ended, active, buffered, writer = [uuid4() for _ in range(4)]
    for sid in (ended, active, buffered, writer):
        await stack.admin.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at) "
            "values($1,$2,'interview',false,now()-interval '3 days',"
            "case when $3 then null else now()-interval '2 days' end)",
            sid,
            uid,
            sid == active,
        )
    await stack.admin.execute(
        "update app.sessions set writer_expires_at=now()+interval '1 minute' where id=$1",
        writer,
    )
    await stack.ctx.valkey.xadd(keys.sess_log(buffered), {"e": "pending retained payload"})
    start = datetime.now(UTC) - timedelta(days=3)
    async with stack.ctx.user(uid) as conn:
        await usage.record_interval(
            conn,
            workspace_id=uid,
            session_id=ended,
            payer_account_id=uid,
            event_id=uuid4(),
            started_at=start,
            ended_at=start + timedelta(seconds=60),
            price=usage.UsagePrice(),
        )
    try:
        assert await retention.expire_history(stack.ctx) == 1
        remaining = set(
            await stack.admin.fetch(
                "select id from app.sessions where user_id=$1",
                uid,
            )
        )
        assert {r["id"] for r in remaining} == {active, buffered, writer}
        assert (
            await stack.admin.fetchval(
                "select sum(duration_microseconds) from app.live_usage_intervals "
                "where session_id=$1",
                ended,
            )
            == 60_000_000
        )
        assert await retention.expire_history(stack.ctx) == 0
    finally:
        await stack.ctx.valkey.delete(keys.sess_log(buffered))


async def document(stack: Stack, uid, key: str):
    did = uuid4()
    await stack.admin.execute(
        "insert into app.documents(id,user_id,kind,s3_key,sha256,content_type) "
        "values($1,$2,'resume',$3,'hash','text/plain')",
        did,
        uid,
        key,
    )
    return did


async def test_storage_failure_retries_and_deletion_reservation_prevents_reuse(
    stack: Stack,
    monkeypatch,
):
    uid, key = await stack.new_user(), f"retention/{uuid4()}"
    did = await document(stack, uid, key)
    await stack.admin.execute("delete from app.documents where id=$1", did)
    delete = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(stack.ctx.store, "delete", delete)
    assert await retention.delete_objects(stack.ctx) == 0
    row = await stack.admin.fetchrow(
        "select state,attempts,last_error from app.object_deletions where object_key=$1",
        key,
    )
    assert row is not None
    assert tuple(row.values()) == ("deleting", 1, "TimeoutError")
    with pytest.raises(asyncpg.CheckViolationError):
        await document(stack, uid, key)
    await stack.admin.execute(
        "update app.object_deletions set next_attempt_at=now() where object_key=$1",
        key,
    )

    async def reserved_delete(target):
        assert target == key
        with pytest.raises(asyncpg.CheckViolationError, match="object is being deleted"):
            await document(stack, uid, key)

    delete.side_effect = reserved_delete
    assert await retention.delete_objects(stack.ctx) == 1
    assert (
        await stack.admin.fetchval(
            "select state from app.object_deletions where object_key=$1",
            key,
        )
        == "done"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await document(stack, uid, key)


async def test_retained_revision_keeps_object_until_source_is_deleted(stack: Stack, monkeypatch):
    uid, key, version = await stack.new_user(), f"retention/{uuid4()}", uuid4()
    did = await document(stack, uid, key)
    await stack.admin.execute(
        "insert into app.resume_versions(id,user_id,kind,label,source,text_enc,key_version,"
        "content_sha256,file_s3_key,file_name_enc) "
        "values($1,$2,'base','Resume','text',$3,1,'first',$4,$3)",
        version,
        uid,
        b"sealed",
        key,
    )
    await stack.admin.execute(
        "update app.resume_versions set content_sha256='second',file_s3_key=null,"
        "file_name_enc=null where id=$1",
        version,
    )
    await stack.admin.execute("delete from app.documents where id=$1", did)
    delete = AsyncMock()
    monkeypatch.setattr(stack.ctx.store, "delete", delete)
    assert await retention.delete_objects(stack.ctx) == 0
    delete.assert_not_awaited()
    await stack.admin.execute("delete from app.resume_versions where id=$1", version)
    assert await retention.delete_objects(stack.ctx) == 1
    delete.assert_awaited_once_with(key)


async def test_first_deletion_intent_waits_for_an_uncommitted_reference(stack: Stack, monkeypatch):
    uid, key = await stack.new_user(), f"retention/{uuid4()}"
    old = await document(stack, uid, key)
    new = uuid4()
    conn = await asyncpg.connect(stack.url)
    try:
        async with conn.transaction():
            await conn.execute(
                "insert into app.documents(id,user_id,kind,s3_key,sha256,content_type) "
                "values($1,$2,'resume',$3,'hash','text/plain')",
                new,
                uid,
                key,
            )
            deleting = asyncio.create_task(
                stack.admin.execute("delete from app.documents where id=$1", old)
            )
            finished, _ = await asyncio.wait({deleting}, timeout=0.1)
            assert not finished  # First-intent publication must wait for reference commit.
        await asyncio.wait_for(deleting, 5)
    finally:
        await conn.close()
    delete = AsyncMock()
    monkeypatch.setattr(stack.ctx.store, "delete", delete)
    assert await retention.delete_objects(stack.ctx) == 0
    delete.assert_not_awaited()
    await stack.admin.execute("delete from app.documents where id=$1", new)
    assert await retention.delete_objects(stack.ctx) == 1
    delete.assert_awaited_once_with(key)
