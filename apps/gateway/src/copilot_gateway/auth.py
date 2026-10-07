"""Access token verification; shared with the API, so it lives in copilot_core.auth."""

from copilot_core.auth import (
    AuthError,
    CognitoVerifier,
    DevTokenVerifier,
    Principal,
    StaticVerifier,
    TokenVerifier,
    bearer_token,
    make_verifier,
)

__all__ = [
    "AuthError",
    "CognitoVerifier",
    "DevTokenVerifier",
    "Principal",
    "StaticVerifier",
    "TokenVerifier",
    "bearer_token",
    "make_verifier",
]
