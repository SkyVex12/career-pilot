"""The Career-OS client (ARCHITECTURE.md, "Career-OS link"). The contract is Career-OS's
`/v1/career-pilot/*` API (docs/career-pilot-api.md in Career-OS):

- `POST /link {code, client_label}`: the one-time code a Career-OS user, admin or super admin
  generated while signed in, exchanged once for a read-only token bound to that account
  (`account_type`). A user's token reads that user's applications; an admin's reads those of
  the users linked to that admin.
- With `Authorization: Bearer <token>`: `GET /me`, `GET /users` (the users the token reads),
  `GET /applications` (search, optionally for one of those users),
  `GET /applications/{id}` (with its JD and resume as text), `GET /applications/{id}/resume-file`,
  and `DELETE /link` (revoke).

Nothing is fetched unless the user asks: no sync runs on its own.

Sign-in ("Career-OS sign-in" in ARCHITECTURE.md) adds two calls with no token:
`POST /oauth/token` exchanges the authorization code the desktop app got in the browser (with
its PKCE verifier) for an **assertion**, a short-lived JWT signed by Career-OS that says who
signed in, and a link code for `POST /link`; `GET /oauth/keys` gives the public keys the
assertion is checked against (`AssertionVerifier`).
"""

import base64
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from copilot_core.db import Conn

PREFIX = "/v1/career-pilot"
PROVIDER = "career_os"  # app.integrations.provider
REQUIRED_CODE = "career_os_required"
REQUIRED_MESSAGE = "Please link your Career-OS account to use this app (Settings → Career-OS)."


async def is_linked(conn: Conn) -> bool:
    """The account (the transaction's user) has an active Career-OS link. A link Career-OS
    stopped accepting is marked revoked, and counts as none."""
    found = await conn.fetchval(
        "select 1 from app.integrations where provider = $1 and status = 'active'", PROVIDER
    )
    return found is not None


MAX_FILE_BYTES = 10 * 1024 * 1024

# Sign-in: the one client Career-OS knows, and what its assertions say about themselves.
SIGN_IN_CLIENT_ID = "career-pilot"
ASSERTION_ISSUER = "career-os"
ASSERTION_AUDIENCE = "career-pilot"
ASSERTION_MAX_LIFE_S = 300
PRINCIPAL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def principal_kind(account_type: str | None) -> str:
    """`user` or `admin`, from a link's account type: a super admin is an admin whose role
    says so, and a link made before the type was kept counts as a user's."""
    return "admin" if account_type in ("admin", "super_admin") else "user"


@dataclass(frozen=True)
class CareerOsPerson:
    """Who signed in on Career-OS, as its assertion says. `email` is what Career-OS has on
    file; unless `email_verified`, it is a hint and proves nothing."""

    kind: str  # "user" | "admin"
    id: str
    account_type: str  # "user" | "admin" | "super_admin"
    name: str
    email: str | None
    email_verified: bool
    jti: str
    expires_at: float

    @property
    def subject(self) -> str:
        """The identity's subject (app.identities) and the key of its sign-in lookups."""
        return f"{self.kind}:{self.id}"


class InvalidAssertion(Exception):
    """The assertion is not one Career-OS signed for this app, now."""


