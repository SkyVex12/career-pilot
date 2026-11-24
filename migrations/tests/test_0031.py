"""Explicit ownership survives profile switching, revocation, and concurrent owner changes."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.crypto import DevKeyProvider, UserCrypto, seal
from copilot_core.db import create_pool, user_tx
from copilot_core.identities import Identities
from copilot_core.migrate import load_migrations
from copilot_core.teststack import MIGRATIONS, admin_url, as_role, temp_database
from copilot_core.usage import UsagePrice, record_adjustment, record_interval, usage_summary

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def url() -> AsyncIterator[str]:
    async with temp_database("copilot_ownership") as value:
        yield value


@asynccontextmanager
async def connection(url: str, actor: UUID, workspace: UUID | None = None, role: str = "app_api"):
    conn = await asyncpg.connect(as_role(url, role))
    try:
        async with conn.transaction():
            await conn.execute(
                "select set_config('app.account_id',$1,true),set_config('app.user_id',$2,true)",
                str(actor),
                str(workspace or actor),
            )
            yield conn
    finally:
        await conn.close()


async def account(conn: asyncpg.Connection) -> UUID:
    uid = uuid4()
    await conn.execute(
        "insert into app.accounts(id,email,display_name) values($1,$2,'Account')",
        uid,
        f"{uid}@example.com",
    )
    return uid


async def test_membership_is_authoritative_and_revocation_is_not_undone_by_listing(url: str):
    admin = await asyncpg.connect(url)
    try:
        owner, stranger = await account(admin), await account(admin)
        wid, app_id, person = uuid4(), uuid4(), str(uuid4())
        async with connection(url, owner) as conn:
            assert (
                await conn.fetchval(
                    "select app.provision_external_workspace($1,$2,'Shared')", person, wid
                )
                == wid
            )
        assert await admin.fetchval("select count(*) from app.accounts where id=$1", wid) == 0
        await admin.execute(
            "insert into app.applications(id,user_id,company,role,jd_text)"
            " values($1,$2,'A','B','C')",
            app_id,
            wid,
        )
        # Merely naming another workspace never grants access, even in a direct DB transaction.
        async with connection(url, stranger, wid) as conn:
            assert await conn.fetchval("select count(*) from app.applications") == 0
            # Account-owned listing metadata cannot grant a workspace membership.
            await conn.execute(
                "insert into app.profiles(id,user_id,career_os_user_id,name)"
                " values($1,$2,$3,'Untrusted listing')",
                wid,
                stranger,
                person,
            )
            assert not await conn.fetchval("select app.has_workspace_access($1)", wid)
            assert await conn.fetchval("select count(*) from app.applications") == 0
        async with connection(url, owner, wid) as conn:
            await conn.execute("select app.set_workspace_membership($1,$2,'editor')", wid, stranger)
        async with connection(url, stranger, wid) as conn:
            assert await conn.fetchval("select id from app.applications") == app_id
            await conn.execute("delete from app.profiles where id=$1", wid)
            assert await conn.fetchval("select app.has_workspace_access($1)", wid)
        async with connection(url, owner, wid) as conn:
            await conn.execute(
                "select app.set_workspace_membership($1,$2,'editor','revoked')",
                wid,
                stranger,
            )
        async with connection(url, stranger, wid) as conn:
            await conn.execute(
                "select app.provision_external_workspace($1,$2,'Shared')", person, wid
            )
            assert await conn.fetchval("select count(*) from app.applications") == 0
            assert not await conn.fetchval("select app.has_workspace_access($1)", wid)
    finally:
        await admin.close()


async def test_shared_personal_workspace_never_exposes_account_private_rows(url: str):
    admin = await asyncpg.connect(url)
    try:
        owner, collaborator = await account(admin), await account(admin)
        conversation, application = uuid4(), uuid4()
        await admin.execute(
            "insert into app.ask_conversations(id,user_id,profile_id,title)"
            " values($1,$2,$2,'Private title')",
            conversation,
            owner,
        )
        await admin.execute(
            "insert into app.applications(id,user_id,company,role,jd_text)"
            " values($1,$2,'A','B','C')",
            application,
            owner,
        )
        async with connection(url, owner) as conn:
            await conn.execute(
                "select app.set_workspace_membership($1,$2,'editor')", owner, collaborator
            )
        async with connection(url, collaborator, owner) as conn:
            assert await conn.fetchval("select id from app.applications") == application
            assert await conn.fetchval("select count(*) from app.ask_conversations") == 0
            assert await conn.fetchval("select count(*) from app.accounts where id=$1", owner) == 0
        async with connection(url, owner) as conn:
            assert await conn.fetchval("select id from app.ask_conversations") == conversation
    finally:
        await admin.close()


async def test_retired_account_ids_and_existing_workspaces_cannot_be_claimed_by_new_accounts(
    url: str,
):
    admin = await asyncpg.connect(url)
    try:
        owner, collaborator = await account(admin), await account(admin)
        await admin.execute(
            "insert into app.workspace_memberships(workspace_id,account_id,role)"
            " values($1,$2,'owner')",
            owner,
            collaborator,
        )
        await admin.execute("delete from app.accounts where id=$1", owner)
        assert (
            await admin.fetchval(
                "select account_id from app.account_tombstones where account_id=$1", owner
            )
            == owner
        )
        async with connection(url, owner) as conn:
            assert await conn.fetchval("select account_id from app.account_tombstones") == owner
            for statement in (
                "insert into app.account_tombstones(account_id) values($1)",
                "update app.account_tombstones set retired_at=now() where account_id=$1",
                "delete from app.account_tombstones where account_id=$1",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(statement, owner)
        for role in ("app_worker", "app_gateway"):
            async with connection(url, owner, role=role) as conn:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.fetch("select * from app.account_tombstones")
        with pytest.raises(asyncpg.CheckViolationError, match="retired account ID"):
            await admin.execute(
                "insert into app.accounts(id,email,display_name) values($1,$2,'New account')",
                owner,
                f"replacement-{owner}@example.com",
            )
        async with connection(url, collaborator, owner) as conn:
            assert await conn.fetchval("select count(*) from app.account_tombstones") == 0
        wid = uuid4()
        await admin.execute(
            "insert into app.workspaces(id,email,display_name,owner_id)"
            " values($1,$2,'Career workspace',$3)",
            wid,
            f"{wid}@profiles.invalid",
            collaborator,
        )
        with pytest.raises(asyncpg.CheckViolationError, match="already belongs to a workspace"):
            await admin.execute(
                "insert into app.accounts(id,email,display_name) values($1,$2,'New account')",
                wid,
                f"{wid}@example.com",
            )
        assert (
            await admin.fetchval(
                "select count(*) from app.workspace_memberships where account_id=$1", owner
            )
            == 0
        )
    finally:
        await admin.close()


async def test_shared_personal_workspace_keeps_usage_private_to_the_authenticated_payer(url: str):
    admin = await asyncpg.connect(url)
    start = datetime(2026, 11, 1, tzinfo=UTC)
    stop = start + timedelta(minutes=1)
    try:
        owner, collaborator = await account(admin), await account(admin)
        async with connection(url, owner) as conn:
            await conn.execute(
                "select app.set_workspace_membership($1,$2,'editor')", owner, collaborator
            )
        for payer in (owner, collaborator):
            session = uuid4()
            async with connection(url, payer, owner, role="app_worker") as conn:
                await record_interval(
                    conn,
                    workspace_id=owner,
                    session_id=session,
                    payer_account_id=payer,
                    event_id=uuid4(),
                    started_at=start,
                    ended_at=stop,
                    price=UsagePrice(),
                )
                await record_adjustment(
                    conn,
                    adjustment_id=uuid4(),
                    workspace_id=owner,
                    session_id=session,
                    payer_account_id=payer,
                    amount_minor=-1,
                    reason="Test correction",
                    effective_at=start,
                )
        # A selected workspace can equal somebody else's account UUID. It must never
        # replace the authenticated account as the ledger's read authorization.
        async with connection(url, collaborator, owner) as conn:
            for table in (
                "live_usage_sessions",
                "live_usage_events",
                "live_usage_intervals",
                "usage_adjustments",
            ):
                assert await conn.fetchval(
                    f"select array_agg(distinct payer_account_id) from app.{table}"
                ) == [collaborator]
            own = await usage_summary(
                conn, payer_account_id=collaborator, started_at=start, ended_at=stop
            )
            other = await usage_summary(
                conn, payer_account_id=owner, started_at=start, ended_at=stop
            )
            assert own.connected_microseconds == 60_000_000
            assert other.connected_microseconds == 0
        # Accounting remains readable by its payer after the content relationship ends.
        async with connection(url, owner) as conn:
            await conn.execute(
                "select app.set_workspace_membership($1,$2,'editor','revoked')",
                owner,
                collaborator,
            )
        async with connection(url, collaborator) as conn:
            assert await conn.fetchval("select count(*) from app.live_usage_sessions") == 1
    finally:
        await admin.close()


async def test_viewer_cannot_write_or_delete_and_members_cannot_promote_themselves(url: str):
    admin = await asyncpg.connect(url)
    try:
        owner, viewer = await account(admin), await account(admin)
        application = uuid4()
        await admin.execute(
            "insert into app.applications(id,user_id,company,role,jd_text)"
            " values($1,$2,'A','B','C')",
            application,
            owner,
        )
        async with connection(url, owner) as conn:
            await conn.execute("select app.set_workspace_membership($1,$2,'viewer')", owner, viewer)
        async with connection(url, viewer, owner) as conn:
            assert await conn.fetchval("select id from app.applications") == application
            assert await conn.execute("delete from app.applications") == "DELETE 0"
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute("update app.applications set company='Changed'")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                async with conn.transaction():
                    await conn.execute(
                        "select app.set_workspace_membership($1,$2,'owner')", owner, viewer
                    )
        with pytest.raises(asyncpg.CheckViolationError, match="active owner"):
            async with connection(url, owner) as conn:
                await conn.execute("select app.set_workspace_membership($1,$1,'editor')", owner)
        with pytest.raises(asyncpg.CheckViolationError, match="active owner"):
            await admin.execute("update app.accounts set status='disabled' where id=$1", owner)
    finally:
        await admin.close()


@pytest.mark.parametrize("isolation", ["read_committed", "repeatable_read"])
async def test_concurrent_owner_removals_cannot_leave_an_active_workspace_ownerless(
    url: str, isolation
):
    admin = await asyncpg.connect(url)
    try:
        first, second = await account(admin), await account(admin)
        async with connection(url, first) as conn:
            await conn.execute("select app.set_workspace_membership($1,$2,'owner')", first, second)
        ready, count = asyncio.Event(), 0

        async def demote(actor: UUID):
            nonlocal count
            conn = await asyncpg.connect(as_role(url, "app_api"))
            try:
                async with conn.transaction(isolation=isolation):
                    await conn.execute(
                        "select set_config('app.account_id',$1,true),"
                        " set_config('app.user_id',$2,true)",
                        str(actor),
                        str(first),
                    )
                    assert await conn.fetchval("select app.has_workspace_access($1)", first)
                    count += 1
                    if count == 2:
                        ready.set()
                    await ready.wait()
                    await conn.execute(
                        "select app.set_workspace_membership($1,$2,'editor')", first, actor
                    )
            finally:
                await conn.close()

        results = await asyncio.gather(demote(first), demote(second), return_exceptions=True)
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], (asyncpg.CheckViolationError, asyncpg.SerializationError))
        assert (
            await admin.fetchval(
                "select count(*) from app.workspace_memberships where workspace_id=$1"
                " and role='owner' and status='active'",
                first,
            )
            == 1
        )
    finally:
        await admin.close()


async def test_identity_realm_is_part_of_lookup_and_cache_identity(url: str):
    admin = await asyncpg.connect(url)
    pool = await create_pool(as_role(url, "app_api"))
    try:
        first, second = await account(admin), await account(admin)
        subject = str(uuid4())
        for who, realm in ((first, "default"), (second, "other-issuer")):
            await admin.execute(
                "insert into app.account_identities(provider,realm,subject,account_id,email)"
                " values('google',$1,$2,$3,$4)",
                realm,
                subject,
                who,
                f"{who}@example.com",
            )
        identities = Identities(pool)
        assert await identities.user_of("google", subject) == first
        assert await identities.user_of("google", subject, "other-issuer") == second
        assert await identities.user_of("google", subject) == first
        async with user_tx(pool, first) as conn:
            assert (
                await conn.fetchval("select count(*) from app.identities where subject=$1", subject)
                == 1
            )
    finally:
        await pool.close()
        await admin.close()


async def test_account_metadata_does_not_bypass_revoked_workspace_ownership(url: str):
    admin = await asyncpg.connect(url)
    try:
        original, successor = await account(admin), await account(admin)
        async with connection(url, original) as conn:
            await conn.execute(
                "select app.set_workspace_membership($1,$2,'owner')",
                original,
                successor,
            )
        async with connection(url, successor, original) as conn:
            await conn.execute(
                "select app.set_workspace_membership($1,$1,'editor','revoked')",
                original,
            )
        async with connection(url, original) as conn:
            await conn.execute(
                "update app.accounts set display_name='Changed' where id=$1", original
            )
        assert (
            await admin.fetchval("select display_name from app.accounts where id=$1", original)
            == "Changed"
        )
        assert (
            await admin.fetchval("select display_name from app.workspaces where id=$1", original)
            == "Account"
        )
    finally:
        await admin.close()


async def test_account_keys_have_independent_lifetime_and_cache_namespace(url: str):
    admin = await asyncpg.connect(url)
    crypto = UserCrypto(DevKeyProvider(b"a" * 32))
    try:
        owner, collaborator = await account(admin), await account(admin)
        async with connection(url, owner) as conn:
            shared, shared_version = await crypto.encrypt(
                conn, owner, "work_logs", "shared", "Shared"
            )
            private, private_version = await crypto.encrypt(
                conn, owner, "integrations", "private", "Secret"
            )
            assert shared_version == private_version == 1
            assert await crypto.decrypt(conn, owner, "work_logs", "shared", shared, 1) == "Shared"
            assert (
                await crypto.decrypt(conn, owner, "integrations", "private", private, 1) == "Secret"
            )
            assert await conn.fetchval(
                "select encrypted_dek from app.account_keys"
            ) != await conn.fetchval("select encrypted_dek from app.user_keys")
            await conn.execute(
                "select app.set_workspace_membership($1,$2,'editor')", owner, collaborator
            )
        async with connection(url, collaborator, owner) as conn:
            assert await conn.fetchval("select count(*) from app.user_keys") == 1
            assert (
                await conn.fetchval(
                    "select count(*) from app.account_keys where account_id=$1", owner
                )
                == 0
            )
        await admin.execute("delete from app.workspaces where id=$1", owner)
        crypto.forget(owner)
        async with connection(url, owner) as conn:
            assert await conn.fetchval("select count(*) from app.user_keys") == 0
            assert (
                await crypto.decrypt(conn, owner, "integrations", "private", private, 1) == "Secret"
            )
            assert await conn.fetchval("select id from app.accounts") == owner
        await admin.execute("delete from app.accounts where id=$1", owner)
        assert (
            await admin.fetchval("select count(*) from app.account_keys where account_id=$1", owner)
            == 0
        )
    finally:
        await admin.close()


async def test_populated_legacy_upgrade_preserves_ids_and_separates_private_foreign_keys():
    admin = await asyncpg.connect(admin_url())
    name = f"copilot_ownership_upgrade_{uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    conn = await asyncpg.connect(urlunsplit(urlsplit(admin_url())._replace(path=f"/{name}")))
    try:
        migrations = load_migrations(MIGRATIONS)
        for migration in migrations:
            if migration.name.startswith("0031"):
                break
            async with conn.transaction():
                await conn.execute(migration.sql)
        first, second, workspace = uuid4(), uuid4(), uuid4()
        for who, creator in ((first, None), (second, None), (workspace, first)):
            await conn.execute(
                "insert into app.users(id,email,display_name,owner_id) values($1,$2,'Legacy',$3)",
                who,
                f"{who}@example.com",
                creator,
            )
        for who in (first, second):
            await conn.execute(
                "insert into app.profiles(id,user_id,career_os_user_id,name)"
                " values($1,$2,'legacy','Legacy')",
                workspace,
                who,
            )
        await conn.execute(
            "insert into app.user_keys(user_id,version,encrypted_dek) values($1,1,$2)",
            workspace,
            b"unchanged encrypted key",
        )
        provider = DevKeyProvider(b"b" * 32)
        dek, wrapped = await provider.generate_data_key(first)
        ciphertext = seal(dek, "integrations", "legacy", "Legacy private token")
        await conn.execute(
            "insert into app.user_keys(user_id,version,encrypted_dek) values($1,2,$2)",
            first,
            wrapped,
        )
        target = next(m for m in migrations if m.name.startswith("0031"))
        async with conn.transaction():
            await conn.execute(target.sql)
        assert await conn.fetchval("select count(*) from app.accounts") == 2
        assert await conn.fetchval("select count(*) from app.workspaces") == 3
        assert await conn.fetchval("select count(*) from app.workspace_memberships") == 4
        assert (
            await conn.fetchval(
                "select encrypted_dek from app.user_keys where user_id=$1", workspace
            )
            == b"unchanged encrypted key"
        )
        assert (
            await conn.fetchval(
                "select encrypted_dek from app.account_keys where account_id=$1 and version=2",
                first,
            )
            == wrapped
        )
        assert (
            await UserCrypto(provider).decrypt(
                conn,
                first,
                "integrations",
                "legacy",
                ciphertext,
                2,
            )
            == "Legacy private token"
        )
        assert (
            await conn.fetchval(
                "select confrelid::regclass::text from pg_constraint"
                " where conname='ask_conversations_user_id_fkey'",  # Account-private owner.
            )
            == "app.accounts"
        )
        assert (
            await conn.fetchval(
                "select confrelid::regclass::text from pg_constraint"
                " where conname='documents_user_id_fkey'",
            )
            == "app.workspaces"
        )
    finally:
        await conn.close()
        await admin.execute(f'drop database "{name}" with(force)')
        await admin.close()
