"""Migration 0014: every existing account gets an identity that keeps its id and carries its
verified email, profiles' rows get none, and the two sign-in tables are forced under
row-level security. Needs the Postgres from `docker compose` (marker `stack`)."""

import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.auth import google_user_id
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
    name = f"copilot_m14_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'create database "{name}"')
    try:
        yield urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    finally:
        await admin.execute(f'drop database if exists "{name}" with (force)')
        await admin.close()


async def test_existing_google_accounts_keep_their_ids(database: str, tmp_path: Path) -> None:
    for sql in sorted(MIGRATIONS.glob("00*.sql")):
        if sql.name < "0014":
            shutil.copy(sql, tmp_path / sql.name)
    await apply(database, tmp_path)
    c = await asyncpg.connect(database)
    try:
        # Two accounts made by Google sign-ins (ids are UUIDv5s of Google's sub), one with an
        # address stored in mixed case, and a profile's row, which is not an account.
        alex, sam = google_user_id("1001"), google_user_id("1002")
        profile = uuid.uuid4()
        for uid, email, owner in (
            (alex, "alex@example.com", None),
            (sam, " Sam@Example.com", None),
            (profile, f"profile-{profile}@profiles.invalid", alex),
        ):
            await c.execute(
                "insert into app.users (id, email, display_name, owner_id)"
                " values ($1, $2, 'U', $3)",
                uid, email, owner,
            )  # fmt: skip
        before = await c.fetch("select id, email from app.users order by id")

        shutil.copy(MIGRATIONS / "0014_email_sign_in.sql", tmp_path)
        assert await apply(database, tmp_path) == ["0014_email_sign_in"]

        assert await c.fetch("select id, email from app.users order by id") == before
        rows = await c.fetch(
            "select provider, subject, user_id, email from app.identities order by email"
        )
        assert [tuple(r) for r in rows] == [
            ("google", str(alex), alex, "alex@example.com"),
            ("google", str(sam), sam, "sam@example.com"),
        ]
        assert await c.fetchval("select count(*) from app.refresh_tokens") == 0
        forced = await c.fetch(
            "select relname, relrowsecurity, relforcerowsecurity from pg_class"
            " where relname in ('identities', 'refresh_tokens', 'users') order by relname"
        )
        assert [tuple(r) for r in forced] == [
            ("identities", True, True),
            ("refresh_tokens", True, True),
            ("users", True, True),
        ]
        # Deleting an account takes its sign-ins with it.
        await c.execute("delete from app.users where id = $1", sam)
        assert await c.fetchval("select count(*) from app.identities") == 1
    finally:
        await c.close()
