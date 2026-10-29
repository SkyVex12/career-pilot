"""API tests against the docker compose stack: a throwaway migrated database (connected as
app_api, the API's own role), Valkey database 14 and the S3 store. Jobs go to an in-process
queue so tests can see what was enqueued (see copilot_api.testing)."""

import base64
from collections.abc import AsyncIterator

import asyncpg
import httpx2
import pytest
import pytest_asyncio

from copilot_api.deps import Services
from copilot_api.main import create_app
from copilot_api.testing import Api, api_settings
from copilot_core.auth import StaticVerifier
from copilot_core.crypto import DevKeyProvider, UserCrypto
from copilot_core.db import create_pool
from copilot_core.jobs import MemoryJobQueue
from copilot_core.storage import S3Store
from copilot_core.teststack import StackUnavailableError, as_role, temp_database
from copilot_core.valkey_client import make_valkey


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


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def fresh_people(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Career-OS people are shared across accounts (copilot_core.profiles), and the fake's
    user ids repeat from test to test: each test starts without any."""
    if "api" in request.fixturenames:
        api: Api = request.getfixturevalue("api")
        await api.admin.execute("delete from app.career_os_people")
    yield
