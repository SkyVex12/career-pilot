"""Session log jobs (ARCHITECTURE.md, "Session logging" and "SQS job catalog").

`session.flush` copies the gateway's `sess:{sid}:log` stream into Aurora every 30 seconds;
`session.ended` copies the rest and closes the session; `me_audio.transcribe` turns the
user's uploaded audio into "me" segments and deletes it; `session.postcall` writes the
summary and feedback.

Writes are idempotent, so any job can run twice: segments and questions keep their keys,
and an answer's id is derived from its question and pass. After a flush commits, the stream
is trimmed up to what was written. One exception: the gateway can log a fast answer before
its (still speculative) question, so an answer whose question is not in Aurora yet stays in
the stream for the next flush. At the end of the session such orphans are dropped.

Without transcript consent the session log holds no text at all: the gateway leaves text out
of the log, and the flush drops any that arrives anyway.
"""

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from copilot_core import keys
from copilot_core.crypto import segment_row_id
from copilot_core.db import Conn
from copilot_core.ids import uuid7_for
from copilot_core.storage import me_audio_key
from copilot_core.valkey_client import text
from copilot_worker import prompts
from copilot_worker.context import Ctx
from copilot_worker.llm import as_dict, as_list, complete_json
from copilot_worker.runtime import Job, RetryLater, handler

log = logging.getLogger(__name__)

ME_AUDIO_GIVE_UP_S = 3600  # a transcribe job still waiting on session.ended after this


@dataclass
class Entry:
    id: str
    data: dict[str, Any]

    @property
    def kind(self) -> str:
        return str(self.data.get("kind", ""))


@dataclass
class FlushResult:
    session_exists: bool = False
    written: dict[str, int] = field(default_factory=dict[str, int])
    deferred: int = 0
    last_at: datetime | None = None
    state: dict[str, str] = field(default_factory=dict[str, str])


def next_id(entry_id: str) -> str:
    ms, _, seq = entry_id.partition("-")
    return f"{ms}-{int(seq or 0) + 1}"


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


async def read_log(ctx: Ctx, sid: UUID, upto: str | None) -> list[Entry]:
    raw = await ctx.valkey.xrange(keys.sess_log(sid), min="-", max=upto or "+")
    entries: list[Entry] = []
    for entry_id, fields in cast(list[tuple[Any, dict[Any, Any]]], raw):
        value = fields.get(b"e", fields.get("e"))
        try:
            data = json.loads(text(value))
        except (TypeError, ValueError):
            log.warning("session %s: unreadable log entry %s", sid, text(entry_id))
            continue
        if isinstance(data, dict):
            entries.append(Entry(text(entry_id), cast(dict[str, Any], data)))
    return entries


async def load_state(ctx: Ctx, sid: UUID) -> dict[str, str]:
    raw = await ctx.valkey.hgetall(keys.sess_state(sid))
    return {text(k): text(v) for k, v in cast(dict[Any, Any], raw).items()}


async def ensure_session(
    conn: Conn, uid: UUID, sid: UUID, meta: dict[str, Any]
) -> dict[str, Any] | None:
    """The sessions row, created from the gateway's state on the first flush."""
    row = await conn.fetchrow(
        "select mode, store_transcript, started_at, ended_at, me_audio_status, job_id"
        " from app.sessions where id = $1",
        sid,
    )
    if row is not None:
        return dict(row)
    started_at = _when(meta.get("started_at"))
    if not meta or started_at is None:
        return None
    # The client names a job and an interview from its start screen; keep them only if they
    # are this user's, since the references are foreign keys. Without an interview id the
    # interview is the one whose pack was loaded.
    job_id = await conn.fetchval("select id from app.jobs where id = $1", _uuid(meta.get("job_id")))
    interview_id = await conn.fetchval(
        "select id from app.interviews where id = $1", _uuid(meta.get("interview_id"))
    ) or await conn.fetchval(
        "select id from app.interviews where pack_id = $1", _uuid(meta.get("pack_id"))
    )
    mode = (
        meta.get("mode") if meta.get("mode") in ("interview", "standup", "meeting") else "meeting"
    )
    await conn.execute(
        "insert into app.sessions (id, user_id, mode, job_id, interview_id, platform, title,"
        " store_transcript, started_at) values ($1, $2, $3, $4, $5, $6, $7, $8, $9)"
        " on conflict (id) do nothing",
        sid,
        uid,
        mode,
        job_id,
        interview_id,
        meta.get("platform") or None,
        meta.get("title") or None,
        str(meta.get("store_transcript")) in ("1", "True", "true"),
        started_at,
    )
    return await ensure_session(conn, uid, sid, {})


