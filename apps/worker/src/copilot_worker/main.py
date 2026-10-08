"""Worker entry point: the SQS consumer (copilot_worker.runtime) and ``GET /healthz``.

The worker has no public HTTP API, so the health check is a minimal stdlib server rather than
FastAPI. SIGTERM stops receiving and drains running jobs (see runtime.Consumer).
"""

import asyncio
import contextlib
import json
import os
import signal

from copilot_core import __version__, logs
from copilot_core.dotenv import load_dotenv
from copilot_worker import documents, packs, sessionlog, system
from copilot_worker.config import WorkerSettings
from copilot_worker.context import build_ctx, close_ctx
from copilot_worker.runtime import Consumer, JobRuns, SqsQueue

SERVICE = "worker"
HANDLER_MODULES = (documents, packs, sessionlog, system)  # importing them registers handlers


def health_body() -> bytes:
    return json.dumps({"status": "ok", "service": SERVICE, "version": __version__}).encode()


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await reader.readline()
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass  # skip headers
        parts = request_line.decode("latin-1").split()
        if len(parts) >= 2 and parts[0] == "GET" and parts[1] == "/healthz":
            status, body = "200 OK", health_body()
        else:
            status, body = "404 Not Found", b'{"status":"not_found"}'
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def serve(host: str, port: int, stop: asyncio.Event) -> None:
    server = await asyncio.start_server(handle_http, host, port)
    async with server:
        print(f"{SERVICE}: health check on http://{host}:{port}/healthz", flush=True)
        await stop.wait()


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler is not available on Windows; Ctrl+C still raises there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    settings = WorkerSettings()
    ctx = await build_ctx(settings)
    consumer = Consumer(
        queue=SqsQueue(
            settings.sqs_queue_url, settings.aws_region, endpoint_url=settings.sqs_endpoint or None
        ),
        runs=JobRuns(ctx.pool),
        ctx=ctx,
        concurrency=settings.concurrency,
        receive_wait_s=settings.receive_wait_s,
        heartbeat_s=settings.heartbeat_s,
        shutdown_grace_s=settings.shutdown_grace_s,
    )
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8082"))
    health = asyncio.create_task(serve(host, port, stop))
    try:
        await consumer.run(stop)
    finally:
        stop.set()
        await health
        await close_ctx(ctx)


def run() -> None:
    load_dotenv()
    logs.configure("worker")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
