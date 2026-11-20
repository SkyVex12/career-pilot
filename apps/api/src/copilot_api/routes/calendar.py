"""Account-owned, profile-scoped calendar consent and read-only meeting preparation."""

import hashlib
import json
import secrets
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx2
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import AccountDep, Services, SvcDep, UserDep
from copilot_core import calendars, profiles
from copilot_core.auth import Principal
from copilot_core.calendars import CalendarClient, CalendarError, Provider, ProviderConfig
from copilot_core.db import Conn, user_tx
from copilot_core.ids import uuid7
from copilot_core.meeting_preparation import (
    ApplicationContext,
    CalendarMeeting,
    PreviousMeeting,
    prepare_meetings,
)
from copilot_core.valkey_client import text

router = APIRouter()
TABLE = "calendar_connections"


def provider_http() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=10, follow_redirects=False)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calendar_ids: list[str] = Field(max_length=10)


def config_of(provider: Provider) -> ProviderConfig:
    config = calendars.configured(provider)
    if config is None:
        raise HTTPException(503, "This calendar provider has not been configured on the server")
    return config


async def connection(
    conn: Conn, provider: Provider, profile_id: UUID, *, lock: bool = False
) -> Any:
    row = await conn.fetchrow(
        "select * from app.calendar_connections where provider=$1 and profile_id=$2"
        + (" for update" if lock else ""),
        provider,
        profile_id,
    )
    if row is None:
        raise HTTPException(404, "Calendar is not connected for this profile")
    return row


async def cache_of(svc: Services, conn: Conn, account_id: UUID, row: Any) -> dict[str, Any]:
    if row["cache_enc"] is None:
        return {"calendars": [], "events": []}
    raw = await svc.crypto.decrypt(
        conn,
        account_id,
        TABLE + ":cache",
        str(row["id"]),
        bytes(row["cache_enc"]),
        int(row["cache_key_version"]),
    )
    return json.loads(raw)


async def token_of(svc: Services, conn: Conn, account_id: UUID, row: Any) -> dict[str, Any]:
    if row["token_enc"] is None:
        raise HTTPException(409, "Complete calendar authorization first")
    raw = await svc.crypto.decrypt(
        conn,
        account_id,
        TABLE + ":tokens",
        str(row["id"]),
        bytes(row["token_enc"]),
        int(row["token_key_version"]),
    )
    return json.loads(raw)


async def mark_failed(
    svc: Services,
    account: Principal,
    row_id: UUID,
    error: CalendarError,
    *,
    nonce: str | None = None,
    expected_updated_at: datetime | None = None,
) -> None:
    async with svc.tx(account) as conn:
        await conn.execute(
            "update app.calendar_connections set status=$2,last_error=$3,updated_at=now()"
            " where id=$1 and ($4::text is null or oauth_nonce=$4)"
            " and ($5::timestamptz is null or updated_at=$5)",
            row_id,
            "reauth_required" if error.reauthorize else "error",
            str(error),
            nonce,
            expected_updated_at,
        )


async def access_token(
    svc: Services, account: Principal, user: Principal, client: CalendarClient
) -> tuple[str, Any, dict[str, Any]]:
    # Serialize refresh-token rotation and commit it before subsequent provider calls,
    # so a failed event fetch cannot roll back a successfully rotated refresh token.
    async with svc.tx(account) as conn:
        row = await connection(conn, client.config.provider, user.user_id, lock=True)
        tokens = await token_of(svc, conn, account.user_id, row)
        if float(tokens["expires_at"]) <= datetime.now(UTC).timestamp() + 60:
            tokens = await client.tokens(refresh_token=str(tokens["refresh_token"]))
            sealed, version = await svc.crypto.encrypt(
                conn, account.user_id, TABLE + ":tokens", str(row["id"]), json.dumps(tokens)
            )
            await conn.execute(
                "update app.calendar_connections set token_enc=$2,token_key_version=$3 where id=$1",
                row["id"],
                sealed,
                version,
            )
        cache = await cache_of(svc, conn, account.user_id, row)
    return str(tokens["access_token"]), row, cache


