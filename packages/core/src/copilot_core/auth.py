"""Access token verification for the gateway and the API (ARCHITECTURE.md, "Connection and
auth" and "REST API").

Four verifiers behind one interface:
- `CognitoVerifier`: a Cognito access token (RS256 JWT), checked against the user pool's JWKS,
  which is cached and refetched at most once a minute when an unknown key id shows up. It
  checks the signature, issuer, expiry, `token_use == "access"` and the app client id
  (Cognito access tokens carry it as `client_id`, not `aud`).
- `GoogleVerifier` (Stage 0 on GCP): a Google ID token (Google's access tokens are opaque),
  checked against Google's JWKS the same way: signature, issuer, expiry, the desktop OAuth
  client as `aud`, a verified email, and that email on the allow-list when there is one (an
  empty list lets every verified Google account in). Google's `sub` is not a
  UUID, so the user id is a UUIDv5 of it: stable, and the same on every service. An account
  first made by an email sign-in has another id; `app.identities` names it (`IdentityLookup`).
- `CopilotVerifier` (email sign-in and Career-OS sign-in): an access token the API signed
  itself, after an emailed one-time code or a sign-in on Career-OS (`TokenSigner`: Ed25519,
  about an hour). `sub` is the user id; `idp` says which sign-in it was when it was not email.
  Checked against the deployment's public keys, with no network call.
- `DevTokenVerifier`: a static dev token for one dev user. It refuses to start outside
  COPILOT_ENV=dev, and refuses on cloud compute (ECS, Lambda, Compute Engine) even then.

A deployment takes its provider's tokens and, with `email_sign_in` or `career_os_sign_in`,
the API's own as well:
`ByIssuer` reads the token's `iss` (unverified, only to choose) and hands the token to that
issuer's verifier, which checks everything.
"""

import asyncio
import base64
import hashlib
import hmac
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from copilot_core.config import CoreSettings, cloud_runtime


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    display_name: str
    expires_at: float | None  # Unix seconds; None for tokens that do not expire
    email: str | None = None  # present when the token carries it; see API first login
    # The sign-in behind the token ("google", "email", "career_os") and its subject there,
    # when the verifier knows them (app.identities).
    provider: str | None = None
    subject: str | None = None
    # No app.identities row names this sign-in yet: the API records one (first login).
    new_identity: bool = False


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> Principal:
        """Return the token's user, or raise AuthError."""
        ...


class IdentityLookup(Protocol):
    async def user_of(self, provider: str, subject: str) -> UUID | None:
        """The account a sign-in belongs to (app.identities), or None when none is recorded.
        Raises AuthError when it cannot be checked."""
        ...


def allow_list(emails: list[str]) -> set[str]:
    return {e.strip().lower() for e in emails if e.strip()}


def refuse_outside_dev(settings: CoreSettings, what: str, environ: Mapping[str, str]) -> None:
    if settings.env != "dev":
        raise RuntimeError(f"{what} needs COPILOT_ENV=dev; this is {settings.env!r}")
    markers = cloud_runtime(environ)
    if markers:
        raise RuntimeError(f"{what} cannot run in the cloud ({', '.join(markers)})")


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


GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# Namespace for user ids made from Google subjects: uuid5(GOOGLE_USER_NAMESPACE, "google:<sub>").
GOOGLE_USER_NAMESPACE = uuid.UUID("5c1f3e0a-9d2b-4c47-8a1e-6f0b2d7c9e41")


def google_user_id(sub: str) -> UUID:
    return uuid.uuid5(GOOGLE_USER_NAMESPACE, f"google:{sub}")


