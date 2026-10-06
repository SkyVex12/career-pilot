"""Migration 0002: question and answer text are optional (no transcript consent), but text
and key version stay together. Needs the Postgres from `docker compose` (marker `stack`)."""

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.migrate import DEFAULT_DATABASE_URL, apply

pytestmark = pytest.mark.stack

MIGRATIONS = Path(__file__).resolve().parent.parent


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def conn() -> AsyncIterator[asyncpg.Connection]:
    admin_url = os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    try:
        admin = await asyncpg.connect(admin_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.fail(f"cannot reach Postgres at {admin_url} ({e})", pytrace=False)
    name = f"copilot_m2_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    url = urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    try:
        await apply(url, MIGRATIONS)
        c = await asyncpg.connect(url)
        try:
            yield c
        finally:
            await c.close()
    finally:
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


async def seed(c: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    uid, sid = uuid.uuid4(), uuid.uuid4()
    await c.execute(
        "insert into app.users (id, email, display_name) values ($1, $2, 'A')", uid, f"{uid}@x"
    )
    await c.execute(
        "insert into app.sessions (id, user_id, mode, store_transcript, started_at)"
        " values ($1, $2, 'interview', false, now())",
        sid,
        uid,
    )
    return uid, sid


async def test_question_and_answer_without_text(conn: asyncpg.Connection) -> None:
    uid, sid = await seed(conn)
    qid = uuid.uuid4()
    await conn.execute(
        "insert into app.questions (id, session_id, user_id, type, addressed_to_user,"
        " speculative_kept, detected_at) values ($1, $2, $3, 'technical', true, true, now())",
        qid,
        sid,
        uid,
    )
    await conn.execute(
        "insert into app.answers (id, question_id, user_id, pass, model, tier, latency_ms)"
        " values ($1, $2, $3, 'fast', 'claude-haiku-4-5', 'standard', '{}')",
        uuid.uuid4(),
        qid,
        uid,
    )
    row = await conn.fetchrow("select text_enc, key_version from app.questions where id = $1", qid)
    assert row is not None
    assert (row["text_enc"], row["key_version"]) == (None, None)


@pytest.mark.parametrize(
    ("text", "key"), [(b"\x01", None), (None, 1)], ids=["text-without-key", "key-without-text"]
)
async def test_text_and_key_version_go_together(
    conn: asyncpg.Connection, text: bytes | None, key: int | None
) -> None:
    uid, sid = await seed(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "insert into app.questions (id, session_id, user_id, text_enc, key_version, type,"
            " addressed_to_user, speculative_kept, detected_at)"
            " values ($1, $2, $3, $4, $5, 'technical', true, true, now())",
            uuid.uuid4(),
            sid,
            uid,
            text,
            key,
        )
