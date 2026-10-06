"""Access token verification (ARCHITECTURE.md, "Connection and auth").

Cognito does not exist yet, so the only implementation is a static dev token. It refuses to
start outside COPILOT_ENV=dev, and refuses inside an ECS or Lambda task even then.
"""

import hmac
import os
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from copilot_gateway.config import Settings


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    display_name: str
    expires_at: float | None  # Unix seconds; None for tokens that do not expire


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


class DevTokenVerifier:
    def __init__(self, settings: Settings, environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        if settings.env != "dev":
            raise RuntimeError(
                f"the dev token verifier needs COPILOT_ENV=dev; this is {settings.env!r}"
            )
        markers = [m for m in _AWS_RUNTIME_MARKERS if env.get(m)]
        if markers:
            raise RuntimeError(f"the dev token verifier cannot run on AWS ({', '.join(markers)})")
        if settings.dev_token is None or len(settings.dev_token.get_secret_value()) < 16:
            raise RuntimeError("COPILOT_DEV_TOKEN must be set to at least 16 characters")
        self._token = settings.dev_token.get_secret_value().encode()
        self._principal = Principal(
            user_id=UUID(settings.dev_user_id),
            display_name=settings.dev_user_name,
            expires_at=None,
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


def make_verifier(settings: Settings) -> TokenVerifier:
    if settings.auth == "dev":
        return DevTokenVerifier(settings)
    raise RuntimeError(
        "COPILOT_AUTH=cognito is not implemented yet (Cognito comes later); "
        "for local development set COPILOT_ENV=dev and COPILOT_AUTH=dev"
    )


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
