"""Applications (what Career Pilot called jobs), interviews, and the old `/jobs` endpoints.

An application is shaped like Career-OS's (company, role, URL, source site, stage, note) with
its JD text, and is paired with one resume version (ARCHITECTURE.md, "Resume versions"):
the user's choice, else its newest tailored version, else the base. Saving an application,
changing its JD or its pairing, or scheduling an interview enqueues `pack.build` for the
current pairing, so the session pack is ready by the time the user starts the call.

`GET /jobs` and `POST /jobs` keep their shape for desktop builds from before applications,
until migration 0009.
"""

from typing import Any, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from copilot_api.deps import Services, SvcDep, UserDep
from copilot_core import jobs, keys, resumes
from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.packs import SessionPack
from copilot_core.valkey_client import text

router = APIRouter()

Stage = Literal["applied", "interview", "offer", "rejected"]  # Career-OS's stages
MAX_JD = 50_000
MAX_RESUME_TEXT = 100_000


class ApplicationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2000)
    source_site: str | None = Field(default=None, max_length=200)
    stage: Stage = "applied"
    note: str | None = Field(default=None, max_length=10_000)
    jd_text: str = Field(min_length=1, max_length=MAX_JD)
    # A tailored resume pasted with the application (an upload goes through POST /documents
    # with this application's id instead).
    resume_text: str | None = Field(default=None, min_length=1, max_length=MAX_RESUME_TEXT)
    resume_label: str | None = Field(default=None, min_length=1, max_length=200)


class ApplicationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, max_length=2000)
    source_site: str | None = Field(default=None, max_length=200)
    stage: Stage | None = None
    note: str | None = Field(default=None, max_length=10_000)
    jd_text: str | None = Field(default=None, min_length=1, max_length=MAX_JD)
    # A version id (the base or one of this application's), or "auto".
    resume_version_id: UUID | Literal["auto"] | None = None


class JobIn(BaseModel):
    """`POST /jobs` from desktop builds before applications."""

    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=MAX_JD)
    url: str | None = Field(default=None, max_length=2000)


class InterviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID  # the application's id
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
        "resume_version_id": str(pack.resume_version_id) if pack.resume_version_id else None,
    }


async def enqueue_packs(svc: Services, conn: Conn, uid: UUID, application_id: UUID) -> None:
    for message in await resumes.pack_messages(
        conn, svc.valkey, uid, application_id, svc.settings.embedding_model_id
    ):
        await svc.queue.send(message)


APPLICATION_SQL = """
select a.id, a.company, a.role, a.url, a.source_site, a.stage, a.note, a.created_at,
       a.updated_at, a.resume_version_id as manual_version, a.career_os_application_id,
       a.pulled_at, a.career_os_hash, p.rv, v.kind as rv_kind, v.label as rv_label,
       v.status as rv_status, v.application_id as rv_application_id
from app.applications a
cross join lateral (select app.paired_resume(a.id) as rv) p
left join app.resume_versions v on v.id = p.rv
"""


