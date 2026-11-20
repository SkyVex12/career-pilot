"""Real database/RLS/encryption lifecycle; requires the repository's local stack."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2
import pytest

from copilot_api.routes import ask, calendar
from copilot_api.testing import Api
from copilot_core import ask as core_ask

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
    # Provider authorization failure cannot leave stale private events usable as evidence.
    await api.admin.execute(
        "update app.calendar_connections set status='reauth_required' where user_id=$1", owner
    )
    assert (await api.client.get("/v1/meeting-preparation", headers=headers)).json()["items"] == []
    from copilot_core.auth import Principal

    principal = Principal(owner, "Owner", None)
    assert await ask.calendar_sources(api.svc, principal, principal, core_ask.Scope()) == []
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


async def test_stale_calendar_failure_cannot_poison_new_authorization(api: Api):
    from copilot_core.auth import Principal
    from copilot_core.calendars import CalendarError

    owner, headers = api.user()
    assert (await api.client.get("/v1/me", headers=headers)).status_code == 200
    cid = uuid4()
    old = await api.admin.fetchval(
        "insert into app.calendar_connections(id,user_id,profile_id,provider,status)"
        " values($1,$2,$2,'google','active') returning updated_at",
        cid,
        owner,
    )
    await api.admin.execute(
        "update app.calendar_connections set updated_at=updated_at+interval '1 second' where id=$1",
        cid,
    )
    await calendar.mark_failed(
        api.svc,
        Principal(owner, "Owner", None),
        cid,
        CalendarError("Expired old consent", reauthorize=True),
        expected_updated_at=old,
    )
    assert (
        await api.admin.fetchval("select status from app.calendar_connections where id=$1", cid)
        == "active"
    )


async def test_preparation_correction_saved_notes_and_completed_recurring_actions(api: Api):
    import json

    from copilot_core.auth import Principal

    owner, headers = api.user()
    other, other_headers = api.user()
    await api.client.get("/v1/me", headers=headers)
    await api.client.get("/v1/me", headers=other_headers)
    app = await api.client.post(
        "/v1/applications",
        headers=headers,
        json={"company": "Unmatched company", "role": "Engineer", "jd_text": "Build systems"},
    )
    assert app.status_code == 201, app.text
    app_id = app.json()["id"]
    foreign_app = await api.client.post(
        "/v1/applications",
        headers=other_headers,
        json={"company": "Private other company", "role": "Engineer", "jd_text": "Other work"},
    )
    now = datetime.now(UTC)
    sid, private_sid, live_sid = uuid4(), uuid4(), uuid4()
    for session_id, ended, private in (
        (sid, True, None),
        (private_sid, True, other),
        (live_sid, False, None),
    ):
        await api.admin.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at,ended_at,"
            "title,private_context_account_id) values($1,$2,'meeting',false,$3,$4,$5,$6)",
            session_id,
            owner,
            now - timedelta(hours=1),
            now if ended else None,
            "Design review" if session_id == sid else "Unavailable call",
            private,
        )
    notes = await api.client.put(
        f"/v1/sessions/{sid}/notes", headers=headers, json={"text": "Use the reviewed design."}
    )
    assert notes.status_code == 200, notes.text
    connection_id = uuid4()
    events = [
        {
            "calendar_id": "primary",
            "instance_id": event,
            "title": "Weekly check-in",
            "start": (now + timedelta(minutes=5)).isoformat(),
            "end": (now + timedelta(minutes=25)).isoformat(),
            "all_day": False,
            "series_id": "other-series" if event == "other" else "weekly",
        }
        for event in ("first", "second", "third", "other")
    ]
    async with api.svc.tx(Principal(owner, "Owner", None)) as conn:
        sealed, version = await api.svc.crypto.encrypt(
            conn,
            owner,
            "calendar_connections:cache",
            str(connection_id),
            json.dumps({"calendars": [{"id": "primary", "name": "Work"}], "events": events}),
        )
        await conn.execute(
            "insert into app.calendar_connections(id,user_id,profile_id,provider,status,"
            "selected_calendars,cache_enc,cache_key_version) "
            "values($1,$2,$2,'google','active',array['primary'],$3,$4)",
            connection_id,
            owner,
            sealed,
            version,
        )
    path = "/v1/calendars/google/context"
    original = {
        "calendar_id": "primary",
        "instance_id": "first",
        "application_id": app_id,
        "session_id": str(sid),
        "unfinished_actions": ["Send the reviewed design"],
    }
    saved = await api.client.put(path, headers=headers, json=original)
    assert saved.status_code == 200, saved.text
    detail = (await api.client.get("/v1/meeting-preparation", headers=headers)).json()
    assert [item["application_id"] for item in detail["applications"]] == [app_id]
    assert [item["id"] for item in detail["saved_sessions"]] == [str(sid)]
    first = next(item for item in detail["items"] if item["meeting"]["instance_id"] == "first")
    assert first["selected_application_id"] == app_id
    assert first["linked_session_id"] == str(sid)
    assert any(item["notes"] == "Use the reviewed design." for item in first["previous"])
    assert first["unfinished_actions"] == ["Send the reviewed design"]
    # The latest explicit empty list closes actions, instead of merging old occurrences forever.
    completed = {**original, "instance_id": "second", "session_id": None, "unfinished_actions": []}
    assert (await api.client.put(path, headers=headers, json=completed)).status_code == 200
    separate = {**completed, "instance_id": "other", "unfinished_actions": ["Separate action"]}
    assert (await api.client.put(path, headers=headers, json=separate)).status_code == 200
    detail = (await api.client.get("/v1/meeting-preparation", headers=headers)).json()
    third = next(item for item in detail["items"] if item["meeting"]["instance_id"] == "third")
    assert third["unfinished_actions"] == []
    assert all(not item["unfinished_actions"] for item in third["previous"])
    assert any(item["notes"] == "Use the reviewed design." for item in third["previous"])
    for forbidden in (
        {**original, "application_id": foreign_app.json()["id"]},
        {**original, "session_id": str(private_sid)},
        {**original, "session_id": str(live_sid)},
    ):
        assert (await api.client.put(path, headers=headers, json=forbidden)).status_code == 404
    assert (await api.client.put(path, headers=other_headers, json=original)).status_code == 404
    assert (await api.client.delete(f"/v1/sessions/{sid}", headers=headers)).status_code == 204
    detail = (await api.client.get("/v1/meeting-preparation", headers=headers)).json()
    assert detail["saved_sessions"] == []
    assert all(item["linked_session_id"] is None for item in detail["items"])
    assert all(not prior["notes"] for item in detail["items"] for prior in item["previous"])
    assert not api.take("session.create")
