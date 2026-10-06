"""Valkey client for the API and the workers (the gateway has its own, with tighter timeouts).

valkey-py types its commands as "value or awaitable" for the sync and async clients alike, so
the client is used untyped and results are cast where they are read.
"""

from typing import Any

import valkey.asyncio as valkey_async
from valkey.asyncio.cluster import ValkeyCluster

type Valkey = Any


def make_valkey(url: str, cluster: bool = False) -> Valkey:
    opts: dict[str, Any] = {"socket_timeout": 5.0, "socket_connect_timeout": 2.0}
    if cluster:
        return ValkeyCluster.from_url(url, **opts)
    return valkey_async.Valkey.from_url(url, **opts)


def text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
