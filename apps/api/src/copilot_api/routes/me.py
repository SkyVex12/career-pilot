"""`GET /me`, `PATCH /me`, `PATCH /me/settings`, `DELETE /me`, `GET /usage`."""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from copilot_api.deps import SvcDep, UserDep

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
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    settings: SettingsPatch | None = None


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
async def get_me(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(ME_SQL, user.user_id)
    if row is None:
        raise HTTPException(404, "no such user")
    return _me(row)


async def _patch(svc: SvcDep, user: UserDep, body: MePatch) -> dict[str, Any]:
    changes = body.settings.model_dump(exclude_unset=True) if body.settings else {}
    async with svc.tx(user) as conn:
        if body.display_name is not None:
            await conn.execute(
                "update app.users set display_name = $2 where id = $1",
                user.user_id,
                body.display_name,
            )
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
    return _me(row)


@router.patch("/me")
async def patch_me(body: MePatch, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    return await _patch(svc, user, body)


@router.patch("/me/settings")
async def patch_settings(body: SettingsPatch, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    return await _patch(svc, user, MePatch(settings=body))


@router.delete("/me", status_code=202)
async def delete_me(svc: SvcDep, user: UserDep) -> dict[str, str]:
    """Account deletion runs as the `user.delete` job (rows, files, data keys)."""
    await svc.enqueue("user.delete", f"user.delete:{user.user_id}", user.user_id, {})
    return {"status": "deleting"}


@router.get("/usage")
async def usage(svc: SvcDep, user: UserDep, month: str | None = None) -> dict[str, Any]:
    """Live minutes this calendar month (UTC), from the sessions' billed seconds."""
    try:
        first = (
            date.fromisoformat(f"{month}-01") if month else datetime.now(UTC).date().replace(day=1)
        )
    except ValueError as e:
        raise HTTPException(422, "month must be YYYY-MM") from e
    nxt = (first + timedelta(days=32)).replace(day=1)
    start = datetime.combine(first, time(), UTC)
    end = datetime.combine(nxt, time(), UTC)
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select (started_at at time zone 'UTC')::date as day, count(*) as sessions,"
            " sum(billed_seconds)::int as seconds from app.sessions"
            " where user_id = $1 and started_at >= $2 and started_at < $3"
            " and ended_at is not null group by 1 order by 1",
            user.user_id,
            start,
            end,
        )
    seconds = sum(int(r["seconds"] or 0) for r in rows)
    return {
        "period": first.strftime("%Y-%m"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "live_seconds": seconds,
        "live_minutes": -(-seconds // 60),
        "sessions": sum(int(r["sessions"]) for r in rows),
        "days": [
            {"day": r["day"].isoformat(), "live_seconds": int(r["seconds"] or 0)} for r in rows
        ],
    }
