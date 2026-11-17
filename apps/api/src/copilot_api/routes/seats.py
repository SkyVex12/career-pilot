"""Guest seats on a live call (ARCHITECTURE.md, "Guest seats").

The host (the profile the call runs for, `UserDep`) invites another Copilot account by email,
changes what they may do, and removes them, all while the call is live. The invite is the
host's permission: the seat is admitted at once, and the guest (always an account,
`AccountDep`) sees it with Join, with nothing to accept and no one to wait for (until
2026-11-17 the guest accepted and the host then let them in; `accept`, `decline` and
`admit` stay for seats from then). After the call a guest has no access to anything of it:
no History, no transcript; only the host's own session routes show it.

Every change is written to `app.session_seats` first and then mirrored into Valkey
(`copilot_core.seats.publish`), which is what the gateway checks; the mirror also tells the
host's session to send the host the new list.

An invite answers the same whether or not the address has an account, so the route does not
say who uses Copilot; the host's own seat list then shows the invite only if there is one.
Invites are limited per call and per hour.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from valkey.exceptions import ValkeyError

from copilot_api.deps import AccountDep, Services, SvcDep, UserDep
from copilot_core import keys, livekit, seats
from copilot_core.auth import Principal
from copilot_core.ids import uuid7
from copilot_core.valkey_client import text

log = logging.getLogger(__name__)
router = APIRouter()


def _caps(value: list[str]) -> list[str]:
    unknown = [c for c in value if c not in seats.GRANTABLE]
    if unknown:
        raise ValueError(f"not grantable now: {', '.join(unknown)}")
    return ["view", *sorted({c for c in value if c != "view"})]


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    capabilities: list[str] = Field(default_factory=lambda: ["view", "note"], max_length=8)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("not an email address")
        return value

    @field_validator("capabilities")
    @classmethod
    def _grantable(cls, value: list[str]) -> list[str]:
        return _caps(value)


class SeatPatch(BaseModel):
    admit: Literal[True] | None = None
    capabilities: list[str] | None = Field(default=None, max_length=8)

    @field_validator("capabilities")
    @classmethod
    def _grantable(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _caps(value)


SEAT_COLUMNS = (
    "id, user_id, session_id, guest_id, host_name, guest_name, guest_email, mode, title,"
    " status, capabilities, rev, created_at, expires_at, accepted_at, admitted_at"
)


def _status(row: Any) -> str:
    if row["status"] in seats.OPEN and row["expires_at"] <= datetime.now(UTC):
        return "expired"
    return row["status"]


def _record(row: Any, **over: Any) -> seats.SeatRecord:
    fields: dict[str, Any] = {
        "seat_id": str(row["id"]),
        "guest_id": str(row["guest_id"]),
        "name": row["guest_name"],
        "status": row["status"],
        "capabilities": list(row["capabilities"]),
        "rev": row["rev"],
        "host_name": row["host_name"],
        "expires_at": row["expires_at"].timestamp(),
    }
    fields.update(over)
    return seats.SeatRecord(**fields)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _host_view(row: Any) -> dict[str, Any]:
    return {
        "seat_id": str(row["id"]),
        "name": row["guest_name"],
        "email": row["guest_email"],
        "status": _status(row),
        "capabilities": list(row["capabilities"]),
        "created_at": _iso(row["created_at"]),
        "accepted_at": _iso(row["accepted_at"]),
        "admitted_at": _iso(row["admitted_at"]),
    }


async def _live_state(svc: Services, sid: UUID) -> dict[str, str]:
    raw = await svc.valkey.hgetall(keys.sess_state(sid))
    return {text(k): text(v) for k, v in raw.items()}


async def _hosts_live_call(svc: Services, user: Principal, sid: UUID) -> dict[str, str]:
    """The gateway's state of this profile's call, which must still be live."""
    state = await _live_state(svc, sid)
    if state.get("user_id") != str(user.user_id):
        raise HTTPException(404, "no such call")
    if state.get("status") in (None, "ended"):
        raise HTTPException(409, "the call is not live")
    return state


async def _publish(svc: Services, sid: UUID | str, record: seats.SeatRecord) -> None:
    """Mirror the change for the gateway. It is recorded already; if Valkey cannot take it,
    say so (503). Every host change can be sent again and writes the mirror again (an admit
    or a removal that is already done is not refused); until then the gateway keeps the last
    grant it saw, and a seat it never saw admitted cannot join."""
    try:
        await seats.publish(svc.valkey, sid, record, seats.MIRROR_TTL_S)
    except (OSError, ValkeyError) as e:
        raise HTTPException(503, "saved, but not yet live; try again") from e


async def _invite_allowed(svc: Services, account: Principal) -> bool:
    key = keys.rate_limit(account.user_id, f"seat_invite:{int(time.time() // 3600)}")
    async with svc.valkey.pipeline(transaction=False) as pipe:
        pipe.incr(key)
        pipe.expire(key, 3600)
        count, _ = await pipe.execute()
    return int(count) <= seats.INVITES_PER_HOUR


def _voice_ready(svc: Services, capabilities: list[str] | None) -> None:
    """Voice can be granted only where LiveKit is set up."""
    if capabilities and "voice" in capabilities and svc.settings.livekit() is None:
        raise HTTPException(422, "Voice is not set up on this server.")


async def _drop_voice(svc: Services, session_id: UUID, seat_id: UUID) -> None:
    """Take a guest out of the call's voice room now (best effort: their seat's mirror says no
    voice too, and the host's app plays no one without it)."""
    lk = svc.settings.livekit()
    if lk is None:
        return
    try:
        await lk.remove(livekit.room_of(session_id), livekit.guest_identity(seat_id))
    except livekit.LiveKitError as e:
        log.warning("seat %s: voice not removed from LiveKit now: %s", seat_id, e)


