"""What every job handler gets: stores, clients and the settings."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from copilot_core import jobs, operations, pack_operations
from copilot_core.careeros import CareerOsClient
from copilot_core.careeros import make_client as make_career_os
from copilot_core.crypto import UserCrypto, make_key_provider
from copilot_core.db import Conn, create_pool, system_tx, user_tx
from copilot_core.embed import Embedder, make_embedder
from copilot_core.identity import CognitoUsers, UserDirectory
from copilot_core.llm import ModelRouter
from copilot_core.providers import make_providers
from copilot_core.storage import ObjectStore, S3Store
from copilot_core.valkey_client import Valkey, make_valkey
from copilot_worker.config import WorkerSettings
from copilot_worker.fixtures import fixture_provider, model_fixtures
from copilot_worker.llm import build_router
from copilot_worker.prompts import JOB_PROMPTS

log = logging.getLogger(__name__)
_connection: ContextVar[Conn | None] = ContextVar("worker_domain_connection", default=None)
_queued: ContextVar[bool] = ContextVar("worker_domain_enqueued", default=False)


class DurableQueue:
    """Handler publication joins its domain transaction when one is active."""

    def __init__(self, ctx: "Ctx") -> None:
        self.ctx = ctx

    async def send(self, message: Mapping[str, Any], delay_s: int = 0) -> None:
        due = datetime.now(UTC) + timedelta(seconds=delay_s)
        if (conn := _connection.get()) is not None:
            await operations.enqueue(conn, message, available_at=due)
            _queued.set(True)
        else:
            async with self.ctx.system() as conn:
                await operations.enqueue(conn, message, available_at=due)
                _queued.set(True)


@dataclass
class Ctx:
    settings: WorkerSettings
    pool: asyncpg.Pool  # app_worker, under row-level security
    system_pool: asyncpg.Pool  # app_system (BYPASSRLS): system jobs only
    valkey: Valkey
    queue: jobs.JobQueue
    store: ObjectStore
    crypto: UserCrypto
    router: ModelRouter
    embedder: Embedder | None = None
    # The Cognito user pool (user.delete); None with the dev token, which has no sign-in.
    users: UserDirectory | None = None
    # Career-OS, to revoke an account's link when the account is deleted.
    career_os: CareerOsClient | None = None
    transport: jobs.JobQueue = field(init=False)
    dispatcher: operations.OutboxDispatcher = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.queue, pack_operations.ObservedQueue):
            self.queue = pack_operations.ObservedQueue(self.queue, self.valkey)
        self.transport = self.queue
        self.dispatcher = operations.OutboxDispatcher(
            self.system_pool,
            self.transport,
            batch_size=self.settings.outbox_batch_size,
            recovery_s=self.settings.outbox_recovery_s,
            publish_concurrency=self.settings.outbox_publish_concurrency,
        )
        self.queue = DurableQueue(self)

    @asynccontextmanager
    async def user(self, user_id: UUID) -> AsyncGenerator[Conn]:
        publish = False
        async with user_tx(self.pool, user_id) as conn:
            token, queued = _connection.set(conn), _queued.set(False)
            try:
                yield conn
                publish = _queued.get()
            finally:
                _connection.reset(token)
                _queued.reset(queued)
        if publish:
            await self.dispatcher.once()

    @asynccontextmanager
    async def system(self) -> AsyncGenerator[Conn]:
        publish = False
        async with system_tx(self.system_pool) as conn:
            token, queued = _connection.set(conn), _queued.set(False)
            try:
                yield conn
                await operations.transaction_guard(conn)
                publish = _queued.get()
            finally:
                _connection.reset(token)
                _queued.reset(queued)
        if publish:
            await self.dispatcher.once()

    async def enqueue(
        self,
        job_type: str,
        key: str,
        user_id: UUID | None,
        payload: Mapping[str, Any],
        delay_s: int = 0,
        *,
        conn: Conn | None = None,
    ) -> None:
        message = jobs.envelope(job_type, key, user_id, payload)
        if conn is None:
            await self.queue.send(message, delay_s=delay_s)
        else:
            await operations.enqueue(
                conn, message, available_at=datetime.now(UTC) + timedelta(seconds=delay_s)
            )
            _queued.set(True)

    @asynccontextmanager
    async def cache_commit(self, user_id: UUID) -> AsyncGenerator[Conn]:
        """Serialize brief cache publication with lease takeover; never call models here."""
        async with self.user(user_id) as conn:
            await operations.transaction_guard(conn)
            async with asyncio.timeout(1.0):
                yield conn


async def build_ctx(settings: WorkerSettings) -> Ctx:
    if settings.fixtures_dir:
        if settings.env not in ("dev", "test"):
            raise RuntimeError("COPILOT_FIXTURES_DIR works only with COPILOT_ENV dev or test")
        directory = Path(settings.fixtures_dir)
        # The first configured provider's recordings: openai/ for OpenAI, claude/ otherwise.
        wire = "openai" if settings.llm_providers[:1] == ["openai"] else "claude"
        fixtures = model_fixtures(directory, JOB_PROMPTS, wire)
        router = build_router(settings, [fixture_provider(fixtures)])
        log.warning("offline: %s answers from fixtures in %s", wire, directory)
    else:
        router = build_router(settings, make_providers(settings, settings.llm_timeout_s))
    embedder: Embedder | None = None
    if not settings.fixtures_dir:
        embedder = make_embedder(settings)
        log.info("pack embeddings: %s", embedder.model_id if embedder else "off")
    store = S3Store(settings)
    if settings.env in ("dev", "test"):
        await store.ensure_bucket()
    users: UserDirectory | None = None
    if settings.auth == "cognito" and settings.cognito_user_pool_id:
        users = CognitoUsers(settings.cognito_user_pool_id, settings.aws_region)
    pool = await create_pool(settings.database_url, settings.db_pool_min, settings.db_pool_max)
    system_pool = await create_pool(settings.system_database_url, 1, 2)
    return Ctx(
        settings=settings,
        pool=pool,
        system_pool=system_pool,
        valkey=make_valkey(settings.valkey_url, settings.valkey_cluster),
        queue=jobs.SqsJobQueue(
            settings.sqs_queue_url, settings.aws_region, endpoint_url=settings.sqs_endpoint or None
        ),
        store=store,
        crypto=UserCrypto(make_key_provider(settings), settings.dek_cache_ttl_s),
        router=router,
        embedder=embedder,
        users=users,
        career_os=make_career_os(settings.career_os_url, settings.career_os_timeout_s),
    )


async def close_ctx(ctx: Ctx) -> None:
    await ctx.pool.close()
    await ctx.system_pool.close()
    await ctx.valkey.aclose()