def _application(row: Any) -> dict[str, Any]:
    resume = None
    if row["rv"] is not None:
        resume = {
            "version_id": str(row["rv"]),
            "kind": row["rv_kind"],
            "label": row["rv_label"],
            "status": row["rv_status"],
            "manual": row["manual_version"] is not None,
        }
    origin = None
    if row["career_os_application_id"] is not None:
        origin = {
            "application_id": row["career_os_application_id"],
            "pulled_at": row["pulled_at"].isoformat(),
            "content_hash": row["career_os_hash"],
        }
    return {
        "id": str(row["id"]),
        "company": row["company"],
        "role": row["role"],
        "url": row["url"],
        "source_site": row["source_site"],
        "stage": row["stage"],
        "note": row["note"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "resume": resume,
        "career_os": origin,
    }


async def application_out(
    svc: Services, conn: Conn, uid: UUID, application_id: UUID, detail: bool = False
) -> dict[str, Any] | None:
    row = await conn.fetchrow(APPLICATION_SQL + " where a.id = $1", application_id)
    if row is None:
        return None
    out = _application(row)
    index = await svc.valkey.hget(keys.pack_index(uid), f"job:{application_id}")
    out["pack"] = await pack_info(svc, uid, text(index) if index is not None else None)
    if detail:
        out["jd_text"] = await conn.fetchval(
            "select jd_text from app.applications where id = $1", application_id
        )
        out["versions"] = [
            {"id": str(r["id"]), "kind": r["kind"], "label": r["label"], "status": r["status"]}
            for r in await conn.fetch(
                "select id, kind, label, status from app.resume_versions"
                " where kind = 'base' or application_id = $1 order by kind, created_at desc",
                application_id,
            )
        ]
    return out


async def seal_tailored(
    svc: Services, conn: Conn, uid: UUID, application_id: UUID, body: str, label: str | None
) -> tuple[UUID, str]:
    version = uuid7()
    sealed, key_version = await svc.crypto.encrypt(conn, uid, "resume_versions", str(version), body)
    sha = resumes.sha256_text(body)
    await conn.execute(
        "insert into app.resume_versions (id, user_id, kind, application_id, label, source,"
        " text_enc, key_version, content_sha256, status)"
        " values ($1, $2, 'tailored', $3, $4, 'text', $5, $6, $7, 'extracting')",
        version, uid, application_id, label or "Tailored resume", sealed, key_version, sha,
    )  # fmt: skip
    return version, sha


@router.get("/applications")
async def list_applications(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(APPLICATION_SQL + " order by a.created_at desc, a.id desc")
    index = await svc.valkey.hgetall(keys.pack_index(user.user_id))
    by_field = {text(k): text(v) for k, v in index.items()}
    out: list[dict[str, Any]] = []
    for r in rows:
        item = _application(r)
        item["pack"] = await pack_info(svc, user.user_id, by_field.get(f"job:{r['id']}"))
        out.append(item)
    return {"applications": out}


@router.post("/applications", status_code=201)
async def create_application(body: ApplicationIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    uid = user.user_id
    app_id = uuid7()
    extract: tuple[UUID, str] | None = None
    async with svc.tx(user) as conn:
        await conn.execute(
            "insert into app.applications (id, user_id, company, role, url, source_site, stage,"
            " note, jd_text) values ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            app_id, uid, body.company, body.role, body.url, body.source_site, body.stage,
            body.note, body.jd_text,
        )  # fmt: skip
        if body.resume_text:
            extract = await seal_tailored(svc, conn, uid, app_id, body.resume_text,
                                          body.resume_label)  # fmt: skip
        await enqueue_packs(svc, conn, uid, app_id)
        out = await application_out(svc, conn, uid, app_id, detail=True)
    if extract is not None:
        await svc.queue.send(jobs.resume_extract(uid, *extract))
    assert out is not None
    return out


@router.get("/applications/{application_id}")
async def get_application(application_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        out = await application_out(svc, conn, user.user_id, application_id, detail=True)
    if out is None:
        raise HTTPException(404, "no such application")
    return out


@router.patch("/applications/{application_id}")
async def update_application(
    application_id: UUID, body: ApplicationPatch, svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    uid = user.user_id
    changes = body.model_dump(exclude_unset=True)
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select jd_text, resume_version_id from app.applications where id = $1 for update",
            application_id,
        )
        if row is None:
            raise HTTPException(404, "no such application")
        editable = ("company", "role", "url", "source_site", "stage", "note", "jd_text")
        fields = {k: v for k, v in changes.items() if k in editable}
        repack = "jd_text" in fields and fields["jd_text"] != row["jd_text"]
        if "resume_version_id" in changes:
            chosen = (
                None
                if changes["resume_version_id"] in ("auto", None)
                else changes["resume_version_id"]
            )
            if chosen != row["resume_version_id"]:
                if chosen is not None:
                    owner = await conn.fetchrow(
                        "select kind, application_id from app.resume_versions where id = $1",
                        chosen,
                    )
                    if owner is None:
                        raise HTTPException(404, "no such resume version")
                    if owner["kind"] == "tailored" and owner["application_id"] != application_id:
                        raise HTTPException(
                            422, "that tailored resume belongs to another application"
                        )
                await conn.execute(
                    "update app.applications set resume_version_id = $2,"
                    " pairing_rev = pairing_rev + 1, updated_at = now() where id = $1",
                    application_id, chosen,
                )  # fmt: skip
                repack = True
        if fields:
            sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
            await conn.execute(
                f"update app.applications set {sets}, updated_at = now() where id = $1",
                application_id, *fields.values(),
            )  # fmt: skip
        if repack:
            await enqueue_packs(svc, conn, uid, application_id)
        out = await application_out(svc, conn, uid, application_id, detail=True)
    assert out is not None
    return out


@router.delete("/applications/{application_id}", status_code=204)
async def delete_application(application_id: UUID, svc: SvcDep, user: UserDep) -> Response:
    """Deletes the application, its tailored resume versions (with their facts) and its
    interviews; past sessions stay, without the link."""
    uid = user.user_id
    async with svc.tx(user) as conn:
        versions = [r["id"] for r in await conn.fetch(
            "select id from app.resume_versions where application_id = $1", application_id
        )]  # fmt: skip
        interviews = [r["id"] for r in await conn.fetch(
            "select id from app.interviews where job_id = $1", application_id
        )]  # fmt: skip
        try:
            status = await conn.execute(
                "delete from app.applications where id = $1", application_id
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(409, "the application is still in use") from e
    if status == "DELETE 0":
        raise HTTPException(404, "no such application")
    fields = [f"job:{application_id}", *(f"interview:{i}" for i in interviews)]
    await svc.valkey.hdel(keys.pack_index(uid), *fields)
    if versions:
        await svc.valkey.delete(*(keys.resume_digest(uid, v) for v in versions))
    return Response(status_code=204)


# Desktop builds from before applications ------------------------------------------------


@router.post("/jobs", status_code=201)
async def create_job(body: JobIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    app_id = uuid7()
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "insert into app.applications (id, user_id, company, role, jd_text, url)"
            " values ($1, $2, $3, $4, $5, $6) returning id, company, role, url, created_at",
            app_id, user.user_id, body.company, body.title, body.description, body.url,
        )  # fmt: skip
        await enqueue_packs(svc, conn, user.user_id, app_id)
    return {**_job(row), "pack": None}


def _job(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "company": row["company"],
        "title": row["role"],
        "url": row["url"],
        "created_at": row["created_at"].isoformat(),
    }


@router.get("/jobs")
async def list_jobs(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select id, company, role, url, created_at from app.applications"
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


# Interviews ------------------------------------------------------------------------------


def _interview(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "job_id": str(row["job_id"]),
        "application_id": str(row["job_id"]),
        "round": row["round"],
        "starts_at": row["starts_at"].isoformat(),
        "pack_id": str(row["pack_id"]) if row["pack_id"] else None,
        "company": row.get("company"),
        "title": row.get("role"),
        "role": row.get("role"),
    }


@router.post("/interviews", status_code=201)
async def create_interview(body: InterviewIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    interview_id = uuid7()
    async with svc.tx(user) as conn:
        if await conn.fetchval("select 1 from app.applications where id = $1", body.job_id) is None:
            raise HTTPException(404, "no such application")
        row = await conn.fetchrow(
            "with i as (insert into app.interviews (id, user_id, job_id, round, starts_at)"
            " values ($1, $2, $3, $4, $5) returning id, job_id, round, starts_at, pack_id)"
            " select i.*, a.company, a.role from i join app.applications a on a.id = i.job_id",
            interview_id, user.user_id, body.job_id, body.round, body.starts_at,
        )  # fmt: skip
        paired = await resumes.pairing(conn, body.job_id)
    assert paired is not None
    digest = await resumes.version_digest(
        svc.valkey, user.user_id, paired.resume_version_id, paired.is_base
    )
    await svc.queue.send(
        jobs.pack_build(
            user.user_id,
            "interview",
            f"interview:{interview_id}",
            body.starts_at,
            digest.version if digest else None,
            svc.settings.embedding_model_id,
            resumes.pairing_tag(paired),
        )
    )
    return _interview(row)


@router.get("/interviews")
async def list_interviews(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select i.id, i.job_id, i.round, i.starts_at, i.pack_id, a.company, a.role"
            " from app.interviews i join app.applications a on a.id = i.job_id"
            " where i.user_id = $1 order by i.starts_at desc",
            user.user_id,
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        item = _interview(r)
        item["pack"] = await pack_info(svc, user.user_id, item["pack_id"])
        out.append(item)
    return {"interviews": out}