async def flush_log(ctx: Ctx, uid: UUID, sid: UUID, upto: str | None, final: bool) -> FlushResult:
    result = FlushResult()
    state = await load_state(ctx, sid)
    result.state = state
    if state.get("user_id") and state["user_id"] != str(uid):
        raise ValueError(f"session {sid} belongs to another user")
    entries = await read_log(ctx, sid, upto)
    meta: dict[str, Any] = dict(state)
    for e in entries:
        if e.kind == "session.started" and not meta.get("started_at"):
            meta.update(e.data)
    for e in entries:
        result.last_at = _when(e.data.get("at")) or result.last_at

    deferred: list[Entry] = []
    async with ctx.user(uid) as conn:
        session = await ensure_session(conn, uid, sid, meta)
        if session is None:
            log.info("session %s: no state and no row; nothing to flush", sid)
            return result
        result.session_exists = True
        store = bool(session["store_transcript"])
        counts = result.written
        known = {
            r["id"]
            for r in await conn.fetch("select id from app.questions where session_id = $1", sid)
        }
        # Questions first, so answers later in the same batch find theirs.
        for e in entries:
            if e.kind == "question" and (qid := _uuid(e.data.get("question_id"))):
                await write_question(ctx, conn, uid, sid, qid, e.data, store)
                known.add(qid)
                counts["question"] = counts.get("question", 0) + 1
        for e in entries:
            kind = e.kind
            if kind == "segment":
                if store and e.data.get("text"):
                    await write_segment(ctx, conn, uid, sid, e.data)
                    counts["segment"] = counts.get("segment", 0) + 1
            elif kind == "answer":
                qid = _uuid(e.data.get("question_id"))
                if qid is None:
                    continue
                if qid not in known:
                    if not final:
                        deferred.append(e)
                    continue
                await write_answer(ctx, conn, uid, qid, e.data, store)
                counts["answer"] = counts.get("answer", 0) + 1
            elif kind == "feedback":
                rating = e.data.get("rating")
                if rating in ("up", "down"):
                    await conn.execute(
                        "update app.answers set feedback = $2 where question_id = $1",
                        _uuid(e.data.get("question_id")),
                        rating,
                    )
            elif kind == "expand":
                await conn.execute(
                    "update app.answers set expanded = true"
                    " where question_id = $1 and pass = 'deep'",
                    _uuid(e.data.get("question_id")),
                )
            elif kind == "mode" and e.data.get("mode") in ("interview", "standup", "meeting"):
                await conn.execute(
                    "update app.sessions set mode = $2 where id = $1", sid, e.data["mode"]
                )
        # Timings that came after their pass was logged (the client's first word, or the
        # confirmation of a speculative question), once every answer in the batch is written.
        # A report whose answer was deferred waits with it.
        for e in entries:
            if e.kind != "latency_report" or (qid := _uuid(e.data.get("question_id"))) is None:
                continue
            report = {k: e.data[k] for k in LATE_LATENCY_FIELDS if e.data.get(k) is not None}
            updated = await conn.fetchval(
                "update app.answers set latency_ms = coalesce(latency_ms, '{}'::jsonb)"
                " || $3::jsonb where question_id = $1 and pass = $2 returning id",
                qid,
                "deep" if e.data.get("pass") == "deep" else "fast",
                report,
            )
            if updated is None and not final:
                deferred.append(e)
    result.deferred = len(deferred)
    if entries:
        trim_to = (
            min(deferred, key=lambda e: _stream_id(e.id)).id
            if deferred
            else next_id(entries[-1].id)
        )
        await ctx.valkey.xtrim(keys.sess_log(sid), minid=trim_to, approximate=False)
    if final:
        await ctx.valkey.expire(keys.sess_log(sid), ctx.settings.log_ttl_after_flush_s)
    return result


