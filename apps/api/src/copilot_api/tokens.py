"""The API's own tokens (ARCHITECTURE.md, "Email sign-in" and "Career-OS sign-in").

Email sign-in and Career-OS sign-in both end the same way: the API issues

- an **access token**, a JWT it signs itself (`copilot_core.auth.TokenSigner`), about an hour;
- a **refresh token**, a row in `app.refresh_tokens` (migrations 0014, 0015) kept as a SHA-256
  hash. It works once: every refresh replaces it, and a replaced one shown again means it
  leaked, which revokes every token of that sign-in (`family_id`).

A refresh can also end a sign-in for a reason of its own: an email address taken off the
allow-list, or whatever a provider's `checks` entry says (Career-OS sign-in asks Career-OS
whether the account was disabled).
"""

import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg

from copilot_api.config import ApiSettings
from copilot_core.auth import CAREER_OS, TokenSigner, allow_list
from copilot_core.db import become, sign_in_tx, user_tx
from copilot_core.ids import uuid7

log = logging.getLogger(__name__)


class RefreshError(Exception):
    """`reason`: "invalid" (unknown, expired or revoked), "reused" (a replaced token shown
    again: its chain is now revoked), "not_allowed" (off the allow-list, or ended by the
    sign-in's provider)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    email: str  # the address of an email sign-in; empty for a Career-OS sign-in
    name: str = ""
    provider: str = "email"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# Whether a sign-in may go on: asked on refresh, with the account's id. False ends it.
type Check = Callable[[UUID], Awaitable[bool]]


class TokenService:
    def __init__(self, settings: ApiSettings, pool: asyncpg.Pool) -> None:
        key = settings.email_signing_key
        if key is None:
            raise RuntimeError(
                "COPILOT_EMAIL_SIGN_IN and COPILOT_CAREER_OS_SIGN_IN need "
                "COPILOT_EMAIL_SIGNING_KEY on the API"
            )
        self.settings = settings
        self.pool = pool
        self.seed = key.get_secret_value()
        self.signer = TokenSigner(self.seed, settings.email_access_ttl_s)
        self.allowed = allow_list(settings.google_allowed_emails)
        self.checks: dict[str, Check] = {}

    def may_sign_in(self, email: str) -> bool:
        return not self.allowed or email in self.allowed

    def _allowed(self, provider: str, email: str | None) -> bool:
        if provider == CAREER_OS:
            return not self.allowed  # an allow-list is of verified addresses; it has none
        return email is not None and self.may_sign_in(email)

    def _tokens(
        self, user_id: UUID, name: str, refresh: str, provider: str, email: str | None,
        subject: str | None,
    ) -> Tokens:  # fmt: skip
        access = self.signer.sign(user_id, email, name, provider=provider, subject=subject)
        return Tokens(access, refresh, self.signer.ttl_s, email or "", name, provider)

    async def _insert(
        self, conn: Any, user_id: UUID, family: UUID, provider: str, email: str | None,
        subject: str | None,
    ) -> str:  # fmt: skip
        token = secrets.token_urlsafe(32)
        await conn.execute(
            "insert into app.refresh_tokens"
            " (token_hash, user_id, family_id, provider, email, subject, expires_at)"
            " values ($1, $2, $3, $4, $5, $6, now() + $7::interval)",
            sha(token), user_id, family, provider, email, subject,
            timedelta(days=self.settings.email_refresh_ttl_days),
        )  # fmt: skip
        return token

    async def issue(
        self, user_id: UUID, name: str, *, email: str | None = None, provider: str = "email",
        subject: str | None = None,
    ) -> Tokens:  # fmt: skip
        """The tokens of a new sign-in to `user_id`."""
        async with user_tx(self.pool, user_id) as conn:
            # Tokens past their time have nothing left to tell; replaced ones are kept until
            # then, to recognize one shown again.
            await conn.execute(
                "delete from app.refresh_tokens where user_id = $1 and expires_at < now()", user_id
            )
            refresh = await self._insert(conn, user_id, uuid7(), provider, email, subject)
        return self._tokens(user_id, name, refresh, provider, email, subject)

    async def refresh(self, token: str) -> Tokens:
        """Replace a refresh token: new access and refresh tokens, and the old one is spent.
        A spent token shown again revokes every token of its sign-in."""
        digest = sha(token)
        async with sign_in_tx(self.pool, f"refresh:{digest}") as conn:
            found = await conn.fetchrow(
                "select user_id, provider from app.refresh_tokens where token_hash = $1", digest
            )
        if found is None:
            raise RefreshError("invalid")
        user_id: UUID = found["user_id"]
        # Asked outside the transaction: it may call another service.
        check = self.checks.get(str(found["provider"]))
        ended = check is not None and not await check(user_id)
        reused = False
        async with sign_in_tx(self.pool, f"refresh:{digest}") as conn:
            await become(conn, user_id)
            row = await conn.fetchrow(
                "select family_id, provider, email, subject, replaced_at, revoked_at,"
                " expires_at <= now() as expired"
                "  from app.refresh_tokens where token_hash = $1 for update",
                digest,
            )
            if row is None or row["revoked_at"] is not None or row["expired"]:
                raise RefreshError("invalid")
            provider, family = str(row["provider"]), row["family_id"]
            email, subject = row["email"], row["subject"]
            if row["replaced_at"] is not None or ended or not self._allowed(provider, email):
                reused = row["replaced_at"] is not None
                await self.revoke_family(conn, user_id, family)
            else:
                await conn.execute(
                    "update app.refresh_tokens set replaced_at = now() where token_hash = $1",
                    digest,
                )
                name = await conn.fetchval(
                    "select display_name from app.users where id = $1", user_id
                )
                fresh = await self._insert(conn, user_id, family, provider, email, subject)
                return self._tokens(
                    user_id, str(name or email or "Career-OS account"), fresh, provider, email,
                    subject,
                )  # fmt: skip
        # Outside the transaction, so the revocation above is committed.
        if reused:
            log.warning("refresh token reuse: revoked the sign-in of user %s", user_id)
        raise RefreshError("reused" if reused else "not_allowed")

    async def sign_out(self, token: str) -> bool:
        """Revoke the refresh token's whole sign-in; False if the token is not known."""
        digest = sha(token)
        async with sign_in_tx(self.pool, f"refresh:{digest}") as conn:
            row = await conn.fetchrow(
                "select user_id, family_id from app.refresh_tokens where token_hash = $1", digest
            )
            if row is None:
                return False
            await become(conn, row["user_id"])
            await self.revoke_family(conn, row["user_id"], row["family_id"])
        return True

    @staticmethod
    async def revoke_family(conn: Any, user_id: UUID, family: UUID) -> None:
        await conn.execute(
            "update app.refresh_tokens set revoked_at = now()"
            " where user_id = $1 and family_id = $2 and revoked_at is null",
            user_id, family,
        )  # fmt: skip

    @staticmethod
    async def revoke_provider(conn: Any, user_id: UUID, provider: str) -> None:
        """End every sign-in of one kind to an account (in the account's transaction)."""
        await conn.execute(
            "update app.refresh_tokens set revoked_at = now()"
            " where user_id = $1 and provider = $2 and revoked_at is null",
            user_id, provider,
        )  # fmt: skip
