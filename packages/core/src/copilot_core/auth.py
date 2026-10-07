"""Access token verification for the gateway and the API (ARCHITECTURE.md, "Connection and
auth" and "REST API").

Two verifiers behind one interface:
- `CognitoVerifier`: a Cognito access token (RS256 JWT), checked against the user pool's JWKS,
  which is cached and refetched at most once a minute when an unknown key id shows up. It
  checks the signature, issuer, expiry, `token_use == "access"` and the app client id
  (Cognito access tokens carry it as `client_id`, not `aud`).
- `DevTokenVerifier`: a static dev token for one dev user. It refuses to start outside
  COPILOT_ENV=dev, and refuses inside an ECS or Lambda task even then.
"""

import asyncio
import hmac
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

import httpx2
import jwt

from copilot_core.config import CoreSettings


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    display_name: str
    expires_at: float | None  # Unix seconds; None for tokens that do not expire
    email: str | None = None  # present when the token carries it; see API first login


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Principal:
        """Return the token's user, or raise AuthError."""
        ...


# Set inside AWS compute; the dev verifier never runs where any of these exist.
_AWS_RUNTIME_MARKERS = (
    "AWS_EXECUTION_ENV",
    "ECS_CONTAINER_METADATA_URI",
    "ECS_CONTAINER_METADATA_URI_V4",
    "AWS_LAMBDA_FUNCTION_NAME",
)


def refuse_outside_dev(settings: CoreSettings, what: str, environ: Mapping[str, str]) -> None:
    if settings.env != "dev":
        raise RuntimeError(f"{what} needs COPILOT_ENV=dev; this is {settings.env!r}")
    markers = [m for m in _AWS_RUNTIME_MARKERS if environ.get(m)]
    if markers:
        raise RuntimeError(f"{what} cannot run on AWS ({', '.join(markers)})")


class DevTokenVerifier:
    def __init__(self, settings: CoreSettings, environ: Mapping[str, str] | None = None) -> None:
        refuse_outside_dev(
            settings, "the dev token verifier", os.environ if environ is None else environ
        )
        if settings.dev_token is None or len(settings.dev_token.get_secret_value()) < 16:
            raise RuntimeError("COPILOT_DEV_TOKEN must be set to at least 16 characters")
        self._token = settings.dev_token.get_secret_value().encode()
        self._principal = Principal(
            user_id=UUID(settings.dev_user_id),
            display_name=settings.dev_user_name,
            expires_at=None,
            email=settings.dev_user_email,
        )

    async def verify(self, token: str) -> Principal:
        if not hmac.compare_digest(token.encode(), self._token):
            raise AuthError("invalid token")
        return self._principal


class StaticVerifier:
    """Test verifier: a fixed map from token to principal, with optional expiry."""

    def __init__(self, principals: dict[str, Principal]) -> None:
        self.principals = principals

    async def verify(self, token: str) -> Principal:
        principal = self.principals.get(token)
        if principal is None:
            raise AuthError("invalid token")
        if principal.expires_at is not None and principal.expires_at <= time.time():
            raise AuthError("token expired")
        return principal


type JwksFetcher = Callable[[str], Awaitable[Mapping[str, Any]]]


async def fetch_json(url: str) -> Mapping[str, Any]:
    async with httpx2.AsyncClient(timeout=5.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return cast(Mapping[str, Any], response.json())


class JwksCache:
    """Signing keys by key id. Refreshed after `ttl_s`, or on an unknown key id at most once
    per `min_refresh_s`, so a flood of tokens with made-up key ids cannot hammer Cognito."""

    def __init__(
        self,
        url: str,
        ttl_s: float = 3600,
        min_refresh_s: float = 60,
        fetch: JwksFetcher = fetch_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.ttl_s = ttl_s
        self.min_refresh_s = min_refresh_s
        self.fetch = fetch
        self.clock = clock
        self.keys: dict[str, Any] = {}
        self.fetched_at: float | None = None
        self.fetches = 0
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        data = await self.fetch(self.url)
        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []):
            if jwk.get("kid") and jwk.get("kty") == "RSA":
                keys[jwk["kid"]] = jwt.PyJWK.from_dict(dict(jwk), algorithm="RS256").key
        self.keys = keys
        self.fetched_at = self.clock()
        self.fetches += 1

    async def key(self, kid: str) -> Any:
        async with self._lock:
            now = self.clock()
            stale = self.fetched_at is None or now - self.fetched_at > self.ttl_s
            unknown = kid not in self.keys and (
                self.fetched_at is None or now - self.fetched_at > self.min_refresh_s
            )
            if stale or unknown:
                await self._refresh()
        if kid not in self.keys:
            raise AuthError("unknown signing key")
        return self.keys[kid]


class CognitoVerifier:
    def __init__(
        self,
        issuer: str,
        client_ids: list[str],
        jwks: JwksCache | None = None,
        leeway_s: float = 30,
    ) -> None:
        if not client_ids:
            raise RuntimeError("COPILOT_COGNITO_CLIENT_IDS must list the app client ids")
        self.issuer = issuer
        self.client_ids = set(client_ids)
        self.jwks = jwks or JwksCache(f"{issuer}/.well-known/jwks.json")
        self.leeway_s = leeway_s

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        if header.get("alg") != "RS256":
            raise AuthError("unexpected token algorithm")
        key = await self.jwks.key(str(header.get("kid", "")))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "sub", "iss", "token_use"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e
        if claims.get("token_use") != "access":
            raise AuthError("not an access token")
        # Access tokens name the app client in client_id; accept aud too, for ID-token-shaped
        # tokens from a federated setup, but never neither.
        audience = claims.get("client_id") or claims.get("aud")
        if audience not in self.client_ids:
            raise AuthError("token is for another app client")
        try:
            user_id = UUID(str(claims["sub"]))
        except ValueError as e:
            raise AuthError("sub is not a user id") from e
        name = claims.get("name") or claims.get("username") or claims.get("email") or str(user_id)
        return Principal(
            user_id=user_id,
            display_name=str(name),
            expires_at=float(claims["exp"]),
            email=claims.get("email"),
        )


def make_verifier(settings: CoreSettings) -> TokenVerifier:
    if settings.auth == "dev":
        return DevTokenVerifier(settings)
    issuer = settings.cognito_issuer
    if issuer is None:
        raise RuntimeError(
            "COPILOT_AUTH=cognito needs COPILOT_COGNITO_USER_POOL_ID and "
            "COPILOT_COGNITO_CLIENT_IDS; for local development set COPILOT_ENV=dev and "
            "COPILOT_AUTH=dev"
        )
    return CognitoVerifier(
        issuer,
        settings.cognito_client_ids,
        JwksCache(f"{issuer}/.well-known/jwks.json", ttl_s=settings.jwks_ttl_s),
    )


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
