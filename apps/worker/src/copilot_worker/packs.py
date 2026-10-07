"""`digest.rebuild` and `pack.build` (ARCHITECTURE.md, "Session pack and hot digest formats").

The digest is rendered from memory (copilot_worker.memory). A pack is built per call context:
an interview (its job and round), a job picked without an interview, or a standup. Interview
packs come from Sonnet: stories ranked against the job and prepared answers citing the
digest's short ids. Standup packs need no model in phase 1 (no tool sync yet): they carry the
open commitments.

`pack:{uid}:index` points the gateway at the newest pack per job and per mode. A build only
updates the index while its digest is still the current one; otherwise it queues a rebuild
for the new digest, which will.

Prepared answers carry vectors from the worker's embedder, and the pack names its model
(`embedding_model`). A digest rebuild also rebuilds packs whose vectors came from another
embedding model than the current one (the gateway asks for the same when it loads one).
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from copilot_core import jobs, keys
from copilot_core.ids import uuid7
from copilot_core.packs import SHORT_ID, Digest, NodeRef, SessionPack, encode_embedding, render_pack
from copilot_core.valkey_client import Valkey, text
from copilot_worker import prompts
from copilot_worker.context import Ctx
from copilot_worker.llm import as_dict, as_list, complete_json
from copilot_worker.memory import load_digest_nodes, render_digest
from copilot_worker.runtime import Job, handler

log = logging.getLogger(__name__)

PACK_TTL_MAX_S = 30 * 24 * 3600


async def current_digest(ctx: Ctx, uid: UUID) -> Digest | None:
    raw = await ctx.valkey.get(keys.digest(uid))
    return None if raw is None else Digest.model_validate_json(raw)


async def digest_version_of(valkey: Valkey, uid: UUID) -> str | None:
    """The current digest's version, or None before the first digest."""
    raw = await valkey.get(keys.digest(uid))
    return None if raw is None else Digest.model_validate_json(raw).version


async def stored_pack(ctx: Ctx, uid: UUID, pack_id: str) -> SessionPack | None:
    raw = await ctx.valkey.get(keys.pack(uid, pack_id))
    return None if raw is None else SessionPack.model_validate_json(raw)


