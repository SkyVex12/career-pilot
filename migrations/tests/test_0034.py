"""Upgrade real pre-revision rows without inventing historical provenance."""

from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from copilot_core.migrate import load_migrations
from copilot_core.teststack import MIGRATIONS, admin_url

pytestmark = pytest.mark.stack


async def test_populated_source_upgrade_preserves_ciphertext_and_unproven_session_history():
    admin = await asyncpg.connect(admin_url())
    name = f"copilot_source_upgrade_{uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    conn = await asyncpg.connect(urlunsplit(urlsplit(admin_url())._replace(path=f"/{name}")))
    try:
        migrations = load_migrations(MIGRATIONS)
        for migration in migrations:
            if migration.version >= "0034":
                break
            async with conn.transaction():
                await conn.execute(migration.sql)
        uid, version, sid = uuid4(), uuid4(), uuid4()
        await conn.execute(
            "insert into app.accounts(id,email,display_name) values($1,$2,'Existing')",
            uid,
            f"{uid}@example.com",
        )
        await conn.execute(
            "update app.workspaces set settings=settings||'{\"retention_days\":30}' where id=$1",
            uid,
        )
        await conn.execute(
            "insert into app.resume_versions(id,user_id,kind,label,source,text_enc,key_version,"
            "content_sha256) values($1,$2,'base','Existing','text',$3,7,'original')",
            version,
            uid,
            b"original ciphertext",
        )
        await conn.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at) "
            "values($1,$2,'interview',false,now()-interval '10 days',now()-interval '9 days')",
            sid,
            uid,
        )
        for migration in migrations:
            if migration.version >= "0034":
                async with conn.transaction():
                    await conn.execute(migration.sql)
        row = await conn.fetchrow(
            "select r.* from app.resume_revisions r join app.resume_versions v "
            "on v.current_revision_id=r.id where v.id=$1",
            version,
        )
        assert row is not None
        assert (row["text_enc"], row["key_version"], row["revision_number"]) == (
            b"original ciphertext",
            7,
            1,
        )
        assert (
            await conn.fetchval(
                "select resume_revision_id from app.sessions where id=$1",
                sid,
            )
            is None
        )
        assert await conn.fetchval(
            "select expires_at=started_at+interval '30 days' from app.sessions where id=$1",
            sid,
        )
        await conn.execute(
            "update app.resume_versions set text_enc=$2,content_sha256='changed' where id=$1",
            version,
            b"replacement ciphertext",
        )
        assert (
            await conn.fetchval(
                "select text_enc from app.resume_revisions where id=$1",
                row["id"],
            )
            == b"original ciphertext"
        )
        assert (
            await conn.fetchval(
                "select count(*) from app.resume_revisions where resume_version_id=$1",
                version,
            )
            == 2
        )
    finally:
        await conn.close()
        await admin.execute(f'drop database "{name}" with(force)')
        await admin.close()
