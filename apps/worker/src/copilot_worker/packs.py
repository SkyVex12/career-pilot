"""`digest.rebuild` and `pack.build` (ARCHITECTURE.md, "Session pack and hot digest formats").

Each resume version's digest is rendered from that version's memory (copilot_worker.memory). A
pack is built per call context: an interview (its application and round), an application
picked without an interview, or a standup. Interview packs come from Sonnet, from the
application's JD and its paired resume version only (copilot_core.resumes): that version's
digest, its stories ranked against the JD, and prepared answers citing its short ids. Standup
packs need no model in phase 1 (no tool sync yet): they carry the open commitments.

`pack:{uid}:index` points the gateway at the newest pack per application, interview and (for
standups) mode. A build only updates the index while its pairing and digest are still the
current ones; otherwise it queues a rebuild for them, which will.

Prepared answers carry vectors from the worker's embedder, and the pack names its model
(`embedding_model`); the build key names it too, so a new embedding model rebuilds the packs.
"""

import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from copilot_core import jobs, keys, resumes
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
    """The base resume's digest (`digest:{uid}`): standups, meetings, sessions for no
    application."""
    raw = await ctx.valkey.get(keys.digest(uid))
    return None if raw is None else Digest.model_validate_json(raw)


async def digest_version_of(valkey: Valkey, uid: UUID) -> str | None:
    """The base digest's version, or None before the first digest."""
    raw = await valkey.get(keys.digest(uid))
    return None if raw is None else Digest.model_validate_json(raw).version


async def stored_pack(ctx: Ctx, uid: UUID, pack_id: str) -> SessionPack | None:
    raw = await ctx.valkey.get(keys.pack(uid, pack_id))
    return None if raw is None else SessionPack.model_validate_json(raw)


@handler("digest.rebuild")
async def digest_rebuild(job: Job, ctx: Ctx) -> None:
    """One resume version's digest (`resume_version_id`), or every version's (the weekly
    rebuild, and messages from before resume versions). Each is rendered from that version's
    nodes only; the base's is also `digest:{uid}`. Then the packs of the applications paired
    with a rebuilt version are built for its new digest."""
    uid = job.uid
    only = job.payload.get("resume_version_id")
    async with ctx.user(uid) as conn:
        name = await conn.fetchval("select display_name from app.users where id = $1", uid)
        if name is None:
            return  # the user was deleted
        versions = await conn.fetch(
            "select id, kind from app.resume_versions where user_id = $1"
            " and ($2::uuid is null or id = $2) order by id",
            uid,
            UUID(str(only)) if only else None,
        )
        nodes = {v["id"]: await load_digest_nodes(conn, uid, v["id"]) for v in versions}
    built_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for v in versions:
        digest = render_digest(str(name), nodes[v["id"]])
        targets = [keys.resume_digest(uid, v["id"])]
        if v["kind"] == "base":
            targets.append(keys.digest(uid))
        if digest is None:
            await ctx.valkey.delete(*targets)
            continue
        digest.built_at = built_at
        for key in targets:
            await ctx.valkey.set(key, digest.model_dump_json(by_alias=True))
        log.info(
            "digest for %s, version %s: %s, %d refs",
            uid, v["id"], digest.version[:19], len(digest.refs),
        )  # fmt: skip
    messages: list[dict[str, Any]] = []
    embedding = ctx.settings.embedding_model_id
    async with ctx.user(uid) as conn:
        seen: set[UUID] = set()
        for v in versions:
            for app_id in await resumes.applications_using(conn, v["id"]):
                if app_id not in seen:
                    seen.add(app_id)
                    messages += await resumes.pack_messages(
                        conn, ctx.valkey, uid, app_id, embedding
                    )
    await send_new(ctx, uid, messages)