@router.get("/calendars")
async def list_connections(svc: SvcDep, account: AccountDep, user: UserDep) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            "select * from app.calendar_connections where profile_id=$1", user.user_id
        )
        by_provider = {row["provider"]: row for row in rows}
        for provider in ("google", "outlook"):
            row = by_provider.get(provider)
            item: dict[str, Any] = {
                "provider": provider,
                "available": calendars.configured(provider) is not None,
                "connected": row is not None and row["token_enc"] is not None,
                "status": "disconnected",
                "calendars": [],
                "selected_calendars": [],
            }
            if row is not None:
                cache = await cache_of(svc, conn, account.user_id, row)
                item.update(
                    status=row["status"],
                    calendars=cache.get("calendars", []),
                    selected_calendars=list(row["selected_calendars"]),
                    last_sync_at=row["last_sync_at"],
                    last_error=row["last_error"],
                )
            items.append(item)
    return {"items": items}


@router.post("/calendars/{provider}/connect")
async def connect(
    provider: Provider, svc: SvcDep, account: AccountDep, user: UserDep
) -> dict[str, str]:
    config = config_of(provider)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    nonce = hashlib.sha256(state.encode()).hexdigest()
    async with svc.tx(account) as conn:
        row_id = await conn.fetchval(
            "insert into app.calendar_connections(id,user_id,profile_id,provider,oauth_nonce)"
            " values($1,$2,$3,$4,$5) on conflict(user_id,profile_id,provider)"
            " do update set oauth_nonce=excluded.oauth_nonce,updated_at=now() returning id",
            uuid7(),
            account.user_id,
            user.user_id,
            provider,
            nonce,
        )
    await svc.valkey.set(
        calendars.state_key(state),
        json.dumps(
            {
                "account_id": str(account.user_id),
                "profile_id": str(user.user_id),
                "connection_id": str(row_id),
                "provider": provider,
                "verifier": verifier,
                "nonce": nonce,
            }
        ),
        ex=600,
    )
    return {"authorization_url": calendars.authorization_url(config, state, verifier)}


@router.get("/calendars/{provider}/callback", response_class=HTMLResponse)
async def callback(
    provider: Provider,
    svc: SvcDep,
    state: str = Query(min_length=20, max_length=256),
    code: str | None = Query(default=None, max_length=4096),
    error: str | None = Query(default=None, max_length=200),
) -> HTMLResponse:
    # Public provider callback: only the random one-use server-side state grants access.
    raw = await svc.valkey.getdel(calendars.state_key(state))
    if raw is None:
        raise HTTPException(400, "Calendar authorization expired or was already used")
    pending = json.loads(text(raw))
    if pending["provider"] != provider:
        raise HTTPException(400, "Calendar provider does not match authorization")
    account_id, profile_id, row_id = (
        UUID(pending[key]) for key in ("account_id", "profile_id", "connection_id")
    )
    account = Principal(account_id, "", None)
    try:
        await profiles.resolve(svc.pool, account, str(profile_id))
    except profiles.ProfileError as failure:
        raise HTTPException(403, "Calendar profile is no longer accessible") from failure
    async with user_tx(svc.pool, account_id) as conn:
        valid = await conn.fetchval(
            "select id from app.calendar_connections where id=$1 and profile_id=$2"
            " and oauth_nonce=$3",
            row_id,
            profile_id,
            pending["nonce"],
        )
    if valid is None:
        raise HTTPException(400, "Calendar authorization was replaced or disconnected")
    if error or not code:
        async with user_tx(svc.pool, account_id) as conn:
            await conn.execute(
                "update app.calendar_connections set oauth_nonce=null,last_error=$2"
                " where id=$1 and oauth_nonce=$3",
                row_id,
                "Calendar authorization was declined",
                pending["nonce"],
            )
        raise HTTPException(400, "Calendar authorization was declined")
    try:
        async with provider_http() as http:
            client = CalendarClient(config_of(provider), http)
            tokens = await client.tokens(code=code, verifier=pending["verifier"])
            available = await client.calendars(tokens["access_token"])
        async with user_tx(svc.pool, account_id) as conn:
            # Check nonce again under lock: disconnect/reconnect during provider exchange
            # cannot resurrect or replace a newer connection.
            row = await conn.fetchrow(
                "select id from app.calendar_connections where id=$1 and oauth_nonce=$2 for update",
                row_id,
                pending["nonce"],
            )
            if row is None:
                raise HTTPException(409, "Calendar connection changed; reconnect")
            # Reauthorization may choose another provider account. Without requesting
            # identity scopes, clear old associations rather than matching unrelated IDs.
            await conn.execute(
                "delete from app.calendar_meeting_context where connection_id=$1", row_id
            )
            token_enc, token_version = await svc.crypto.encrypt(
                conn, account_id, TABLE + ":tokens", str(row_id), json.dumps(tokens)
            )
            cache_enc, cache_version = await svc.crypto.encrypt(
                conn,
                account_id,
                TABLE + ":cache",
                str(row_id),
                json.dumps({"calendars": available, "events": []}),
            )
            await conn.execute(
                "update app.calendar_connections set status='active',oauth_nonce=null,token_enc=$2,"
                "token_key_version=$3,cache_enc=$4,cache_key_version=$5,selected_calendars='{}',"
                "last_sync_at=null,last_error=null,updated_at=now() where id=$1",
                row_id,
                token_enc,
                token_version,
                cache_enc,
                cache_version,
            )
    except CalendarError as failure:
        await mark_failed(svc, account, row_id, failure, nonce=pending["nonce"])
        raise HTTPException(failure.status, str(failure)) from failure
    return HTMLResponse(
        "<!doctype html><title>Calendar connected</title><p>Calendar connected."
        " Return to Ruby and choose calendars for Meeting preparation.</p>",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        },
    )


