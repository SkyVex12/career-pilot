"""Opt-in real nginx forwarding against a localhost-only fictional upstream.
Run COPILOT_PROXY_TEST=1 pytest tests/test_proxy_forwarding.py (local Docker required).
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
import websockets

pytestmark = pytest.mark.skipif(
    os.environ.get("COPILOT_PROXY_TEST") != "1",
    reason="Explicit local Docker forwarding validation",
)
ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30, check=True)


async def test_actual_nginx_companion_upgrade_and_rest_fallback(tmp_path):
    backend, edge = free_port(), free_port()
    config = (ROOT / "docker/proxy/nginx.conf").read_text(encoding="utf-8")
    config = config.replace("127.0.0.1:8081", f"host.docker.internal:{backend}").replace(
        "127.0.0.1:8080", f"host.docker.internal:{backend}"
    )
    path = tmp_path / "nginx.conf"
    path.write_text(config, encoding="utf-8")
    name = "companion-proxy-fixture-" + uuid4().hex[:12]
    process = await asyncio.create_subprocess_exec(
        *[
            sys.executable,
            str(ROOT / "apps/mobile/scripts/native_fixture.py"),
            "--port",
            str(backend),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    started = False
    try:
        async with httpx2.AsyncClient(timeout=5) as http:
            deadline = time.monotonic() + 10
            while True:
                try:
                    response = await http.get(f"http://127.0.0.1:{backend}/healthz")
                    assert response.json()["fixture"] is True
                    break
                except httpx2.ConnectError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.1)
            docker(
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--publish",
                f"127.0.0.1:{edge}:80",
                "--mount",
                f"type=bind,source={path.resolve().as_posix()},target=/etc/nginx/nginx.conf,readonly",
                "public.ecr.aws/nginx/nginx:1.29-alpine",
            )
            started = True
            headers = {
                "Authorization": "Bearer fictional-native-fixture-access",
                "x-copilot-profile": "11111111-1111-4111-8111-111111111111",
            }
            response = await http.post(
                f"http://127.0.0.1:{edge}/v1/companion/sessions/22222222-2222-4222-8222-222222222222/attach",
                headers=headers,
                json={"device_name": "Nginx fixture"},
            )
            assert response.status_code == 200, response.text
            async with websockets.connect(
                f"ws://127.0.0.1:{edge}/v1/companion", open_timeout=5
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "companion.join",
                            "attachment_id": response.json()["attachment_id"],
                            "access_token": "fictional-native-fixture-access",
                        }
                    )
                )
                for kind in ("companion.ready", "companion.snapshot", "companion.host"):
                    message = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    assert message["type"] == kind
    finally:
        if started:
            docker("stop", name)
        process.terminate()
        await asyncio.wait_for(process.wait(), 10)
