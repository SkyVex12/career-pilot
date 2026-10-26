"""Shared services and request dependencies.

Every request carries an access token (Cognito, or the dev token locally). The first request
of a new user creates their `app.users` row from the token. Every handler then runs its
queries inside `services.tx(user)`, which opens a transaction that sets `app.user_id` first
(ARCHITECTURE.md, "Row-level security").

A request may name one of the account's profiles (`x-copilot-profile`; ARCHITECTURE.md,
"Profiles"). `UserDep` is then the profile, so the handler reads and writes the profile's rows;
`AccountDep` is always the signed-in account, for what belongs to the account (its settings,
its Career-OS link, its deletion).
"""

import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import asyncpg
import httpx2
from fastapi import Depends, Header, HTTPException, Request

from copilot_api.config import ApiSettings
from copilot_core import jobs, keys, logs, profiles
from copilot_core.auth import AuthError, Principal, TokenVerifier, bearer_token
from copilot_core.careeros import CareerOsClient
from copilot_core.crypto import UserCrypto
from copilot_core.db import Conn, user_tx
from copilot_core.packs import Digest
from copilot_core.storage import ObjectStore
from copilot_core.valkey_client import Valkey

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: ApiSettings
    pool: asyncpg.Pool
    valkey: Valkey
    queue: jobs.JobQueue
    store: ObjectStore
    crypto: UserCrypto
    verifier: TokenVerifier
    career_os: CareerOsClient | None = None  # None: COPILOT_CAREER_OS_URL is unset

    @asynccontextmanager
    async def tx(self, user: Principal) -> AsyncGenerator[Conn]:
        async with user_tx(self.pool, user.user_id) as conn:
            yield conn

    async def enqueue(
        self, job_type: str, key: str, user_id: UUID | None, payload: Mapping[str, Any],
        delay_s: int = 0,
    ) -> None:  # fmt: skip
        await self.queue.send(jobs.envelope(job_type, key, user_id, payload), delay_s=delay_s)

    async def digest_version(self, user_id: UUID) -> str | None:
        raw = await self.valkey.get(keys.digest(user_id))
        return None if raw is None else Digest.model_validate_json(raw).version


def services(request: Request) -> Services:
    return request.app.state.services


async def userinfo_email(settings: ApiSettings, token: str) -> str | None:
    """Cognito access tokens carry no email; its userInfo endpoint returns it (first login)."""
    if not settings.cognito_domain:
        return None
    url = settings.cognito_domain.rstrip("/") + "/oauth2/userInfo"
    async with httpx2.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        return None
    email = response.json().get("email")
    return str(email) if email else None


async def current_account(
    svc: Annotated[Services, Depends(services)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    token = bearer_token(authorization)
    if token is None:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        principal = await svc.verifier.verify(token)
    except AuthError as e:
        raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"}) from e
    logs.set_ids(user_id=principal.user_id)
    async with svc.tx(principal) as conn:
        known = await conn.fetchval("select 1 from app.users where id = $1", principal.user_id)
    if known is None:
        email = principal.email or await userinfo_email(svc.settings, token)
        if not email:
            raise HTTPException(403, "the token carries no email for the new account")
        try:
            async with svc.tx(principal) as conn:
                await conn.execute(
                    "insert into app.users (id, email, display_name) values ($1, $2, $3)"
                    " on conflict (id) do nothing",
                    principal.user_id,
                    email,
                    principal.display_name,
                )
        except asyncpg.UniqueViolationError as e:  # the email belongs to another account
            raise HTTPException(409, "this email belongs to another account") from e
        log.info("created user %s on first login", principal.user_id)
    return principal


async def current_user(
    svc: Annotated[Services, Depends(services)],
    account: Annotated[Principal, Depends(current_account)],
    x_copilot_profile: Annotated[str | None, Header()] = None,
) -> Principal:
    """The principal the request acts for: the account, or the profile it names."""
    try:
        principal = await profiles.resolve(svc.pool, account, x_copilot_profile)
    except profiles.ProfileError as e:
        raise HTTPException(404, f"profile: {e}") from e
    if principal is not account:
        logs.set_ids(user_id=principal.user_id)
    return principal


SvcDep = Annotated[Services, Depends(services)]
AccountDep = Annotated[Principal, Depends(current_account)]
UserDep = Annotated[Principal, Depends(current_user)]
