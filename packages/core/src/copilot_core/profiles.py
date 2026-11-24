"""Workspace selection backed by explicit account memberships.

Profile remains the client term for a Career-OS workspace. Memberships authorize
access; profile rows only retain external-listing metadata. Stable workspace UUIDs
preserve encrypted content, source references and existing clients' selections.
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
    pass


async def person_of(conn: Conn, career_os_user_id: str) -> UUID | None:
    return await conn.fetchval(
        "select workspace_id from app.external_workspace_links"
        " where provider='career_os' and realm='default' and external_id=$1",
        career_os_user_id,
    )


async def claim(conn: Conn, career_os_user_id: str, user_id: UUID) -> UUID:
    """Trusted remote authorization has established this actor's external grant."""
    result = await conn.fetchval(
        "select app.provision_external_workspace($1,$2,$3)",
        career_os_user_id,
        user_id,
        career_os_user_id,
    )
    assert isinstance(result, UUID)
    return result


async def me_of(pool: asyncpg.Pool, account: Principal) -> Principal:
    async with user_tx(pool, account.user_id, actor_account_id=account.user_id) as conn:
        link = await conn.fetchrow(
            "select external_id,external_kind from app.integrations"
            " where provider='career_os' and status='active'"
        )
        person = account.user_id
        if link is not None and link["external_kind"] == "user" and link["external_id"]:
            person = await person_of(conn, str(link["external_id"])) or person
        if not await conn.fetchval("select app.has_workspace_access($1)", person):
            raise ProfileError("workspace membership is inactive")
    if person == account.user_id:
        return account
    return Principal(person, account.display_name, account.expires_at, email=None)


async def claim_me(pool: asyncpg.Pool, account: Principal, career_os_user_id: str) -> None:
    async with user_tx(pool, account.user_id, actor_account_id=account.user_id) as conn:
        await conn.fetchval(
            "select app.provision_external_workspace($1,$2,$3)",
            career_os_user_id,
            account.user_id,
            account.display_name,
        )


async def resolve(pool: asyncpg.Pool, account: Principal, profile: str | None) -> Principal:
    if not profile or profile.strip() in ("", "me", str(account.user_id)):
        return await me_of(pool, account)
    try:
        pid = UUID(profile.strip())
    except ValueError as e:
        raise ProfileError("not a profile id") from e
    async with user_tx(pool, account.user_id, actor_account_id=account.user_id) as conn:
        name = await conn.fetchval("select name from app.account_workspaces() where id=$1", pid)
    if name is None:
        raise ProfileError("no active workspace membership for this account")
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
    async with user_tx(pool, account.user_id, actor_account_id=account.user_id) as conn:
        rows = await conn.fetch(
            "select * from app.account_workspaces() where id<>$1", account.user_id
        )
    return [profile_out(r) for r in rows]


async def ensure(pool: asyncpg.Pool, account: Principal, career_os_user_id: str, name: str) -> UUID:
    """Provision membership after the API verifies the remote user's visibility."""
    name = (name or career_os_user_id).strip()[:MAX_NAME] or career_os_user_id
    async with user_tx(pool, account.user_id, actor_account_id=account.user_id) as conn:
        person = await conn.fetchval(
            "select app.provision_external_workspace($1,$2,$3)",
            career_os_user_id,
            uuid7(),
            name,
        )
        assert isinstance(person, UUID)
        if person != account.user_id:
            await conn.execute(
                "insert into app.profiles(id,user_id,career_os_user_id,name) values($1,$2,$3,$4)"
                " on conflict(user_id,career_os_user_id) do update set"
                " name=excluded.name,updated_at=clock_timestamp()",
                person,
                account.user_id,
                career_os_user_id,
                name,
            )
    if person != account.user_id:
        async with user_tx(pool, person, actor_account_id=account.user_id) as conn:
            await conn.execute(
                "update app.workspaces set display_name=$2 where id=$1 and kind='career'",
                person,
                name,
            )
    return person
