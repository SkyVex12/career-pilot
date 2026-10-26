"""Migration 0008: jobs become applications in place, the newest ingested resume becomes the
base resume version and every resume node belongs to it, interviews and sessions keep their
references, and code from before 0008 still reads and writes `app.jobs`. Needs the Postgres
from `docker compose` (marker `stack`)."""

import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.migrate import DEFAULT_DATABASE_URL, apply

pytestmark = pytest.mark.stack

MIGRATIONS = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database() -> AsyncIterator[str]:
    admin_url = os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    try:
        admin = await asyncpg.connect(admin_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.fail(f"cannot reach Postgres at {admin_url} ({e})", pytrace=False)
    name = f"copilot_m8_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    try:
        yield urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    finally:
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


async def node(
    c: asyncpg.Connection, uid: uuid.UUID, source: dict[str, str], status: str
) -> uuid.UUID:
    nid = uuid.uuid4()
    await c.execute(
        "insert into app.memory_nodes (id, user_id, kind, domain, title, summary, source, status)"
        " values ($1, $2, 'story', 'career', 'Ledger migration', 'Moved the ledger', $3, $4)",
        nid, uid, json.dumps(source), status,
    )  # fmt: skip
    return nid


async def test_existing_jobs_interviews_and_resume_move_into_the_new_structure(
    database: str, tmp_path: Path
) -> None:
    for sql in sorted(MIGRATIONS.glob("000[1-7]_*.sql")):
        shutil.copy(sql, tmp_path / sql.name)
    await apply(database, tmp_path)
    c = await asyncpg.connect(database)
    try:
        a, b = uuid.uuid4(), uuid.uuid4()
        for uid in (a, b):
            await c.execute(
                "insert into app.users (id, email, display_name) values ($1, $2, 'U')",
                uid, f"{uid}@example.com",
            )  # fmt: skip
        job_interviewed, job_applied, job_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for jid, uid, company in ((job_interviewed, a, "Acme"), (job_applied, a, "Globex"),
                                  (job_b, b, "Initech")):  # fmt: skip
            await c.execute(
                "insert into app.jobs (id, user_id, company, title, description, url)"
                " values ($1, $2, $3, 'Backend Engineer', 'Build payment services', 'https://x')",
                jid, uid, company,
            )  # fmt: skip
        interview = uuid.uuid4()
        await c.execute(
            "insert into app.interviews (id, user_id, job_id, round, starts_at)"
            " values ($1, $2, $3, 'technical', $4)",
            interview, a, job_interviewed, NOW,
        )  # fmt: skip
        session = uuid.uuid4()
        await c.execute(
            "insert into app.sessions (id, user_id, mode, job_id, interview_id, store_transcript,"
            " started_at) values ($1, $2, 'interview', $3, $4, false, $5)",
            session, a, job_interviewed, interview, NOW,
        )  # fmt: skip
        # User A uploaded two resumes (the newer one counts) and one that could not be read.
        old_doc, new_doc, failed_doc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for doc, status, at in ((old_doc, "ingested", NOW - timedelta(days=9)),
                                (new_doc, "ingested", NOW - timedelta(days=2)),
                                (failed_doc, "failed", NOW)):  # fmt: skip
            await c.execute(
                "insert into app.documents (id, user_id, kind, s3_key, sha256, status, created_at)"
                " values ($1, $2, 'resume', $3, $4, $5, $6)",
                doc, a, f"uploads/{a}/documents/{doc}", doc.hex + doc.hex, status, at,
            )  # fmt: skip
        archived = await node(c, a, {"type": "resume", "ref": str(old_doc)}, "archived")
        active = await node(c, a, {"type": "resume", "ref": str(new_doc)}, "active")
        from_session = await node(c, a, {"type": "session", "ref": str(session)}, "active")

        # Up to 0008 only, so later migrations do not change what this checks.
        shutil.copy(MIGRATIONS / "0008_applications_resumes.sql", tmp_path)
        assert await apply(database, tmp_path) == ["0008_applications_resumes"]

        rows = {
            r["id"]: r
            for r in await c.fetch(
                "select id, user_id, company, role, jd_text, stage, url,"
                " resume_version_id from app.applications"
            )
        }
        assert set(rows) == {job_interviewed, job_applied, job_b}
        assert rows[job_interviewed]["stage"] == "interview"  # it had an interview
        assert rows[job_applied]["stage"] == "applied"
        assert (rows[job_applied]["role"], rows[job_applied]["jd_text"]) == (
            "Backend Engineer", "Build payment services")  # fmt: skip
        assert all(r["resume_version_id"] is None for r in rows.values())  # automatic pairing

        # Interviews and sessions still point at the same ids.
        assert await c.fetchval("select job_id from app.interviews where id = $1", interview) == (
            job_interviewed)  # fmt: skip
        assert await c.fetchval("select job_id from app.sessions where id = $1", session) == (
            job_interviewed)  # fmt: skip

        # The newest ingested resume is the base, with that document's id.
        versions = await c.fetch("select id, user_id, kind, document_id, status, label"
                                 " from app.resume_versions")  # fmt: skip
        found = [
            (v["id"], v["user_id"], v["kind"], v["document_id"], v["status"]) for v in versions
        ]
        assert found == [(new_doc, a, "base", new_doc, "extracted")]
        assert await c.fetchval("select resume_version_id from app.documents where id = $1",
                                new_doc) == new_doc  # fmt: skip
        by_node: dict[uuid.UUID, uuid.UUID | None] = {
            r["id"]: r["resume_version_id"]
            for r in await c.fetch("select id, resume_version_id from app.memory_nodes")
        }
        assert by_node[active] == new_doc
        assert by_node[archived] == new_doc
        assert by_node[from_session] is None  # not from a resume: no version

        # Every application pairs with the base automatically; B has no resume.
        assert await c.fetchval("select app.paired_resume($1)", job_applied) == new_doc
        assert await c.fetchval("select app.paired_resume($1)", job_b) is None

        # Code from before 0008 reads and writes app.jobs as it did.
        old = await c.fetchrow("select company, title, description from app.jobs where id = $1",
                               job_applied)  # fmt: skip
        assert old is not None
        assert tuple(old) == ("Globex", "Backend Engineer", "Build payment services")
        newer = uuid.uuid4()
        await c.execute(
            "insert into app.jobs (id, user_id, company, title, description)"
            " values ($1, $2, 'Hooli', 'SRE', 'Keep it up') on conflict (id) do nothing",
            newer, a,
        )  # fmt: skip
        assert await c.fetchval("select stage from app.applications where id = $1", newer) == (
            "applied")  # fmt: skip

        # A tailored version belongs to one application: another may not pair with it.
        tailored = uuid.uuid4()
        await c.execute(
            "insert into app.resume_versions (id, user_id, kind, application_id, label, source,"
            " document_id, content_sha256) values ($1, $2, 'tailored', $3, 'For Acme', 'file',"
            " $4, $5)",
            tailored, a, job_interviewed, old_doc, "0" * 64,
        )  # fmt: skip
        assert await c.fetchval("select app.paired_resume($1)", job_interviewed) == tailored
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("update app.applications set resume_version_id = $1 where id = $2",
                            tailored, job_applied)  # fmt: skip
        await c.execute("update app.applications set resume_version_id = $1 where id = $2",
                        new_doc, job_interviewed)  # the base: allowed  # fmt: skip
        assert await c.fetchval("select app.paired_resume($1)", job_interviewed) == new_doc
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute("update app.resume_versions set application_id = $1 where id = $2",
                            job_applied, tailored)  # fmt: skip
        with pytest.raises(asyncpg.UniqueViolationError):  # one base per user
            await c.execute(
                "insert into app.resume_versions (id, user_id, kind, label, source, document_id,"
                " content_sha256) values ($1, $2, 'base', 'Another', 'file', $3, $4)",
                uuid.uuid4(), a, old_doc, "0" * 64,
            )  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):  # a resume node names its version
            await node(c, a, {"type": "resume", "ref": "x"}, "active")

        # Deleting the application deletes its tailored versions and their nodes.
        await c.execute(
            "update app.memory_nodes set resume_version_id = $1 where id = $2", tailored, active
        )
        await c.execute("delete from app.applications where id = $1", job_interviewed)
        assert await c.fetchval("select count(*) from app.resume_versions where id = $1",
                                tailored) == 0  # fmt: skip
        assert await c.fetchval("select count(*) from app.memory_nodes where id = $1", active) == 0

        forced = await c.fetch(
            "select relname, relforcerowsecurity from pg_class where relname in"
            " ('applications', 'resume_versions', 'memory_nodes', 'documents', 'integrations',"
            " 'interviews') and relnamespace = 'app'::regnamespace"
        )
        assert {r["relname"]: r["relforcerowsecurity"] for r in forced} == dict.fromkeys(
            ("applications", "resume_versions", "memory_nodes", "documents", "integrations",
             "interviews"), True)  # fmt: skip
    finally:
        await c.close()
