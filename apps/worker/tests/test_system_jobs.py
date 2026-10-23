"""System jobs: usage.report, user.delete (nothing left behind) and the session sweeper."""

import json
import time
from datetime import UTC, date, datetime, timedelta

import httpx2
import pytest

from copilot_core import jobs, keys
from copilot_core.careeros import CareerOsClient
from copilot_core.identity import CognitoUsers
from copilot_core.storage import me_audio_key, user_prefix
from copilot_worker.testing import FIXTURES, GatewayLog, Stack

pytestmark = pytest.mark.stack


async def test_usage_report_fills_usage_daily(stack: Stack) -> None:
    uid = await stack.new_user()
    day = date(2026, 9, 30)
    for i, seconds in enumerate((600, 300)):
        start = datetime(2026, 9, 30, 10 + i, tzinfo=UTC)
        await stack.admin.execute(
            "insert into app.sessions (id, user_id, mode, store_transcript, started_at, ended_at,"
            " billed_seconds) values (gen_random_uuid(), $1, 'interview', true, $2, $3, $4)",
            uid,
            start,
            start + timedelta(seconds=seconds),
            seconds,
        )
    message = jobs.envelope("usage.report", f"usage.report:{day}", None, {"day": str(day)})
    await stack.run(message)
    await stack.run(message)  # a rerun recomputes, never adds twice
    row = await stack.admin.fetchrow(
        "select live_seconds, reported_at from app.usage_daily where user_id = $1 and day = $2",
        uid,
        day,
    )
    assert row is not None
    assert (row["live_seconds"], row["reported_at"]) == (900, None)


async def user_tables(stack: Stack) -> list[str]:
    rows = await stack.admin.fetch(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        " join pg_attribute a on a.attrelid = c.oid and a.attname = 'user_id'"
        " where n.nspname = 'app' and c.relkind = 'r' and not a.attisdropped"
    )
    return sorted(r["relname"] for r in rows)


async def test_user_delete_leaves_nothing_behind(stack: Stack) -> None:
    uid = await stack.new_user()
    keep = await stack.new_user("Someone Else")
    # A full set of data: memory, a job and its pack, a session with outputs, documents.
    for owner in (uid, keep):
        doc = await stack.upload(
            owner, "resume", FIXTURES / "inputs" / "resume.md", "text/markdown"
        )
        await stack.run(jobs.envelope(
            "memory.extract", f"memory.extract:{owner}:del", owner, {"document_id": str(doc)}
        ))  # fmt: skip
        await stack.drain("digest.rebuild", "pack.build")
        jd = await stack.upload(
            owner, "job_description", FIXTURES / "inputs" / "job.md", "text/markdown"
        )
        await stack.run(jobs.envelope(
            "memory.extract", f"memory.extract:{owner}:deljd", owner, {"document_id": str(jd)}
        ))  # fmt: skip
        await stack.drain("pack.build")
        log = GatewayLog(stack, owner)
        await log.start()
        await log.segment("Hello there", 0)
        await log.end(seconds=60)
        await stack.run(log.ended_job())
        await stack.ctx.store.put(me_audio_key(owner, log.sid), b"audio", "audio/wav")
        await stack.drain("session.postcall")
        await stack.ctx.valkey.set(keys.qa(owner, "abc"), "{}")
        await stack.ctx.valkey.set(keys.user_active_session(owner), str(log.sid))
        await stack.admin.execute(
            "insert into app.job_runs (idempotency_key, job_type, user_id, status)"
            " values ($1, 'pack.build', $2, 'done')",
            f"test:{owner}",
            owner,
        )
    tables = await user_tables(stack)
    before = {t: await stack.admin.fetchval(f"select count(*) from app.{t} where user_id = $1", uid)
              for t in tables}  # fmt: skip
    for t in ("user_keys", "memory_nodes", "memory_edges", "applications", "resume_versions",
              "sessions",
              "transcript_segments", "session_outputs", "documents", "job_runs"):  # fmt: skip
        assert before[t] > 0, f"the test should have put rows in {t}"
    assert await stack.ctx.store.list_keys(user_prefix(uid))

    message = jobs.envelope("user.delete", f"user.delete:{uid}", uid, {})
    await stack.run(message)
    await stack.run(message)  # a rerun after a crash finds nothing to do and does not fail

    for t in tables:
        n = await stack.admin.fetchval(f"select count(*) from app.{t} where user_id = $1", uid)
        assert n == 0, f"{t} still has {n} rows of the deleted user"
    assert await stack.admin.fetchval("select count(*) from app.users where id = $1", uid) == 0
    assert await stack.ctx.store.list_keys(user_prefix(uid)) == []
    left = [k async for k in stack.ctx.valkey.scan_iter(match=f"*{uid}*")]
    assert left == [], f"Valkey keys left: {left}"
    # The other user is untouched.
    assert await stack.admin.fetchval("select count(*) from app.users where id = $1", keep) == 1
    assert await stack.ctx.store.list_keys(user_prefix(keep))
    assert await stack.ctx.valkey.get(keys.digest(keep)) is not None


