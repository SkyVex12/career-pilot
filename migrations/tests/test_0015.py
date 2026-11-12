"""Migration 0015: Career-OS sign-in changes no existing row. Identities and refresh tokens
keep what they had, refresh tokens made before it are email sign-ins, and accounts and links
are untouched. Needs the Postgres from `docker compose` (marker `stack`)."""

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


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database() -> AsyncIterator[str]:
    admin_url = os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)
    try:
        admin = await asyncpg.connect(admin_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.fail(f"cannot reach Postgres at {admin_url} ({e})", pytrace=False)
    name = f"copilot_m15_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    try:
        yield urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    finally:
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


async def test_nothing_existing_changes(database: str, tmp_path: Path) -> None:
    for sql in sorted(MIGRATIONS.glob("00*.sql")):
        if sql.name < "0015":
            shutil.copy(sql, tmp_path / sql.name)
    await apply(database, tmp_path)
    c = await asyncpg.connect(database)
    try:
        alex, sam = uuid.uuid4(), uuid.uuid4()
        for uid, email in ((alex, "alex@example.com"), (sam, "sam@example.com")):
            await c.execute(
                "insert into app.users (id, email, display_name) values ($1, $2, 'U')", uid, email
            )
        await c.execute(
            "insert into app.identities (provider, subject, user_id, email) values"
            " ('google', '1001', $1, 'alex@example.com'),"
            " ('email', 'sam@example.com', $2, 'sam@example.com')",
            alex, sam,
        )  # fmt: skip
        await c.execute(
            "insert into app.refresh_tokens (token_hash, user_id, family_id, email, expires_at)"
            " values ($1, $2, $3, 'sam@example.com', $4)",
            "a" * 64, sam, uuid.uuid4(), datetime.now(UTC) + timedelta(days=60),
        )  # fmt: skip
        await c.execute(
            "insert into app.integrations (user_id, provider, token_enc, key_version, scopes,"
            " status, external_id, external_kind) values"
            " ($1, 'career_os', $2, 1, $3, 'active', 'u1', 'user')",
            alex, b"ciphertext", ["applications:read"],
        )  # fmt: skip
        tables = ("users", "identities", "integrations")
        before = {t: await c.fetch(f"select * from app.{t} order by 1, 2") for t in tables}
        tokens = await c.fetch(
            "select token_hash, user_id, family_id, email from app.refresh_tokens"
        )

        shutil.copy(MIGRATIONS / "0015_career_os_sign_in.sql", tmp_path)
        assert await apply(database, tmp_path) == ["0015_career_os_sign_in"]

        for t in tables:
            assert await c.fetch(f"select * from app.{t} order by 1, 2") == before[t], t
        after = await c.fetch(
            "select token_hash, user_id, family_id, email, provider, subject"
            " from app.refresh_tokens"
        )
        assert [tuple(r)[:4] for r in after] == [tuple(r) for r in tokens]
        assert [(r["provider"], r["subject"]) for r in after] == [("email", None)]
        # A Career-OS sign-in's refresh token names its Career-OS account instead of an email.
        with pytest.raises(asyncpg.CheckViolationError):
            await c.execute(
                "insert into app.refresh_tokens (token_hash, user_id, family_id, provider,"
                " expires_at) values ($1, $2, $3, 'career_os', now())",
                "b" * 64, alex, uuid.uuid4(),
            )  # fmt: skip
        policies = await c.fetch(
            "select tablename, policyname, cmd from pg_policies"
            " where schemaname = 'app' and policyname = 'sign_in' order by tablename"
        )
        assert [tuple(r) for r in policies] == [
            ("identities", "sign_in", "SELECT"),
            ("integrations", "sign_in", "SELECT"),
            ("refresh_tokens", "sign_in", "SELECT"),
        ]
    finally:
        await c.close()
