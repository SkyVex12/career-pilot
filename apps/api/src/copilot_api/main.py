"""Copilot API (ARCHITECTURE.md, "REST API"): base path /v1, a bearer token on every request."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from copilot_api.config import ApiSettings
from copilot_api.deps import Services
from copilot_api.routes import documents, jobs, me, sessions
from copilot_core import __version__
from copilot_core.auth import make_verifier
from copilot_core.crypto import UserCrypto, make_key_provider
from copilot_core.db import create_pool
from copilot_core.dotenv import load_dotenv
from copilot_core.jobs import SqsJobQueue
from copilot_core.storage import S3Store
from copilot_core.valkey_client import make_valkey

SERVICE = "api"


async def build_services(settings: ApiSettings) -> Services:
    store = S3Store(settings)
    if settings.env in ("dev", "test"):
        await store.ensure_bucket()
    return Services(
        settings=settings,
        pool=await create_pool(settings.database_url, settings.db_pool_min, settings.db_pool_max),
        valkey=make_valkey(settings.valkey_url, settings.valkey_cluster),
        queue=SqsJobQueue(
            settings.sqs_queue_url, settings.aws_region, endpoint_url=settings.sqs_endpoint or None
        ),
        store=store,
        crypto=UserCrypto(make_key_provider(settings), settings.dek_cache_ttl_s),
        verifier=make_verifier(settings),
    )


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        owned = services is None
        app.state.services = services or await build_services(ApiSettings())
        try:
            yield
        finally:
            if owned:
                await app.state.services.pool.close()
                await app.state.services.valkey.aclose()

    app = FastAPI(title="Copilot API", version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "service": SERVICE, "version": __version__}

    for module in (me, documents, jobs, sessions):
        app.include_router(module.router, prefix="/v1")
    return app


app = create_app()


def run() -> None:
    load_dotenv()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "copilot_api.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
    )
