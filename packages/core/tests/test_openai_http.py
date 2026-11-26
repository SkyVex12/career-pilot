"""Socket-free package client factory/proxy/body/ownership checks."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import socket
import ssl
from pathlib import Path
from typing import Any

import httpcore2
import httpx2
import openai
import pytest

from copilot_core import _openai_http as candidate
from copilot_core._http2_cancel import CancellationSafePool


@pytest.fixture(autouse=True)
def network_and_environment(monkeypatch):
    for name in list(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Actual network forbidden in factory tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def direct_pool(client: Any) -> Any:
    return client._transport._pool


def pool_for(client, url):
    return client._transport_for_url(httpx2.URL(url))._pool


def pool_signature(pool):
    return (
        type(pool).__name__,
        pool._http1,
        pool._http2,
        pool._max_connections,
        pool._max_keepalive_connections,
        pool._keepalive_expiry,
    )


@pytest.mark.asyncio
async def test_feature_disabled_preserves_native_default_factory_and_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    baseline = openai.DefaultAsyncHttpxClient(http2=True)
    client = candidate.make_http_client(http2=True)
    try:
        assert type(client) is type(baseline)
        assert pool_signature(direct_pool(client)) == pool_signature(direct_pool(baseline))
        assert pool_signature(pool_for(client, "https://outside.invalid")) == pool_signature(
            pool_for(baseline, "https://outside.invalid")
        )
    finally:
        await baseline.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_environment_http_https_and_no_proxy_are_preserved(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://user:fixture-pass@proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:fixture-pass@proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "direct.invalid,.internal.invalid")
    client = candidate.make_http_client(http2=True, cancellation_fix=True)
    try:
        for url in ("https://direct.invalid", "https://a.internal.invalid"):
            assert isinstance(pool_for(client, url), CancellationSafePool)
        for url in ("http://outside.invalid", "https://outside.invalid"):
            pool = pool_for(client, url)
            assert type(pool) is httpcore2.AsyncHTTPProxy
            assert pool._http1 is True
            assert pool._http2 is False
            assert (
                b"Proxy-Authorization",
                b"Basic " + base64.b64encode(b"user:fixture-pass"),
            ) in pool._proxy_headers
        assert isinstance(client, candidate.ProxyPreservingHttpxClient)
        assert client.transport_policy == "fixed_direct_http2_native_proxy_http1"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_explicit_proxy_native_mapping_overrides_environment_no_proxy(
    monkeypatch,
):
    monkeypatch.setenv("NO_PROXY", "direct.invalid")
    client = candidate.make_http_client(
        http2=True, cancellation_fix=True, proxy="http://explicit.invalid:8080"
    )
    try:
        assert type(pool_for(client, "https://direct.invalid")) is httpcore2.AsyncHTTPProxy
        assert pool_for(client, "https://direct.invalid")._http2 is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_trust_env_false_disables_proxy_discovery(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    client = candidate.make_http_client(http2=True, cancellation_fix=True, trust_env=False)
    try:
        assert client._mounts == {}
        assert isinstance(pool_for(client, "https://outside.invalid"), CancellationSafePool)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http2_false_wins_without_private_version_dependency(monkeypatch):
    def should_not_validate():
        raise AssertionError("Version guard not needed for explicitly disabled H2")

    monkeypatch.setattr(candidate, "validate_client_hooks", should_not_validate)
    client = candidate.make_http_client(http2=False, cancellation_fix=True, trust_env=False)
    try:
        pool = direct_pool(client)
        assert type(pool) is httpcore2.AsyncConnectionPool
        assert pool._http2 is False
        assert isinstance(client, candidate.ProxyPreservingHttpxClient)
        assert client.transport_policy == "http2_disabled"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unsupported_version_is_explicit_failure_or_explicit_h1(monkeypatch):
    def unsupported():
        raise candidate.UnsupportedTransportVersion("authored version change")

    monkeypatch.setattr(candidate, "validate_client_hooks", unsupported)
    with pytest.raises(candidate.UnsupportedTransportVersion):
        candidate.make_http_client(http2=True, cancellation_fix=True, trust_env=False)
    client = candidate.make_http_client(
        http2=True,
        cancellation_fix=True,
        unsupported_policy="http1",
        http1=False,
        trust_env=False,
    )
    try:
        assert type(direct_pool(client)) is httpcore2.AsyncConnectionPool
        assert direct_pool(client)._http2 is False
        assert direct_pool(client)._http1 is True
        assert isinstance(client, candidate.ProxyPreservingHttpxClient)
        assert client.transport_policy == "unsupported_version_explicit_http1_fallback"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_tls_context_limits_timeout_and_redirect_defaults_are_preserved():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    limits = httpx2.Limits(max_connections=17, max_keepalive_connections=7, keepalive_expiry=120)
    baseline = openai.DefaultAsyncHttpxClient(
        verify=context, http2=True, limits=limits, trust_env=False
    )
    client = candidate.make_http_client(
        verify=context,
        http2=True,
        cancellation_fix=True,
        limits=limits,
        trust_env=False,
    )
    try:
        pool = direct_pool(client)
        original = direct_pool(baseline)
        assert pool._ssl_context is context
        assert pool_signature(pool)[1:] == pool_signature(original)[1:]
        assert pool._retries == 0
        assert pool._connections == []
        assert pool._requests == []
        assert client.timeout == baseline.timeout
        assert client.follow_redirects == baseline.follow_redirects
    finally:
        await baseline.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_https_proxy_ssl_and_headers_remain_native():
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    proxy_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    proxy = httpx2.Proxy(
        "https://u:p@proxy.invalid:8443",
        ssl_context=proxy_context,
        headers={"X-Authored-Proxy": "fixture"},
    )
    client = candidate.make_http_client(
        http2=True,
        cancellation_fix=True,
        verify=client_context,
        proxy=proxy,
        trust_env=False,
    )
    try:
        pool = pool_for(client, "https://outside.invalid")
        assert type(pool) is httpcore2.AsyncHTTPProxy
        assert pool._ssl_context is client_context
        assert pool._proxy_ssl_context is proxy_context
        assert (
            b"Proxy-Authorization",
            b"Basic " + base64.b64encode(b"u:p"),
        ) in pool._proxy_headers
        assert (b"X-Authored-Proxy", b"fixture") in pool._proxy_headers
        assert pool._http2 is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
async def test_socks_uses_native_factory_or_native_missing_extra_error(scheme):
    kwargs: dict[str, Any] = dict(
        http2=True,
        cancellation_fix=True,
        proxy=scheme + "://proxy.invalid:1080",
        trust_env=False,
    )
    if importlib.util.find_spec("socksio") is None:
        with pytest.raises(ImportError, match="socksio"):
            candidate.make_http_client(**kwargs)
    else:
        client = candidate.make_http_client(**kwargs)
        try:
            pool = pool_for(client, "https://outside.invalid")
            assert type(pool) is httpcore2.AsyncSOCKSProxy
            assert pool._http1 is True
            assert pool._http2 is False
        finally:
            await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("proxied", [False, True])
async def test_exact_sdk_request_body_unchanged_and_no_hidden_retries(monkeypatch, proxied):
    if proxied:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    bodies = []
    raw_bodies = []
    calls = 0

    async def authored_response(self, request):
        nonlocal calls
        calls += 1
        content = b"".join([chunk async for chunk in request.stream])
        bodies.append(json.loads(content))
        raw_bodies.append(content)
        return httpcore2.Response(
            200,
            headers=[(b"content-type", b"text/event-stream")],
            content=b'data: {"type":"response.output_text.delta","delta":"OK",'
            b'"item_id":"m","output_index":0,"content_index":0,'
            b'"sequence_number":1}\n\ndata: [DONE]\n\n',
        )

    monkeypatch.setattr(httpcore2.AsyncConnectionPool, "handle_async_request", authored_response)
    for fixed in (False, True):
        transport = candidate.make_http_client(
            http2=True, cancellation_fix=fixed, trust_env=proxied
        )
        sdk = openai.AsyncOpenAI(
            api_key="authored-dummy",
            base_url="https://mock.invalid/v1",
            max_retries=0,
            timeout=openai.Timeout(30, connect=2),
            http_client=transport,
        )
        try:
            stream = await sdk.responses.create(
                model="authored-model",
                input=[{"role": "user", "content": "Authored fixed context"}],
                max_output_tokens=1200,
                reasoning={"effort": "none"},
                store=False,
                stream=True,
            )
            assert [getattr(e, "delta", None) async for e in stream] == ["OK"]
            await stream.close()
            assert sdk.max_retries == 0
        finally:
            await sdk.close()
    assert calls == 2
    assert bodies[0] == bodies[1]
    assert raw_bodies[0] == raw_bodies[1]
    assert bodies[1]["store"] is False
    assert bodies[1]["max_output_tokens"] == 1200


@pytest.mark.asyncio
async def test_sdk_close_owns_direct_and_all_proxy_mounts_once(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    client = candidate.make_http_client(http2=True, cancellation_fix=True)
    transports = [
        client._transport,
        *[t for t in client._mounts.values() if t is not None],
    ]
    closed = {}
    for transport in transports:

        async def close(transport=transport):
            key = id(transport)
            closed[key] = closed.get(key, 0) + 1

        monkeypatch.setattr(transport, "aclose", close)
    sdk = openai.AsyncOpenAI(api_key="authored-dummy", max_retries=0, http_client=client)
    await sdk.close()
    assert closed == {id(t): 1 for t in transports}


@pytest.mark.parametrize("field", ["transport", "mounts"])
def test_custom_transport_bypass_is_rejected_when_enabled(field):
    mock = httpx2.MockTransport(lambda request: httpx2.Response(200))
    kwargs: dict[str, Any] = {field: mock if field == "transport" else {"https://": mock}}
    with pytest.raises(ValueError, match="would bypass"):
        candidate.make_http_client(http2=True, cancellation_fix=True, **kwargs)


def test_private_hook_source_drift_fails_before_network(monkeypatch):
    monkeypatch.setitem(candidate.SUPPORTED_CLIENT_SOURCES, "httpx2/_client.py", "0" * 64)
    with pytest.raises(candidate.UnsupportedTransportVersion, match="source differs"):
        candidate.make_http_client(http2=True, cancellation_fix=True)


def test_failed_pin_read_is_not_silently_downgraded(monkeypatch):
    def unavailable():
        raise OSError("authored unreadable dependency source")

    monkeypatch.setattr(candidate, "validate_client_hooks", unavailable)
    with pytest.raises(OSError, match="unreadable"):
        candidate.make_http_client(http2=True, cancellation_fix=True, unsupported_policy="http1")


@pytest.mark.asyncio
async def test_default_direct_tls_verifies_hostname_and_peer():
    client = candidate.make_http_client(http2=True, cancellation_fix=True, trust_env=False)
    try:
        context = direct_pool(client)._ssl_context
        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED
    finally:
        await client.aclose()


def test_pool_replacement_rejects_existing_request_owner(monkeypatch):
    original = httpx2.AsyncHTTPTransport.__init__

    def already_used(self, **kwargs):
        original(self, **kwargs)
        self._pool._requests.append(object())

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "__init__", already_used)
    with pytest.raises(RuntimeError, match="unused direct pool"):
        candidate.CancellationSafeTransport(http2=True, trust_env=False)


@pytest.mark.asyncio
async def test_feature_disabled_keeps_custom_transport_contract():
    mock = httpx2.MockTransport(lambda request: httpx2.Response(200, text="authored"))
    client = candidate.make_http_client(http2=True, transport=mock, trust_env=False)
    try:
        assert client._transport is mock
        assert (await client.get("https://mock.invalid")).text == "authored"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_private_import_failure_is_isolated_from_disabled_h1_and_explicit_fallback(
    monkeypatch,
):
    original = importlib.import_module

    def missing(name, *args, **kwargs):
        if name == "._http2_cancel":
            raise ImportError("authored unavailable private hook")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", missing)
    policies: tuple[dict[str, Any], ...] = (
        {"http2": True, "cancellation_fix": False},
        {"http2": False, "cancellation_fix": True},
        {"http2": True, "cancellation_fix": True, "unsupported_policy": "http1"},
    )
    for kwargs in policies:
        client = candidate.make_http_client(**kwargs, trust_env=False)
        await client.aclose()
    with pytest.raises(candidate.UnsupportedTransportVersion):
        candidate.make_http_client(http2=True, cancellation_fix=True, trust_env=False)


def test_adapter_imports_no_private_module_eagerly():
    import ast

    assert candidate.__file__ is not None
    tree = ast.parse(Path(candidate.__file__).read_text(encoding="utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom):
            assert statement.module not in {
                "_http2_cancel",
                "copilot_core._http2_cancel",
                "httpx2._config",
            }
            if statement.module == "copilot_core":
                assert all(alias.name != "_http2_cancel" for alias in statement.names)
        if isinstance(statement, ast.Import):
            assert all(alias.name != "copilot_core._http2_cancel" for alias in statement.names)
