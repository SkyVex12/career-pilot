"""Migration 0017: guest seats. The guest reads only their own open seats and changes nothing
but the answer to an invite (through `app.seat_answer`); the email lookup finds accounts, never
profiles, and only inside the transaction that asks. Needs the Postgres from `docker compose`
(marker `stack`)."""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.migrate import DEFAULT_DATABASE_URL, apply

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]

MIGRATIONS = Path(__file__).resolve().parent.parent
INSUFFICIENT_PRIVILEGE = "42501"


@dataclass
class Db:
    conn: asyncpg.Connection
    host: uuid.UUID
    guest: uuid.UUID
    other: uuid.UUID
    profile: uuid.UUID


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def db() -> AsyncIterator[Db]:
    admin_url = os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    try:
        admin = await asyncpg.connect(admin_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.fail(f"cannot reach Postgres at {admin_url} ({e})", pytrace=False)
    name = f"copilot_m17_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    url = urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    try:
        await apply(url, MIGRATIONS)
        conn = await asyncpg.connect(url)
        host, guest, other, profile = (uuid.uuid4() for _ in range(4))
        for uid, email, owner in (
            (host, "host@example.com", None),
            (guest, "guest@example.com", None),
            (other, "other@example.com", None),
            (profile, "profile@example.com", host),
        ):
            await conn.execute(
                "insert into app.users (id, email, display_name, owner_id)"
                " values ($1, $2, 'X', $3)",
                uid, email, owner,
            )  # fmt: skip
        try:
            yield Db(conn, host, guest, other, profile)
        finally:
            await conn.close()
    finally:
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


async def seat(db: Db, status: str = "invited", expires_in: timedelta = timedelta(minutes=10),
               guest: uuid.UUID | None = None) -> uuid.UUID:  # fmt: skip
    sid = uuid.uuid4()
    session_id = uuid.uuid4()
    await db.conn.execute(
        "insert into app.sessions(id,user_id,mode,store_transcript,started_at)"
        " values($1,$2,'interview',false,now())",
        session_id,
        db.host,
    )
    await db.conn.execute(
        "insert into app.session_seats (id, user_id, session_id, host_account_id, guest_id,"
        " host_name, guest_name, guest_email, mode, status, expires_at)"
        " values ($1, $2, $3, $2, $4, 'Host', 'Guest', 'guest@example.com', 'interview', $5, $6)",
        sid, db.host, session_id, guest or db.guest, status, datetime.now(UTC) + expires_in,
    )  # fmt: skip
    return sid


class Rollback(Exception):
    pass


async def as_user(
    db: Db, user: uuid.UUID, sql: str, *args: Any, role: str = "app_api", lookup: str | None = None
) -> tuple[list[asyncpg.Record], str | None]:
    rows: list[asyncpg.Record] = []
    try:
        async with db.conn.transaction():
            await db.conn.execute(f"set local role {role}")
            await db.conn.execute(
                "select set_config('app.user_id', $1, true),set_config('app.account_id', $1, true)",
                str(user),
            )
            if lookup is not None:
                await db.conn.execute("select set_config('app.seat_lookup', $1, true)", lookup)
            rows = await db.conn.fetch(sql, *args)
            raise Rollback
    except Rollback:
        return rows, None
    except asyncpg.PostgresError as e:
        return [], e.sqlstate


@pytest.mark.parametrize("role", ["app_api", "app_gateway", "app_worker"])
async def test_a_guest_reads_only_their_own_open_seats(db: Db, role: str) -> None:
    open_ids = {await seat(db, "invited"), await seat(db, "accepted"), await seat(db, "admitted")}
    closed = {await seat(db, s) for s in ("declined", "removed", "expired", "ended")}
    stale = await seat(db, "admitted", expires_in=timedelta(minutes=-1))
    theirs = await seat(db, "admitted", guest=db.other)
    rows, err = await as_user(db, db.guest, "select id from app.session_seats", role=role)
    assert err is None
    seen = {r["id"] for r in rows}
    assert open_ids <= seen
    assert not seen & (closed | {stale, theirs})


async def test_a_guest_cannot_change_a_seat_directly(db: Db) -> None:
    sid = await seat(db, "invited")
    for sql in (
        "update app.session_seats set capabilities = '{view,note}' where id = $1 returning id",
        "update app.session_seats set status = 'admitted' where id = $1 returning id",
        "delete from app.session_seats where id = $1 returning id",
    ):
        rows, err = await as_user(db, db.guest, sql, sid)
        assert err is None, sql
        assert rows == [], sql


async def test_a_guest_answers_their_invite_once(db: Db) -> None:
    sid = await seat(db, "invited")
    rows, err = await as_user(db, db.guest, "select app.seat_answer($1, true) as s", sid)
    assert err is None
    assert rows[0]["s"] is not None
    # Not committed (as_user rolls back); answer for real now.
    async with db.conn.transaction():
        await db.conn.execute("set local role app_api")
        await db.conn.execute(
            "select set_config('app.user_id', $1, true),set_config('app.account_id', $1, true)",
            str(db.guest),
        )
        assert await db.conn.fetchval("select app.seat_answer($1, true)", sid) is not None
    row = await db.conn.fetchrow(
        "select status, rev, accepted_at from app.session_seats where id = $1", sid
    )
    assert row is not None
    assert row["status"] == "accepted"
    assert row["rev"] == 2
    assert row["accepted_at"] is not None
    # A second answer, or an answer to someone else's or an expired invite, changes nothing.
    rows, _ = await as_user(db, db.guest, "select app.seat_answer($1, false) as s", sid)
    assert rows[0]["s"] is None
    rows, _ = await as_user(db, db.other, "select app.seat_answer($1, true) as s", await seat(db))
    assert rows[0]["s"] is None
    expired = await seat(db, expires_in=timedelta(minutes=-1))
    rows, _ = await as_user(db, db.guest, "select app.seat_answer($1, true) as s", expired)
    assert rows[0]["s"] is None


async def test_declining_closes_the_invite(db: Db) -> None:
    sid = await seat(db, "invited")
    async with db.conn.transaction():
        await db.conn.execute("set local role app_api")
        await db.conn.execute(
            "select set_config('app.user_id', $1, true),set_config('app.account_id', $1, true)",
            str(db.guest),
        )
        assert await db.conn.fetchval("select app.seat_answer($1, false)", sid) is not None
    row = await db.conn.fetchrow(
        "select status, ended_at from app.session_seats where id = $1", sid
    )
    assert row is not None
    assert row["status"] == "declined"
    assert row["ended_at"] is not None


async def test_only_the_api_may_answer_for_a_guest(db: Db) -> None:
    _, err = await as_user(
        db, db.guest, "select app.seat_answer($1, true)", await seat(db), role="app_gateway"
    )
    assert err == INSUFFICIENT_PRIVILEGE


async def test_the_email_lookup_finds_accounts_only_when_asked(db: Db) -> None:
    sql = "select id from app.accounts where email = $1"
    rows, _ = await as_user(db, db.host, sql, "guest@example.com")
    assert rows == []
    rows, _ = await as_user(db, db.host, sql, "guest@example.com", lookup="guest@example.com")
    assert [r["id"] for r in rows] == [db.guest]
    # The lookup names one address: no other account shows, and never a profile.
    rows, _ = await as_user(db, db.host, "select id from app.accounts", lookup="guest@example.com")
    assert {r["id"] for r in rows} == {db.host, db.guest}
    rows, _ = await as_user(db, db.other, sql, "profile@example.com", lookup="profile@example.com")
    assert rows == []
    # The gateway never looks anyone up: it does not set the key, so it sees no one else.
    rows, _ = await as_user(db, db.host, "select id from app.users", role="app_gateway")
    assert {r["id"] for r in rows} == {db.host}


async def test_one_open_seat_per_guest_and_session(db: Db) -> None:
    sid = await seat(db, "invited")
    session = await db.conn.fetchval("select session_id from app.session_seats where id = $1", sid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.conn.execute(
            "insert into app.session_seats (id, user_id, session_id, host_account_id, guest_id,"
            " host_name, guest_name, guest_email, mode, status, expires_at)"
            " values ($1, $2, $3, $2, $4, 'H', 'G', 'g@x', 'interview', 'accepted', now())",
            uuid.uuid4(), db.host, session, db.guest,
        )  # fmt: skip


async def test_capabilities_always_include_view_and_only_known_values(db: Db) -> None:
    for caps in (["note"], ["view", "screen.everything"]):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.conn.execute(
                "insert into app.session_seats (id, user_id, session_id, host_account_id,"
                " guest_id, host_name, guest_name, guest_email, mode, status, expires_at,"
                " capabilities) values ($1, $2, $3, $2, $4, 'H', 'G', 'g@x', 'interview',"
                " 'invited', now(), $5)",
                uuid.uuid4(), db.host, uuid.uuid4(), db.guest, caps,
            )  # fmt: skip