class GoogleVerifier:
    def __init__(
        self,
        client_ids: list[str],
        allowed_emails: list[str],
        jwks: JwksCache | None = None,
        leeway_s: float = 30,
        identities: IdentityLookup | None = None,
    ) -> None:
        if not client_ids:
            raise RuntimeError("COPILOT_GOOGLE_CLIENT_IDS must list the desktop OAuth client id")
        self.client_ids = list(client_ids)
        # Empty: any Google account with a verified email may sign in.
        self.allowed = allow_list(allowed_emails)
        self.jwks = jwks or JwksCache(GOOGLE_JWKS_URL)
        self.leeway_s = leeway_s
        self.identities = identities

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
                audience=self.client_ids,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except jwt.InvalidAudienceError as e:
            raise AuthError("token is for another app client") from e
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise AuthError("token is not from Google")
        email = str(claims.get("email") or "").lower()
        if not email or claims.get("email_verified") is not True:
            raise AuthError("the Google account has no verified email")
        if self.allowed and email not in self.allowed:
            raise AuthError("this Google account is not on the allow-list")
        # The account this Google sign-in was recorded for; without a record, the UUIDv5 of
        # `sub`, which is the id of every account a Google sign-in made.
        sub = str(claims["sub"])
        known = await self.identities.user_of("google", sub) if self.identities else None
        return Principal(
            user_id=known or google_user_id(sub),
            display_name=str(claims.get("name") or email),
            expires_at=float(claims["exp"]),
            email=email,
            provider="google",
            subject=sub,
            new_identity=self.identities is not None and known is None,
        )


# --- Email sign-in: access tokens the API signs itself -----------------------------------------

COPILOT_ISSUER = "copilot"
COPILOT_AUDIENCE = "copilot"
COPILOT_ALGORITHM = "EdDSA"
CAREER_OS = "career_os"  # the `idp` of a Career-OS sign-in, and its app.identities.provider


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _key_bytes(text: str, what: str) -> bytes:
    text = text.strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as e:
        raise RuntimeError(f"{what} is not base64url") from e
    if len(raw) != 32:
        raise RuntimeError(f"{what} must be 32 bytes (an Ed25519 key), base64url")
    return raw


