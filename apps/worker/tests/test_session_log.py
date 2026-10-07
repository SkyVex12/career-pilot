"""Session log jobs: flush, end, the user's audio, post-call outputs, and transcript consent.

`GatewayLog` writes `sess:{sid}:state` and `sess:{sid}:log` the way the gateway does
(copilot_gateway.session and answer: the same fields and entry kinds); tests/test_end_to_end.py
runs the real gateway instead.
"""

import uuid
from datetime import timedelta
from uuid import UUID

import pytest

from copilot_core import jobs, keys
from copilot_core.crypto import DecryptError, open_sealed, segment_row_id
from copilot_core.ids import uuid7
from copilot_core.storage import me_audio_key
from copilot_worker.sessionlog import answer_id
from copilot_worker.testing import STARTED, GatewayLog, Stack

pytestmark = pytest.mark.stack


async def decrypt_all(stack: Stack, uid: UUID, sid: UUID) -> dict[str, list[str]]:
    """Every stored text of a session, decrypted through the worker's crypto (as the user)."""
    out: dict[str, list[str]] = {"segments": [], "questions": [], "answers": [], "outputs": []}
    async with stack.ctx.user(uid) as conn:
        for r in await conn.fetch(
            "select channel, seg_id, text_enc, key_version from app.transcript_segments"
            " where session_id = $1 order by start_ms",
            sid,
        ):
            row = segment_row_id(sid, r["channel"], r["seg_id"])
            text = await stack.ctx.crypto.decrypt(
                conn, uid, "transcript_segments", row, r["text_enc"], r["key_version"]
            )
            out["segments"].append(f"{r['channel']}: {text}")
        for r in await conn.fetch(
            "select id, text_enc, key_version from app.questions where session_id = $1", sid
        ):
            if r["text_enc"] is not None:
                text = await stack.ctx.crypto.decrypt(
                    conn, uid, "questions", str(r["id"]), r["text_enc"], r["key_version"]
                )
                out["questions"].append(text)
        for r in await conn.fetch(
            "select a.id, a.text_enc, a.key_version from app.answers a"
            " join app.questions q on q.id = a.question_id where q.session_id = $1",
            sid,
        ):
            if r["text_enc"] is not None:
                text = await stack.ctx.crypto.decrypt(
                    conn, uid, "answers", str(r["id"]), r["text_enc"], r["key_version"]
                )
                out["answers"].append(text)
        for r in await conn.fetch(
            "select id, kind, content_enc, key_version from app.session_outputs"
            " where session_id = $1 order by kind",
            sid,
        ):
            text = await stack.ctx.crypto.decrypt(
                conn, uid, "session_outputs", str(r["id"]), r["content_enc"], r["key_version"]
            )
            out["outputs"].append(f"{r['kind']}: {text}")
    return out