@router.patch("/calendars/{provider}/selection")
async def select_calendars(
    provider: Provider, body: Selection, svc: SvcDep, account: AccountDep, user: UserDep
) -> dict[str, Any]:
    selected = list(dict.fromkeys(body.calendar_ids))
    async with svc.tx(account) as conn:
        row = await connection(conn, provider, user.user_id, lock=True)
        cache = await cache_of(svc, conn, account.user_id, row)
        allowed = {item["id"] for item in cache.get("calendars", [])}
        if any(value not in allowed for value in selected):
            raise HTTPException(422, "Choose calendars from this connection")
        cache["events"] = []
        sealed, version = await svc.crypto.encrypt(
            conn, account.user_id, TABLE + ":cache", str(row["id"]), json.dumps(cache)
        )
        await conn.execute(
            "update app.calendar_connections set selected_calendars=$2,cache_enc=$3,"
            "cache_key_version=$4,last_sync_at=null,updated_at=now() where id=$1",
            row["id"],
            selected,
            sealed,
            version,
        )
    return {"selected_calendars": selected}


@router.post("/calendars/{provider}/sync")
async def sync(
    provider: Provider, svc: SvcDep, account: AccountDep, user: UserDep
) -> dict[str, Any]:
    config = config_of(provider)
    async with svc.tx(account) as conn:
        original = await connection(conn, provider, user.user_id)
    # No periodic jobs: explicit refresh is bounded to once per five minutes per connection.
    if not await svc.valkey.set(f"calendar:sync:{original['id']}", "1", ex=300, nx=True):
        raise HTTPException(429, "Calendar was recently refreshed; try again in five minutes")
    try:
        async with provider_http() as http:
            client = CalendarClient(config, http)
            token, row, cache = await access_token(svc, account, user, client)
            available = await client.calendars(token)
            allowed = {item["id"] for item in available}
            selected = list(row["selected_calendars"])
            if any(value not in allowed for value in selected):
                raise CalendarError(
                    "A selected calendar is no longer available; select calendars again"
                )
            now = datetime.now(UTC)
            events: list[dict[str, Any]] = []
            for calendar_id in selected:
                events.extend(
                    await client.events(
                        token, calendar_id, now - timedelta(days=1), now + timedelta(days=14)
                    )
                )
                if len(events) > calendars.MAX_EVENTS:
                    raise CalendarError("Selected calendars exceed the synchronization limit")
            cache = {"calendars": available, "events": events}
        async with svc.tx(account) as conn:
            current = await connection(conn, provider, user.user_id, lock=True)
            if current["id"] != row["id"] or current["updated_at"] != row["updated_at"]:
                raise HTTPException(
                    409, "Calendar connection changed during synchronization; refresh again"
                )
            sealed, version = await svc.crypto.encrypt(
                conn, account.user_id, TABLE + ":cache", str(row["id"]), json.dumps(cache)
            )
            await conn.execute(
                "update app.calendar_connections set cache_enc=$2,cache_key_version=$3,"
                "last_sync_at=now(),status='active',last_error=null,updated_at=now() where id=$1",
                row["id"],
                sealed,
                version,
            )
    except CalendarError as failure:
        await mark_failed(
            svc, account, original["id"], failure, expected_updated_at=original["updated_at"]
        )
        raise HTTPException(failure.status, str(failure)) from failure
    return {"event_count": len(events), "synced_at": now.isoformat()}