async def write_question(
    ctx: Ctx, conn: Conn, uid: UUID, sid: UUID, qid: UUID, d: dict[str, Any], store: bool
) -> None:
    text_enc, version = None, None
    if store and d.get("text"):
        text_enc, version = await ctx.crypto.encrypt(conn, uid, "questions", str(qid), d["text"])
    await conn.execute(
        "insert into app.questions (id, session_id, user_id, text_enc, key_version, type,"
        " addressed_to_user, speculative_kept, forced_by_hotkey, detected_at)"
        " values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) on conflict (id) do nothing",
        qid,
        sid,
        uid,
        text_enc,
        version,
        str(d.get("type") or "other"),
        bool(d.get("addressed_to_user", True)),
        bool(d.get("speculative_kept", True)),
        bool(d.get("forced_by_hotkey", False)),
        _when(d.get("detected_at")) or datetime.now(UTC),
    )


async def write_segment(ctx: Ctx, conn: Conn, uid: UUID, sid: UUID, d: dict[str, Any]) -> None:
    channel = "me" if d.get("channel") == "me" else "them"
    seg_id = int(d.get("seg_id", 0))
    row_id = segment_row_id(sid, channel, seg_id)
    enc, version = await ctx.crypto.encrypt(conn, uid, "transcript_segments", row_id, d["text"])
    await conn.execute(
        "insert into app.transcript_segments (session_id, channel, seg_id, user_id, start_ms,"
        " end_ms, text_enc, key_version) values ($1, $2, $3, $4, $5, $6, $7, $8)"
        " on conflict do nothing",
        sid,
        channel,
        seg_id,
        uid,
        int(d.get("start_ms", 0)),
        int(d.get("end_ms", 0)),
        enc,
        version,
    )


def _stream_id(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms), int(seq or 0)


# What a latency_report entry may add to answers.latency_ms.
LATE_LATENCY_FIELDS = ("client_first_word_ms", "client_anchor", "turn_end_ms", "final_ms")


def _cost(value: Any) -> Decimal | None:
    """The gateway's price for an answer (a float), as numeric(10,6)."""
    try:
        return None if value is None else Decimal(str(value)).quantize(Decimal("0.000001"))
    except (ArithmeticError, ValueError):
        return None


def answer_id(qid: UUID, pass_: str) -> UUID:
    return uuid7_for(qid.int >> 80, str(qid), pass_)  # the question's time, then its pass


async def write_answer(
    ctx: Ctx, conn: Conn, uid: UUID, qid: UUID, d: dict[str, Any], store: bool
) -> None:
    pass_ = "deep" if d.get("pass") == "deep" else "fast"
    aid = answer_id(qid, pass_)
    text_enc, version = None, None
    if store and d.get("text"):
        text_enc, version = await ctx.crypto.encrypt(conn, uid, "answers", str(aid), d["text"])
    qtype = await conn.fetchval("select type from app.questions where id = $1", qid)
    # A deep answer to a type that does not get one on its own means the user tapped expand
    # (the expand entry may have been flushed before this answer existed).
    expanded = pass_ == "deep" and qtype not in ctx.settings.deep_pass_auto_types
    node_ids = [u for u in map(_uuid, d.get("memory_node_ids") or []) if u is not None]
    await conn.execute(
        "insert into app.answers (id, question_id, user_id, pass, model, tier, text_enc,"
        " key_version, input_tokens, cached_tokens, output_tokens, cost_usd, latency_ms,"
        " memory_node_ids, expanded)"
        " values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)"
        " on conflict (id) do nothing",
        aid,
        qid,
        uid,
        pass_,
        str(d.get("model") or d.get("source") or "unknown"),
        str(d.get("tier") or "standard"),
        text_enc,
        version,
        d.get("input_tokens"),
        d.get("cached_tokens"),
        d.get("output_tokens"),
        _cost(d.get("cost_usd")),
        d.get("latency_ms") or {},
        node_ids,
        expanded,
    )