async def test_full_interview_log_to_postcall(stack: Stack) -> None:
    uid = await stack.new_user()
    log = GatewayLog(stack, uid)
    await log.start()
    await log.segment("Tell me about a migration you led.", 1000)
    q1 = uuid7()
    # The fast pass of a speculative question finishes before the question is confirmed
    # and logged: its answer arrives first.
    early = await log.answer(q1, "fast", "- Moved the ledger live [n:aaaa]")
    upto = early
    await stack.run(log.flush_job(upto))
    assert (
        await stack.admin.fetchval("select count(*) from app.answers where user_id = $1", uid) == 0
    )
    # The orphan answer stays in the stream for the next flush.
    remaining = await stack.ctx.valkey.xrange(keys.sess_log(log.sid))
    assert [r[0].decode() for r in remaining] == [early]

    await log.question(q1, "Tell me about a migration you led.")
    await log.answer(q1, "deep", "Situation: the ledger was full...")
    upto = await log.add({"kind": "feedback", "question_id": str(q1), "rating": "up"})
    await stack.run(log.flush_job(upto))
    await stack.run(log.flush_job(upto))  # a replayed flush changes nothing
    answers = await stack.admin.fetch(
        "select id, pass, feedback, expanded, model, input_tokens from app.answers"
        " where question_id = $1 order by pass",
        q1,
    )
    assert [(a["pass"], a["feedback"], a["expanded"]) for a in answers] == [
        ("deep", "up", False),  # behavioral gets a deep pass on its own: not an expand
        ("fast", "up", False),
    ]
    assert {a["id"] for a in answers} == {answer_id(q1, "deep"), answer_id(q1, "fast")}
    assert await stack.ctx.valkey.xlen(keys.sess_log(log.sid)) == 0  # trimmed after commit

    await log.end(seconds=605)
    await stack.run(log.ended_job())
    session = await stack.admin.fetchrow(
        "select mode, ended_at, billed_seconds, store_transcript from app.sessions where id = $1",
        log.sid,
    )
    assert session is not None
    assert session["billed_seconds"] == 605
    assert session["ended_at"] == STARTED + timedelta(seconds=605)
    [transcribe] = stack.take("me_audio.transcribe")
    assert transcribe["idempotency_key"] == f"me_audio.transcribe:{log.sid}"
    assert await stack.ctx.valkey.ttl(keys.sess_log(log.sid)) > 0  # kept 1 h after the flush

    # The user's own audio arrives; it is transcribed, stored as "me" segments, and deleted.
    await stack.ctx.store.put(me_audio_key(uid, log.sid), b"RIFF fake wav", "audio/wav")
    await stack.admin.execute(
        "update app.sessions set me_audio_status = 'uploaded' where id = $1", log.sid
    )
    await stack.run(transcribe)
    assert await stack.ctx.store.head(me_audio_key(uid, log.sid)) is None
    sent = stack.deepgram.requests[-1]
    assert sent.headers["content-type"] == "audio/wav"
    assert sent.url.params["model"] == "nova-3"
    [postcall] = stack.take("session.postcall")
    await stack.run(postcall)

    texts = await decrypt_all(stack, uid, log.sid)
    assert texts["segments"][0] == "them: Tell me about a migration you led."
    assert sum(s.startswith("me: ") for s in texts["segments"]) == 2
    assert texts["questions"] == ["Tell me about a migration you led."]
    assert sorted(texts["answers"]) == [
        "- Moved the ledger live [n:aaaa]",
        "Situation: the ledger was full...",
    ]
    kinds = [o.split(":", 1)[0] for o in texts["outputs"]]
    assert kinds == ["feedback", "follow_up_email", "summary"]
    assert "Subject: Thank you for today's conversation" in texts["outputs"][1]
    ready = await stack.admin.fetchval(
        "select outputs_ready_at from app.sessions where id = $1", log.sid
    )
    assert ready is not None
    # The post-call prompt got the transcript and the session log as data blocks.
    job, body = stack.claude.requests[-1]
    assert job == "postcall_interview"
    content = body["messages"][0]["content"]
    assert "<transcript>" in content
    assert "<session_log>" in content
    assert "me: Sure. At Acme" in content

    # Post-call again (a retried job) replaces the outputs instead of adding to them.
    await stack.run(postcall)
    count = await stack.admin.fetchval(
        "select count(*) from app.session_outputs where session_id = $1", log.sid
    )
    assert count == 3


async def test_expand_on_a_non_auto_type_marks_the_deep_answer(stack: Stack) -> None:
    uid = await stack.new_user()
    log = GatewayLog(stack, uid)
    await log.start()
    q = uuid7()
    await log.question(q, "What is your favorite database?", qtype="other")
    await log.add({"kind": "expand", "question_id": str(q)})
    upto = await log.answer(q, "deep", "PostgreSQL, because...")
    await stack.run(log.flush_job(upto))
    expanded = await stack.admin.fetchval(
        "select expanded from app.answers where question_id = $1 and pass = 'deep'", q
    )
    assert expanded is True