class FakeUserPool:
    """Cognito's AdminDeleteUser as boto3 shapes it: a UserNotFoundException once gone."""

    class exceptions:
        class UserNotFoundException(Exception):
            pass

    def __init__(self, users: set[str]) -> None:
        self.users = users
        self.calls: list[dict[str, str]] = []

    def admin_delete_user(self, **kwargs: str) -> None:
        self.calls.append(kwargs)
        if kwargs["Username"] not in self.users:
            raise self.exceptions.UserNotFoundException()
        self.users.discard(kwargs["Username"])


async def test_user_delete_removes_the_cognito_user(stack: Stack) -> None:
    uid = await stack.new_user()
    pool = FakeUserPool({str(uid)})
    stack.ctx.users = CognitoUsers("us-east-1_Test", "us-east-1", client=pool)
    try:
        message = jobs.envelope("user.delete", f"user.delete:{uid}", uid, {})
        await stack.run(message)
        assert pool.users == set()
        assert pool.calls == [{"UserPoolId": "us-east-1_Test", "Username": str(uid)}]
        # A retry after a crash (the job_runs row was not marked done) finds the user gone.
        await stack.admin.execute("delete from app.job_runs where idempotency_key = $1",
                                  f"user.delete:{uid}")  # fmt: skip
        await stack.run(message)
        assert len(pool.calls) == 2
    finally:
        stack.ctx.users = None
    assert await stack.admin.fetchval("select count(*) from app.users where id = $1", uid) == 0


async def test_sweeper_ends_abandoned_sessions(stack: Stack) -> None:
    uid = await stack.new_user()
    abandoned = GatewayLog(stack, uid)
    await abandoned.start()
    await stack.ctx.valkey.hset(
        keys.sess_state(abandoned.sid), mapping={"updated_at": str(time.time() - 600)}
    )
    await stack.ctx.valkey.set(keys.user_active_session(uid), str(abandoned.sid))
    live = GatewayLog(stack, uid)
    await live.start()
    await stack.ctx.valkey.hset(
        keys.sess_state(live.sid), mapping={"updated_at": str(time.time() - 600)}
    )
    await stack.ctx.valkey.set(keys.sess_lease(live.sid), "task-2", ex=30)  # a gateway holds it
    recent = GatewayLog(stack, uid)
    await recent.start()  # just updated: inside the resume window

    sweep = jobs.envelope("sessions.sweep", "sessions.sweep:t1", None, {})
    await stack.run(sweep)
    ended = [m["payload"]["session_id"] for m in stack.take("session.ended")]
    assert str(abandoned.sid) in ended
    assert str(live.sid) not in ended
    assert str(recent.sid) not in ended
    state = await stack.ctx.valkey.hgetall(keys.sess_state(abandoned.sid))
    assert state[b"status"] == b"ended"
    assert state[b"ended_reason"] == b"abandoned"
    assert await stack.ctx.valkey.get(keys.user_active_session(uid)) is None
    await stack.run(sweep)  # already ended: not enqueued again
    assert str(abandoned.sid) not in [
        m["payload"]["session_id"] for m in stack.take("session.ended")
    ]
    # The enqueued session.ended bills up to the last state update, not until now.
    await stack.run(abandoned.ended_job())
    billed = await stack.admin.fetchval(
        "select billed_seconds from app.sessions where id = $1", abandoned.sid
    )
    assert billed is not None


async def test_user_delete_revokes_the_career_os_link(stack: Stack) -> None:
    uid = await stack.new_user()
    async with stack.ctx.user(uid) as conn:
        sealed, version = await stack.ctx.crypto.encrypt(
            conn, uid, "integrations", f"{uid}:career_os",
            json.dumps({"token": "cpt_secret", "link_id": "cpl_1"}),
        )  # fmt: skip
        await conn.execute(
            "insert into app.integrations (user_id, provider, token_enc, key_version, scopes,"
            " external_id) values ($1, 'career_os', $2, $3, '{applications:read}', 'u1')",
            uid, sealed, version,
        )  # fmt: skip
    seen: list[tuple[str, str, str]] = []

    def career_os(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx2.Response(204)

    stack.ctx.career_os = CareerOsClient("http://career-os.test",
                                         transport=httpx2.MockTransport(career_os))  # fmt: skip
    try:
        await stack.run(jobs.envelope("user.delete", f"user.delete:{uid}", uid, {}))
    finally:
        stack.ctx.career_os = None
    assert seen == [("DELETE", "/v1/career-pilot/link", "Bearer cpt_secret")]
    assert await stack.admin.fetchval("select count(*) from app.integrations where user_id = $1",
                                      uid) == 0  # fmt: skip
