"""Private practice sessions. They never start, capture, or mutate a real call."""

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import AccountDep, Services, SvcDep, UserDep, require_link
from copilot_api.routes import ask
from copilot_core import practice
from copilot_core.ask import Scope, semantic_window
from copilot_core.auth import Principal
from copilot_core.db import Conn
from copilot_core.languages import (
    practice_voice_supported,
    recognition_supported,
    validate_language,
)
from copilot_core.llm import LLMRequest, ModelRouter, Route
from copilot_core.providers import make_providers, openai_params

router = APIRouter(prefix="/practice", tags=["practice"], dependencies=[Depends(require_link)])
_running: dict[UUID, asyncio.Task[Any]] = {}


async def load(
    conn: Conn, svc: Services, account: Principal, profile: Principal, sid: UUID, lock: bool = False
) -> tuple[Any, dict[str, Any]]:
    row = await conn.fetchrow(
        "select * from app.practice_sessions where id=$1 and profile_id=$2"
        + (" for update" if lock else ""),
        sid,
        profile.user_id,
    )
    if row is None:
        raise HTTPException(404, "Practice unavailable in this account/profile")
    state = json.loads(
        await svc.crypto.decrypt(
            conn,
            account.user_id,
            "practice_sessions",
            str(sid),
            row["state_enc"],
            row["key_version"],
        )
    )
    return row, state


async def save(
    conn: Conn, svc: Services, account: Principal, sid: UUID, state: dict[str, Any]
) -> None:
    sealed, version = await svc.crypto.encrypt(
        conn, account.user_id, "practice_sessions", str(sid), json.dumps(state, ensure_ascii=False)
    )
    await conn.execute(
        "update app.practice_sessions set state_enc=$2,key_version=$3,status=$4,"
        "revision=revision+1,updated_at=now() where id=$1",
        sid,
        sealed,
        version,
        state["status"],
    )


def public(sid: UUID, state: dict[str, Any], svc: Services) -> dict[str, Any]:
    result = {k: v for k, v in state.items() if k not in ("pending", "active_operation")}
    result["sources"] = [
        {k: v for k, v in source.items() if k != "content"} for source in state["sources"]
    ]
    result["voice_available"] = bool(
        svc.settings.deepgram_api_key
        and state["spoken_language"] in svc.settings.verified_call_languages
        and recognition_supported(
            state["spoken_language"], svc.settings.deepgram_model, ("deepgram",)
        )
    )
    result["speech_available"] = bool(
        svc.settings.deepgram_api_key
        and state["spoken_language"] in svc.settings.verified_call_languages
        and practice_voice_supported(
            state["spoken_language"], svc.settings.practice_tts_models.get(state["spoken_language"])
        )
    )
    return {
        "id": str(sid),
        **result,
        "elapsed_seconds": practice.elapsed(state),
        "retention_notice": "Answers, feedback and generated questions remain until you delete "
        "practice. Raw microphone audio is never retained; transcription text is saved.",
    }


async def practice_sources(
    svc: Services,
    user: Principal,
    account: Principal,
    scope: Scope,
    query: str,
    *,
    preview: bool = False,
) -> list[dict[str, Any]]:
    async with svc.tx(user) as conn:
        sources = await ask.retrieve(
            conn, svc, user, scope, query, account.user_id, rank_results=not preview
        )
    if preview:
        sources = semantic_window(
            sources, 9
        )  # A source preview does not make embedding/model calls.
    # Explicit private context is already scoped to this account/profile and selected
    # application. Keep its provenance and re-read current versions before each turn.
    briefs = await ask.private_brief_sources(svc, user, account, scope)
    return [*briefs, *sources][:12]


@router.get("/context")
async def preview_context(
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
    application_id: UUID | None = None,
) -> dict[str, Any]:
    """Preview authorized source records without starting a practice or provider call."""
    scope = Scope(kind="application", id=application_id) if application_id else Scope()
    sources = await practice_sources(
        svc, user, account, scope, "projects experience skills", preview=True
    )
    return {
        "sources": [
            {k: v for k, v in source.items() if k != "content"}
            | {"excerpt": source["content"][:1200]}
            for source in sources
        ],
        "notice": "Review the selected profile, resume and experience before starting. "
        "Correct original records in Interview prep or Work; practice does not modify them.",
    }


