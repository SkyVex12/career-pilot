import asyncio
import json

from copilot_worker.main import handle_http


async def _get(path: str) -> tuple[str, dict[str, str]]:
    server = await asyncio.start_server(handle_http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
    head, _, body = raw.decode().partition("\r\n\r\n")
    return head.splitlines()[0], json.loads(body)


async def test_healthz() -> None:
    status, body = await _get("/healthz")
    assert status == "HTTP/1.1 200 OK"
    assert body["status"] == "ok"
    assert body["service"] == "worker"


async def test_unknown_path() -> None:
    status, _ = await _get("/nope")
    assert status == "HTTP/1.1 404 Not Found"
