"""`GET /profiles`: the people the account works for (ARCHITECTURE.md, "Profiles").

"Me" (the account) first, then a profile for each Career-OS user the account's Career-OS
admin link reads, by name; the desktop app's switch lists them and names the one it acts for
in `x-copilot-profile`. A Career-OS user seen here for the first time gets its profile now. A
user no longer linked to the admin in Career-OS drops off the list (their profile's data stays,
and comes back if they are linked again). When Career-OS cannot be reached, the profiles made
so far are listed.
"""

from typing import Any, cast

from fastapi import APIRouter

from copilot_api.deps import AccountDep, SvcDep
from copilot_api.routes.career_os import PROVIDER, kind_of, obj, token_of, user_out
from copilot_core import profiles
from copilot_core.careeros import CareerOsError

router = APIRouter()


@router.get("/profiles")
async def list_profiles(svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        link = await conn.fetchrow(
            "select status, external_id, external_kind from app.integrations where provider = $1",
            PROVIDER,
        )
        name = await conn.fetchval(
            "select display_name from app.users where id = $1", account.user_id
        )
    me = profiles.me_out(account)
    me["name"] = name or account.display_name
    kind = await kind_of(svc, account, link)
    if link is None or link["status"] != "active":
        return {"items": [me, *await profiles.listed(svc.pool, account)], "career_os": None}
    if kind == "user":
        me["career_os_user_id"] = link["external_id"]
        if link["external_id"]:
            await profiles.claim_me(svc.pool, account, str(link["external_id"]))
        return {"items": [me], "career_os": "user"}
    if svc.career_os is None:
        return {"items": [me, *await profiles.listed(svc.pool, account)], "career_os": kind}
    async with svc.tx(account) as conn:
        try:
            token = await token_of(svc, conn, account.user_id)
        except Exception:
            token = None
    if token is None:
        return {"items": [me, *await profiles.listed(svc.pool, account)], "career_os": kind}
    try:
        found = await svc.career_os.users(token)
    except CareerOsError:
        return {"items": [me, *await profiles.listed(svc.pool, account)], "career_os": kind}
    users = [user_out(obj(e)) for e in cast(list[object], found.get("items") or [])]
    for u in users:
        if u["id"]:
            await profiles.ensure(svc.pool, account, u["id"], u["name"])
    current = {u["id"] for u in users if u["id"]}
    listed = [
        p for p in await profiles.listed(svc.pool, account) if p["career_os_user_id"] in current
    ]
    return {"items": [me, *listed], "career_os": kind}