@router.get("/sessions")
async def list_sessions(svc: SvcDep, user: UserDep, account: AccountDep) -> dict[str, Any]:
    sessions: list[dict[str, Any]] = []
    async with svc.tx(account) as conn:
        rows = await conn.fetch(
            "select * from app.practice_sessions where profile_id=$1 "
            "order by updated_at desc limit 100",
            user.user_id,
        )
        for row in rows:
            state = json.loads(
                await svc.crypto.decrypt(
                    conn,
                    account.user_id,
                    "practice_sessions",
                    str(row["id"]),
                    row["state_enc"],
                    row["key_version"],
                )
            )
            sessions.append(
                {
                    "id": str(row["id"]),
                    "status": state["status"],
                    "setup": state["setup"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "questions": len(state["questions"]),
                    "saved_questions": sum(q["saved_for_later"] for q in state["questions"]),
                }
            )
    return {"sessions": sessions}


@router.post("/sessions", status_code=202)
async def create_session(
    body: practice.Setup,
    background: BackgroundTasks,
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
) -> dict[str, Any]:
    hashed = practice.digest(body.model_dump(mode="json"))
    async with svc.tx(account) as conn:
        existing = await conn.fetchrow(
            "select id,profile_id,request_hash from app.practice_sessions where request_id=$1",
            body.request_id,
        )
        if existing:
            if existing["request_hash"] != hashed or existing["profile_id"] != user.user_id:
                raise HTTPException(409, "request_id already used with another setup")
            _, state = await load(conn, svc, account, user, existing["id"])
            return public(existing["id"], state, svc)
    async with svc.tx(user) as conn:
        language = (
            body.spoken_language
            or await conn.fetchval("select call_language from app.users where id=$1", user.user_id)
            or "en"
        )
        try:
            validate_language(language, svc.settings.verified_call_languages)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        scope = (
            Scope(kind="application", id=body.application_id) if body.application_id else Scope()
        )
    sources = await practice_sources(
        svc, user, account, scope, body.topic + " projects experience skills"
    )
    imported = None
    if body.source_question:
        selected = body.source_question
        async with svc.tx(user) as conn:
            if not await conn.fetchval(
                "select 1 from app.sessions where id=$1 and mode='interview' "
                "and store_transcript and ended_at is not null "
                "and (private_context_account_id is null or private_context_account_id=$2)",
                selected.session_id,
                account.user_id,
            ):
                raise HTTPException(404, "Choose an authorized saved interview transcript")
        imported = await ask.resolve_source(
            "transcript",
            f"{selected.session_id}:{selected.channel}:{selected.seg_id}",
            svc,
            user,
            account,
        )
        sources = [imported, *sources[:11]]
    state = practice.new_state(body, sources, language, imported)
    sid, operation = uuid4(), uuid4()
    state["active_operation"] = str(operation)
    async with svc.tx(account) as conn:
        sealed, version = await svc.crypto.encrypt(
            conn,
            account.user_id,
            "practice_sessions",
            str(sid),
            json.dumps(state, ensure_ascii=False),
        )
        inserted = await conn.fetchval(
            "insert into app.practice_sessions(id,user_id,profile_id,request_id,"
            "request_hash,status,state_enc,key_version) values($1,$2,$3,$4,$5,$6,$7,$8) "
            "on conflict(user_id,request_id) do nothing returning id",
            sid,
            account.user_id,
            user.user_id,
            body.request_id,
            hashed,
            state["status"],
            sealed,
            version,
        )
        if inserted is None:
            raise HTTPException(409, "Setup is already being created; retry the same request id")
        await conn.execute(
            "insert into app.practice_operations(id,user_id,session_id,kind,request_hash,status) "
            "values($1,$2,$3,'plan',$4,'pending')",
            operation,
            account.user_id,
            sid,
            hashed,
        )
    background.add_task(generate, svc, user, account, sid, operation)
    return public(sid, state, svc)


@router.get("/sessions/{sid}")
async def get_session(sid: UUID, svc: SvcDep, user: UserDep, account: AccountDep) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        row, state = await load(conn, svc, account, user, sid, True)
        if state["status"] in ("preparing", "processing"):
            stale = await conn.fetchval(
                "select 1 from app.practice_sessions where id=$1 "
                "and updated_at<now()-interval '5 minutes'",
                sid,
            )
            if stale:
                practice.stop_clock(state)
                state["status"] = "failed"
                state["error"] = "Generation interrupted. Retry generation to continue."
                await conn.execute(
                    "update app.practice_operations set status='failed' "
                    "where session_id=$1 and status='pending'",
                    sid,
                )
                await save(conn, svc, account, sid, state)
    result = public(sid, state, svc)
    result["created_at"] = row["created_at"]
    result["updated_at"] = row["updated_at"]
    return result


@router.post("/sessions/{sid}/actions", status_code=202)
async def action(
    sid: UUID,
    body: practice.Action,
    background: BackgroundTasks,
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
) -> dict[str, Any]:
    hashed = practice.digest(body.model_dump(mode="json"))
    cancel = False
    async with svc.tx(account) as conn:
        _, state = await load(conn, svc, account, user, sid, True)
        previous = await conn.fetchrow(
            "select request_hash from app.practice_operations where session_id=$1 and id=$2",
            sid,
            body.request_id,
        )
        if previous:
            if previous["request_hash"] != hashed:
                raise HTTPException(409, "request_id already used with another action")
            return public(sid, state, svc)
        try:
            needs_model = practice.reduce(state, body)
        except practice.PracticeError as exc:
            raise HTTPException(409, str(exc)) from exc
        if body.action in ("pause", "finish", "retry"):
            cancel = True
            await conn.execute(
                "update app.practice_operations set status='cancelled',updated_at=now() "
                "where session_id=$1 and status='pending'",
                sid,
            )
        if needs_model:
            state["active_operation"] = str(body.request_id)
        await conn.execute(
            "insert into app.practice_operations(id,user_id,session_id,kind,request_hash,status) "
            "values($1,$2,$3,$4,$5,$6)",
            body.request_id,
            account.user_id,
            sid,
            body.action,
            hashed,
            "pending" if needs_model else "done",
        )
        await save(conn, svc, account, sid, state)
    if cancel and (task := _running.get(sid)):
        task.cancel()
    if needs_model:
        background.add_task(generate, svc, user, account, sid, body.request_id)
    return public(sid, state, svc)


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(sid: UUID, svc: SvcDep, user: UserDep, account: AccountDep) -> None:
    async with svc.tx(account) as conn:
        await load(conn, svc, account, user, sid, True)
        await conn.execute("delete from app.practice_sessions where id=$1", sid)
    if task := _running.get(sid):
        task.cancel()


async def complete(svc: Services, request: LLMRequest) -> dict[str, Any]:
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
        model_router = ModelRouter({"practice": routes}, first_token_timeout_s=60)
        text, done = await model_router.complete("practice", request)
        if done.stop_reason == "max_tokens":
            raise practice.PracticeError("Provider response exceeded its limit")
        data = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
        if not isinstance(data, dict):
            raise practice.PracticeError("Provider response was not an object")
        return cast(dict[str, Any], data)
    finally:
        for provider in providers:
            client = getattr(provider, "client", None)
            if client is not None:
                await client.close()


async def generate(
    svc: Services, user: Principal, account: Principal, sid: UUID, operation: UUID
) -> None:
    task = asyncio.current_task()
    if task:
        _running[sid] = task
    model_task: asyncio.Task[dict[str, Any]] | None = None
    try:
        async with svc.tx(account) as conn:
            _, state = await load(conn, svc, account, user, sid)
            if state.get("active_operation") != str(operation) or state["status"] not in (
                "preparing",
                "processing",
            ):
                return
        # Resolve again on every turn so deleted material is never reintroduced as evidence.
        authorized: list[dict[str, Any]] = []
        setup = state["setup"]
        application_id = setup.get("application_id")
        scope = Scope(kind="application", id=UUID(application_id)) if application_id else Scope()
        current_briefs = await ask.private_brief_sources(svc, user, account, scope)
        # Old brief versions can remain readable for old citations; only currently
        # active versions may become evidence for a new generation.
        state["sources"] = [s for s in state["sources"] if s["kind"] != "private_brief"]
        state["sources"] = [*current_briefs, *state["sources"]][:12]
        for source in state["sources"]:
            try:
                resolved = await ask.resolve_source(
                    source["kind"], source["id"], svc, user, account
                )
                authorized.append({**source, "content": resolved["content"]})
            except HTTPException:
                continue
        state["sources"] = authorized
        model_task = asyncio.create_task(complete(svc, practice.request(state)))
        async with asyncio.timeout(180):
            while not model_task.done():
                done, _ = await asyncio.wait({model_task}, timeout=1)
                if done:
                    break
                async with svc.tx(account) as conn:
                    alive = await conn.fetchval(
                        "select 1 from app.practice_operations where session_id=$1 "
                        "and id=$2 and status='pending'",
                        sid,
                        operation,
                    )
                if not alive:
                    model_task.cancel()
                    return
            result = await model_task
        async with svc.tx(account) as conn:
            _, fresh = await load(conn, svc, account, user, sid, True)
            if fresh.get("active_operation") != str(operation) or fresh["status"] not in (
                "preparing",
                "processing",
            ):
                return
            fresh["sources"] = authorized
            practice.apply_result(fresh, result)
            await save(conn, svc, account, sid, fresh)
            await conn.execute(
                "update app.practice_operations set status='done',updated_at=now() "
                "where session_id=$1 and id=$2 and status='pending'",
                sid,
                operation,
            )
    except asyncio.CancelledError:
        if model_task:
            model_task.cancel()
    except Exception:
        if model_task:
            model_task.cancel()
        async with svc.tx(account) as conn:
            row = await conn.fetchrow(
                "select id from app.practice_sessions where id=$1 and profile_id=$2",
                sid,
                user.user_id,
            )
            if row:
                _, fresh = await load(conn, svc, account, user, sid, True)
                if fresh.get("active_operation") == str(operation) and fresh["status"] in (
                    "preparing",
                    "processing",
                ):
                    practice.stop_clock(fresh)
                    fresh["status"] = "failed"
                    fresh["error"] = (
                        "Provider unavailable or returned unusable content. Your "
                        "completed answers are saved; retry generation."
                    )
                    await save(conn, svc, account, sid, fresh)
                    await conn.execute(
                        "update app.practice_operations set status='failed',updated_at=now() "
                        "where session_id=$1 and id=$2 and status='pending'",
                        sid,
                        operation,
                    )
    finally:
        if _running.get(sid) is task:
            _running.pop(sid, None)


def practice_keyterms(question: str, sources: list[dict[str, Any]]) -> list[str]:
    """Bounded proper-name/product hints; audio alone remains the transcript source."""
    text = question[:2000] + " " + " ".join(str(s.get("label", ""))[:150] for s in sources[:12])
    common = {
        "Describe",
        "Explain",
        "Tell",
        "What",
        "How",
        "When",
        "Which",
        "Why",
        "Could",
        "Would",
        "Can",
        "Resume",
        "Original",
        "Kind",
        "Private",
        "Interview",
    }
    return list(
        dict.fromkeys(
            word for word in re.findall(r"[A-Z][A-Za-z0-9+#.-]{1,39}", text) if word not in common
        )
    )[:20]


# Voice is microphone-only at the client. Only these explicit uploads reach recognition;
# generated interviewer audio is never written as a candidate transcript.
async def voice_request(
    svc: Services,
    kind: str,
    payload: bytes,
    content_type: str,
    language: str,
    text: str = "",
    keyterms: Sequence[str] = (),
) -> bytes:
    import httpx2

    from copilot_core.languages import CALL_LANGUAGES

    key = svc.settings.deepgram_api_key
    if key is None:
        raise HTTPException(503, "Voice is not configured. Continue in text mode.")
    try:
        validate_language(language, svc.settings.verified_call_languages)
    except ValueError as exc:
        raise HTTPException(
            503, "Voice is unavailable for this session language; use text."
        ) from exc
    if kind != "speech" and not recognition_supported(
        language, svc.settings.deepgram_model, ("deepgram",)
    ):
        raise HTTPException(
            503, "Microphone transcription is unavailable in this language; use text."
        )
    headers = {"Authorization": "Token " + key.get_secret_value()}
    async with httpx2.AsyncClient(timeout=90) as client:
        if kind == "speech":
            model = getattr(svc.settings, "practice_tts_models", {}).get(language)
            if not practice_voice_supported(language, model):
                raise HTTPException(
                    503, "Speech playback is unavailable in this language; use text."
                )
            response = await client.post(
                "https://api.deepgram.com/v1/speak",
                params={"model": model, "encoding": "mp3", "mip_opt_out": "true"},
                headers=headers,
                json={"text": text},
            )
            response.raise_for_status()
            return response.content
        params: list[tuple[str, str]] = [
            ("model", svc.settings.deepgram_model),
            ("smart_format", "true"),
            ("language", CALL_LANGUAGES[language].deepgram_code),
            ("mip_opt_out", "true"),
            ("redact_usage", "true"),
        ]
        if svc.settings.deepgram_model in {"nova-3", "nova-3-general"}:
            params.extend(
                ("keyterm", term[:40]) for term in dict.fromkeys(keyterms) if term.strip()
            )
            params = params[:25]  # At most 20 short hints; below the provider 500-token limit.
        response = await client.post(
            "https://api.deepgram.com/v1/listen",
            params=tuple(params),
            headers={**headers, "Content-Type": content_type},
            content=payload,
        )
        response.raise_for_status()
        data = response.json()
        transcript: Any = data["results"]["channels"][0]["alternatives"][0]["transcript"]
        if not isinstance(transcript, str):
            raise ValueError("Invalid transcription response")
        return transcript[:16000].encode()


async def media(
    sid: UUID,
    operation: UUID,
    kind: str,
    svc: Services,
    user: Principal,
    account: Principal,
    payload: bytes = b"",
    content_type: str = "",
    qid: UUID | None = None,
) -> bytes:
    import hashlib

    row_id = f"{sid}:{operation}"
    async with svc.tx(account) as conn:
        _, state = await load(conn, svc, account, user, sid, True)
        if state["status"] != "answering":
            raise HTTPException(409, "Resume an active question before using voice")
        question = practice.current(state)
        if qid and str(qid) != question["id"]:
            raise HTTPException(409, "Only the current question can be spoken")
        hashed = practice.digest(
            {
                "kind": kind,
                "body": hashlib.sha256(payload).hexdigest(),
                "type": content_type,
                "question": question["id"],
                "language": state["spoken_language"],
            }
        )
        old = await conn.fetchrow(
            "select * from app.practice_operations where session_id=$1 and id=$2 for update",
            sid,
            operation,
        )
        if old and old["request_hash"] != hashed:
            raise HTTPException(409, "request_id already used for different audio")
        if old and old["status"] == "done":
            return await svc.crypto.decrypt_bytes(
                conn,
                account.user_id,
                "practice_operations",
                row_id,
                old["result_enc"],
                old["key_version"],
            )
        await conn.execute(
            "update app.practice_operations set status='failed' where session_id=$1 "
            "and kind in ('speech','transcription') and status='pending' "
            "and updated_at<now()-interval '2 minutes'",
            sid,
        )
        if await conn.fetchval(
            "select 1 from app.practice_operations where session_id=$1 "
            "and kind in ('speech','transcription') and status='pending'",
            sid,
        ):
            raise HTTPException(409, "Voice processing is already in progress")
        await conn.execute(
            "insert into app.practice_operations(id,user_id,session_id,kind,request_hash,status) "
            "values($1,$2,$3,$4,$5,'pending') on conflict(session_id,id) "
            "do update set status='pending',updated_at=now()",
            operation,
            account.user_id,
            sid,
            kind,
            hashed,
        )
    pending = asyncio.create_task(
        voice_request(
            svc,
            kind,
            payload,
            content_type,
            state["spoken_language"],
            question["text"],
            keyterms=practice_keyterms(question["text"], state["sources"]),
        )
    )
    try:
        async with asyncio.timeout(100):
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=1)
                if done:
                    break
                async with svc.tx(account) as conn:
                    alive = await conn.fetchval(
                        "select 1 from app.practice_operations where session_id=$1 "
                        "and id=$2 and status='pending'",
                        sid,
                        operation,
                    )
                if not alive:
                    raise HTTPException(409, "Voice request cancelled")
            result = await pending
        if len(result) > 8 * 1024 * 1024:
            raise HTTPException(502, "Voice provider response exceeds the limit")
        async with svc.tx(account) as conn:
            _, fresh = await load(conn, svc, account, user, sid, True)
            alive = await conn.fetchval(
                "select 1 from app.practice_operations where session_id=$1 "
                "and id=$2 and status='pending'",
                sid,
                operation,
            )
            if (
                not alive
                or fresh["status"] != "answering"
                or fresh["current_question_id"] != question["id"]
            ):
                raise HTTPException(409, "Voice request cancelled or question changed")
            sealed, version = await svc.crypto.encrypt_bytes(
                conn, account.user_id, "practice_operations", row_id, result
            )
            await conn.execute(
                "update app.practice_operations set status='done',result_enc=$3,key_version=$4,"
                "updated_at=now() where session_id=$1 and id=$2",
                sid,
                operation,
                sealed,
                version,
            )
        return result
    except BaseException as exc:
        pending.cancel()
        async with svc.tx(account) as conn:
            await conn.execute(
                "update app.practice_operations set status='failed',updated_at=now() "
                "where session_id=$1 and id=$2 and status='pending'",
                sid,
                operation,
            )
        if isinstance(exc, (HTTPException, asyncio.CancelledError)):
            raise
        raise HTTPException(
            503, "Voice provider unavailable. Retry explicitly or continue with text."
        ) from exc


