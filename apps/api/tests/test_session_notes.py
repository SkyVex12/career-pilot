"""Session notes HTTP validation and encrypted write boundary without the database stack.

The stack tests in test_api and test_profiles separately exercise real RLS and round trips.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from fastapi import FastAPI

from copilot_api.deps import current_account, current_user, services
from copilot_api.routes.sessions import router
from copilot_core.auth import Principal


@pytest.fixture
def notes_api():
    profile = Principal(uuid.uuid4(), "profile", None)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"ended_at": datetime.now(UTC)})
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    svc = MagicMock()
    svc.valkey.hgetall = AsyncMock(return_value={})
    svc.crypto.encrypt = AsyncMock(return_value=(b"encrypted-notes", 3))

    @asynccontextmanager
    async def transaction(user):
        assert user == profile
        yield conn

    svc.tx = transaction
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[services] = lambda: svc
    app.dependency_overrides[current_user] = lambda: profile
    app.dependency_overrides[current_account] = lambda: profile
    return app, svc, conn, profile


async def test_notes_write_encrypts_for_the_active_profile_and_clear_removes_ciphertext(notes_api):
    app, svc, conn, profile = notes_api
    sid = uuid.uuid4()
    path = f"/sessions/{sid}/notes"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        response = await client.put(path, json={"text": "First line\nSecond line"})
        assert response.status_code == 200
        assert response.json() == {"notes": "First line\nSecond line"}
        svc.crypto.encrypt.assert_awaited_once_with(
            conn, profile.user_id, "sessions", str(sid), "First line\nSecond line"
        )
        assert conn.execute.await_args.args[1:] == (sid, b"encrypted-notes", 3)
        response = await client.put(path, json={"text": ""})
        assert response.status_code == 200
        assert conn.execute.await_args.args[1:] == (sid, None, None)
        assert svc.crypto.encrypt.await_count == 1


@pytest.mark.parametrize(
    "body", [{"text": "x" * 20_001}, {"text": None}, {"text": "x", "owner": "other"}]
)
async def test_notes_reject_invalid_bodies_before_writing(notes_api, body):
    app, svc, conn, _ = notes_api
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        response = await client.put(f"/sessions/{uuid.uuid4()}/notes", json=body)
    assert response.status_code == 422
    conn.execute.assert_not_awaited()
    svc.crypto.encrypt.assert_not_awaited()


@pytest.mark.parametrize(("row", "status"), [(None, 404), ({"ended_at": None}, 409)])
async def test_notes_refuse_an_inaccessible_or_live_session(notes_api, row, status):
    app, svc, conn, _ = notes_api
    conn.fetchrow.return_value = row
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        response = await client.put(f"/sessions/{uuid.uuid4()}/notes", json={"text": "Private"})
    assert response.status_code == status
    conn.execute.assert_not_awaited()
    svc.crypto.encrypt.assert_not_awaited()
