"""Bounded factual projections for explicitly consented XRuby connections.

No model calls, calendar refresh, pack generation or mutation. Source hashes cover
this versioned projection, not every field in the originating product row.
"""

import asyncio
import contextlib
import hashlib
import json
import secrets
import time
from datetime import UTC, date, datetime
from datetime import time as day_time
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from copilot_api.deps import Services, SvcDep
from copilot_api.routes.xruby_oauth import (
    CONTRACT,
    HEADERS,
    Delegated,
    Delegation,
    connection,
    key,
    read_scope,
)

router = APIRouter(prefix="/v1/xruby", tags=["xruby-delegation"])
MAX_RESPONSE = 128 * 1024
MAX_SOURCE = 32768
SOURCE_SCOPE = {
    "interview": "xruby:interviews:read",
    "interview_preparation": "xruby:interviews:read",
    "work_item": "xruby:work:read",
    "work_log": "xruby:retrospective:read",
    "feedback": "xruby:retrospective:read",
}


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def bounded_response(value: Any) -> JSONResponse:
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_RESPONSE:
        raise HTTPException(413, "Delegated response exceeds the bounded read limit")
    return JSONResponse(value, headers=HEADERS)


def query_fields(request: Request, allowed: set[str]) -> None:
    if any(
        k not in allowed or len(request.query_params.getlist(k)) != 1 for k in request.query_params
    ):
        raise HTTPException(422, "Unsupported or duplicate delegated query parameter")


async def timezone(conn: Any, grant: Delegation) -> str:
    name = await conn.fetchval(
        "select settings->>'timezone' from app.accounts where id=$1", grant.account.user_id
    )
    try:
        return ZoneInfo(name or "UTC").key
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return "UTC"


def citation(
    grant: Delegation,
    kind: str,
    identifier: UUID,
    content: dict[str, Any],
    revision: str | None = None,
) -> dict[str, Any]:
    sha = digest(content)
    return {
        "product": "coruby",
        "profile_id": str(grant.profile_id),
        "employer_id": None
        if kind in {"interview", "interview_preparation"}
        else str(grant.employer_id),
        "kind": kind,
        "id": str(identifier),
        "revision": revision or sha,
        "source_hash": sha,
        "path": f"/v1/xruby/sources/{kind}/{identifier}",
    }


