"""`POST /documents` (presigned upload), `GET /documents`, `POST /documents/{id}/ingest`.

The client declares the content type, size and SHA-256 up front; the upload goes straight to
S3 under `uploads/{user_id}/`. Ingest checks that the object arrived and enqueues
`memory.extract`, which checks the hash again before reading it.
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import SvcDep, UserDep
from copilot_core.ids import uuid7
from copilot_core.storage import document_key

router = APIRouter()

INGESTED_KINDS = ("resume", "job_description")  # phase 1; LinkedIn exports come later


class DocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["resume", "job_description", "linkedin_export", "other"]
    content_type: Literal["application/pdf", "text/plain", "text/markdown"]
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _doc(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "content_type": row["content_type"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
    }


@router.post("/documents", status_code=201)
async def create_document(body: DocumentIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    if body.size_bytes > svc.settings.max_document_bytes:
        raise HTTPException(413, f"documents are at most {svc.settings.max_document_bytes} bytes")
    doc_id = uuid7()
    key = document_key(user.user_id, doc_id, body.content_type)
    async with svc.tx(user) as conn:
        await conn.execute(
            "insert into app.documents (id, user_id, kind, s3_key, sha256, content_type)"
            " values ($1, $2, $3, $4, $5, $6)",
            doc_id,
            user.user_id,
            body.kind,
            key,
            body.sha256,
            body.content_type,
        )
    upload = await svc.store.presign_put(key, body.content_type, svc.settings.upload_url_ttl_s)
    return {"document_id": str(doc_id), "upload": upload}


@router.get("/documents")
async def list_documents(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select id, kind, content_type, status, created_at from app.documents"
            " where user_id = $1 order by id desc",
            user.user_id,
        )
    return {"documents": [_doc(r) for r in rows]}


@router.post("/documents/{document_id}/ingest", status_code=202)
async def ingest(document_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select id, kind, content_type, status, created_at, s3_key, sha256"
            " from app.documents where id = $1",
            document_id,
        )
    if row is None:
        raise HTTPException(404, "no such document")
    if row["kind"] not in INGESTED_KINDS:
        raise HTTPException(422, f"{row['kind']} documents are not ingested yet")
    if row["status"] == "ingested":
        return _doc(row)
    head = await svc.store.head(row["s3_key"])
    if head is None:
        raise HTTPException(409, "the document has not been uploaded yet")
    if int(head.get("ContentLength", 0)) > svc.settings.max_document_bytes:
        raise HTTPException(413, "the uploaded document is too large")
    # One extraction per uploaded content (the key also names the document: two uploads of
    # the same file are two documents, each ingested once).
    key = f"memory.extract:{user.user_id}:{document_id}:{row['sha256']}"
    await svc.enqueue("memory.extract", key, user.user_id, {"document_id": str(document_id)})
    return {**_doc(row), "status": "ingesting"}
