"""The backend for the desktop client's API integration tests (apps/desktop/core/tests/api.rs).

Runs, in this one process, on the local stack (`docker compose up -d --wait`):

- the real Copilot API (uvicorn, as `app_api`, on a migrated throwaway database),
- a real worker consumer (as `app_worker` and `app_system`) reading a throwaway ElasticMQ
  queue, with Claude and Deepgram answered from the recorded fixtures in
  apps/worker/tests/fixtures (the real SDK and HTTP clients over a mock transport),
- the real gateway with scripted STT and Claude (copilot_gateway.testing), sending its
  session log jobs to the same queue.

All three share one bearer token for a test user, Valkey database 13 and the S3 store. The
process prints one JSON line with the URLs once everything accepts connections, then runs
until its stdin closes, and drops the database and the queue on the way out. If the stack is
not reachable it prints `unavailable: <reason>` and exits 3.

    uv run python scripts/desktop_test_stack.py
"""

import asyncio
import base64
import contextlib
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import uvicorn

from copilot_api.config import ApiSettings
from copilot_api.deps import Services as ApiServices
from copilot_api.main import create_app
from copilot_core.auth import Principal, StaticVerifier
from copilot_core.aws import boto3_client
from copilot_core.crypto import DevKeyProvider, UserCrypto
from copilot_core.db import create_pool
from copilot_core.jobs import SqsJobQueue
from copilot_core.storage import S3Store
from copilot_core.teststack import (
    S3_ENDPOINT,
    SQS_ENDPOINT,
    StackUnavailableError,
    as_role,
    temp_database,
)
from copilot_core.valkey_client import make_valkey
from copilot_gateway.config import Settings as GatewaySettings
from copilot_gateway.router import build_router as gateway_router
from copilot_gateway.session import Services as GatewayServices
from copilot_gateway.store import GatewayStore, make_client
from copilot_gateway.testing import GatewayServer, ScriptedProvider, TextSTT, TextSTTControl
from copilot_worker import documents, packs, sessionlog, system
from copilot_worker.config import WorkerSettings
from copilot_worker.context import Ctx
from copilot_worker.fixtures import ClaudeFixtures, DeepgramFixtures, fixture_provider
from copilot_worker.llm import build_router as worker_router
from copilot_worker.prompts import JOB_PROMPTS
from copilot_worker.runtime import Consumer, JobRuns, SqsQueue
from copilot_worker.stt import DeepgramPrerecorded

HANDLER_MODULES = (documents, packs, sessionlog, system)  # importing them registers handlers
FIXTURES = Path(__file__).resolve().parent.parent / "apps" / "worker" / "tests" / "fixtures"
VALKEY_URL = os.environ.get("COPILOT_DESKTOP_TEST_VALKEY_URL", "redis://localhost:6379/13")
TOKEN = "desktop-test-token-0123456789"
REGION = "us-east-1"


class ApiServer:
    """The API under uvicorn on a free localhost port, inside this event loop."""

    def __init__(self, services: ApiServices) -> None:
        app = create_app(services)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self.task: asyncio.Task[None] | None = None
        self.port = 0

    async def __aenter__(self) -> "ApiServer":
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            if self.task.done():
                self.task.result()
            await asyncio.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.server.should_exit = True
        if self.task is not None:
            await self.task


