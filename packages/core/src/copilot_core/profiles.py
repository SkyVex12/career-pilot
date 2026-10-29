"""Profiles (ARCHITECTURE.md, "Profiles"): the people one account works for.

The account itself is "Me". Each Career-OS user that the account's Career-OS link reads (an
admin link) gets a profile: an `app.users` row of its own, with `owner_id` naming the account,
listed in the account's `app.profiles`. Requests name the profile they act for (the
`x-copilot-profile` header on the API, the `profile` query parameter on the gateway's
WebSocket); `resolve` checks it against the account and returns the profile's principal, so
every query, data key, job and Valkey key from there on is the profile's.

One workspace per Career-OS person, shared (`app.career_os_people`): every account linked to a
Career-OS user works in that user's one `app.users` row, whether it reaches them as a profile
(an admin link) or as "Me" (a user link, the account being that person). The first row that
held the person's data is theirs: an admin's profile row, or the user's own account row. A
user-linked account's "Me" resolves to that row when it is not the account's own; what belongs
to the account (`AccountDep`: settings, the link, usage, deletion) stays the account's.
"""

from typing import Any
from uuid import UUID

import asyncpg

from copilot_core.auth import Principal
from copilot_core.db import Conn, user_tx
from copilot_core.ids import uuid7

HEADER = "x-copilot-profile"
QUERY = "profile"
MAX_NAME = 200


class ProfileError(Exception):
    """The profile named is not one of the account's."""


async def person_of(conn: Conn, career_os_user_id: str) -> UUID | None:
    """The shared workspace of a Career-OS user, if one exists yet."""
    return await conn.fetchval(
        "select user_id from app.career_os_people where career_os_user_id = $1",
        career_os_user_id,
    )


async def claim(conn: Conn, career_os_user_id: str, user_id: UUID) -> UUID:
    """Make `user_id` the Career-OS user's workspace unless one exists; returns the one that
    is. Only called once Career-OS has said the account may read that user."""
    await conn.execute(
        "insert into app.career_os_people (career_os_user_id, user_id) values ($1, $2)"
        " on conflict (career_os_user_id) do nothing",
        career_os_user_id, user_id,
    )  # fmt: skip
    found = await person_of(conn, career_os_user_id)
    assert found is not None
    return found


async def me_of(pool: asyncpg.Pool, account: Principal) -> Principal:
    """ "Me": the account, or, for an active user link whose person's workspace is another
    row (an admin's profile made it first), that workspace."""
    async with user_tx(pool, account.user_id) as conn:
        link = await conn.fetchrow(
            "select external_id, external_kind from app.integrations"
            " where provider = 'career_os' and status = 'active'"
        )
        if link is None or link["external_kind"] != "user" or not link["external_id"]:
            return account
        person = await person_of(conn, str(link["external_id"]))
    if person is None or person == account.user_id:
        return account
    return Principal(person, account.display_name, account.expires_at, email=None)


async def claim_me(pool: asyncpg.Pool, account: Principal, career_os_user_id: str) -> None:
    """A user link: the person's workspace is the account's own row unless one exists."""
    async with user_tx(pool, account.user_id) as conn:
        await claim(conn, career_os_user_id, account.user_id)


async def resolve(pool: asyncpg.Pool, account: Principal, profile: str | None) -> Principal:
    """The principal to act as: "Me" when no profile (or the account's own id) is named,
    else the account's profile with that id."""
    if not profile or profile.strip() in ("", "me", str(account.user_id)):
        return await me_of(pool, account)
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
    """The account's home for a Career-OS user: the person's shared workspace, made on first
    use (a users row with the account's settings, owned by the account) and listed in the
    account's `profiles`; or the account itself when its own row is the workspace (a user
    link). A changed name follows Career-OS."""
    name = (name or career_os_user_id).strip()[:MAX_NAME] or career_os_user_id
    async with user_tx(pool, account.user_id) as conn:
        row = await conn.fetchrow(
            "select id, name from app.profiles where career_os_user_id = $1", career_os_user_id
        )
        if row is not None and row["name"] != name:
            await conn.execute(
                "update app.profiles set name = $2, updated_at = now() where id = $1",
                row["id"], name,
            )  # fmt: skip
        person = row["id"] if row is not None else await person_of(conn, career_os_user_id)
        settings = await conn.fetchval(
            "select settings from app.users where id = $1", account.user_id
        )
    if row is not None:
        if row["name"] != name:
            await _rename(pool, row["id"], name)
        return row["id"]
    if person is None:
        made = uuid7()
        async with user_tx(pool, made) as conn:
            await conn.execute(
                "insert into app.users (id, email, display_name, settings, owner_id)"
                " values ($1, $2, $3, $4, $5)",
                made, f"profile-{made}@profiles.invalid", name, settings or {}, account.user_id,
            )  # fmt: skip
            person = await claim(conn, career_os_user_id, made)
        if person != made:  # another account made it at the same time: use that one
            async with user_tx(pool, made) as conn:
                await conn.execute("delete from app.users where id = $1", made)
    if person == account.user_id:
        return person
    async with user_tx(pool, account.user_id) as conn:
        await conn.execute(
            "insert into app.profiles (id, user_id, career_os_user_id, name)"
            " values ($1, $2, $3, $4) on conflict (user_id, career_os_user_id) do nothing",
            person, account.user_id, career_os_user_id, name,
        )  # fmt: skip
        got = await conn.fetchval(
            "select id from app.profiles where career_os_user_id = $1", career_os_user_id
        )
    return got


async def _rename(pool: asyncpg.Pool, person: UUID, name: str) -> None:
    """A profile row's display name follows Career-OS; an account's own row keeps its own."""
    async with user_tx(pool, person) as conn:
        await conn.execute(
            "update app.users set display_name = $2 where id = $1 and owner_id is not null",
            person, name,
        )  # fmt: skip


async def sync_settings(pool: asyncpg.Pool, account: Principal, settings: dict[str, Any]) -> None:
    """Copy the account's settings (retention, consent, standup time) to the profiles it owns:
    their users rows carry them for the services that read a user's settings. A shared
    workspace another account owns keeps its owner's."""
    for p in await listed(pool, account):
        async with user_tx(pool, UUID(p["id"])) as conn:
            await conn.execute(
                "update app.users set settings = $2 where id = $1 and owner_id = $3",
                UUID(p["id"]), settings, account.user_id,
            )  # fmt: skip
