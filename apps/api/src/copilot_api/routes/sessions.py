"""Sessions: list, detail with post-call outputs, transcript, delete, and the user's audio.

`GET /sessions/{id}` reports `status`: `live` while the call runs, `processing` after it ends
until the post-call outputs are written, then `ready` with the outputs. The desktop app polls
it after a call. Text is decrypted in memory only for the response.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from copilot_api.deps import Services, SvcDep, UserDep
from copilot_core import keys
from copilot_core.auth import Principal
from copilot_core.crypto import segment_row_id
from copilot_core.storage import me_audio_key
from copilot_core.valkey_client import text

router = APIRouter()

SESSION_COLUMNS = (
    "id, mode, title, platform, job_id, interview_id, store_transcript, started_at, ended_at,"
    " billed_seconds, me_audio_status, outputs_ready_at"
)


def status_of(row: Any) -> str:
    if row["ended_at"] is None:
        return "live"
    return "ready" if row["outputs_ready_at"] is not None else "processing"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _session(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "mode": row["mode"],
        "title": row["title"],
        "platform": row["platform"],
        "job_id": str(row["job_id"]) if row["job_id"] else None,
        "interview_id": str(row["interview_id"]) if row["interview_id"] else None,
        "store_transcript": row["store_transcript"],
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "billed_seconds": row["billed_seconds"],
        "me_audio_status": row["me_audio_status"],
        "status": status_of(row),
    }


@router.get("/sessions")
async def list_sessions(
    svc: SvcDep, user: UserDep, limit: int = 20, before: datetime | None = None
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    async with svc.tx(user) as conn:
        rows = await conn.fetch(
            f"select {SESSION_COLUMNS} from app.sessions where user_id = $1"
            " and ($2::timestamptz is null or started_at < $2)"
            " order by started_at desc limit $3",
            user.user_id,
            before,
            limit,
        )
    items = [_session(r) for r in rows]
    return {
        "sessions": items,
        "next_before": items[-1]["started_at"] if len(items) == limit else None,
    }


async def _live_owner(svc: Services, user: Principal, sid: UUID) -> dict[str, str] | None:
    """The gateway's state for a session that has no row yet (before its first flush)."""
    raw = await svc.valkey.hgetall(keys.sess_state(sid))
    state = {text(k): text(v) for k, v in raw.items()}
    return state if state.get("user_id") == str(user.user_id) else None


@router.get("/sessions/{session_id}")
async def get_session(session_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            f"select {SESSION_COLUMNS} from app.sessions where id = $1", session_id
        )
        if row is None:
            state = await _live_owner(svc, user, session_id)
            if state is None:
                raise HTTPException(404, "no such session")
            return {
                "id": str(session_id),
                "mode": state.get("mode"),
                "status": "live",
                "started_at": state.get("started_at"),
                "outputs": {},
                "questions": 0,
            }
        outputs: dict[str, str] = {}
        for o in await conn.fetch(
            "select id, kind, content_enc, key_version from app.session_outputs"
            " where session_id = $1 order by kind",
            session_id,
        ):
            outputs[o["kind"]] = await svc.crypto.decrypt(
                conn, user.user_id, "session_outputs", str(o["id"]), o["content_enc"],
                o["key_version"],
            )  # fmt: skip
        questions = await conn.fetchval(
            "select count(*) from app.questions where session_id = $1 and speculative_kept",
            session_id,
        )
    return {**_session(row), "outputs": outputs, "questions": questions}


