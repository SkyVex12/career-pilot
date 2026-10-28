"""Session log jobs (ARCHITECTURE.md, "Session logging" and "SQS job catalog").

`session.flush` copies the gateway's `sess:{sid}:log` stream into Aurora every 30 seconds;
`session.ended` copies the rest, closes the session and starts `session.postcall`, which writes
the summary and feedback. The user's own words arrive in the log like the other side's: the
gateway transcribes the mic live ("me" segments), so nothing waits for an upload.

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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from copilot_core import jobs, keys
from copilot_core.crypto import UserCrypto, segment_row_id
from copilot_core.db import Conn
from copilot_core.ids import uuid7, uuid7_for
from copilot_core.storage import me_audio_key
from copilot_core.valkey_client import text
from copilot_core.workplace import local_today, memory_version
from copilot_worker import prompts
from copilot_worker.context import Ctx
from copilot_worker.llm import as_dict, as_list, complete_json
from copilot_worker.runtime import Job, handler
from copilot_worker.workplace import close_commitments, open_commitments_block, reopen_commitments

log = logging.getLogger(__name__)
# New commitments a standup report keeps: the post-call prompt asks for at most this many.
MAX_STANDUP_COMMITMENTS = 3
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    conn: Conn, uid: UUID, sid: UUID, meta: dict[str, Any], crypto: UserCrypto | None = None
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
    # The company a standup or meeting is at, if it is still this user's.
    employer_id = await conn.fetchval(
        "select id from app.employers where id = $1", _uuid(meta.get("employer_id"))
    )
    await conn.execute(
        "insert into app.sessions (id, user_id, mode, job_id, interview_id, platform, title,"
        " store_transcript, started_at, employer_id) values ($1, $2, $3, $4, $5, $6, $7, $8, $9,"
        " $10) on conflict (id) do nothing",
        sid,
        uid,
        mode,
        job_id,
        interview_id,
        meta.get("platform") or None,
        meta.get("title") or None,
        str(meta.get("store_transcript")) in ("1", "True", "true"),
        started_at,
        employer_id,
    )
    agenda = str(meta.get("agenda") or "")
    if agenda and crypto is not None:
        enc, version = await crypto.encrypt(conn, uid, "sessions", str(sid), agenda)
        await conn.execute(
            "update app.sessions set agenda_enc = $2, agenda_key_version = $3 where id = $1",
            sid,
            enc,
            version,
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
        session = await ensure_session(conn, uid, sid, meta, ctx.crypto)
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
    # The gateway says whether the user tapped expand for this deep answer; an expand entry
    # flushed later sets it too.
    expanded = pass_ == "deep" and bool(d.get("expanded"))
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
            " where id = $1 returning id",
            sid,
            ended_at,
        )
        assert row is not None
        if result.state.get("summarize") == "0":
            # The user chose the transcript only: no post-call outputs, so the session is
            # ready now and nothing is written to the company's memory.
            await conn.execute(
                "update app.sessions set outputs_ready_at = coalesce(outputs_ready_at, now())"
                " where id = $1",
                sid,
            )
            log.info("session %s: ended without a summary, as asked", sid)
            return
    await ctx.enqueue("session.postcall", f"session.postcall:{sid}", uid, {"session_id": str(sid)})


@handler("me_audio.transcribe")
async def me_audio_transcribe(job: Job, ctx: Ctx) -> None:
    """Left from before live transcription of the user's mic: a job enqueued by an older
    gateway or API. Nothing is transcribed any more; an uploaded recording is deleted and the
    post-call job runs (enqueued once: its key is the session's)."""
    sid = UUID(job.payload["session_id"])
    uid = job.uid
    await ctx.store.delete(me_audio_key(uid, sid))
    async with ctx.user(uid) as conn:
        await conn.execute(
            "update app.sessions set me_audio_status = 'none'"
            " where id = $1 and me_audio_status in ('pending', 'uploaded')",
            sid,
        )
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
            for c in map(as_dict, as_list(data.get("commitments"))[:MAX_STANDUP_COMMITMENTS])
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
            " s.employer_id, e.name as employer_name,"
            " j.company, j.title as job_title, i.round, u.display_name, u.settings"
            " from app.sessions s join app.users u on u.id = s.user_id"
            " left join app.jobs j on j.id = s.job_id"
            " left join app.employers e on e.id = s.employer_id"
            " left join app.interviews i on i.id = s.interview_id where s.id = $1",
            sid,
        )
        if session is None:
            return
        transcript = await load_transcript(ctx, conn, uid, sid)
    mode = str(session["mode"])
    types = [str(q["type"]) for q in transcript.questions]
    minutes = max(1, math.ceil(int(session["billed_seconds"]) / 60))
    data: dict[str, Any] = {}
    closable: dict[str, UUID] = {}
    if not session["store_transcript"] or not (transcript.segments or transcript.questions):
        outputs = {"summary": metadata_summary(mode, minutes, types)}
    else:
        # The call's day in the user's time zone, so "tomorrow" or "by Friday" becomes a date.
        day = local_today(as_dict(session["settings"]), session["started_at"])
        context = [
            f"User: {session['display_name']}",
            f"Mode: {mode}",
            f"Date: {day:%A} {day.isoformat()}",
            f"Length: {minutes} min",
        ]
        if session["company"]:
            context.append(f"Company: {session['company']}, role: {session['job_title']}")
        if session["employer_name"]:
            context.append(f"At work, company: {session['employer_name']}")
        if session["round"]:
            context.append(f"Round: {session['round']}")
        open_block: str | None = None
        if mode == "standup" and session["employer_id"] is not None:
            # 2026-10-27: without them the report stored 6 commitments, mostly steps of one
            # task and repeats of what was already open, and closed none.
            async with ctx.user(uid) as conn:
                open_block, closable = await open_commitments_block(
                    conn, uid, session["employer_id"], str(sid)
                )
        req = prompts.postcall_request(
            mode, context, transcript_text(transcript), questions_text(transcript), open_block
        )
        data = await complete_json(ctx.router, "postcall", req)
        outputs = render_outputs(mode, data)
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
        employer_id = session["employer_id"]
        version = None
        if mode == "standup" and employer_id is not None:
            # The standup's commitments go back into that company's memory and feed the next
            # standup pack. They are the user's own promises, so they are kept at once (the
            # post-call screen shows them with a remove button). A rerun replaces them.
            day = local_today(as_dict(session["settings"]), session["started_at"])
            added, closed = await replace_session_commitments(
                conn, uid, employer_id, sid, day, as_list(data.get("commitments")),
                as_list(data.get("closed_commitments")), closable,
            )  # fmt: skip
            log.info(
                "session %s: %d commitments for employer %s, %d closed",
                sid, added, employer_id, closed,
            )  # fmt: skip
            version = await memory_version(conn, uid, employer_id)
        await conn.execute("update app.sessions set outputs_ready_at = now() where id = $1", sid)
    if version is not None:
        await ctx.queue.send(jobs.workplace_refresh(uid, session["employer_id"], version))
    log.info("session %s: post-call outputs %s", sid, sorted(outputs))