async def source(
    conn: Any, svc: Services, grant: Delegation, kind: str, identifier: UUID
) -> dict[str, Any]:
    if kind == "interview_preparation":
        occurrence = await source(conn, svc, grant, "interview", identifier)
        row = await conn.fetchrow(
            "select i.pack_id,octet_length(coalesce(a.jd_text,'')) as jd_bytes,"
            " encode(sha256(convert_to(coalesce(a.jd_text,''),'UTF8')),'hex') as jd_hash,"
            " r.id as resume_id,r.current_revision_id,r.content_sha256,r.extracted_sha256,"
            " r.status as resume_status,"
            " encode(sha256(coalesce(rv.text_enc,rx.text_enc)),'hex') as text_source_hash"
            " from app.interviews i"
            " join app.applications a on a.id=i.job_id and a.user_id=i.user_id"
            " left join app.resume_versions r on r.id=app.paired_resume(a.id)"
            " and r.user_id=a.user_id"
            " left join app.resume_revisions rv on rv.id=r.current_revision_id"
            " and rv.resume_version_id=r.id"
            " left join app.resume_revision_extractions rx on rx.revision_id=rv.id"
            " where i.id=$1 and i.user_id=$2",
            identifier,
            grant.profile_id,
        )
        if row is None:
            raise HTTPException(404, "Preparation source is unavailable")
        resume_ready = bool(
            row["resume_id"]
            and row["content_sha256"]
            and row["content_sha256"] == row["extracted_sha256"]
        )
        content = {
            "title": "Interview preparation — " + occurrence["content"]["title"],
            "status": "draft",
            "generated": False,
            "interview": occurrence["content"],
            "job_description": {"available": row["jd_bytes"] > 0, "source_hash": row["jd_hash"]},
            "paired_resume": {
                "id": str(row["resume_id"]) if row["resume_id"] else None,
                "revision": str(row["current_revision_id"]) if row["current_revision_id"] else None,
                "source_hash": row["content_sha256"],
                "status": row["resume_status"],
                "extraction_current": resume_ready,
                "text_source_hash": row["text_source_hash"],
            },
            "prepared_pack": {
                "id": str(row["pack_id"]) if row["pack_id"] else None,
                "verified_current": False,
                "cache_availability": "not_checked",
            },
            "draft_checklist": [
                "Confirm the invite, time zone and cancellation status in your calendar.",
                "Review the saved job description."
                if row["jd_bytes"]
                else "Add the missing job description in CoRuby.",
                "Review the paired resume and select examples."
                if resume_ready
                else "Check the paired resume and complete its source extraction in CoRuby.",
                "Practice questions for the recorded interview round.",
                "Review any existing preparation in CoRuby before relying on it.",
            ],
            "actions_performed": [],
        }
        return {**citation(grant, kind, identifier, content), "content": content}
    if kind == "interview":
        row = await conn.fetchrow(
            "select i.id,i.job_id,left(i.round,200) as round,i.starts_at,"
            " left(a.company,200) as company,left(a.role,200) as role from app.interviews i"
            " join app.applications a on a.id=i.job_id and a.user_id=i.user_id"
            " where i.id=$1 and i.user_id=$2",
            identifier,
            grant.profile_id,
        )
        if not row:
            raise HTTPException(404, "Source is unavailable")
        content = {
            "occurrence_id": str(identifier),
            "application_id": str(row["job_id"]),
            "title": row["company"] + " — " + row["role"],
            "company": row["company"],
            "role": row["role"],
            "round": row["round"],
            "starts_at": iso(row["starts_at"]),
            "status": "recorded",
            "cancellation_state": "not_recorded",
            "source_timezone": None,
        }
        return {**citation(grant, kind, identifier, content), "content": content}
    if grant.employer_id is None:
        raise HTTPException(403, "No company was granted")
    if kind == "work_item":
        row = await conn.fetchrow(
            "select id,kind,left(title,200) as title,left(summary,2000) as summary,"
            " valid_from,updated_at,left(body->>'state',40) as state,"
            " left(body->>'due_date',32) as due_date,body->>'edited'='true' as edited,"
            " left(source->>'type',80) as source_type from app.memory_nodes"
            " where id=$1 and user_id=$2 and employer_id=$3 and status='active'"
            " and kind in ('work_item','commitment')",
            identifier,
            grant.profile_id,
            grant.employer_id,
        )
        if not row:
            raise HTTPException(404, "Source is unavailable")
        due = None
        with contextlib.suppress(ValueError, TypeError):
            due = date.fromisoformat(row["due_date"]).isoformat() if row["due_date"] else None
        state = (
            row["state"]
            if row["state"] in {"planned", "in_progress", "blocked", "done", "open", "dropped"}
            else "unknown"
        )
        content = {
            "title": row["title"],
            "summary": row["summary"],
            "kind": row["kind"],
            "status": state,
            "day": row["valid_from"].isoformat() if row["valid_from"] else None,
            "updated_at": iso(row["updated_at"]),
            "due_date": due,
            "due_precision": "date" if due else None,
            "priority": None,
            "blocked": state == "blocked",
            "missed": None,
            "provenance": "user_edited_memory" if row["edited"] else "derived_memory",
            "source_type": row["source_type"],
        }
        return {**citation(grant, kind, identifier, content), "content": content}
    if kind == "work_log":
        row = await conn.fetchrow(
            "select w.id,w.day,w.current_revision_id,"
            " case when octet_length(r.text_enc)<=$4 then r.text_enc end as sealed,r.key_version"
            " from app.work_logs w join app.work_log_revisions r"
            " on r.id=w.current_revision_id and r.workspace_id=w.user_id and r.work_log_id=w.id"
            " where w.id=$1 and w.user_id=$2 and w.employer_id=$3",
            identifier,
            grant.profile_id,
            grant.employer_id,
            MAX_SOURCE,
        )
        if not row:
            raise HTTPException(404, "Source is unavailable")
        if row["sealed"] is None:
            raise HTTPException(413, "Source exceeds the delegated text limit")
        body = await svc.crypto.decrypt(
            conn, grant.profile_id, "work_logs", str(identifier), row["sealed"], row["key_version"]
        )
        content = {
            "title": "Work log " + row["day"].isoformat(),
            "day": row["day"].isoformat(),
            "text": body,
            "provenance": "user_report",
            "attachments_included": False,
        }
        return {
            **citation(grant, kind, identifier, content, str(row["current_revision_id"])),
            "content": content,
        }
    if kind == "feedback":
        row = await conn.fetchrow(
            "select o.id,o.session_id,o.created_at,"
            " case when octet_length(o.content_enc)<=$4 then o.content_enc end as sealed,"
            " o.key_version"
            " from app.session_outputs o join app.sessions s"
            " on s.id=o.session_id and s.user_id=o.user_id"
            " where o.id=$1 and o.user_id=$2 and s.employer_id=$3"
            " and o.kind='feedback'",
            identifier,
            grant.profile_id,
            grant.employer_id,
            MAX_SOURCE,
        )
        if not row:
            raise HTTPException(404, "Source is unavailable")
        if row["sealed"] is None:
            raise HTTPException(413, "Source exceeds the delegated text limit")
        body = await svc.crypto.decrypt(
            conn,
            grant.profile_id,
            "session_outputs",
            str(identifier),
            row["sealed"],
            row["key_version"],
        )
        content = {
            "title": "Recorded session feedback",
            "session_id": str(row["session_id"]),
            "created_at": iso(row["created_at"]),
            "text": body,
            "provenance": "ai_generated_feedback",
            "verified_mistake": False,
        }
        return {**citation(grant, kind, identifier, content), "content": content}
    raise HTTPException(404, "Unknown source kind")


