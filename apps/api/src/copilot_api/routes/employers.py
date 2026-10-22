"""Company workspaces (ARCHITECTURE.md, "REST API", company workspaces).

- Employers: the companies the user works at, apart from the jobs they apply to. Deleting one
  deletes everything for that company (logs, documents, workplace memory, its standup and
  meeting sessions) and enqueues `employer.purge` for its files and packs.
- Work logs: one entry per company and day. Saving one seals its text with the user's data
  key and enqueues `worklog.extract`; its attachments are sealed files under the company.
- Company documents: uploaded through the API (not a presigned URL) so the file is sealed
  before it is stored; `company_doc.extract` turns it into proposals.
- Memory and proposals: the company's facts, to view, edit and archive; proposals from
  documents, approved or rejected one by one or per document.
- Packs: picking a company on the start screen asks for its standup or meeting pack, built
  only when the company's memory changed since the last one.
"""

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import Services, SvcDep, UserDep
from copilot_core import jobs, keys
from copilot_core.auth import Principal
from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.packs import SessionPack
from copilot_core.storage import company_document_key
from copilot_core.valkey_client import text
from copilot_core.workplace import WORKPLACE_KINDS, local_today, memory_version

router = APIRouter()

CONTENT_TYPES = ("application/pdf", "text/plain", "text/markdown")
MAX_LOG_CHARS = 50_000
MAX_ATTACHMENTS = 10


class EmployerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=200)
    team: str = Field(default="", max_length=200)
    started_on: date | None = None
    ended_on: date | None = None


class EmployerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=200)
    team: str | None = Field(default=None, max_length=200)
    started_on: date | None = None
    ended_on: date | None = None
    status: Literal["active", "archived"] | None = None


class LogIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_LOG_CHARS)


class NodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str | None = Field(default=None, min_length=1, max_length=1000)
    body: dict[str, Any] | None = None  # merged into the node's body


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str | None = Field(default=None, min_length=1, max_length=1000)


class BulkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject"]
    ids: list[UUID] = Field(default_factory=list[UUID], max_length=500)
    company_document_id: UUID | None = None  # every pending proposal of this document


# --- helpers ------------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _employer(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "title": row["title"],
        "team": row["team"],
        "started_on": _iso(row["started_on"]),
        "ended_on": _iso(row["ended_on"]),
        "status": row["status"],
        "current": row["status"] == "active" and row["ended_on"] is None,
        "created_at": _iso(row["created_at"]),
    }


async def _require_employer(conn: Conn, employer_id: UUID) -> Any:
    row = await conn.fetchrow(
        "select id, name, title, team, started_on, ended_on, status, created_at"
        " from app.employers where id = $1",
        employer_id,
    )
    if row is None:
        raise HTTPException(404, "no such company")
    return row


def _body(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


def _node(row: Any) -> dict[str, Any]:
    body = _body(row["body"])
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "title": row["title"],
        "summary": row["summary"],
        "body": body,
        "day": _iso(row["valid_from"]),
        "status": row["status"],
        "source": _body(row["source"]),
        "edited": body.get("edited") is True,
    }


NODE_COLUMNS = "id, kind, title, summary, body, valid_from, status, source"


def content_sha(log_text: str, attachment_shas: list[str]) -> str:
    """A log's content: its text and its attachments. Extraction runs once per content."""
    h = hashlib.sha256(log_text.encode("utf-8"))
    for sha in sorted(attachment_shas):
        h.update(b"\n" + sha.encode("ascii"))
    return h.hexdigest()


async def _refresh(svc: Services, conn: Conn, user: Principal, employer_id: UUID) -> None:
    """The company's memory changed: embed what is new and rebuild its packs."""
    version = await memory_version(conn, user.user_id, employer_id)
    await svc.queue.send(jobs.workplace_refresh(user.user_id, employer_id, version))


