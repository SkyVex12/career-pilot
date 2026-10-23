"""Resume versions and their pairing with applications (ARCHITECTURE.md, "Resume versions").

A user has one base resume and tailored versions, each owned by one application. An
application's interview packs use its paired version (`app.paired_resume`: the user's choice,
else its newest tailored version, else the base) and nothing else: the version's own digest
(`digest:{uid}:{version}`) and its own memory nodes. Shared by the API, the workers and the
gateway, so all three resolve a pairing and name a pack build the same way.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from copilot_core import jobs, keys
from copilot_core.db import Conn
from copilot_core.packs import Digest
from copilot_core.valkey_client import Valkey


@dataclass(frozen=True)
class Pairing:
    application_id: UUID
    resume_version_id: UUID | None  # None: the user has no resume yet
    is_base: bool
    manual: bool  # the user chose it, rather than the automatic rule
    jd_sha256: str
    rev: int  # applications.pairing_rev


def sha256_text(text: str) -> str:
    """The content hash of a JD or of a resume's text (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def pairing(conn: Conn, application_id: UUID) -> Pairing | None:
    """The application's resume pairing, or None if it is not this user's."""
    row = await conn.fetchrow(
        "select a.id, a.jd_text, a.pairing_rev, a.resume_version_id is not null as manual,"
        " p.rv, v.kind from app.applications a"
        " cross join lateral (select app.paired_resume(a.id) as rv) p"
        " left join app.resume_versions v on v.id = p.rv"
        " where a.id = $1",
        application_id,
    )
    if row is None:
        return None
    return Pairing(
        application_id=row["id"],
        resume_version_id=row["rv"],
        is_base=row["kind"] == "base",
        manual=bool(row["manual"]),
        jd_sha256=sha256_text(str(row["jd_text"])),
        rev=int(row["pairing_rev"]),
    )


def pairing_tag(p: Pairing) -> str:
    """The part of an application pack's idempotency key that names its pairing."""
    rv = p.resume_version_id.hex[:12] if p.resume_version_id else "none"
    return f"{rv}:{p.jd_sha256[:8]}:r{p.rev}"


async def version_digest(
    valkey: Valkey, uid: UUID, resume_version_id: UUID | None, is_base: bool
) -> Digest | None:
    """A version's digest. The base's may still sit only at `digest:{uid}` (written before
    resume versions); a tailored version's never falls back, so it never gets the base's."""
    if resume_version_id is None:
        return None
    raw = await valkey.get(keys.resume_digest(uid, resume_version_id))
    if raw is None and is_base:
        raw = await valkey.get(keys.digest(uid))
    return None if raw is None else Digest.model_validate_json(raw)


async def pack_messages(
    conn: Conn,
    valkey: Valkey,
    uid: UUID,
    application_id: UUID,
    embedding_model: str | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """`pack.build` for an application and its upcoming interviews, for its current pairing:
    what to enqueue after the pairing, the JD or the paired resume changed."""
    p = await pairing(conn, application_id)
    if p is None:
        return []
    now = now or datetime.now(UTC)
    digest = await version_digest(valkey, uid, p.resume_version_id, p.is_base)
    version = digest.version if digest else None
    tag = pairing_tag(p)
    out = [
        jobs.pack_build(
            uid, "interview", f"job:{application_id}", now, version, embedding_model, tag
        )
    ]
    for row in await conn.fetch(
        "select id, starts_at from app.interviews"
        " where job_id = $1 and starts_at > now() - interval '2 hours'",
        application_id,
    ):
        out.append(
            jobs.pack_build(
                uid,
                "interview",
                f"interview:{row['id']}",
                row["starts_at"],
                version,
                embedding_model,
                tag,
            )
        )
    return out


async def applications_using(conn: Conn, resume_version_id: UUID) -> list[UUID]:
    """Applications whose interview packs use this version."""
    rows = await conn.fetch(
        "select id from app.applications where app.paired_resume(id) = $1 order by id",
        resume_version_id,
    )
    return [r["id"] for r in rows]


async def base_version(conn: Conn, uid: UUID) -> UUID | None:
    return await conn.fetchval(
        "select id from app.resume_versions where user_id = $1 and kind = 'base'", uid
    )


async def attach_document(
    conn: Conn,
    uid: UUID,
    document_id: UUID,
    sha256: str,
    application_id: UUID | None,
    resume_version_id: UUID | None,
    label: str | None,
) -> UUID:
    """Make an uploaded resume file a version's content: the version it names, else a new
    tailored version of its application, else the base (created on first upload). Returns the
    version, now `extracting` that content."""
    from copilot_core.ids import uuid7

    replace = (
        "update app.resume_versions set source = 'file', document_id = $2, text_enc = null,"
        " key_version = null, file_s3_key = null, file_name_enc = null,"
        " file_content_type = null, career_os_ref = null, pulled_at = null,"
        " content_sha256 = $3, status = 'extracting', label = coalesce($4, label),"
        " updated_at = now() where id = $1 returning id"
    )
    target = resume_version_id
    if target is None and application_id is None:
        target = await base_version(conn, uid)
    if target is not None:
        done = await conn.fetchval(replace, target, document_id, sha256, label)
        if done is not None:
            await conn.execute(
                "update app.documents set resume_version_id = $2 where id = $1", document_id, done
            )
            return done
    new_id = uuid7()
    kind = "tailored" if application_id is not None else "base"
    await conn.execute(
        "insert into app.resume_versions (id, user_id, kind, application_id, label, source,"
        " document_id, content_sha256, status) values ($1, $2, $3, $4, $5, 'file', $6, $7,"
        " 'extracting')",
        new_id, uid, kind, application_id,
        label or ("Base resume" if kind == "base" else "Tailored resume"), document_id, sha256,
    )  # fmt: skip
    await conn.execute(
        "update app.documents set resume_version_id = $2 where id = $1", document_id, new_id
    )
    return new_id
