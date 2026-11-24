"""`memory.extract` for uploaded documents, and `resume.extract` for resume versions.

- A resume version's content (an uploaded file, pasted text, or a copy pulled from Career-OS)
  becomes that version's career facts in `memory_nodes` (Opus), then that version's digest is
  rebuilt (ARCHITECTURE.md, "Resume versions").
- A job description becomes an application with the document's id (Sonnet reads the company,
  role and text), or fills the JD of the application it was uploaded for; then that
  application's session packs are built.

Claude reads PDFs directly for fact extraction. A separate bounded parser retains original file
text for source citations. The object's SHA-256 must match
what the client declared when it asked for the upload URL.
"""

import base64
import hashlib
import logging
from typing import Any
from uuid import UUID

from copilot_core import jobs, resumes
from copilot_worker import prompts
from copilot_worker.context import Ctx
from copilot_worker.llm import complete_json
from copilot_worker.memory import memory_version, replace_resume_nodes
from copilot_worker.resume_text import original_text
from copilot_worker.runtime import Job, handler

log = logging.getLogger(__name__)


def document_block(body: bytes, content_type: str, title: str) -> dict[str, Any]:
    if content_type == "application/pdf":
        data = base64.standard_b64encode(body).decode("ascii")
        source: dict[str, Any] = {"type": "base64", "media_type": "application/pdf", "data": data}
    else:
        source = {"type": "text", "media_type": "text/plain", "data": body.decode("utf-8")}
    return {"type": "document", "title": title, "source": source}


async def set_status(ctx: Ctx, uid: UUID, doc_id: UUID, status: str) -> None:
    async with ctx.user(uid) as conn:
        await conn.execute("update app.documents set status = $2 where id = $1", doc_id, status)