MAX_AUDIO = 8 * 1024 * 1024
AUDIO_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/mpeg",
    "audio/mp3",
}


def audio_type(value: str) -> str:
    mime = value.split(";", 1)[0].strip().lower()
    if mime not in AUDIO_TYPES:
        raise HTTPException(415, "Use WebM, Ogg, WAV, MP4 or MP3 microphone audio")
    return mime


class AudioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    content_type: str = Field(max_length=100)
    audio_base64: str = Field(min_length=4, max_length=((MAX_AUDIO + 2) // 3) * 4)


@router.post("/sessions/{sid}/transcribe-json")
async def transcribe_json(
    sid: UUID, body: AudioIn, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    import base64
    import binascii

    mime = audio_type(body.content_type)
    try:
        raw = base64.b64decode(body.audio_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Invalid microphone audio encoding") from exc
    if not raw or len(raw) > MAX_AUDIO:
        raise HTTPException(413, "Microphone recording must be between 1 byte and 8 MiB")
    result = await media(sid, body.request_id, "transcription", svc, user, account, raw, mime)
    return {"text": result.decode(), "silence": not bool(result.strip()), "audio_retained": False}


@router.post("/sessions/{sid}/transcribe")
async def transcribe(
    sid: UUID, request_id: UUID, request: Request, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    mime = audio_type(request.headers.get("content-type", ""))
    audio = bytearray()
    async for chunk in request.stream():
        audio.extend(chunk)
        if len(audio) > MAX_AUDIO:
            raise HTTPException(413, "Microphone recording exceeds 8 MiB")
    if not audio:
        raise HTTPException(422, "No microphone audio received")
    result = await media(sid, request_id, "transcription", svc, user, account, bytes(audio), mime)
    return {"text": result.decode(), "silence": not bool(result.strip()), "audio_retained": False}


async def spoken(sid: UUID, qid: UUID, svc: Services, user: Principal, account: Principal) -> bytes:
    from uuid import NAMESPACE_URL, uuid5

    operation = uuid5(NAMESPACE_URL, f"practice:{sid}:question:{qid}:speech")
    return await media(sid, operation, "speech", svc, user, account, qid=qid)


@router.get("/sessions/{sid}/questions/{qid}/speech")
async def speech(sid: UUID, qid: UUID, svc: SvcDep, user: UserDep, account: AccountDep) -> Response:
    return Response(
        await spoken(sid, qid, svc, user, account),
        media_type="audio/mpeg",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/sessions/{sid}/questions/{qid}/speech-json")
async def speech_json(
    sid: UUID, qid: UUID, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, str]:
    import base64

    data = await spoken(sid, qid, svc, user, account)
    return {"content_type": "audio/mpeg", "audio_base64": base64.b64encode(data).decode()}
