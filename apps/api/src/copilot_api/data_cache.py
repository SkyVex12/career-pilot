"""Private conditional reads with authorization before validation and one DB snapshot.

Only explicit pure representations participate. Live state, expiring prepared packs,
speech generation, timeout reconciliation, account/link discovery and signed URLs do not.
"""

import hashlib
import json
from collections.abc import Callable, Coroutine
from datetime import date
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute

from copilot_api.cache_context import ReadSnapshot, snapshot
from copilot_api.deps import current_account, current_user, require_link, services
from copilot_core import __version__
from copilot_core.db import Conn

WORK = {
    "/employers",
    "/employers/{employer_id}/logs",
    "/employers/{employer_id}/logs/{day}",
    "/employers/{employer_id}/documents",
    "/employers/{employer_id}/documents/{document_id}",
    "/employers/{employer_id}/memory",
    "/proposals",
}
HISTORY = {
    "/sessions",
    "/sessions/{session_id}",
    "/sessions/{session_id}/transcript",
    "/sessions/{session_id}/replay",
}


async def revision(conn: Conn, uid: UUID, domain: str) -> int:
    return int(
        await conn.fetchval(
            "select revision from app.data_revisions where user_id=$1 and domain=$2", uid, domain
        )
        or 0
    )


async def resource_revision(conn: Conn, uid: UUID, domain: str, kind: str, rid: str) -> int:
    return int(
        await conn.fetchval(
            "select revision from app.data_changes where user_id=$1 and domain=$2"
            " and resource=$3 and resource_id=$4",
            uid,
            domain,
            kind,
            rid,
        )
        or 0
    )


def validator(*parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return '"' + hashlib.sha256(raw.encode()).hexdigest() + '"'


def matches(header: str | None, etag: str) -> bool:
    # If-None-Match on a GET uses weak comparison (RFC 9110, section 13.1.2).
    return any(part.strip().removeprefix("W/") in (etag, "*") for part in (header or "").split(","))


class DataCacheRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()
        path = self.path.removeprefix("/v1")
        domain = "work" if path in WORK else "history" if path in HISTORY else None
        catalog = path == "/call-languages"
        # A list can hide a live privacy marker before its next database flush.
        # Rebuilding this small metadata page checks those markers; never authorize
        # a cold persisted history index solely with the durable collection version.
        conditional = path != "/sessions"
        if not domain and not catalog:
            return original

        async def read_once(request: Request) -> Response:
            if request.method != "GET":
                return await original(request)
            # Small route-only test applications supply dependencies, not a Services
            # instance; they exercise the ordinary handler without the cache layer.
            if not getattr(request.app.state, "services", None):
                return await original(request)
            svc = services(request)
            authorization = request.headers.get("authorization")
            profile = request.headers.get("x-copilot-profile")
            account = await current_account(svc, authorization)
            user = await current_user(svc, account, Response(), profile)
            if not catalog:
                await require_link(svc, account)
            async with svc.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
                await conn.execute("select set_config('app.user_id',$1,true)", str(user.user_id))
                token = snapshot.set(ReadSnapshot(account, user, authorization, profile, conn))
                try:
                    eligible = True
                    sid = request.path_params.get("session_id")
                    try:
                        eid = request.path_params.get("employer_id")
                        if sid:
                            # Durable privacy and live privacy are both authoritative.
                            from copilot_api.routes.sessions import session_privacy

                            await session_privacy(svc, user, account, UUID(sid))
                            row = await conn.fetchrow(
                                "select ended_at from app.sessions where id=$1 for share", UUID(sid)
                            )
                            eligible = row is not None and row["ended_at"] is not None
                        elif domain == "history":
                            # A live session's privacy marker can precede its DB flush.
                            eligible = not await conn.fetchval(
                                "select exists(select 1 from app.sessions where ended_at is null)"
                            )
                        if eid:
                            exists = await conn.fetchval(
                                "select 1 from app.employers where id=$1", UUID(eid)
                            )
                            if not exists:
                                raise HTTPException(404, "no such employer")
                            if "day" in request.path_params:
                                exists = await conn.fetchval(
                                    "select 1 from app.work_logs where employer_id=$1 and day=$2",
                                    UUID(eid),
                                    date.fromisoformat(request.path_params["day"]),
                                )
                                if not exists:
                                    raise HTTPException(404, "no log for that day")
                            if "document_id" in request.path_params:
                                exists = await conn.fetchval(
                                    "select 1 from app.company_documents"
                                    " where employer_id=$1 and id=$2"
                                    " and work_log_id is null",
                                    UUID(eid),
                                    UUID(request.path_params["document_id"]),
                                )
                                if not exists:
                                    raise HTTPException(404, "no such document")
                    except ValueError:
                        # Let FastAPI return its normal parameter validation response.
                        eligible = False
                    rev = await revision(conn, user.user_id, domain) if domain else 0
                    content_revision: object = rev
                    if sid and domain == "history":
                        content_revision = await resource_revision(
                            conn, user.user_id, "history", "sessions", str(sid)
                        )
                        if path.endswith("/replay"):
                            content_revision = [
                                content_revision,
                                await resource_revision(
                                    conn, user.user_id, "history", "references", "*"
                                ),
                            ]
                    config = (
                        (
                            svc.settings.verified_call_languages,
                            svc.settings.stt_providers,
                            svc.settings.deepgram_model,
                        )
                        if catalog
                        else None
                    )
                    etag = validator(
                        "data-v1",
                        __version__,
                        account.user_id,
                        user.user_id,
                        request.url.path,
                        sorted(request.query_params.multi_items()),
                        content_revision,
                        config,
                    )
                    headers = {
                        "Cache-Control": (
                            "private, max-age=86400, must-revalidate"
                            if catalog
                            else "private, max-age=30, must-revalidate"
                        ),
                        "Vary": "Authorization, X-Copilot-Profile",
                        "X-Copilot-Account": str(account.user_id),
                        "X-Copilot-Workspace": str(user.user_id),
                        "X-Copilot-Revision": str(rev),
                    }
                    if conditional:
                        headers["ETag"] = etag
                    if (
                        eligible
                        and conditional
                        and matches(request.headers.get("if-none-match"), etag)
                    ):
                        return Response(status_code=304, headers=headers)
                    response = await original(request)
                    if eligible and response.status_code == 200:
                        response.headers.update(headers)
                    else:
                        response.headers["Cache-Control"] = "private, no-store"
                    return response
                finally:
                    snapshot.reset(token)

        async def read(request: Request) -> Response:
            if request.method != "GET":
                return await original(request)
            # The privacy row lock can meet a notes/worker update committed since the
            # repeatable-read snapshot began. Retry the whole pure read with a fresh
            # snapshot so its response and validator can never describe different data.
            for attempt in range(3):
                try:
                    return await read_once(request)
                except asyncpg.SerializationError as error:
                    if attempt == 2:
                        raise HTTPException(
                            503,
                            "This data is changing; retry the request.",
                            headers={"Retry-After": "1", "Cache-Control": "private, no-store"},
                        ) from error
            raise AssertionError("unreachable")

        return read
