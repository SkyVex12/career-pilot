"""Socket-free production wiring; run only after candidate integration is authorized."""

import builtins
import json
import os
import socket
from typing import Any

import httpcore2
import httpx2
import openai
import pytest

from copilot_core.config import CoreSettings
from copilot_core.providers import openai_client


def settings(**values: Any) -> CoreSettings:
    options: Any = {"_env_file": None, "openai_api_key": "authored-dummy", **values}
    return CoreSettings(**options)


@pytest.fixture(autouse=True)
def no_network_or_host_proxy(monkeypatch):
    for name in list(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("COPILOT_OPENAI_HTTP2_CANCEL_FIX", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network is forbidden in these authored wiring tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize(("enabled", "http2"), [(False, True), (False, False), (True, False)])
async def test_native_and_explicit_h1_do_not_import_private_transport(monkeypatch, enabled, http2):
    assert settings().openai_http2_cancel_fix is False
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.endswith(("_openai_http", "_http2_cancel")):
            raise AssertionError("Disabled or H1 path imported private transport hooks")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    sdk = openai_client(settings(openai_http2=http2, openai_http2_cancel_fix=enabled))
    try:
        client: Any = sdk._client
        assert type(client) is openai.DefaultAsyncHttpxClient
        transport: Any = client._transport
        assert transport._pool._http2 is http2
        assert sdk.max_retries == 0
    finally:
        await sdk.close()


async def test_opt_in_keeps_proxy_auth_no_proxy_limits_and_timeouts(monkeypatch):
    from copilot_core._http2_cancel import CancellationSafePool
    from copilot_core._openai_http import ProxyPreservingHttpxClient

    monkeypatch.setenv("HTTPS_PROXY", "http://u:p@proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "direct.invalid")
    sdk = openai_client(
        settings(
            openai_http2=True,
            openai_http2_cancel_fix=True,
            llm_keepalive_s=91,
            llm_connect_timeout_s=3,
            openai_base_url="https://direct.invalid/v1",
        ),
        timeout_s=44,
    )
    try:
        client: Any = sdk._client
        direct = client._transport_for_url(httpx2.URL("https://direct.invalid"))._pool
        proxy = client._transport_for_url(httpx2.URL("https://proxied.invalid"))._pool
        assert isinstance(client, ProxyPreservingHttpxClient)
        assert isinstance(direct, CancellationSafePool)
        assert type(proxy) is httpcore2.AsyncHTTPProxy
        assert proxy._http1 is True
        assert proxy._http2 is False
        assert (b"Proxy-Authorization", b"Basic dTpw") in proxy._proxy_headers
        assert direct._max_connections == 1000
        assert direct._max_keepalive_connections == 100
        assert direct._keepalive_expiry == proxy._keepalive_expiry == 91
        timeout: Any = sdk.timeout
        assert timeout.connect == 3
        assert timeout.read == 44
        assert sdk.max_retries == 0
        assert str(sdk.base_url) == "https://direct.invalid/v1/"
        assert client.transport_policy == "fixed_direct_http2_native_proxy_http1"
    finally:
        await sdk.close()


@pytest.mark.parametrize("kind", ["unsupported", "unreadable"])
def test_enabled_unsupported_installation_fails_before_sdk_construction(monkeypatch, kind):
    from copilot_core import _openai_http

    calls = []

    def guard():
        if kind == "unreadable":
            raise OSError("authored unavailable source")
        raise _openai_http.UnsupportedTransportVersion("authored version mismatch")

    def constructed(*args, **kwargs):
        calls.append(True)
        raise AssertionError("SDK must not be constructed after the transport guard fails")

    monkeypatch.setattr(_openai_http, "validate_client_hooks", guard)
    monkeypatch.setattr(openai, "AsyncOpenAI", constructed)
    error = OSError if kind == "unreadable" else _openai_http.UnsupportedTransportVersion
    with pytest.raises(error):
        openai_client(settings(openai_http2=True, openai_http2_cancel_fix=True))
    assert calls == []


@pytest.mark.parametrize("proxied", [False, True])
async def test_factory_request_bytes_are_unchanged_and_single_attempt(monkeypatch, proxied):
    if proxied:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    bodies = []

    async def authored_response(self, request):
        bodies.append(b"".join([chunk async for chunk in request.stream]))
        return httpcore2.Response(
            200,
            headers=[(b"content-type", b"text/event-stream")],
            content=b'data: {"type":"response.output_text.delta","delta":"OK",'
            b'"item_id":"m","output_index":0,"content_index":0,'
            b'"sequence_number":1}\n\ndata: [DONE]\n\n',
        )

    monkeypatch.setattr(httpcore2.AsyncConnectionPool, "handle_async_request", authored_response)
    for enabled in (False, True):
        sdk = openai_client(
            settings(
                openai_http2=True,
                openai_http2_cancel_fix=enabled,
                openai_base_url="https://mock.invalid/v1",
            )
        )
        try:
            stream = await sdk.responses.create(
                model="authored-model",
                input=[{"role": "user", "content": "Authored context"}],
                max_output_tokens=1200,
                reasoning={"effort": "none"},
                store=False,
                stream=True,
            )
            assert [getattr(event, "delta", None) async for event in stream] == ["OK"]
            await stream.close()
        finally:
            await sdk.close()
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[1])["store"] is False


async def test_sdk_close_keeps_ownership_of_direct_and_proxy_mounts(monkeypatch):
    # This verifies factory ownership only. Application request-owner drain is a
    # separate explicit prerequisite; this test must not claim it is implemented.
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    sdk = openai_client(settings(openai_http2=True, openai_http2_cancel_fix=True))
    client: Any = sdk._client
    transports = [client._transport, *[t for t in client._mounts.values() if t is not None]]
    closed = {}
    for transport in transports:

        async def close(transport=transport):
            key = id(transport)
            closed[key] = closed.get(key, 0) + 1

        monkeypatch.setattr(transport, "aclose", close)
    await sdk.close()
    assert closed == {id(t): 1 for t in transports}
