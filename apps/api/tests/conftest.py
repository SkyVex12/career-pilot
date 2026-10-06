"""API tests against the docker compose stack: a throwaway migrated database (connected as
app_api, the API's own role), Valkey database 14 and the S3 store. Jobs go to an in-process
queue so tests can see what was enqueued."""

import base64
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx2
import pytest
import pytest_asyncio

from copilot_api.config import ApiSettings
from copilot_api.deps import Services
from copilot_api.main import create_app
from copilot_core.auth import Principal, StaticVerifier
from copilot_core.crypto import DevKeyProvider, UserCrypto
from copilot_core.db import create_pool
from copilot_core.jobs import MemoryJobQueue
from copilot_core.storage import S3Store
from copilot_core.teststack import (
    S3_ENDPOINT,
    VALKEY_URL,
    StackUnavailableError,
    as_role,
    temp_database,
)
from copilot_core.valkey_client import make_valkey


def api_settings(**over: Any) -> ApiSettings:
    base: dict[str, Any] = {
        "env": "test",
        "key_provider": "dev",
        "dev_master_key": base64.b64encode(os.urandom(32)).decode(),
        "s3_endpoint": S3_ENDPOINT,
        "s3_bucket": "copilot-test",
        "s3_access_key": "local",
        "s3_secret_key": "local-secret",
        "valkey_url": VALKEY_URL,
        "max_document_bytes": 64 * 1024,
    }
    base.update(over)
    return ApiSettings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


@dataclass
class Api:
    svc: Services
    client: httpx2.AsyncClient
    admin: asyncpg.Connection
    jobs: MemoryJobQueue
    verifier: StaticVerifier

    def user(self, name: str = "Alex") -> tuple[uuid.UUID, dict[str, str]]:
        """A new user's id and auth headers (their row is created on first request)."""
        uid = uuid.uuid4()
        token = f"token-{uid}"
        self.verifier.principals[token] = Principal(uid, name, None, email=f"{uid.hex}@example.com")
        return uid, {"Authorization": f"Bearer {token}"}

    def take(self, job_type: str) -> list[dict[str, Any]]:
        out = [m for m in self.jobs.sent if m["type"] == job_type]
        self.jobs.sent = [m for m in self.jobs.sent if m not in out]
        return out


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def api() -> AsyncIterator[Api]:
    try:
        db = temp_database("copilot_api")
        url = await db.__aenter__()
    except StackUnavailableError as e:
        pytest.fail(str(e), pytrace=False)
    settings = api_settings()
    assert settings.dev_master_key is not None
    store = S3Store(settings)
    await store.ensure_bucket()
    verifier = StaticVerifier({})
    jobs = MemoryJobQueue()
    svc = Services(
        settings=settings,
        pool=await create_pool(as_role(url, "app_api"), 1, 5),
        valkey=make_valkey(settings.valkey_url),
        queue=jobs,
        store=store,
        crypto=UserCrypto(
            DevKeyProvider(base64.b64decode(settings.dev_master_key.get_secret_value()))
        ),
        verifier=verifier,
    )
    app = create_app(svc)
    app.state.services = svc
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://api.test")
    admin = await asyncpg.connect(url)
    try:
        yield Api(svc, client, admin, jobs, verifier)
    finally:
        await client.aclose()
        await admin.close()
        await svc.pool.close()
        await svc.valkey.aclose()
        await db.__aexit__(None, None, None)
