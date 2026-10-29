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
"""

from dataclasses import dataclass
from typing import Any

import httpx2

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


def make_client(base_url: str | None, timeout_s: float = 15.0) -> CareerOsClient | None:
    return CareerOsClient(base_url, timeout_s) if base_url else None