def _voice(
    lk: livekit.LiveKit, session_id: UUID, identity: str, name: str, guest: bool
) -> dict[str, Any]:
    room = livekit.room_of(session_id)
    token = lk.token(room, identity, name, publish_mic=guest, subscribe=not guest)
    return {"url": lk.url, "room": room, "identity": identity, "token": token,
            "expires_in": livekit.TOKEN_TTL_S}  # fmt: skip


# The host.


@router.post("/sessions/{session_id}/seats", status_code=202)
async def invite(
    session_id: UUID, body: InviteBody, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, str]:
    state = await _hosts_live_call(svc, user, session_id)
    _voice_ready(svc, body.capabilities)
    if not await _invite_allowed(svc, account):
        raise HTTPException(429, "too many invites; wait a while")
    async with svc.tx(user) as conn:
        open_count = await conn.fetchval(
            "select count(*) from app.session_seats where session_id = $1"
            " and status = any($2::text[]) and expires_at > now()",
            session_id,
            list(seats.OPEN),
        )
        admitted = await conn.fetchval(
            "select count(*) from app.session_seats where session_id = $1"
            " and status = 'admitted' and expires_at > now()",
            session_id,
        )
    if open_count >= seats.MAX_OPEN:
        raise HTTPException(409, f"a call has at most {seats.MAX_OPEN} open invites")
    if admitted >= seats.MAX_GUESTS:
        raise HTTPException(409, f"a call has at most {seats.MAX_GUESTS} guests")
    async with svc.tx(account) as conn:
        host_name = await conn.fetchval(
            "select display_name from app.users where id = $1", account.user_id
        )
        await conn.execute("select set_config('app.seat_lookup', $1, true)", body.email)
        guest = await conn.fetchrow(
            "select id, display_name from app.users where lower(email) = $1 and owner_id is null",
            body.email,
        )
    sent = {"status": "sent"}
    if guest is None or guest["id"] in (account.user_id, user.user_id):
        return sent  # the same answer: the route does not say who has an account
    now = datetime.now(UTC)
    seat_id = uuid7()
    mode = state.get("mode") if state.get("mode") in ("interview", "standup", "meeting") else None
    try:
        async with svc.tx(user) as conn:
            row = await conn.fetchrow(
                "insert into app.session_seats (id, user_id, session_id, host_account_id,"
                " guest_id, host_name, guest_name, guest_email, mode, title, status,"
                " capabilities, expires_at, admitted_at)"
                " values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'admitted', $11, $12, $13)"
                f" returning {SEAT_COLUMNS}",
                seat_id, user.user_id, session_id, account.user_id, guest["id"],
                (host_name or "Your host")[:200], (guest["display_name"] or body.email)[:200],
                body.email, mode or "interview", (state.get("title") or None),
                body.capabilities, now + timedelta(seconds=seats.SEAT_MAX_S), now,
            )  # fmt: skip
    except asyncpg.UniqueViolationError:
        return sent  # already invited to this call
    assert row is not None
    await _publish(svc, session_id, _record(row))
    return sent


