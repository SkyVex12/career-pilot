"""Company workspace jobs (ARCHITECTURE.md, "Memory system", company workspaces).

- `worklog.extract`: the user's own log entry for one company and day (and its attachments)
  becomes workplace memory at once: work items, decisions, commitments, and the projects,
  systems, people and terms it names. The user wrote it, so nothing waits for approval; the
  app shows what came out under the entry, to edit or remove. A changed entry is extracted
  again: the nodes from its earlier content are replaced (those the user edited are kept),
  and commitments it had closed are opened again before the new content closes its own.
- `company_doc.extract`: a company document becomes memory proposals, which the user approves
  per document. Other people wrote it, so its facts wait like any other proposal.
- `workplace.refresh`: after the company's memory changed, embed the nodes that have no vector
  from the current model and rebuild the company's standup and meeting packs.
- `employer.purge`: after "delete all data for this company" removed the rows, delete the
  company's sealed files, its packs and the exact-repeat answers given from them.

Idempotency: the extraction keys name the content's SHA-256, and each handler checks that
the row still has that content before writing, so a stale or repeated job changes nothing.
"""

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast
from uuid import UUID

from copilot_core import jobs, keys
from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.packs import SessionPack, short_ids
from copilot_core.storage import employer_prefix
from copilot_core.valkey_client import text
from copilot_core.workplace import (
    SHORT_PREFIX,
    body_of,
    carry_vectors,
    item_text,
    local_today,
    memory_version,
    normalize,
    open_commitments,
    render_employer_pack,
)
from copilot_worker import prompts
from copilot_worker.context import Ctx
from copilot_worker.documents import document_block
from copilot_worker.llm import ModelOutputError, as_dict, as_list, complete_json
from copilot_worker.packs import PACK_TTL_MAX_S, embed_prepared, stored_pack
from copilot_worker.runtime import Job, handler

log = logging.getLogger(__name__)

NAMED_KINDS = ("project", "system", "person", "term")
EMBED_BATCH = 64


