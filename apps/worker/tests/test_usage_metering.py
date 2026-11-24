"""Connected-time checkpoints reach an independent, retry-safe usage ledger."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from copilot_core import jobs, keys
from copilot_worker.testing import STARTED, GatewayLog, Stack

pytestmark = pytest.mark.stack


async def metered_log(stack: Stack, *, payer: UUID | None = None) -> GatewayLog:
    workspace = await stack.new_user()
    log = GatewayLog(stack, workspace)
    await log.start()
    await stack.ctx.valkey.hset(
        keys.sess_state(log.sid),
        mapping={
            "metering_version": "connected-v1",
            "usage_payer_account_id": str(payer or workspace),
            "billing_price_version": "live-usd-v1",
            "billing_currency": "USD",
            "billing_rate_minor_per_hour": "500",
            "summarize": "0",
        },
    )
    return log


def checkpoint(log: GatewayLog, start: datetime, seconds: int, **overrides: object) -> dict:
    return {
        "kind": "usage.interval",
        "metering_version": "connected-v1",
        "service": "live",
        "event_id": str(uuid4()),
        "session_id": str(log.sid),
        "payer_account_id": str(log.uid),
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(seconds=seconds)).isoformat(),
        "duration_ms": seconds * 1000,
        "price_version": "live-usd-v1",
        "currency": "USD",
        "rate_minor_per_hour": 500,
        **overrides,
    }


async def recorded_seconds(stack: Stack, sid: UUID) -> int:
    return int(
        await stack.admin.fetchval(
            "select coalesce(sum(duration_microseconds),0)/1000000"
            " from app.live_usage_intervals where session_id=$1",
            sid,
        )
    )


async def test_connected_time_excludes_gap_and_deduplicates_retry(stack: Stack) -> None:
    log = await metered_log(stack)
    first = checkpoint(log, STARTED, 60)
    await log.add(first)
    upto = await log.add(first)  # transport retry with the same logical event ID
    await stack.run(log.flush_job(upto))
    await stack.run(log.flush_job(upto))
    assert await recorded_seconds(stack, log.sid) == 60

    # A new attachment starts after two disconnected minutes. A stale overlapping
    # checkpoint from another producer cannot charge those first 60 seconds twice.
    await log.add(checkpoint(log, STARTED, 60))
    await log.add(checkpoint(log, STARTED + timedelta(seconds=180), 60))
    await log.end(seconds=600)
    await stack.run(log.ended_job())
    assert await recorded_seconds(stack, log.sid) == 120
    assert (
        await stack.admin.fetchval(
            "select billed_seconds from app.sessions where id=$1",
            log.sid,
        )
        == 120
    )
    assert (
        await stack.admin.fetchval(
            "select count(*) from app.legacy_usage_estimates where session_id=$1",
            log.sid,
        )
        == 0
    )

    # History deletion cannot erase already-accepted metering or change its rate.
    await stack.admin.execute("delete from app.sessions where id=$1", log.sid)
    assert await recorded_seconds(stack, log.sid) == 120
    row = await stack.admin.fetchrow(
        "select payer_account_id,rate_minor_per_hour,price_version"
        " from app.live_usage_sessions where session_id=$1",
        log.sid,
    )
    assert row is not None
    assert tuple(row.values()) == (log.uid, 500, "live-usd-v1")


async def test_usage_keeps_actual_payer_separate_from_workspace(stack: Stack) -> None:
    payer = await stack.new_user("Account paying for a shared workspace")
    log = await metered_log(stack, payer=payer)
    upto = await log.add(checkpoint(log, STARTED, 10, payer_account_id=str(payer)))
    await stack.run(log.flush_job(upto))
    row = await stack.admin.fetchrow(
        "select workspace_id,payer_account_id from app.live_usage_sessions where session_id=$1",
        log.sid,
    )
    assert row is not None
    assert (row["workspace_id"], row["payer_account_id"]) == (log.uid, payer)


async def test_checkpoint_split_at_utc_month_boundary(stack: Stack) -> None:
    log = await metered_log(stack)
    start = datetime(2026, 10, 31, 23, 59, 50, tzinfo=UTC)
    upto = await log.add(checkpoint(log, start, 20))
    await stack.run(log.flush_job(upto))
    rows = await stack.admin.fetch(
        "select day,duration_microseconds from app.live_usage_intervals"
        " where session_id=$1 order by day",
        log.sid,
    )
    assert [(str(r["day"]), r["duration_microseconds"]) for r in rows] == [
        ("2026-10-31", 10_000_000),
        ("2026-11-01", 10_000_000),
    ]


async def test_crash_finalization_uses_only_committed_checkpoints(stack: Stack) -> None:
    log = await metered_log(stack)
    upto = await log.add(checkpoint(log, STARTED, 10))
    await stack.run(log.flush_job(upto))
    # Simulate expired state after the gateway dies. The last ten seconds are the
    # only confirmed usage; the sweeper's later run must not bill until "now".
    await stack.ctx.valkey.delete(keys.sess_state(log.sid), keys.sess_log(log.sid))
    await stack.run(log.ended_job())
    assert (
        await stack.admin.fetchval(
            "select billed_seconds from app.sessions where id=$1",
            log.sid,
        )
        == 10
    )
    assert await stack.admin.fetchval(
        "select ended_at from app.sessions where id=$1",
        log.sid,
    ) == STARTED + timedelta(seconds=10)


async def test_pending_checkpoint_uses_durable_attribution_after_cache_expiry(stack: Stack) -> None:
    log = await metered_log(stack)
    upto = await log.add({"kind": "connected"})
    await stack.run(log.flush_job(upto))  # pin payer and price before the first interval
    upto = await log.add(checkpoint(log, STARTED, 10))
    await stack.ctx.valkey.delete(keys.sess_state(log.sid))
    await stack.run(log.flush_job(upto))
    assert await recorded_seconds(stack, log.sid) == 10


@pytest.mark.parametrize(
    "change",
    [
        {"duration_ms": 999},
        {"payer_account_id": str(uuid4())},
        {"rate_minor_per_hour": 300},
        {"session_id": str(uuid4())},
    ],
)
async def test_invalid_checkpoint_rolls_back_and_stays_retryable(
    stack: Stack,
    change: dict,
) -> None:
    log = await metered_log(stack)
    upto = await log.add(checkpoint(log, STARTED, 10, **change))
    with pytest.raises(ValueError, match="usage"):
        await stack.run(log.flush_job(upto))
    assert await recorded_seconds(stack, log.sid) == 0
    assert await stack.ctx.valkey.xlen(keys.sess_log(log.sid)) > 0


async def test_daily_rollup_survives_deleted_history(stack: Stack) -> None:
    log = await metered_log(stack)
    upto = await log.add(checkpoint(log, STARTED, 60))
    await stack.run(log.flush_job(upto))
    await stack.admin.execute("delete from app.sessions where id=$1", log.sid)
    day = STARTED.date()
    await stack.run(jobs.envelope("usage.report", f"usage.report:{day}", None, {"day": str(day)}))
    assert (
        await stack.admin.fetchval(
            "select live_seconds from app.usage_daily where user_id=$1 and day=$2",
            log.uid,
            day,
        )
        == 60
    )