async def replace_session_commitments(
    conn: Conn,
    uid: UUID,
    employer_id: UUID,
    sid: UUID,
    day: date,
    commitments: Sequence[Any],
    closed: Sequence[Any] = (),
    closable: Mapping[str, UUID] | None = None,
) -> tuple[int, int]:
    """A standup's commitments as `commitment` nodes of its company (source: the session),
    at most `MAX_STANDUP_COMMITMENTS`, and the open ones it closed (by short id, from
    `closable`) marked done with `closed_by` the session. A rerun replaces both."""
    await reopen_commitments(conn, uid, employer_id, str(sid))
    await conn.execute(
        "delete from app.memory_nodes where kind = 'commitment' and source->>'type' = 'session'"
        " and source->>'ref' = $1",
        str(sid),
    )
    rows: list[tuple[Any, ...]] = []
    for c in map(as_dict, commitments[:MAX_STANDUP_COMMITMENTS]):
        text_ = str(c.get("text") or "").strip()
        if not text_:
            continue
        due = str(c.get("due_date") or "").strip()
        body = {"due_date": due if DATE.fullmatch(due) else "", "state": "open",
                "session_id": str(sid)}  # fmt: skip
        rows.append((uuid7(), uid, employer_id, text_, text_, body,
                     {"type": "session", "ref": str(sid)}, day))  # fmt: skip
    await conn.executemany(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " body, source, valid_from) values ($1, $2, $3, 'commitment', 'workplace', $4, $5, $6,"
        " $7, $8)",
        rows,
    )
    done = await close_commitments(conn, closed, closable or {}, str(sid))
    return len(rows), done
