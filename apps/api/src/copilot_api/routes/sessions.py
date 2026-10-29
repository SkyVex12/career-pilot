"""Sessions: list, detail with post-call outputs, transcript, replay, and delete.

The user's own words are transcribed live by the gateway (ARCHITECTURE.md, "Desktop client"),
so there is no audio upload after the call any more.

`GET /sessions/{id}/replay` is what the overlay showed during the call: both sides' lines and
each question with its talking points and detailed answer, timed from the start of the call,
so History can show the same window afterwards.

`GET /sessions/{id}` reports `status`: `live` while the call runs, `processing` after it ends
until the post-call outputs are written, then `ready` with the outputs. The desktop app polls
it after a call. Text is decrypted in memory only for the response.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response

from copilot_api.deps import Services, SvcDep, UserDep
from copilot_core import keys
from copilot_core.auth import Principal
from copilot_core.crypto import segment_row_id
from copilot_core.storage import me_audio_key
from copilot_core.valkey_client import text

router = APIRouter()

SESSION_COLUMNS = (
    "id, mode, title, platform, job_id, interview_id, store_transcript, started_at, ended_at,"
    " billed_seconds, me_audio_status, outputs_ready_at, employer_id"
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
        "employer_id": str(row["employer_id"]) if row["employer_id"] else None,
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


def _offset_ms(at: datetime | None, started_at: datetime | None) -> int:
    if at is None or started_at is None:
        return 0
    return max(0, int((at - started_at).total_seconds() * 1000))


@router.get("/sessions/{session_id}/replay")
async def get_replay(session_id: UUID, svc: SvcDep, user: UserDep) -> dict[str, Any]:
    """The call as the overlay showed it. Questions the gateway dropped (a speculative one it
    did not keep) are left out, as the overlay left them out. Without transcript consent no
    text was stored, so there is nothing but the questions' types."""
    transcript = await get_transcript(session_id, svc, user)
    async with svc.tx(user) as conn:
        started_at = await conn.fetchval(
            "select started_at from app.sessions where id = $1", session_id
        )
        questions: list[dict[str, Any]] = []
        by_id: dict[UUID, dict[str, Any]] = {}
        for q in await conn.fetch(
            "select id, text_enc, key_version, type, forced_by_hotkey, detected_at"
            " from app.questions where session_id = $1 and speculative_kept"
            " order by detected_at, id",
            session_id,
        ):
            q_text = ""
            if q["text_enc"] is not None:
                q_text = await svc.crypto.decrypt(
                    conn, user.user_id, "questions", str(q["id"]), q["text_enc"], q["key_version"]
                )
            item: dict[str, Any] = {
                "id": str(q["id"]),
                "text": q_text,
                "type": q["type"],
                "forced": q["forced_by_hotkey"],
                "offset_ms": _offset_ms(q["detected_at"], started_at),
                "feedback": None,
                "fast": None,
                "deep": None,
            }
            questions.append(item)
            by_id[q["id"]] = item
        answers = await conn.fetch(
            "select a.id, a.question_id, a.pass, a.model, a.text_enc, a.key_version,"
            " a.memory_node_ids, a.expanded, a.feedback from app.answers a"
            " join app.questions q on q.id = a.question_id where q.session_id = $1",
            session_id,
        )
        node_ids = {n for a in answers for n in a["memory_node_ids"]}
        labels: dict[UUID, str] = {}
        if node_ids:
            labels = {
                r["id"]: r["title"]
                for r in await conn.fetch(
                    "select id, title from app.memory_nodes where id = any($1::uuid[])",
                    list(node_ids),
                )
            }
        for a in answers:
            found = by_id.get(a["question_id"])
            if found is None:
                continue
            a_text = ""
            if a["text_enc"] is not None:
                a_text = await svc.crypto.decrypt(
                    conn, user.user_id, "answers", str(a["id"]), a["text_enc"], a["key_version"]
                )
            found[a["pass"]] = {
                "text": a_text,
                "source": "prepared" if a["model"] == "prepared" else "model",
                "refs": [
                    {"node_id": str(n), "label": labels[n]}
                    for n in a["memory_node_ids"]
                    if n in labels
                ],
                "expanded": a["expanded"],
            }
            found["feedback"] = found["feedback"] or a["feedback"]
    return {
        "session_id": str(session_id),
        "stored": transcript["stored"],
        "started_at": _iso(started_at),
        "segments": transcript["segments"],
        "questions": questions,
    }


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
    # A recording an older client uploaded before live transcription, if one is left.
    await svc.store.delete(me_audio_key(user.user_id, session_id))
    await svc.valkey.delete(
        keys.sess_state(session_id), keys.sess_events(session_id), keys.sess_log(session_id)
    )
    return Response(status_code=204)
