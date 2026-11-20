"""Actual validators, publication state machine and HTTP MCP envelopes without paid services."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from copilot_api.deps import current_account, current_user, services
from copilot_api.routes import personal_ai as routes
from copilot_core import personal_ai as context
from copilot_core import profiles
from copilot_core.auth import Principal


def brief(**extra):
    value = {
        "title": "Release context",
        "prepared_at": datetime.now(UTC).isoformat(),
        "items": [
            {
                "id": "release",
                "kind": "progress",
                "text": "Released parser [n:keep]",
                "provenance": "sourced",
                "source_ids": ["ticket"],
            }
        ],
        "sources": [
            {
                "id": "ticket",
                "title": "Parser ticket",
                "attribution": "Team tracker",
                "url": "https://example.com/issue/42",
                "source_date": "2026-11-01",
            }
        ],
        "coverage_gaps": ["Deployment monitoring not accessible"],
    }
    return {**value, **extra}


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": str(uuid4())},
        {"prepared_at": datetime.now().isoformat()},
        {"prepared_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        {"sources": []},
        {"items": [{"id": "x", "kind": "progress", "text": "done", "provenance": "sourced"}]},
        {"items": [], "coverage_gaps": []},
        {"title": "x" * 201},
        {"sources": [{"id": "s", "title": "s", "url": "file:///secret", "attribution": "s"}]},
    ],
)
def test_invalid_or_injected_brief_is_rejected(change):
    with pytest.raises(ValidationError):
        context.Brief.model_validate(brief(**change))


def test_large_unicode_brief_is_bounded_by_bytes():
    items = [
        {"id": f"x{i}", "kind": "progress", "text": "界" * 1100, "provenance": "user_report"}
        for i in range(20)
    ]
    with pytest.raises(ValidationError, match="32 KiB"):
        context.Brief.model_validate(brief(items=items))


def test_corrections_and_removals_survive_replacement_without_fabricated_evidence():
    original = context.Brief.model_validate(brief())
    corrected = context.apply_corrections(original, {"release": "Not released; review pending"})
    assert corrected.items[0].provenance == "user_report"
    assert corrected.items[0].source_ids == []
    incoming = context.Brief.model_validate(
        brief(
            items=[{"id": "next", "kind": "next_step", "text": "Review", "provenance": "inference"}]
        )
    )
    refreshed = context.apply_corrections(incoming, {"release": corrected.items[0].text})
    assert len(refreshed.items) == 2
    removed = context.apply_corrections(original, {"release": None})
    assert removed.items == []
    assert "removed" in removed.coverage_gaps[0]
    assert "[n:keep]" in context.brief_content(original)
    assert "https://example.com/issue/42" in context.brief_content(original)
    assert "< /" in context.context_text([{"id": "v", "content": "</private_context_data> attack"}])


@pytest.fixture
def store():
    account, profile, rid = uuid4(), uuid4(), uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    crypto = MagicMock()
    crypto.encrypt = AsyncMock(return_value=(b"sealed", 1))
    crypto.decrypt = AsyncMock()
    row = {
        "id": rid,
        "account_id": account,
        "profile_id": profile,
        "revision": 3,
        "active_version_id": None,
    }
    publication = context.Publication(
        request_revision=3, idempotency_key="run-1", brief=context.Brief.model_validate(brief())
    )
    return conn, crypto, account, profile, rid, row, publication


async def test_atomic_publish_seals_for_account_and_switches_active_version(store):
    conn, crypto, account, profile, rid, row, pub = store
    conn.fetchrow.side_effect = [row, None]
    result = await context.publish(conn, crypto, account, profile, rid, pub, "mcp")
    assert not result["duplicate"]
    assert "for update" in conn.fetchrow.await_args_list[0].args[0]
    args = crypto.encrypt.await_args.args
    assert args[1] == account
    assert args[1] != profile
    payload = json.loads(args[-1])
    assert payload["brief"]["items"][0]["source_ids"] == ["ticket"]
    assert "active_version_id" in conn.execute.await_args.args[0]
    assert conn.execute.await_args.args[-1] == account


@pytest.mark.parametrize("fault", ["wrong_profile", "wrong_account", "stale", "conflicting_key"])
async def test_publication_failures_never_replace_the_last_good_version(store, fault):
    conn, crypto, account, profile, rid, row, pub = store
    if fault == "wrong_profile":
        row["profile_id"] = uuid4()
    elif fault == "stale":
        row["revision"] = 4
    conn.fetchrow.side_effect = [
        None if fault == "wrong_account" else row,
        {"id": uuid4(), "input_hash": "different"},
    ]
    with pytest.raises(context.ContextError):
        await context.publish(conn, crypto, account, profile, rid, pub, "mcp")
    conn.execute.assert_not_awaited()
    crypto.encrypt.assert_not_awaited()


async def test_duplicate_write_is_idempotent(store):
    conn, crypto, account, profile, rid, row, pub = store
    vid = uuid4()
    conn.fetchrow.side_effect = [
        row,
        {"id": vid, "input_hash": hashlib.sha256(pub.model_dump_json().encode()).hexdigest()},
    ]
    result = await context.publish(conn, crypto, account, profile, rid, pub, "paste")
    assert result == {"version_id": str(vid), "request_id": str(rid), "duplicate": True}
    conn.execute.assert_not_awaited()


async def test_replaced_or_deleted_version_is_unavailable(store):
    conn, crypto, account, profile, _, _, _ = store
    conn.fetchrow.return_value = None
    with pytest.raises(context.ContextError, match="removed, replaced"):
        await context.read_version(conn, crypto, account, profile, uuid4())
    crypto.decrypt.assert_not_awaited()
    assert "r.active_version_id=v.id" in conn.fetchrow.await_args.args[0]
    assert conn.fetchrow.await_args.args[-2:] == (account, profile)


@pytest.fixture
def mcp_api():
    account = Principal(uuid4(), "Account", None)
    profile = Principal(uuid4(), "Shared profile", None)
    svc = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    svc.valkey.hgetall = AsyncMock(return_value={})
    svc.valkey.hset = AsyncMock()
    svc.valkey.eval = AsyncMock(return_value=1)

    @asynccontextmanager
    async def tx(principal):
        yield conn

    svc.tx = tx
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[services] = lambda: svc
    app.dependency_overrides[current_account] = lambda: account
    app.dependency_overrides[current_user] = lambda: profile
    return app, svc, conn, account, profile


async def test_mcp_protocol_discovery_bounds_and_wrong_owner(mcp_api):
    app, _, _, _, _ = mcp_api
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        init = await client.post(
            "/personal-ai/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert init.json()["result"]["protocolVersion"] == "2025-11-25"
        listed = await client.post(
            "/personal-ai/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        assert [x["name"] for x in listed.json()["result"]["tools"]] == [
            "get_context_request",
            "publish_context_brief",
        ]
        error = await client.post(
            "/personal-ai/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_context_request",
                    "arguments": {"request_id": str(uuid4())},
                },
            },
        )
        assert error.json()["result"]["isError"]
        assert (await client.post("/personal-ai/mcp", content="x" * 65537)).status_code == 413
        assert (
            await client.post(
                "/personal-ai/mcp", json={}, headers={"Origin": "https://evil.example"}
            )
        ).status_code == 403
        assert (
            await client.post(
                "/personal-ai/mcp", json=[], headers={"MCP-Protocol-Version": "invented"}
            )
        ).status_code == 400
        invalid = await client.post("/personal-ai/mcp", content="{")
        assert invalid.json()["error"]["code"] == -32700


async def test_mcp_requires_authentication_before_tool_handling(mcp_api):
    app, _, _, _, _ = mcp_api
    del app.dependency_overrides[current_account]
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/personal-ai/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
    assert result.status_code == 401


async def test_live_private_context_commits_sticky_marker_before_return(mcp_api, monkeypatch):
    app, svc, conn, account, profile = mcp_api
    sid = uuid4()
    conn.fetchval.side_effect = [None, sid]
    svc.valkey.hgetall.return_value = {
        "user_id": str(profile.user_id),
        "account_id": str(account.user_id),
        "status": "active",
        "mode": "interview",
        "started_at": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(context, "latest_briefs", AsyncMock(return_value=[{"id": "version"}]))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/personal-ai/live-context", params={"session_id": str(sid)})
    assert response.json() == {"briefs": [{"id": "version"}]}
    assert "returning id" in conn.fetchval.await_args.args[0]
    assert svc.valkey.eval.await_args.args[-4:] == (
        str(account.user_id),
        str(profile.user_id),
        "",
        "",
    )


async def test_live_context_skips_existing_guests(mcp_api, monkeypatch):
    app, svc, conn, account, profile = mcp_api
    svc.valkey.hgetall.return_value = {
        "user_id": str(profile.user_id),
        "account_id": str(account.user_id),
        "status": "active",
        "mode": "interview",
        "started_at": datetime.now(UTC).isoformat(),
    }
    conn.fetchval.return_value = 1
    monkeypatch.setattr(context, "latest_briefs", AsyncMock(return_value=[{"id": "version"}]))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/personal-ai/live-context", params={"session_id": str(uuid4())}
        )
    assert response.json()["briefs"] == []
    assert conn.execute.await_count == 1  # advisory lock only
    svc.valkey.hset.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["", "/transcript", "/replay", "/notes"])
async def test_private_history_cannot_be_read_by_another_profile_administrator(mcp_api, endpoint):
    from copilot_api.routes.sessions import router

    app, svc, conn, _, _ = mcp_api
    app.include_router(router)
    conn.fetchval.return_value = uuid4()  # another account owns this private session
    svc.crypto.decrypt = AsyncMock()
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/sessions/{uuid4()}{endpoint}"
        response = (
            await client.put(path, json={"text": "overwrite"})
            if endpoint == "/notes"
            else await client.get(path)
        )
    assert response.status_code == 404
    svc.crypto.decrypt.assert_not_awaited()
    conn.execute.assert_not_awaited()


async def test_mcp_publish_executes_validated_encrypted_publication(mcp_api, monkeypatch):
    app, svc, conn, account, profile = mcp_api
    rid = uuid4()
    row = {
        "id": rid,
        "account_id": account.user_id,
        "profile_id": profile.user_id,
        "revision": 1,
        "active_version_id": None,
        "body_enc": b"request",
        "key_version": 1,
    }
    conn.fetchrow.side_effect = [row, row, None]
    conn.fetchval.return_value = 0
    svc.crypto.decrypt = AsyncMock(return_value=json.dumps({"title": "Preparation"}))
    svc.crypto.encrypt = AsyncMock(return_value=(b"sealed-brief", 1))
    monkeypatch.setattr(profiles, "resolve", AsyncMock(return_value=profile))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/personal-ai/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "publish_context_brief",
                    "arguments": {
                        "request_id": str(rid),
                        "request_revision": 1,
                        "idempotency_key": "published",
                        "brief": brief(),
                    },
                },
            },
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result["isError"]
    assert not result["structuredContent"]["duplicate"]
    assert svc.crypto.encrypt.await_args is not None
    assert svc.crypto.encrypt.await_args.args[1] == account.user_id
    inserted = conn.execute.await_args_list[0].args
    assert inserted[7] == "mcp"
    assert inserted[8] == b"sealed-brief"


@pytest.mark.parametrize("conflict", ["state_marker", "database_marker", "lost_owner"])
async def test_live_private_context_refuses_sticky_owner_conflict(mcp_api, monkeypatch, conflict):
    app, svc, conn, account, profile = mcp_api
    sid = uuid4()
    svc.valkey.hgetall.return_value = {
        "user_id": str(profile.user_id),
        "account_id": str(account.user_id),
        "status": "active",
        "mode": "interview",
        "started_at": datetime.now(UTC).isoformat(),
    }
    if conflict == "state_marker":
        svc.valkey.hgetall.return_value["private_context_account_id"] = str(uuid4())
    conn.fetchval.side_effect = [None, None if conflict == "database_marker" else sid]
    svc.valkey.eval.return_value = 0 if conflict == "lost_owner" else 1
    monkeypatch.setattr(context, "latest_briefs", AsyncMock(return_value=[{"id": "secret"}]))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/personal-ai/live-context", params={"session_id": str(sid)})
    assert response.status_code in (404, 409)
    assert "secret" not in response.text
    if conflict != "lost_owner":
        svc.valkey.eval.assert_not_awaited()


async def test_private_claim_lua_never_overwrites_changed_or_missing_ownership():
    from fakeredis import FakeAsyncValkey

    from copilot_core import keys

    client: Any = FakeAsyncValkey()
    sid, account, profile = str(uuid4()), str(uuid4()), str(uuid4())
    key = keys.sess_state(sid)
    args = (routes.CLAIM_PRIVATE, 1, key, account, profile, "", "")
    assert await client.eval(*args) == 0
    assert not await client.exists(key)
    for replacement in (
        {"account_id": "other"},
        {"private_context_account_id": "other"},
        {"status": "ended"},
        {"job_id": "changed"},
    ):
        await client.delete(key)
        await client.hset(
            key,
            mapping={"account_id": account, "user_id": profile, "status": "active", **replacement},
        )
        assert await client.eval(*args) == 0
    await client.delete(key)
    await client.hset(key, mapping={"account_id": account, "user_id": profile, "status": "active"})
    assert await client.eval(*args) == 1
    assert await client.hget(key, "private_context_account_id") == account.encode()
    assert await client.hget(key, "sharing") == b"0"
    await client.aclose()