def public_key_of(signing_key: str) -> str:
    """The public key (base64url) of an Ed25519 signing key given as its base64url seed."""
    private = Ed25519PrivateKey.from_private_bytes(_key_bytes(signing_key, "the signing key"))
    return _b64(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def new_signing_key() -> tuple[str, str]:
    """A new signing key and its public key, both base64url."""
    seed = _b64(os.urandom(32))
    return seed, public_key_of(seed)


def key_id(public_key: str) -> str:
    """The `kid` of a public key: the start of its SHA-256."""
    return hashlib.sha256(_key_bytes(public_key, "a public key")).hexdigest()[:16]


class TokenSigner:
    """Signs the API's own access tokens. `sub` is the user id, so a verifier needs nothing
    but the public key."""

    def __init__(self, signing_key: str, ttl_s: int = 3600) -> None:
        self.key = Ed25519PrivateKey.from_private_bytes(_key_bytes(signing_key, "the signing key"))
        self.kid = key_id(public_key_of(signing_key))
        self.ttl_s = ttl_s

    def sign(
        self, user_id: UUID, email: str | None, name: str, now: float | None = None,
        provider: str = "email", subject: str | None = None,
    ) -> str:  # fmt: skip
        """An email sign-in's token carries its address. Any other sign-in's (`provider`
        "career_os") carries `idp` and its subject there (`idp_sub`), and no email: Career-OS
        does not verify addresses, so none is vouched for."""
        issued = int(time.time() if now is None else now)
        claims: dict[str, Any] = {
            "iss": COPILOT_ISSUER,
            "aud": COPILOT_AUDIENCE,
            "sub": str(user_id),
            "name": name,
            "token_use": "access",
            "iat": issued,
            "exp": issued + self.ttl_s,
        }
        if provider == "email":
            claims["email"] = email
        else:
            claims["idp"] = provider
            claims["idp_sub"] = subject
        return jwt.encode(claims, self.key, algorithm=COPILOT_ALGORITHM, headers={"kid": self.kid})


class CopilotVerifier:
    def __init__(
        self, public_keys: list[str], allowed_emails: list[str], leeway_s: float = 30
    ) -> None:
        if not public_keys:
            raise RuntimeError(
                "COPILOT_EMAIL_SIGN_IN and COPILOT_CAREER_OS_SIGN_IN need "
                "COPILOT_EMAIL_PUBLIC_KEYS (or, on the API and the local stack, "
                "COPILOT_EMAIL_SIGNING_KEY)"
            )
        self.keys = {
            key_id(k): Ed25519PublicKey.from_public_bytes(_key_bytes(k, "a public key"))
            for k in public_keys
        }
        self.allowed = allow_list(allowed_emails)
        self.leeway_s = leeway_s

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise AuthError("malformed token") from e
        if header.get("alg") != COPILOT_ALGORITHM:
            raise AuthError("unexpected token algorithm")
        key = self.keys.get(str(header.get("kid", "")))
        if key is None:
            raise AuthError("unknown signing key")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[COPILOT_ALGORITHM],
                issuer=COPILOT_ISSUER,
                audience=COPILOT_AUDIENCE,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e
        if claims.get("token_use") != "access":
            raise AuthError("not an access token")
        try:
            user_id = UUID(str(claims["sub"]))
        except ValueError as e:
            raise AuthError("sub is not a user id") from e
        if claims.get("idp") == CAREER_OS:
            subject = str(claims.get("idp_sub") or "")
            if not subject:
                raise AuthError("the token names no Career-OS account")
            # An allow-list is a list of verified addresses, and a Career-OS sign-in has none.
            if self.allowed:
                raise AuthError("Career-OS sign-in is off while this server has an allow-list")
            return Principal(
                user_id=user_id,
                display_name=str(claims.get("name") or "Career-OS account"),
                expires_at=float(claims["exp"]),
                provider=CAREER_OS,
                subject=subject,
            )
        if "idp" in claims:
            raise AuthError("unknown sign-in")
        email = str(claims.get("email") or "").lower()
        if not email:
            raise AuthError("the token carries no email")
        # Checked on every use, so taking an address off the list ends its access at once.
        if self.allowed and email not in self.allowed:
            raise AuthError("this email is not on the allow-list")
        return Principal(
            user_id=user_id,
            display_name=str(claims.get("name") or email),
            expires_at=float(claims["exp"]),
            email=email,
            provider="email",
            subject=email,
        )


def unverified_issuer(token: str) -> str | None:
    """The `iss` a token claims, to choose its verifier; None for anything but a JWT."""
    try:
        issuer = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return None
    return issuer if isinstance(issuer, str) else None


class ByIssuer:
    """One verifier per issuer, and the deployment's own for every other token."""

    def __init__(self, issuers: Mapping[str, TokenVerifier], default: TokenVerifier) -> None:
        self.issuers = dict(issuers)
        self.default = default

    async def verify(self, token: str) -> Principal:
        verifier = self.issuers.get(unverified_issuer(token) or "", self.default)
        return await verifier.verify(token)


def email_public_keys(settings: CoreSettings) -> list[str]:
    """The keys this service checks the API's tokens against: the configured ones, or the
    signing key's own where the service has it (the API, and every service locally)."""
    if settings.email_public_keys:
        return list(settings.email_public_keys)
    if settings.email_signing_key is not None:
        return [public_key_of(settings.email_signing_key.get_secret_value())]
    return []


def make_verifier(
    settings: CoreSettings, identities: IdentityLookup | None = None
) -> TokenVerifier:
    """The deployment's verifier; with email sign-in or Career-OS sign-in on, the API's own
    tokens as well. `identities` resolves a Google sign-in to its account (services with a
    database)."""
    provider = _provider_verifier(settings, identities)
    if not (settings.email_sign_in or settings.career_os_sign_in):
        return provider
    own = CopilotVerifier(email_public_keys(settings), settings.google_allowed_emails)
    return ByIssuer({COPILOT_ISSUER: own}, provider)


def _provider_verifier(settings: CoreSettings, identities: IdentityLookup | None) -> TokenVerifier:
    if settings.auth == "dev":
        return DevTokenVerifier(settings)
    if settings.auth == "google":
        return GoogleVerifier(
            settings.google_client_ids,
            settings.google_allowed_emails,
            JwksCache(GOOGLE_JWKS_URL, ttl_s=settings.jwks_ttl_s),
            identities=identities,
        )
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
