"""Worker entry point.

The SQS consumer comes later. For now the process only serves ``GET /healthz`` so the
local stack and the ECS container health check have something to probe. The worker has
no public HTTP API, so this is a minimal stdlib server rather than FastAPI.
"""

import asyncio
import contextlib
import json
import os
import signal

from copilot_core import __version__

SERVICE = "worker"


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
    await serve(os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8082")), stop)


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