def stdin_closed(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """Set `stop` once stdin reaches EOF (the test dropped its handle)."""
    with contextlib.suppress(Exception):
        while sys.stdin.buffer.read(1):
            pass
    loop.call_soon_threadsafe(stop.set)


async def serve() -> int:
    # ElasticMQ takes any key, but boto3 wants one.
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
    try:
        db = temp_database("copilot_desktop")
        url = await db.__aenter__()
    except StackUnavailableError as e:
        print(f"unavailable: {e}", flush=True)
        return 3
    sqs: Any = boto3_client("sqs", region_name=REGION, endpoint_url=SQS_ENDPOINT)
    queue_url: str = sqs.create_queue(QueueName=f"jobs-desktop-{uuid.uuid4().hex[:10]}")["QueueUrl"]
    uid = uuid.uuid4()
    verifier = StaticVerifier(
        {TOKEN: Principal(uid, "Alex Doe", None, email=f"{uid.hex}@example.com")}
    )
    master = os.urandom(32)
    common: dict[str, Any] = {
        "env": "test",
        "valkey_url": VALKEY_URL,
        "s3_endpoint": S3_ENDPOINT,
        "s3_bucket": "copilot-test",
        "s3_access_key": "local",
        "s3_secret_key": "local-secret",
        "key_provider": "dev",
        "dev_master_key": base64.b64encode(master).decode(),
        "llm_providers": ["anthropic"],
        "embedder": "none",
        "sqs_endpoint": SQS_ENDPOINT,
        "sqs_queue_url": queue_url,
    }
    stop = asyncio.Event()
    pools: list[Any] = []
    try:
        api_settings = ApiSettings(_env_file=None, **common)  # pyright: ignore[reportCallIssue]
        store = S3Store(api_settings)
        await store.ensure_bucket()
        jobs = SqsJobQueue(queue_url, REGION, endpoint_url=SQS_ENDPOINT)
        api_svc = ApiServices(
            settings=api_settings,
            pool=await create_pool(as_role(url, "app_api"), 1, 4),
            valkey=make_valkey(VALKEY_URL),
            queue=jobs,
            store=store,
            crypto=UserCrypto(DevKeyProvider(master)),
            verifier=verifier,
        )
        pools.append(api_svc.pool)

        worker_settings = WorkerSettings(_env_file=None, **common)  # pyright: ignore[reportCallIssue]
        ctx = Ctx(
            settings=worker_settings,
            pool=await create_pool(as_role(url, "app_worker"), 1, 4),
            system_pool=await create_pool(as_role(url, "app_system"), 1, 2),
            valkey=make_valkey(VALKEY_URL),
            queue=jobs,
            store=store,
            crypto=UserCrypto(DevKeyProvider(master)),
            router=worker_router(
                worker_settings, [fixture_provider(ClaudeFixtures(FIXTURES, JOB_PROMPTS))]
            ),
            stt=DeepgramPrerecorded("fixture", transport=DeepgramFixtures(FIXTURES)),
        )
        pools += [ctx.pool, ctx.system_pool]
        consumer = Consumer(
            queue=SqsQueue(queue_url, REGION, endpoint_url=SQS_ENDPOINT),
            runs=JobRuns(ctx.pool),
            ctx=ctx,
            concurrency=4,
            receive_wait_s=1,
            heartbeat_s=30.0,
            shutdown_grace_s=5.0,
        )

        gw_settings = GatewaySettings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            **{
                **common,
                "speculative_start_ms": 100,
                "silence_timer_ms": 250,
                "final_wait_max_ms": 500,
            },
        )
        control = TextSTTControl()
        gw_svc = GatewayServices(
            settings=gw_settings,
            store=GatewayStore(make_client(gw_settings), gw_settings),
            jobs=jobs,
            router=gateway_router(gw_settings, [ScriptedProvider("anthropic")]),
            verifier=verifier,
            stt_providers=[("text", lambda: TextSTT(control, "text"))],
        )

        async with ApiServer(api_svc) as api, GatewayServer(gw_svc) as gw:
            worker = asyncio.create_task(consumer.run(stop))
            print(
                json.dumps(
                    {
                        "api": f"http://127.0.0.1:{api.port}/v1",
                        "gateway": gw.url,
                        "token": TOKEN,
                        "user_id": str(uid),
                    }
                ),
                flush=True,
            )
            loop = asyncio.get_running_loop()
            threading.Thread(target=stdin_closed, args=(loop, stop), daemon=True).start()
            await stop.wait()
            await worker
    finally:
        stop.set()
        for pool in pools:
            await pool.close()
        with contextlib.suppress(Exception):
            sqs.delete_queue(QueueUrl=queue_url)
        await db.__aexit__(None, None, None)
    return 0


def main() -> int:
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
