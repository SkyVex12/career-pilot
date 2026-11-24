"""`GET /me`, `PATCH /me`, `PATCH /me/settings`, `DELETE /me`, `GET /usage`.

All of them are the signed-in account's, whichever profile the app has switched to
(ARCHITECTURE.md, "Profiles"): preferences belong to the account, measured usage belongs to
its payer, and deletion transfers shared workspaces before removing private account data."""

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from copilot_api.deps import AccountDep, SvcDep
from copilot_core import profiles
from copilot_core import usage as usage_ledger
from copilot_core.auth import CAREER_OS, Principal

router = APIRouter()


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store_transcript: bool | None = None


class SettingsPatch(BaseModel):
    """Account settings (ARCHITECTURE.md, `accounts.settings`): retention days, standup time,
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


ME_SQL = (
    "select id, email, display_name, plan, settings, created_at from app.accounts where id = $1"
)


@router.get("/me")
async def get_me(svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    """The account, with the ways it signs in (`sign_in_methods`: Google, an emailed code,
    Career-OS). Settings shows them; Career-OS cannot be disconnected while it is the only one
    (`DELETE /career-os/connection`)."""
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(ME_SQL, user.user_id)
        methods = await conn.fetch(
            "select provider, email, created_at from app.identities order by created_at, provider"
        )
        career_os = await conn.fetchval(
            "select external_label from app.integrations where provider = $1", CAREER_OS
        )
    if row is None:
        raise HTTPException(404, "no such user")
    out = _me(row)
    out["sign_in_methods"] = [
        {
            "provider": m["provider"],
            # Whose sign-in it is: the address Google or the emailed code verified, or the
            # linked Career-OS account's name.
            "label": (career_os or "") if m["provider"] == CAREER_OS else m["email"],
            "added_at": m["created_at"].isoformat(),
        }
        for m in methods
    ]
    return out


@router.patch("/me")
async def patch_me(body: MePatch, svc: SvcDep, user: AccountDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        if body.display_name is not None:
            await conn.execute(
                "update app.accounts set display_name = $2 where id = $1",
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
                await conn.fetchval("select settings from app.accounts where id = $1", user.user_id)
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
                "update app.accounts set settings = $2 where id = $1", user.user_id, current
            )
        row = await conn.fetchrow(ME_SQL, user.user_id)
    return _me(row)


@router.delete("/me", status_code=202)
async def delete_me(svc: SvcDep, user: AccountDep) -> dict[str, str]:
    """Account deletion runs as the `user.delete` job (rows, files, data keys, profiles)."""
    await svc.enqueue("user.delete", f"user.delete:{user.user_id}", user.user_id, {})
    return {"status": "deleting"}


@router.get("/usage")
async def usage(
    svc: SvcDep, user: AccountDep, response: Response, month: str | None = None
) -> dict[str, Any]:
    """Durable connected usage owned by the signed-in payer, plus nonbillable history.

    Old clients retain the combined live_seconds/live_minutes display. Only measured
    intervals contribute to price estimates; legacy wall-clock durations never do.
    """
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    as_of = datetime.now(UTC)
    try:
        if month is not None and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) is None:
            raise ValueError("invalid month format")
        first = (
            date.fromisoformat(f"{month}-01") if month is not None else as_of.date().replace(day=1)
        )
        nxt = (first + timedelta(days=32)).replace(day=1)
    except (ValueError, OverflowError) as e:
        raise HTTPException(422, "month must be YYYY-MM") from e
    start = datetime.combine(first, time(), UTC)
    end = datetime.combine(nxt, time(), UTC)
    async with svc.tx(user, isolation="repeatable_read") as conn:
        measured = await usage_ledger.usage_summary(
            conn, payer_account_id=user.user_id, started_at=start, ended_at=end
        )
    who = [user] + [
        Principal(UUID(p["id"]), p["name"], user.expires_at)
        for p in await profiles.listed(svc.pool, user)
    ]
    legacy_us = legacy_sessions = 0
    by_day: dict[date, list[int]] = {}
    for day in measured.days:
        by_day[day.day] = [day.connected_microseconds, 0]
    for principal in who:
        async with svc.tx(principal) as conn:
            legacy = await usage_ledger.legacy_summary(
                conn, workspace_id=principal.user_id, started_at=start, ended_at=end
            )
        legacy_us += legacy.estimated_microseconds
        legacy_sessions += legacy.session_count
        for day in legacy.days:
            by_day.setdefault(day.day, [0, 0])[1] += day.estimated_microseconds
    total_us = measured.connected_microseconds + legacy_us
    return {
        "period": first.strftime("%Y-%m"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "as_of": as_of.isoformat(),
        "last_recorded_at": measured.last_recorded_at.isoformat()
        if measured.last_recorded_at is not None
        else None,
        "tracking_only": True,
        "measurement": "connected-v1",
        "live_seconds": total_us // 1_000_000,
        "live_minutes": -(-total_us // 60_000_000),
        "sessions": measured.session_count + legacy_sessions,
        "measured_seconds": measured.connected_microseconds / 1_000_000,
        "measured_sessions": measured.session_count,
        "legacy_estimated_seconds": legacy_us / 1_000_000,
        "legacy_sessions": legacy_sessions,
        "legacy_billable": False,
        "estimated_costs": [
            {
                "currency": cost.currency,
                "estimated_minor": cost.estimated_minor,
                "adjustment_minor": cost.adjustment_minor,
            }
            for cost in measured.costs
        ],
        "prices": [
            {
                "currency": price.currency,
                "price_version": price.price_version,
                "rate_minor_per_hour": price.rate_minor_per_hour,
                "measured_seconds": price.connected_microseconds / 1_000_000,
            }
            for price in measured.prices
        ],
        "days": [
            {
                "day": day.isoformat(),
                "live_seconds": (values[0] + values[1]) // 1_000_000,
                "measured_seconds": values[0] / 1_000_000,
                "legacy_estimated_seconds": values[1] / 1_000_000,
            }
            for day, values in sorted(by_day.items())
        ],
    }
