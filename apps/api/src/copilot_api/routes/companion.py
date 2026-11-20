"""Same-account display attachments. These routes never claim a capture lease or start STT."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import AccountDep, Services, SvcDep, UserDep
from copilot_core import companions, keys
from copilot_core.auth import Principal
from copilot_core.ids import uuid7
from copilot_core.valkey_client import text

router = APIRouter()


class Attach(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str = Field(min_length=1, max_length=100)


async def live(svc: Services, account: Principal, user: Principal, sid: UUID) -> dict[str, str]:
    raw = await svc.valkey.hgetall(keys.sess_state(sid))
    state = {text(key): text(value) for key, value in raw.items()}
    if (
        state.get("account_id") != str(account.user_id)
        or state.get("user_id") != str(user.user_id)
        or state.get("private_context_account_id") not in (None, "", str(account.user_id))
    ):
        raise HTTPException(404, "No desktop call for this account and profile")
    if state.get("status") in (None, "ended"):
        raise HTTPException(409, "The desktop call has ended")
    if state.get("platform") == "mobile_in_person" or state.get("client_version", "").startswith(
        "mobile-"
    ):
        raise HTTPException(409, "Companion mode requires a desktop capture session")
    return state


@router.get("/companion/sessions")
async def current_sessions(svc: SvcDep, account: AccountDep, user: UserDep) -> dict[str, Any]:
    active = await svc.valkey.get(keys.user_active_session(user.user_id))
    if not active:
        return {"items": []}
    try:
        sid = UUID(text(active))
        state = await live(svc, account, user, sid)
    except (HTTPException, ValueError):
        return {"items": []}
    return {
        "items": [
            {
                "session_id": str(sid),
                "profile_id": str(user.user_id),
                "title": state.get("title") or "Live call",
                "mode": state.get("mode", "interview"),
                "spoken_language": state.get("spoken_language") or None,
                "audio_source": "desktop",
                "capture_connected": state.get("status") == "active",
                "status": state.get("status"),
                "platform": state.get("platform", "unknown"),
            }
        ]
    }


@router.post("/companion/sessions/{session_id}/attach")
async def attach(
    session_id: UUID, body: Attach, svc: SvcDep, account: AccountDep, user: UserDep
) -> dict[str, Any]:
    state = await live(svc, account, user, session_id)
    expires = datetime.now(UTC) + timedelta(seconds=companions.TTL_S)
    async with svc.tx(account) as conn:
        # Serialize the account's attach requests to enforce the display bound.
        await conn.execute(
            "select pg_advisory_xact_lock(hashtextextended($1,0))", str(account.user_id)
        )
        count = await conn.fetchval(
            "select count(*) from app.companion_attachments where session_id=$1"
            " and revoked_at is null and expires_at>now()",
            session_id,
        )
        if count >= companions.MAX_DISPLAYS:
            raise HTTPException(409, "Disconnect an existing companion before adding another")
        aid = uuid7()
        await conn.execute(
            "insert into app.companion_attachments"
            "(id,user_id,profile_id,session_id,device_name,expires_at) values($1,$2,$3,$4,$5,$6)",
            aid,
            account.user_id,
            user.user_id,
            session_id,
            body.device_name,
            expires,
        )
        record = companions.Attachment(
            str(aid),
            str(account.user_id),
            str(user.user_id),
            str(session_id),
            body.device_name,
            expires.timestamp(),
        )
        await companions.publish(svc.valkey, record)
    return {
        **asdict(record),
        "audio_source": "desktop",
        "spoken_language": state.get("spoken_language") or None,
    }


@router.get("/companion/attachments")
async def attachments(svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            "select id,profile_id,session_id,device_name,expires_at,revoked_at"
            " from app.companion_attachments order by created_at desc limit 100"
        )
    return {
        "items": [
            {
                "attachment_id": str(row["id"]),
                "profile_id": str(row["profile_id"]),
                "session_id": str(row["session_id"]),
                "device_name": row["device_name"],
                "expires_at": row["expires_at"],
                "revoked": row["revoked_at"] is not None,
            }
            for row in rows
        ]
    }


async def revoke(svc: Services, account: Principal, attachment_id: UUID | None) -> int:
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            "select id,session_id from app.companion_attachments"
            " where ($1::uuid is null or id=$1) for update",
            attachment_id,
        )
        if attachment_id is not None and not rows:
            raise HTTPException(404, "No such companion attachment")
        # Invalidate the live mirror before committing revocation. A mirror failure leaves
        # the DB grant unchanged and returns failure, so the user can retry honestly.
        for row in rows:
            await svc.valkey.hdel(companions.records_key(row["session_id"]), str(row["id"]))
        await conn.execute(
            "update app.companion_attachments set revoked_at=now()"
            " where ($1::uuid is null or id=$1)",
            attachment_id,
        )
    return len(rows)


@router.delete("/companion/attachments/{attachment_id}")
async def disconnect(attachment_id: UUID, svc: SvcDep, account: AccountDep) -> dict[str, int]:
    return {"revoked": await revoke(svc, account, attachment_id)}


@router.delete("/companion/attachments")
async def disconnect_all(svc: SvcDep, account: AccountDep) -> dict[str, int]:
    return {"revoked": await revoke(svc, account, None)}