def meta(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if k != "content"}


def envelope(
    grant: Delegation,
    as_of: str,
    items: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    coverage: dict[str, Any],
    cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "connection": connection(grant.record),
        "as_of": as_of,
        "items": items,
        "sources": sources,
        "next_cursor": cursor,
        "coverage": coverage,
    }


async def page_state(
    svc: Services, grant: Delegation, kind: str, params: Any, cursor: str | None
) -> dict[str, Any]:
    binding = digest({"grant": grant.record["id"], "kind": kind, "params": params})
    if cursor:
        if len(cursor) != 43 or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in cursor
        ):
            raise HTTPException(422, "Invalid cursor")
        raw = await svc.valkey.get(key("cursor", cursor))
        state: dict[str, Any] = json.loads(raw) if raw else {}
        if state.get("binding") != binding:
            raise HTTPException(409, "Cursor expired or belongs to another query or connection")
        return state
    return {"binding": binding, "as_of": iso(datetime.now(UTC)), "after": None}


async def next_cursor(svc: Services, grant: Delegation, state: dict[str, Any], after: Any) -> str:
    token = secrets.token_urlsafe(32)
    ttl = max(1, min(300, int(grant.record["expires_at"] - time.time())))
    await svc.valkey.set(key("cursor", token), json.dumps({**state, "after": after}), ex=ttl)
    return token


@router.get("/next-interview")
async def next_interview(
    request: Request, svc: SvcDep, grant: Delegated, as_of: AwareDatetime | None = None
) -> JSONResponse:
    query_fields(request, {"as_of"})
    at = as_of or datetime.now(UTC)
    async with asyncio.timeout(15), read_scope(svc, grant, "xruby:interviews:read") as conn:
        identifier = await conn.fetchval(
            "select i.id from app.interviews i"
            " join app.applications a on a.id=i.job_id and a.user_id=i.user_id"
            " where i.user_id=$1 and i.starts_at>=$2 order by i.starts_at,i.id limit 1",
            grant.profile_id,
            at,
        )
        values = [await source(conn, svc, grant, "interview", identifier)] if identifier else []
        zone = await timezone(conn, grant)
        result = envelope(
            grant,
            iso(at),
            [{**v["content"], "timezone": zone, "source": meta(v)} for v in values],
            [meta(v) for v in values],
            {
                "schedule": "recorded_schedule_only",
                "calendar_synced": False,
                "cancellation_state": "not_recorded",
                "timezone": zone,
                "source_timezone": None,
            },
        )
    return bounded_response(result)


