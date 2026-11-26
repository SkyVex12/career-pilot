"""Synthetic restricted-role SQL checks, called only by an owned-cluster harness.

This intentionally isolates touched tables without pgvector/S3. It is not a
substitute for replaying all CoRuby migrations in the deployment rehearsal.
"""

# Static synthetic fixture DDL and local harness paths, never request input.
# ruff: noqa: E501, ASYNC240

import asyncio
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import asyncpg
import fakeredis.aioredis
import httpx2
from fastapi import FastAPI

from copilot_api.deps import Services, services
from copilot_api.routes import xruby_oauth as auth
from copilot_api.routes import xruby_reads as reads
from copilot_core.db import create_pool


async def run_checks(config):
    root = Path(__file__).resolve().parents[3]
    admin = await asyncpg.connect(
        host="127.0.0.1",
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database="postgres",
    )
    assert (
        Path(await admin.fetchval("show data_directory")).resolve()
        == Path(config["cluster"]).resolve()
    )
    password = secrets.token_urlsafe(32)
    await admin.execute(
        "create role app_owner nologin; create role app_worker nologin; create role app_gateway nologin; create role app_system nologin; create role app_api login nosuperuser nobypassrls password '"
        + password
        + "'; create schema app authorization app_owner;"
    )
    await admin.execute("set role app_owner")
    await admin.execute("""
create table app.accounts(id uuid primary key,status text,settings jsonb);
create table app.workspaces(id uuid primary key,status text,authorization_revision bigint);
create table app.workspace_memberships(workspace_id uuid,account_id uuid,status text,authorization_revision bigint,role text default 'owner',primary key(workspace_id,account_id));
create table app.employers(id uuid primary key,user_id uuid,status text,name text);
create table app.applications(id uuid primary key,user_id uuid,company text,role text,jd_text text,resume_version_id uuid);
create table app.interviews(id uuid primary key,user_id uuid,job_id uuid,round text,starts_at timestamptz,pack_id uuid);
create table app.resume_versions(id uuid primary key,user_id uuid,current_revision_id uuid,content_sha256 text,extracted_sha256 text,status text,kind text,application_id uuid,created_at timestamptz);
create table app.resume_revisions(id uuid primary key,resume_version_id uuid,workspace_id uuid,text_enc bytea,key_version integer);
create table app.resume_revision_extractions(revision_id uuid primary key,workspace_id uuid,text_enc bytea,key_version integer);
create table app.memory_nodes(id uuid primary key,user_id uuid,employer_id uuid,kind text,status text,title text,summary text,body jsonb,source jsonb,valid_from date,updated_at timestamptz);
create table app.work_logs(id uuid primary key,user_id uuid,employer_id uuid,day date,current_revision_id uuid);
create table app.work_log_revisions(id uuid primary key,workspace_id uuid,work_log_id uuid,text_enc bytea,key_version int);
create table app.sessions(id uuid primary key,user_id uuid,employer_id uuid);
create table app.session_outputs(id uuid primary key,user_id uuid,session_id uuid,kind text,created_at timestamptz,content_enc bytea,key_version int);
grant usage on schema app to app_api;
grant select on all tables in schema app to app_api;
grant update on app.accounts,app.workspaces,app.employers,app.applications,app.interviews,app.resume_versions to app_api;
""")
    pairing_migration = (root / "migrations/0008_applications_resumes.sql").read_text("utf-8")
    a = pairing_migration.index("create function app.paired_resume(application uuid)")
    b = pairing_migration.index("$$;", a) + 3
    await admin.execute(pairing_migration[a:b])
    # Install the exact production membership predicate from its owning migration.
    migration = (root / "migrations/0031_explicit_ownership.sql").read_text("utf-8")
    start = migration.index("create function app.has_workspace_access(wid uuid)")
    end = migration.index("$$;", start) + 3
    await admin.execute(migration[start:end])
    await admin.execute(
        "revoke all on function app.has_workspace_access(uuid) from public; grant execute on function app.has_workspace_access(uuid) to app_api"
    )
    start = migration.index("create function app.has_workspace_write(wid uuid)")
    end = migration.index("$$;", start) + 3
    await admin.execute(migration[start:end])
    await admin.execute(
        "revoke all on function app.has_workspace_write(uuid) from public; grant execute on function app.has_workspace_write(uuid) to app_api"
    )
    for table, predicate in {
        "accounts": "id=nullif(current_setting('app.account_id',true),'')::uuid",
        "workspace_memberships": "account_id=nullif(current_setting('app.account_id',true),'')::uuid",
        "workspaces": "id=nullif(current_setting('app.user_id',true),'')::uuid and app.has_workspace_access(id)",
        **{
            name: "user_id=nullif(current_setting('app.user_id',true),'')::uuid and app.has_workspace_access(user_id)"
            for name in (
                "employers",
                "applications",
                "interviews",
                "resume_versions",
                "memory_nodes",
                "work_logs",
                "sessions",
                "session_outputs",
            )
        },
        "resume_revisions": "workspace_id=nullif(current_setting('app.user_id',true),'')::uuid and app.has_workspace_access(workspace_id)",
        "resume_revision_extractions": "workspace_id=nullif(current_setting('app.user_id',true),'')::uuid and app.has_workspace_access(workspace_id)",
        "work_log_revisions": "workspace_id=nullif(current_setting('app.user_id',true),'')::uuid and app.has_workspace_access(workspace_id)",
    }.items():
        await admin.execute(
            f"alter table app.{table} enable row level security; alter table app.{table} force row level security; create policy actor on app.{table} to app_api using ({predicate}); create policy owner on app.{table} to app_owner using(true) with check(true)"
        )
    async with admin.transaction():
        await admin.execute((root / "migrations/0036_xruby_delegated_reads.sql").read_text("utf-8"))
    async with admin.transaction():
        await admin.execute((root / "migrations/0037_xruby_preparation.sql").read_text("utf-8"))
    await admin.execute("reset role")
    account, profile, employer, foreign_profile, foreign_employer = (uuid4() for _ in range(5))
    app, interview, node, log, revision, session, feedback = (uuid4() for _ in range(7))
    await admin.execute(
        "insert into app.accounts values($1,'active',$2::jsonb)",
        account,
        json.dumps({"timezone": "America/Chicago"}),
    )
    for pid in (profile, foreign_profile):
        await admin.execute("insert into app.workspaces values($1,'active',1)", pid)
    await admin.execute(
        "insert into app.workspace_memberships(workspace_id,account_id,status,authorization_revision) values($1,$2,'active',1)",
        profile,
        account,
    )
    for eid, pid in ((employer, profile), (foreign_employer, foreign_profile)):
        await admin.execute("insert into app.employers values($1,$2,'active','Company')", eid, pid)
    await admin.execute(
        "insert into app.applications values($1,$2,'Company','Engineer','Recorded JD',null)",
        app,
        profile,
    )
    await admin.execute(
        "insert into app.interviews values($1,$2,$3,'technical',$4,null)",
        interview,
        profile,
        app,
        datetime.now(UTC) + timedelta(days=1),
    )
    for nid, pid, eid in ((node, profile, employer), (uuid4(), foreign_profile, foreign_employer)):
        await admin.execute(
            "insert into app.memory_nodes values($1,$2,$3,'work_item','active','Bounded work','Recorded summary',$4::jsonb,$5::jsonb,'2026-11-20',now())",
            nid,
            pid,
            eid,
            json.dumps({"state": "blocked", "due_date": "2026-11-01"}),
            json.dumps({"type": "worklog"}),
        )
    await admin.execute(
        "insert into app.work_logs values($1,$2,$3,'2026-11-20',$4)",
        log,
        profile,
        employer,
        revision,
    )
    await admin.execute(
        "insert into app.work_log_revisions values($1,$2,$3,$4,1)",
        revision,
        profile,
        log,
        b"Fictional saved observation",
    )
    await admin.execute("insert into app.sessions values($1,$2,$3)", session, profile, employer)
    await admin.execute(
        "insert into app.session_outputs values($1,$2,$3,'feedback','2026-11-21T12:00:00Z',$4,1)",
        feedback,
        profile,
        session,
        b"Fictional AI feedback; unverified",
    )
    pool = await create_pool(
        f"postgresql://app_api:{password}@127.0.0.1:{config['port']}/postgres", 1, 3
    )
    valkey = fakeredis.aioredis.FakeRedis()
    settings = SimpleNamespace(
        xruby_oauth_issuer="https://coruby.test",
        xruby_oauth_clients={"hubruby": ["https://hubruby.test/callback"]},
        require_career_os=False,
        env="test",
    )

    class Crypto:
        async def decrypt(self, *args):
            return args[-2].decode().removeprefix("fixture-encrypted:")

        async def encrypt(self, *args):
            return ("fixture-encrypted:" + args[-1]).encode(), 1

    svc = SimpleNamespace(settings=settings, pool=pool, valkey=valkey, crypto=Crypto())
    host = FastAPI()
    host.include_router(auth.router)
    host.include_router(reads.router)
    host.dependency_overrides[services] = lambda: svc
    access = "xruby_" + secrets.token_urlsafe(32)
    record = {
        "id": "fixture-grant",
        "grant_version": 1,
        "issuer": "https://coruby.test/v1/xruby/oauth",
        "resource": "https://coruby.test/v1/xruby",
        "account_id": str(account),
        "profile_id": str(profile),
        "employer_id": str(employer),
        "client_id": "hubruby",
        "scope": " ".join(sorted(auth.SCOPES)),
        "binding": "1:1",
        "expires_at": int(time.time()) + 600,
    }
    await valkey.set(auth.key("access", access), json.dumps(record), ex=600)
    headers = {"Authorization": "Bearer " + access}
    results = {}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=host), base_url="https://coruby.test"
    ) as client:
        try:
            async with pool.acquire() as conn:
                assert await conn.fetchval("select current_user") == "app_api"
                assert await conn.fetchval("select count(*) from app.memory_nodes") == 0
                assert not await conn.fetchval(
                    "select rolbypassrls from pg_roles where rolname=current_user"
                )
            for path, params in (
                ("connection", {}),
                ("next-interview", {}),
                ("current-work", {}),
                ("retrospective", {"start": "2026-11-19", "end": "2026-11-24"}),
            ):
                response = await client.get("/v1/xruby/" + path, params=params, headers=headers)
                assert response.status_code == 200, (path, response.text)
                body = response.json()
                if path == "next-interview":
                    cited = body["sources"][0]
                    prep = await client.get(
                        f"/v1/xruby/interviews/{interview}/preparation",
                        params={
                            "revision": cited["revision"],
                            "expected_hash": cited["source_hash"],
                        },
                        headers=headers,
                    )
                    assert prep.status_code == 200, prep.text
                    composite = prep.json()["sources"][1]
                    assert prep.json()["items"][0]["job_description"]["available"] is True
                    assert prep.json()["items"][0]["paired_resume"]["id"] is None
                    assert prep.json()["items"][0]["generated"] is False
                    await admin.execute(
                        "update app.applications set jd_text='Changed JD' where id=$1", app
                    )
                    assert (
                        await client.get(
                            composite["path"],
                            params={
                                "revision": composite["revision"],
                                "expected_hash": composite["source_hash"],
                            },
                            headers=headers,
                        )
                    ).status_code == 409
                    results["preparation_current_source_hash"] = True
                if path == "current-work":
                    assert len(body["items"]) == 1
                    assert body["items"][0]["overdue"] is True
                    cited = body["sources"][0]
                    check = await client.get(
                        cited["path"],
                        params={
                            "revision": cited["revision"],
                            "expected_hash": cited["source_hash"],
                        },
                        headers=headers,
                    )
                    assert check.status_code == 200, check.text
                    await admin.execute(
                        "update app.memory_nodes set summary='Changed' where id=$1", node
                    )
                    assert (
                        await client.get(
                            cited["path"],
                            params={
                                "revision": cited["revision"],
                                "expected_hash": cited["source_hash"],
                            },
                            headers=headers,
                        )
                    ).status_code == 409
                if path == "retrospective":
                    assert {v["kind"] for v in body["sources"]} == {"work_log", "feedback"}
                results[path] = True
            from xruby_preparation_postgres_checks import run_checks as check_preparation

            results.update(
                await check_preparation(
                    svc, admin, account, profile, app, interview, record, access
                )
            )
            # Foreign profile cannot be selected even if an attacker corrupts local grant metadata.
            await valkey.set(
                auth.key("access", access),
                json.dumps(
                    {
                        **record,
                        "profile_id": str(foreign_profile),
                        "employer_id": str(foreign_employer),
                    }
                ),
                ex=600,
            )
            assert (await client.get("/v1/xruby/current-work", headers=headers)).status_code == 403
            await valkey.set(auth.key("access", access), json.dumps(record), ex=600)
            # Real locks: owner revocation waits for the in-flight read, then future reads deny.
            grant = auth.Delegation(access, record)
            async with auth.read_scope(cast(Services, svc), grant, "xruby:work:read"):

                async def revoke():
                    async with admin.transaction():
                        await admin.execute(
                            "select id from app.workspaces where id=$1 for update", profile
                        )
                        await admin.execute(
                            "update app.workspace_memberships set status='revoked',authorization_revision=2 where workspace_id=$1 and account_id=$2",
                            profile,
                            account,
                        )

                task = asyncio.create_task(revoke())
                await asyncio.sleep(0.1)
                assert not task.done()
            await asyncio.wait_for(task, 5)
            assert (await client.get("/v1/xruby/current-work", headers=headers)).status_code == 403
            await admin.execute(
                "update app.workspace_memberships set status='active',authorization_revision=3 where workspace_id=$1 and account_id=$2",
                profile,
                account,
            )
            assert (await client.get("/v1/xruby/current-work", headers=headers)).status_code == 403
            results.update(
                rls_isolation=True,
                scope_override_denied=True,
                source_change_denied=True,
                locked_membership_revocation=True,
                regrant_requires_reconnect=True,
            )
        finally:
            await pool.close()
            await valkey.aclose()
            await admin.close()
    return results
