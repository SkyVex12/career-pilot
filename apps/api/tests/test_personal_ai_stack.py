"""Real migrated PostgreSQL/RLS and encryption integration; needs the local stack."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from copilot_api.testing import Api

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]


async def test_request_mcp_publish_refresh_correct_remove_and_account_isolation(api: Api):
    _, alice = api.user("context-alice")
    _, bob = api.user("context-bob")
    for header in (alice, bob):
        assert (await api.client.get("/v1/me", headers=header)).status_code == 200
    created = await api.client.post(
        "/v1/personal-ai/requests",
        headers=alice,
        json={
            "title": "Prepare standup",
            "questions": ["What changed?"],
            "date_from": date.today().isoformat(),
            "date_to": date.today().isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    request = created.json()
    rid = request["id"]
    path = f"/v1/personal-ai/requests/{rid}"
    assert (await api.client.get(path, headers=bob)).status_code == 404
    body = {
        "request_id": rid,
        "request_revision": 1,
        "idempotency_key": "host-run-1",
        "brief": {
            "title": "Standup",
            "prepared_at": datetime.now(UTC).isoformat(),
            "items": [
                {
                    "id": "one",
                    "kind": "progress",
                    "text": "Parser completed",
                    "provenance": "user_report",
                }
            ],
            "sources": [],
            "coverage_gaps": ["No production telemetry available"],
        },
    }
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "publish_context_brief", "arguments": body},
    }
    published = await api.client.post("/v1/personal-ai/mcp", headers=alice, json=envelope)
    assert not published.json()["result"]["isError"], published.text
    vid = published.json()["result"]["structuredContent"]["version_id"]
    duplicate = await api.client.post("/v1/personal-ai/mcp", headers=alice, json=envelope)
    assert duplicate.json()["result"]["structuredContent"]["version_id"] == vid
    assert duplicate.json()["result"]["structuredContent"]["duplicate"]
    corrected = await api.client.patch(
        path + "/items/one",
        headers=alice,
        json={"expected_version_id": vid, "text": "Parser still in review"},
    )
    assert corrected.status_code == 200, corrected.text
    assert (
        await api.client.post(path + "/refresh", headers=alice, json={"expected_revision": 1})
    ).status_code == 200
    stale = await api.client.post("/v1/personal-ai/mcp", headers=alice, json=envelope)
    assert stale.json()["result"]["isError"]
    body.update(request_revision=2, idempotency_key="host-run-2")
    refreshed = await api.client.post("/v1/personal-ai/mcp", headers=alice, json=envelope)
    assert not refreshed.json()["result"]["isError"]
    detail = (await api.client.get(path, headers=alice)).json()
    assert detail["active_brief"]["brief"]["items"][0]["text"] == "Parser still in review"
    assert detail["active_brief"]["origin"] == "mcp"
    # Malformed replacement cannot destroy the last usable version.
    invalid = await api.client.post(
        path + "/publish",
        headers=alice,
        json={"request_revision": 2, "idempotency_key": str(uuid4()), "brief": {}},
    )
    assert invalid.status_code == 422
    assert (await api.client.get(path, headers=alice)).json()["active_version_id"] == detail[
        "active_version_id"
    ]
    assert (await api.client.delete(path, headers=bob)).status_code == 404
    assert (await api.client.delete(path, headers=alice)).status_code == 204
    assert (await api.client.get(path, headers=alice)).status_code == 404
    replay = await api.client.post("/v1/personal-ai/mcp", headers=alice, json=envelope)
    assert replay.json()["result"]["isError"]
