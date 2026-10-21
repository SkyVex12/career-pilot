"""Google Cloud access tokens for the services on the Stage 0 VM (ARCHITECTURE.md, "Stage 0 on
GCP").

The VM's service account is the services' only Google identity: its tokens come from the
metadata server, which the host's firewall opens only to the API's and the workers' uids
(infra/gcp/modules/host). Nothing here holds a key.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import httpx2

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
# A token this close to its expiry is fetched again.
EXPIRY_MARGIN_S = 120.0


class TokenSource(Protocol):
    async def token(self) -> str: ...


type TokenFetcher = Callable[[], Awaitable[dict[str, Any]]]


async def fetch_metadata_token() -> dict[str, Any]:
    async with httpx2.AsyncClient(timeout=5.0) as client:
        response = await client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


class MetadataTokenSource:
    """The VM service account's access token, cached until shortly before it expires."""

    def __init__(
        self,
        fetch: TokenFetcher = fetch_metadata_token,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fetch = fetch
        self.clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._token is None or self.clock() >= self._expires_at - EXPIRY_MARGIN_S:
                data = await self.fetch()
                self._token = str(data["access_token"])
                self._expires_at = self.clock() + float(data.get("expires_in", 300))
            return self._token
