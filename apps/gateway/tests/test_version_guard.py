"""The version guard (README.md, "Releasing the desktop app"): an app older than
min_client_version gets the `client_outdated` error and close 4426, before any session
exists; 0.1.0, which reconnects on codes it does not know, gets 4410 instead."""

from typing import Any

import pytest

from copilot_core import versions
from copilot_gateway.testing import GatewayServer, ProtocolClient


def start_message(client_version: str) -> dict[str, Any]:
    return {
        "type": "session.start",
        "mode": "interview",
        "platform": "zoom",
        "client_version": client_version,
        "consent": {"store_transcript": False},
        "audio": {"sample_rate": 16000, "encoding": "pcm16"},
    }


async def refused(gw: GatewayServer, client_version: str) -> tuple[int, dict[str, Any]]:
    c = ProtocolClient(gw.url)
    await c.connect()
    await c.send(start_message(client_version))
    code = await c.closed()
    return code, c.messages[0]


async def test_an_app_older_than_the_minimum_is_refused(env) -> None:
    async with GatewayServer(env.services(env.settings(min_client_version="0.3.0"))) as gw:
        code, error = await refused(gw, "desktop-0.2.9")
        assert code == 4426
        assert error["type"] == "error"
        assert error["code"] == "client_outdated"
        assert error["fatal"] is True
        assert "0.2.9" in error["message"]
        assert "0.3.0" in error["message"]
        # The first release reconnects on an unknown code: it is stopped with 4410.
        code, error = await refused(gw, "desktop-0.1.0")
        assert (code, error["code"]) == (4410, "client_outdated")
        assert not gw.manager.sessions
        # The minimum itself, newer ones, dev builds and other clients get a session.
        for ok in ("desktop-0.3.0", "desktop-0.10.0", "desktop-0.0.0", "test"):
            c = ProtocolClient(gw.url)
            await c.connect()
            await c.send(start_message(ok))
            await c.wait_type("session.ready")
            await c.send({"type": "session.end"})
            assert await c.closed() == 1000, ok


async def test_the_default_minimum_keeps_the_first_release_working(env) -> None:
    assert env.settings().min_client_version == "0.1.0"
    async with GatewayServer(env.services()) as gw:
        c = ProtocolClient(gw.url)
        await c.connect()
        await c.send(start_message("desktop-0.1.0"))
        await c.wait_type("session.ready")


def test_version_rules() -> None:
    assert versions.is_outdated("desktop-0.1.0", "0.1.1")
    assert versions.is_outdated("desktop-0.2.0-rc.1", "0.2.0")
    assert not versions.is_outdated("desktop-0.2.0", "0.2.0")
    assert not versions.is_outdated("desktop-0.0.0", "9.0.0")  # a dev build
    assert not versions.is_outdated("copilot-replay", "9.0.0")
    assert not versions.is_outdated(None, "9.0.0")
    assert versions.knows_4426("desktop-0.1.1")
    assert not versions.knows_4426("desktop-0.1.0")
    body = versions.outdated("desktop-0.1.0", "0.2.0")
    assert body["min_version"] == "0.2.0"
    assert body["client_version"] == "0.1.0"


def test_the_minimum_must_be_a_version(env) -> None:
    with pytest.raises(ValueError, match="min_client_version"):
        env.settings(min_client_version="latest")