@router.get("/sessions/{session_id}/transcript")
async def get_transcript(session_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select store_transcript from app.sessions where id = $1", session_id
        )
        if row is None:
            raise HTTPException(404, "no such session")
        segments: list[dict[str, Any]] = []
        for s in await conn.fetch(
            "select channel, seg_id, start_ms, end_ms, text_enc, key_version"
            " from app.transcript_segments where session_id = $1"
            " order by start_ms, channel, seg_id",
            session_id,
        ):
            row_id = segment_row_id(session_id, s["channel"], s["seg_id"])
            segment_text = await svc.crypto.decrypt(
                conn, user.user_id, "transcript_segments", row_id, s["text_enc"], s["key_version"]
            )
            segments.append({"channel": s["channel"], "seg_id": s["seg_id"],
                             "start_ms": s["start_ms"], "end_ms": s["end_ms"],
                             "text": segment_text})  # fmt: skip
    return {"session_id": str(session_id), "stored": row["store_transcript"], "segments": segments}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: UUID, svc: SvcDep, user: UserDep) -> Response:
    """The session and its whole log; facts already approved into memory stay."""
    state = await _live_owner(svc, user, session_id)
    if state is not None and state.get("status") != "ended":
        raise HTTPException(409, "the session is still live; end it first")
    async with svc.tx(user) as conn:
        deleted = await conn.fetchval(
            "delete from app.sessions where id = $1 returning id", session_id
        )
    if deleted is None and state is None:
        raise HTTPException(404, "no such session")
    await svc.store.delete(me_audio_key(user.user_id, session_id))
    await svc.valkey.delete(
        keys.sess_state(session_id), keys.sess_events(session_id), keys.sess_log(session_id)
    )
    return Response(status_code=204)


class MeAudioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: Literal["audio/wav", "audio/ogg", "audio/webm", "audio/mpeg"] = "audio/wav"


async def _me_audio_session(svc: Services, user: Principal, sid: UUID) -> dict[str, Any]:
    """The session's consent and audio status, from its row or, before the row exists, from
    the gateway's state (the call may end before its first flush)."""
    async with svc.tx(user) as conn:
        row = await conn.fetchrow(
            "select store_transcript, me_audio_status from app.sessions where id = $1", sid
        )
    if row is not None:
        return dict(row)
    state = await _live_owner(svc, user, sid)
    if state is None:
        raise HTTPException(404, "no such session")
    return {"store_transcript": state.get("store_transcript") == "1", "me_audio_status": "pending"}


@router.post("/sessions/{session_id}/me-audio", status_code=201)
async def me_audio_upload(
    session_id: UUID, body: MeAudioIn, svc: SvcDep, user: UserDep
) -> dict[str, Any]:
    """A presigned upload for the user's own audio after the call."""
    session = await _me_audio_session(svc, user, session_id)
    if not session["store_transcript"]:
        raise HTTPException(409, "no transcript consent for this session; delete the recording")
    if session["me_audio_status"] != "pending":
        raise HTTPException(409, f"the audio is already {session['me_audio_status']}")
    upload = await svc.store.presign_put(
        me_audio_key(user.user_id, session_id), body.content_type, svc.settings.upload_url_ttl_s
    )
    return {"session_id": str(session_id), "upload": upload}


@router.post("/sessions/{session_id}/me-audio/complete", status_code=202)
async def me_audio_complete(session_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    """The client finished the upload: transcribe it now instead of after the 10-minute wait."""
    session = await _me_audio_session(svc, user, session_id)
    key = me_audio_key(user.user_id, session_id)
    head = await svc.store.head(key)
    if head is None:
        raise HTTPException(409, "no audio was uploaded for this session")
    if session["me_audio_status"] != "pending" or not session["store_transcript"]:
        await svc.store.delete(key)  # too late, or not wanted: never keep it
        raise HTTPException(409, f"the audio is already {session['me_audio_status']}")
    if int(head.get("ContentLength", 0)) > svc.settings.max_me_audio_bytes:
        await svc.store.delete(key)
        raise HTTPException(413, "the recording is too large")
    async with svc.tx(user) as conn:
        await conn.execute(
            "update app.sessions set me_audio_status = 'uploaded'"
            " where id = $1 and me_audio_status = 'pending'",
            session_id,
        )
    await svc.enqueue(
        "me_audio.transcribe", f"me_audio.transcribe:{session_id}", user.user_id,
        {"session_id": str(session_id)},
    )  # fmt: skip
    return {"session_id": str(session_id), "me_audio_status": "uploaded"}
