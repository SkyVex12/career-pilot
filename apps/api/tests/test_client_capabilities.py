"""Contract discovery is authenticated and does not probe feature dependencies."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest

from copilot_api.main import create_app
from copilot_core import __version__
from copilot_core.auth import AuthError, Principal


@pytest.fixture
def capability_api():
    principal = Principal(uuid4(), "Account", None, provider="email")
    conn = SimpleNamespace(fetchval=AsyncMock(return_value="active"), execute=AsyncMock())

    @asynccontextmanager
    async def tx(user):
        assert user is principal
        yield conn

    svc: Any = SimpleNamespace(
        settings=SimpleNamespace(min_client_version="0.0.0", require_career_os=True),
        verifier=SimpleNamespace(verify=AsyncMock(return_value=principal)),
        tx=tx,
    )
    app = create_app(svc)
    app.state.services = svc
    return app, svc, conn


async def test_capability_contract_is_authenticated_private_and_read_only(capability_api):
    app, svc, conn = capability_api
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing = await client.get("/v1/client-capabilities")
        assert missing.status_code == 401
        svc.verifier.verify.assert_not_awaited()
        conn.fetchval.assert_not_awaited()
        response = await client.get(
            "/v1/client-capabilities",
            headers={"Authorization": "Bearer valid", "x-copilot-profile": str(uuid4())},
        )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {
        "schema_version": 1,
        "server_version": __version__,
        "contracts": {"companion": 1, "ask": 1, "practice": 1, "preparation": 1, "languages": 1},
    }
    conn.fetchval.assert_awaited_once()
    conn.execute.assert_not_awaited()


async def test_invalid_token_does_not_expose_capabilities(capability_api):
    app, svc, conn = capability_api
    svc.verifier.verify.side_effect = AuthError("invalid token")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/client-capabilities", headers={"Authorization": "Bearer invalid"}
        )
    assert response.status_code == 401
    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


def test_advertised_contracts_have_implemented_routes(capability_api):
    app, _, _ = capability_api
    paths = app.openapi()["paths"]
    for path, method in [
        ("/v1/companion/sessions", "get"),
        ("/v1/companion/sessions/{session_id}/attach", "post"),
        ("/v1/ask/conversations", "post"),
        ("/v1/ask/sources/{kind}/{source_id}", "get"),
        ("/v1/practice/sessions", "post"),
        ("/v1/practice/sessions/{sid}/actions", "post"),
        ("/v1/meeting-preparation", "get"),
        ("/v1/call-languages", "get"),
    ]:
        assert method in paths[path]
