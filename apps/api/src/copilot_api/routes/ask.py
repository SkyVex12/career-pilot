"""Central Ask: private conversations, bounded retrieval, and cancellable generation."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import AccountDep, Services, SvcDep, UserDep, require_link
from copilot_core import ask, personal_ai
from copilot_core.auth import Principal
from copilot_core.crypto import segment_row_id
from copilot_core.db import Conn
from copilot_core.llm import ModelRouter, RefusedError, Route
from copilot_core.providers import make_providers, openai_params

router = APIRouter(prefix="/ask", tags=["ask"], dependencies=[Depends(require_link)])
# Local cancellation is an optimization. The database attempt/status guard is authoritative
# across processes, and cancellation on another replica is observed while generating.
_running: dict[UUID, asyncio.Task[Any]] = {}


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="New conversation", min_length=1, max_length=200)
    scope: ask.Scope = Field(default_factory=ask.Scope)


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    scope: ask.Scope | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    question: str = Field(min_length=1, max_length=8000)
    scope: ask.Scope | None = None


def obj(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


def conversation(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "scope": obj(row["scope"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def require_conversation(conn: Conn, cid: UUID, profile_id: UUID, lock: bool = False) -> Any:
    row = await conn.fetchrow(
        "select * from app.ask_conversations where id=$1 and profile_id=$2"
        + (" for update" if lock else ""),
        cid,
        profile_id,
    )
    if row is None:
        raise HTTPException(404, "conversation unavailable in this profile")
    return row


async def check_scope(conn: Conn, scope: ask.Scope, account_id: UUID | None = None) -> None:
    tables = {
        "company": "employers",
        "application": "applications",
        "document": "company_documents",
        "call": "sessions",
    }
    if scope.kind == "call":
        row = await conn.fetchval(
            "select id from app.sessions where id=$1 and (private_context_account_id is null "
            "or private_context_account_id=$2)",
            scope.id,
            account_id,
        )
        if row is None:
            raise HTTPException(404, "context unavailable in this account/profile")
        return
    if scope.kind in tables:
        row = await conn.fetchval(f"select id from app.{tables[scope.kind]} where id=$1", scope.id)
        if row is None:
            raise HTTPException(404, "context unavailable in this profile")


async def authorize_scope(
    conn: Conn, account: Principal, user: Principal, scope: ask.Scope
) -> None:
    # Both principals were authenticated by dependencies. Switch only for the source check,
    # then restore account RLS before touching private conversation data (no nested pool lease).
    await conn.execute("select set_config('app.user_id',$1,true)", str(user.user_id))
    try:
        await check_scope(conn, scope, account.user_id)
    finally:
        await conn.execute("select set_config('app.user_id',$1,true)", str(account.user_id))


async def expire(conn: Conn, cid: UUID) -> None:
    await conn.execute(
        "update app.ask_messages set status='failed',answer_enc=null,answer_key_version=null,error="
        "'Generation interrupted; retry explicitly.',updated_at=now() "
        "where conversation_id=$1 and status='pending' "
        "and updated_at < now()-interval '5 minutes'",
        cid,
    )


async def message(conn: Conn, svc: Services, user: Principal, row: Any) -> dict[str, Any]:
    async def decrypt(column: str, version: str) -> str:
        if row[column] is None:
            return ""
        return await svc.crypto.decrypt(
            conn, user.user_id, "ask_messages", str(row["id"]), row[column], row[version]
        )

    return {
        "id": str(row["id"]),
        "request_id": str(row["request_id"]),
        "question": await decrypt("question_enc", "key_version"),
        "answer": await decrypt("answer_enc", "answer_key_version"),
        "sources": json.loads(await decrypt("sources_enc", "sources_key_version") or "[]"),
        "status": row["status"],
        "error": row["error"],
        "scope": obj(row["scope"]),
        "created_at": row["created_at"],
    }


@router.get("/conversations")
async def list_conversations(svc: SvcDep, user: UserDep, account: AccountDep) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            "select * from app.ask_conversations where profile_id=$1 "
            "order by updated_at desc limit 100",
            user.user_id,
        )
    return {"conversations": [conversation(row) for row in rows]}


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: ConversationIn, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        await authorize_scope(conn, account, user, body.scope)
        row = await conn.fetchrow(
            "insert into app.ask_conversations(id,user_id,title,scope,profile_id) "
            "values($1,$2,$3,$4::jsonb,$5) returning *",
            uuid4(),
            account.user_id,
            body.title.strip() or "New conversation",
            body.scope.model_dump_json(),
            user.user_id,
        )
    return conversation(row)


@router.get("/conversations/{cid}")
async def get_conversation(
    cid: UUID, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        row = await require_conversation(conn, cid, user.user_id)
        await expire(conn, cid)
        rows = await conn.fetch(
            "select * from app.ask_messages where conversation_id=$1 order by created_at", cid
        )
        messages = [await message(conn, svc, account, item) for item in rows]
    return {
        **conversation(row),
        "messages": messages,
        "retrieval_notice": "Search uses bounded recent source windows. Narrow by dates or "
        "call/document to find older information. Absence of evidence is not proof of absence.",
        "retention_notice": "Answers retain excerpts until this conversation is deleted. "
        "Deleting a source prevents new retrieval but does not erase earlier answers.",
    }


@router.patch("/conversations/{cid}")
async def patch_conversation(
    cid: UUID, body: ConversationPatch, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        await require_conversation(conn, cid, user.user_id, True)
        if body.scope:
            await authorize_scope(conn, account, user, body.scope)
        row = await conn.fetchrow(
            "update app.ask_conversations set title=coalesce($2,title), "
            "scope=coalesce($3::jsonb,scope),updated_at=now() where id=$1 returning *",
            cid,
            body.title,
            body.scope.model_dump_json() if body.scope else None,
        )
    return conversation(row)


@router.delete("/conversations/{cid}", status_code=204)
async def delete_conversation(cid: UUID, svc: SvcDep, user: UserDep, account: AccountDep) -> None:
    async with svc.tx(account) as conn:
        await require_conversation(conn, cid, user.user_id, True)
        mids = await conn.fetch("select id from app.ask_messages where conversation_id=$1", cid)
        await conn.execute("delete from app.ask_conversations where id=$1", cid)
    for row in mids:
        if task := _running.get(row["id"]):
            task.cancel()


@router.post("/conversations/{cid}/messages", status_code=202)
async def post_message(
    cid: UUID,
    body: MessageIn,
    background: BackgroundTasks,
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
) -> dict[str, Any]:
    if not body.question.strip():
        raise HTTPException(422, "question must not be blank")
    async with svc.tx(account) as conn:
        convo = await require_conversation(conn, cid, user.user_id, True)
        await expire(conn, cid)
        old = await conn.fetchrow(
            "select * from app.ask_messages where conversation_id=$1 and request_id=$2",
            cid,
            body.request_id,
        )
        if old:
            previous = await message(conn, svc, account, old)
            if previous["question"] != body.question.strip() or (
                body.scope and obj(old["scope"]) != body.scope.model_dump(mode="json")
            ):
                raise HTTPException(409, "request_id already belongs to another message")
            return previous
        if await conn.fetchval(
            "select 1 from app.ask_messages where conversation_id=$1 and status='pending'", cid
        ):
            raise HTTPException(409, "wait for or cancel the pending answer")
        count = await conn.fetchval(
            "select count(*) from app.ask_messages where conversation_id=$1", cid
        )
        if count >= 200:
            raise HTTPException(409, "start a new conversation after 200 messages")
        scope = body.scope or ask.Scope.model_validate(obj(convo["scope"]))
        await authorize_scope(conn, account, user, scope)
        mid = uuid4()
        sealed, version = await svc.crypto.encrypt(
            conn, account.user_id, "ask_messages", str(mid), body.question.strip()
        )
        row = await conn.fetchrow(
            "insert into app.ask_messages(id,user_id,conversation_id,request_id,"
            "scope,question_enc,key_version) values($1,$2,$3,$4,$5::jsonb,$6,$7) "
            "returning *",
            mid,
            account.user_id,
            cid,
            body.request_id,
            scope.model_dump_json(),
            sealed,
            version,
        )
        await conn.execute(
            "update app.ask_conversations set scope=$2::jsonb,updated_at=now(), "
            "title=case when title='New conversation' then $3 else title end where id=$1",
            cid,
            scope.model_dump_json(),
            body.question.strip()[:80],
        )
        result = await message(conn, svc, account, row)
    background.add_task(generate, svc, user, account, cid, mid, 1)
    return result


@router.post("/conversations/{cid}/messages/{mid}/cancel")
async def cancel_message(
    cid: UUID, mid: UUID, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        await require_conversation(conn, cid, user.user_id, True)
        row = await conn.fetchrow(
            "update app.ask_messages set status=case when status='pending' "
            "then 'cancelled' else status end,updated_at=now() "
            "where id=$1 and conversation_id=$2 returning *",
            mid,
            cid,
        )
        if row is None:
            raise HTTPException(404, "message unavailable")
        result = await message(conn, svc, account, row)
    if task := _running.get(mid):
        task.cancel()
    return result


@router.post("/conversations/{cid}/messages/{mid}/retry", status_code=202)
async def retry_message(
    cid: UUID,
    mid: UUID,
    background: BackgroundTasks,
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        await require_conversation(conn, cid, user.user_id, True)
        await expire(conn, cid)
        row = await conn.fetchrow(
            "select * from app.ask_messages where id=$1 and conversation_id=$2", mid, cid
        )
        if row is None:
            raise HTTPException(404, "message unavailable")
        if row["status"] in ("pending", "done"):
            return await message(conn, svc, account, row)
        if await conn.fetchval(
            "select 1 from app.ask_messages where conversation_id=$1 and status='pending'", cid
        ):
            raise HTTPException(409, "another answer is pending")
        await authorize_scope(conn, account, user, ask.Scope.model_validate(obj(row["scope"])))
        row = await conn.fetchrow(
            "update app.ask_messages set status='pending',error=null,"
            "answer_enc=null,answer_key_version=null,sources_enc=null,sources_key_version=null,"
            "attempt=attempt+1,updated_at=now() where id=$1 returning *",
            mid,
        )
        result = await message(conn, svc, account, row)
    assert row is not None
    background.add_task(generate, svc, user, account, cid, mid, row["attempt"])
    return result


def source(
    kind: str, sid: Any, label: str, content: str, when: Any, target_id: Any = None, **extra: Any
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": str(sid),
        "target_id": str(target_id or sid),
        "label": label,
        "content": content[:8000],
        "date": str(when),
        **extra,
    }


async def retrieve(
    conn: Conn,
    svc: Services,
    user: Principal,
    scope: ask.Scope,
    question: str,
    account_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Decrypt a bounded candidate window, rank locally, send at most 12 excerpts.

    No plaintext index/cache duplicates the authoritative stores. Windows are deliberately
    bounded; the UI/API discloses this rather than claiming exhaustive historical recall.
    """
    if scope.kind == "general":
        return []
    await check_scope(conn, scope, account_id)
    candidates: list[dict[str, Any]] = []
    company = scope.id if scope.kind == "company" else None
    application = scope.id if scope.kind == "application" else None
    call = scope.id if scope.kind == "call" else None
    document = scope.id if scope.kind == "document" else None
    if scope.kind in ("auto", "all", "company", "application"):
        rows = await conn.fetch(
            "select n.*, e.name as employer_name from app.memory_nodes n "
            "left join app.employers e on e.id=n.employer_id where n.status='active' "
            "and ($1::uuid is null or n.employer_id=$1) "
            "and ($2::uuid is null or (n.domain='career' and (n.resume_version_id is null or "
            "n.resume_version_id in (select id from app.resume_versions "
            "where application_id=$2 or kind='base')))) "
            "and ($4::date is null or n.updated_at::date >= $4) "
            "and ($5::date is null or n.updated_at::date <= $5) "
            "order by ts_rank(n.search,websearch_to_tsquery('english',$3)) desc,"
            "n.updated_at desc limit 200",
            company,
            application,
            question,
            scope.from_date,
            scope.to_date,
        )
        for row in rows:
            candidates.append(
                source(
                    "fact",
                    row["id"],
                    ((row["employer_name"] + ": ") if row["employer_name"] else "") + row["title"],
                    "Kind: "
                    + row["kind"]
                    + "\n"
                    + row["summary"]
                    + "\n"
                    + json.dumps(obj(row["body"])),
                    row["updated_at"],
                    record_type="approved fact",
                    employer_id=str(row["employer_id"]) if row["employer_id"] else None,
                )
            )
    if scope.kind in ("auto", "all", "application"):
        for row in await conn.fetch(
            "select * from app.applications where "
            "($1::uuid is null or id=$1) "
            "and ($2::date is null or updated_at::date >= $2) "
            "and ($3::date is null or updated_at::date <= $3) order by updated_at desc limit 60",
            application,
            scope.from_date,
            scope.to_date,
        ):
            candidates.append(
                source(
                    "application",
                    row["id"],
                    row["company"] + " — " + row["role"],
                    row["jd_text"] + "\nStage: " + row["stage"] + "\n" + (row["note"] or ""),
                    row["updated_at"],
                    record_type="application",
                )
            )
    if scope.kind in ("auto", "all", "company", "document"):
        for row in await conn.fetch(
            "select * from app.knowledge_chunks where "
            "($1::uuid is null or employer_id=$1) and "
            "($2::uuid is null or document_id=$2) "
            "and ($3::date is null or created_at::date >= $3) "
            "and ($4::date is null or created_at::date <= $4) "
            "order by created_at desc,ord limit 1000",
            company,
            document,
            scope.from_date,
            scope.to_date,
        ):
            text = await svc.crypto.decrypt(
                conn,
                user.user_id,
                "knowledge_chunks",
                str(row["id"]),
                row["body_enc"],
                row["key_version"],
            )
            data = json.loads(text)
            candidates.append(
                source(
                    "document",
                    row["id"],
                    data["doc"] + " > " + data.get("path", ""),
                    data["text"],
                    row["created_at"],
                    row["document_id"],
                    employer_id=str(row["employer_id"]),
                    record_type="original document",
                )
            )
    if scope.kind in ("auto", "all", "company"):
        for row in await conn.fetch(
            "select * from app.work_logs where "
            "($1::uuid is null or employer_id=$1) "
            "and ($2::date is null or day >= $2) and ($3::date is null or day <= $3) "
            "order by day desc limit 90",
            company,
            scope.from_date,
            scope.to_date,
        ):
            text = await svc.crypto.decrypt(
                conn, user.user_id, "work_logs", str(row["id"]), row["text_enc"], row["key_version"]
            )
            candidates.append(
                source(
                    "work_log",
                    row["id"],
                    "Work log " + str(row["day"]),
                    text,
                    row["day"],
                    employer_id=str(row["employer_id"]),
                    record_type="original work log",
                )
            )
    if scope.kind in ("auto", "all", "company", "application", "call"):
        sessions = await conn.fetch(
            "select * from app.sessions where ended_at is not null "
            "and ($1::uuid is null or employer_id=$1) and ($2::uuid is null or job_id=$2) "
            "and ($3::uuid is null or id=$3) and ($4::date is null or started_at::date >= $4) "
            "and ($5::date is null or started_at::date <= $5) "
            "and (private_context_account_id is null or private_context_account_id=$6) "
            "order by started_at desc limit 100",
            company,
            application,
            call,
            scope.from_date,
            scope.to_date,
            account_id,
        )
        by_id = {row["id"]: row for row in sessions}
        ids = list(by_id)
        if ids:
            for row in await conn.fetch(
                "select * from app.session_outputs where session_id=any($1::uuid[]) "
                "and kind in ('summary','standup_report') order by created_at desc limit 200",
                ids,
            ):
                text = await svc.crypto.decrypt(
                    conn,
                    user.user_id,
                    "session_outputs",
                    str(row["id"]),
                    row["content_enc"],
                    row["key_version"],
                )
                candidates.append(
                    source(
                        "summary",
                        row["id"],
                        by_id[row["session_id"]]["title"] or "Saved call",
                        text,
                        row["created_at"],
                        row["session_id"],
                        record_type="generated summary",
                    )
                )
            stored = [sid for sid, row in by_id.items() if row["store_transcript"]]
            # Round-robin segment order keeps a long recent call from crowding out other calls.
            for row in await conn.fetch(
                "select t.*,s.started_at,s.title from (select *,row_number() over "
                "(partition by session_id order by start_ms) as ordinal "
                "from app.transcript_segments "
                "where session_id=any($1::uuid[])) t join app.sessions s on s.id=t.session_id "
                "order by t.ordinal,s.started_at desc limit 2000",
                stored,
            ):
                sid = segment_row_id(row["session_id"], row["channel"], row["seg_id"])
                text = await svc.crypto.decrypt(
                    conn,
                    user.user_id,
                    "transcript_segments",
                    sid,
                    row["text_enc"],
                    row["key_version"],
                )
                candidates.append(
                    source(
                        "transcript",
                        sid,
                        row["title"] or "Saved call",
                        text,
                        row["started_at"],
                        row["session_id"],
                        start_ms=row["start_ms"],
                        channel=row["channel"],
                        record_type="original transcript",
                    )
                )
    if scope.from_date:
        candidates = [s for s in candidates if s["date"][:10] >= str(scope.from_date)]
    if scope.to_date:
        candidates = [s for s in candidates if s["date"][:10] <= str(scope.to_date)]
    ranked = ask.rank(question, candidates)
    # An explicit item is useful even for follow-ups such as "summarize it".
    if not ranked and scope.kind in ("application", "document", "call"):
        ranked = candidates[:12]
    return ranked


