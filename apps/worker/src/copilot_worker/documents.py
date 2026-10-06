"""`memory.extract` for uploaded documents, as far as phase 1 needs it.

- A resume becomes career facts in `memory_nodes` (Opus), then the digest is rebuilt.
- A job description becomes a row in `app.jobs` with the document's id (Sonnet reads the
  company, title and summary), then that job's session pack is built.

Claude reads PDFs directly, so there is no document parser. The object's SHA-256 must match
what the client declared when it asked for the upload URL.
"""

import base64
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from copilot_core import jobs
from copilot_core.llm import LLMRequest, SystemBlock
from copilot_worker import prompts
from copilot_worker.context import Ctx
from copilot_worker.llm import complete_json
from copilot_worker.memory import memory_version, replace_resume_nodes
from copilot_worker.packs import current_digest
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
            "select kind, s3_key, sha256, status, content_type from app.documents where id = $1",
            doc_id,
        )
    if doc is None or doc["status"] == "ingested":
        return
    body = await ctx.store.get(doc["s3_key"])
    if body is None or hashlib.sha256(body).hexdigest() != doc["sha256"]:
        log.warning("document %s: missing or not the declared content", doc_id)
        await set_status(ctx, uid, doc_id, "failed")
        return
    content_type = doc["content_type"] or "text/plain"
    if doc["kind"] == "resume":
        await ingest_resume(ctx, uid, doc_id, body, content_type)
    elif doc["kind"] == "job_description":
        await ingest_job_description(ctx, uid, doc_id, body, content_type)
    else:
        log.warning("document %s: kind %s is not ingested in phase 1", doc_id, doc["kind"])
        await set_status(ctx, uid, doc_id, "failed")


async def ingest_resume(ctx: Ctx, uid: UUID, doc_id: UUID, body: bytes, content_type: str) -> None:
    req = LLMRequest(
        system=[SystemBlock(prompts.EXTRACT_SYSTEM)],
        content=[
            document_block(body, content_type, "Resume"),
            {"type": "text", "text": "Extract the career facts from this resume as JSON."},
        ],
        max_tokens=32000,
        json_schema=prompts.EXTRACT_SCHEMA,
    )
    data = await complete_json(ctx.router, "extract", req)
    async with ctx.user(uid) as conn:
        status = await conn.fetchval(
            "select status from app.documents where id = $1 for update", doc_id
        )
        if status == "ingested":  # a parallel run finished first
            return
        count = await replace_resume_nodes(conn, uid, doc_id, data)
        await conn.execute("update app.documents set status = 'ingested' where id = $1", doc_id)
        version = await memory_version(conn, uid)
    log.info("resume %s: %d memory nodes", doc_id, count)
    await ctx.enqueue("digest.rebuild", f"digest.rebuild:{uid}:{version}", uid, {})


async def ingest_job_description(
    ctx: Ctx, uid: UUID, doc_id: UUID, body: bytes, content_type: str
) -> None:
    req = LLMRequest(
        system=[SystemBlock(prompts.JOB_SYSTEM)],
        content=[
            document_block(body, content_type, "Job description"),
            {"type": "text", "text": "Read this job description and return the JSON."},
        ],
        max_tokens=16000,
        json_schema=prompts.JOB_SCHEMA,
    )
    data = await complete_json(ctx.router, "job", req)
    # A text file is the description as written; a PDF's text comes from the model.
    description = body.decode("utf-8") if content_type != "application/pdf" else data["description"]
    async with ctx.user(uid) as conn:
        await conn.execute(
            "insert into app.jobs (id, user_id, company, title, description)"
            " values ($1, $2, $3, $4, $5) on conflict (id) do nothing",
            doc_id,
            uid,
            data["company"].strip() or "Unknown company",
            data["title"].strip() or "Unknown role",
            description,
        )
        await conn.execute("update app.documents set status = 'ingested' where id = $1", doc_id)
    digest = await current_digest(ctx, uid)
    await ctx.queue.send(
        jobs.pack_build(
            uid, "interview", f"job:{doc_id}", datetime.now(UTC), digest.version if digest else None
        )
    )
