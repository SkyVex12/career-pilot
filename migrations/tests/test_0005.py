"""Migration 0005: a memory node's vector and the embedding model that made it go together,
and vectors from before 0005 (model unknown) are cleared. Needs the Postgres from
`docker compose` (marker `stack`)."""

import os
import shutil
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
VECTOR = "[" + ",".join(["0.5"] * 1024) + "]"


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database() -> AsyncIterator[str]:
    admin_url = os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    try:
        admin = await asyncpg.connect(admin_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.fail(f"cannot reach Postgres at {admin_url} ({e})", pytrace=False)
    name = f"copilot_m5_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    try:
        yield urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    finally:
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


async def insert_node(c: asyncpg.Connection, uid: uuid.UUID, vector: str | None,
                      model: str | None) -> uuid.UUID:  # fmt: skip
    node = uuid.uuid4()
    columns = "id, user_id, kind, domain, title, summary, source, embedding"
    values = "$1, $2, 'skill', 'career', 'Python', 'Python', '{}', $3::vector"
    args: list[object] = [node, uid, vector]
    if model is not None:
        columns, values = columns + ", embedding_model", values + ", $4"
        args.append(model)
    await c.execute(f"insert into app.memory_nodes ({columns}) values ({values})", *args)
    return node


async def test_old_vectors_are_cleared_and_new_ones_name_their_model(
    database: str, tmp_path: Path
) -> None:
    # The schema before 0005, with a vector of unknown origin.
    for sql in sorted(MIGRATIONS.glob("000[1-4]_*.sql")):
        shutil.copy(sql, tmp_path / sql.name)
    await apply(database, tmp_path)
    c = await asyncpg.connect(database)
    try:
        uid = uuid.uuid4()
        await c.execute(
            "insert into app.users (id, email, display_name) values ($1, $2, 'A')", uid, f"{uid}@x"
        )
        old = await insert_node(c, uid, VECTOR, None)

        await apply(database, MIGRATIONS)
        row = await c.fetchrow(
            "select embedding, embedding_model from app.memory_nodes where id = $1", old
        )
        assert row is not None
        assert (row["embedding"], row["embedding_model"]) == (None, None)

        with pytest.raises(asyncpg.CheckViolationError):
            await insert_node(c, uid, VECTOR, None)  # a vector without its model
        with pytest.raises(asyncpg.CheckViolationError):
            await insert_node(c, uid, None, "openai:text-embedding-3-large@1024")
        await insert_node(c, uid, VECTOR, "openai:text-embedding-3-large@1024")
        await insert_node(c, uid, None, None)
        # Row-level security is forced again after the migration lifted it.
        forced = await c.fetchval(
            "select relforcerowsecurity from pg_class where oid = 'app.memory_nodes'::regclass"
        )
        assert forced is True
    finally:
        await c.close()
