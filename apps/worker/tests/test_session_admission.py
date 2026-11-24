"""Retained payloads require committed writer receipts and an existing parent."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from copilot_core import jobs, keys
from copilot_core.db import create_pool
from copilot_core.teststack import as_role
from copilot_gateway.admission import DurableSession
from copilot_worker.testing import Stack

pytestmark = pytest.mark.stack


async def test_old_confirmed_batch_survives_takeover_but_unconfirmed_payload_is_dropped(
    stack: Stack,
):
    uid, sid = await stack.new_user(), uuid4()
    pool = await create_pool(as_role(stack.url, "app_gateway"))
    meta = {
        "mode": "meeting",
        "started_at": datetime.now(UTC).isoformat(),
        "store_transcript": "1",
        "metering_version": "connected-v1",
        "billing_price_version": "live-usd-v1",
        "billing_currency": "USD",
        "billing_rate_minor_per_hour": "500",
        "summarize": "0",
    }
    try:
        original = await DurableSession.acquire(pool, sid, uid, uid, metadata=meta)
        confirmed, unconfirmed = uuid4(), uuid4()
        event = {"kind": "question", "question_id": str(confirmed), "text": "Confirmed question"}
        async with original.batch(str(uuid4()), [json.dumps(event)]) as retained:
            await stack.ctx.valkey.xadd(keys.sess_log(sid), {"e": retained[0]})
        latest = await DurableSession.acquire(pool, sid, uid, uid)
        await stack.ctx.valkey.hset(
            keys.sess_state(sid),
            mapping={**meta, "writer_generation": "2", "user_id": str(uid)},
        )
        # A stale producer forges the old batch ID but changes the payload. Its
        # receipt proves exactly the old bytes, so this new question is refused.
        forged = {**json.loads(retained[0]), "question_id": str(unconfirmed)}
        upto = await stack.ctx.valkey.xadd(keys.sess_log(sid), {"e": json.dumps(forged)})
        await stack.run(
            jobs.envelope(
                "session.flush",
                f"flush:{sid}",
                uid,
                {
                    "session_id": str(sid),
                    "upto_entry_id": upto.decode(),
                },
            )
        )
        assert (
            await stack.admin.fetchval(
                "select count(*) from app.questions where session_id=$1",
                sid,
            )
            == 1
        )
        assert (
            await stack.admin.fetchval("select id from app.questions where session_id=$1", sid)
            == confirmed
        )
        assert await stack.ctx.valkey.xlen(keys.sess_log(sid)) == 0

        guest = await stack.new_user()
        seat = uuid4()
        await stack.admin.execute(
            "insert into app.session_seats(id,user_id,session_id,host_account_id,guest_id,"
            "host_name,guest_name,guest_email,mode,status,expires_at)"
            " values($1,$2,$3,$2,$4,'Host','Guest','guest@test.invalid','meeting','admitted',$5)",
            seat,
            uid,
            sid,
            guest,
            datetime.now(UTC) + timedelta(hours=1),
        )
        # A delayed end of generation one cannot close generation two or its guest.
        await stack.run(
            jobs.envelope(
                "session.ended",
                f"end:{sid}:g1",
                uid,
                {
                    "session_id": str(sid),
                    "writer_generation": 1,
                },
            )
        )
        assert (
            await stack.admin.fetchval("select ended_at from app.sessions where id=$1", sid) is None
        )
        assert (
            await stack.admin.fetchval("select status from app.session_seats where id=$1", seat)
            == "admitted"
        )
        await latest.close("normal")
        await stack.run(
            jobs.envelope(
                "session.ended",
                f"end:{sid}:g2",
                uid,
                {
                    "session_id": str(sid),
                    "writer_generation": 2,
                },
            )
        )
        assert (
            await stack.admin.fetchval("select ended_at from app.sessions where id=$1", sid)
            is not None
        )
        assert (
            await stack.admin.fetchval("select status from app.session_seats where id=$1", seat)
            == "ended"
        )
    finally:
        await pool.close()


async def test_deleted_durable_parent_cannot_be_recreated_from_cache(stack: Stack):
    uid, sid = await stack.new_user(), uuid4()
    await stack.ctx.valkey.hset(
        keys.sess_state(sid),
        mapping={
            "user_id": str(uid),
            "writer_generation": "1",
            "mode": "meeting",
            "started_at": datetime.now(UTC).isoformat(),
            "store_transcript": "1",
        },
    )
    await stack.run(jobs.envelope("session.ended", f"end:{sid}", uid, {"session_id": str(sid)}))
    assert await stack.admin.fetchval("select count(*) from app.sessions where id=$1", sid) == 0
