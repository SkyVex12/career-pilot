"""Non-stack tests for one-use consent and local disconnect/rate-limit semantics."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from copilot_api.deps import Services
from copilot_api.routes import calendar
from copilot_core import calendars, profiles
from copilot_core.auth import Principal
from copilot_core.calendars import CalendarClient, ProviderConfig, state_key


class OneUseStore:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = json.dumps(value)

    async def getdel(self, key: str):
        current, self.value = self.value, None
        return current


def pending():
    return {
        "provider": "google",
        "account_id": str(uuid4()),
        "profile_id": str(uuid4()),
        "connection_id": str(uuid4()),
        "verifier": "private-verifier",
        "nonce": "nonce",
    }


async def test_wrong_provider_consumes_one_use_state() -> None:
    svc = cast(Services, SimpleNamespace(valkey=OneUseStore(pending())))
    with pytest.raises(HTTPException, match="provider") as failure:
        await calendar.callback("outlook", svc, state="state" * 8, code="code", error=None)
    assert failure.value.status_code == 400
    with pytest.raises(HTTPException, match="already used"):
        await calendar.callback("google", svc, state="state" * 8, code="code", error=None)


async def test_disconnected_callback_cannot_exchange_code(monkeypatch) -> None:
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=None))

    @asynccontextmanager
    async def tx(*args):
        yield conn

    svc = cast(Services, SimpleNamespace(valkey=OneUseStore(pending()), pool=object()))
    monkeypatch.setattr(calendar, "user_tx", tx)
    monkeypatch.setattr(profiles, "resolve", AsyncMock())
    provider = AsyncMock()
    monkeypatch.setattr(calendar, "provider_http", provider)
    with pytest.raises(HTTPException, match="disconnected"):
        await calendar.callback("google", svc, state="state" * 8, code="code", error=None)
    provider.assert_not_called()


async def test_removed_profile_cannot_finish_authorization(monkeypatch) -> None:
    svc = cast(Services, SimpleNamespace(valkey=OneUseStore(pending()), pool=object()))
    monkeypatch.setattr(profiles, "resolve", AsyncMock(side_effect=profiles.ProfileError("gone")))
    with pytest.raises(HTTPException, match="no longer accessible") as failure:
        await calendar.callback("google", svc, state="state" * 8, code="code", error=None)
    assert failure.value.status_code == 403


async def test_disconnect_commits_deletion_before_failed_remote_revocation(monkeypatch) -> None:
    row = {"id": uuid4(), "token_enc": b"sealed"}
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=row), execute=AsyncMock())
    committed = []

    @asynccontextmanager
    async def tx(*args):
        yield conn
        committed.append(True)

    svc = cast(Services, SimpleNamespace(tx=tx))
    owner = Principal(uuid4(), "owner", None)
    monkeypatch.setattr(calendar, "token_of", AsyncMock(return_value={"refresh_token": "refresh"}))
    monkeypatch.setattr(
        calendars,
        "configured",
        lambda provider: ProviderConfig(provider, "client", "secret", "https://example/callback"),
    )

    async def revoke(self, token):
        assert committed
        assert conn.execute.await_count == 1
        return False

    monkeypatch.setattr(CalendarClient, "revoke", revoke)
    result = await calendar.disconnect("google", svc, owner, owner)
    assert result["disconnected"]
    assert not result["provider_revoked"]
    assert result["provider_consent_removal_required"]


async def test_sync_rate_limit_never_calls_provider(monkeypatch) -> None:
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"id": uuid4()}))

    @asynccontextmanager
    async def tx(*args):
        yield conn

    svc = cast(
        Services, SimpleNamespace(tx=tx, valkey=SimpleNamespace(set=AsyncMock(return_value=None)))
    )
    owner = Principal(uuid4(), "owner", None)
    monkeypatch.setattr(
        calendar,
        "config_of",
        lambda provider: ProviderConfig(provider, "client", "secret", "https://example/callback"),
    )
    provider = AsyncMock()
    monkeypatch.setattr(calendar, "provider_http", provider)
    with pytest.raises(HTTPException) as failure:
        await calendar.sync("google", svc, owner, owner)
    assert failure.value.status_code == 429
    provider.assert_not_called()


def test_oauth_cache_key_does_not_contain_state() -> None:
    assert "secret-state" not in state_key("secret-state")
    assert state_key("secret-state") != state_key("other-state")