class CareerOsError(Exception):
    """Career-OS refused or could not be reached. `status` is its HTTP status, or 502 when it
    was unreachable or answered with something that is not the contract."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def unauthorized(self) -> bool:
        """The token was revoked or expired on the Career-OS side."""
        return self.status == 401


@dataclass
class ResumeFile:
    body: bytes
    content_type: str
    filename: str


class CareerOsClient:
    def __init__(
        self, base_url: str, timeout_s: float = 15.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:  # fmt: skip
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.transport = transport

    async def _request(
        self, method: str, path: str, token: str | None = None, **kwargs: Any
    ) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx2.AsyncClient(
                base_url=self.base_url, timeout=self.timeout_s, transport=self.transport
            ) as client:
                response = await client.request(method, PREFIX + path, headers=headers, **kwargs)
        except httpx2.HTTPError as e:
            raise CareerOsError(502, f"Career-OS could not be reached ({type(e).__name__})") from e
        if response.status_code >= 400:
            raise CareerOsError(response.status_code, _detail(response))
        return response

    async def _json(self, method: str, path: str, token: str | None = None,
                    **kwargs: Any) -> dict[str, Any]:  # fmt: skip
        response = await self._request(method, path, token, **kwargs)
        try:
            data = response.json()
        except ValueError as e:
            raise CareerOsError(502, "Career-OS answered with something that is not JSON") from e
        if not isinstance(data, dict):
            raise CareerOsError(502, "Career-OS answered with something unexpected")
        return data  # pyright: ignore[reportUnknownVariableType]

    async def link(self, code: str, client_label: str) -> dict[str, Any]:
        """Exchange a one-time code: `{token, link_id, user: {id, name}, account_type, scopes}`.
        `user` is the linked account: the admin, for an admin link."""
        data = await self._json("POST", "/link", json={"code": code, "client_label": client_label})
        if not isinstance(data.get("token"), str) or not isinstance(data.get("user"), dict):
            raise CareerOsError(502, "Career-OS answered the link without a token")
        return data

    async def sign_in_token(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> dict[str, Any]:
        """Exchange a sign-in's authorization code: `{assertion, link_code, ...}`. Career-OS
        answers 400 for a code that is unknown, used or expired (or the wrong verifier), and
        403 when the account was disabled."""
        data = await self._json("POST", "/oauth/token", json={
            "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
            "redirect_uri": redirect_uri, "client_id": SIGN_IN_CLIENT_ID,
        })  # fmt: skip
        if not isinstance(data.get("assertion"), str) or not isinstance(data.get("link_code"), str):
            raise CareerOsError(502, "Career-OS answered the sign-in without an assertion")
        return data

    async def sign_in_keys(self) -> dict[str, Any]:
        """`{keys: [{kid, alg, public_key}]}`: the keys Career-OS signs assertions with."""
        return await self._json("GET", "/oauth/keys")

    async def revoke(self, token: str) -> None:
        """Revoke the token. Already revoked (401) counts as done."""
        try:
            await self._request("DELETE", "/link", token)
        except CareerOsError as e:
            if not e.unauthorized:
                raise

    async def me(self, token: str) -> dict[str, Any]:
        return await self._json("GET", "/me", token)

    async def users(self, token: str) -> dict[str, Any]:
        """`{items: [{id, name}]}`: the users whose applications the token reads."""
        return await self._json("GET", "/users", token)

    async def search(self, token: str, params: dict[str, str]) -> dict[str, Any]:
        """`{items: [...], next_cursor}`: the applications in the token's scope; `user_id` in
        `params` narrows to one of its users. Each item names its `user`."""
        return await self._json("GET", "/applications", token, params=params)

    async def application(self, token: str, application_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/applications/{application_id}", token)

    async def resume_file(self, token: str, application_id: str) -> ResumeFile | None:
        try:
            response = await self._request(
                "GET", f"/applications/{application_id}/resume-file", token
            )
        except CareerOsError as e:
            if e.status == 404:
                return None
            raise
        if len(response.content) > MAX_FILE_BYTES:
            raise CareerOsError(502, "the resume file from Career-OS is too large")
        disposition = response.headers.get("content-disposition", "")
        filename = (
            disposition.split("filename=", 1)[-1].strip('"; ')
            if "filename=" in disposition
            else "resume"
        )
        return ResumeFile(
            body=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream").split(
                ";"
            )[0],
            filename=filename or "resume",
        )


def _detail(response: httpx2.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail:
        return detail[:300]
    return f"Career-OS answered {response.status_code}"


class AssertionVerifier:
    """Checks a sign-in assertion: Career-OS's signature (Ed25519, against the keys its API
    serves over HTTPS), that it was made for this app (`aud`), by Career-OS (`iss`), and is
    still fresh. The keys are kept for `ttl_s`; an unknown key id refetches them at most once
    per `min_refresh_s`, so made-up key ids cannot hammer Career-OS."""

    def __init__(
        self, client: CareerOsClient, ttl_s: float = 3600, min_refresh_s: float = 60,
        leeway_s: float = 30, clock: Callable[[], float] = time.monotonic,
    ) -> None:  # fmt: skip
        self.client = client
        self.ttl_s = ttl_s
        self.min_refresh_s = min_refresh_s
        self.leeway_s = leeway_s
        self.clock = clock
        self.keys: dict[str, Ed25519PublicKey] = {}
        self.fetched_at: float | None = None

    async def _refresh(self) -> None:
        data = await self.client.sign_in_keys()
        keys: dict[str, Ed25519PublicKey] = {}
        entries: list[Any] = data.get("keys") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            found: dict[str, Any] = entry  # pyright: ignore[reportUnknownVariableType]
            if found.get("alg") != "EdDSA":
                continue
            text = str(found.get("public_key") or "")
            try:
                raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
                keys[str(found.get("kid"))] = Ed25519PublicKey.from_public_bytes(raw)
            except ValueError:
                continue
        self.keys = keys
        self.fetched_at = self.clock()

    async def _key(self, kid: str) -> Ed25519PublicKey:
        now = self.clock()
        stale = self.fetched_at is None or now - self.fetched_at > self.ttl_s
        unknown = kid not in self.keys and (
            self.fetched_at is None or now - self.fetched_at > self.min_refresh_s
        )
        if stale or unknown:
            await self._refresh()  # CareerOsError when Career-OS cannot be reached
        key = self.keys.get(kid)
        if key is None:
            raise InvalidAssertion("unknown signing key")
        return key

    async def verify(self, assertion: str) -> CareerOsPerson:
        try:
            header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError as e:
            raise InvalidAssertion("malformed assertion") from e
        if header.get("alg") != "EdDSA":
            raise InvalidAssertion("unexpected algorithm")
        key = await self._key(str(header.get("kid", "")))
        try:
            claims: dict[str, Any] = jwt.decode(
                assertion,
                key,
                algorithms=["EdDSA"],
                issuer=ASSERTION_ISSUER,
                audience=ASSERTION_AUDIENCE,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "sub", "iss", "aud", "jti"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise InvalidAssertion("the assertion expired") from e
        except jwt.InvalidAudienceError as e:
            raise InvalidAssertion("the assertion is for another app") from e
        except jwt.PyJWTError as e:
            raise InvalidAssertion(f"invalid assertion: {type(e).__name__}") from e
        if float(claims["exp"]) - float(claims["iat"]) > ASSERTION_MAX_LIFE_S:
            raise InvalidAssertion("the assertion lives too long")
        kind, pid = claims.get("principal_kind"), claims.get("principal_id")
        if kind not in ("user", "admin") or not isinstance(pid, str):
            raise InvalidAssertion("the assertion names no account")
        if not PRINCIPAL_ID.fullmatch(pid) or claims["sub"] != f"{kind}:{pid}":
            raise InvalidAssertion("the assertion names no account")
        account_type = claims.get("account_type")
        if account_type not in ("user", "admin", "super_admin") or (
            principal_kind(account_type) != kind
        ):
            raise InvalidAssertion("the assertion's account type does not match")
        email = str(claims.get("email") or "").strip().lower()
        return CareerOsPerson(
            kind=str(kind),
            id=pid,
            account_type=str(account_type),
            name=str(claims.get("name") or "").strip()[:200],
            email=email if 3 <= len(email) <= 320 and "@" in email else None,
            email_verified=claims.get("email_verified") is True,
            jti=str(claims["jti"]),
            expires_at=float(claims["exp"]),
        )


def make_client(base_url: str | None, timeout_s: float = 15.0) -> CareerOsClient | None:
    return CareerOsClient(base_url, timeout_s) if base_url else None
