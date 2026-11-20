"""Opt-in real Codex MCP host smoke test; fictional data in a disposable migrated database.

Run with the repository venv and local Docker stack. Uses installed Codex authentication
through its CLI, never reads credentials. No persistent MCP configuration is written.
"""

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import uvicorn

from copilot_core import personal_ai
from copilot_core.auth import Principal

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "target" / "personal-ai-host"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "host_fixture", ROOT / "apps/api/tests/conftest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.api.__wrapped__()
    api = await anext(fixture)
    uid, headers = api.user("Fictional MCP validation person")
    token = headers["Authorization"].removeprefix("Bearer ")
    env = os.environ.copy()
    env["CAREER_PILOT_HOST_TEST_TOKEN"] = token
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    api.svc.settings.personal_ai_oauth_issuer = origin
    api.svc.settings.personal_ai_oauth_clients = {
        "host-fixture": ["http://127.0.0.1:53682/callback"]
    }
    app = api.client._transport.app
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    cli = Path(os.environ["APPDATA"]) / "npm/node_modules/@openai/codex/bin/codex.js"
    assert cli.exists(), "Installed Codex CLI entrypoint not found"
    config = [
        "mcp_servers.career_pilot.url=" + json.dumps(f"http://127.0.0.1:{port}/v1/personal-ai/mcp"),
        'mcp_servers.career_pilot.bearer_token_env_var="CAREER_PILOT_HOST_TEST_TOKEN"',
        "mcp_servers.career_pilot.required=true",
        'mcp_servers.career_pilot.default_tools_approval_mode="approve"',
    ]

    async def host(phase, request_id, revision):
        prompt = (
            "Validate two MCP tools with fictional test data only. Do not use shell, files, "
            "web, other tools or real user information. Call career_pilot get_context_request for "
            f"{request_id}. Inspect scope/revision. Then call publish_context_brief exactly once "
            f"for that request revision {revision}, idempotency_key host-validation-{revision}. "
            "The user explicitly approves this fictional publication. Publish one item id parser, "
            "kind progress, text 'Fictional parser review is complete', provenance user_report, "
            "source_ids []. Use no sources. coverage_gaps ['Fictional user-provided fixture only; "
            "no external source research performed']. Title 'Fictional parser review'. prepared_at "
            f"{datetime.now(UTC).isoformat()}. Report the returned version_id and nothing else."
        )
        command = [
            shutil.which("node"),
            str(cli),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "-C",
            str(OUT),
        ]
        for setting in config:
            command.extend(["-c", setting])
        command.append(prompt)
        proc = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 180)
        except TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            raise RuntimeError("Codex host exceeded 180 second validation limit") from None
        # Fixture token is never retained in evidence, even if a host error prints its environment.
        text = (
            (stdout + b"\n" + stderr).decode("utf-8", "replace").replace(token, "[fixture-token]")
        )
        (OUT / f"{phase}.jsonl").write_text(text, encoding="utf-8")
        print(
            f"{phase}: Codex exit {proc.returncode}; evidence {OUT / (phase + '.jsonl')}",
            flush=True,
        )
        if proc.returncode:
            print(text[-3500:], flush=True)
            raise RuntimeError("Codex host did not complete")

    try:
        assert (await api.client.get("/v1/me", headers=headers)).status_code == 200
        today = datetime.now(UTC).date().isoformat()
        created = await api.client.post(
            "/v1/personal-ai/requests",
            headers=headers,
            json={
                "title": "Fictional host validation",
                "purpose": "ask",
                "questions": ["What is the fictional parser status?"],
                "date_from": today,
                "date_to": today,
            },
        )
        assert created.status_code == 201, created.text
        rid = created.json()["id"]
        path = f"/v1/personal-ai/requests/{rid}"
        verifier = "a" * 43
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        prefix = "/v1/personal-ai/oauth"
        redirect = "http://127.0.0.1:53682/callback"
        page = await api.client.get(
            prefix + "/authorize",
            params={
                "client_id": "host-fixture",
                "redirect_uri": redirect,
                "response_type": "code",
                "resource": origin + "/v1/personal-ai/mcp",
                "scope": "context:read context:publish",
                "code_challenge": digest,
                "code_challenge_method": "S256",
                "state": "fixture",
            },
        )
        assert page.status_code == 200, page.text
        match = re.search(r"handle:'([A-Za-z0-9_-]+)'", page.text)
        assert match
        consent = await api.client.post(
            prefix + "/consent",
            headers={**headers, "Origin": origin},
            json={"handle": match.group(1), "approve": True},
        )
        assert consent.status_code == 200, consent.text
        code = parse_qs(urlsplit(consent.json()["redirect_uri"]).query)["code"][0]
        exchange = await api.client.post(
            prefix + "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "host-fixture",
                "redirect_uri": redirect,
                "code": code,
                "code_verifier": verifier,
                "resource": origin + "/v1/personal-ai/mcp",
            },
        )
        assert exchange.status_code == 200, exchange.text
        token = exchange.json()["access_token"]
        env["CAREER_PILOT_HOST_TEST_TOKEN"] = token
        await host("publish", rid, 1)
        detail = (await api.client.get(path, headers=headers)).json()
        assert detail["active_brief"]["origin"] == "mcp", detail
        first = detail["active_version_id"]
        async with api.svc.tx(Principal(uid, "Fictional", None)) as conn:
            briefs = await personal_ai.latest_briefs(conn, api.svc.crypto, uid, uid)
        assert briefs
        assert "Fictional parser" in personal_ai.context_text(briefs)
        refreshed = await api.client.post(
            path + "/refresh", headers=headers, json={"expected_revision": 1}
        )
        assert refreshed.status_code == 200, refreshed.text
        await host("refresh", rid, 2)
        detail = (await api.client.get(path, headers=headers)).json()
        assert detail["active_version_id"] != first
        assert (await api.client.delete(path, headers=headers)).status_code == 204
        async with api.svc.tx(Principal(uid, "Fictional", None)) as conn:
            assert await personal_ai.latest_briefs(conn, api.svc.crypto, uid, uid) == []
        assert (await api.client.get(path, headers=headers)).status_code == 404
        (OUT / "result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "host": "Codex CLI",
                    "authentication": "OAuth authorization-code S256 PKCE scoped token",
                    "publish": True,
                    "refresh": True,
                    "private_retrieval": True,
                    "remove": True,
                    "external_research": False,
                    "fixture_only": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print("PASS: real host publish/refresh; private retrieval/removal validated", flush=True)
    finally:
        server.should_exit = True
        await serving
        listener.close()
        await fixture.aclose()


if __name__ == "__main__":
    asyncio.run(main())
