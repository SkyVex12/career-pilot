"""Postgres access (ARCHITECTURE.md, "Row-level security").

Every request and job runs its queries inside `user_tx`, which opens a transaction and sets
`app.user_id` for it first. The setting is transaction-scoped, so it never leaks to the next
user of a pooled connection, and queries without it fail closed.
"""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Literal
from uuid import UUID

import asyncpg
from asyncpg.pool import PoolConnectionProxy

# A pooled connection or a plain one; queries take either.
type Conn = asyncpg.Connection | PoolConnectionProxy

# The authenticated actor stays distinct from the workspace selected for domain queries.
# Request middleware resets this context at both boundaries; jobs pass their actor explicitly.
actor_account: ContextVar[UUID | None] = ContextVar("actor_account", default=None)


async def _init(conn: asyncpg.Connection) -> None:
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(url: str, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(url, min_size=min_size, max_size=max_size, init=_init)
    assert pool is not None
    return pool


@asynccontextmanager
async def user_tx(
    pool: asyncpg.Pool,
    user_id: UUID,
    *,
    isolation: Literal["repeatable_read"] | None = None,
    actor_account_id: UUID | None = None,
) -> AsyncGenerator[Conn]:
    """Set tenant scope; related read aggregates may request one repeatable snapshot."""
    async with pool.acquire() as conn, conn.transaction(isolation=isolation):
        await conn.execute(
            "select set_config('app.user_id', $1, true), set_config('app.account_id', $2, true)",
            str(user_id),
            str(actor_account_id or actor_account.get() or user_id),
        )
        yield conn
        from copilot_core.operations import transaction_guard

        await transaction_guard(conn)


NO_USER = UUID(int=0)


@asynccontextmanager
async def sign_in_tx(pool: asyncpg.Pool, key: str) -> AsyncGenerator[Conn]:
    """A transaction before the user is known (migration 0014): it sees the sign-in rows whose
    key it presents (`google:<sub>`, `email:<address>`, `refresh:<hash>`) and no user's rows,
    until `become` names the user those rows gave."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "select set_config('app.user_id', $1, true), set_config('app.sign_in', $2, true),"
            " set_config('app.account_id', $1, true)",
            str(NO_USER), key,
        )  # fmt: skip
        yield conn


async def become(conn: Conn, user_id: UUID) -> None:
    """The rest of this transaction acts as `user_id`."""
    await conn.execute(
        "select set_config('app.user_id', $1, true), set_config('app.account_id', $1, true)",
        str(user_id),
    )


@asynccontextmanager
async def system_tx(pool: asyncpg.Pool) -> AsyncGenerator[Conn]:
    """For the app_system pool (BYPASSRLS) and the job_runs table, which has no RLS."""
    async with pool.acquire() as conn, conn.transaction():
        yield conn