async def answer(
    svc: Services,
    question: str,
    history: list[dict[str, str]],
    sources: list[dict[str, Any]],
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    providers = make_providers(svc.settings, timeout_s=120)
    try:
        routes: list[Route] = []
        for provider in providers:
            if provider.name == "openai":
                model, _ = svc.settings.openai_route("pack")
                params = openai_params(svc.settings, "pack")
            else:
                model = (
                    svc.settings.bedrock_sonnet_model
                    if provider.name == "bedrock"
                    else svc.settings.sonnet_model
                )
                params = {"thinking": {"type": "adaptive"}}
            routes.append(Route(provider, model, params))
        model_router = ModelRouter({"ask": routes}, first_token_timeout_s=60)
        text = ""
        done = None
        last_write = 0.0
        async for chunk in model_router.stream("ask", ask.request(question, history, sources)):
            if isinstance(chunk, str):
                text += chunk
                now = asyncio.get_running_loop().time()
                if progress is not None and now - last_write >= 0.5:
                    await progress(ask.partial_answer(text))
                    last_write = now
            else:
                done = chunk
        if done is None:
            raise RuntimeError("model stream ended without completion")
        if done.stop_reason == "refusal":
            raise RefusedError("ask", done)
        if done.stop_reason == "max_tokens":
            text += "\n\n[Answer reached its length limit; ask a follow-up to continue.]"
        return text
    finally:
        for provider in providers:
            client = getattr(provider, "client", None)
            if client is not None:
                await client.close()


async def generate(
    svc: Services, user: Principal, account: Principal, cid: UUID, mid: UUID, attempt: int
) -> None:
    current = asyncio.current_task()
    if current is not None:
        _running[mid] = current
    model_task: asyncio.Task[str] | None = None
    try:
        async with svc.tx(account) as conn:
            row = await conn.fetchrow(
                "select * from app.ask_messages where id=$1 and status='pending' and attempt=$2",
                mid,
                attempt,
            )
            if row is None:
                return
            original = await message(conn, svc, account, row)
            scope = ask.Scope.model_validate(obj(row["scope"]))
            previous = await conn.fetch(
                "select * from app.ask_messages where conversation_id=$1 "
                "and status='done' and created_at<$2 and scope=$3::jsonb "
                "order by created_at desc limit 8",
                cid,
                row["created_at"],
                scope.model_dump_json(),
            )
            history: list[dict[str, str]] = []
            for earlier in reversed(previous):
                item = await message(conn, svc, account, earlier)
                history.append(
                    {"question": item["question"][:2000], "answer": item["answer"][:4000]}
                )
            query = original["question"]
            if history:
                query = history[-1]["question"] + "\n" + query
        async with svc.tx(user) as conn:
            sources = await retrieve(conn, svc, user, scope, query, account.user_id)
        if scope.kind in ("auto", "all", "company", "application"):
            async with svc.tx(account) as conn:
                briefs = await personal_ai.latest_briefs(
                    conn,
                    svc.crypto,
                    account.user_id,
                    user.user_id,
                    employer_id=scope.id if scope.kind == "company" else None,
                    job_id=scope.id if scope.kind == "application" else None,
                )
            if scope.kind in ("auto", "all", "application"):
                async with svc.tx(account) as conn:
                    rows = await conn.fetch(
                        "select * from app.practice_sessions where profile_id=$1 "
                        "order by updated_at desc limit 30",
                        user.user_id,
                    )
                    for row in rows:
                        data = json.loads(
                            await svc.crypto.decrypt(
                                conn,
                                account.user_id,
                                "practice_sessions",
                                str(row["id"]),
                                row["state_enc"],
                                row["key_version"],
                            )
                        )
                        if scope.kind == "application" and data["setup"].get(
                            "application_id"
                        ) != str(scope.id):
                            continue
                        when = str(row["updated_at"])
                        if scope.from_date and when[:10] < str(scope.from_date):
                            continue
                        if scope.to_date and when[:10] > str(scope.to_date):
                            continue
                        excerpt = practice_excerpt(data)
                        if excerpt:
                            sources.append(
                                source(
                                    "practice",
                                    row["id"],
                                    "Practice: " + data["setup"]["topic"],
                                    excerpt,
                                    when,
                                    record_type="tentative practice response",
                                )
                            )
            for brief in briefs:
                when = str(brief["prepared_at"])
                if scope.from_date and when[:10] < str(scope.from_date):
                    continue
                if scope.to_date and when[:10] > str(scope.to_date):
                    continue
                sources.append(
                    source(
                        "private_brief",
                        brief["id"],
                        brief["title"],
                        brief["content"],
                        brief["prepared_at"],
                        record_type="user-provided private brief",
                    )
                )
            if scope.kind in ("auto", "all"):
                sources.extend(await calendar_sources(svc, account, user, scope))
            sources = ask.rank(query, sources) or sources[:12]

        async def progress(text: str) -> None:
            async with svc.tx(account) as conn:
                # Same account RLS and locked attempt guard as the final write.
                row = await conn.fetchrow(
                    "select id from app.ask_messages where id=$1 and status='pending' "
                    "and attempt=$2 for update",
                    mid,
                    attempt,
                )
                if row is None:
                    raise asyncio.CancelledError
                sealed, version = await svc.crypto.encrypt(
                    conn, account.user_id, "ask_messages", str(mid), text
                )
                await conn.execute(
                    "update app.ask_messages set answer_enc=$2,answer_key_version=$3,"
                    "updated_at=now() where id=$1",
                    mid,
                    sealed,
                    version,
                )

        model_task = asyncio.create_task(
            answer(svc, original["question"], history, sources, progress)
        )
        async with asyncio.timeout(180):
            while not model_task.done():
                done, _ = await asyncio.wait({model_task}, timeout=1)
                if done:
                    break
                async with svc.tx(account) as conn:
                    alive = await conn.fetchval(
                        "select 1 from app.ask_messages where id=$1 "
                        "and status='pending' and attempt=$2",
                        mid,
                        attempt,
                    )
                if not alive:
                    model_task.cancel()
                    return
            text = await model_task
        async with svc.tx(account) as conn:
            # Lock before encrypt/write: deletion/cancellation cannot resurrect a message.
            row = await conn.fetchrow(
                "select id from app.ask_messages where id=$1 and status='pending' "
                "and attempt=$2 for update",
                mid,
                attempt,
            )
            if row is None:
                return
            sealed, version = await svc.crypto.encrypt(
                conn, account.user_id, "ask_messages", str(mid), text
            )
            refs = [
                {k: v for k, v in s.items() if k != "content"} | {"citation": f"S{i + 1}"}
                for i, s in enumerate(sources)
            ]
            sources_enc, sources_version = await svc.crypto.encrypt(
                conn, account.user_id, "ask_messages", str(mid), json.dumps(refs)
            )
            await conn.execute(
                "update app.ask_messages set answer_enc=$2,answer_key_version=$3,"
                "sources_enc=$4,sources_key_version=$5,status='done',updated_at=now() "
                "where id=$1",
                mid,
                sealed,
                version,
                sources_enc,
                sources_version,
            )
            await conn.execute("update app.ask_conversations set updated_at=now() where id=$1", cid)
    except asyncio.CancelledError:
        if model_task:
            model_task.cancel()
    except Exception:
        if model_task:
            model_task.cancel()
        async with svc.tx(account) as conn:
            await conn.execute(
                "update app.ask_messages set status='failed',answer_enc=null,"
                "answer_key_version=null,error="
                "'Could not generate an answer. Check model configuration or retry.',"
                "updated_at=now() where id=$1 and status='pending' and attempt=$2",
                mid,
                attempt,
            )
    finally:
        if _running.get(mid) is current:
            _running.pop(mid, None)


async def calendar_sources(
    svc: Services,
    account: Principal,
    user: Principal,
    scope: ask.Scope,
) -> list[dict[str, Any]]:
    from hashlib import sha256

    from copilot_api.routes.calendar import cache_of

    result: list[dict[str, Any]] = []
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            "select * from app.calendar_connections where profile_id=$1 "
            "and token_enc is not null and status='active' order by updated_at desc limit 2",
            user.user_id,
        )
        for row in rows:
            cache = await cache_of(svc, conn, account.user_id, row)
            for event in cache.get("events", [])[:1000]:
                if event["calendar_id"] not in row["selected_calendars"]:
                    continue
                day = event["start"][:10]
                if scope.from_date and day < str(scope.from_date):
                    continue
                if scope.to_date and day > str(scope.to_date):
                    continue
                identity = sha256(
                    (event["calendar_id"] + "\0" + event["instance_id"]).encode()
                ).hexdigest()
                result.append(
                    source(
                        "calendar",
                        str(row["id"]) + ":" + identity,
                        event["title"],
                        "Calendar event: "
                        + event["title"]
                        + "\nStarts: "
                        + event["start"]
                        + "\nEnds: "
                        + event["end"]
                        + "\nLast synchronized: "
                        + str(row["last_sync_at"])
                        + "\nConnection state: "
                        + row["status"]
                        + "\nThis is a cached planned event, not proof that a meeting occurred.",
                        event["start"],
                        record_type="cached calendar event",
                        href="/meeting-preparation",
                    )
                )
    return result


