"""Engineering context jobs (ARCHITECTURE.md, "Memory system", engineering context).

- `knowledge.index`: a company document becomes searchable sections, kept sealed, and facts
  in the company's memory: components, decisions with their reason and rejected alternatives,
  flows, constraints, open risks and terms. The user uploaded the document for this, so
  nothing waits for approval; the app shows what came out per file, to edit or remove. A new
  upload of a file of the same name replaces the older one once it is indexed.
- `knowledge.publish`: a company's sections, opened into Valkey for live calls (the gateway
  cannot open sealed text), with a vector for each that lacks one.
- `knowledge.ask`: "test a question": the answer a standup would get, with its sources.
- `company_doc.extract`, the job from before engineering context, runs `knowledge.index`.

Idempotency: indexing is keyed by the document's content hash and skipped when the document
already has an index from it. Sections are replaced whole, and so are the document's facts,
except those the user edited. Nothing of a document's text is logged: only ids and counts.
"""

import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from uuid import UUID

from copilot_core import jobs, keys, knowledge, prompts, workplace
from copilot_core.db import Conn
from copilot_core.ids import uuid7
from copilot_core.packs import NodeRef, cited_refs, pack_items, ref_map, render_pack
from copilot_core.workplace import local_today, memory_version, normalize
from copilot_worker import prompts as worker_prompts
from copilot_worker.context import Ctx
from copilot_worker.llm import ModelOutputError, as_dict, as_list, complete_json
from copilot_worker.runtime import Job, handler
from copilot_worker.workplace import insert_node, known_titles, render_known

log = logging.getLogger(__name__)

TEXT_TYPES = ("text/plain", "text/markdown")
# One model call reads at most this many words of sections; a longer document is read in
# parts, each told what the earlier parts already named.
PART_WORDS = 6000
EMBED_BATCH = 64
EMBED_CHARS = 6000
LIVE_TTL_S = 30 * 24 * 3600
ASK_TTL_S = 600
SOURCE = "company_document"


