"""`GET /me`, `PATCH /me`, `PATCH /me/settings`, `DELETE /me`, `GET /usage`.

All of them are the signed-in account's, whichever profile the app has switched to
(ARCHITECTURE.md, "Profiles"): settings are copied to the profiles, usage counts them too, and
deleting the account deletes its profiles."""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from copilot_api.deps import AccountDep, SvcDep
from copilot_core import profiles
from copilot_core.auth import Principal

router = APIRouter()


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_transcript: bool | None = None


class SettingsPatch(BaseModel):
    """User settings (ARCHITECTURE.md, `users.settings`): retention days, standup time,
    consent defaults. Fields left out stay as they are; null clears one."""

    model_config = ConfigDict(extra="forbid")
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    standup_time: str | None = None  # HH:MM in the user's timezone
    timezone: str | None = None  # IANA name, e.g. Europe/Berlin
    consent: Consent | None = None

    @field_validator("standup_time")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("standup_time must be HH:MM")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError(f"unknown timezone {v!r}") from e
        return v


class MePatch(BaseModel):
    """Profile fields only; settings go through `PATCH /me/settings`."""

    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


def _me(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "plan": row["plan"],
        "settings": row["settings"],
        "created_at": row["created_at"].isoformat(),
    }


ME_SQL = "select id, email, display_name, plan, settings, created_at from app.users where id = $1"


@router.get("/me")
async def get_me(svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(ME_SQL, user.user_id)
    if row is None:
        raise HTTPException(404, "no such user")
    return _me(row)


@router.patch("/me")
async def patch_me(body: MePatch, svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        if body.display_name is not None:
            await conn.execute(
                "update app.users set display_name = $2 where id = $1",
                user.user_id,
                body.display_name,
            )
        row = await conn.fetchrow(ME_SQL, user.user_id)
    return _me(row)


@router.patch("/me/settings")
async def patch_settings(body: SettingsPatch, svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    async with svc.tx(user) as conn:
        if changes:
            current: dict[str, Any] = dict(
                await conn.fetchval("select settings from app.users where id = $1", user.user_id)
                or {}
            )
            for key, value in changes.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key] = {**current[key], **value}
                elif value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            await conn.execute(
                "update app.users set settings = $2 where id = $1", user.user_id, current
            )
        row = await conn.fetchrow(ME_SQL, user.user_id)
    if changes and row is not None:
        settings = cast(dict[str, Any], row["settings"] or {})
        await profiles.sync_settings(svc.pool, user, settings)
    return _me(row)


@router.delete("/me", status_code=202)
async def delete_me(svc: SvcDep, user: AccountDep) -> dict[str, str]:
    """Account deletion runs as the `user.delete` job (rows, files, data keys, profiles)."""
    await svc.enqueue("user.delete", f"user.delete:{user.user_id}", user.user_id, {})
    return {"status": "deleting"}


@router.get("/usage")
async def usage(svc: SvcDep, user: AccountDep, month: str | None = None) -> dict[str, Any]:
    """Live minutes this calendar month (UTC), from the sessions' billed seconds: the
    account's and its profiles'."""
    try:
        first = (
            date.fromisoformat(f"{month}-01") if month else datetime.now(UTC).date().replace(day=1)
        )
    except ValueError as e:
        raise HTTPException(422, "month must be YYYY-MM") from e
    nxt = (first + timedelta(days=32)).replace(day=1)
    start = datetime.combine(first, time(), UTC)
    end = datetime.combine(nxt, time(), UTC)
    who = [user] + [
        Principal(UUID(p["id"]), p["name"], user.expires_at)
        for p in await profiles.listed(svc.pool, user)
    ]
    by_day: dict[date, list[int]] = {}
    for principal in who:
        async with svc.tx(principal) as conn:
            for r in await conn.fetch(
                "select (started_at at time zone 'UTC')::date as day, count(*) as sessions,"
                " sum(billed_seconds)::int as seconds from app.sessions"
                " where user_id = $1 and started_at >= $2 and started_at < $3"
                " and ended_at is not null group by 1",
                principal.user_id,
                start,
                end,
            ):
                day = by_day.setdefault(r["day"], [0, 0])
                day[0] += int(r["sessions"])
                day[1] += int(r["seconds"] or 0)
    days = sorted(by_day.items())
    seconds = sum(v[1] for _, v in days)
    return {
        "period": first.strftime("%Y-%m"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "live_seconds": seconds,
        "live_minutes": -(-seconds // 60),
        "sessions": sum(v[0] for _, v in days),
        "days": [{"day": d.isoformat(), "live_seconds": v[1]} for d, v in days],
    }
