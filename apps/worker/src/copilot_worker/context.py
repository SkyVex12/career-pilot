"""What every job handler gets: stores, clients and the settings."""

import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from copilot_core import jobs
from copilot_core.crypto import UserCrypto, make_key_provider
from copilot_core.db import Conn, create_pool, system_tx, user_tx
from copilot_core.embed import Embedder, make_embedder
from copilot_core.identity import CognitoUsers, UserDirectory
from copilot_core.llm import ModelRouter
from copilot_core.providers import make_providers
from copilot_core.storage import ObjectStore, S3Store
from copilot_core.valkey_client import Valkey, make_valkey
from copilot_worker.config import WorkerSettings
from copilot_worker.fixtures import DeepgramFixtures, fixture_provider, model_fixtures
from copilot_worker.llm import build_router
from copilot_worker.prompts import JOB_PROMPTS
from copilot_worker.stt import DeepgramPrerecorded, PrerecordedSTT

log = logging.getLogger(__name__)


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
    stt: PrerecordedSTT | None = None
    embedder: Embedder | None = None
    # The Cognito user pool (user.delete); None with the dev token, which has no sign-in.
    users: UserDirectory | None = None

    @asynccontextmanager
    async def user(self, user_id: UUID) -> AsyncGenerator[Conn]:
        async with user_tx(self.pool, user_id) as conn:
            yield conn

    @asynccontextmanager
    async def system(self) -> AsyncGenerator[Conn]:
        async with system_tx(self.system_pool) as conn:
            yield conn

    async def enqueue(
        self,
        job_type: str,
        key: str,
        user_id: UUID | None,
        payload: Mapping[str, Any],
        delay_s: int = 0,
    ) -> None:
        await self.queue.send(jobs.envelope(job_type, key, user_id, payload), delay_s=delay_s)


async def build_ctx(settings: WorkerSettings) -> Ctx:
    if settings.fixtures_dir:
        if settings.env not in ("dev", "test"):
            raise RuntimeError("COPILOT_FIXTURES_DIR works only with COPILOT_ENV dev or test")
        directory = Path(settings.fixtures_dir)
        # The first configured provider's recordings: openai/ for OpenAI, claude/ otherwise.
        wire = "openai" if settings.llm_providers[:1] == ["openai"] else "claude"
        fixtures = model_fixtures(directory, JOB_PROMPTS, wire)
        router = build_router(settings, [fixture_provider(fixtures)])
        stt: PrerecordedSTT | None = DeepgramPrerecorded(
            "fixture", transport=DeepgramFixtures(directory)
        )
        log.warning("offline: %s and Deepgram answer from fixtures in %s", wire, directory)
    else:
        router = build_router(settings, make_providers(settings, settings.llm_timeout_s))
        stt = None
        if settings.deepgram_api_key is not None:
            stt = DeepgramPrerecorded(
                settings.deepgram_api_key.get_secret_value(),
                url=settings.deepgram_prerecorded_url,
                model=settings.deepgram_model,
            )
        else:
            log.warning("DEEPGRAM_API_KEY is not set; the user's own audio is not transcribed")
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
        stt=stt,
        embedder=embedder,
        users=users,
    )


async def close_ctx(ctx: Ctx) -> None:
    await ctx.pool.close()
    await ctx.system_pool.close()
    await ctx.valkey.aclose()
