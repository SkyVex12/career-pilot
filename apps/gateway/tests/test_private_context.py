"""Private source refresh and sticky sharing policy on the live answer path."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx2

from copilot_core.ids import uuid7
from copilot_gateway.auth import Principal
from copilot_gateway.session import Session, SessionInfo, SessionManager


async def test_private_context_is_revalidated_and_never_kept_after_source_removal(env, monkeypatch):
    svc = env.services(env.settings(personal_ai_api_url="https://api.example.com"))
    info = SessionInfo("interview", None, None, "test", "test", True, datetime.now(UTC))
    session = Session(
        svc, SessionManager(svc), uuid7(), Principal(env.user_a, "Alex", None), info, None, None
    )
    session.account_id = env.user_a
    session.account_token = "token-a"
    request = httpx2.Request("GET", "https://api.example.com/v1/personal-ai/live-context")
    http = MagicMock()
    http.get = AsyncMock(
        side_effect=[
            httpx2.Response(
                200,
                request=request,
                json={"briefs": [{"id": "v1", "content": "Private sourced progress"}]},
            ),
            httpx2.Response(200, request=request, json={"briefs": []}),
            httpx2.ConnectError("unavailable"),
        ]
    )
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx2, "AsyncClient", MagicMock(return_value=http))
    first = await session.private_memory()
    assert "Private sourced progress" in first[0]
    assert session.private_context_account_id == str(env.user_a)
    assert session.seatdesk.fold.sharing is False
    assert session.state_fields()["private_context_account_id"] == str(env.user_a)
    assert (await session.private_memory()) == []  # deleted or revoked, not a cached copy
    assert (await session.private_memory()) == []  # API failure is not stale evidence
    assert session.private_context_account_id == str(env.user_a)  # History stays private
    assert http.get.await_count == 3
    assert http.get.await_args.kwargs["headers"]["x-copilot-profile"] == str(env.user_a)
    assert "account_token" not in session.state_fields()