@handler("session.flush")
async def session_flush(job: Job, ctx: Ctx) -> None:
    sid = UUID(job.payload["session_id"])
    upto = job.payload.get("upto_entry_id")
    result = await flush_log(ctx, job.uid, sid, str(upto) if upto else None, final=False)
    log.info("session %s flushed: %s, %d deferred", sid, result.written, result.deferred)


@handler("session.ended")
async def session_ended(job: Job, ctx: Ctx) -> None:
    sid = UUID(job.payload["session_id"])
    uid = job.uid
    result = await flush_log(ctx, uid, sid, None, final=True)
    if not result.session_exists:
        return
    ended_at = _when(result.state.get("ended_at")) or result.last_at
    async with ctx.user(uid) as conn:
        if ended_at is None:
            # Valkey forgot the session (the sweeper found it open in Aurora): bill up to the
            # last thing the log recorded, never up to now.
            ended_at = await conn.fetchval(
                "select greatest(s.started_at, max(q.detected_at)) from app.sessions s"
                " left join app.questions q on q.session_id = s.id where s.id = $1"
                " group by s.started_at",
                sid,
            )
        row = await conn.fetchrow(
            "update app.sessions set ended_at = coalesce(ended_at, $2),"
            " billed_seconds = greatest(0, ceil(extract(epoch from"
            " (coalesce(ended_at, $2) - started_at))))::int"
            " where id = $1 returning store_transcript, me_audio_status",
            sid,
            ended_at,
        )
    assert row is not None
    # Post-call work waits up to 10 minutes for the user's audio (SQS DelaySeconds); an
    # upload that completes sooner enqueues the same job at once through the API.
    waiting = row["store_transcript"] and row["me_audio_status"] == "pending"
    await ctx.enqueue(
        "me_audio.transcribe",
        f"me_audio.transcribe:{sid}",
        uid,
        {"session_id": str(sid)},
        delay_s=ctx.settings.me_audio_wait_s if waiting else 0,
    )


@handler("me_audio.transcribe")
async def me_audio_transcribe(job: Job, ctx: Ctx) -> None:
    sid = UUID(job.payload["session_id"])
    uid = job.uid
    async with ctx.user(uid) as conn:
        row = await conn.fetchrow(
            "select store_transcript, ended_at, me_audio_status from app.sessions where id = $1",
            sid,
        )
    if row is None or row["ended_at"] is None:
        if job.age_s() < ME_AUDIO_GIVE_UP_S:
            raise RetryLater(30, "waiting for session.ended")
        log.warning("session %s never ended; transcribing without it", sid)
        if row is None:
            return
    key = me_audio_key(uid, sid)
    status = row["me_audio_status"]
    if status not in ("transcribed", "none"):
        audio = await ctx.store.get(key)
        segments = 0
        if audio is not None and row["store_transcript"] and ctx.stt is not None:
            head = await ctx.store.head(key)
            content_type = str((head or {}).get("ContentType") or "audio/wav")
            found = await ctx.stt.transcribe(audio, content_type)
            async with ctx.user(uid) as conn:
                for i, seg in enumerate(found):
                    await write_segment(
                        ctx, conn, uid, sid,
                        {"channel": "me", "seg_id": i, "start_ms": seg.start_ms,
                         "end_ms": seg.end_ms, "text": seg.text},
                    )  # fmt: skip
                await conn.execute(
                    "update app.sessions set me_audio_status = 'transcribed' where id = $1", sid
                )
            segments = len(found)
            status = "transcribed"
        else:
            if audio is not None and ctx.stt is None:
                log.warning("session %s: no pre-recorded STT configured; audio dropped", sid)
            async with ctx.user(uid) as conn:
                await conn.execute(
                    "update app.sessions set me_audio_status = 'none' where id = $1", sid
                )
            status = "none"
        log.info("session %s: me audio %s (%d segments)", sid, status, segments)
    # The user's audio stays only until its transcription (or until we know it is not needed).
    await ctx.store.delete(key)
    await ctx.enqueue("session.postcall", f"session.postcall:{sid}", uid, {"session_id": str(sid)})


