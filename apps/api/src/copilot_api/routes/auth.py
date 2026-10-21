"""`GET /auth/config`: how the desktop app signs in to this deployment, with no token.

The app knows only the API's base URL; this tells it whether to use a browser sign-in (OAuth
with PKCE: Cognito's own domain on AWS, Google on GCP) or the dev token on the local stack.
Nothing here is secret: Cognito's app client is a public client without a secret, and Google
treats a desktop client's secret as not confidential (it ships inside every installed app).
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from copilot_api.deps import SvcDep

router = APIRouter()

SCOPES = ["openid", "email", "profile"]
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"


class AuthConfig(BaseModel):
    provider: Literal["cognito", "google", "dev"]
    # Cognito: the user pool's OAuth domain (https://<prefix>.auth.<region>.amazoncognito
    # .com), with /oauth2/authorize, /oauth2/token and /oauth2/revoke under it.
    domain: str | None = None
    client_id: str | None = None
    scopes: list[str] = []
    # Google (and any provider whose endpoints are not under one domain): the endpoints, the
    # desktop client's secret, which token the services take as the bearer, and extra
    # authorize parameters (a refresh token needs access_type=offline).
    authorize_url: str | None = None
    token_url: str | None = None
    revoke_url: str | None = None
    client_secret: str | None = None
    bearer: Literal["access_token", "id_token"] = "access_token"
    authorize_params: dict[str, str] = {}


# Defaults are left out, so Cognito's answer is what it was before Google was added.
@router.get("/auth/config", response_model_exclude_defaults=True)
async def auth_config(svc: SvcDep) -> AuthConfig:
    s = svc.settings
    if s.auth == "dev":
        return AuthConfig(provider="dev")
    if s.auth == "google":
        return AuthConfig(
            provider="google",
            client_id=s.google_client_ids[0] if s.google_client_ids else None,
            client_secret=s.google_client_secret.get_secret_value()
            if s.google_client_secret
            else None,
            scopes=SCOPES,
            authorize_url=GOOGLE_AUTHORIZE,
            token_url=GOOGLE_TOKEN,
            revoke_url=GOOGLE_REVOKE,
            # Google's access tokens are opaque; its ID token is the JWT the services verify.
            bearer="id_token",
            # prompt=consent: Google returns a refresh token only on a consented sign-in.
            authorize_params={"access_type": "offline", "prompt": "consent"},
        )
    return AuthConfig(
        provider="cognito",
        domain=(s.cognito_domain or "").rstrip("/") or None,
        client_id=s.cognito_client_ids[0] if s.cognito_client_ids else None,
        scopes=SCOPES,
    )