async def send_new(ctx: Ctx, uid: UUID, messages: list[dict[str, Any]]) -> None:
    """Send the builds whose pack is not current: the indexed pack for that context was built
    for the same pairing, from the same digest, with vectors from the current embedder."""
    index = await ctx.valkey.hgetall(keys.pack_index(uid))
    by_field = {text(k): text(v) for k, v in index.items()}
    current = ctx.embedder.model_id if ctx.embedder is not None else None
    for message in messages:
        payload = message["payload"]
        pack_id = by_field.get(str(payload["context"]))
        pack = await stored_pack(ctx, uid, pack_id) if pack_id else None
        if (
            pack is not None
            and pack.pairing is not None
            and message["idempotency_key"].endswith(f":{pack.pairing}")
            and f":{jobs.digest_tag(pack.digest_version or None)}:" in message["idempotency_key"]
            and not (current and pack.prepared_answers and pack.embedding_model != current)
        ):
            continue
        await ctx.queue.send(message)


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
    if context.startswith("employer:"):
        from copilot_worker.workplace import build_employer_pack

        await build_employer_pack(ctx, job)
        return
    due_at = datetime.fromisoformat(str(job.payload["due_at"]))
    job_id: UUID | None = None
    interview_id: UUID | None = None
    paired: resumes.Pairing | None = None
    used: Digest | None
    if mode == "standup":
        used = await current_digest(ctx, uid)
        pack = await build_standup_pack(ctx, uid, used)
    else:
        built = await build_interview_pack(ctx, uid, context)
        if built is None:
            return
        pack, job_id, interview_id, paired, used = built
    pack.token_count = (len(used.text) if used else 0) // 4 + len(render_pack(pack)) // 4
    ttl = int((due_at - datetime.now(UTC)).total_seconds()) + ctx.settings.pack_ttl_s
    ttl = max(ctx.settings.pack_ttl_s, min(ttl, PACK_TTL_MAX_S))
    async with ctx.user(uid) as conn:
        if await conn.fetchval("select 1 from app.users where id = $1", uid) is None:
            return  # the user was deleted meanwhile
        now_paired = await resumes.pairing(conn, job_id) if job_id is not None else None
    await ctx.valkey.set(
        keys.pack(uid, pack.pack_id), pack.model_dump_json(by_alias=True, exclude_none=True), ex=ttl
    )
    if job_id is not None and paired is not None:
        # The pairing, the JD or the version's digest changed while this pack was built: build
        # again for what is current (a new key, so it runs once) and leave the index alone, so
        # the gateway never loads a pack of a version the application no longer uses.
        latest = await resumes.version_digest(
            ctx.valkey, uid, paired.resume_version_id, paired.is_base
        )
        if now_paired != paired or (latest.version if latest else "") != pack.digest_version:
            log.info("pack %s is out of date already; rebuilding it", pack.pack_id)
            if now_paired is not None:
                async with ctx.user(uid) as conn:
                    messages = await resumes.pack_messages(
                        conn, ctx.valkey, uid, job_id, ctx.settings.embedding_model_id
                    )
                for message in messages:
                    await ctx.queue.send(message)
            return
    else:
        latest = await current_digest(ctx, uid)
        if latest is not None and latest.version != pack.digest_version:
            log.info("pack %s built from an older digest; rebuilding it", pack.pack_id)
            await ctx.queue.send(
                jobs.pack_build(
                    uid, mode, context, due_at, latest.version, ctx.settings.embedding_model_id
                )
            )
            return
    # An application's pack is indexed under its application and interview only, never as
    # the mode's: a session for no application must not get one application's resume.
    fields: dict[str, str] = {}
    if job_id is None:
        fields[f"mode:{mode}"] = str(pack.pack_id)
    else:
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
    ctx: Ctx, uid: UUID, context: str
) -> tuple[SessionPack, UUID, UUID | None, resumes.Pairing, Digest | None] | None:
    """An application's interview pack from its JD and its paired resume version only: that
    version's digest and stories. No other version's facts can reach it."""
    kind, _, ref = context.partition(":")
    async with ctx.user(uid) as conn:
        if kind == "interview":
            row = await conn.fetchrow(
                "select i.id as interview_id, i.round, a.id as job_id, a.company, a.role,"
                " a.jd_text from app.interviews i join app.applications a on a.id = i.job_id"
                " where i.id = $1",
                UUID(ref),
            )
        else:
            row = await conn.fetchrow(
                "select null::uuid as interview_id, null as round, id as job_id, company, role,"
                " jd_text from app.applications where id = $1",
                UUID(ref),
            )
        if row is None:
            log.info("pack context %s is gone", context)
            return None
        paired = await resumes.pairing(conn, row["job_id"])
        assert paired is not None
        stories = {
            r["id"]: r
            for r in await conn.fetch(
                "select id, title, summary, body from app.memory_nodes"
                " where user_id = $1 and resume_version_id = $2 and kind = 'story'"
                " and status = 'active' order by id",
                uid,
                paired.resume_version_id,
            )
        }
    digest = await resumes.version_digest(ctx.valkey, uid, paired.resume_version_id, paired.is_base)
    round_ = str(row["round"] or "general")
    req = prompts.pack_request(
        digest.text if digest else None,
        str(row["company"]),
        str(row["role"]),
        round_,
        str(row["jd_text"]),
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
    version = paired.resume_version_id
    pack = SessionPack.model_validate(
        {
            "pack_id": str(uuid7()),
            "user_id": str(uid),
            "mode": "interview",
            "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "digest_version": digest.version if digest else "",
            "context": {
                "company": row["company"],
                "role_title": row["role"],
                "round": row["round"],
                "job_summary": str(data.get("job_summary", "")),
            },
            "ranked_stories": ranked,
            "prepared_answers": prepared,
            "resume_version_id": str(version) if version else None,
            "pairing": resumes.pairing_tag(paired),
        }
    )
    await embed_prepared(ctx, pack)
    return pack, row["job_id"], row["interview_id"], paired, digest


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
            " where user_id = $1 and coalesce(state, 'open') = 'open' and employer_id is null"
            " order by due_date nulls last",
            uid,
        )
    open_commitments = [
        {"node_id": str(r["id"]), "text": r["title"], "due_date": r["due_date"] or None}
        for r in rows
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
