"""Row-level security test (ARCHITECTURE.md, "Testing and evals").

Creates two users with a row in every user table, then checks for every app role that
user A can neither read nor write user B's rows: reads return only A's rows, inserts
for B are refused, updates and deletes of B's rows touch nothing, and A cannot move its
own rows to B. Roles without a grant on a table must get "permission denied".

Needs the local Postgres (docker compose up -d --wait). Each run migrates a fresh
throwaway database and drops it afterwards. Run with: uv run pytest -m stack
"""

import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.migrate import DEFAULT_DATABASE_URL, apply

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]

MIGRATIONS = Path(__file__).resolve().parent.parent
INSUFFICIENT_PRIVILEGE = "42501"  # both "permission denied" and RLS policy violations

APP_ROLES = ["app_api", "app_worker", "app_gateway"]
# Tables each role may read, from the role table in ARCHITECTURE.md.
# resume_versions: metadata columns only (a column grant), never the text or file.
GATEWAY_TABLES = {"users", "applications", "jobs", "interviews", "memory_nodes", "memory_edges",
                  "employers", "resume_versions", "profiles", "integrations"}  # fmt: skip
# integrations: the Career-OS link's status only (a column grant), never the token.
# Tables the gateway reads through a table-wide grant (the other two have column grants).
GATEWAY_TABLE_GRANTS = GATEWAY_TABLES - {"resume_versions", "integrations"}
# Tables without row-level security, on purpose (system table, workers only).
NO_RLS_TABLES = {"job_runs"}


@dataclass
class World:
    """One user and the parent rows other tables point at."""

    user_id: uuid.UUID = field(default_factory=uuid.uuid4)
    job_id: uuid.UUID = field(default_factory=uuid.uuid4)
    interview_id: uuid.UUID = field(default_factory=uuid.uuid4)
    node_ids: tuple[uuid.UUID, uuid.UUID] = field(
        default_factory=lambda: (uuid.uuid4(), uuid.uuid4())
    )
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    question_id: uuid.UUID = field(default_factory=uuid.uuid4)
    employer_id: uuid.UUID = field(default_factory=uuid.uuid4)
    work_log_id: uuid.UUID = field(default_factory=uuid.uuid4)
    company_document_id: uuid.UUID = field(default_factory=uuid.uuid4)
    resume_version_id: uuid.UUID = field(default_factory=uuid.uuid4)
    document_id: uuid.UUID = field(default_factory=uuid.uuid4)


NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
RELS = ["part_of", "demonstrates", "used", "worked_with", "about", "follows_up"]
PROVIDERS = ["github", "jira", "slack", "google_calendar", "microsoft_calendar"]


def _id(w: World, n: int, canonical: uuid.UUID) -> uuid.UUID:
    return canonical if n == 0 else uuid.uuid4()