@router.get("/sessions/{session_id}/seats")
async def list_seats(session_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            f"select {SEAT_COLUMNS} from app.session_seats where session_id = $1"
            " and user_id = $2 order by created_at",
            session_id,
            user.user_id,
        )
    return {"seats": [_host_view(r) for r in rows]}


@router.patch("/sessions/{session_id}/seats/{seat_id}")
async def change_seat(
    session_id: UUID, seat_id: UUID, body: SeatPatch, svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    await _hosts_live_call(svc, user, session_id)
    _voice_ready(svc, body.capabilities)
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            f"select {SEAT_COLUMNS} from app.session_seats where id = $1 and session_id = $2"
            " and user_id = $3 for update",
            seat_id, session_id, user.user_id,
        )  # fmt: skip
        if row is None:
            raise HTTPException(404, "no such seat")
        had_voice = "voice" in row["capabilities"]
        status = _status(row)
        if status not in seats.OPEN:
            raise HTTPException(409, f"the seat is {status}")
        sets: list[str] = ["rev = rev + 1"]
        args: list[Any] = [seat_id]
        if body.admit and status != "admitted":  # admitted already: a retry, mirrored again
            if status != "accepted":
                raise HTTPException(409, "only a guest who accepted can be admitted")
            admitted = await conn.fetchval(
                "select count(*) from app.session_seats where session_id = $1"
                " and status = 'admitted' and expires_at > now()",
                session_id,
            )
            if admitted >= seats.MAX_GUESTS:
                raise HTTPException(409, f"a call has at most {seats.MAX_GUESTS} guests")
            args.append(row["created_at"] + timedelta(seconds=seats.SEAT_MAX_S))
            sets += ["status = 'admitted'", "admitted_at = now()", f"expires_at = ${len(args)}"]
        if body.capabilities is not None:
            args.append(body.capabilities)
            sets.append(f"capabilities = ${len(args)}")
        row = await conn.fetchrow(
            f"update app.session_seats set {', '.join(sets)} where id = $1"
            f" returning {SEAT_COLUMNS}",
            *args,
        )
    assert row is not None
    await _publish(svc, session_id, _record(row))
    if had_voice and "voice" not in row["capabilities"]:
        await _drop_voice(svc, session_id, seat_id)
    return _host_view(row)


