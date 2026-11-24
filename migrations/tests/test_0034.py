"""Upgrade real pre-revision rows without inventing historical provenance."""

from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from copilot_core.migrate import apply, load_migrations
from copilot_core.teststack import MIGRATIONS, admin_url

pytestmark = pytest.mark.stack


async def test_0028_upgrade_with_extracted_work_logs_preserves_sources_and_constraints(
    tmp_path: Path,
):
    """Exercise the deployed upgrade path, including nonempty extraction backfills."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for path in MIGRATIONS.glob("*.sql"):
        if path.name[:4] <= "0028":
            (legacy / path.name).write_bytes(path.read_bytes())
    admin = await asyncpg.connect(admin_url())
    name = f"copilot_populated_source_upgrade_{uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    url = urlunsplit(urlsplit(admin_url())._replace(path=f"/{name}"))
    conn = await asyncpg.connect(url)
    try:
        await apply(url, legacy)
        owner, other = uuid4(), uuid4()
        employer, second_employer, foreign_employer = uuid4(), uuid4(), uuid4()
        extracted, stale, unprocessed, foreign_log = (uuid4() for _ in range(4))
        resume, foreign_resume, document, foreign_document, chunk, session = (
            uuid4() for _ in range(6)
        )
        for uid in (owner, other):
            await conn.execute(
                "insert into app.users(id,email,display_name,settings) "
                "values($1,$2,'Legacy','{\"retention_days\":30}')",
                uid,
                f"{uid}@example.com",
            )
        for eid, uid in ((employer, owner), (second_employer, owner), (foreign_employer, other)):
            await conn.execute(
                "insert into app.employers(id,user_id,name) values($1,$2,'Legacy employer')",
                eid,
                uid,
            )
        for log_id, uid, eid, day, content_hash, extracted_hash in (
            (extracted, owner, employer, date(2026, 10, 1), "current", "current"),
            (stale, owner, employer, date(2026, 10, 2), "changed", "previous"),
            (unprocessed, owner, second_employer, date(2026, 10, 1), "unprocessed", None),
            (foreign_log, other, foreign_employer, date(2026, 10, 1), "foreign", "foreign"),
        ):
            await conn.execute(
                "insert into app.work_logs(id,user_id,employer_id,day,text_enc,key_version,"
                "content_sha256,extracted_sha256,status) values($1,$2,$3,$4,$5,7,$6,$7,$8)",
                log_id,
                uid,
                eid,
                day,
                b"preserved work log ciphertext",
                content_hash,
                extracted_hash,
                "extracted" if extracted_hash else "saved",
            )
        for did, uid, eid, log_id in (
            (document, owner, employer, extracted),
            (foreign_document, other, foreign_employer, foreign_log),
        ):
            await conn.execute(
                "insert into app.company_documents(id,user_id,employer_id,work_log_id,name_enc,"
                "key_version,s3_key,sha256,content_type,size_bytes,status) "
                "values($1,$2,$3,$4,$5,7,$6,'file-hash','text/plain',123,'ingested')",
                did,
                uid,
                eid,
                log_id,
                b"preserved filename ciphertext",
                f"existing/company/{did}",
            )
        await conn.execute(
            "insert into app.knowledge_chunks(id,user_id,employer_id,document_id,ord,body_enc,"
            "key_version) values($1,$2,$3,$4,0,$5,7)",
            chunk,
            owner,
            employer,
            document,
            b"preserved chunk ciphertext",
        )
        for rid, uid in ((resume, owner), (foreign_resume, other)):
            await conn.execute(
                "insert into app.resume_versions(id,user_id,kind,label,source,text_enc,key_version,"
                "content_sha256,file_s3_key,file_name_enc,file_content_type,original_text_enc,"
                "original_text_key_version,original_content_sha256) "
                "values($1,$2,'base','Legacy resume','career_os',$3,7,'resume-hash',$4,$5,"
                "'application/pdf',$6,7,'resume-hash')",
                rid,
                uid,
                b"preserved resume ciphertext",
                f"existing/resume/{rid}",
                b"preserved resume filename ciphertext",
                b"preserved original extraction ciphertext",
            )
        await conn.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at,"
            "billed_seconds) values($1,$2,'interview',false,now()-interval '10 days',"
            "now()-interval '9 days',17)",
            session,
            owner,
        )
        tables = (
            "resume_versions",
            "work_logs",
            "company_documents",
            "knowledge_chunks",
            "sessions",
        )
        before = {
            table: [dict(row) for row in await conn.fetch(f"select * from app.{table} order by id")]
            for table in tables
        }

        # The old migration failed here with ObjectInUseError on extracted work_logs.
        upgraded = await apply(url, MIGRATIONS)
        assert len(upgraded) == 7
        assert await conn.fetchval("select max(version) from public.schema_migrations") == "0035"
        assert await apply(url, MIGRATIONS) == []
        for table, expected in before.items():
            columns = ",".join(expected[0])
            actual = await conn.fetch(f"select {columns} from app.{table} order by id")
            assert [dict(row) for row in actual] == expected

        logs = {
            row["id"]: row
            for row in await conn.fetch(
                "select id,current_revision_id,extracted_revision_id from app.work_logs"
            )
        }
        for log_id in (extracted, foreign_log):
            assert logs[log_id]["extracted_revision_id"] == logs[log_id]["current_revision_id"]
        for log_id in (stale, unprocessed):
            assert logs[log_id]["extracted_revision_id"] is None
        assert await conn.fetchval("select count(*) from app.work_log_revisions") == 4
        assert await conn.fetchval("select count(*) from app.company_document_revisions") == 2
        assert await conn.fetchval("select count(*) from app.resume_revisions") == 2
        assert (
            await conn.fetchval(
                "select count(*) from app.resume_revision_extractions "
                "where text_enc=$1 and key_version=7",
                b"preserved original extraction ciphertext",
            )
            == 2
        )
        assert await conn.fetchval(
            "select k.document_revision_id=d.current_revision_id from app.knowledge_chunks k "
            "join app.company_documents d on d.id=k.document_id where k.id=$1",
            chunk,
        )
        assert await conn.fetchval(
            "select resume_revision_id is null and expires_at=started_at+interval '30 days' "
            "from app.sessions where id=$1",
            session,
        )
        assert await conn.fetchval(
            "select not billable and payer_account_id is null and duration_microseconds=17000000 "
            "from app.legacy_usage_estimates where session_id=$1",
            session,
        )

        constraints = await conn.fetch(
            "select conname,convalidated,condeferrable,condeferred from pg_constraint "
            "where connamespace='app'::regnamespace and conname=any($1::text[])",
            [
                "resume_current_revision_fk",
                "work_log_current_revision_fk",
                "company_document_current_revision_fk",
                "work_log_extracted_revision_fk",
            ],
        )
        assert len(constraints) == 4
        assert all(
            row["convalidated"] and row["condeferrable"] and row["condeferred"]
            for row in constraints
        )
        assert (
            await conn.fetchval(
                "select count(*) from pg_class where relnamespace='app'::regnamespace "
                "and relrowsecurity and not relforcerowsecurity"
            )
            == 0
        )
        for wrong_log in (stale, foreign_log):
            with pytest.raises(
                asyncpg.ForeignKeyViolationError, match="work_log_extracted_revision_fk"
            ):
                async with conn.transaction():
                    await conn.execute(
                        "update app.work_logs set extracted_revision_id=$2 where id=$1",
                        extracted,
                        logs[wrong_log]["current_revision_id"],
                    )
        with pytest.raises(asyncpg.ForeignKeyViolationError, match="document_log_employer_fk"):
            await conn.execute(
                "update app.company_documents set work_log_id=$2 where id=$1",
                document,
                unprocessed,
            )
        with pytest.raises(asyncpg.ForeignKeyViolationError, match="chunk_document_employer_fk"):
            await conn.execute(
                "update app.knowledge_chunks set employer_id=$2 where id=$1", chunk, second_employer
            )

        # Deferred pointers still permit source updates while old extraction provenance stays fixed.
        await conn.execute(
            "update app.work_logs set text_enc=$2,content_sha256='next' where id=$1",
            extracted,
            b"new work log ciphertext",
        )
        changed = await conn.fetchrow(
            "select current_revision_id,extracted_revision_id from app.work_logs where id=$1",
            extracted,
        )
        assert changed is not None
        assert changed["current_revision_id"] != logs[extracted]["current_revision_id"]
        assert changed["extracted_revision_id"] == logs[extracted]["current_revision_id"]
        assert (
            await conn.fetchval(
                "select text_enc from app.work_log_revisions where id=$1",
                logs[extracted]["current_revision_id"],
            )
            == b"preserved work log ciphertext"
        )
        assert (
            await conn.fetchval(
                "select count(*) from app.work_log_revisions where work_log_id=$1", extracted
            )
            == 2
        )
    finally:
        await conn.close()
        await admin.execute(f'drop database "{name}" with(force)')
        await admin.close()


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