@router.delete("/calendars/{provider}")
async def disconnect(
    provider: Provider, svc: SvcDep, account: AccountDep, user: UserDep
) -> dict[str, bool]:
    async with svc.tx(account) as conn:
        row = await connection(conn, provider, user.user_id, lock=True)
        tokens = None
        if row["token_enc"]:
            try:
                tokens = await token_of(svc, conn, account.user_id, row)
            except Exception:
                # A lost key or unavailable key service must never prevent local deletion.
                # No exception detail is exposed, since provider/KMS errors may carry secrets.
                tokens = None
        # Remove local tokens/cache first even when the remote provider is offline.
        await conn.execute("delete from app.calendar_connections where id=$1", row["id"])
    config = calendars.configured(provider)
    revoked = False
    if config and tokens:
        async with provider_http() as http:
            revoked = await CalendarClient(config, http).revoke(str(tokens["refresh_token"]))
    return {
        "disconnected": True,
        "provider_revoked": revoked,
        "provider_consent_removal_required": not revoked,
    }


class MeetingContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calendar_id: str = Field(min_length=1, max_length=2000)
    instance_id: str = Field(min_length=1, max_length=2000)
    application_id: UUID | None = None
    session_id: UUID | None = None
    unfinished_actions: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(
        default_factory=list, max_length=50
    )


@router.put("/calendars/{provider}/context")
async def save_context(
    provider: Provider, body: MeetingContext, svc: SvcDep, account: AccountDep, user: UserDep
) -> dict[str, bool]:
    async with svc.tx(user) as conn:
        if body.application_id and not await conn.fetchval(
            "select id from app.applications where id=$1", body.application_id
        ):
            raise HTTPException(404, "Application does not belong to this profile")
        if body.session_id and not await conn.fetchval(
            "select id from app.sessions where id=$1 and"
            " (private_context_account_id is null or private_context_account_id=$2)",
            body.session_id,
            account.user_id,
        ):
            raise HTTPException(404, "Session does not belong to this profile")
    async with svc.tx(account) as conn:
        row = await connection(conn, provider, user.user_id, lock=True)
        cache = await cache_of(svc, conn, account.user_id, row)
        event = next(
            (
                event
                for event in cache.get("events", [])
                if event["calendar_id"] == body.calendar_id
                and event["instance_id"] == body.instance_id
            ),
            None,
        )
        if event is None or body.calendar_id not in row["selected_calendars"]:
            raise HTTPException(404, "Meeting is not in the selected calendar cache")
        context_id = await conn.fetchval(
            "select id from app.calendar_meeting_context where connection_id=$1"
            " and calendar_id=$2 and instance_id=$3",
            row["id"],
            body.calendar_id,
            body.instance_id,
        )
        context_id = context_id or uuid7()
        sealed, version = await svc.crypto.encrypt(
            conn,
            account.user_id,
            "calendar_meeting_context",
            str(context_id),
            body.model_dump_json(),
        )
        await conn.execute(
            "insert into app.calendar_meeting_context"
            "(id,user_id,connection_id,calendar_id,instance_id,series_id,context_enc,key_version)"
            " values($1,$2,$3,$4,$5,$6,$7,$8) on conflict(connection_id,calendar_id,instance_id)"
            " do update set context_enc=excluded.context_enc,key_version=excluded.key_version,"
            "series_id=excluded.series_id,updated_at=now()",
            context_id,
            account.user_id,
            row["id"],
            body.calendar_id,
            body.instance_id,
            event.get("series_id"),
            sealed,
            version,
        )
    return {"saved": True}


