"""`GET /profiles`: the people the account works for (ARCHITECTURE.md, "Profiles").

"Me" (the account) first, then a profile for each Career-OS user the account's Career-OS
admin link reads, by name; the desktop app's switch lists them and names the one it acts for
in `x-copilot-profile`. A Career-OS user seen here for the first time gets its profile now. A
user no longer linked to the admin in Career-OS drops off the list (their profile's data stays,
and comes back if they are linked again). When Career-OS cannot be reached, the profiles made
so far are listed.
"""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.data_cache import DataCacheRoute
from copilot_api.deps import AccountDep, SvcDep, UserDep
from copilot_api.routes.career_os import PROVIDER, kind_of, obj, token_of, user_out
from copilot_core import profiles
from copilot_core.careeros import CareerOsError
from copilot_core.languages import CALL_LANGUAGES, recognition_supported, validate_language

router = APIRouter(route_class=DataCacheRoute)


class CallPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spoken_language: str = Field(min_length=2, max_length=16)


@router.get("/call-languages")
async def call_languages(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    return {
        "items": [
            {
                "id": code,
                "name": language.name,
                "native_name": language.native_name,
                "direction": language.direction,
            }
            for code, language in CALL_LANGUAGES.items()
            if code in svc.settings.verified_call_languages
            and recognition_supported(code, svc.settings.deepgram_model, svc.settings.stt_providers)
        ]
    }


@router.get("/profile/call-preferences")
async def call_preferences(svc: SvcDep, user: UserDep) -> dict[str, str]:
    async with svc.tx(user) as conn:
        language = await conn.fetchval(
            "select call_language from app.workspaces where id = $1", user.user_id
        )
    return {"spoken_language": language or "en"}


@router.patch("/profile/call-preferences")
async def save_call_preferences(body: CallPreference, svc: SvcDep, user: UserDep) -> dict[str, str]:
    try:
        validate_language(body.spoken_language, svc.settings.verified_call_languages)
        if not recognition_supported(
            body.spoken_language, svc.settings.deepgram_model, svc.settings.stt_providers
        ):
            raise ValueError(
                "Selected language is unavailable on the configured recognition paths."
            )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    async with svc.tx(user) as conn:
        await conn.execute(
            "update app.workspaces set call_language = $2 where id = $1",
            user.user_id,
            body.spoken_language,
        )
    return {"spoken_language": body.spoken_language}


@router.get("/profiles")
async def list_profiles(
    svc: SvcDep,
    account: AccountDep,
    response: Response,
    x_copilot_profile: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    result = await _list_profiles(svc, account)
    # Discovery can establish a shared workspace. Bind the requested selection after
    # those writes, but still return the chooser if that selection no longer exists.
    response.headers["X-Copilot-Account"] = str(account.user_id)
    try:
        canonical = await profiles.resolve(svc.pool, account, x_copilot_profile)
    except profiles.ProfileError:
        pass
    else:
        response.headers["X-Copilot-Workspace"] = str(canonical.user_id)
    response.headers["Cache-Control"] = "private, no-store"
    return result


async def _list_profiles(svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        link = await conn.fetchrow(
            "select status, external_id, external_kind from app.integrations where provider = $1",
            PROVIDER,
        )
        name = await conn.fetchval(
            "select display_name from app.accounts where id = $1", account.user_id
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
