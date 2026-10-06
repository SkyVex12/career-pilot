"""Test helpers for the docker compose stack, shared by the API, worker and end-to-end tests.

`temp_database` migrates a throwaway database and gives the app roles their local logins, so
tests connect as `app_api`, `app_worker` and `app_system` exactly as the services do.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from copilot_core.devroles import grant_login, role_url
from copilot_core.migrate import DEFAULT_DATABASE_URL, apply

MIGRATIONS = Path(__file__).resolve().parents[4] / "migrations"
VALKEY_URL = os.environ.get("COPILOT_TEST_VALKEY_URL", "redis://localhost:6379/14")
SQS_ENDPOINT = os.environ.get("COPILOT_SQS_ENDPOINT", "http://localhost:9324")
S3_ENDPOINT = os.environ.get("COPILOT_S3_ENDPOINT", "http://localhost:8333")


class StackUnavailableError(Exception):
    pass


def admin_url() -> str:
    return os.environ.get("COPILOT_DATABASE_URL", DEFAULT_DATABASE_URL)


@asynccontextmanager
async def temp_database(prefix: str) -> AsyncGenerator[str]:
    """A migrated database that is dropped afterwards; yields its superuser URL."""
    admin = admin_url()
    try:
        conn = await asyncpg.connect(admin, timeout=5)
    except (OSError, asyncpg.PostgresError) as e:
        raise StackUnavailableError(
            f"cannot reach Postgres at {admin} ({e}). Start the local stack with "
            "`docker compose up -d --wait`, or deselect with -m 'not stack'."
        ) from e
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    await conn.execute(f'create database "{name}"')
    url = urlunsplit(urlsplit(admin)._replace(path=f"/{name}"))
    try:
        await apply(url, MIGRATIONS)
        await grant_login(admin)
        yield url
    finally:
        await conn.execute(f'drop database if exists "{name}" with (force)')
        await conn.close()


def as_role(url: str, role: str) -> str:
    return role_url(url, role)
