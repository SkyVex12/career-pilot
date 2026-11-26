"""Private opt-in client factory; preserves SDK environment/NO_PROXY discovery.

No installed module is replaced. The fixed direct pool is isolated to this client;
HTTP/CONNECT/SOCKS proxy factories stay native and explicitly use HTTP/1.1.
"""

from __future__ import annotations

# This version-pinned adapter intentionally reuses the verified pool internals.
# pyright: reportPrivateUsage=false
import hashlib
import importlib.metadata
from pathlib import Path
from typing import Any, Literal

import httpcore2
import httpx2
import openai

DEFAULT_LIMITS = httpx2.Limits(max_connections=100, max_keepalive_connections=20)
# HTTPX supplies its actual SDK limits to both hooks. These public-value defaults
# match the pinned hook signature without importing private code on the H1 path.

SUPPORTED_CLIENT_SOURCES = {
    "httpx2/_client.py": "fa3948aa66a9efd5edf097c6d13d927612c617328b43ceefc4bdd5e350aaa93b",
    "httpx2/_transports/default.py": (
        "8fc43a2882d471aff751366a7f28e91c5e31831a4b29aa3bb4e66931a2148e97"
    ),
    "httpx2/_config.py": "0b53f754f1bb708f2ea485bccbb4ab1ffa07259cf8c008a42d8959b9cb1e3db0",
    "openai/_base_client.py": "7a0a173edf3fadb6b310895d5db6fa0b14264bdaa25b33dd4eb29d860a82639b",
}


class UnsupportedTransportVersion(RuntimeError):
    pass


def scoped_module() -> Any:
    try:
        return importlib.import_module("._http2_cancel", package=__package__)
    except (ImportError, AttributeError) as exc:
        raise UnsupportedTransportVersion("Scoped HTTP/2 imports are unsupported") from exc


def validate_client_hooks() -> None:
    versions = {"httpx2": "2.13.1", "openai": "3.26.0", "httpcore2": "2.13.1", "h2": "4.4.1"}
    try:
        if any(importlib.metadata.version(name) != value for name, value in versions.items()):
            raise UnsupportedTransportVersion(
                "Scoped client hooks have not been reviewed for this version"
            )
    except importlib.metadata.PackageNotFoundError as exc:
        raise UnsupportedTransportVersion("Scoped HTTP/2 dependency is unavailable") from exc
    site = Path(httpx2.__file__).parent.parent
    for relative, expected in SUPPORTED_CLIENT_SOURCES.items():
        if hashlib.sha256((site / relative).read_bytes()).hexdigest() != expected:
            raise UnsupportedTransportVersion("Scoped client hook source differs from review")
    try:
        scoped_module().validate_dependencies()
    except RuntimeError as exc:
        raise UnsupportedTransportVersion("Scoped HTTP/2 dependency is unsupported") from exc


class CancellationSafeTransport(httpx2.AsyncHTTPTransport):
    def __init__(self, **kwargs: Any) -> None:
        if kwargs.get("proxy") is not None:
            raise ValueError("Scoped transport only accepts direct connections")
        super().__init__(**kwargs)
        original = self._pool
        if (
            type(original) is not httpcore2.AsyncConnectionPool
            or original.connections
            or original._requests
        ):
            raise RuntimeError("Expected a newly constructed unused direct pool")
        # Reuse HTTPX's exact SSL/proxy/certificate configuration parsing. The
        # original pool has no socket or request owner before its first request.
        self._pool = scoped_module().CancellationSafePool(
            ssl_context=original._ssl_context,
            max_connections=original._max_connections,
            max_keepalive_connections=original._max_keepalive_connections,
            keepalive_expiry=original._keepalive_expiry,
            http1=original._http1,
            http2=original._http2,
            retries=original._retries,
            local_address=original._local_address,
            uds=original._uds,
            network_backend=original._network_backend,
            socket_options=original._socket_options,
        )


class ProxyPreservingHttpxClient(openai.DefaultAsyncHttpxClient):
    def __init__(
        self,
        *,
        http2: bool = False,
        unsupported_policy: Literal["fail", "http1"] = "fail",
        **kwargs: Any,
    ) -> None:
        if unsupported_policy not in {"fail", "http1"}:
            raise ValueError("Unsupported transport version policy")
        if kwargs.get("transport") is not None or kwargs.get("mounts"):
            raise ValueError("Custom transport/mounts would bypass this scoped transport policy")
        self.transport_policy = "http2_disabled"
        self._fixed_http2 = http2
        if http2:
            try:
                validate_client_hooks()
            except UnsupportedTransportVersion:
                if unsupported_policy == "fail":
                    raise
                http2 = False
                kwargs["http1"] = True
                self._fixed_http2 = False
                self.transport_policy = "unsupported_version_explicit_http1_fallback"
            else:
                self.transport_policy = "fixed_direct_http2_native_proxy_http1"
        # Deliberately do not pass transport=. HTTPX's allow_env_proxies remains
        # trust_env and transport is None, so normal env/NO_PROXY maps survive.
        super().__init__(http2=http2, **kwargs)

    def _init_transport(
        self,
        verify: Any = True,
        cert: Any = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx2.Limits = DEFAULT_LIMITS,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncBaseTransport:
        if not self._fixed_http2:
            return super()._init_transport(
                verify=verify,
                cert=cert,
                trust_env=trust_env,
                http1=http1,
                http2=http2,
                limits=limits,
                transport=transport,
            )
        return CancellationSafeTransport(
            verify=verify,
            cert=cert,
            trust_env=trust_env,
            http1=http1,
            http2=http2,
            limits=limits,
        )

    def _init_proxy_transport(
        self,
        proxy: httpx2.Proxy,
        verify: Any = True,
        cert: Any = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx2.Limits = DEFAULT_LIMITS,
    ) -> httpx2.AsyncBaseTransport:
        return super()._init_proxy_transport(
            proxy=proxy,
            verify=verify,
            cert=cert,
            trust_env=trust_env,
            http1=True,
            http2=False,
            limits=limits,
        )


def make_http_client(
    *,
    http2: bool,
    cancellation_fix: bool = False,
    unsupported_policy: Literal["fail", "http1"] = "fail",
    **kwargs: Any,
) -> httpx2.AsyncClient:
    if not cancellation_fix:
        return openai.DefaultAsyncHttpxClient(http2=http2, **kwargs)
    return ProxyPreservingHttpxClient(http2=http2, unsupported_policy=unsupported_policy, **kwargs)
