"""Worker tests against the docker compose stack: a throwaway migrated database (connected as
app_worker and app_system), Valkey database 14, the S3 store, and recorded Claude and
Deepgram responses replayed through the real clients."""

import asyncio
import base64
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from copilot_core.crypto import DevKeyProvider, UserCrypto
from copilot_core.db import create_pool
from copilot_core.jobs import MemoryJobQueue
from copilot_core.storage import S3Store, document_key
from copilot_core.teststack import (
    S3_ENDPOINT,
    VALKEY_URL,
    StackUnavailableError,
    as_role,
    temp_database,
)
from copilot_core.valkey_client import make_valkey
from copilot_worker import documents, packs, sessionlog, system
from copilot_worker.config import WorkerSettings
from copilot_worker.context import Ctx
from copilot_worker.fixtures import ClaudeFixtures, DeepgramFixtures, fixture_provider
from copilot_worker.llm import build_router
from copilot_worker.prompts import JOB_PROMPTS
from copilot_worker.runtime import HANDLERS, Job
from copilot_worker.stt import DeepgramPrerecorded

FIXTURES = Path(__file__).parent / "fixtures"
HANDLER_MODULES = (documents, packs, sessionlog, system)

pytestmark = pytest.mark.stack


def worker_settings(**over: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "env": "test",
        "llm_providers": ["anthropic"],
        "embedder": "none",
        "key_provider": "dev",
        "dev_master_key": base64.b64encode(os.urandom(32)).decode(),
        "s3_endpoint": S3_ENDPOINT,
        "s3_bucket": "copilot-test",
        "s3_access_key": "local",
        "s3_secret_key": "local-secret",
        "valkey_url": VALKEY_URL,
    }
    base.update(over)
    return WorkerSettings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


@dataclass
class Stack:
    ctx: Ctx
    admin: asyncpg.Connection  # superuser on the test database, for checks only
    jobs: MemoryJobQueue
    claude: ClaudeFixtures
    deepgram: DeepgramFixtures

    async def run(self, message: dict[str, Any]) -> None:
        """Run one enqueued message's handler directly."""
        job = Job.parse(_json(message))
        await HANDLERS[job.type](job, self.ctx)

    def take(self, job_type: str | None = None) -> list[dict[str, Any]]:
        """Remove and return the enqueued messages (of one type)."""
        out = [m for m in self.jobs.sent if job_type is None or m["type"] == job_type]
        self.jobs.sent = [m for m in self.jobs.sent if m not in out]
        return out

    async def drain(self, *types: str, limit: int = 50) -> list[str]:
        """Run enqueued jobs of these types until none are left; returns their keys."""
        ran: list[str] = []
        for _ in range(limit):
            ready = [m for m in self.jobs.sent if m["type"] in types]
            if not ready:
                return ran
            m = ready[0]
            self.jobs.sent.remove(m)
            await self.run(m)
            ran.append(m["idempotency_key"])
        raise AssertionError(f"jobs still pending after {limit}: {self.jobs.sent}")

    async def new_user(self, name: str = "Alex Doe") -> uuid.UUID:
        uid = uuid.uuid4()
        await self.admin.execute(
            "insert into app.users (id, email, display_name) values ($1, $2, $3)",
            uid,
            f"{uid.hex}@example.com",
            name,
        )
        return uid

    async def upload(self, uid: uuid.UUID, kind: str, path: Path, content_type: str) -> uuid.UUID:
        body = await asyncio.to_thread(path.read_bytes)
        doc_id = uuid.uuid4()
        key = document_key(uid, doc_id, content_type)
        await self.ctx.store.put(key, body, content_type)
        await self.admin.execute(
            "insert into app.documents (id, user_id, kind, s3_key, sha256, content_type)"
            " values ($1, $2, $3, $4, $5, $6)",
            doc_id,
            uid,
            kind,
            key,
            hashlib.sha256(body).hexdigest(),
            content_type,
        )
        return doc_id


def _json(message: dict[str, Any]) -> str:
    import json

    return json.dumps(message)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def stack() -> AsyncIterator[Stack]:
    try:
        db = temp_database("copilot_worker")
        url = await db.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    settings = worker_settings()
    claude = ClaudeFixtures(FIXTURES, JOB_PROMPTS)
    deepgram = DeepgramFixtures(FIXTURES)
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
        stt=DeepgramPrerecorded("fixture", transport=deepgram),
    )
    admin = await asyncpg.connect(url)
    try:
        yield Stack(ctx, admin, jobs, claude, deepgram)
    finally:
        await admin.close()
        await ctx.pool.close()
        await ctx.system_pool.close()
        await ctx.valkey.aclose()
        await db.__aexit__(None, None, None)
