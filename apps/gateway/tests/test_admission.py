"""Real PostgreSQL admission, immutable identity, and stale writer fencing."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from copilot_core import keys
from copilot_core.db import create_pool, user_tx
from copilot_core.session_writes import entry_hash
from copilot_core.teststack import as_role, temp_database
from copilot_gateway.admission import ContextNotReady, DurableSession, WriterOwnershipLost
from copilot_gateway.auth import Principal, StaticVerifier
from copilot_gateway.testing import GatewayServer, ProtocolClient

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database() -> AsyncGenerator[tuple[str, asyncpg.Pool]]:
    async with temp_database("copilot_admission") as url:
        pool = await create_pool(as_role(url, "app_gateway"))
        try:
            yield url, pool
        finally:
            await pool.close()


async def seed(url: str) -> tuple[UUID, UUID, UUID]:
    owner, member, workspace = uuid4(), uuid4(), uuid4()
    conn = await asyncpg.connect(url)
    try:
        for actor in (owner, member):
            await conn.execute(
                "insert into app.accounts(id,email,display_name) values($1,$2,'Test')",
                actor,
                f"{actor}@test.invalid",
            )
        await conn.execute(
            "insert into app.workspaces(id,email,display_name,created_by_account_id,kind)"
            " values($1,$2,'Shared',$3,'career')",
            workspace,
            f"{workspace}@test.invalid",
            owner,
        )
        await conn.execute(
            "insert into app.workspace_memberships(workspace_id,account_id,role)"
            " values($1,$2,'editor')",
            workspace,
            member,
        )
    finally:
        await conn.close()
    return owner, member, workspace


def metadata() -> dict[str, str]:
    return {
        "mode": "meeting",
        "started_at": datetime.now(UTC).isoformat(),
        "store_transcript": "1",
        "metering_version": "connected-v1",
        "billing_price_version": "live-usd-v1",
        "billing_currency": "USD",
        "billing_rate_minor_per_hour": "500",
    }


async def test_takeover_fences_old_writer_and_preserves_confirmed_batch(database) -> None:
    url, pool = database
    actor, member, workspace = await seed(url)
    sid = uuid4()
    first = await DurableSession.acquire(pool, sid, workspace, actor, metadata=metadata())
    event = {"kind": "question.detected", "question_id": str(uuid4()), "text": "Hello"}
    batch = str(uuid4())
    async with first.batch(batch, [json.dumps(event)]) as retained:
        tagged = json.loads(retained[0])
        assert tagged["writer_generation"] == 1
    # Same batch is idempotent even if Valkey accepted it before the caller lost its reply.
    async with first.batch(batch, [json.dumps(event)]):
        pass
    second = await DurableSession.acquire(pool, sid, workspace, actor)
    assert second.generation == 2
    with pytest.raises(WriterOwnershipLost):
        async with first.batch(str(uuid4()), [json.dumps(event)]):
            pytest.fail("stale writer entered the retained transport")
    with pytest.raises(WriterOwnershipLost):
        await first.renew()
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await DurableSession.acquire(pool, sid, workspace, member)
    conn = await asyncpg.connect(url)
    try:
        parent = await conn.fetchrow("select * from app.sessions where id=$1", sid)
        assert parent is not None
        assert parent["workspace_id"] == workspace
        assert parent["created_by_account_id"] == parent["payer_account_id"] == actor
        assert parent["writer_generation"] == 2
        receipts = await conn.fetch(
            "select * from app.session_write_batches where session_id=$1", sid
        )
        assert len(receipts) == 1
        assert receipts[0]["entry_hashes"] == [entry_hash(event)]
        assert receipts[0]["generation"] == 1
    finally:
        await conn.close()


async def test_private_parent_hides_all_children_from_other_workspace_member(database) -> None:
    url, gateway = database
    actor, member, workspace = await seed(url)
    sid = uuid4()
    await DurableSession.acquire(gateway, sid, workspace, actor, metadata=metadata())
    question, answer = uuid4(), uuid4()
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(
            "insert into app.questions(id,user_id,session_id,type,detected_at,"
            "addressed_to_user,speculative_kept)"
            " values($1,$2,$3,'behavioral',now(),true,true)",
            question,
            workspace,
            sid,
        )
        await conn.execute(
            "insert into app.answers(id,user_id,question_id,pass,model,tier,latency_ms)"
            " values($1,$2,$3,'fast','test','fast','{}')",
            answer,
            workspace,
            question,
        )
        await conn.execute(
            "update app.sessions set private_context_account_id=$2 where id=$1",
            sid,
            actor,
        )
    finally:
        await conn.close()
    api = await create_pool(as_role(url, "app_api"))
    try:
        async with user_tx(api, workspace, actor_account_id=member) as tx:
            for table in ("sessions", "questions", "answers"):
                assert await tx.fetchval(f"select count(*) from app.{table}") == 0
        async with user_tx(api, workspace, actor_account_id=actor) as tx:
            for table in ("sessions", "questions", "answers"):
                assert await tx.fetchval(f"select count(*) from app.{table}") == 1
    finally:
        await api.close()


async def test_two_live_gateways_parent_before_ready_and_fenced_takeover(database, env) -> None:
    url, pool = database
    actor, _, workspace = await seed(url)
    env.verifier = StaticVerifier({"token-a": Principal(actor, "Host", None)})
    settings = env.settings(lease_renew_s=0.1)
    first_services = env.services(settings, pool=pool)
    next_services = env.services(settings, pool=pool)
    admin = await asyncpg.connect(url)
    try:
        async with GatewayServer(first_services) as first, GatewayServer(next_services) as second:
            client = ProtocolClient(f"{first.url}?profile={workspace}")
            await client.start(mode="meeting")
            sid = UUID(client.session_id)
            parent = await admin.fetchrow("select * from app.sessions where id=$1", sid)
            assert parent is not None  # admission committed before session.ready
            assert parent["payer_account_id"] == actor
            assert parent["workspace_id"] == workspace
            original = first.manager.sessions[sid]
            assert original.durable is not None
            assert await original.writer.drain(within_s=2)
            takeover = ProtocolClient(f"{second.url}?profile={workspace}")
            takeover.session_id, takeover.last_seq = client.session_id, client.last_seq
            await takeover.resume()
            async with asyncio.timeout(3):
                while sid not in second.manager.sessions:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            assert await client.closed() == 4410
            assert second.manager.sessions[sid].durable.generation == 2
            with pytest.raises(WriterOwnershipLost):
                async with original.durable.batch(str(uuid4()), ['{"kind":"stale"}']):
                    pytest.fail("stale gateway wrote a retained batch")
            await takeover.send({"type": "session.end"})
            assert await takeover.closed() == 1000
            assert (
                await admin.fetchval(
                    "select admission_state from app.sessions where id=$1",
                    sid,
                )
                == "closing"
            )
            assert (
                await admin.fetchval(
                    "select count(*) from app.outbox_events where operation_key=$1",
                    f"session.ended:{sid}:g2",
                )
                == 1
            )
    finally:
        await admin.close()


async def test_admission_requires_exact_prepared_resume_revision(database) -> None:
    url, pool = database
    actor, _, workspace = await seed(url)
    resume = uuid4()
    admin = await asyncpg.connect(url)
    try:
        row = await admin.fetchrow(
            "insert into app.resume_versions(id,user_id,kind,label,source,text_enc,key_version,"
            "content_sha256) values($1,$2,'base','Resume','text','\\x00',1,$3)"
            " returning current_revision_id",
            resume,
            workspace,
            "a" * 64,
        )
        assert row is not None
        expected = row["current_revision_id"]
        source = {**metadata(), "mode": "interview"}
        with pytest.raises(ContextNotReady):
            await DurableSession.acquire(pool, uuid4(), workspace, actor, metadata=source)
        source.update(resume_revision_id=str(expected), resume_content_sha256="a" * 64)
        first = await DurableSession.acquire(pool, uuid4(), workspace, actor, metadata=source)
        assert (
            await admin.fetchval(
                "select resume_revision_id from app.sessions where id=$1",
                first.sid,
            )
            == expected
        )
        await admin.execute(
            "update app.resume_versions set content_sha256=$2 where id=$1",
            resume,
            "b" * 64,
        )
        with pytest.raises(ContextNotReady):
            await DurableSession.acquire(pool, uuid4(), workspace, actor, metadata=source)
        # Replacing source content never repins an existing admitted session.
        assert (
            await admin.fetchval(
                "select resume_revision_id from app.sessions where id=$1",
                first.sid,
            )
            == expected
        )
    finally:
        await admin.close()


async def test_admission_links_loaded_interview_pack_only_in_selected_job(database) -> None:
    url, pool = database
    actor, _, workspace = await seed(url)
    _, _, other_workspace = await seed(url)
    job, other_job, foreign_job = uuid4(), uuid4(), uuid4()
    interview, other_interview, foreign_interview = uuid4(), uuid4(), uuid4()
    pack, other_pack, foreign_pack = uuid4(), uuid4(), uuid4()
    admin = await asyncpg.connect(url)
    try:
        for job_id, workspace_id in (
            (job, workspace),
            (other_job, workspace),
            (foreign_job, other_workspace),
        ):
            await admin.execute(
                "insert into app.applications(id,user_id,company,role,jd_text)"
                " values($1,$2,'Example','Engineer','Build software')",
                job_id,
                workspace_id,
            )
        for interview_id, job_id, workspace_id, pack_id in (
            (interview, job, workspace, pack),
            (other_interview, other_job, workspace, other_pack),
            (foreign_interview, foreign_job, other_workspace, foreign_pack),
        ):
            await admin.execute(
                "insert into app.interviews(id,user_id,job_id,round,starts_at,pack_id)"
                " values($1,$2,$3,'general',now(),$4)",
                interview_id,
                workspace_id,
                job_id,
                pack_id,
            )
        for loaded_pack, expected in ((pack, interview), (other_pack, None), (foreign_pack, None)):
            session = await DurableSession.acquire(
                pool,
                uuid4(),
                workspace,
                actor,
                metadata={
                    **metadata(),
                    "mode": "interview",
                    "job_id": str(job),
                    "pack_id": str(loaded_pack),
                },
            )
            assert (
                await admin.fetchval(
                    "select interview_id from app.sessions where id=$1", session.sid
                )
                == expected
            )
    finally:
        await admin.close()


async def test_failed_retention_transaction_has_no_confirmed_receipt(database) -> None:
    url, pool = database
    actor, _, workspace = await seed(url)
    session = await DurableSession.acquire(pool, uuid4(), workspace, actor, metadata=metadata())
    with pytest.raises(TimeoutError):
        async with session.batch(str(uuid4()), ['{"kind":"connected"}']):
            raise TimeoutError("retained transport unavailable")
    admin = await asyncpg.connect(url)
    try:
        assert (
            await admin.fetchval(
                "select count(*) from app.session_write_batches where session_id=$1",
                session.sid,
            )
            == 0
        )
        await admin.execute("delete from app.sessions where id=$1", session.sid)
        with pytest.raises(WriterOwnershipLost):
            await session.renew()
    finally:
        await admin.close()


async def test_durable_renewal_bounds_parent_lock_wait(database) -> None:
    url, pool = database
    actor, _, workspace = await seed(url)
    session = await DurableSession.acquire(pool, uuid4(), workspace, actor, metadata=metadata())
    admin = await asyncpg.connect(url)
    try:
        async with admin.transaction():
            await admin.execute("select id from app.sessions where id=$1 for update", session.sid)
            async with asyncio.timeout(3):
                with pytest.raises(TimeoutError):
                    await session.renew()
        # Cancellation releases the blocked connection and rolls the transaction back.
        await session.renew()
    finally:
        await admin.close()


async def test_unrenewed_durable_ownership_stops_live_production(
    database, env, monkeypatch
) -> None:
    url, pool = database
    actor, _, workspace = await seed(url)
    env.verifier = StaticVerifier({"token-a": Principal(actor, "Host", None)})
    services = env.services(env.settings(lease_renew_s=0.02), pool=pool)
    async with GatewayServer(services) as gateway:
        client = ProtocolClient(f"{gateway.url}?profile={workspace}")
        await client.start(mode="meeting")
        session = gateway.manager.sessions[UUID(client.session_id)]
        assert session.durable is not None

        async def unavailable() -> None:
            session._durable_renewed_at = time.monotonic() - 31
            raise asyncpg.CannotConnectNowError("database unavailable")

        monkeypatch.setattr(session.durable, "renew", unavailable)
        error = await client.wait_type("error", code="retention_unavailable")
        assert error["fatal"]
        assert session._retention_failed
        assert not session.usage_meter.active
        assert await client.closed() == 4500


async def test_resume_with_disappearing_state_closes_new_durable_claim(
    database, env, monkeypatch
) -> None:
    url, pool = database
    actor, _, workspace = await seed(url)
    env.verifier = StaticVerifier({"token-a": Principal(actor, "Host", None)})
    admin = await asyncpg.connect(url)
    try:
        async with (
            GatewayServer(env.services(pool=pool)) as first,
            GatewayServer(env.services(pool=pool)) as second,
        ):
            client = ProtocolClient(f"{first.url}?profile={workspace}")
            await client.start(mode="meeting")
            sid = UUID(client.session_id)
            await client.drop()
            await first.manager.sessions[sid].abandon(1001, "test handover")
            store = second.services.store
            acquire = store.take_resume_lease

            async def disappeared(sid: UUID, task_id: str) -> bool:
                await store.client.delete(keys.sess_state(sid))
                return await acquire(sid, task_id)

            monkeypatch.setattr(store, "take_resume_lease", disappeared)
            client.url = f"{second.url}?profile={workspace}"
            await client.resume()
            assert await client.closed() == 4408
            assert second.manager.sessions == {}
            assert (
                await admin.fetchval("select admission_state from app.sessions where id=$1", sid)
                == "failed"
            )
            assert (
                await admin.fetchval(
                    "select count(*) from app.outbox_events where operation_key=$1",
                    f"session.ended:{sid}:g2",
                )
                == 1
            )
    finally:
        await admin.close()


async def test_account_deletion_cannot_make_private_history_shared(database) -> None:
    url, pool = database
    actor, member, workspace = await seed(url)
    private = await DurableSession.acquire(pool, uuid4(), workspace, actor, metadata=metadata())
    shared = await DurableSession.acquire(pool, uuid4(), workspace, actor, metadata=metadata())
    admin = await asyncpg.connect(url)
    try:
        await admin.execute(
            "update app.sessions set private_context_account_id=$2 where id=$1",
            private.sid,
            actor,
        )
        await admin.execute(
            "update app.workspace_memberships set role='owner'"
            " where workspace_id=$1 and account_id=$2",
            workspace,
            member,
        )
        await admin.execute("delete from app.workspaces where id=$1", actor)
        await admin.execute("delete from app.accounts where id=$1", actor)
        assert await admin.fetchval("select id from app.sessions where id=$1", private.sid) is None
        shared_parent = await admin.fetchrow("select * from app.sessions where id=$1", shared.sid)
        assert shared_parent is not None
        assert shared_parent["payer_account_id"] is None
        assert (
            await admin.fetchval(
                "select payer_account_id from app.live_usage_sessions where session_id=$1",
                shared.sid,
            )
            == actor
        )
        assert (
            await admin.fetchval(
                "select status from app.workspace_memberships"
                " where workspace_id=$1 and account_id=$2",
                workspace,
                member,
            )
            == "active"
        )
    finally:
        await admin.close()