@router.get("/interviews/{identifier}/preparation")
async def preparation(
    request: Request,
    identifier: UUID,
    svc: SvcDep,
    grant: Delegated,
    revision: str = Query(min_length=1, max_length=64),
    expected_hash: str = Query(pattern=r"^[a-f0-9]{64}$"),
) -> JSONResponse:
    query_fields(request, {"revision", "expected_hash"})
    async with asyncio.timeout(15), read_scope(svc, grant, "xruby:interviews:read") as conn:
        value = await source(conn, svc, grant, "interview_preparation", identifier)
        occurrence = citation(grant, "interview", identifier, value["content"]["interview"])
        if occurrence["revision"] != revision or occurrence["source_hash"] != expected_hash:
            raise HTTPException(409, "Interview changed; select the current occurrence")
        result = envelope(
            grant,
            iso(datetime.now(UTC)),
            [{**value["content"], "source": meta(value)}],
            [occurrence, meta(value)],
            {
                "preparation": "deterministic_draft",
                "model_called": False,
                "calendar_synced": False,
                "pack_currentness": "not_verified",
                "source_bodies": "not_included",
                "scheduling_performed": False,
            },
        )
    return bounded_response(result)


@router.get("/current-work")
async def current_work(
    request: Request,
    svc: SvcDep,
    grant: Delegated,
    limit: int = Query(20, ge=1, le=20),
    cursor: str | None = None,
) -> JSONResponse:
    query_fields(request, {"limit", "cursor"})
    state = await page_state(svc, grant, "work", {"limit": limit}, cursor)
    async with asyncio.timeout(15), read_scope(svc, grant, "xruby:work:read") as conn:
        zone = await timezone(conn, grant)
        today = (
            datetime.fromisoformat(state["as_of"].replace("Z", "+00:00"))
            .astimezone(ZoneInfo(zone))
            .date()
        )
        rows = await conn.fetch(
            "select id from app.memory_nodes where user_id=$1 and employer_id=$2"
            " and status='active' and kind in ('work_item','commitment')"
            " and coalesce(body->>'state','unknown') not in ('done','dropped')"
            " and ($3::uuid is null or id>$3) order by id limit $4",
            grant.profile_id,
            grant.employer_id,
            UUID(state["after"]) if state["after"] else None,
            limit + 1,
        )
        values = [await source(conn, svc, grant, "work_item", row["id"]) for row in rows[:limit]]
        items = [
            {
                **v["content"],
                "overdue": (
                    date.fromisoformat(v["content"]["due_date"]) < today
                    if v["content"]["due_date"]
                    else None
                ),
                "source": meta(v),
            }
            for v in values
        ]
        more = (
            await next_cursor(svc, grant, state, str(rows[limit - 1]["id"]))
            if len(rows) > limit
            else None
        )
        result = envelope(
            grant,
            state["as_of"],
            items,
            [meta(v) for v in values],
            {
                "sources": "active_recorded_work_memory",
                "exhaustive_tracker": False,
                "timezone": zone,
                "priority": "not_recorded",
                "missed": "not_inferred",
                "pagination": "live_keyset",
            },
            more,
        )
    return bounded_response(result)