@handler("digest.rebuild")
async def digest_rebuild(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    async with ctx.user(uid) as conn:
        name = await conn.fetchval("select display_name from app.users where id = $1", uid)
        if name is None:
            return  # the user was deleted
        nodes = await load_digest_nodes(conn, uid)
        interviews = await conn.fetch(
            "select id, pack_id from app.interviews"
            " where user_id = $1 and starts_at > now() - interval '2 hours'",
            uid,
        )
    digest = render_digest(str(name), nodes)
    if digest is None:
        return
    digest.built_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    await ctx.valkey.set(keys.digest(uid), digest.model_dump_json(by_alias=True))
    log.info("digest for %s: %s, %d refs", uid, digest.version[:19], len(digest.refs))

    # Packs built from an older digest are rebuilt: upcoming interviews, and jobs whose pack
    # the gateway would load.
    contexts: list[str] = []
    for row in interviews:
        pack = await stored_pack(ctx, uid, str(row["pack_id"])) if row["pack_id"] else None
        if pack is None or stale(ctx, pack, digest):
            contexts.append(f"interview:{row['id']}")
    index = await ctx.valkey.hgetall(keys.pack_index(uid))
    for field, pack_id in index.items():
        name_ = text(field)
        if not name_.startswith("job:"):
            continue
        pack = await stored_pack(ctx, uid, text(pack_id))
        if pack is None or stale(ctx, pack, digest):
            contexts.append(name_)
    now = datetime.now(UTC)
    embedding = ctx.settings.embedding_model_id
    for context in dict.fromkeys(contexts):
        await ctx.queue.send(
            jobs.pack_build(uid, "interview", context, now, digest.version, embedding)
        )


def stale(ctx: Ctx, pack: SessionPack, digest: Digest) -> bool:
    """Built from an older digest, or its vectors came from another embedding model (when
    there is an embedder to replace them with)."""
    if pack.digest_version != digest.version:
        return True
    current = ctx.embedder.model_id if ctx.embedder is not None else None
    return current is not None and bool(pack.prepared_answers) and pack.embedding_model != current


@handler("pack.build")
async def pack_build(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    mode = str(job.payload.get("mode", "interview"))
    context = str(job.payload["context"])
    due_at = datetime.fromisoformat(str(job.payload["due_at"]))
    digest = await current_digest(ctx, uid)
    if mode == "standup":
        pack = await build_standup_pack(ctx, uid, digest)
        job_id = None
        interview_id = None
    else:
        built = await build_interview_pack(ctx, uid, context, digest)
        if built is None:
            return
        pack, job_id, interview_id = built
    pack.token_count = (len(digest.text) if digest else 0) // 4 + len(render_pack(pack)) // 4
    ttl = int((due_at - datetime.now(UTC)).total_seconds()) + ctx.settings.pack_ttl_s
    ttl = max(ctx.settings.pack_ttl_s, min(ttl, PACK_TTL_MAX_S))
    async with ctx.user(uid) as conn:
        if await conn.fetchval("select 1 from app.users where id = $1", uid) is None:
            return  # the user was deleted meanwhile
    await ctx.valkey.set(
        keys.pack(uid, pack.pack_id), pack.model_dump_json(by_alias=True, exclude_none=True), ex=ttl
    )
    latest = await current_digest(ctx, uid)
    if latest is not None and latest.version != pack.digest_version:
        # The digest changed while this pack was built. A digest rebuild only rebuilds packs
        # it can see in the index, and this one is not there yet, so rebuild it from here:
        # the new digest makes a new key, which runs once.
        log.info("pack %s built from an older digest; rebuilding it", pack.pack_id)
        await ctx.queue.send(
            jobs.pack_build(
                uid, mode, context, due_at, latest.version, ctx.settings.embedding_model_id
            )
        )
        return
    fields = {f"mode:{mode}": str(pack.pack_id)}
    if job_id is not None:
        fields[f"job:{job_id}"] = str(pack.pack_id)
    if interview_id is not None:
        fields[f"interview:{interview_id}"] = str(pack.pack_id)
    await ctx.valkey.hset(keys.pack_index(uid), mapping=fields)
    if interview_id is not None:
        async with ctx.user(uid) as conn:
            await conn.execute(
                "update app.interviews set pack_id = $2 where id = $1", interview_id, pack.pack_id
            )
    log.info("pack %s for %s (%s): %s", pack.pack_id, uid, context, sorted(fields))


async def build_interview_pack(
    ctx: Ctx, uid: UUID, context: str, digest: Digest | None
) -> tuple[SessionPack, UUID, UUID | None] | None:
    kind, _, ref = context.partition(":")
    async with ctx.user(uid) as conn:
        if kind == "interview":
            row = await conn.fetchrow(
                "select i.id as interview_id, i.round, j.id as job_id, j.company, j.title,"
                " j.description from app.interviews i join app.jobs j on j.id = i.job_id"
                " where i.id = $1",
                UUID(ref),
            )
        else:
            row = await conn.fetchrow(
                "select null::uuid as interview_id, null as round, id as job_id, company, title,"
                " description from app.jobs where id = $1",
                UUID(ref),
            )
        if row is None:
            log.info("pack context %s is gone", context)
            return None
        stories = {
            r["id"]: r
            for r in await conn.fetch(
                "select id, title, summary, body from app.memory_nodes"
                " where user_id = $1 and kind = 'story' and status = 'active' order by id",
                uid,
            )
        }
    round_ = str(row["round"] or "general")
    req = prompts.pack_request(
        digest.text if digest else None,
        str(row["company"]),
        str(row["title"]),
        round_,
        str(row["description"]),
        ctx.settings.prepared_answers,
    )
    data = await complete_json(ctx.router, "pack", req)
    refs: dict[str, NodeRef] = dict(digest.refs) if digest else {}
    by_node = {r.node_id: short for short, r in refs.items()}

    ranked: list[dict[str, Any]] = []
    ranked_ids = [short_id(s) for s in as_list(data.get("ranked_story_ids"))]
    order = [refs[s].node_id for s in ranked_ids if s in refs]
    order += [n for n in stories if n not in order]
    for node_id in order:
        story = stories.get(node_id)
        if story is None or node_id not in by_node:
            continue
        body = as_dict(story["body"])
        ranked.append(
            {
                "node_id": str(node_id),
                "short_id": by_node[node_id],
                "title": story["title"],
                "one_liner": story["summary"],
                "competencies": [str(c) for c in as_list(body.get("competencies"))],
            }
        )
    prepared: list[dict[str, Any]] = []
    for answer in map(as_dict, as_list(data.get("prepared_answers"))):
        points = [
            _keep_known_citations(str(p), refs) for p in as_list(answer.get("talking_points"))
        ]
        points = [p for p in points if p]
        if not answer.get("question") or not points:
            continue
        cited = dict.fromkeys(s for p in points for s in SHORT_ID.findall(p))
        prepared.append(
            {
                "question": str(answer["question"]),
                "talking_points": points,
                "node_ids": [str(refs[s].node_id) for s in cited],
            }
        )
    pack = SessionPack.model_validate(
        {
            "pack_id": str(uuid7()),
            "user_id": str(uid),
            "mode": "interview",
            "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "digest_version": digest.version if digest else "",
            "context": {
                "company": row["company"],
                "role_title": row["title"],
                "round": row["round"],
                "job_summary": str(data.get("job_summary", "")),
            },
            "ranked_stories": ranked,
            "prepared_answers": prepared,
        }
    )
    await embed_prepared(ctx, pack)
    return pack, row["job_id"], row["interview_id"]


def short_id(value: object) -> str:
    """A ranked story id as the model wrote it: `n:3f2a`, `[n:3f2a]` or a bare `3f2a` (OpenAI
    models drop the prefix), to the digest's form."""
    text = str(value).strip().strip("[]").strip()
    return text if text.startswith("n:") else f"n:{text}"


def _keep_known_citations(point: str, refs: dict[str, NodeRef]) -> str:
    """Drop citations of short ids the digest does not have (they would point nowhere)."""
    return re.sub(
        r"\s*\[(n:[0-9a-z]{2,8})\]", lambda m: m.group(0) if m.group(1) in refs else "", point
    ).strip()


async def embed_prepared(ctx: Ctx, pack: SessionPack) -> None:
    if ctx.embedder is None or not pack.prepared_answers:
        return
    try:
        vectors = await ctx.embedder.embed([a.question for a in pack.prepared_answers])
    except Exception as e:  # matching is an optimization; the pack works without it
        log.warning("no embeddings for pack %s (%s); prepared-answer matching off", pack.pack_id, e)
        return
    for answer, vec in zip(pack.prepared_answers, vectors, strict=True):
        answer.embedding_b64 = encode_embedding(vec)
    pack.embedding_model = ctx.embedder.model_id


async def build_standup_pack(ctx: Ctx, uid: UUID, digest: Digest | None) -> SessionPack:
    async with ctx.user(uid) as conn:
        rows = await conn.fetch(
            "select id, title, due_date from app.commitments"
            " where user_id = $1 and coalesce(state, 'open') = 'open' order by due_date nulls last",
            uid,
        )
    open_commitments = [
        {"node_id": str(r["id"]), "text": r["title"], "due_date": r["due_date"]} for r in rows
    ]
    return SessionPack.model_validate(
        {
            "pack_id": str(uuid7()),
            "user_id": str(uid),
            "mode": "standup",
            "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "digest_version": digest.version if digest else "",
            "standup": {
                "yesterday": [],
                "today": [],
                "blockers": [],
                "open_commitments": open_commitments,
            },
        }
    )
