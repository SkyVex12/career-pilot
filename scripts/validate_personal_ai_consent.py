"""Real browser consent validation with local MemoryMailer; never sends real email."""

import asyncio
import importlib.util
import os
import re
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parents[1]


async def main():
    spec = importlib.util.spec_from_file_location(
        "consent_fixture", ROOT / "apps/api/tests/conftest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.api.__wrapped__()
    api = await anext(fixture)
    app = api.client._transport.app
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    api.svc.settings.personal_ai_oauth_issuer = origin
    api.svc.settings.personal_ai_oauth_clients = {"browser-fixture": [origin + "/callback"]}
    control = secrets.token_urlsafe(32)

    @app.post("/_fixture/mail")
    async def captured_mail(request: Request):
        if request.headers.get("authorization") != "Bearer " + control:
            raise HTTPException(403)
        await api.svc.email.drain()
        email = (await request.json())["email"]
        matches = [message for recipient, _, message in api.mailer.sent if recipient == email]
        assert matches
        code = re.search(r"\b[0-9]{6}\b", matches[-1])
        assert code
        return {"code": code.group(0)}

    @app.get("/callback")
    async def callback():
        return HTMLResponse(
            "<!doctype html><html lang=en><title>Fixture callback</title>"
            "<main><h1>Connection returned</h1></main></html>"
        )

    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    env = {
        **os.environ,
        "FIXTURE_ORIGIN": origin,
        "FIXTURE_CONTROL_TOKEN": control,
        "PW_DIR": os.environ.get(
            "PW_DIR", "C:/Users/A/.cache/codex-runtimes/codex-primary-runtime/dependencies/node"
        ),
    }
    node = shutil.which("node")
    assert node is not None
    try:
        process = await asyncio.create_subprocess_exec(
            node,
            str(ROOT / "scripts/validate_personal_ai_consent.mjs"),
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 120)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise RuntimeError("Browser consent validation timed out") from None
        print(output.decode("utf-8", "replace").replace(control, "[fixture-control]"), flush=True)
        status = process.returncode
        if status:
            raise RuntimeError(f"Browser consent validation failed: {status}")
    finally:
        server.should_exit = True
        await serving
        listener.close()
        await fixture.aclose()


if __name__ == "__main__":
    asyncio.run(main())