@router.delete("/sessions/{session_id}/seats/{seat_id}", status_code=204)
async def remove_seat(session_id: UUID, seat_id: UUID, svc: SvcDep, user: UserDep) -> Response:
    """Remove a guest (or withdraw an invite). A connected guest is closed within a second."""
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "update app.session_seats set status = 'removed', ended_at = now(), rev = rev + 1"
            " where id = $1 and session_id = $2 and user_id = $3"
            " and status = any($4::text[])"
            f" returning {SEAT_COLUMNS}",
            seat_id, session_id, user.user_id, list(seats.OPEN),
        )  # fmt: skip
        if row is None:
            # Removed already: a retry after the mirror could not be written. Write it again,
            # so a removal always reaches the gateway once it is recorded.
            row = await conn.fetchrow(
                f"select {SEAT_COLUMNS} from app.session_seats where id = $1 and session_id = $2"
                " and user_id = $3 and status = 'removed'",
                seat_id, session_id, user.user_id,
            )  # fmt: skip
            if row is None:
                raise HTTPException(404, "no open seat with that id")
    await _publish(svc, session_id, _record(row))
    await _drop_voice(svc, session_id, seat_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/voice")
async def host_voice(session_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    """The host's way into the call's voice room: listen only. 404 when voice is not set up."""
    await _hosts_live_call(svc, user, session_id)
    lk = svc.settings.livekit()
    if lk is None:
        raise HTTPException(404, "Voice is not set up on this server.")
    return _voice(lk, session_id, livekit.HOST, "Host", guest=False)


# The guest.


@router.get("/seats")
async def my_seats(svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    """The account's open seats on calls that are still live (the app polls this only while
    the stage is awake, and the VM's idle check does not count it)."""
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            f"select {SEAT_COLUMNS} from app.session_seats where guest_id = $1"
            " and status = any($2::text[]) and expires_at > now() order by created_at",
            account.user_id,
            list(seats.OPEN),
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        state = await _live_state(svc, r["session_id"])
        if state.get("user_id") != str(r["user_id"]) or state.get("status") in (None, "ended"):
            continue
        out.append(
            {
                "seat_id": str(r["id"]),
                "session_id": str(r["session_id"]),
                "host_name": r["host_name"],
                "mode": r["mode"],
                "title": r["title"],
                "status": r["status"],
                "capabilities": list(r["capabilities"]),
                "expires_at": _iso(r["expires_at"]),
            }
        )
    return {"seats": out}


async def _answer(svc: Services, account: Principal, seat_id: UUID, accept: bool) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        row = await conn.fetchrow(
            f"select {SEAT_COLUMNS} from app.session_seats where id = $1 and guest_id = $2"
            " and status = 'invited' and expires_at > now()",
            seat_id,
            account.user_id,
        )
        if row is None:
            raise HTTPException(404, "no open invite with that id")
        state = await _live_state(svc, row["session_id"])
        if state.get("user_id") != str(row["user_id"]) or state.get("status") in (None, "ended"):
            raise HTTPException(409, "the call is not live")
        sid = await conn.fetchval("select app.seat_answer($1, $2)", seat_id, accept)
        if sid is None:
            raise HTTPException(404, "no open invite with that id")
    status = "accepted" if accept else "declined"
    await _publish(svc, sid, _record(row, status=status, rev=row["rev"] + 1))
    return {"seat_id": str(seat_id), "session_id": str(sid), "status": status}


@router.post("/seats/{seat_id}/voice")
async def guest_voice(seat_id: UUID, svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    """A guest's way into the call's voice room: their microphone only, to the host. Only for
    an admitted seat with voice, on a call that is live."""
    lk = svc.settings.livekit()
    if lk is None:
        raise HTTPException(404, "Voice is not set up on this server.")
    async with svc.tx(account) as conn:
        row = await conn.fetchrow(
            f"select {SEAT_COLUMNS} from app.session_seats where id = $1 and guest_id = $2"
            " and status = 'admitted' and expires_at > now()",
            seat_id,
            account.user_id,
        )
    if row is None:
        raise HTTPException(404, "no open seat with that id")
    if "voice" not in row["capabilities"]:
        raise HTTPException(403, "The host has not turned on your voice.")
    state = await _live_state(svc, row["session_id"])
    if state.get("user_id") != str(row["user_id"]) or state.get("status") in (None, "ended"):
        raise HTTPException(409, "the call is not live")
    return _voice(
        lk, row["session_id"], livekit.guest_identity(seat_id), row["guest_name"], guest=True
    )


@router.post("/seats/{seat_id}/accept")
async def accept(seat_id: UUID, svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    return await _answer(svc, account, seat_id, True)


@router.post("/seats/{seat_id}/decline")
async def decline(seat_id: UUID, svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    return await _answer(svc, account, seat_id, False)
