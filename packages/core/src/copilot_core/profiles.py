"""Profiles (ARCHITECTURE.md, "Profiles"): the people one account works for.

The account itself is "Me". Each Career-OS user that the account's Career-OS link reads (an
admin link) gets a profile: an `app.users` row of its own, with `owner_id` naming the account,
listed in the account's `app.profiles`. Requests name the profile they act for (the
`x-copilot-profile` header on the API, the `profile` query parameter on the gateway's
WebSocket); `resolve` checks it against the account and returns the profile's principal, so
every query, data key, job and Valkey key from there on is the profile's.
"""

from typing import Any
from uuid import UUID

import asyncpg

from copilot_core.auth import Principal
from copilot_core.db import user_tx
from copilot_core.ids import uuid7

HEADER = "x-copilot-profile"
QUERY = "profile"
MAX_NAME = 200


class ProfileError(Exception):
    """The profile named is not one of the account's."""


async def resolve(pool: asyncpg.Pool, account: Principal, profile: str | None) -> Principal:
    """The principal to act as: the account itself when no profile (or the account's own id)
    is named, else the account's profile with that id."""
    if not profile or profile.strip() in ("", "me", str(account.user_id)):
        return account
    try:
        pid = UUID(profile.strip())
    except ValueError as e:
        raise ProfileError("not a profile id") from e
    async with user_tx(pool, account.user_id) as conn:
        name = await conn.fetchval("select name from app.profiles where id = $1", pid)
    if name is None:
        raise ProfileError("no such profile for this account")
    return Principal(pid, str(name) or account.display_name, account.expires_at, email=None)


def profile_out(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "career_os_user_id": row["career_os_user_id"],
        "me": False,
    }


def me_out(account: Principal, career_os_user_id: str | None = None) -> dict[str, Any]:
    return {
        "id": str(account.user_id),
        "name": account.display_name,
        "career_os_user_id": career_os_user_id,
        "me": True,
    }


async def listed(pool: asyncpg.Pool, account: Principal) -> list[dict[str, Any]]:
    """The account's profiles, by name."""
    async with user_tx(pool, account.user_id) as conn:
        rows = await conn.fetch(
            "select id, name, career_os_user_id from app.profiles order by name, created_at"
        )
    return [profile_out(r) for r in rows]


async def ensure(pool: asyncpg.Pool, account: Principal, career_os_user_id: str, name: str) -> UUID:
    """The profile for a Career-OS user, made on first use (its users row first, with the
    account's settings, then the account's `profiles` row). A changed name follows Career-OS."""
    name = (name or career_os_user_id).strip()[:MAX_NAME] or career_os_user_id
    async with user_tx(pool, account.user_id) as conn:
        row = await conn.fetchrow(
            "select id, name from app.profiles where career_os_user_id = $1", career_os_user_id
        )
        settings = await conn.fetchval(
            "select settings from app.users where id = $1", account.user_id
        )
        if row is not None and row["name"] != name:
            await conn.execute(
                "update app.profiles set name = $2, updated_at = now() where id = $1",
                row["id"], name,
            )  # fmt: skip
    if row is not None:
        if row["name"] != name:
            async with user_tx(pool, row["id"]) as conn:
                await conn.execute(
                    "update app.users set display_name = $2 where id = $1", row["id"], name
                )
        return row["id"]
    pid = uuid7()
    async with user_tx(pool, pid) as conn:
        await conn.execute(
            "insert into app.users (id, email, display_name, settings, owner_id)"
            " values ($1, $2, $3, $4, $5)",
            pid, f"profile-{pid}@profiles.invalid", name, settings or {}, account.user_id,
        )  # fmt: skip
    async with user_tx(pool, account.user_id) as conn:
        got = await conn.fetchval(
            "insert into app.profiles (id, user_id, career_os_user_id, name)"
            " values ($1, $2, $3, $4) on conflict (user_id, career_os_user_id) do nothing"
            " returning id",
            pid, account.user_id, career_os_user_id, name,
        )  # fmt: skip
        if got is None:  # made at the same time by another request: use that one
            got = await conn.fetchval(
                "select id from app.profiles where career_os_user_id = $1", career_os_user_id
            )
    if got != pid:
        async with user_tx(pool, pid) as conn:
            await conn.execute("delete from app.users where id = $1", pid)
    return got


async def sync_settings(pool: asyncpg.Pool, account: Principal, settings: dict[str, Any]) -> None:
    """Copy the account's settings (retention, consent, standup time) to its profiles: their
    users rows carry them for the services that read a user's settings."""
    for p in await listed(pool, account):
        async with user_tx(pool, UUID(p["id"])) as conn:
            await conn.execute(
                "update app.users set settings = $2 where id = $1", UUID(p["id"]), settings
            )