async def _read_upload(
    request: Request, svc: Services, content_type: str | None, file_name: str | None
) -> tuple[bytes, str, str]:
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind not in CONTENT_TYPES:
        raise HTTPException(415, f"files are {', '.join(CONTENT_TYPES)}")
    declared = int(request.headers.get("content-length") or 0)
    if declared > svc.settings.max_document_bytes:
        raise HTTPException(413, f"files are at most {svc.settings.max_document_bytes} bytes")
    body = await request.body()
    if not body:
        raise HTTPException(422, "the file is empty")
    if len(body) > svc.settings.max_document_bytes:
        raise HTTPException(413, f"files are at most {svc.settings.max_document_bytes} bytes")
    if kind != "application/pdf":
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(422, "a text file must be UTF-8") from e
    name = unquote(file_name or "").strip()[:255] or "document"
    return body, kind, name


async def _store_document(
    svc: Services,
    conn: Conn,
    user: Principal,
    employer_id: UUID,
    body: bytes,
    content_type: str,
    name: str,
    work_log_id: UUID | None,
) -> dict[str, Any]:
    doc_id = uuid7()
    key = company_document_key(user.user_id, employer_id, doc_id)
    sealed, version = await svc.crypto.encrypt_bytes(
        conn, user.user_id, "company_documents", str(doc_id), body
    )
    name_enc, _ = await svc.crypto.encrypt(
        conn, user.user_id, "company_documents", str(doc_id), name
    )
    await svc.store.put(key, sealed, "application/octet-stream")
    sha = hashlib.sha256(body).hexdigest()
    status = "ingesting"
    await conn.execute(
        "insert into app.company_documents (id, user_id, employer_id, work_log_id, name_enc,"
        " key_version, s3_key, sha256, content_type, size_bytes, status)"
        " values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
        doc_id,
        user.user_id,
        employer_id,
        work_log_id,
        name_enc,
        version,
        key,
        sha,
        content_type,
        len(body),
        status,
    )
    return {
        "id": str(doc_id),
        "name": name,
        "content_type": content_type,
        "size_bytes": len(body),
        "status": status,
        "sha256": sha,
    }


async def _doc_name(svc: Services, conn: Conn, user: Principal, row: Any) -> str:
    return await svc.crypto.decrypt(
        conn, user.user_id, "company_documents", str(row["id"]), row["name_enc"],
        row["key_version"],
    )  # fmt: skip


# --- employers ----------------------------------------------------------------------------


@router.get("/employers")
async def list_employers(svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            "select e.id, e.name, e.title, e.team, e.started_on, e.ended_on, e.status,"
            " e.created_at,"
            " (select count(*) from app.work_logs w where w.employer_id = e.id) as logs,"
            " (select max(day) from app.work_logs w where w.employer_id = e.id) as last_log,"
            " (select count(*) from app.company_documents d where d.employer_id = e.id"
            "  and d.work_log_id is null) as documents,"
            " (select count(*) from app.memory_proposals p where p.employer_id = e.id"
            "  and p.status = 'pending') as pending"
            " from app.employers e where e.user_id = $1"
            " order by (e.status = 'active' and e.ended_on is null) desc, e.created_at desc",
            user.user_id,
        )
    return {
        "employers": [
            {
                **_employer(r),
                "logs": r["logs"],
                "last_log": _iso(r["last_log"]),
                "documents": r["documents"],
                "pending_proposals": r["pending"],
            }
            for r in rows
        ]
    }


@router.post("/employers", status_code=201)
async def create_employer(body: EmployerIn, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "insert into app.employers (id, user_id, name, title, team, started_on, ended_on)"
            " values ($1, $2, $3, $4, $5, $6, $7)"
            " returning id, name, title, team, started_on, ended_on, status, created_at",
            uuid7(),
            user.user_id,
            body.name.strip(),
            body.title.strip(),
            body.team.strip(),
            body.started_on,
            body.ended_on,
        )
    return _employer(row)


