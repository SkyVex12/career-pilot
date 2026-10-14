"""`GET /auth/config`: how the desktop app signs in to this deployment, with no token.

The app knows only the API's base URL; this tells it whether to use the Cognito sign-in
(OAuth with PKCE in the browser, against the user pool's own domain) or the dev token on the
local stack. Nothing here is secret: the app client is a public client without a secret.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from copilot_api.deps import SvcDep

router = APIRouter()

SCOPES = ["openid", "email", "profile"]


class AuthConfig(BaseModel):
    provider: Literal["cognito", "dev"]
    # Cognito only: the user pool's OAuth domain (https://<prefix>.auth.<region>.amazoncognito
    # .com), with /oauth2/authorize and /oauth2/token under it, and the app client to use.
    domain: str | None = None
    client_id: str | None = None
    scopes: list[str] = []


@router.get("/auth/config")
async def auth_config(svc: SvcDep) -> AuthConfig:
    s = svc.settings
    if s.auth == "dev":
        return AuthConfig(provider="dev")
    return AuthConfig(
        provider="cognito",
        domain=(s.cognito_domain or "").rstrip("/") or None,
        client_id=s.cognito_client_ids[0] if s.cognito_client_ids else None,
        scopes=SCOPES,
    )
