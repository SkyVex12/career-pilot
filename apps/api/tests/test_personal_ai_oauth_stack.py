"""MCP OAuth on migrated PostgreSQL/RLS and real Valkey, with fictional accounts."""

import asyncio
import base64
import hashlib
import json
import re
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from copilot_api.routes.personal_ai_oauth import key
from copilot_api.testing import Api

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]
BASE = "https://api.test"
REDIRECT = "http://127.0.0.1:53682/callback"
VERIFIER = "a" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
PREFIX = "/v1/personal-ai/oauth"


async def prepare(
    api: Api, headers: dict[str, str], scope="context:read context:publish", **changes
):
    params = {
        "client_id": "fixture-host",
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "original-state",
        "resource": BASE + "/v1/personal-ai/mcp",
        "scope": scope,
        **changes,
    }
    response = await api.client.get(PREFIX + "/authorize", params=params)
    assert response.status_code == 200, response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    match = re.search(r"handle:'([A-Za-z0-9_-]+)'", response.text)
    assert match is not None
    handle = match.group(1)
    consent = await api.client.post(
        PREFIX + "/consent",
        json={"handle": handle, "approve": True},
        headers={**headers, "Origin": BASE},
    )
    assert consent.status_code == 200, consent.text
    target = consent.json()["redirect_uri"]
    query = parse_qs(urlsplit(target).query)
    assert query["state"] == ["original-state"]
    assert query["iss"] == [BASE]
    return query["code"][0]


async def exchange(api: Api, code: str, **changes):
    return await api.client.post(
        PREFIX + "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "client_id": "fixture-host",
            "redirect_uri": REDIRECT,
            "resource": BASE + "/v1/personal-ai/mcp",
            **changes,
        },
    )


async def test_oauth_pkce_single_use_scopes_hash_storage_revocation_and_account_boundary(api: Api):
    api.svc.settings.personal_ai_oauth_issuer = BASE
    api.svc.settings.personal_ai_oauth_clients = {"fixture-host": [REDIRECT]}
    uid, alice = api.user("OAuth fiction Alice")
    _, bob = api.user("OAuth fiction Bob")
    for headers in (alice, bob):
        assert (await api.client.get("/v1/me", headers=headers)).status_code == 200
    metadata = await api.client.get("/.well-known/oauth-authorization-server")
    assert metadata.json()["code_challenge_methods_supported"] == ["S256"]
    protected = await api.client.get("/.well-known/oauth-protected-resource/v1/personal-ai/mcp")
    assert protected.json()["resource"] == BASE + "/v1/personal-ai/mcp"
    missing = await api.client.post("/v1/personal-ai/mcp", json={})
    assert missing.status_code == 401
    assert "resource_metadata=" in missing.headers["www-authenticate"]
    code = await prepare(api, alice, "context:read")
    assert (await exchange(api, code, code_verifier="b" * 43)).status_code == 400
    assert (await exchange(api, code, resource="https://other.example/mcp")).status_code == 400
    response = await exchange(api, code)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    token = response.json()["access_token"]
    raw = await api.svc.valkey.get(key("access", token))
    assert token.encode() not in raw
    assert (await exchange(api, code)).json()["error"] == "invalid_grant"
    oauth = {"Authorization": "Bearer " + token}
    listed = await api.client.post(
        "/v1/personal-ai/mcp",
        headers=oauth,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert [t["name"] for t in listed.json()["result"]["tools"]] == ["get_context_request"]
    denied = await api.client.post(
        "/v1/personal-ai/mcp",
        headers=oauth,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "publish_context_brief", "arguments": {}},
        },
    )
    assert denied.status_code == 403
    assert 'error="insufficient_scope"' in denied.headers["www-authenticate"]
    assert (await api.client.get("/v1/me", headers=oauth)).status_code == 401
    connections = (await api.client.get(PREFIX + "/connections", headers=alice)).json()[
        "connections"
    ]
    assert len(connections) == 1
    identifier = connections[0]["id"]
    assert (await api.client.get(PREFIX + "/connections", headers=bob)).json()["connections"] == []
    await api.client.delete(PREFIX + "/connections/" + identifier, headers=bob)
    assert await api.svc.valkey.get(key("access", token))
    await api.client.delete(PREFIX + "/connections/" + identifier, headers=alice)
    assert await api.svc.valkey.get(key("access", token)) is None
    assert (await api.client.post("/v1/personal-ai/mcp", headers=oauth, json={})).status_code == 401
    # Existing issued tokens also stop at account deletion and do not recreate the user.
    second = (await exchange(api, await prepare(api, alice))).json()["access_token"]
    async with api.admin.transaction():
        await api.admin.execute("delete from app.workspaces where id=$1", uid)
        await api.admin.execute("delete from app.accounts where id=$1", uid)
    assert (
        await api.client.post(
            "/v1/personal-ai/mcp", headers={"Authorization": "Bearer " + second}, json={}
        )
    ).status_code == 401
    await api.svc.valkey.delete(key("access", second), "mcp-oauth:account:" + str(uid))