async def test_without_transcript_consent_no_text_is_stored(stack: Stack) -> None:
    uid = await stack.new_user()
    log = GatewayLog(stack, uid, store_transcript=False)
    await log.start()
    # A gateway bug that leaked text into the log must still not reach Aurora.
    await log.segment("Private words from the other side", 1000)
    q = uuid7()
    await log.question(q, "A question in their words?")
    upto = await log.answer(q, "fast", "- an answer")
    await stack.run(log.flush_job(upto))
    await log.end(seconds=120)
    await stack.run(log.ended_job())
    [transcribe] = stack.take("me_audio.transcribe")
    assert transcribe["payload"] == {"session_id": str(log.sid)}
    await stack.ctx.store.put(me_audio_key(uid, log.sid), b"RIFF audio", "audio/wav")
    calls = len(stack.deepgram.requests)
    await stack.run(transcribe)
    assert len(stack.deepgram.requests) == calls  # no audio sent to STT
    assert await stack.ctx.store.head(me_audio_key(uid, log.sid)) is None  # and none kept
    [postcall] = stack.take("session.postcall")
    claude_calls = len(stack.claude.requests)
    await stack.run(postcall)
    assert len(stack.claude.requests) == claude_calls  # no transcript, no model call

    assert (
        await stack.admin.fetchval(
            "select count(*) from app.transcript_segments where session_id = $1", log.sid
        )
        == 0
    )
    q_row = await stack.admin.fetchrow(
        "select text_enc, key_version, type from app.questions where id = $1", q
    )
    assert q_row is not None
    assert (q_row["text_enc"], q_row["key_version"], q_row["type"]) == (None, None, "behavioral")
    a_row = await stack.admin.fetchrow(
        "select text_enc, input_tokens, latency_ms from app.answers where question_id = $1", q
    )
    assert a_row is not None
    assert a_row["text_enc"] is None
    assert a_row["input_tokens"] == 4200  # the metadata stays
    me_status = await stack.admin.fetchval(
        "select me_audio_status from app.sessions where id = $1", log.sid
    )
    assert me_status == "none"
    texts = await decrypt_all(stack, uid, log.sid)
    assert texts["segments"] == texts["questions"] == texts["answers"] == []
    [summary] = texts["outputs"]
    assert summary.startswith("summary: Interview of about 2 minutes. 1 questions detected")
    assert "Private" not in summary


async def test_ciphertext_is_bound_to_row_and_user(stack: Stack) -> None:
    uid, other = await stack.new_user(), await stack.new_user()
    log = GatewayLog(stack, uid)
    await log.start()
    upto = await log.segment("Secret plan for the migration", 0)
    await stack.run(log.flush_job(upto))
    row = await stack.admin.fetchrow(
        "select text_enc, key_version from app.transcript_segments where session_id = $1", log.sid
    )
    assert row is not None
    assert b"Secret" not in row["text_enc"]
    row_id = segment_row_id(log.sid, "them", 0)
    async with stack.ctx.user(uid) as conn:
        dek = await stack.ctx.crypto.key(conn, uid, row["key_version"])
    assert open_sealed(dek, "transcript_segments", row_id, row["text_enc"]) == (
        "Secret plan for the migration"
    )
    # Another row id (copied ciphertext) does not open.
    with pytest.raises(DecryptError):
        open_sealed(dek, "transcript_segments", segment_row_id(log.sid, "them", 1), row["text_enc"])
    # The other user's key does not open it either.
    async with stack.ctx.user(other) as conn:
        _, other_dek = await stack.ctx.crypto.current_key(conn, other)
        with pytest.raises(DecryptError):
            open_sealed(other_dek, "transcript_segments", row_id, row["text_enc"])
        # And as the other user, the first user's data key is not even visible (RLS).
        assert (
            await conn.fetchval("select count(*) from app.user_keys where user_id = $1", uid) == 0
        )
    # The wrapped data key only unwraps for its own user.
    wrapped = await stack.admin.fetchval(
        "select encrypted_dek from app.user_keys where user_id = $1", uid
    )
    with pytest.raises(DecryptError):
        await stack.ctx.crypto.provider.decrypt_data_key(wrapped, other)


async def test_session_ended_without_any_flush_creates_the_row(stack: Stack) -> None:
    uid = await stack.new_user()
    log = GatewayLog(stack, uid)
    await log.start()
    await log.end(seconds=30)
    await stack.run(log.ended_job())
    await stack.run(log.ended_job())  # twice: still one row, same billing
    assert (
        await stack.admin.fetchval("select billed_seconds from app.sessions where id = $1", log.sid)
        == 30
    )


async def test_a_session_of_another_user_is_refused(stack: Stack) -> None:
    uid, intruder = await stack.new_user(), await stack.new_user()
    log = GatewayLog(stack, uid)
    await log.start()
    upto = await log.segment("hello", 0)
    with pytest.raises(ValueError, match="another user"):
        await stack.run(jobs.envelope(
            "session.flush", f"session.flush:{log.sid}:x", intruder,
            {"session_id": str(log.sid), "upto_entry_id": upto},
        ))  # fmt: skip


async def test_me_audio_waits_for_session_ended(stack: Stack) -> None:
    from copilot_worker.runtime import RetryLater

    uid = await stack.new_user()
    sid = uuid.uuid4()
    with pytest.raises(RetryLater):
        await stack.run(jobs.envelope(
            "me_audio.transcribe", f"me_audio.transcribe:{sid}", uid, {"session_id": str(sid)}
        ))  # fmt: skip
