"""Gateway: live WebSocket sessions (see ARCHITECTURE.md, "Gateway internals")."""

import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket

from copilot_core import __version__
from copilot_core.embed import BedrockCohereEmbedder, Embedder
from copilot_core.jobs import SqsJobQueue
from copilot_gateway.auth import make_verifier
from copilot_gateway.config import Settings
from copilot_gateway.router import build_router
from copilot_gateway.session import Services, SessionManager
from copilot_gateway.store import GatewayStore, make_client
from copilot_gateway.stt import STTFactory
from copilot_gateway.stt.deepgram import DeepgramSTT

SERVICE = "gateway"

log = logging.getLogger(__name__)


def stt_providers(settings: Settings) -> list[tuple[str, STTFactory]]:
    providers: list[tuple[str, STTFactory]] = []
    for name in settings.stt_providers:
        if name == "deepgram":
            if settings.deepgram_api_key is None:
                log.warning("DEEPGRAM_API_KEY is not set; Deepgram STT is off")
                continue
            key = settings.deepgram_api_key.get_secret_value()

            def deepgram(key: str = key) -> DeepgramSTT:
                return DeepgramSTT(key, url=settings.deepgram_url, model=settings.deepgram_model)

            providers.append(("deepgram", deepgram))
        else:
            log.warning("STT provider %s is not implemented yet; skipping it", name)
    return providers


def build_services(settings: Settings) -> Services:
    embedder: Embedder | None = None
    if settings.embedder == "bedrock":
        embedder = BedrockCohereEmbedder(settings.embed_model, settings.aws_region)
    return Services(
        settings=settings,
        store=GatewayStore(make_client(settings), settings),
        jobs=SqsJobQueue(
            settings.sqs_queue_url, settings.aws_region, endpoint_url=settings.sqs_endpoint
        ),
        router=build_router(settings),
        verifier=make_verifier(settings),
        stt_providers=stt_providers(settings),
        embedder=embedder,
    )


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        svc = services or build_services(Settings())
        app.state.manager = SessionManager(svc)
        try:
            yield
        finally:
            await app.state.manager.shutdown()

    app = FastAPI(title="Copilot gateway", version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "service": SERVICE, "version": __version__}

    @app.websocket("/v1/session")
    async def session(ws: WebSocket) -> None:  # pyright: ignore[reportUnusedFunction]
        manager: SessionManager = ws.app.state.manager
        await manager.handle(ws)

    return app


app = create_app()


def uvicorn_options(settings: Settings) -> dict[str, object]:
    return {
        # Screenshots are the largest message (2 MB); the session enforces the per-type limits.
        "ws_max_size": settings.max_screenshot_bytes + 4096,
        # Server pings every 30 s; a peer silent for another 60 s is closed (90 s in all).
        "ws_ping_interval": settings.ws_ping_interval_s,
        "ws_ping_timeout": settings.ws_ping_timeout_s,
        "loop": "uvloop" if sys.platform != "win32" else "asyncio",
    }


def run() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "copilot_gateway.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8081")),
        **uvicorn_options(Settings()),  # pyright: ignore[reportArgumentType]
    )
