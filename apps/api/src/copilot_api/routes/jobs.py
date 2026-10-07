"""`POST /jobs`, `GET /jobs`, `POST /interviews`, `GET /interviews`.

Saving a job, or scheduling an interview for one, enqueues `pack.build`, so the session pack
is ready by the time the user starts the call. `GET /jobs` shows each job's pack from
`pack:{uid}:index`, the same index the gateway loads packs from.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from copilot_api.deps import Services, SvcDep, UserDep
from copilot_core import jobs, keys
from copilot_core.ids import uuid7
from copilot_core.packs import SessionPack
from copilot_core.valkey_client import text

router = APIRouter()


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=50_000)
    url: str | None = Field(default=None, max_length=2000)


class InterviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    round: Literal["phone_screen", "technical", "system_design", "final"]
    starts_at: AwareDatetime


async def pack_info(svc: Services, uid: UUID, pack_id: str | None) -> dict[str, Any] | None:
    if not pack_id:
        return None
    raw = await svc.valkey.get(keys.pack(uid, pack_id))
    if raw is None:
        return {"pack_id": pack_id, "available": False}
    pack = SessionPack.model_validate_json(raw)
    return {
        "pack_id": pack_id,
        "available": True,
        "built_at": pack.built_at,
        "round": pack.context.round,
        "prepared_answers": len(pack.prepared_answers),
        "ranked_stories": len(pack.ranked_stories),
    }


@router.post("/jobs", status_code=201)
async def create_job(body: JobIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    job_id = uuid7()
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "insert into app.jobs (id, user_id, company, title, description, url)"
            " values ($1, $2, $3, $4, $5, $6) returning id, company, title, url, created_at",
            job_id,
            user.user_id,
            body.company,
            body.title,
            body.description,
            body.url,
        )
    digest = await svc.digest_version(user.user_id)
    await svc.queue.send(
        jobs.pack_build(user.user_id, "interview", f"job:{job_id}", datetime.now(UTC), digest)
    )
    return {**_job(row), "pack": None}


def _job(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "company": row["company"],
        "title": row["title"],
        "url": row["url"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/jobs")
async def list_jobs(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select id, company, title, url, created_at from app.jobs"
            " where user_id = $1 order by created_at desc",
            user.user_id,
        )
    index = await svc.valkey.hgetall(keys.pack_index(user.user_id))
    by_field = {text(k): text(v) for k, v in index.items()}
    out = [
        {**_job(r), "pack": await pack_info(svc, user.user_id, by_field.get(f"job:{r['id']}"))}
        for r in rows
    ]
    return {"jobs": out}


def _interview(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "job_id": str(row["job_id"]),
        "round": row["round"],
        "starts_at": row["starts_at"].isoformat(),
        "pack_id": str(row["pack_id"]) if row["pack_id"] else None,
        "company": row.get("company"),
        "title": row.get("title"),
    }


@router.post("/interviews", status_code=201)
async def create_interview(body: InterviewIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    interview_id = uuid7()
    async with svc.tx(user) as conn:
        if await conn.fetchval("select 1 from app.jobs where id = $1", body.job_id) is None:
            raise HTTPException(404, "no such job")
        row = await conn.fetchrow(
            "with i as (insert into app.interviews (id, user_id, job_id, round, starts_at)"
            " values ($1, $2, $3, $4, $5) returning id, job_id, round, starts_at, pack_id)"
            " select i.*, j.company, j.title from i join app.jobs j on j.id = i.job_id",
            interview_id,
            user.user_id,
            body.job_id,
            body.round,
            body.starts_at,
        )
    digest = await svc.digest_version(user.user_id)
    await svc.queue.send(
        jobs.pack_build(
            user.user_id, "interview", f"interview:{interview_id}", body.starts_at, digest
        )
    )
    return _interview(row)


@router.get("/interviews")
async def list_interviews(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select i.id, i.job_id, i.round, i.starts_at, i.pack_id, j.company, j.title"
            " from app.interviews i join app.jobs j on j.id = i.job_id"
            " where i.user_id = $1 order by i.starts_at desc",
            user.user_id,
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = _interview(r)
        item["pack"] = await pack_info(svc, user.user_id, item["pack_id"])
        out.append(item)
    return {"interviews": out}