# --- Post-call ----------------------------------------------------------------------


@dataclass
class Transcript:
    segments: list[tuple[int, str, str]]  # (start_ms, channel, text)
    questions: list[dict[str, Any]]


async def load_transcript(ctx: Ctx, conn: Conn, uid: UUID, sid: UUID) -> Transcript:
    """Decrypted in memory only."""
    segs: list[tuple[int, str, str]] = []
    for r in await conn.fetch(
        "select channel, seg_id, start_ms, text_enc, key_version from app.transcript_segments"
        " where session_id = $1 order by start_ms, channel, seg_id",
        sid,
    ):
        row_id = segment_row_id(sid, r["channel"], r["seg_id"])
        t = await ctx.crypto.decrypt(
            conn, uid, "transcript_segments", row_id, r["text_enc"], r["key_version"]
        )
        segs.append((r["start_ms"], r["channel"], t))
    questions: list[dict[str, Any]] = []
    for q in await conn.fetch(
        "select id, type, text_enc, key_version, forced_by_hotkey, detected_at"
        " from app.questions where session_id = $1 and speculative_kept order by detected_at",
        sid,
    ):
        item: dict[str, Any] = {"type": q["type"], "forced": q["forced_by_hotkey"]}
        if q["text_enc"] is not None:
            item["text"] = await ctx.crypto.decrypt(
                conn, uid, "questions", str(q["id"]), q["text_enc"], q["key_version"]
            )
        for a in await conn.fetch(
            "select id, pass, text_enc, key_version from app.answers where question_id = $1"
            " order by pass desc",
            q["id"],
        ):
            if a["text_enc"] is not None:
                item[f"{a['pass']}_answer"] = await ctx.crypto.decrypt(
                    conn, uid, "answers", str(a["id"]), a["text_enc"], a["key_version"]
                )
        questions.append(item)
    return Transcript(segs, questions)


def _clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def transcript_text(t: Transcript) -> str:
    return "\n".join(f"[{_clock(ms)}] {ch}: {txt}" for ms, ch, txt in t.segments)


def questions_text(t: Transcript) -> str:
    lines: list[str] = []
    for i, q in enumerate(t.questions, 1):
        lines.append(f"Q{i} ({q['type']}): {q.get('text', '(text not stored)')}")
        for p in ("fast", "deep"):
            if q.get(f"{p}_answer"):
                lines.append(f"Suggested ({p}): {q[f'{p}_answer']}")
    return "\n".join(lines)


def metadata_summary(mode: str, minutes: int, types: Sequence[str]) -> str:
    """The only output without transcript consent: counts, never words."""
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1
    by_type = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted(counts.items()))
    asked = f"{len(types)} questions detected ({by_type})." if types else "No questions detected."
    return (
        f"{mode.capitalize()} of about {minutes} minutes. {asked}\n\n"
        "The transcript was not stored for this session, so there is no written summary."
    )


def _bullets(items: object) -> str:
    return "\n".join(f"- {i}" for i in as_list(items)) or "- (none)"


