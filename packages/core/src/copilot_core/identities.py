"""Sign-in identities (ARCHITECTURE.md, "Email sign-in"; migration 0014).

`app.identities` maps a sign-in to its account: `("google", <Google's sub>)` or
`("email", <the normalized address>)`. An account can have both.

The rule that joins them: **an email sign-in and a Google sign-in with the same verified
address are the same account.** Google vouches for the address in its ID token
(`email_verified`); the emailed code proves the email sign-in's. Whichever comes second joins
the account the first one made:

- `account_for_email`: the address of an email sign-in. It signs in to the account that has
  that address as its email identity, else the account whose Google identity has that verified
  address, else a new account.
- `adopt_google`: a Google sign-in with no identity row yet. It is the account with the id
  Google sign-ins always had (a UUIDv5 of `sub`) when that exists, else the account whose email
  identity is its verified address, else a new account with the UUIDv5 id.

Addresses are compared after `normalize_email` (trimmed, lowercased), nothing more: no dots or
plus tags are folded.
"""

import logging
import re
import time
from collections.abc import Callable
from uuid import UUID

import asyncpg

from copilot_core.auth import AuthError, Principal
from copilot_core.db import Conn, sign_in_tx, user_tx
from copilot_core.ids import uuid7

log = logging.getLogger(__name__)

MAX_EMAIL = 320
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def normalize_email(raw: str) -> str:
    """Trimmed and lowercased; ValueError for anything that is not one address."""
    email = raw.strip().lower()
    if len(email) > MAX_EMAIL or not _EMAIL.fullmatch(email):
        raise ValueError("not an email address")
    return email


class Identities:
    """`IdentityLookup` over the database. Found rows are remembered for a few minutes (a
    sign-in's account does not change); a miss is asked again every time, so a sign-in the API
    has just recorded is seen at once."""

    def __init__(
        self,
        pool: asyncpg.Pool | None = None,
        ttl_s: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pool = pool  # None: no database (the gateway offline), nothing is recorded
        self.ttl_s = ttl_s
        self.clock = clock
        self._found: dict[tuple[str, str], tuple[UUID, float]] = {}

    async def user_of(self, provider: str, subject: str) -> UUID | None:
        if self.pool is None:
            return None
        hit = self._found.get((provider, subject))
        if hit is not None and self.clock() - hit[1] < self.ttl_s:
            return hit[0]
        try:
            async with sign_in_tx(self.pool, f"{provider}:{subject}") as conn:
                user_id = await conn.fetchval(
                    "select user_id from app.identities where provider = $1 and subject = $2",
                    provider, subject,
                )  # fmt: skip
        except (OSError, asyncpg.PostgresError) as e:
            raise AuthError("could not check the sign-in; try again") from e
        if user_id is None:
            self._found.pop((provider, subject), None)
            return None
        if self.ttl_s <= 0:
            return user_id
        if len(self._found) > 10_000:
            self._found.clear()
        self._found[(provider, subject)] = (user_id, self.clock())
        return user_id

    def forget(self, provider: str, subject: str) -> None:
        self._found.pop((provider, subject), None)


async def _add(conn: Conn, provider: str, subject: str, user_id: UUID, email: str) -> None:
    await conn.execute(
        "insert into app.identities (provider, subject, user_id, email) values ($1, $2, $3, $4)"
        " on conflict (provider, subject) do nothing",
        provider, subject, user_id, email,
    )  # fmt: skip


def name_of(email: str) -> str:
    """A new email account's display name: the address before the @ (PATCH /me changes it)."""
    return email.split("@", 1)[0][:200] or email


class EmailTaken(Exception):
    """The address is another account's `users.email`, and no identity says it may sign in."""


async def account_for_email(pool: asyncpg.Pool, email: str) -> tuple[UUID, str]:
    """The account an email sign-in with this (normalized, code-verified) address signs in to,
    made when there is none: its id and display name."""
    async with sign_in_tx(pool, f"email:{email}") as conn:
        row = await conn.fetchrow(
            "select provider, user_id from app.identities"
            " where (provider = 'email' and subject = $1) or (provider = 'google' and email = $1)"
            " order by (provider = 'email') desc, created_at, user_id limit 1",
            email,
        )
    if row is not None:
        user_id: UUID = row["user_id"]
        async with user_tx(pool, user_id) as conn:
            if row["provider"] != "email":
                # The account's verified Google address: from now on its email sign-in too.
                await _add(conn, "email", email, user_id, email)
                log.info("email sign-in joined the Google account %s", user_id)
            name = await conn.fetchval("select display_name from app.users where id = $1", user_id)
        return user_id, str(name or name_of(email))
    user_id = uuid7()
    try:
        async with user_tx(pool, user_id) as conn:
            await conn.execute(
                "insert into app.users (id, email, display_name) values ($1, $2, $3)",
                user_id, email, name_of(email),
            )  # fmt: skip
            await _add(conn, "email", email, user_id, email)
    except asyncpg.UniqueViolationError as e:
        raise EmailTaken(email) from e
    log.info("created user %s on first email sign-in", user_id)
    return user_id, name_of(email)


async def adopt_google(pool: asyncpg.Pool, principal: Principal) -> Principal:
    """Record a Google sign-in that has no identity row (`principal.new_identity`) and return
    the principal of its account. A sign-in for a new account is left as it is: the caller
    makes the account, then calls `record_google`."""
    assert principal.subject
    assert principal.email
    email = principal.email.strip().lower()
    async with user_tx(pool, principal.user_id) as conn:
        if await conn.fetchval("select 1 from app.users where id = $1", principal.user_id):
            await record_google(conn, principal)
            return _recorded(principal)
    async with sign_in_tx(pool, f"email:{email}") as conn:
        other = await conn.fetchval(
            "select user_id from app.identities where provider = 'email' and subject = $1", email
        )
    if other is None:
        return principal
    async with user_tx(pool, other) as conn:
        await _add(conn, "google", principal.subject, other, email)
    log.info("Google sign-in joined the email account %s", other)
    return Principal(
        other, principal.display_name, principal.expires_at, email=principal.email,
        provider="google", subject=principal.subject,
    )  # fmt: skip


async def record_google(conn: Conn, principal: Principal) -> None:
    """The identity row of a Google sign-in, in its account's transaction. It replaces the
    row migration 0014 made for an account from before identities, whose subject is the
    account's id (Google's `sub` was not kept then)."""
    assert principal.subject
    assert principal.email
    await conn.execute(
        "delete from app.identities where provider = 'google' and subject = $1 and user_id = $2",
        str(principal.user_id), principal.user_id,
    )  # fmt: skip
    await _add(
        conn, "google", principal.subject, principal.user_id, principal.email.strip().lower()
    )


def _recorded(p: Principal) -> Principal:
    return Principal(
        p.user_id, p.display_name, p.expires_at, email=p.email,
        provider=p.provider, subject=p.subject,
    )  # fmt: skip
