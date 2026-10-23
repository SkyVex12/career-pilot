"""Worker tests against the docker compose stack: a throwaway migrated database (connected as
app_worker and app_system), Valkey database 14, the S3 store, and recorded model responses
replayed through the real clients (see copilot_worker.testing).
`stack` replays Claude's recordings; `openai_stack` OpenAI's, with only openai configured."""

import base64
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.crypto import DevKeyProvider, UserCrypto
from copilot_core.db import create_pool
from copilot_core.jobs import MemoryJobQueue
from copilot_core.storage import S3Store
from copilot_core.teststack import StackUnavailableError, as_role, temp_database
from copilot_core.valkey_client import make_valkey
from copilot_worker.context import Ctx
from copilot_worker.fixtures import fixture_provider, model_fixtures
from copilot_worker.llm import build_router
from copilot_worker.prompts import JOB_PROMPTS
from copilot_worker.testing import FIXTURES, Stack, worker_settings

OPENAI: dict[str, Any] = {
    "llm_providers": ["openai"],
    "openai_extract_model": "gpt-6-astra",
    "openai_extract_reasoning": "medium",
    **{f"openai_{job}_model": "gpt-6.1-sol" for job in ("job", "pack", "postcall")},
    **{f"openai_{job}_reasoning": "medium" for job in ("job", "pack", "postcall")},
}


async def make_stack(wire: str, **settings_over: Any) -> AsyncIterator[Stack]:
    try:
        db = temp_database("copilot_worker")
        url = await db.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    settings = worker_settings(**settings_over)
    claude = model_fixtures(FIXTURES, JOB_PROMPTS, wire)
    jobs = MemoryJobQueue()
    store = S3Store(settings)
    await store.ensure_bucket()
    assert settings.dev_master_key is not None
    ctx = Ctx(
        settings=settings,
        pool=await create_pool(as_role(url, "app_worker"), 1, 5),
        system_pool=await create_pool(as_role(url, "app_system"), 1, 2),
        valkey=make_valkey(settings.valkey_url),
        queue=jobs,
        store=store,
        crypto=UserCrypto(
            DevKeyProvider(base64.b64decode(settings.dev_master_key.get_secret_value()))
        ),
        router=build_router(settings, [fixture_provider(claude)]),
    )
    admin = await asyncpg.connect(url)
    try:
        yield Stack(ctx, admin, jobs, claude, url)
    finally:
        await admin.close()
        await ctx.pool.close()
        await ctx.system_pool.close()
        await ctx.valkey.aclose()
        await db.__aexit__(None, None, None)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def stack() -> AsyncIterator[Stack]:
    async for s in make_stack("claude"):
        yield s


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def openai_stack() -> AsyncIterator[Stack]:
    async for s in make_stack("openai", **OPENAI):
        yield s
