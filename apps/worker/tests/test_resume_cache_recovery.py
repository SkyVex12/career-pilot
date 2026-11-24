"""Real gateway admission recovers derived resume caches through the actual worker."""

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from copilot_core import keys, resumes
from copilot_core.db import create_pool, user_tx
from copilot_core.jobs import MemoryJobQueue
from copilot_core.packs import Digest
from copilot_core.teststack import as_role
from copilot_gateway import session as gateway_session
from copilot_gateway.auth import Principal, StaticVerifier
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.session import Services
from copilot_gateway.store import GatewayStore
from copilot_gateway.testing import (
    GatewayServer,
    ProtocolClient,
    ScriptedProvider,
    TextSTT,
    TextSTTControl,
)
from copilot_worker.memory import replace_resume_nodes
from copilot_worker.testing import Stack

pytestmark = pytest.mark.stack


@pytest.mark.parametrize("legacy_cache", [True, False], ids=["legacy", "missing"])
@pytest.mark.parametrize("application", [False, True], ids=["base", "application"])
async def test_resume_cache_repair_then_admission(
    stack: Stack, monkeypatch, legacy_cache: bool, application: bool
) -> None:
    workspace, actor, version = await stack.new_user(), await stack.new_user(), uuid4()
    job_id = uuid4() if application else None
    sha = resumes.sha256_text("Current source resume")
    await stack.admin.execute(
        "insert into app.workspace_memberships(workspace_id,account_id,role)"
        " values($1,$2,'editor')",
        workspace,
        actor,
    )
    async with stack.ctx.user(workspace) as conn:
        revision = await conn.fetchval(
            "insert into app.resume_versions(id,user_id,kind,label,source,text_enc,key_version,"
            "content_sha256,extracted_sha256,status)"
            " values($1,$2,'base','Resume','text','\\x00',1,$3,$3,'extracted')"
            " returning current_revision_id",
            version,
            workspace,
            sha,
        )
        await replace_resume_nodes(conn, workspace, version, {"headline": "Current source facts"})
        if job_id:
            await conn.execute(
                "insert into app.applications(id,user_id,company,role,jd_text)"
                " values($1,$2,'Example','Engineer','Build software')",
                job_id,
                workspace,
            )
    assert revision is not None
    if legacy_cache:
        legacy = Digest(
            schema="digest.v1", version="sha256:legacy", text="Pre-migration cached facts"
        )
        for key in (keys.digest(workspace), keys.resume_digest(workspace, version)):
            await stack.ctx.valkey.set(key, legacy.model_dump_json())

    queued = MemoryJobQueue()
    settings = Settings(_env_file=None, env="test", embedder="none", llm_providers=["anthropic"])  # pyright: ignore[reportCallIssue]
    pool = await create_pool(as_role(stack.url, "app_gateway"), 1, 3)
    svc = Services(
        settings=settings,
        store=GatewayStore(stack.ctx.valkey, settings),
        jobs=queued,
        router=build_router(settings, [ScriptedProvider("anthropic")]),
        verifier=StaticVerifier({"token-a": Principal(actor, "Member", None)}),
        stt_providers=[("text", lambda _: TextSTT(TextSTTControl(), "text"))],
        pool=pool,
    )
    scopes: list[str] = []

    @asynccontextmanager
    async def scoped(*args, **kwargs):
        async with user_tx(*args, **kwargs) as conn:
            if args[1] == workspace:
                scope = await conn.fetchval("select current_setting('app.account_id')")
                scopes.append(scope)
                assert scope == str(actor)
            yield conn

    monkeypatch.setattr(gateway_session, "user_tx", scoped)

    async def rejected(url: str) -> None:
        client = ProtocolClient(url)
        await client.connect()
        await client.send(
            {
                "type": "session.start",
                "mode": "interview",
                "platform": "zoom",
                "client_version": "test",
                "consent": {"store_transcript": True},
                "audio": {"sample_rate": 16000, "encoding": "pcm16"},
                **({"job_id": str(job_id)} if job_id else {}),
            }
        )
        error = await client.wait_type("error")
        assert error["code"] == "memory_not_ready"
        assert error["retry_after_ms"] == 5000
        assert await client.closed() == 4429
        assert not client.of_type("session.ready")

    try:
        async with GatewayServer(svc) as gateway:
            url = f"{gateway.url}?profile={workspace}"
            await rejected(url)
            await rejected(url)
            repairs = [message for message in queued.sent if message["type"] == "digest.rebuild"]
            assert len(repairs) == 1  # reconnects share the short repair lease
            first = repairs[0]
            assert first["payload"] == {
                "resume_version_id": str(version),
                "resume_revision_id": str(revision),
            }
            assert (
                await stack.admin.fetchval(
                    "select count(*) from app.sessions where user_id=$1", workspace
                )
                == 0
            )
            # Only the real worker can regenerate and stamp current, source-locked memory.
            await stack.run(first)
            rebuilt = Digest.model_validate_json(await stack.ctx.valkey.get(keys.digest(workspace)))
            assert rebuilt.resume_revision_id == revision
            assert rebuilt.resume_content_sha256 == sha
            assert "Current source facts" in rebuilt.text
            assert "Pre-migration" not in rebuilt.text
            client = ProtocolClient(url)
            await client.start(**({"job_id": str(job_id)} if job_id else {}))
            parent = await stack.admin.fetchrow(
                "select resume_revision_id,payer_account_id from app.sessions where id=$1",
                UUID(client.session_id),
            )
            assert parent is not None
            assert parent["resume_revision_id"] == revision
            assert parent["payer_account_id"] == actor
            await client.send({"type": "session.end"})
            assert await client.closed() == 1000
            # Cache loss after the lease expires must create a fresh operation key.
            await stack.ctx.valkey.delete(
                keys.digest(workspace),
                keys.resume_digest(workspace, version),
                keys.resume_digest_repair(workspace, revision),
            )
            await rejected(url)
            repairs = [message for message in queued.sent if message["type"] == "digest.rebuild"]
            assert len(repairs) == 2
            assert repairs[1]["idempotency_key"] != first["idempotency_key"]
            await stack.run(repairs[1])
            assert await stack.ctx.valkey.get(keys.digest(workspace)) is not None
            assert scopes  # source metadata queries used the authenticated member's scope
    finally:
        await pool.close()
