"""Copilot API (ARCHITECTURE.md, "REST API"): base path /v1, a bearer token on every request."""

import logging
import os
import re
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from copilot_api.config import ApiSettings
from copilot_api.deps import Services
from copilot_api.routes import auth, documents, employers, jobs, me, sessions
from copilot_core import __version__, logs
from copilot_core.auth import make_verifier
from copilot_core.crypto import UserCrypto, make_key_provider
from copilot_core.db import create_pool
from copilot_core.dotenv import load_dotenv
from copilot_core.jobs import SqsJobQueue
from copilot_core.storage import S3Store
from copilot_core.valkey_client import make_valkey

SERVICE = "api"
SESSION_PATH = re.compile(r"/v1/sessions/([0-9a-fA-F-]{36})(?:/|$)")

log = logging.getLogger(__name__)


class RequestLog:
    """One line per request (method, path, status, time), with the session id bound for
    requests under /v1/sessions/{id}, so a session's API calls show up next to its gateway and
    worker lines in CloudWatch. Pure ASGI, so the ids reach the handler's context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        match = SESSION_PATH.match(scope["path"])
        logs.set_ids(session_id=match.group(1).lower() if match else None)
        started = time.monotonic()
        status: dict[str, Any] = {"code": 500}

        async def send_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            log.info(
                "%s %s %s %.0fms", scope["method"], scope["path"], status["code"],
                (time.monotonic() - started) * 1000,
            )  # fmt: skip


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
    app.add_middleware(RequestLog)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "service": SERVICE, "version": __version__}

    for module in (auth, me, documents, jobs, sessions, employers):
        app.include_router(module.router, prefix="/v1")
    return app


app = create_app()


def run() -> None:
    load_dotenv()
    logs.configure("api")
    uvicorn.run(
        "copilot_api.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        log_config=None,  # logs.configure owns the handlers
    )