def _str(value: object, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def _strs(value: object, limit: int = 300, most: int = 8) -> list[str]:
    return [s for s in (_str(v, limit) for v in as_list(value)) if s][:most]


# --- knowledge.index ------------------------------------------------------------------------


@handler("company_doc.extract")
@handler("knowledge.index")
async def knowledge_index(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    doc_id = UUID(job.payload["document_id"])
    async with ctx.user(uid) as conn:
        doc = await conn.fetchrow(
            "select d.employer_id, d.s3_key, d.sha256, d.status, d.content_type, d.name_enc,"
            " d.key_version, d.work_log_id, d.indexed_sha256, e.name, u.display_name"
            " from app.company_documents d join app.employers e on e.id = d.employer_id"
            " join app.users u on u.id = d.user_id where d.id = $1",
            doc_id,
        )
        if doc is None or doc["work_log_id"] is not None:
            return  # deleted, or a work log's attachment (read with its log)
        if doc["indexed_sha256"] == doc["sha256"] and doc["status"] == "ingested":
            return  # this content is indexed: a redelivery does nothing
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
    if body is None or hashlib.sha256(body).hexdigest() != doc["sha256"]:
        log.warning("company document %s: missing or not the declared content", doc_id)
        await _fail(ctx, uid, doc_id, "The file could not be read back from storage.")
        return
    if doc["content_type"] not in TEXT_TYPES:
        await _fail(ctx, uid, doc_id, "Only Markdown and text files are read for now.")
        return
    sections = knowledge.split(body.decode("utf-8", "replace"))
    if not sections:
        await _fail(ctx, uid, doc_id, "The file has no text.")
        return
    await write_sections(ctx, uid, eid, doc_id, name, sections)
    try:
        data = await extract(ctx, uid, eid, str(doc["display_name"]), str(doc["name"]), sections)
    except Exception as e:
        # The sections are searchable already. Failed is shown on the file; a bad answer is
        # final, anything else is raised so the queue tries again.
        log.warning("company document %s: extraction failed (%s)", doc_id, type(e).__name__)
        await _fail(
            ctx,
            uid,
            doc_id,
            "Its sections are searchable; its facts could not be read.",
            employer_id=eid,
        )
        if isinstance(e, ModelOutputError):
            return
        raise
    async with ctx.user(uid) as conn:
        row = await conn.fetchrow(
            "select sha256, replaces from app.company_documents where id = $1 for update", doc_id
        )
        if row is None or row["sha256"] != doc["sha256"]:
            return  # deleted or replaced while the model read it
        # The earlier upload goes first, so its facts are not taken for facts another
        # source holds, and the new document's take their place.
        await replace_older(conn, uid, eid, doc_id, row["replaces"])
        count = await write_facts(conn, uid, eid, doc_id, data, sections)
        await conn.execute(
            "delete from app.memory_proposals where company_document_id = $1"
            " and status = 'pending'",
            doc_id,
        )
        await _after(ctx, uid, eid, conn)
        await conn.execute(
            "update app.company_documents set status = 'ingested', indexed_sha256 = sha256,"
            " error = null, replaces = null where id = $1",
            doc_id,
        )
    log.info("company document %s: %d sections, %d facts", doc_id, len(sections), count)


async def _fail(
    ctx: Ctx, uid: UUID, doc_id: UUID, error: str, *, employer_id: UUID | None = None
) -> None:
    async with ctx.user(uid) as conn:
        await conn.execute(
            "update app.company_documents set status = 'failed', error = $2 where id = $1",
            doc_id,
            error,
        )
        if employer_id is not None:
            await _after(ctx, uid, employer_id, conn)


async def _after(ctx: Ctx, uid: UUID, eid: UUID, conn: Conn) -> None:
    """The company's memory or sections changed: embed, rebuild its packs, republish."""
    version = await memory_version(conn, uid, eid)
    chunks = await conn.fetch("select id from app.knowledge_chunks where employer_id = $1", eid)
    await ctx.queue.send(jobs.workplace_refresh(uid, eid, version))
    live = knowledge.live_version(r["id"] for r in chunks)
    await ctx.queue.send(jobs.knowledge_publish(uid, eid, live))


async def write_sections(
    ctx: Ctx, uid: UUID, eid: UUID, doc_id: UUID, name: str, sections: Sequence[knowledge.Section]
) -> None:
    """Replace the document's sections, each sealed with the user's data key."""
    async with ctx.user(uid) as conn:
        await conn.execute("delete from app.knowledge_chunks where document_id = $1", doc_id)
        rows: list[tuple[UUID, UUID, UUID, UUID, int, bytes, int]] = []
        for s in sections:
            chunk_id = uuid7()
            sealed, version = await ctx.crypto.encrypt(
                conn, uid, "knowledge_chunks", str(chunk_id), knowledge.seal_body(name, s)
            )
            rows.append((chunk_id, uid, eid, doc_id, s.ord, sealed, version))
        await conn.executemany(
            "insert into app.knowledge_chunks (id, user_id, employer_id, document_id, ord,"
            " body_enc, key_version) values ($1, $2, $3, $4, $5, $6, $7)",
            rows,
        )
        await conn.execute(
            "update app.company_documents set chunk_count = $2, status = 'ingesting',"
            " error = null where id = $1",
            doc_id,
            len(rows),
        )


def parts(sections: Sequence[knowledge.Section]) -> list[list[knowledge.Section]]:
    """The document in parts of at most PART_WORDS words, cut between sections."""
    out: list[list[knowledge.Section]] = [[]]
    words = 0
    for s in sections:
        n = len(s.text.split())
        if out[-1] and words + n > PART_WORDS:
            out.append([])
            words = 0
        out[-1].append(s)
        words += n
    return out


def render_sections(sections: Sequence[knowledge.Section]) -> str:
    return "\n\n".join(
        f"[section {s.ord}] {s.path or '(start of the document)'}\n{s.text}" for s in sections
    )


FIELDS = ("components", "decisions", "flows", "constraints", "risks", "terms", "projects",
          "people")  # fmt: skip


async def extract(
    ctx: Ctx,
    uid: UUID,
    eid: UUID,
    user_name: str,
    company: str,
    sections: Sequence[knowledge.Section],
) -> dict[str, list[dict[str, Any]]]:
    """Every part's entries, merged. A later part is told the names the earlier ones gave,
    so one component described over several sections stays one entry."""
    async with ctx.user(uid) as conn:
        known = await known_titles(conn, uid, eid)
    known_text = render_known(known)
    merged: dict[str, list[dict[str, Any]]] = {f: [] for f in FIELDS}
    for part in parts(sections):
        named = sorted(
            {_str(i.get("name") or i.get("title") or i.get("term"), 120)
             for f in FIELDS for i in merged[f]} - {""}
        )  # fmt: skip
        seen = known_text + (f"\nalready taken from this document: {'; '.join(named)}"
                             if named else "")  # fmt: skip
        req = worker_prompts.knowledge_request(user_name, company, render_sections(part), seen)
        data = await complete_json(ctx.router, "knowledge", req)
        for f in FIELDS:
            merged[f] += [as_dict(i) for i in as_list(data.get(f))]
    return merged


def _where(item: Mapping[str, Any], sections: Sequence[knowledge.Section]) -> str:
    """The heading path of the section an entry came from, kept with the fact as its source."""
    n = item.get("section")
    if isinstance(n, int) and 0 <= n < len(sections):
        return sections[n].path
    return ""


def entries(
    data: Mapping[str, Sequence[Mapping[str, Any]]], sections: Sequence[knowledge.Section]
) -> list[tuple[str, str, str, dict[str, Any], date | None]]:
    """(kind, title, summary, body, valid_from) for every entry of an extraction."""
    out: list[tuple[str, str, str, dict[str, Any], date | None]] = []
    for i in data.get("components", ()):
        body = {"talks_to": _strs(i.get("talks_to"), 120), "stack": _strs(i.get("stack"), 60)}
        out.append(("system", _str(i.get("name"), 200), _str(i.get("does")), body, None))
    for i in data.get("decisions", ()):
        when = _str(i.get("date"), 10)
        day: date | None = None
        if len(when) == 10:
            try:
                day = date.fromisoformat(when)
            except ValueError:
                day = None
        body = {
            "why": _str(i.get("why")),
            "alternatives": _strs(i.get("alternatives")),
            "tradeoff": _str(i.get("tradeoff")),
        }
        out.append(("decision", _str(i.get("title"), 200), _str(i.get("what")), body, day))
    for i in data.get("flows", ()):
        body = {"steps": _strs(i.get("steps"), 240, 5)}
        out.append(("flow", _str(i.get("name"), 200), _str(i.get("summary")), body, None))
    for kind, f in (("constraint", "constraints"), ("risk", "risks")):
        for i in data.get(f, ()):
            out.append((kind, _str(i.get("title"), 200), _str(i.get("detail")), {}, None))
    for i in data.get("terms", ()):
        out.append(("term", _str(i.get("term"), 200), _str(i.get("meaning")), {}, None))
    for i in data.get("projects", ()):
        body = {"stack": _strs(i.get("stack"), 60)}
        out.append(("project", _str(i.get("name"), 200), _str(i.get("summary")), body, None))
    for i in data.get("people", ()):
        body = {"role": _str(i.get("role"), 200)}
        out.append(("person", _str(i.get("name"), 200), _str(i.get("summary")), body, None))
    paths = [
        _where(i, sections)
        for f in ("components", "decisions", "flows", "constraints", "risks", "terms")
        for i in data.get(f, ())
    ]
    return [
        (k, t, s, {**b, "section": paths[n]} if n < len(paths) and paths[n] else b, d)
        for n, (k, t, s, b, d) in enumerate(out)
    ]


async def write_facts(
    conn: Conn,
    uid: UUID,
    eid: UUID,
    doc_id: UUID,
    data: Mapping[str, Sequence[Mapping[str, Any]]],
    sections: Sequence[knowledge.Section],
) -> int:
    """Replace the facts this document gave before with the new extraction's. A fact the
    user edited stays as they wrote it. A fact another source already holds under the same
    name (a system first named in a work log) keeps its text and gains what the document
    adds: the fields its body lacks."""
    ref = str(doc_id)
    await conn.execute(
        "delete from app.memory_nodes where user_id = $1 and employer_id = $2"
        " and source->>'type' = $3 and source->>'ref' = $4"
        " and coalesce(body->>'edited', '') <> 'true'",
        uid,
        eid,
        SOURCE,
        ref,
    )
    known = await known_titles(conn, uid, eid)
    source = {"type": SOURCE, "ref": ref}
    count = 0
    for kind, title, summary, body, day in entries(data, sections):
        key = normalize(title)
        if not key:
            continue
        existing = known.get(kind, {}).get(key)
        if existing is None:
            node_id = await insert_node(conn, uid, eid, kind, title, summary, body, source, day)
            known.setdefault(kind, {})[key] = {"id": node_id, "title": title, "body": {}}
            count += 1
            continue
        raw = existing["body"]
        had = as_dict(json.loads(raw) if isinstance(raw, str) else raw)
        add = {k: v for k, v in body.items() if v and not had.get(k)}
        if add and had.get("edited") is not True:
            await conn.execute(
                "update app.memory_nodes set body = body || $2, updated_at = now(),"
                " embedding = null, embedding_model = null where id = $1",
                existing["id"],
                add,
            )
            count += 1
    return count


async def replace_older(conn: Conn, uid: UUID, eid: UUID, doc_id: UUID, older: UUID | None) -> None:
    """Delete the earlier upload this document replaces, with its sections (they cascade)
    and its facts. A fact the user edited stays, and now belongs to the new document.
    The deletion trigger durably schedules the old object's cleanup."""
    if older is None:
        return None
    await conn.execute(
        "delete from app.memory_nodes where user_id = $1 and employer_id = $2"
        " and source->>'type' = $3 and source->>'ref' = $4"
        " and coalesce(body->>'edited', '') <> 'true'",
        uid,
        eid,
        SOURCE,
        str(older),
    )
    await conn.execute(
        "update app.memory_nodes set source = jsonb_set(source, '{ref}', to_jsonb($4::text))"
        " where user_id = $1 and employer_id = $2 and source->>'type' = $3"
        " and source->>'ref' = $5",
        uid,
        eid,
        SOURCE,
        str(doc_id),
        str(older),
    )
    await conn.execute(
        "delete from app.company_documents where id = $1 and employer_id = $2"
        " and work_log_id is null",
        older,
        eid,
    )


# --- knowledge.publish ----------------------------------------------------------------------


async def load_chunks(
    ctx: Ctx, conn: Conn, uid: UUID, eid: UUID
) -> tuple[list[knowledge.LiveChunk], int]:
    """A company's sections, opened: the newest documents first, each in order, at most
    LIVE_MAX_CHUNKS. Also how many it has in all."""
    rows = await conn.fetch(
        "select id, document_id, body_enc, key_version from app.knowledge_chunks"
        " where employer_id = $1 order by document_id desc, ord",
        eid,
    )
    out: list[knowledge.LiveChunk] = []
    for r in rows[: knowledge.LIVE_MAX_CHUNKS]:
        raw = await ctx.crypto.decrypt(
            conn, uid, "knowledge_chunks", str(r["id"]), r["body_enc"], r["key_version"]
        )
        out.append(
            knowledge.LiveChunk.model_validate(
                {**json.loads(raw), "id": r["id"], "document_id": r["document_id"]}
            )
        )
    return out, len(rows)


@handler("knowledge.publish")
async def knowledge_publish(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    eid = UUID(job.payload["employer_id"])
    async with ctx.user(uid) as conn:
        if await conn.fetchval("select 1 from app.employers where id = $1", eid) is None:
            return
        chunks, total = await load_chunks(ctx, conn, uid, eid)
        model = ctx.embedder.model_id if ctx.embedder is not None else None
        missing: set[UUID] = set()
        if model is not None:
            missing = {
                r["id"]
                for r in await conn.fetch(
                    "select id from app.knowledge_chunks where employer_id = $1"
                    " and embedding_model is distinct from $2",
                    eid,
                    model,
                )
            }
    key = keys.knowledge(uid, eid)
    if not chunks:
        async with ctx.cache_commit(uid):
            await ctx.valkey.delete(key)
        return
    live = knowledge.LiveIndex(
        version=knowledge.live_version(c.id for c in chunks), chunks=chunks, total=total
    )
    async with ctx.cache_commit(uid):
        await ctx.valkey.set(key, live.model_dump_json(), ex=LIVE_TTL_S)
    if total > len(chunks):
        log.warning("employer %s: %d of %d sections are live (the cap)", eid, len(chunks), total)
    await embed_chunks(ctx, uid, [c for c in chunks if c.id in missing])
    log.info("employer %s: %d sections published", eid, len(chunks))


async def embed_chunks(ctx: Ctx, uid: UUID, chunks: Sequence[knowledge.LiveChunk]) -> int:
    """Vectors for the detailed answer's search. Keyword search works without them, so a
    failure is logged and left for the next publish."""
    if ctx.embedder is None:
        return 0
    done = 0
    for start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[start : start + EMBED_BATCH]
        try:
            vectors = await ctx.embedder.embed([f"{c.path}\n{c.text}"[:EMBED_CHARS] for c in batch])
        except Exception as e:
            log.warning("section embeddings failed (%s); search stays keyword only", e)
            return done
        async with ctx.user(uid) as conn:
            await conn.executemany(
                "update app.knowledge_chunks set embedding = $2::vector, embedding_model = $3"
                " where id = $1",
                [
                    (c.id, workplace.vector_literal(v), ctx.embedder.model_id)
                    for c, v in zip(batch, vectors, strict=True)
                ],
            )
        done += len(batch)
    return done


# --- knowledge.ask --------------------------------------------------------------------------

ASK_SECTIONS = 6


async def find_sections(
    ctx: Ctx,
    conn: Conn,
    uid: UUID,
    eid: UUID,
    index: knowledge.Index,
    question: str,
    names: Sequence[str] = (),
    recent: str = "",
) -> list[knowledge.LiveChunk]:
    """The sections for a question, as the detailed answer gets them live: keywords, the
    same with the names from the conversation when the question is short or points back,
    and the vector search, interleaved."""
    lists = [[c for c, _ in index.search(question, ASK_SECTIONS)]]
    if knowledge.needs_context(question, names):
        said = knowledge.context_names(recent, names)
        if said:
            more = index.search(f"{question} {' '.join(said)}", ASK_SECTIONS)
            lists.append([c for c, _ in more])
    if ctx.embedder is not None:
        try:
            [vec] = await ctx.embedder.embed([question])
            rows = await conn.fetch(
                knowledge.VECTOR_SQL,
                uid,
                eid,
                workplace.vector_literal(vec),
                ctx.embedder.model_id,
                ASK_SECTIONS,
            )
            found = [index.get(r["id"]) for r in rows]
            lists.append([c for c in found if c is not None])
        except Exception as e:
            log.warning("question embedding failed (%s); keyword search only", e)
    return knowledge.merge(*lists, limit=ASK_SECTIONS)


def source_of(ref: NodeRef, index: knowledge.Index) -> dict[str, str]:
    chunk = index.get(ref.node_id)
    if chunk is not None:
        return {"kind": "section", "label": chunk.label()}
    return {"kind": "fact", "label": ref.label}


@handler("knowledge.ask")
async def knowledge_ask(job: Job, ctx: Ctx) -> None:
    """Answer one typed question the way a standup or meeting would: the company's digest
    and pack in the prompt, the same searches, the same types and shapes. The result waits
    in Valkey for the app to fetch."""
    uid = job.uid
    eid = UUID(job.payload["employer_id"])
    question = str(job.payload["question"])
    mode = str(job.payload.get("mode") or "standup")
    key = keys.knowledge_ask(uid, str(job.payload["ask_id"]))
    started = time.monotonic()
    try:
        result = await answer(ctx, uid, eid, question, mode)
    except Exception as e:
        log.warning("test question for employer %s failed (%s)", eid, type(e).__name__)
        result = {"status": "failed", "error": "The question could not be answered. Try again."}
        async with ctx.cache_commit(uid):
            await ctx.valkey.set(key, json.dumps(result), ex=ASK_TTL_S)
        if isinstance(e, ModelOutputError):
            return
        raise
    result["ms"] = int((time.monotonic() - started) * 1000)
    async with ctx.cache_commit(uid):
        await ctx.valkey.set(key, json.dumps(result), ex=ASK_TTL_S)


async def answer(ctx: Ctx, uid: UUID, eid: UUID, question: str, mode: str) -> dict[str, Any]:
    async with ctx.user(uid) as conn:
        user = await conn.fetchrow(
            "select display_name, settings from app.users where id = $1", uid
        )
        if user is None:
            raise ModelOutputError("no such user")
        today = local_today(as_dict(user["settings"]))
        pack = await workplace.render_employer_pack(conn, uid, eid, mode, today)
        if pack is None or pack.employer is None:
            raise ModelOutputError("no such company")
        chunks, _ = await load_chunks(ctx, conn, uid, eid)
        index = knowledge.Index.build(chunks)
        names = [*pack.keyterms, *(pack.knowledge.names if pack.knowledge else [])]
        sections = await find_sections(ctx, conn, uid, eid, index, question, names)
        hits = await workplace.search(conn, uid, eid, question, limit=6)
    refs = ref_map(None, pack)
    shown = {i.node_id for i in pack_items(pack)}
    if pack.knowledge is not None:
        shown |= {r.node_id for r in pack.knowledge.refs.values()}
    memory = workplace.memory_lines(hits, refs, shown)
    memory += knowledge.memory_lines(sections, refs, question, None)
    name = str(user["display_name"])
    qtype = await classify(ctx, name, question, mode)
    pctx = prompts.PromptContext(
        user_name=name,
        mode=mode,
        digest_text=None,
        pack_text=render_pack(pack),
        company=pack.employer.name,
        role=pack.employer.title or None,
        company_digest=pack.knowledge.text if pack.knowledge else None,
    )
    req = prompts.deep_pass(pctx, "", question, qtype, 800, memory)
    text, done = await ctx.router.complete("pack", req)
    if done.stop_reason == "refusal":
        raise ModelOutputError("refused")
    return {
        "status": "done",
        "type": qtype,
        "answer": text.strip(),
        "sources": [source_of(r, index) for r in cited_refs(text, refs)],
        "searched": {"sections": len(sections), "facts": len(memory) - len(sections)},
    }


async def classify(ctx: Ctx, user_name: str, question: str, mode: str) -> str:
    """The type the live detector's prompt gives the question; a typed question is always
    answered, so anything that is not a type counts as `other`."""
    req = prompts.detector(user_name, "", question, 400, mode)
    try:
        data = await complete_json(ctx.router, "pack", req)
    except ModelOutputError:
        return "other"
    found = str(data.get("answer") or "")
    return found if found in (*prompts.QUESTION_TYPES, *prompts.WORK_TYPES) else "other"