def _str(value: object, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


async def known_titles(
    conn: Conn, uid: UUID, eid: UUID, exclude_log: UUID | None = None
) -> dict[str, dict[str, Any]]:
    """Normalized title to node, per kind, of the company's active memory."""
    rows = await conn.fetch(
        "select id, kind, title, summary, body, source from app.memory_nodes"
        " where user_id = $1 and employer_id = $2 and status = 'active'",
        uid,
        eid,
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        source = as_dict(r["source"])
        if exclude_log is not None and source.get("ref") == str(exclude_log):
            continue
        out.setdefault(r["kind"], {})[normalize(r["title"])] = r
    return out


def render_known(known: Mapping[str, Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for kind in ("project", "system", "person", "term"):
        titles = sorted(str(r["title"]) for r in known.get(kind, {}).values())
        if titles:
            lines.append(f"{kind}s: " + "; ".join(titles[:80]))
    return "\n".join(lines)


async def insert_node(
    conn: Conn,
    uid: UUID,
    eid: UUID,
    kind: str,
    title: str,
    summary: str,
    body: Mapping[str, Any],
    source: Mapping[str, Any],
    valid_from: date | None = None,
) -> UUID:
    node_id = uuid7()
    await conn.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " body, source, valid_from) values ($1, $2, $3, $4, 'workplace', $5, $6, $7, $8, $9)",
        node_id,
        uid,
        eid,
        kind,
        title[:300],
        (summary or title)[:1000],
        dict(body),
        dict(source),
        valid_from,
    )
    return node_id


async def _edge(conn: Conn, uid: UUID, src: UUID, rel: str, dst: UUID) -> None:
    if src != dst:
        await conn.execute(
            "insert into app.memory_edges (user_id, src, rel, dst) values ($1, $2, $3, $4)"
            " on conflict do nothing",
            uid,
            src,
            rel,
            dst,
        )


# --- worklog.extract ----------------------------------------------------------------------


@handler("worklog.extract")
async def worklog_extract(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    log_id = UUID(job.payload["work_log_id"])
    sha = str(job.payload["content_sha256"])
    async with ctx.user(uid) as conn:
        row = await conn.fetchrow(
            "select w.id, w.employer_id, w.day, w.text_enc, w.key_version, w.content_sha256,"
            " w.extracted_sha256, e.name, u.display_name from app.work_logs w"
            " join app.employers e on e.id = w.employer_id"
            " join app.users u on u.id = w.user_id where w.id = $1",
            log_id,
        )
        if row is None or row["content_sha256"] != sha or row["extracted_sha256"] == sha:
            return  # deleted, changed since (a newer job runs), or already done
        eid: UUID = row["employer_id"]
        log_text = await ctx.crypto.decrypt(
            conn, uid, "work_logs", str(log_id), row["text_enc"], row["key_version"]
        )
        files = await conn.fetch(
            "select id, s3_key, content_type, name_enc, key_version from app.company_documents"
            " where work_log_id = $1 order by id",
            log_id,
        )
        names = [
            await ctx.crypto.decrypt(
                conn, uid, "company_documents", str(f["id"]), f["name_enc"], f["key_version"]
            )
            for f in files
        ]
        known_text, open_text, by_short = await log_context(conn, uid, eid, log_id)
        attachments: list[dict[str, Any]] = []
        for f, name in zip(files, names, strict=True):
            sealed = await ctx.store.get(f["s3_key"])
            if sealed is None:
                continue
            body = await ctx.crypto.decrypt_bytes(
                conn, uid, "company_documents", str(f["id"]), sealed, f["key_version"]
            )
            attachments.append(document_block(body, f["content_type"], f"Attachment: {name}"))
    day: date = row["day"]
    req = prompts.worklog_request(
        str(row["display_name"]),
        str(row["name"]),
        day.isoformat(),
        log_text,
        attachments,
        known_text,
        open_text,
    )
    try:
        data = await complete_json(ctx.router, "worklog", req)
    except Exception as e:
        # Marked failed whatever went wrong, so the log never sits at "Reading" for good (a
        # provider outage on 2026-10-22 left one there for four days). A bad answer is final;
        # anything else is raised again, so the queue retries it, and the scheduler retries
        # failed logs every hour after that.
        log.warning("work log %s: extraction failed (%s)", log_id, e)
        async with ctx.user(uid) as conn:
            await conn.execute(
                "update app.work_logs set status = 'failed' where id = $1 and content_sha256 = $2",
                log_id,
                sha,
            )
        if isinstance(e, ModelOutputError):
            return
        raise
    async with ctx.user(uid) as conn:
        current = await conn.fetchval(
            "select content_sha256 from app.work_logs where id = $1 for update", log_id
        )
        if current != sha:
            return  # changed while the model ran; the newer job writes
        count = await write_worklog_nodes(conn, uid, eid, log_id, day, data, by_short)
        await conn.execute(
            "update app.work_logs set status = 'extracted', extracted_sha256 = $2 where id = $1",
            log_id,
            sha,
        )
        await conn.execute(
            "update app.company_documents set status = 'ingested' where work_log_id = $1", log_id
        )
        version = await memory_version(conn, uid, eid)
    log.info("work log %s (%s): %d nodes", log_id, day, count)
    await ctx.queue.send(jobs.workplace_refresh(uid, eid, version))


async def log_context(
    conn: Conn, uid: UUID, eid: UUID, log_id: UUID
) -> tuple[str, str, dict[str, UUID]]:
    """What the extraction of one log is given besides the log: the company's known titles
    (without this log's own), and the open commitments it may close, by a short id the model
    returns, with the map back to their nodes. Commitments this log closed earlier count as
    open again."""
    known = await known_titles(conn, uid, eid, exclude_log=log_id)
    block, closable = await open_commitments_block(conn, uid, eid, str(log_id))
    return render_known(known), block, closable


async def open_commitments_block(
    conn: Conn, uid: UUID, eid: UUID, ref: str
) -> tuple[str, dict[str, UUID]]:
    """A company's open commitments for a prompt, by short id, with the map back to their
    nodes. `ref` is the work log or session asking: commitments it closed earlier count as
    open again, and commitments it made itself are left out (a rerun replaces them)."""
    nodes = await conn.fetch(
        "select id, kind, title, summary, body, valid_from from app.memory_nodes"
        " where user_id = $1 and employer_id = $2 and status = 'active' and kind = 'commitment'"
        " and coalesce(source->>'ref', '') <> $3",
        uid,
        eid,
        ref,
    )
    reopenable = [
        n
        for n in nodes
        if body_of(n).get("state") in ("", None, "open") or body_of(n).get("closed_by") == ref
    ]
    sids = short_ids([n["id"] for n in reopenable], SHORT_PREFIX)
    lines: list[str] = []
    for n in open_commitments(reopenable):
        due = body_of(n).get("due_date")
        lines.append(f"[{sids[n['id']]}] {item_text(n)}" + (f" (due {due})" if due else ""))
    return "\n".join(lines), {s: n for n, s in sids.items()}


async def write_worklog_nodes(
    conn: Conn,
    uid: UUID,
    eid: UUID,
    log_id: UUID,
    day: date,
    data: Mapping[str, Any],
    closable: Mapping[str, UUID],
) -> int:
    """Replace the nodes this log produced before with the new extraction's."""
    ref = str(log_id)
    # Commitments the earlier content closed open again; the new content closes its own.
    await reopen_commitments(conn, uid, eid, ref)
    await conn.execute(
        "delete from app.memory_nodes where user_id = $1 and employer_id = $2"
        " and source->>'type' = 'worklog' and source->>'ref' = $3"
        " and coalesce(body->>'edited', '') <> 'true'",
        uid,
        eid,
        ref,
    )
    known = await known_titles(conn, uid, eid)
    # What the user edited stays as they wrote it, and is not added a second time.
    kept: set[tuple[str, str]] = set()
    for r in await conn.fetch(
        "select kind, title, body->>'extracted_as' as extracted_as from app.memory_nodes"
        " where employer_id = $1 and source->>'type' = 'worklog' and source->>'ref' = $2",
        eid,
        ref,
    ):
        kept.add((r["kind"], normalize(r["title"])))
        if r["extracted_as"]:
            kept.add((r["kind"], normalize(r["extracted_as"])))
    source = {"type": "worklog", "ref": ref}
    count = 0
    named: dict[str, UUID] = {}  # normalized name of a project or system to its node
    for kind in ("project", "system"):
        for title, r in known.get(kind, {}).items():
            named.setdefault(title, r["id"])

    async def add_named(kind: str, title: str, summary: str, body: dict[str, Any]) -> None:
        nonlocal count
        key = normalize(title)
        if not key or key in known.get(kind, {}):
            return
        node_id = await insert_node(conn, uid, eid, kind, title, summary, body, source, day)
        known.setdefault(kind, {})[key] = {"id": node_id, "title": title}
        if kind in ("project", "system"):
            named.setdefault(key, node_id)
        count += 1

    for kind, field in (("project", "projects"), ("system", "systems")):
        for item in map(as_dict, as_list(data.get(field))):
            stack = [_str(s, 60) for s in as_list(item.get("stack")) if _str(s, 60)]
            await add_named(
                kind, _str(item.get("name"), 200), _str(item.get("summary")), {"stack": stack}
            )
    for item in map(as_dict, as_list(data.get("people"))):
        await add_named(
            "person",
            _str(item.get("name"), 200),
            _str(item.get("summary")),
            {"role": _str(item.get("role"), 200)},
        )
    for item in map(as_dict, as_list(data.get("terms"))):
        await add_named("term", _str(item.get("term"), 200), _str(item.get("meaning")), {})

    for item in map(as_dict, as_list(data.get("work_items"))):
        title = _str(item.get("title"), 200)
        if not title or ("work_item", normalize(title)) in kept:
            continue
        state = _str(item.get("state"), 20)
        state = state if state in ("done", "in_progress", "planned", "blocked") else "in_progress"
        project = _str(item.get("project"), 200)
        body = {
            "state": state,
            "external_id": _str(item.get("external_id"), 100),
            "project": project,
        }
        node_id = await insert_node(
            conn, uid, eid, "work_item", title, _str(item.get("summary")), body, source, day
        )
        count += 1
        if (target := named.get(normalize(project))) is not None:
            await _edge(conn, uid, node_id, "part_of", target)
    seen_decisions: set[str] = set()
    for item in map(as_dict, as_list(data.get("decisions"))):
        title = _str(item.get("title"), 200)
        if (
            not title
            or normalize(title) in seen_decisions
            or ("decision", normalize(title)) in kept
        ):
            continue
        seen_decisions.add(normalize(title))
        project = _str(item.get("project"), 200)
        node_id = await insert_node(
            conn,
            uid,
            eid,
            "decision",
            title,
            _str(item.get("summary")),
            {"project": project},
            source,
            day,
        )
        count += 1
        if (target := named.get(normalize(project))) is not None:
            await _edge(conn, uid, node_id, "about", target)
    for item in map(as_dict, as_list(data.get("commitments"))):
        title = _str(item.get("text"), 300)
        if not title or ("commitment", normalize(title)) in kept:
            continue
        due = _str(item.get("due_date"), 10)
        due = due if len(due) == 10 and due[4] == "-" and due[7] == "-" else ""
        await insert_node(
            conn,
            uid,
            eid,
            "commitment",
            title,
            title,
            {"due_date": due, "state": "open"},
            source,
            day,
        )
        count += 1
    await close_commitments(conn, as_list(data.get("closed_commitments")), closable, ref)
    return count


async def reopen_commitments(conn: Conn, uid: UUID, eid: UUID, ref: str) -> None:
    """Commitments that `ref` (a work log or a session) closed are open again."""
    await conn.execute(
        "update app.memory_nodes set body = body - 'closed_by' || '{\"state\": \"open\"}',"
        " updated_at = now() where user_id = $1 and employer_id = $2 and kind = 'commitment'"
        " and body->>'closed_by' = $3",
        uid,
        eid,
        ref,
    )


async def close_commitments(
    conn: Conn, shorts: Sequence[Any], closable: Mapping[str, UUID], ref: str
) -> int:
    """Mark the commitments the model named by short id done, closed by `ref`."""
    closed = 0
    for short in shorts:
        short_id = str(short).strip().strip("[]")
        short_id = short_id if short_id.startswith("n:") else f"n:{short_id}"
        if (node_id := closable.get(short_id)) is not None:
            await conn.execute(
                "update app.memory_nodes set body = body || $2, updated_at = now()"
                " where id = $1 and kind = 'commitment'",
                node_id,
                {"state": "done", "closed_by": ref},
            )
            closed += 1
    return closed


# --- company_doc.extract ------------------------------------------------------------------


@handler("company_doc.extract")
async def company_doc_extract(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    doc_id = UUID(job.payload["document_id"])
    async with ctx.user(uid) as conn:
        doc = await conn.fetchrow(
            "select d.employer_id, d.s3_key, d.sha256, d.status, d.content_type, d.name_enc,"
            " d.key_version, d.work_log_id, e.name, u.display_name from app.company_documents d"
            " join app.employers e on e.id = d.employer_id"
            " join app.users u on u.id = d.user_id where d.id = $1",
            doc_id,
        )
        if doc is None or doc["status"] == "ingested" or doc["work_log_id"] is not None:
            return
        eid: UUID = doc["employer_id"]
        name = await ctx.crypto.decrypt(
            conn, uid, "company_documents", str(doc_id), doc["name_enc"], doc["key_version"]
        )
        sealed = await ctx.store.get(doc["s3_key"])
        body = None
        if sealed is not None:
            body = await ctx.crypto.decrypt_bytes(
                conn, uid, "company_documents", str(doc_id), sealed, doc["key_version"]
            )
        known = await known_titles(conn, uid, eid)
    if body is None or hashlib.sha256(body).hexdigest() != doc["sha256"]:
        log.warning("company document %s: missing or not the declared content", doc_id)
        await _doc_status(ctx, uid, doc_id, "failed")
        return
    req = prompts.company_doc_request(
        str(doc["display_name"]),
        str(doc["name"]),
        document_block(body, doc["content_type"], name),
        render_known(known),
    )
    try:
        data = await complete_json(ctx.router, "company_doc", req)
    except ModelOutputError as e:
        log.warning("company document %s: extraction failed (%s)", doc_id, e)
        await _doc_status(ctx, uid, doc_id, "failed")
        return
    proposals = document_proposals(data, known)
    async with ctx.user(uid) as conn:
        status = await conn.fetchval(
            "select status from app.company_documents where id = $1 for update", doc_id
        )
        if status is None or status == "ingested":
            return
        await conn.execute(
            "delete from app.memory_proposals where company_document_id = $1"
            " and status = 'pending'",
            doc_id,
        )
        source = {"type": "company_document", "ref": str(doc_id)}
        await conn.executemany(
            "insert into app.memory_proposals (id, user_id, employer_id, company_document_id,"
            " source, action, payload) values ($1, $2, $3, $4, $5, 'create', $6)",
            [(uuid7(), uid, eid, doc_id, source, p) for p in proposals],
        )
        await conn.execute(
            "update app.company_documents set status = 'ingested' where id = $1", doc_id
        )
    log.info("company document %s: %d proposals", doc_id, len(proposals))


async def _doc_status(ctx: Ctx, uid: UUID, doc_id: UUID, status: str) -> None:
    async with ctx.user(uid) as conn:
        await conn.execute(
            "update app.company_documents set status = $2 where id = $1", doc_id, status
        )


def document_proposals(
    data: Mapping[str, Any], known: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Proposal payloads ({kind, title, summary, body, valid_from, evidence}) for the facts a
    document gives that the company's memory does not have yet."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(
        kind: str,
        title: str,
        summary: str,
        body: dict[str, Any],
        evidence: str,
        valid_from: str | None = None,
    ) -> None:
        key = (kind, normalize(title))
        if not key[1] or key in seen or key[1] in known.get(kind, {}):
            return
        seen.add(key)
        out.append(
            {
                "kind": kind,
                "title": title,
                "summary": summary or title,
                "body": body,
                "valid_from": valid_from,
                "evidence": evidence,
            }
        )

    for kind, field in (("project", "projects"), ("system", "systems")):
        for item in map(as_dict, as_list(data.get(field))):
            stack = [_str(s, 60) for s in as_list(item.get("stack")) if _str(s, 60)]
            add(
                kind,
                _str(item.get("name"), 200),
                _str(item.get("summary")),
                {"stack": stack},
                _str(item.get("evidence"), 300),
            )
    for item in map(as_dict, as_list(data.get("people"))):
        add(
            "person",
            _str(item.get("name"), 200),
            _str(item.get("summary")),
            {"role": _str(item.get("role"), 200)},
            _str(item.get("evidence"), 300),
        )
    for item in map(as_dict, as_list(data.get("decisions"))):
        when = _str(item.get("date"), 10)
        add(
            "decision",
            _str(item.get("title"), 200),
            _str(item.get("summary")),
            {},
            _str(item.get("evidence"), 300),
            when if len(when) == 10 else None,
        )
    for item in map(as_dict, as_list(data.get("terms"))):
        add(
            "term",
            _str(item.get("term"), 200),
            _str(item.get("meaning")),
            {},
            _str(item.get("evidence"), 300),
        )
    return out


# --- workplace.refresh --------------------------------------------------------------------


@handler("workplace.refresh")
async def workplace_refresh(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    eid = UUID(job.payload["employer_id"])
    model = ctx.embedder.model_id if ctx.embedder is not None else None
    async with ctx.user(uid) as conn:
        if await conn.fetchval("select 1 from app.employers where id = $1", eid) is None:
            return  # deleted meanwhile
        settings = as_dict(await conn.fetchval("select settings from app.users where id = $1", uid))
        missing: list[Any] = []
        if model is not None:
            missing = list(
                await conn.fetch(
                    "select id, kind, title, summary, body from app.memory_nodes"
                    " where user_id = $1 and employer_id = $2 and status = 'active'"
                    " and embedding_model is distinct from $3 order by id",
                    uid,
                    eid,
                    model,
                )
            )
        version = await memory_version(conn, uid, eid)
    if missing:
        await embed_nodes(ctx, uid, missing)
    day = local_today(settings).isoformat()
    for mode in ("standup", "meeting"):
        await ctx.queue.send(jobs.employer_pack_build(uid, eid, mode, version, day, model))


async def embed_nodes(ctx: Ctx, uid: UUID, rows: Sequence[Any]) -> int:
    """Vectors for live search, from the node's text as packs and search show it. Search
    works without them (full text only), so a failure is logged and left for the next
    refresh."""
    assert ctx.embedder is not None
    done = 0
    for start in range(0, len(rows), EMBED_BATCH):
        batch = rows[start : start + EMBED_BATCH]
        try:
            vectors = await ctx.embedder.embed([item_text(r) for r in batch])
        except Exception as e:
            log.warning("node embeddings failed (%s); search stays full-text only for now", e)
            return done
        async with ctx.user(uid) as conn:
            await conn.executemany(
                "update app.memory_nodes set embedding = $2::vector, embedding_model = $3"
                " where id = $1",
                [
                    (r["id"], "[" + ",".join(f"{x:.7g}" for x in v) + "]", ctx.embedder.model_id)
                    for r, v in zip(batch, vectors, strict=True)
                ],
            )
        done += len(batch)
    return done


# --- pack.build for a company (called from copilot_worker.packs) --------------------------


async def build_employer_pack(ctx: Ctx, job: Job) -> None:
    uid = job.uid
    mode = str(job.payload.get("mode", "meeting"))
    eid = UUID(str(job.payload["context"]).split(":", 1)[1])
    async with ctx.user(uid) as conn:
        settings = as_dict(await conn.fetchval("select settings from app.users where id = $1", uid))
        raw_day = job.payload.get("day")
        today = date.fromisoformat(str(raw_day)) if raw_day else local_today(settings)
        pack = await render_employer_pack(conn, uid, eid, mode, today)
    if pack is None:
        log.info("pack for employer %s: the company is gone", eid)
        return
    field = keys.employer_pack_field(eid, mode)
    previous_id = await ctx.valkey.hget(keys.pack_index(uid), field)  # pyright: ignore[reportUnknownMemberType]
    previous: SessionPack | None = None
    if previous_id is not None:
        previous = await stored_pack(ctx, uid, text(previous_id))
    carry_vectors(pack, previous)
    if pack.prepared_answers and pack.embedding_model is None:
        await embed_prepared(ctx, pack)
    await ctx.valkey.set(
        keys.pack(uid, pack.pack_id),
        pack.model_dump_json(by_alias=True, exclude_none=True),
        ex=min(PACK_TTL_MAX_S, 7 * 24 * 3600),
    )
    await ctx.valkey.hset(keys.pack_index(uid), mapping={field: str(pack.pack_id)})  # pyright: ignore[reportUnknownMemberType]
    if previous_id is not None and text(previous_id) != str(pack.pack_id):
        await ctx.valkey.delete(keys.pack(uid, text(previous_id)))
    log.info("pack %s for employer %s (%s, %s)", pack.pack_id, eid, mode, pack.memory_version)


# --- employer.purge -----------------------------------------------------------------------


@handler("employer.purge")
async def employer_purge(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    eid = str(job.payload["employer_id"])
    removed = await ctx.store.delete_prefix(employer_prefix(uid, UUID(eid)))
    index = await ctx.valkey.hgetall(keys.pack_index(uid))
    pack_ids: set[str] = set()
    for field, value in index.items():
        name = text(field)
        if name.startswith(f"employer:{eid}:"):
            pack_ids.add(text(value))
            await ctx.valkey.hdel(keys.pack_index(uid), name)  # pyright: ignore[reportUnknownMemberType]
    for pack_id in pack_ids:
        await ctx.valkey.delete(keys.pack(uid, pack_id))
    # Exact-repeat answers given from this company's packs (the gateway stores the employer).
    answers = 0
    async for key in ctx.valkey.scan_iter(match=f"qa:{{{uid}}}:*", count=500):
        raw = await ctx.valkey.get(key)
        try:
            value = json.loads(text(raw)) if raw is not None else None
        except ValueError:
            value = None
        if isinstance(value, dict) and cast(dict[str, Any], value).get("employer") == eid:
            answers += int(await ctx.valkey.delete(key))
    log.info(
        "employer %s purged: %d objects, %d packs, %d cached answers",
        eid,
        removed,
        len(pack_ids),
        answers,
    )
