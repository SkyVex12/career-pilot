"""The version guard (README.md, "Releasing the desktop app"): an app older than
min_client_version gets HTTP 426 with the `ClientOutdated` body on every /v1 request. No
stack needed: the guard answers before any route or database."""

from types import SimpleNamespace

import httpx2

from copilot_api.main import create_app
from copilot_api.testing import api_settings
from copilot_protocol.models import ClientOutdated


def client(minimum: str | None = None) -> httpx2.AsyncClient:
    settings = api_settings(auth="dev", **({"min_client_version": minimum} if minimum else {}))
    app = create_app(SimpleNamespace(settings=settings))  # pyright: ignore[reportArgumentType]
    app.state.services = SimpleNamespace(settings=settings)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://api.test")


async def test_an_app_older_than_the_minimum_gets_426() -> None:
    async with client("0.2.0") as c:
        r = await c.get("/v1/auth/config", headers={"x-copilot-client": "desktop-0.1.4"})
        assert r.status_code == 426
        body = ClientOutdated.model_validate(r.json())
        assert body.code == "client_outdated"
        assert (body.min_version, body.client_version) == ("0.2.0", "0.1.4")
        # Any /v1 path, before auth: the app learns it must update even when signed out.
        r = await c.get("/v1/me", headers={"x-copilot-client": "desktop-0.1.4"})
        assert r.status_code == 426
        # The minimum, newer apps, dev builds and requests without the header pass.
        for headers in (
            {"x-copilot-client": "desktop-0.2.0"},
            {"x-copilot-client": "desktop-1.0.0"},
            {"x-copilot-client": "desktop-0.0.0"},
            {},
        ):
            assert (await c.get("/v1/auth/config", headers=headers)).status_code == 200
        # The health check is not behind the guard.
        r = await c.get("/healthz", headers={"x-copilot-client": "desktop-0.1.4"})
        assert r.status_code == 200


async def test_the_default_minimum_keeps_the_first_release_working() -> None:
    async with client() as c:
        r = await c.get("/v1/auth/config", headers={"x-copilot-client": "desktop-0.1.0"})
        assert r.status_code == 200
