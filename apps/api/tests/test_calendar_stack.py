"""Real database/RLS/encryption lifecycle; requires the repository's local stack."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2
import pytest

from copilot_api.routes import ask, calendar
from copilot_api.testing import Api

pytestmark = pytest.mark.stack


async def test_calendar_consent_cache_rls_and_disconnect(api: Api, monkeypatch) -> None:
    for key, value in {
        "CLIENT_ID": "client",
        "CLIENT_SECRET": "server-secret",
        "REDIRECT_URI": "https://example.test/v1/calendars/google/callback",
    }.items():
        monkeypatch.setenv("COPILOT_CALENDAR_GOOGLE_" + key, value)
    now = datetime.now(UTC)
    calls = []

    def provider(request):
        calls.append(request)
        if request.url.path == "/token":
            return httpx2.Response(
                200,
                json={
                    "access_token": "private-access",
                    "refresh_token": "private-refresh",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("calendarList"):
            return httpx2.Response(200, json={"items": [{"id": "primary", "summary": "Work"}]})
        if request.url.path.endswith("events"):
            return httpx2.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "meeting",
                            "summary": "Interview with Acme",
                            "start": {"dateTime": (now + timedelta(minutes=5)).isoformat()},
                            "end": {"dateTime": (now + timedelta(minutes=35)).isoformat()},
                        }
                    ]
                },
            )
        if request.url.path == "/revoke":
            return httpx2.Response(503)
        raise AssertionError(str(request.url))

    monkeypatch.setattr(
        calendar,
        "provider_http",
        lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(provider)),
    )
    owner, headers = api.user()
    _, other = api.user()
    response = await api.client.post("/v1/calendars/google/connect", headers=headers)
    assert response.status_code == 200, response.text
    state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
    response = await api.client.get(
        "/v1/calendars/google/callback", params={"state": state, "code": "code"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    replay = await api.client.get(
        "/v1/calendars/google/callback", params={"state": state, "code": "code"}
    )
    assert replay.status_code == 400
    row = await api.admin.fetchrow("select * from app.calendar_connections where user_id=$1", owner)
    assert row is not None
    assert b"private-refresh" not in bytes(row["token_enc"])
    assert b"Work" not in bytes(row["cache_enc"])
    selection = await api.client.patch(
        "/v1/calendars/google/selection", headers=headers, json={"calendar_ids": ["primary"]}
    )
    assert selection.status_code == 200
    result = await api.client.post("/v1/calendars/google/sync", headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["event_count"] == 1
    preparation = await api.client.get("/v1/meeting-preparation", headers=headers)
    assert preparation.status_code == 200
    assert preparation.json()["items"][0]["meeting"]["title"] == "Interview with Acme"
    # Ask uses only selected account-owned cached events and rechecks on source open.
    model = AsyncMock(return_value="Your Acme interview is upcoming [S1].")
    monkeypatch.setattr(ask, "answer", model)
    convo = await api.client.post("/v1/ask/conversations", headers=headers, json={})
    ask_url = "/v1/ask/conversations/" + convo.json()["id"]
    await api.client.post(
        ask_url + "/messages",
        headers=headers,
        json={"request_id": str(uuid4()), "question": "Acme interview"},
    )
    detail = (await api.client.get(ask_url, headers=headers)).json()
    assert detail["messages"][0]["status"] == "done"
    citation = next(s for s in detail["messages"][0]["sources"] if s["kind"] == "calendar")
    source_url = "/v1/ask/sources/calendar/" + citation["id"]
    opened = await api.client.get(source_url, headers=headers)
    assert opened.status_code == 200
    assert "not proof" in opened.json()["content"]
    assert (await api.client.get(source_url, headers=other)).status_code == 404
    forbidden = await api.client.delete("/v1/calendars/google", headers=other)
    assert forbidden.status_code == 404
    assert (await api.client.get("/v1/meeting-preparation", headers=other)).json()["items"] == []
    throttled = await api.client.post("/v1/calendars/google/sync", headers=headers)
    assert throttled.status_code == 429
    deleted = await api.client.delete("/v1/calendars/google", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["provider_consent_removal_required"]
    assert (
        await api.admin.fetchval(
            "select count(*) from app.calendar_connections where user_id=$1", owner
        )
        == 0
    )
    assert (await api.client.get("/v1/meeting-preparation", headers=headers)).json()["items"] == []
    assert (await api.client.get(source_url, headers=headers)).status_code == 404
    assert not api.take("session.create")