@router.get("/meeting-preparation")
async def preparation(svc: SvcDep, account: AccountDep, user: UserDep) -> dict[str, Any]:
    meetings: list[CalendarMeeting] = []
    selected: set[tuple[str, str]] = set()
    states: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            "select * from app.calendar_connections where profile_id=$1", user.user_id
        )
        for row in rows:
            context_rows = await conn.fetch(
                "select * from app.calendar_meeting_context where connection_id=$1"
                " order by updated_at desc limit 1000",
                row["id"],
            )
            event_context: dict[tuple[str, str], dict[str, Any]] = {}
            for context_row in context_rows:
                content = json.loads(
                    await svc.crypto.decrypt(
                        conn,
                        account.user_id,
                        "calendar_meeting_context",
                        str(context_row["id"]),
                        bytes(context_row["context_enc"]),
                        int(context_row["key_version"]),
                    )
                )
                event_context[(context_row["calendar_id"], context_row["instance_id"])] = content
                contexts.append(
                    {
                        **content,
                        "connection_id": str(row["id"]),
                        "series_id": context_row["series_id"],
                    }
                )
            cache = await cache_of(svc, conn, account.user_id, row)
            connection_id = str(row["id"])
            selected.update(
                (connection_id, calendar_id) for calendar_id in row["selected_calendars"]
            )
            states.append(
                {
                    "provider": row["provider"],
                    "connection_id": connection_id,
                    "status": row["status"],
                    "last_sync_at": row["last_sync_at"],
                    "last_error": row["last_error"],
                }
            )
            if row["status"] != "active":
                continue
            for event in cache.get("events", []):
                meetings.append(
                    CalendarMeeting(
                        account_id=connection_id,
                        calendar_id=event["calendar_id"],
                        instance_id=event["instance_id"],
                        title=event["title"],
                        start=datetime.fromisoformat(event["start"]),
                        end=datetime.fromisoformat(event["end"]),
                        all_day=event["all_day"],
                        series_id=event.get("series_id"),
                        application_id=event_context.get(
                            (event["calendar_id"], event["instance_id"]), {}
                        ).get("application_id"),
                    )
                )
    async with svc.tx(user) as conn:
        apps = await conn.fetch(
            "select id,company,role,app.paired_resume(id) as resume_id"
            " from app.applications order by updated_at desc limit 500"
        )
    history: list[PreviousMeeting] = []
    async with svc.tx(user) as conn:
        for context in contexts:
            if not context.get("series_id"):
                continue
            notes = ""
            if context.get("session_id"):
                session = await conn.fetchrow(
                    "select notes_enc,notes_key_version from app.sessions where id=$1 and"
                    " (private_context_account_id is null or private_context_account_id=$2)",
                    UUID(context["session_id"]),
                    account.user_id,
                )
                if session is not None and session["notes_enc"] is not None:
                    notes = await svc.crypto.decrypt(
                        conn,
                        user.user_id,
                        "sessions",
                        context["session_id"],
                        bytes(session["notes_enc"]),
                        session["notes_key_version"],
                    )
            actions = tuple(context.get("unfinished_actions", []))
            if notes or actions:
                history.append(
                    PreviousMeeting(
                        str(user.user_id),
                        context["connection_id"],
                        context["calendar_id"],
                        context["series_id"],
                        notes,
                        actions,
                    )
                )
    candidates = [
        ApplicationContext(
            str(app["id"]),
            str(user.user_id),
            app["company"],
            app["role"],
            str(app["resume_id"]) if app["resume_id"] else None,
            str(app["id"]),
        )
        for app in apps
    ]
    items = prepare_meetings(
        meetings,
        now=datetime.now(UTC),
        profile_id=str(user.user_id),
        selected_calendars=selected,
        applications=candidates,
        previous=history,
    )
    return {"items": [asdict(item) for item in items], "connections": states}