# A valid row for each user table. n == 0 is the seeded row; n > 0 makes a new row with
# fresh keys. Order matters: parents come before the tables that reference them.
ROWS: dict[str, Callable[[World, int], dict[str, Any]]] = {
    "users": lambda w, n: {
        "id": w.user_id if n == 0 else uuid.uuid4(),
        "email": f"{uuid.uuid4().hex}@example.com",
        "display_name": "Test user",
    },
    # The profile's own users row is not needed: profiles.id has no foreign key.
    "profiles": lambda w, n: {
        "id": uuid.uuid4(),
        "user_id": w.user_id,
        "career_os_user_id": f"u{n}",
        "name": f"Profile {n}",
    },
    "employers": lambda w, n: {
        "id": _id(w, n, w.employer_id),
        "user_id": w.user_id,
        "name": "Fernhill Logistics",
    },
    "work_logs": lambda w, n: {
        "id": _id(w, n, w.work_log_id),
        "user_id": w.user_id,
        "employer_id": w.employer_id,
        "day": date(2026, 10, 1) + timedelta(days=n),
        "text_enc": b"ciphertext",
        "key_version": 1,
        "content_sha256": "0" * 64,
    },
    "company_documents": lambda w, n: {
        "id": _id(w, n, w.company_document_id),
        "user_id": w.user_id,
        "employer_id": w.employer_id,
        "work_log_id": w.work_log_id,
        "name_enc": b"ciphertext",
        "key_version": 1,
        "s3_key": f"uploads/{w.user_id}/employers/{w.employer_id}/{n}",
        "sha256": "0" * 64,
        "content_type": "text/markdown",
        "size_bytes": 10,
    },
    "user_keys": lambda w, n: {"user_id": w.user_id, "version": n + 1, "encrypted_dek": b"dek"},
    "applications": lambda w, n: {
        "id": _id(w, n, w.job_id),
        "user_id": w.user_id,
        "company": "Acme",
        "role": "Engineer",
        "jd_text": "Build things",
    },
    "interviews": lambda w, n: {
        "id": _id(w, n, w.interview_id),
        "user_id": w.user_id,
        "job_id": w.job_id,
        "round": "technical",
        "starts_at": NOW,
    },
    "memory_nodes": lambda w, n: {
        "id": w.node_ids[n] if n < 2 else uuid.uuid4(),
        "user_id": w.user_id,
        "kind": "commitment",
        "domain": "workplace",
        "employer_id": w.employer_id,
        "title": f"Node {n}",
        "summary": "A commitment",
        "body": {"due_date": "2026-10-06", "state": "open"},
        "source": {"type": "session", "ref": "test"},
    },
    "memory_edges": lambda w, n: {
        "user_id": w.user_id,
        "src": w.node_ids[0],
        "rel": RELS[n],
        "dst": w.node_ids[1],
    },
    "memory_proposals": lambda w, n: {
        "id": uuid.uuid4(),
        "user_id": w.user_id,
        "source": {"type": "company_document"},
        "action": "create",
        "payload": {},
        "employer_id": w.employer_id,
        "company_document_id": w.company_document_id,
    },
    "sessions": lambda w, n: {
        "id": _id(w, n, w.session_id),
        "user_id": w.user_id,
        "mode": "meeting",
        "store_transcript": True,
        "started_at": NOW,
        "employer_id": w.employer_id,
    },
    "transcript_segments": lambda w, n: {
        "session_id": w.session_id,
        "channel": "them",
        "seg_id": n,
        "user_id": w.user_id,
        "start_ms": 0,
        "end_ms": 1000,
        "text_enc": b"ciphertext",
        "key_version": 1,
    },
    "questions": lambda w, n: {
        "id": _id(w, n, w.question_id),
        "session_id": w.session_id,
        "user_id": w.user_id,
        "text_enc": b"ciphertext",
        "key_version": 1,
        "type": "technical",
        "addressed_to_user": True,
        "speculative_kept": True,
        "detected_at": NOW,
    },
    "answers": lambda w, n: {
        "id": uuid.uuid4(),
        "question_id": w.question_id,
        "user_id": w.user_id,
        "pass": "fast",
        "model": "haiku-4.5",
        "tier": "standard",
        "latency_ms": {"first_token": 500},
    },
    "session_outputs": lambda w, n: {
        "id": uuid.uuid4(),
        "session_id": w.session_id,
        "user_id": w.user_id,
        "kind": "summary",
        "content_enc": b"ciphertext",
        "key_version": 1,
    },
    "documents": lambda w, n: {
        "id": _id(w, n, w.document_id),
        "user_id": w.user_id,
        "kind": "resume",
        "s3_key": f"uploads/{w.user_id}/{n}",
        "sha256": "0" * 64,
    },
    # The seeded version is the base; new ones are tailored versions of the seeded application.
    "resume_versions": lambda w, n: {
        "id": _id(w, n, w.resume_version_id),
        "user_id": w.user_id,
        "kind": "base" if n == 0 else "tailored",
        "application_id": None if n == 0 else w.job_id,
        "label": f"Resume {n}",
        "source": "file",
        "document_id": w.document_id,
        "content_sha256": "0" * 64,
    },
    "integrations": lambda w, n: {
        "user_id": w.user_id,
        "provider": PROVIDERS[n],
        "token_enc": b"ciphertext",
        "key_version": 1,
        "scopes": ["repo"],
    },
    "usage_daily": lambda w, n: {
        "user_id": w.user_id,
        "day": date(2026, 10, 1) + timedelta(days=n),
        "live_seconds": 60,
    },
}
USER_TABLES = list(ROWS)
# Views over user tables. They use security_invoker, so the base table's RLS applies;
# their grants are their own (the gateway gets none).
VIEWS = ["commitments", "jobs"]