@handler("memory.extract")
async def memory_extract(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    doc_id = UUID(job.payload["document_id"])
    async with ctx.user(uid) as conn:
        doc = await conn.fetchrow(
            "select kind, s3_key, sha256, status, content_type, application_id,"
            " resume_version_id, label from app.documents where id = $1",
            doc_id,
        )
    if doc is None or doc["status"] == "ingested":
        return
    if doc["kind"] == "resume":
        # Sent by an API from before resume versions, which did not attach the file to one.
        async with ctx.user(uid) as conn:
            version = await resumes.attach_document(
                conn, uid, doc_id, doc["sha256"], doc["application_id"],
                doc["resume_version_id"], doc["label"],
            )  # fmt: skip
        await extract_version(ctx, uid, version, str(doc["sha256"]))
        return
    body = await ctx.store.get(doc["s3_key"])
    if body is None or hashlib.sha256(body).hexdigest() != doc["sha256"]:
        log.warning("document %s: missing or not the declared content", doc_id)
        await set_status(ctx, uid, doc_id, "failed")
        return
    content_type = doc["content_type"] or "text/plain"
    if doc["kind"] == "job_description":
        await ingest_job_description(ctx, uid, doc_id, body, content_type, doc["application_id"])
    else:
        log.warning("document %s: kind %s is not ingested in phase 1", doc_id, doc["kind"])
        await set_status(ctx, uid, doc_id, "failed")


@handler("resume.extract")
async def resume_extract(job: Job, ctx: Ctx) -> None:
    await extract_version(
        ctx,
        job.uid,
        UUID(job.payload["resume_version_id"]),
        str(job.payload["content_sha256"]),
        UUID(str(job.payload["resume_revision_id"]))
        if job.payload.get("resume_revision_id")
        else None,
    )


async def version_content(ctx: Ctx, uid: UUID, row: Any) -> dict[str, Any] | None:
    """The version's content as a document block for the model, or None if it is missing or
    not what the version says it is."""
    label = "Resume"  # the user's label stays out of the prompt
    text = ""
    if row["text_enc"] is not None:
        async with ctx.user(uid) as conn:
            text = await ctx.crypto.decrypt(
                conn, uid, "resume_versions", str(row["id"]), bytes(row["text_enc"]),
                int(row["key_version"]),
            )  # fmt: skip
    if text.strip():
        if resumes.sha256_text(text) != row["content_sha256"]:
            return None
        return document_block(text.encode("utf-8"), "text/plain", label)
    if row["file_s3_key"] is not None:
        # Pulled from Career-OS without text: its generated file, if the model can read it.
        if row["file_content_type"] not in ("application/pdf", "text/plain", "text/markdown"):
            return None
        sealed = await ctx.store.get(row["file_s3_key"])
        if sealed is None:
            return None
        async with ctx.user(uid) as conn:
            body = await ctx.crypto.decrypt_bytes(
                conn, uid, "resume_versions", f"{row['id']}:file", sealed, int(row["key_version"])
            )
        if hashlib.sha256(body).hexdigest() != row["content_sha256"]:
            return None
        return document_block(body, row["file_content_type"], label)
    if row["s3_key"] is None:
        return None
    body = await ctx.store.get(row["s3_key"])
    if body is None or hashlib.sha256(body).hexdigest() != row["doc_sha"]:
        return None
    return document_block(body, row["content_type"] or "text/plain", label)


async def persist_original(
    ctx: Ctx,
    uid: UUID,
    version_id: UUID,
    sha: str,
    block: dict[str, Any],
    revision_id: UUID | None = None,
) -> None:
    text = await original_text(block)
    async with ctx.user(uid) as conn:
        current = await conn.fetchrow(
            "select content_sha256,text_enc,current_revision_id"
            " from app.resume_versions where id=$1 for update",
            version_id,
        )
        if (
            current is None
            or current["content_sha256"] != sha
            or current["text_enc"] is not None
            or (revision_id is not None and current["current_revision_id"] != revision_id)
        ):
            return
        sealed, key = (None, None)
        if text:
            sealed, key = await ctx.crypto.encrypt(
                conn, uid, "resume_versions:original", str(version_id), text
            )
        await conn.execute(
            "update app.resume_versions set original_text_enc=$2,original_text_key_version=$3,"
            "original_content_sha256=$4 where id=$1",
            version_id,
            sealed,
            key,
            sha,
        )


@handler("resume.original")
async def backfill_original(job: Job, ctx: Ctx) -> None:
    uid, version_id, sha = (
        job.uid,
        UUID(job.payload["resume_version_id"]),
        str(job.payload["content_sha256"]),
    )
    expected_revision = job.payload.get("resume_revision_id")
    async with ctx.user(uid) as conn:
        row = await conn.fetchrow(
            "select v.*,d.s3_key,d.sha256 as doc_sha,d.content_type from app.resume_versions v "
            "left join app.documents d on d.id=v.document_id where v.id=$1",
            version_id,
        )
    if (
        row is None
        or row["content_sha256"] != sha
        or (expected_revision and str(row["current_revision_id"]) != expected_revision)
        or row["text_enc"] is not None
        or row["original_content_sha256"] == sha
    ):
        return
    block = await version_content(ctx, uid, row)
    if block is not None:
        await persist_original(ctx, uid, version_id, sha, block, row["current_revision_id"])


async def extract_version(
    ctx: Ctx,
    uid: UUID,
    version_id: UUID,
    sha: str,
    revision_id: UUID | None = None,
) -> None:
    """One version's content into its own memory nodes, then its digest. Content that has
    been replaced since the job was sent is skipped (the newer content has its own job)."""
    async with ctx.user(uid) as conn:
        row = await conn.fetchrow(
            "select v.id, v.label, v.text_enc, v.key_version, v.content_sha256,"
            " v.extracted_sha256, v.status, v.document_id, v.file_s3_key, v.file_content_type,"
            " v.original_content_sha256,v.current_revision_id,"
            " d.s3_key, d.sha256 as doc_sha,"
            " d.content_type from app.resume_versions v"
            " left join app.documents d on d.id = v.document_id where v.id = $1",
            version_id,
        )
    if (
        row is None
        or row["content_sha256"] != sha
        or (revision_id is not None and row["current_revision_id"] != revision_id)
    ):
        return
    # Even a legacy message captures the source once, before external model work.
    revision_id = row["current_revision_id"]
    already_extracted = row["status"] == "extracted" and row["extracted_sha256"] == sha
    original_missing = row["text_enc"] is None and dict(row).get("original_content_sha256") != sha
    if already_extracted and not original_missing:
        async with ctx.user(uid) as conn:
            current = await conn.fetchval(
                "select current_revision_id from app.resume_versions where id=$1 for share",
                version_id,
            )
            if current == revision_id:
                version = await memory_version(conn, uid, version_id)
                await ctx.queue.send(
                    jobs.digest_rebuild(
                        uid,
                        version_id,
                        version,
                        resume_revision_id=revision_id,
                    )
                )
        return
    block = await version_content(ctx, uid, row)
    if block is None:
        log.warning("resume version %s: content missing or not as declared", version_id)
        async with ctx.user(uid) as conn:
            changed = await conn.fetchval(
                "update app.resume_versions set status = 'failed', updated_at = now()"
                " where id = $1 and content_sha256 = $2 and current_revision_id=$3 returning id",
                version_id, sha, revision_id,
            )  # fmt: skip
            if changed is not None and row["document_id"] is not None:
                await conn.execute(
                    "update app.documents set status = 'failed' where id = $1", row["document_id"]
                )
        return
    if original_missing:
        await persist_original(ctx, uid, version_id, sha, block, revision_id)
    if already_extracted:
        return
    data = await complete_json(ctx.router, "extract", prompts.resume_request(block))
    async with ctx.user(uid) as conn:
        current = await conn.fetchrow(
            "select content_sha256,current_revision_id from app.resume_versions"
            " where id = $1 for update",
            version_id,
        )
        if (
            current is None
            or current["content_sha256"] != sha
            or current["current_revision_id"] != revision_id
        ):
            return
        count = await replace_resume_nodes(conn, uid, version_id, data)
        await conn.execute(
            "update app.resume_versions set status = 'extracted', extracted_sha256 = $2,"
            " updated_at = now() where id = $1",
            version_id, sha,
        )  # fmt: skip
        if row["document_id"] is not None:
            await conn.execute(
                "update app.documents set status = 'ingested' where id = $1", row["document_id"]
            )
        version = await memory_version(conn, uid, version_id)
        await ctx.queue.send(
            jobs.digest_rebuild(
                uid,
                version_id,
                version,
                resume_revision_id=revision_id,
            )
        )
    log.info("resume version %s: %d memory nodes", version_id, count)


async def ingest_job_description(
    ctx: Ctx, uid: UUID, doc_id: UUID, body: bytes, content_type: str, application_id: UUID | None
) -> None:
    req = prompts.job_request(document_block(body, content_type, "Job description"))
    data = await complete_json(ctx.router, "job", req)
    # A text file is the description as written; a PDF's text comes from the model.
    description = body.decode("utf-8") if content_type != "application/pdf" else data["description"]
    async with ctx.user(uid) as conn:
        if application_id is not None:
            # The JD of an application the user entered: its company and role stay theirs.
            await conn.execute(
                "update app.applications set jd_text = $2, updated_at = now() where id = $1",
                application_id, description,
            )  # fmt: skip
            target = application_id
        else:
            await conn.execute(
                "insert into app.applications (id, user_id, company, role, jd_text)"
                " values ($1, $2, $3, $4, $5) on conflict (id) do nothing",
                doc_id,
                uid,
                data["company"].strip() or "Unknown company",
                data["title"].strip() or "Unknown role",
                description,
            )
            target = doc_id
        await conn.execute("update app.documents set status = 'ingested' where id = $1", doc_id)
        messages = await resumes.pack_messages(
            conn, ctx.valkey, uid, target, ctx.settings.embedding_model_id
        )
        for message in messages:
            await ctx.queue.send(message)