async def test_oauth_rejects_redirect_pkce_scope_origin_and_expired_codes(api: Api):
    api.svc.settings.personal_ai_oauth_issuer = BASE
    api.svc.settings.personal_ai_oauth_clients = {"fixture-host": [REDIRECT]}
    _, alice = api.user("OAuth fiction")
    await api.client.get("/v1/me", headers=alice)
    valid = {
        "client_id": "fixture-host",
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "state",
        "resource": BASE + "/v1/personal-ai/mcp",
        "scope": "context:read",
    }
    for change in (
        {"redirect_uri": "https://evil.example/cb"},
        {"code_challenge_method": "plain"},
        {"scope": "app:admin"},
        {"resource": "https://other.example/mcp"},
        {"client_id": "unknown"},
    ):
        response = await api.client.get(PREFIX + "/authorize", params={**valid, **change})
        assert response.status_code == 400
        assert "error" in response.json()
        assert "location" not in response.headers
    response = await api.client.get(PREFIX + "/authorize", params=valid)
    match = re.search(r"handle:'([A-Za-z0-9_-]+)'", response.text)
    assert match is not None
    handle = match.group(1)
    denied = await api.client.post(
        PREFIX + "/consent",
        headers={**alice, "Origin": "https://evil.example"},
        json={"handle": handle, "approve": True},
    )
    assert denied.status_code == 403
    cancelled = await api.client.post(
        PREFIX + "/consent",
        headers={**alice, "Origin": BASE},
        json={"handle": handle, "approve": False},
    )
    assert "error=access_denied" in cancelled.json()["redirect_uri"]
    repeat = await api.client.post(
        PREFIX + "/consent",
        headers={**alice, "Origin": BASE},
        json={"handle": handle, "approve": True},
    )
    assert repeat.status_code == 400
    code = await prepare(api, alice)
    await api.svc.valkey.delete(key("code", code))
    assert (await exchange(api, code)).json()["error"] == "invalid_grant"
    await api.client.post(
        PREFIX + "/revoke", data={"token": "unknown", "client_id": "fixture-host"}
    )


async def test_oauth_concurrent_redemption_grant_cap_expiry_and_explicit_revoke(
    api: Api, monkeypatch
):
    from copilot_api.routes import personal_ai_oauth

    # Capacity boundaries are tested independently from the per-peer traffic limiter.
    monkeypatch.setattr(personal_ai_oauth, "bounded", AsyncMock())
    api.svc.settings.personal_ai_oauth_issuer = BASE
    api.svc.settings.personal_ai_oauth_clients = {"fixture-host": [REDIRECT]}
    uid, alice = api.user("OAuth capacity fiction")
    await api.client.get("/v1/me", headers=alice)
    code = await prepare(api, alice)
    attempts = await asyncio.gather(exchange(api, code), exchange(api, code))
    assert sorted(r.status_code for r in attempts) == [200, 400]
    tokens = [next(r.json()["access_token"] for r in attempts if r.status_code == 200)]
    for _ in range(19):
        response = await exchange(api, await prepare(api, alice))
        assert response.status_code == 200
        tokens.append(response.json()["access_token"])
    capped = await exchange(api, await prepare(api, alice))
    assert capped.json()["error"] == "access_denied"
    await api.svc.valkey.delete(key("access", tokens[0]))
    replacement = await exchange(api, await prepare(api, alice))
    assert replacement.status_code == 200
    access = replacement.json()["access_token"]
    tokens.append(access)
    oauth = {"Authorization": "Bearer " + access}
    record = json.loads(await api.svc.valkey.get(key("access", access)))
    for change in ({"expires_at": 0}, {"resource": "https://wrong.example/mcp"}):
        await api.svc.valkey.set(key("access", access), json.dumps({**record, **change}), ex=60)
        assert (
            await api.client.post("/v1/personal-ai/mcp", headers=oauth, json={})
        ).status_code == 401
    await api.svc.valkey.set(key("access", access), json.dumps(record), ex=60)
    api.svc.settings.personal_ai_oauth_clients = {}
    assert (await api.client.post("/v1/personal-ai/mcp", headers=oauth, json={})).status_code == 401
    api.svc.settings.personal_ai_oauth_clients = {"fixture-host": [REDIRECT]}
    response = await api.client.post(
        PREFIX + "/revoke", data={"token": access, "client_id": "fixture-host"}
    )
    assert response.status_code == 200
    assert await api.svc.valkey.get(key("access", access)) is None
    assert (await api.client.post("/v1/personal-ai/mcp", headers=oauth, json={})).status_code == 401
    await api.svc.valkey.delete(
        *(key("access", value) for value in tokens), "mcp-oauth:account:" + str(uid)
    )


async def test_registered_loopback_allows_only_port_variation(api: Api):
    from copilot_api.routes.personal_ai_oauth import redirect_allowed

    api.svc.settings.personal_ai_oauth_clients = {"fixture-host": ["http://127.0.0.1/callback"]}
    assert redirect_allowed(api.svc, "fixture-host", "http://127.0.0.1:55443/callback")
    for uri in (
        "http://localhost:55443/callback",
        "http://127.0.0.2:55443/callback",
        "http://127.0.0.1:55443/other",
        "http://127.0.0.1:55443/callback?other=1",
        "http://evil@127.0.0.1:55443/callback",
        "http://127.0.0.1:999999/callback",
    ):
        assert not redirect_allowed(api.svc, "fixture-host", uri)