@router.get("/retrospective")
async def retrospective(
    request: Request,
    svc: SvcDep,
    grant: Delegated,
    start: date,
    end: date,
    limit: int = Query(20, ge=1, le=20),
    cursor: str | None = None,
) -> JSONResponse:
    query_fields(request, {"start", "end", "limit", "cursor"})
    if not 0 < (end - start).days <= 31:
        raise HTTPException(422, "Choose an exclusive-end date window of 1 to 31 days")
    state = await page_state(
        svc,
        grant,
        "retro",
        {"start": start.isoformat(), "end": end.isoformat(), "limit": limit},
        cursor,
    )
    async with asyncio.timeout(15), read_scope(svc, grant, "xruby:retrospective:read") as conn:
        zone = await timezone(conn, grant)
        lower = datetime.combine(start, day_time(), ZoneInfo(zone)).astimezone(UTC)
        upper = datetime.combine(end, day_time(), ZoneInfo(zone)).astimezone(UTC)
        after = state["after"]
        rows = await conn.fetch(
            "select kind,id,at from ("
            " select 'work_log'::text as kind,id,day::timestamp at time zone $5 as at"
            " from app.work_logs where user_id=$1 and employer_id=$2 and day>=$3 and day<$4"
            " union all select 'feedback',o.id,o.created_at from app.session_outputs o"
            " join app.sessions s on s.id=o.session_id and s.user_id=o.user_id"
            " where o.user_id=$1 and s.employer_id=$2 and o.kind='feedback'"
            " and o.created_at>=$6 and o.created_at<$7) sources"
            " where ($8::timestamptz is null or (at,id,kind)<($8,$9::uuid,$10::text))"
            " order by at desc,id desc,kind desc limit $11",
            grant.profile_id,
            grant.employer_id,
            start,
            end,
            zone,
            lower,
            upper,
            datetime.fromisoformat(after[0].replace("Z", "+00:00")) if after else None,
            UUID(after[1]) if after else None,
            after[2] if after else None,
            limit + 1,
        )
        values: list[dict[str, Any]] = []
        omitted = 0
        for row in rows[:limit]:
            try:
                values.append(await source(conn, svc, grant, row["kind"], row["id"]))
            except HTTPException as exc:
                if exc.status_code != 413:
                    raise
                omitted += 1
        items = [
            {
                **v["content"],
                "kind": v["kind"],
                "text": v["content"]["text"][:2000],
                "truncated": len(v["content"]["text"]) > 2000,
                "source": meta(v),
            }
            for v in values
        ]
        tail = rows[limit - 1] if len(rows) > limit else None
        more = (
            await next_cursor(svc, grant, state, [iso(tail["at"]), str(tail["id"]), tail["kind"]])
            if tail
            else None
        )
        result = envelope(
            grant,
            state["as_of"],
            items,
            [meta(v) for v in values],
            {
                "sources": ["saved_work_logs", "ai_generated_session_feedback"],
                "verified_mistakes": False,
                "start_inclusive": start.isoformat(),
                "end_exclusive": end.isoformat(),
                "timezone": zone,
                "omitted_oversized": omitted,
                "attachments_included": False,
                "pagination": "live_keyset",
            },
            more,
        )
    return bounded_response(result)


@router.get("/sources/{kind}/{identifier}")
async def read_source(
    request: Request,
    kind: Literal["interview", "interview_preparation", "work_item", "work_log", "feedback"],
    identifier: UUID,
    svc: SvcDep,
    grant: Delegated,
    revision: str = Query(min_length=1, max_length=64),
    expected_hash: str = Query(pattern=r"^[a-f0-9]{64}$"),
) -> JSONResponse:
    query_fields(request, {"revision", "expected_hash"})
    if "xruby:sources:read" not in grant.record["scope"].split():
        raise HTTPException(403, "Delegation does not grant source retrieval")
    async with asyncio.timeout(15), read_scope(svc, grant, SOURCE_SCOPE[kind]) as conn:
        value = await source(conn, svc, grant, kind, identifier)
        if value["revision"] != revision or value["source_hash"] != expected_hash:
            raise HTTPException(409, "Source changed; repeat the factual query")
        result = {
            "contract": CONTRACT,
            "connection": connection(grant.record),
            "as_of": iso(datetime.now(UTC)),
            "source": value,
            "coverage": {"projection": "xruby-delegation-v1", "source_current": True},
        }
    return bounded_response(result)