def tenant_column(table: str) -> str:
    return "id" if table == "users" else "user_id"


def can_read(role: str, table: str) -> bool:
    return role != "app_gateway" or table in GATEWAY_TABLES


def can_write(role: str) -> bool:
    return role != "app_gateway"


async def insert(conn: asyncpg.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    params = ", ".join(f"${i}" for i in range(1, len(row) + 1))
    await conn.execute(f"insert into app.{table} ({cols}) values ({params})", *row.values())


async def json_codecs(conn: asyncpg.Connection) -> None:
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


@dataclass
class Db:
    url: str
    conn: asyncpg.Connection
    a: World
    b: World


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def db() -> AsyncIterator[Db]:
    admin_url = os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    try:
        admin = await asyncpg.connect(admin_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.fail(
            f"cannot reach Postgres at {admin_url} ({e}). Start the local stack with "
            "`docker compose up -d --wait`, or deselect with -m 'not stack'.",
            pytrace=False,
        )
    name = f"copilot_rls_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    url = urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    conn: asyncpg.Connection | None = None
    try:
        await apply(url, MIGRATIONS)
        conn = await asyncpg.connect(url)
        await json_codecs(conn)
        a, b = World(), World()
        async with conn.transaction():  # seeded as the superuser, which bypasses RLS
            for w in (a, b):
                for table, make in ROWS.items():
                    await insert(conn, table, make(w, 0))
                    if table == "memory_nodes":
                        await insert(conn, table, make(w, 1))
        yield Db(url, conn, a, b)
    finally:
        if conn is not None:
            await conn.close()
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


class Rollback(Exception):
    pass


async def as_user(
    db: Db, role: str, user: World, sql: str, *args: Any
) -> tuple[str | None, list[asyncpg.Record], str | None]:
    """Run one statement as `role` for `user` in a transaction that is always rolled back.

    Returns (command status, rows, SQLSTATE of the error or None).
    """
    status: str | None = None
    rows: list[asyncpg.Record] = []
    try:
        async with db.conn.transaction():
            await db.conn.execute(f"set local role {role}")
            await db.conn.execute("select set_config('app.user_id', $1, true)", str(user.user_id))
            if sql.lstrip().lower().startswith("select"):
                rows = await db.conn.fetch(sql, *args)
            else:
                status = await db.conn.execute(sql, *args)
            raise Rollback
    except Rollback:
        return status, rows, None
    except asyncpg.PostgresError as e:
        return None, [], e.sqlstate


@pytest.mark.parametrize("role", APP_ROLES)
@pytest.mark.parametrize("table", [*USER_TABLES, *VIEWS])
async def test_reads_see_only_own_rows(db: Db, role: str, table: str) -> None:
    col = tenant_column(table)
    _, rows, err = await as_user(db, role, db.a, f"select {col} as tenant from app.{table}")
    if not can_read(role, table):
        assert err == INSUFFICIENT_PRIVILEGE, f"{role} should not be able to read {table}"
        return
    assert err is None
    tenants = {r["tenant"] for r in rows}
    assert tenants == {db.a.user_id}, f"{role} read other users' rows from {table}"

    _, rows, err = await as_user(
        db, role, db.a, f"select 1 from app.{table} where {col} = $1", db.b.user_id
    )
    assert err is None
    assert rows == [], f"{role} read user B's rows from {table} with an explicit filter"


@pytest.mark.parametrize("role", APP_ROLES)
@pytest.mark.parametrize("table", USER_TABLES)
async def test_cannot_insert_for_another_user(db: Db, role: str, table: str) -> None:
    row = ROWS[table](db.b, 2)  # a valid new row that belongs to user B
    cols = ", ".join(row)
    params = ", ".join(f"${i}" for i in range(1, len(row) + 1))
    _, _, err = await as_user(
        db, role, db.a, f"insert into app.{table} ({cols}) values ({params})", *row.values()
    )
    assert err == INSUFFICIENT_PRIVILEGE, f"{role} inserted a row for user B into {table}"


@pytest.mark.parametrize("role", APP_ROLES)
@pytest.mark.parametrize("table", USER_TABLES)
async def test_cannot_update_or_delete_another_users_rows(db: Db, role: str, table: str) -> None:
    col = tenant_column(table)
    for sql, verb in (
        (f"update app.{table} set {col} = {col} where {col} = $1", "UPDATE"),
        (f"delete from app.{table} where {col} = $1", "DELETE"),
    ):
        status, _, err = await as_user(db, role, db.a, sql, db.b.user_id)
        if can_write(role):
            assert err is None
            assert status == f"{verb} 0", f"{role} changed user B's rows in {table}: {status}"
        else:
            assert err == INSUFFICIENT_PRIVILEGE, f"{role} should not be able to write {table}"


@pytest.mark.parametrize("role", APP_ROLES)
@pytest.mark.parametrize("table", USER_TABLES)
async def test_cannot_move_own_rows_to_another_user(db: Db, role: str, table: str) -> None:
    col = tenant_column(table)
    sql = f"update app.{table} set {col} = $1 where {col} = $2"
    status, _, err = await as_user(db, role, db.a, sql, db.b.user_id, db.a.user_id)
    assert err == INSUFFICIENT_PRIVILEGE, f"{role} moved a row in {table} to user B: {status}"


@pytest.mark.parametrize("role", APP_ROLES)
async def test_queries_fail_closed_without_a_user(db: Db, role: str) -> None:
    """Without app.user_id set, reading a user table raises instead of returning rows."""
    table = "memory_nodes"
    fresh = await asyncpg.connect(db.url)  # app.user_id was never set on this connection
    try:
        async with fresh.transaction():
            await fresh.execute(f"set local role {role}")
            with pytest.raises(asyncpg.PostgresError):
                await fresh.fetch(f"select * from app.{table}")
        # After a transaction-scoped set_config ends, the setting is '' (not missing):
        # that must fail too, so a pooled connection never falls back to "all users".
        async with fresh.transaction():
            await fresh.execute("select set_config('app.user_id', $1, true)", str(db.a.user_id))
        async with fresh.transaction():
            await fresh.execute(f"set local role {role}")
            with pytest.raises(asyncpg.PostgresError):
                await fresh.fetch(f"select * from app.{table}")
    finally:
        await fresh.close()


async def test_every_user_table_has_forced_rls_and_is_tested(db: Db) -> None:
    rows = await db.conn.fetch(
        """
        select c.relname, c.relrowsecurity, c.relforcerowsecurity,
               exists (select 1 from pg_policy p
                       where p.polrelid = c.oid and p.polname = 'tenant_isolation') as has_policy
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'app' and c.relkind = 'r'
          and (c.relname = 'users' or exists (
                select 1 from pg_attribute a
                where a.attrelid = c.oid and a.attname = 'user_id' and not a.attisdropped))
        """
    )
    found = {r["relname"] for r in rows} - NO_RLS_TABLES
    assert found == set(USER_TABLES), (
        "user tables and this test's ROWS differ; add new tables to ROWS (and give them RLS)"
    )
    for r in rows:
        if r["relname"] in NO_RLS_TABLES:
            continue
        assert r["relrowsecurity"], f"{r['relname']}: RLS not enabled"
        assert r["relforcerowsecurity"], f"{r['relname']}: RLS not forced"
        assert r["has_policy"], f"{r['relname']}: no tenant_isolation policy"


async def test_role_attributes_and_privileges(db: Db) -> None:
    roles = {
        r["rolname"]: r
        for r in await db.conn.fetch(
            "select rolname, rolsuper, rolbypassrls, rolcanlogin from pg_roles"
            " where rolname like 'app\\_%'"
        )
    }
    assert set(roles) >= {"app_owner", "app_api", "app_gateway", "app_worker", "app_system"}
    for name in APP_ROLES:
        assert not roles[name]["rolsuper"]
        assert not roles[name]["rolbypassrls"], f"{name} must not bypass RLS"
    assert roles["app_system"]["rolbypassrls"]

    owners = await db.conn.fetch(
        "select c.relname, pg_get_userbyid(c.relowner) as owner from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = 'app' and c.relkind in ('r', 'v')"
    )
    assert {o["owner"] for o in owners} == {"app_owner"}

    # Gateway: SELECT on exactly its five tables, and no writes anywhere.
    grants = await db.conn.fetch(
        "select table_name, privilege_type from information_schema.role_table_grants"
        " where grantee = 'app_gateway' and table_schema = 'app'"
    )
    assert {(g["table_name"], g["privilege_type"]) for g in grants} == {
        (t, "SELECT") for t in GATEWAY_TABLE_GRANTS
    }
    columns = await db.conn.fetch(
        "select column_name, privilege_type from information_schema.column_privileges"
        " where grantee = 'app_gateway' and table_schema = 'app' and table_name = 'resume_versions'"
    )
    assert {c["privilege_type"] for c in columns} == {"SELECT"}
    readable = {c["column_name"] for c in columns}
    assert not readable & {"text_enc", "file_s3_key", "file_name_enc", "document_id"}
    columns = await db.conn.fetch(
        "select column_name, privilege_type from information_schema.column_privileges"
        " where grantee = 'app_gateway' and table_schema = 'app' and table_name = 'integrations'"
    )
    assert {(c["column_name"], c["privilege_type"]) for c in columns} == {
        ("user_id", "SELECT"),
        ("provider", "SELECT"),
        ("status", "SELECT"),
    }
    # job_runs: workers and system jobs only.
    for role, allowed in (("app_api", False), ("app_gateway", False), ("app_worker", True)):
        _, _, err = await as_user(db, role, db.a, "select 1 from app.job_runs")
        assert (err is None) == allowed, f"{role} job_runs access should be {allowed}"


# Cross-user references ---------------------------------------------------------------
# Foreign keys between user-owned tables are composite on (id, user_id), so a row can
# only point at rows of the same user. Row-level security alone does not stop this:
# foreign key checks do not apply RLS.

FOREIGN_KEY_VIOLATION = "23503"
CHECK_VIOLATION = "23514"

# (child table, reference column, parent table) for every foreign key between user tables.
REFERENCES = [
    ("interviews", "job_id", "applications"),
    ("memory_edges", "src", "memory_nodes"),
    ("memory_edges", "dst", "memory_nodes"),
    ("memory_proposals", "target_node_id", "memory_nodes"),
    ("memory_proposals", "session_id", "sessions"),
    ("sessions", "job_id", "applications"),
    ("sessions", "interview_id", "interviews"),
    ("transcript_segments", "session_id", "sessions"),
    ("questions", "session_id", "sessions"),
    ("answers", "question_id", "questions"),
    ("session_outputs", "session_id", "sessions"),
    ("work_logs", "employer_id", "employers"),
    ("company_documents", "employer_id", "employers"),
    ("company_documents", "work_log_id", "work_logs"),
    ("memory_nodes", "employer_id", "employers"),
    ("memory_proposals", "employer_id", "employers"),
    ("memory_proposals", "company_document_id", "company_documents"),
    ("sessions", "employer_id", "employers"),
    ("resume_versions", "application_id", "applications"),
    ("resume_versions", "document_id", "documents"),
    ("applications", "resume_version_id", "resume_versions"),
    ("memory_nodes", "resume_version_id", "resume_versions"),
    ("documents", "application_id", "applications"),
    ("documents", "resume_version_id", "resume_versions"),
]
WRITE_ROLES = [r for r in APP_ROLES if can_write(r)]


def parent_id(w: World, parent: str) -> uuid.UUID:
    return {
        "applications": w.job_id,
        "resume_versions": w.resume_version_id,
        "documents": w.document_id,
        "interviews": w.interview_id,
        "memory_nodes": w.node_ids[0],
        "sessions": w.session_id,
        "questions": w.question_id,
        "employers": w.employer_id,
        "work_logs": w.work_log_id,
        "company_documents": w.company_document_id,
    }[parent]


def insert_sql(table: str, row: dict[str, Any]) -> str:
    params = ", ".join(f"${i}" for i in range(1, len(row) + 1))
    return f"insert into app.{table} ({', '.join(row)}) values ({params})"


def ref_id(child: str, column: str, parent: str) -> str:
    return f"{child}.{column}->{parent}"


@pytest.mark.parametrize("role", WRITE_ROLES)
@pytest.mark.parametrize(
    ("child", "column", "parent"), REFERENCES, ids=[ref_id(*r) for r in REFERENCES]
)
async def test_cannot_insert_reference_to_another_users_row(
    db: Db, role: str, child: str, column: str, parent: str
) -> None:
    own = ROWS[child](db.a, 3) | {column: parent_id(db.a, parent)}
    _, _, err = await as_user(db, role, db.a, insert_sql(child, own), *own.values())
    assert err is None, f"control insert into {child} failed ({err}); the test row is broken"

    other = ROWS[child](db.a, 3) | {column: parent_id(db.b, parent)}
    _, _, err = await as_user(db, role, db.a, insert_sql(child, other), *other.values())
    assert err == FOREIGN_KEY_VIOLATION, (
        f"{role} inserted a {child} row of user A that points at user B's {parent}"
    )


@pytest.mark.parametrize("role", WRITE_ROLES)
@pytest.mark.parametrize(
    ("child", "column", "parent"), REFERENCES, ids=[ref_id(*r) for r in REFERENCES]
)
async def test_cannot_repoint_reference_to_another_users_row(
    db: Db, role: str, child: str, column: str, parent: str
) -> None:
    # transcript_segments' key includes session_id; move seg_id so the update cannot collide
    # with user B's seeded segment, which would fail on the key before the foreign key check.
    # work_logs is unique per (employer, day) the same way.
    extra = {"transcript_segments": ", seg_id = seg_id + 1000",
             "work_logs": ", day = day + 1000"}.get(child, "")  # fmt: skip
    sql = f"update app.{child} set {column} = $1{extra} where user_id = $2"
    status, _, err = await as_user(db, role, db.a, sql, parent_id(db.b, parent), db.a.user_id)
    # A resume version never moves to another application: its trigger refuses first.
    expected = CHECK_VIOLATION if (child, column) == ("resume_versions", "application_id") else (
        FOREIGN_KEY_VIOLATION)  # fmt: skip
    assert err == expected, (
        f"{role} pointed user A's {child}.{column} at user B's {parent}: {status}"
    )


async def test_cross_user_reference_fails_even_without_rls(db: Db) -> None:
    """The constraint holds for roles that bypass RLS too (here the superuser)."""
    row = ROWS["interviews"](db.a, 3) | {"job_id": db.b.job_id}
    try:
        async with db.conn.transaction():
            await insert(db.conn, "interviews", row)
            raise Rollback
    except Rollback:
        pytest.fail("superuser inserted an interview pointing at another user's job")
    except asyncpg.ForeignKeyViolationError:
        pass


async def test_deleting_a_job_nulls_session_references_but_keeps_the_session(db: Db) -> None:
    """sessions uses `on delete set null (job_id)`: only the reference column is cleared."""
    a = db.a
    try:
        async with db.conn.transaction():
            await db.conn.execute(
                "update app.sessions set job_id = $1, interview_id = $2 where id = $3",
                a.job_id,
                a.interview_id,
                a.session_id,
            )
            await db.conn.execute(
                "delete from app.applications where id = $1", a.job_id
            )  # cascades
            row = await db.conn.fetchrow(
                "select job_id, interview_id, user_id from app.sessions where id = $1",
                a.session_id,
            )
            assert row is not None, "deleting a job deleted the session"
            assert (row["job_id"], row["interview_id"], row["user_id"]) == (None, None, a.user_id)
            raise Rollback
    except Rollback:
        pass


async def test_every_reference_between_user_tables_is_same_user(db: Db) -> None:
    rows = await db.conn.fetch(
        """
        select cl.relname as child, pcl.relname as parent,
               array(select a.attname from unnest(c.conkey) with ordinality k(n, i)
                     join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.n
                     order by k.i)::text[] as cols,
               array(select a.attname from unnest(c.confkey) with ordinality k(n, i)
                     join pg_attribute a on a.attrelid = c.confrelid and a.attnum = k.n
                     order by k.i)::text[] as ref_cols
        from pg_constraint c
        join pg_class cl on cl.oid = c.conrelid
        join pg_class pcl on pcl.oid = c.confrelid
        where c.contype = 'f' and c.connamespace = 'app'::regnamespace
          and pcl.relname <> 'users'
        """
    )
    found: set[tuple[str, str, str]] = set()
    for r in rows:
        cols, ref_cols = list(r["cols"]), list(r["ref_cols"])
        assert "user_id" in cols, f"{r['child']} -> {r['parent']}: foreign key lacks user_id"
        pairs = dict(zip(cols, ref_cols, strict=True))
        assert pairs.pop("user_id") == "user_id", f"{r['child']}: user_id not matched to user_id"
        assert list(pairs.values()) == ["id"], f"{r['child']}: unexpected key {r['ref_cols']}"
        found.add((r["child"], next(iter(pairs)), r["parent"]))
    assert found == set(REFERENCES), "foreign keys and this test's REFERENCES differ"


async def test_deleting_an_employer_removes_its_data_and_only_its_data(db: Db) -> None:
    """ "Delete all data for this company": the cascade takes the company's logs, documents,
    workplace memory, proposals and sessions (with their logs), and nothing of another company
    of the same user or of their career memory."""
    a = db.a
    other = uuid.uuid4()
    career = uuid.uuid4()
    try:
        async with db.conn.transaction():
            await insert(db.conn, "employers", {"id": other, "user_id": a.user_id, "name": "B"})
            await insert(
                db.conn, "memory_nodes", ROWS["memory_nodes"](a, 5) | {"employer_id": other}
            )
            await insert(db.conn, "memory_nodes", ROWS["memory_nodes"](a, 6) | {
                "id": career, "domain": "career", "employer_id": None})  # fmt: skip
            await insert(db.conn, "sessions", ROWS["sessions"](a, 7) | {"employer_id": other})
            await db.conn.execute("delete from app.employers where id = $1", a.employer_id)
            for table, col in (("work_logs", "employer_id"), ("company_documents", "employer_id"),
                               ("memory_nodes", "employer_id"), ("memory_proposals", "employer_id"),
                               ("sessions", "employer_id")):  # fmt: skip
                gone = await db.conn.fetchval(
                    f"select count(*) from app.{table} where {col} = $1", a.employer_id
                )
                assert gone == 0, f"{table} rows of the deleted company remain"
                kept = await db.conn.fetchval(
                    f"select count(*) from app.{table} where {col} = $1", other
                )
                if table in ("memory_nodes", "sessions"):
                    assert kept == 1, f"{table} rows of the other company were deleted"
            # The seeded session belonged to the deleted company: its log went with it.
            assert (
                await db.conn.fetchval(
                    "select count(*) from app.questions where session_id = $1", a.session_id
                )
                == 0
            )
            assert (
                await db.conn.fetchval(
                    "select count(*) from app.memory_nodes where id = $1", career
                )
                == 1
            )
            # User B's company is untouched.
            assert (
                await db.conn.fetchval(
                    "select count(*) from app.memory_nodes where employer_id = $1", db.b.employer_id
                )
                == 2
            )
            raise Rollback
    except Rollback:
        pass


async def test_workplace_nodes_name_their_employer(db: Db) -> None:
    for row in (ROWS["memory_nodes"](db.a, 8) | {"employer_id": None},
                ROWS["memory_nodes"](db.a, 9) | {"domain": "career"}):  # fmt: skip
        with pytest.raises(asyncpg.CheckViolationError):
            async with db.conn.transaction():
                await insert(db.conn, "memory_nodes", row)