@router.get("/sources/{kind}/{source_id}")
async def resolve_source(
    kind: str, source_id: str, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    """Recheck RLS and transcript storage consent every time a citation is opened."""
    if kind == "calendar":
        for event in await calendar_sources(svc, account, user, ask.Scope()):
            if event["id"] == source_id:
                return event
        raise HTTPException(404, "Calendar event removed, deselected, or unavailable")
    if kind == "practice":
        try:
            sid = UUID(source_id)
        except ValueError as exc:
            raise HTTPException(404, "practice unavailable") from exc
        async with svc.tx(account) as conn:
            row = await conn.fetchrow(
                "select * from app.practice_sessions where id=$1 and profile_id=$2",
                sid,
                user.user_id,
            )
            if row is None:
                raise HTTPException(404, "practice deleted or unavailable")
            data = json.loads(
                await svc.crypto.decrypt(
                    conn,
                    account.user_id,
                    "practice_sessions",
                    source_id,
                    row["state_enc"],
                    row["key_version"],
                )
            )
        return source(
            kind,
            source_id,
            "Practice: " + data["setup"]["topic"],
            practice_excerpt(data),
            row["updated_at"],
            href="/practice",
        )
    if kind == "private_brief":
        try:
            async with svc.tx(account) as conn:
                brief = await personal_ai.read_version(
                    conn, svc.crypto, account.user_id, user.user_id, UUID(source_id)
                )
            return source(
                kind,
                source_id,
                brief["title"],
                brief["content"],
                brief["prepared_at"],
                href="/personal-context",
            )
        except (personal_ai.ContextError, ValueError) as exc:
            raise HTTPException(404, "private brief removed or unavailable") from exc
    async with svc.tx(user) as conn:
        try:
            if kind == "transcript":
                sid, channel, segment = source_id.split(":")
                row = await conn.fetchrow(
                    "select t.*,s.title,s.started_at from app.transcript_segments t "
                    "join app.sessions s "
                    "on s.id=t.session_id where t.session_id=$1 and t.channel=$2 and t.seg_id=$3 "
                    "and s.store_transcript and s.ended_at is not null "
                    "and (s.private_context_account_id is null or s.private_context_account_id=$4)",
                    UUID(sid),
                    channel,
                    int(segment),
                    account.user_id,
                )
                if row:
                    text = await svc.crypto.decrypt(
                        conn,
                        user.user_id,
                        "transcript_segments",
                        source_id,
                        row["text_enc"],
                        row["key_version"],
                    )
                    return source(
                        kind,
                        source_id,
                        row["title"] or "Saved call",
                        text,
                        row["started_at"],
                        sid,
                        start_ms=row["start_ms"],
                        href=f"/history/{sid}?at={row['start_ms']}",
                    )
            else:
                tables = {
                    "fact": "memory_nodes",
                    "application": "applications",
                    "document": "knowledge_chunks",
                    "work_log": "work_logs",
                    "summary": "session_outputs",
                }
                if kind not in tables:
                    raise HTTPException(404, "source unavailable")
                row = await conn.fetchrow(
                    f"select * from app.{tables[kind]} where id=$1", UUID(source_id)
                )
                if row and kind == "summary":
                    permitted = await conn.fetchval(
                        "select 1 from app.sessions where id=$1 and "
                        "(private_context_account_id is null or private_context_account_id=$2)",
                        row["session_id"],
                        account.user_id,
                    )
                    if not permitted:
                        raise HTTPException(404, "source unavailable in this account")
                if row:
                    if kind == "fact":
                        if row["status"] != "active":
                            raise HTTPException(404, "Fact removed or superseded")
                        return source(
                            kind,
                            source_id,
                            row["title"],
                            row["summary"] + "\n" + json.dumps(obj(row["body"])),
                            row["updated_at"],
                            href="/memory",
                        )
                    if kind == "application":
                        return source(
                            kind,
                            source_id,
                            row["company"] + " — " + row["role"],
                            row["jd_text"],
                            row["updated_at"],
                            href=f"/applications/{source_id}",
                        )
                    column = {
                        "document": "body_enc",
                        "work_log": "text_enc",
                        "summary": "content_enc",
                    }[kind]
                    text = await svc.crypto.decrypt(
                        conn, user.user_id, tables[kind], source_id, row[column], row["key_version"]
                    )
                    if kind == "document":
                        data = json.loads(text)
                        return source(
                            kind,
                            source_id,
                            data["doc"],
                            data["text"],
                            row["created_at"],
                            row["document_id"],
                            href=f"/work/{row['employer_id']}",
                        )
                    if kind == "work_log":
                        return source(
                            kind,
                            source_id,
                            "Work log " + str(row["day"]),
                            text,
                            row["day"],
                            href=f"/work/{row['employer_id']}",
                        )
                    return source(
                        kind,
                        source_id,
                        "Saved call summary",
                        text,
                        row["created_at"],
                        row["session_id"],
                        href=f"/history/{row['session_id']}",
                    )
        except (ValueError, TypeError) as exc:
            raise HTTPException(404, "source unavailable") from exc
    raise HTTPException(404, "source deleted or unavailable in this profile")


@router.get("/context-options")
async def context_options(
    kind: str, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        if kind == "company":
            rows = await conn.fetch(
                "select id,name as label from app.employers order by name limit 200"
            )
        elif kind == "application":
            rows = await conn.fetch(
                "select id,company || ' — ' || role as label from app.applications "
                "order by updated_at desc limit 200"
            )
        elif kind == "call":
            rows = await conn.fetch(
                "select id,coalesce(title,'Saved call') || ' · ' || started_at::date as label "
                "from app.sessions where ended_at is not null "
                "and (private_context_account_id is null or private_context_account_id=$1) "
                "order by started_at desc limit 200",
                account.user_id,
            )
        elif kind == "document":
            docs = await conn.fetch(
                "select id,name_enc,key_version from app.company_documents "
                "where status='ingested' order by created_at desc limit 200"
            )
            return {
                "options": [
                    {
                        "id": str(row["id"]),
                        "label": await svc.crypto.decrypt(
                            conn,
                            user.user_id,
                            "company_documents",
                            str(row["id"]),
                            row["name_enc"],
                            row["key_version"],
                        ),
                    }
                    for row in docs
                ]
            }
        else:
            raise HTTPException(422, "choose company, application, document, or call")
    return {"options": [{"id": str(row["id"]), "label": row["label"]} for row in rows]}


def practice_excerpt(state: dict[str, Any]) -> str:
    parts: list[str] = []
    for question in state.get("questions", [])[-8:]:
        attempts = [a for a in question.get("attempts", []) if a.get("feedback")]
        if attempts:
            attempt = attempts[-1]
            parts.append(
                "Question: "
                + question["text"]
                + "\nTentative answer: "
                + attempt["answer"][:2000]
                + "\nPractice feedback: "
                + json.dumps(attempt["feedback"], ensure_ascii=False)[:1200]
            )
    return "\n\n".join(parts)[:8000]