def render_outputs(mode: str, data: dict[str, Any]) -> dict[str, str]:
    """Model output (already schema-checked) to the session_outputs kinds, as Markdown."""
    out: dict[str, str] = {"summary": str(data.get("summary", "")).strip()}
    if mode == "interview":
        parts: list[str] = [str(data.get("feedback", "")).strip()]
        if data.get("strengths"):
            parts.append("**Strengths**\n" + _bullets(data["strengths"]))
        if data.get("improvements"):
            parts.append("**To improve**\n" + _bullets(data["improvements"]))
        for i, q in enumerate(map(as_dict, as_list(data.get("questions"))), 1):
            parts.append(
                f"**Q{i} ({q.get('type', '')}): {q.get('question', '')}**\n"
                f"How it went: {q.get('how_it_went', '')}\n"
                f"Better: {q.get('better_answer', '')}"
            )
        out["feedback"] = "\n\n".join(p for p in parts if p)
        email = as_dict(data.get("follow_up_email"))
        out["follow_up_email"] = f"Subject: {email.get('subject', '')}\n\n{email.get('body', '')}"
    elif mode == "standup":
        commitments = [
            f"{c.get('text', '')}" + (f" (due {c['due_date']})" if c.get("due_date") else "")
            for c in map(as_dict, as_list(data.get("commitments")))
        ]
        out["standup_report"] = "\n\n".join(
            [
                "**Yesterday**\n" + _bullets(data.get("yesterday")),
                "**Today**\n" + _bullets(data.get("today")),
                "**Blockers**\n" + _bullets(data.get("blockers")),
                "**Commitments**\n" + _bullets(commitments),
            ]
        )
    else:
        if data.get("decisions"):
            out["summary"] += "\n\n**Decisions**\n" + _bullets(data["decisions"])
        if data.get("action_items"):
            out["summary"] += "\n\n**Action items**\n" + _bullets(data["action_items"])
    return out


@handler("session.postcall")
async def session_postcall(job: Job, ctx: Ctx) -> None:
    sid = UUID(job.payload["session_id"])
    uid = job.uid
    async with ctx.user(uid) as conn:
        session = await conn.fetchrow(
            "select s.mode, s.store_transcript, s.started_at, s.ended_at, s.billed_seconds,"
            " j.company, j.title as job_title, i.round, u.display_name"
            " from app.sessions s join app.users u on u.id = s.user_id"
            " left join app.jobs j on j.id = s.job_id"
            " left join app.interviews i on i.id = s.interview_id where s.id = $1",
            sid,
        )
        if session is None:
            return
        transcript = await load_transcript(ctx, conn, uid, sid)
    mode = str(session["mode"])
    types = [str(q["type"]) for q in transcript.questions]
    minutes = max(1, math.ceil(int(session["billed_seconds"]) / 60))
    if not session["store_transcript"] or not (transcript.segments or transcript.questions):
        outputs = {"summary": metadata_summary(mode, minutes, types)}
    else:
        context = [f"User: {session['display_name']}", f"Mode: {mode}", f"Length: {minutes} min"]
        if session["company"]:
            context.append(f"Company: {session['company']}, role: {session['job_title']}")
        if session["round"]:
            context.append(f"Round: {session['round']}")
        req = prompts.postcall_request(
            mode, context, transcript_text(transcript), questions_text(transcript)
        )
        outputs = render_outputs(mode, await complete_json(ctx.router, "postcall", req))
    async with ctx.user(uid) as conn:
        await conn.execute("delete from app.session_outputs where session_id = $1", sid)
        for kind, content in outputs.items():
            oid = uuid7_for(sid.int >> 80, str(sid), kind)
            enc, version = await ctx.crypto.encrypt(conn, uid, "session_outputs", str(oid), content)
            await conn.execute(
                "insert into app.session_outputs (id, session_id, user_id, kind, content_enc,"
                " key_version) values ($1, $2, $3, $4, $5, $6)",
                oid,
                sid,
                uid,
                kind,
                enc,
                version,
            )
        await conn.execute("update app.sessions set outputs_ready_at = now() where id = $1", sid)
    log.info("session %s: post-call outputs %s", sid, sorted(outputs))