@router.patch("/employers/{employer_id}")
async def update_employer(
    employer_id: UUID, body: EmployerPatch, svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        if fields:
            sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
            await conn.execute(
                f"update app.employers set {sets}, updated_at = now() where id = $1",
                employer_id,
                *[v.strip() if isinstance(v, str) else v for v in fields.values()],
            )
        row = await _require_employer(conn, employer_id)
        if {"name", "title", "team"} & set(fields):
            await _refresh(svc, conn, user, employer_id)  # the packs show these
    return _employer(row)


async def _live_at(svc: Services, user: Principal, employer_id: UUID) -> bool:
    sid = await svc.valkey.get(keys.user_active_session(user.user_id))
    if sid is None:
        return False
    raw = await svc.valkey.hgetall(keys.sess_state(text(sid)))
    state = {text(k): text(v) for k, v in raw.items()}
    return state.get("employer_id") == str(employer_id) and state.get("status") != "ended"


@router.delete("/employers/{employer_id}", status_code=202)
async def delete_employer(
    employer_id: UUID, svc: SvcDep, user: UserDep, confirm: Annotated[str, Query()] = ""
) -> dict[str, Any]:
    """Delete all data for this company. `confirm` must be the company's name."""
    async with svc.tx(user) as conn:
        row = await _require_employer(conn, employer_id)
    if confirm.strip().casefold() != str(row["name"]).strip().casefold():
        raise HTTPException(422, "confirm with the company's name to delete all its data")
    if await _live_at(svc, user, employer_id):
        raise HTTPException(409, "a call at this company is live; end it first")
    async with svc.tx(user) as conn:
        counts = await conn.fetchrow(
            "select (select count(*) from app.work_logs where employer_id = $1) as logs,"
            " (select count(*) from app.company_documents where employer_id = $1) as documents,"
            " (select count(*) from app.memory_nodes where employer_id = $1) as facts,"
            " (select count(*) from app.sessions where employer_id = $1) as sessions",
            employer_id,
        )
        sessions = [
            r["id"]
            for r in await conn.fetch(
                "select id from app.sessions where employer_id = $1", employer_id
            )
        ]
        await conn.execute("delete from app.employers where id = $1", employer_id)  # cascades
    for sid in sessions:
        await svc.valkey.delete(keys.sess_state(sid), keys.sess_events(sid), keys.sess_log(sid))
    await svc.enqueue(
        "employer.purge", f"employer.purge:{employer_id}", user.user_id,
        {"employer_id": str(employer_id)},
    )  # fmt: skip
    return {"deleted": dict(counts) if counts else {}}


# --- work logs ----------------------------------------------------------------------------


async def _log_entry(
    svc: Services, conn: Conn, user: Principal, row: Any, with_items: bool = True
) -> dict[str, Any]:
    log_text = await svc.crypto.decrypt(
        conn, user.user_id, "work_logs", str(row["id"]), row["text_enc"], row["key_version"]
    )
    files = await conn.fetch(
        "select id, name_enc, key_version, content_type, size_bytes, status"
        " from app.company_documents where work_log_id = $1 order by id",
        row["id"],
    )
    attachments = [
        {
            "id": str(f["id"]),
            "name": await _doc_name(svc, conn, user, f),
            "content_type": f["content_type"],
            "size_bytes": f["size_bytes"],
        }
        for f in files
    ]
    entry: dict[str, Any] = {
        "id": str(row["id"]),
        "day": row["day"].isoformat(),
        "text": log_text,
        "status": row["status"],
        "attachments": attachments,
        "updated_at": _iso(row["updated_at"]),
    }
    if with_items:
        items = await conn.fetch(
            f"select {NODE_COLUMNS} from app.memory_nodes where source->>'type' = 'worklog'"
            " and source->>'ref' = $1 and status = 'active' order by kind, id",
            str(row["id"]),
        )
        entry["items"] = [_node(i) for i in items]
    return entry


LOG_COLUMNS = "id, day, text_enc, key_version, content_sha256, status, updated_at"


@router.get("/employers/{employer_id}/logs")
async def list_logs(
    employer_id: UUID,
    svc: SvcDep,
    user: UserDep,
    before: date | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        rows = await conn.fetch(
            f"select {LOG_COLUMNS} from app.work_logs where employer_id = $1"
            " and ($2::date is null or day < $2) order by day desc limit $3",
            employer_id,
            before,
            limit,
        )
        entries = [await _log_entry(svc, conn, user, r) for r in rows]
    return {
        "logs": entries,
        "next_before": entries[-1]["day"] if len(entries) == limit else None,
    }


@router.get("/employers/{employer_id}/logs/{day}")
async def get_log(employer_id: UUID, day: date, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        row = await conn.fetchrow(
            f"select {LOG_COLUMNS} from app.work_logs where employer_id = $1 and day = $2",
            employer_id,
            day,
        )
        if row is None:
            raise HTTPException(404, "no log for that day")
        return await _log_entry(svc, conn, user, row)


async def _attachment_shas(conn: Conn, log_id: UUID) -> list[str]:
    return [
        r["sha256"]
        for r in await conn.fetch(
            "select sha256 from app.company_documents where work_log_id = $1", log_id
        )
    ]


async def _extract_log(svc: Services, user: Principal, log_id: UUID, sha: str) -> None:
    await svc.enqueue(
        "worklog.extract", f"worklog.extract:{user.user_id}:{log_id}:{sha}", user.user_id,
        {"work_log_id": str(log_id), "content_sha256": sha},
    )  # fmt: skip


@router.put("/employers/{employer_id}/logs/{day}")
async def put_log(
    employer_id: UUID, day: date, body: LogIn, svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    """Write or replace the log for that day; a changed log is extracted again."""
    log_text = body.text.strip()
    if not log_text:
        raise HTTPException(422, "the log is empty; delete it instead")
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        row = await conn.fetchrow(
            "select id, content_sha256 from app.work_logs where employer_id = $1 and day = $2"
            " for update",
            employer_id,
            day,
        )
        log_id: UUID = row["id"] if row is not None else uuid7()
        shas = await _attachment_shas(conn, log_id) if row is not None else []
        sha = content_sha(log_text, shas)
        changed = row is None or row["content_sha256"] != sha
        if changed:
            enc, version = await svc.crypto.encrypt(
                conn, user.user_id, "work_logs", str(log_id), log_text
            )
            await conn.execute(
                "insert into app.work_logs (id, user_id, employer_id, day, text_enc, key_version,"
                " content_sha256, status) values ($1, $2, $3, $4, $5, $6, $7, 'extracting')"
                " on conflict (id) do update set text_enc = excluded.text_enc,"
                " key_version = excluded.key_version, content_sha256 = excluded.content_sha256,"
                " status = 'extracting', updated_at = now()",
                log_id,
                user.user_id,
                employer_id,
                day,
                enc,
                version,
                sha,
            )
        saved = await conn.fetchrow(
            f"select {LOG_COLUMNS} from app.work_logs where id = $1", log_id
        )
        entry = await _log_entry(svc, conn, user, saved)
    if changed:
        await _extract_log(svc, user, log_id, sha)
    return entry


async def _forget_log_nodes(conn: Conn, user: Principal, employer_id: UUID, log_id: UUID) -> None:
    """A deleted log takes the facts that came from it, and reopens what it closed."""
    ref = str(log_id)
    await conn.execute(
        "update app.memory_nodes set body = body - 'closed_by' || '{\"state\": \"open\"}',"
        " updated_at = now() where employer_id = $1 and kind = 'commitment'"
        " and body->>'closed_by' = $2",
        employer_id,
        ref,
    )
    await conn.execute(
        "delete from app.memory_nodes where employer_id = $1 and source->>'type' = 'worklog'"
        " and source->>'ref' = $2",
        employer_id,
        ref,
    )


@router.delete("/employers/{employer_id}/logs/{day}", status_code=204)
async def delete_log(employer_id: UUID, day: date, svc: SvcDep, user: UserDep) -> Response:
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        row = await conn.fetchrow(
            "select id from app.work_logs where employer_id = $1 and day = $2", employer_id, day
        )
        if row is None:
            raise HTTPException(404, "no log for that day")
        files = [
            r["s3_key"]
            for r in await conn.fetch(
                "select s3_key from app.company_documents where work_log_id = $1", row["id"]
            )
        ]
        await _forget_log_nodes(conn, user, employer_id, row["id"])
        await conn.execute("delete from app.work_logs where id = $1", row["id"])  # cascades
        await _refresh(svc, conn, user, employer_id)
    for key in files:
        await svc.store.delete(key)
    return Response(status_code=204)


@router.post("/employers/{employer_id}/logs/{day}/attachments", status_code=201)
async def add_attachment(
    employer_id: UUID,
    day: date,
    request: Request,
    svc: SvcDep,
    user: UserDep,
    content_type: Annotated[str | None, Header()] = None,
    x_file_name: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Attach a file to that day's log (save the log first). The raw file is the body."""
    upload, kind, name = await _read_upload(request, svc, content_type, x_file_name)
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        row = await conn.fetchrow(
            "select id, text_enc, key_version from app.work_logs"
            " where employer_id = $1 and day = $2 for update",
            employer_id,
            day,
        )
        if row is None:
            raise HTTPException(409, "save the log for that day before attaching files")
        count = await conn.fetchval(
            "select count(*) from app.company_documents where work_log_id = $1", row["id"]
        )
        if count >= MAX_ATTACHMENTS:
            raise HTTPException(409, f"a log takes at most {MAX_ATTACHMENTS} files")
        doc = await _store_document(svc, conn, user, employer_id, upload, kind, name, row["id"])
        log_text = await svc.crypto.decrypt(
            conn, user.user_id, "work_logs", str(row["id"]), row["text_enc"], row["key_version"]
        )
        sha = content_sha(log_text, await _attachment_shas(conn, row["id"]))
        await conn.execute(
            "update app.work_logs set content_sha256 = $2, status = 'extracting',"
            " updated_at = now() where id = $1",
            row["id"],
            sha,
        )
    await _extract_log(svc, user, row["id"], sha)
    doc.pop("sha256")
    return doc


# --- company documents --------------------------------------------------------------------


@router.get("/employers/{employer_id}/documents")
async def list_documents(employer_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        rows = await conn.fetch(
            "select d.id, d.name_enc, d.key_version, d.content_type, d.size_bytes, d.status,"
            " d.created_at,"
            " (select count(*) from app.memory_proposals p where p.company_document_id = d.id"
            "  and p.status = 'pending') as pending,"
            " (select count(*) from app.memory_proposals p where p.company_document_id = d.id"
            "  and p.status = 'approved') as approved"
            " from app.company_documents d where d.employer_id = $1 and d.work_log_id is null"
            " order by d.id desc",
            employer_id,
        )
        out = [
            {
                "id": str(r["id"]),
                "name": await _doc_name(svc, conn, user, r),
                "content_type": r["content_type"],
                "size_bytes": r["size_bytes"],
                "status": r["status"],
                "created_at": _iso(r["created_at"]),
                "pending_proposals": r["pending"],
                "approved_proposals": r["approved"],
            }
            for r in rows
        ]
    return {"documents": out}


@router.post("/employers/{employer_id}/documents", status_code=201)
async def upload_document(
    employer_id: UUID,
    request: Request,
    svc: SvcDep,
    user: UserDep,
    content_type: Annotated[str | None, Header()] = None,
    x_file_name: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Upload company material; the raw file is the body. Its facts come back as proposals."""
    upload, kind, name = await _read_upload(request, svc, content_type, x_file_name)
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        doc = await _store_document(svc, conn, user, employer_id, upload, kind, name, None)
    sha = doc.pop("sha256")
    await svc.enqueue(
        "company_doc.extract", f"company_doc.extract:{user.user_id}:{doc['id']}:{sha}",
        user.user_id, {"document_id": doc["id"]},
    )  # fmt: skip
    return {**doc, "pending_proposals": 0, "approved_proposals": 0}


@router.delete("/employers/{employer_id}/documents/{document_id}", status_code=204)
async def delete_document(
    employer_id: UUID, document_id: UUID, svc: SvcDep, user: UserDep
) -> Response:
    """Delete a document or an attachment. Facts already approved from a document stay; an
    attachment's log is extracted again without it."""
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "delete from app.company_documents where id = $1 and employer_id = $2"
            " returning s3_key, work_log_id",
            document_id,
            employer_id,
        )
        if row is None:
            raise HTTPException(404, "no such document")
        log_id = row["work_log_id"]
        sha = None
        if log_id is not None:
            log_row = await conn.fetchrow(
                "select text_enc, key_version from app.work_logs where id = $1", log_id
            )
            if log_row is not None:
                log_text = await svc.crypto.decrypt(
                    conn, user.user_id, "work_logs", str(log_id), log_row["text_enc"],
                    log_row["key_version"],
                )  # fmt: skip
                sha = content_sha(log_text, await _attachment_shas(conn, log_id))
                await conn.execute(
                    "update app.work_logs set content_sha256 = $2, status = 'extracting',"
                    " updated_at = now() where id = $1",
                    log_id,
                    sha,
                )
    await svc.store.delete(row["s3_key"])
    if log_id is not None and sha is not None:
        await _extract_log(svc, user, log_id, sha)
    return Response(status_code=204)


# --- memory -------------------------------------------------------------------------------


@router.get("/employers/{employer_id}/memory")
async def list_memory(
    employer_id: UUID, svc: SvcDep, user: UserDep, kind: str | None = None
) -> dict[str, Any]:
    if kind is not None and kind not in WORKPLACE_KINDS:
        raise HTTPException(422, f"kind is one of {', '.join(WORKPLACE_KINDS)}")
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        rows = await conn.fetch(
            f"select {NODE_COLUMNS} from app.memory_nodes where employer_id = $1"
            " and status = 'active' and ($2::text is null or kind = $2)"
            " order by kind, valid_from desc nulls last, title",
            employer_id,
            kind,
        )
    return {"memory": [_node(r) for r in rows]}


@router.patch("/memory/{node_id}")
async def update_memory(
    node_id: UUID, body: NodePatch, svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    """Edit a fact. An edited fact from a work log survives the log's re-extraction."""
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select employer_id, domain, title, body from app.memory_nodes where id = $1"
            " for update",
            node_id,
        )
        if row is None:
            raise HTTPException(404, "no such memory node")
        merged = {**_body(row["body"]), **(body.body or {}), "edited": True}
        if body.title and "extracted_as" not in merged:
            # A renamed fact from a work log is still the same item when the log is
            # extracted again, so it is not added a second time.
            merged["extracted_as"] = row["title"]
        await conn.execute(
            "update app.memory_nodes set title = coalesce($2, title),"
            " summary = coalesce($3, summary), body = $4, updated_at = now(),"
            " embedding = null, embedding_model = null where id = $1",
            node_id,
            body.title,
            body.summary,
            merged,
        )
        updated = await conn.fetchrow(
            f"select {NODE_COLUMNS} from app.memory_nodes where id = $1", node_id
        )
        await _after_memory_change(svc, conn, user, row["employer_id"])
    return _node(updated)


async def _after_memory_change(
    svc: Services, conn: Conn, user: Principal, employer_id: UUID | None
) -> None:
    if employer_id is not None:
        await _refresh(svc, conn, user, employer_id)
        return
    # Career memory feeds the digest instead (debounced like approvals).
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
    await svc.enqueue("digest.rebuild", f"digest.rebuild:{user.user_id}:edit:{stamp}",
                      user.user_id, {}, delay_s=60)  # fmt: skip


@router.delete("/memory/{node_id}", status_code=204)
async def archive_memory(node_id: UUID, svc: SvcDep, user: UserDep) -> Response:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "update app.memory_nodes set status = 'archived', updated_at = now()"
            " where id = $1 and status = 'active' returning employer_id",
            node_id,
        )
        if row is None:
            raise HTTPException(404, "no such memory node")
        await _after_memory_change(svc, conn, user, row["employer_id"])
    return Response(status_code=204)


# --- proposals ----------------------------------------------------------------------------


def _proposal(row: Any, document_name: str | None) -> dict[str, Any]:
    payload = _body(row["payload"])
    return {
        "id": str(row["id"]),
        "employer_id": str(row["employer_id"]) if row["employer_id"] else None,
        "company_document_id": str(row["company_document_id"])
        if row["company_document_id"]
        else None,
        "document_name": document_name,
        "status": row["status"],
        "kind": payload.get("kind"),
        "title": payload.get("title"),
        "summary": payload.get("summary"),
        "evidence": payload.get("evidence"),
        "body": payload.get("body") or {},
    }


PROPOSAL_COLUMNS = "id, employer_id, company_document_id, status, payload, source"


@router.get("/proposals")
async def list_proposals(
    svc: SvcDep,
    user: UserDep,
    status: Literal["pending", "approved", "rejected"] = "pending",
    employer_id: UUID | None = None,
) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            f"select {PROPOSAL_COLUMNS} from app.memory_proposals where status = $1"
            " and ($2::uuid is null or employer_id = $2) order by company_document_id, id",
            status,
            employer_id,
        )
        names: dict[UUID, str] = {}
        for doc_id in {r["company_document_id"] for r in rows if r["company_document_id"]}:
            doc = await conn.fetchrow(
                "select id, name_enc, key_version from app.company_documents where id = $1",
                doc_id,
            )
            if doc is not None:
                names[doc_id] = await _doc_name(svc, conn, user, doc)
        out = [_proposal(r, names.get(r["company_document_id"])) for r in rows]
    return {"proposals": out}


async def _approve(
    conn: Conn, user: Principal, row: Any, edit: ProposalDecision | None = None
) -> UUID | None:
    payload = _body(row["payload"])
    kind = str(payload.get("kind") or "")
    if row["employer_id"] is None or kind not in WORKPLACE_KINDS:
        return None
    title = (edit.title if edit and edit.title else None) or str(payload.get("title") or "")
    summary = (edit.summary if edit and edit.summary else None) or str(
        payload.get("summary") or title
    )
    valid_from = payload.get("valid_from")
    node_id = uuid7()
    await conn.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " body, source, valid_from) values ($1, $2, $3, $4, 'workplace', $5, $6, $7, $8, $9)",
        node_id,
        user.user_id,
        row["employer_id"],
        kind,
        title[:300],
        summary[:1000],
        {**dict(payload.get("body") or {}), "evidence": payload.get("evidence") or ""},
        _body(row["source"]),
        date.fromisoformat(valid_from) if isinstance(valid_from, str) and valid_from else None,
    )
    await conn.execute(
        "update app.memory_proposals set status = 'approved', decided_at = now(),"
        " target_node_id = $2 where id = $1",
        row["id"],
        node_id,
    )
    return node_id


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: UUID, svc: SvcDep, user: UserDep, body: ProposalDecision | None = None
) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            f"select {PROPOSAL_COLUMNS} from app.memory_proposals where id = $1 for update",
            proposal_id,
        )
        if row is None:
            raise HTTPException(404, "no such proposal")
        if row["status"] != "pending":
            raise HTTPException(409, f"the proposal is already {row['status']}")
        node_id = await _approve(conn, user, row, body)
        if node_id is None:
            raise HTTPException(422, "this proposal cannot be approved here")
        await _refresh(svc, conn, user, row["employer_id"])
    return {"id": str(proposal_id), "status": "approved", "node_id": str(node_id)}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        status = await conn.fetchval(
            "update app.memory_proposals set status = 'rejected', decided_at = now()"
            " where id = $1 and status = 'pending' returning status",
            proposal_id,
        )
        if status is None:
            exists = await conn.fetchval(
                "select status from app.memory_proposals where id = $1", proposal_id
            )
            if exists is None:
                raise HTTPException(404, "no such proposal")
            raise HTTPException(409, f"the proposal is already {exists}")
    return {"id": str(proposal_id), "status": "rejected"}


@router.post("/proposals/bulk")
async def bulk_proposals(body: BulkDecision, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    """Approve or reject many at once: the given ids, or every pending proposal of a
    document ("approve all 18 from architecture.md")."""
    if not body.ids and body.company_document_id is None:
        raise HTTPException(422, "give ids or a company_document_id")
    decided = 0
    employers: set[UUID] = set()
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            f"select {PROPOSAL_COLUMNS} from app.memory_proposals where status = 'pending'"
            " and (id = any($1::uuid[]) or company_document_id = $2) order by id for update",
            body.ids,
            body.company_document_id,
        )
        for row in rows:
            if body.action == "approve":
                if await _approve(conn, user, row) is None:
                    continue
                employers.add(row["employer_id"])
            else:
                await conn.execute(
                    "update app.memory_proposals set status = 'rejected', decided_at = now()"
                    " where id = $1",
                    row["id"],
                )
            decided += 1
        for employer_id in employers:
            await _refresh(svc, conn, user, employer_id)
    return {"decided": decided, "action": body.action}


# --- packs --------------------------------------------------------------------------------


async def _pack_state(
    svc: Services, user: Principal, employer_id: UUID, mode: str, version: str
) -> dict[str, Any]:
    pack_id = await svc.valkey.hget(  # pyright: ignore[reportUnknownMemberType]
        keys.pack_index(user.user_id), keys.employer_pack_field(employer_id, mode)
    )
    pack = None
    if pack_id is not None:
        raw = await svc.valkey.get(keys.pack(user.user_id, text(pack_id)))
        pack = SessionPack.model_validate_json(raw) if raw is not None else None
    if pack is None:
        return {"mode": mode, "status": "none"}
    return {
        "mode": mode,
        "status": "ready" if pack.memory_version == version else "stale",
        "pack_id": str(pack.pack_id),
        "built_at": pack.built_at,
        "day": pack.standup.day if pack.standup else None,
        "token_count": pack.token_count,
    }


@router.get("/employers/{employer_id}/packs")
async def get_packs(employer_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        version = await memory_version(conn, user.user_id, employer_id)
    return {
        "packs": [
            await _pack_state(svc, user, employer_id, mode, version)
            for mode in ("standup", "meeting")
        ]
    }


@router.post("/employers/{employer_id}/packs/{mode}", status_code=202)
async def ensure_pack(
    employer_id: UUID, mode: Literal["standup", "meeting"], svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    """The start screen picked this company: build its pack unless the current one is
    already built from the company's memory as it is now (and, for a standup, for today)."""
    async with svc.tx(user) as conn:
        await _require_employer(conn, employer_id)
        version = await memory_version(conn, user.user_id, employer_id)
        settings = _body(await conn.fetchval("select settings from app.users where id = $1",
                                             user.user_id))  # fmt: skip
    today = local_today(settings).isoformat()
    state = await _pack_state(svc, user, employer_id, mode, version)
    if state["status"] == "ready" and (mode != "standup" or state.get("day") == today):
        return state
    await svc.queue.send(
        jobs.employer_pack_build(
            user.user_id, employer_id, mode, version, today, svc.settings.embedding_model_id
        )
    )
    return {**state, "status": "building"}
