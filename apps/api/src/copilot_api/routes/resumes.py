"""Resume versions (ARCHITECTURE.md, "Resume versions"): one base resume plus tailored versions,
each owned by one application.

Pasted text is sealed with the user's data key before it is stored; an uploaded file goes
through `POST /documents` (kind `resume`, with `application_id` for a tailored version or
`resume_version_id` to replace a version's content). New content enqueues `resume.extract`,
which replaces that version's facts and rebuilds its digest and the packs that use it.
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import Services, SvcDep, UserDep
from copilot_api.routes.applications import MAX_RESUME_TEXT, enqueue_packs
from copilot_core import jobs, keys, resumes
from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.packs import Digest

router = APIRouter()


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["base", "tailored"]
    application_id: UUID | None = None  # required for a tailored version
    label: str | None = Field(default=None, min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_RESUME_TEXT)


class ResumePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=200)
    text: str | None = Field(default=None, min_length=1, max_length=MAX_RESUME_TEXT)


VERSION_SQL = """
select v.id, v.kind, v.application_id, v.label, v.source, v.status, v.created_at,
       v.updated_at, v.career_os_ref, v.pulled_at, v.file_s3_key is not null as has_file,
       v.file_content_type, a.company, a.role
from app.resume_versions v left join app.applications a on a.id = v.application_id
"""


async def _digest_info(
    svc: Services, uid: UUID, version: UUID, is_base: bool
) -> dict[str, Any] | None:
    d: Digest | None = await resumes.version_digest(svc.valkey, uid, version, is_base)
    if d is None:
        return None
    return {"version": d.version, "built_at": d.built_at, "facts": len(d.refs)}


async def version_out(svc: Services, conn: Conn, uid: UUID, row: Any) -> dict[str, Any]:
    used_by = await resumes.applications_using(conn, row["id"])
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "label": row["label"],
        "source": row["source"],
        "status": row["status"],
        "application": {
            "id": str(row["application_id"]),
            "company": row["company"],
            "role": row["role"],
        }
        if row["application_id"]
        else None,
        "career_os": {"ref": row["career_os_ref"], "pulled_at": row["pulled_at"].isoformat()}
        if row["career_os_ref"] and row["pulled_at"]
        else None,
        "file": {"content_type": row["file_content_type"]} if row["has_file"] else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "used_by": [str(a) for a in used_by],
        "digest": await _digest_info(svc, uid, row["id"], row["kind"] == "base"),
    }


async def set_text(svc: Services, conn: Conn, uid: UUID, version: UUID, body: str) -> str:
    """New pasted content for an existing version: sealed, and extracted again."""
    sealed, key_version = await svc.crypto.encrypt(conn, uid, "resume_versions", str(version), body)
    sha = resumes.sha256_text(body)
    await conn.execute(
        "update app.resume_versions set source = 'text', text_enc = $2, key_version = $3,"
        " document_id = null, file_s3_key = null, file_name_enc = null,"
        " file_content_type = null, career_os_ref = null, pulled_at = null,"
        " content_sha256 = $4, status = 'extracting', updated_at = now() where id = $1",
        version, sealed, key_version, sha,
    )  # fmt: skip
    return sha


@router.get("/resumes")
async def list_resumes(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    uid = user.user_id
    async with svc.tx(user) as conn:
        rows = await conn.fetch(VERSION_SQL + " order by v.kind, v.created_at desc")
        out = [await version_out(svc, conn, uid, r) for r in rows]
    base = next((v for v in out if v["kind"] == "base"), None)
    return {"base": base, "versions": [v for v in out if v["kind"] == "tailored"]}


@router.post("/resumes", status_code=201)
async def create_resume(body: ResumeIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    """Pasted text: a tailored version of an application, or the base (replacing the base's
    content if there is one)."""
    uid = user.user_id
    if (body.kind == "tailored") != (body.application_id is not None):
        raise HTTPException(422, "a tailored resume names its application; the base names none")
    async with svc.tx(user) as conn:
        if body.application_id is not None:
            if (
                await conn.fetchval(
                    "select 1 from app.applications where id = $1", body.application_id
                )
                is None
            ):
                raise HTTPException(404, "no such application")
            version = uuid7()
            sealed, key_version = await svc.crypto.encrypt(
                conn, uid, "resume_versions", str(version), body.text
            )
            sha = resumes.sha256_text(body.text)
            await conn.execute(
                "insert into app.resume_versions (id, user_id, kind, application_id, label,"
                " source, text_enc, key_version, content_sha256, status)"
                " values ($1, $2, 'tailored', $3, $4, 'text', $5, $6, $7, 'extracting')",
                version, uid, body.application_id, body.label or "Tailored resume", sealed,
                key_version, sha,
            )  # fmt: skip
        else:
            existing = await resumes.base_version(conn, uid)
            if existing is not None:
                version = existing
                sha = await set_text(svc, conn, uid, version, body.text)
                if body.label:
                    await conn.execute(
                        "update app.resume_versions set label = $2 where id = $1", version,
                        body.label,
                    )  # fmt: skip
            else:
                version = uuid7()
                sealed, key_version = await svc.crypto.encrypt(
                    conn, uid, "resume_versions", str(version), body.text
                )
                sha = resumes.sha256_text(body.text)
                await conn.execute(
                    "insert into app.resume_versions (id, user_id, kind, label, source,"
                    " text_enc, key_version, content_sha256, status)"
                    " values ($1, $2, 'base', $3, 'text', $4, $5, $6, 'extracting')",
                    version, uid, body.label or "Base resume", sealed, key_version, sha,
                )  # fmt: skip
        row = await conn.fetchrow(VERSION_SQL + " where v.id = $1", version)
        out = await version_out(svc, conn, uid, row)
    await svc.queue.send(jobs.resume_extract(uid, version, sha))
    return out


@router.get("/resumes/{version_id}")
async def get_resume(version_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    """One version, with its text when it was pasted or pulled."""
    uid = user.user_id
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            VERSION_SQL.replace(
                "v.file_content_type,", "v.file_content_type, v.text_enc, v.key_version,"
            )
            + " where v.id = $1",
            version_id,
        )
        if row is None:
            raise HTTPException(404, "no such resume version")
        out = await version_out(svc, conn, uid, row)
        out["text"] = None
        if row["text_enc"] is not None:
            out["text"] = await svc.crypto.decrypt(
                conn, uid, "resume_versions", str(version_id), bytes(row["text_enc"]),
                int(row["key_version"]),
            )  # fmt: skip
    return out


@router.get("/resumes/{version_id}/file")
async def get_resume_file(version_id: UUID, svc: SvcDep, user: UserDep) -> Response:
    """The generated file pulled from Career-OS with this version, unsealed."""
    uid = user.user_id
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select file_s3_key, file_name_enc, file_content_type, key_version"
            " from app.resume_versions where id = $1",
            version_id,
        )
        if row is None or row["file_s3_key"] is None:
            raise HTTPException(404, "this resume version has no file")
        sealed = await svc.store.get(row["file_s3_key"])
        if sealed is None:
            raise HTTPException(404, "the file is gone")
        name = await svc.crypto.decrypt(
            conn, uid, "resume_versions", f"{version_id}:file-name", bytes(row["file_name_enc"]),
            int(row["key_version"]),
        )  # fmt: skip
        body = await svc.crypto.decrypt_bytes(
            conn, uid, "resume_versions", f"{version_id}:file", sealed, int(row["key_version"])
        )
    safe = "".join(c for c in name if c.isalnum() or c in "._- ")[:120] or "resume"
    return Response(
        body, media_type=row["file_content_type"],
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )  # fmt: skip


@router.patch("/resumes/{version_id}")
async def update_resume(
    version_id: UUID, body: ResumePatch, svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    uid = user.user_id
    sha: str | None = None
    async with svc.tx(user) as conn:
        if (
            await conn.fetchval(
                "select 1 from app.resume_versions where id = $1 for update", version_id
            )
            is None
        ):
            raise HTTPException(404, "no such resume version")
        if body.label is not None:
            await conn.execute(
                "update app.resume_versions set label = $2, updated_at = now() where id = $1",
                version_id, body.label,
            )  # fmt: skip
        if body.text is not None:
            sha = await set_text(svc, conn, uid, version_id, body.text)
        row = await conn.fetchrow(VERSION_SQL + " where v.id = $1", version_id)
        out = await version_out(svc, conn, uid, row)
    if sha is not None:
        await svc.queue.send(jobs.resume_extract(uid, version_id, sha))
    return out


@router.delete("/resumes/{version_id}", status_code=204)
async def delete_resume(version_id: UUID, svc: SvcDep, user: UserDep) -> Response:
    """Deletes the version and its facts. Applications paired with it fall back to the
    automatic pairing, and their packs are rebuilt."""
    uid = user.user_id
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select kind, file_s3_key from app.resume_versions where id = $1 for update",
            version_id,
        )
        if row is None:
            raise HTTPException(404, "no such resume version")
        affected = await resumes.applications_using(conn, version_id)
        await conn.execute(
            "update app.documents set resume_version_id = null where resume_version_id = $1",
            version_id,
        )
        await conn.execute(
            "update app.applications set pairing_rev = pairing_rev + 1 where id = any($1::uuid[])",
            affected,
        )
        await conn.execute("delete from app.resume_versions where id = $1", version_id)
        for app_id in affected:
            await enqueue_packs(svc, conn, uid, app_id)
    stale = [keys.resume_digest(uid, version_id)]
    if row["kind"] == "base":
        stale.append(keys.digest(uid))
    await svc.valkey.delete(*stale)
    if row["file_s3_key"]:
        await svc.store.delete(row["file_s3_key"])
    return Response(status_code=204)
