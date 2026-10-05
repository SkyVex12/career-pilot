"""Row-level security test (ARCHITECTURE.md, "Testing and evals").

Creates two users with a row in every user table, then checks for every app role that
user A can neither read nor write user B's rows: reads return only A's rows, inserts
for B are refused, updates and deletes of B's rows touch nothing, and A cannot move its
own rows to B. Roles without a grant on a table must get "permission denied".

Needs the local Postgres (docker compose up -d --wait). Each run migrates a fresh
throwaway database and drops it afterwards. Run with: uv run pytest -m db
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

pytestmark = [pytest.mark.db, pytest.mark.asyncio(loop_scope="session")]

MIGRATIONS = Path(__file__).resolve().parent.parent
INSUFFICIENT_PRIVILEGE = "42501"  # both "permission denied" and RLS policy violations

APP_ROLES = ["app_api", "app_worker", "app_gateway"]
# Tables each role may read, from the role table in ARCHITECTURE.md.
GATEWAY_TABLES = {"users", "jobs", "interviews", "memory_nodes", "memory_edges"}
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
    "user_keys": lambda w, n: {"user_id": w.user_id, "version": n + 1, "encrypted_dek": b"dek"},
    "jobs": lambda w, n: {
        "id": _id(w, n, w.job_id),
        "user_id": w.user_id,
        "company": "Acme",
        "title": "Engineer",
        "description": "Build things",
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
        "domain": "career",
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
        "source": {"type": "session"},
        "action": "create",
        "payload": {},
    },
    "sessions": lambda w, n: {
        "id": _id(w, n, w.session_id),
        "user_id": w.user_id,
        "mode": "meeting",
        "store_transcript": True,
        "started_at": NOW,
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
        "id": uuid.uuid4(),
        "user_id": w.user_id,
        "kind": "resume",
        "s3_key": f"uploads/{w.user_id}/{n}",
        "sha256": "0" * 64,
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
VIEWS = ["commitments"]


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
            "`docker compose up -d --wait`, or deselect with -m 'not db'.",
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
        (t, "SELECT") for t in GATEWAY_TABLES
    }
    # job_runs: workers and system jobs only.
    for role, allowed in (("app_api", False), ("app_gateway", False), ("app_worker", True)):
        _, _, err = await as_user(db, role, db.a, "select 1 from app.job_runs")
        assert (err is None) == allowed, f"{role} job_runs access should be {allowed}"
