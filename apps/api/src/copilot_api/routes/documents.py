"""`POST /documents` (presigned upload), `GET /documents`, `POST /documents/{id}/ingest`.

The client declares the content type, size and SHA-256 up front; the upload goes straight to
S3 under `uploads/{user_id}/`. Ingest checks that the object arrived, marks the document
`ingesting` and enqueues `memory.extract`, which checks the hash again before reading it and
ends as `ingested` or `failed`.

A resume upload is a resume version's content (ARCHITECTURE.md, "Resume versions"): with
`application_id` a new tailored version of that application, with `resume_version_id` new
content for that version, with neither the base. Ingest attaches it to its version and
enqueues `resume.extract`. A job description upload with `application_id` fills that
application's JD; without, it becomes a new application.

`GET /documents` also says what the documents have produced so far: the base resume's hot
digest, and for each job description the application it became. Packs are on
`GET /applications`.
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import SvcDep, UserDep
from copilot_core import jobs, keys, resumes
from copilot_core.ids import uuid7
from copilot_core.packs import Digest
from copilot_core.storage import document_key

router = APIRouter()

INGESTED_KINDS = ("resume", "job_description")  # phase 1; LinkedIn exports come later


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["resume", "job_description", "linkedin_export", "other"]
    content_type: Literal["application/pdf", "text/plain", "text/markdown"]
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    application_id: UUID | None = None
    resume_version_id: UUID | None = None
    label: str | None = Field(default=None, min_length=1, max_length=200)


def _doc(row: Any) -> dict[str, Any]:
    job_id = row.get("job_id")  # asyncpg records have .get too
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "content_type": row["content_type"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
        "job_id": str(job_id) if job_id else None,  # a job description's application
        "application_id": str(job_id) if job_id else None,
        "resume_version_id": str(row["resume_version_id"])
        if row.get("resume_version_id")
        else None,
    }


@router.post("/documents", status_code=201)
async def create_document(body: DocumentIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    if body.size_bytes > svc.settings.max_document_bytes:
        raise HTTPException(413, f"documents are at most {svc.settings.max_document_bytes} bytes")
    if body.kind not in ("resume", "job_description") and (
        body.application_id or body.resume_version_id
    ):
        raise HTTPException(422, "only a resume or a job description is for an application")
    if body.kind == "job_description" and body.resume_version_id:
        raise HTTPException(422, "a job description is not a resume version's content")
    doc_id = uuid7()
    key = document_key(user.user_id, doc_id, body.content_type)
    async with svc.tx(user) as conn:
        if (
            body.application_id is not None
            and await conn.fetchval(
                "select 1 from app.applications where id = $1", body.application_id
            )
            is None
        ):
            raise HTTPException(404, "no such application")
        if body.resume_version_id is not None:
            owner = await conn.fetchval(
                "select application_id from app.resume_versions where id = $1",
                body.resume_version_id,
            )
            exists = await conn.fetchval(
                "select 1 from app.resume_versions where id = $1", body.resume_version_id
            )
            if exists is None:
                raise HTTPException(404, "no such resume version")
            if body.application_id is not None and owner != body.application_id:
                raise HTTPException(422, "that resume version belongs to another application")
        await conn.execute(
            "insert into app.documents (id, user_id, kind, s3_key, sha256, content_type,"
            " application_id, resume_version_id, label) values ($1, $2, $3, $4, $5, $6, $7,"
            " $8, $9)",
            doc_id, user.user_id, body.kind, key, body.sha256, body.content_type,
            body.application_id, body.resume_version_id, body.label,
        )  # fmt: skip
    upload = await svc.store.presign_put(key, body.content_type, svc.settings.upload_url_ttl_s)
    return {"document_id": str(doc_id), "upload": upload}


@router.get("/documents")
async def list_documents(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select d.id, d.kind, d.content_type, d.status, d.created_at,"
            " coalesce(a.id, d.application_id) as job_id, d.resume_version_id"
            " from app.documents d left join app.applications a on a.id = d.id"
            " where d.user_id = $1 order by d.id desc",
            user.user_id,
        )
    raw = await svc.valkey.get(keys.digest(user.user_id))
    digest = None
    if raw is not None:
        d = Digest.model_validate_json(raw)
        digest = {"version": d.version, "built_at": d.built_at, "facts": len(d.refs)}
    return {"documents": [_doc(r) for r in rows], "digest": digest}


@router.post("/documents/{document_id}/ingest", status_code=202)
async def ingest(document_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select id, kind, content_type, status, created_at, s3_key, sha256,"
            " application_id, resume_version_id, label from app.documents where id = $1",
            document_id,
        )
    if row is None:
        raise HTTPException(404, "no such document")
    if row["kind"] not in INGESTED_KINDS:
        raise HTTPException(422, f"{row['kind']} documents are not ingested yet")
    if row["status"] in ("ingested", "ingesting"):
        return _doc(row)
    if row["status"] == "failed":
        raise HTTPException(409, "this document could not be read; upload it again")
    head = await svc.store.head(row["s3_key"])
    if head is None:
        raise HTTPException(409, "the document has not been uploaded yet")
    if int(head.get("ContentLength", 0)) > svc.settings.max_document_bytes:
        raise HTTPException(413, "the uploaded document is too large")
    async with svc.tx(user) as conn:
        updated = await conn.execute(
            "update app.documents set status = 'ingesting' where id = $1 and status = 'uploaded'",
            document_id,
        )
        version = None
        if row["kind"] == "resume" and updated == "UPDATE 1":
            # The file becomes its version's content; resume.extract reads it from there.
            version = await resumes.attach_document(
                conn, user.user_id, document_id, row["sha256"], row["application_id"],
                row["resume_version_id"], row["label"],
            )  # fmt: skip
    if row["kind"] == "resume":
        if version is not None:
            await svc.queue.send(jobs.resume_extract(user.user_id, version, row["sha256"]))
        return {**_doc(row), "status": "ingesting",
                "resume_version_id": str(version) if version else None}  # fmt: skip
    # One extraction per uploaded content (the key also names the document: two uploads of
    # the same file are two documents, each ingested once).
    key = f"memory.extract:{user.user_id}:{document_id}:{row['sha256']}"
    await svc.enqueue("memory.extract", key, user.user_id, {"document_id": str(document_id)})
    return {**_doc(row), "status": "ingesting"}
