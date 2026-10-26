"""The local scheduler: cadences, one send per slot, and standup packs 30 minutes ahead."""

from datetime import UTC, date, datetime, timedelta

import pytest

from copilot_core.ids import uuid7
from copilot_core.jobs import MemoryJobQueue
from copilot_worker.scheduler import Scheduler, next_standup, slots
from copilot_worker.testing import Stack


def test_slots() -> None:
    s = slots(datetime(2026, 10, 6, 0, 7, tzinfo=UTC))  # Tuesday, before 00:15
    assert s == {"sessions.sweep": "20261006T0005", "usage.report": None,
                 "digest.rebuild": "2026-10-05"}  # fmt: skip
    s = slots(datetime(2026, 10, 5, 2, 59, tzinfo=UTC))  # Monday before 03:00
    assert s["digest.rebuild"] is None
    assert s["usage.report"] == "2026-10-04"


def test_next_standup() -> None:
    now = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)  # Tuesday 09:00 in New York
    settings = {"standup_time": "09:30", "timezone": "America/New_York"}
    assert next_standup(now, settings) == datetime(2026, 10, 6, 13, 30, tzinfo=UTC)
    friday_late = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)  # after Friday's: next is Saturday
    assert next_standup(friday_late, settings) is None
    assert next_standup(now, {}) is None
    assert next_standup(now, {"standup_time": "9am"}) is None
    assert next_standup(now, {"standup_time": "09:30", "timezone": "Mars/Base"}) is None


@pytest.mark.stack
async def test_tick_sends_each_slot_once(stack: Stack) -> None:
    uid = await stack.new_user()
    await stack.admin.execute(
        "update app.users set settings = $2::jsonb where id = $1",
        uid,
        '{"standup_time": "09:30", "timezone": "America/New_York"}',
    )
    queue = MemoryJobQueue()
    scheduler = Scheduler(stack.ctx.system_pool, stack.ctx.valkey, queue)
    now = datetime(2031, 3, 4, 14, 10, tzinfo=UTC)  # a Tuesday, 09:10 in New York (EST)
    await stack.ctx.valkey.delete(
        *[k async for k in stack.ctx.valkey.scan_iter(match="sched:*")] or ["x"]
    )
    sent = await scheduler.tick(now)
    assert "sessions.sweep:20310304T1410" in sent
    assert "usage.report:2031-03-03" in sent
    assert f"digest.rebuild:{uid}:" in " ".join(sent)
    standup = [m for m in queue.sent if m["type"] == "pack.build" and m["user_id"] == str(uid)]
    assert len(standup) == 1
    assert standup[0]["payload"]["mode"] == "standup"
    assert standup[0]["payload"]["due_at"] == "2031-03-04T14:30:00Z"
    assert await scheduler.tick(now) == []  # nothing twice in the same slots


@pytest.mark.stack
async def test_standup_time_builds_each_current_companys_pack(stack: Stack) -> None:
    uid = await stack.new_user()
    await stack.admin.execute(
        "update app.users set settings = $2::jsonb where id = $1",
        uid,
        '{"standup_time": "09:30", "timezone": "America/New_York"}',
    )
    now_id, past_id = uuid7(), uuid7()
    await stack.admin.execute(
        "insert into app.employers (id, user_id, name) values ($1, $3, 'Now Co'),"
        " ($2, $3, 'Past Co')",
        now_id,
        past_id,
        uid,
    )
    await stack.admin.execute(
        "update app.employers set ended_on = '2030-01-01' where id = $1", past_id
    )
    queue = MemoryJobQueue()
    scheduler = Scheduler(stack.ctx.system_pool, stack.ctx.valkey, queue)
    now = datetime(2031, 3, 5, 14, 10, tzinfo=UTC)  # a Wednesday, 09:10 in New York
    await stack.ctx.valkey.delete(
        *[k async for k in stack.ctx.valkey.scan_iter(match="sched:*")] or ["x"]
    )
    await scheduler.tick(now)
    company = [
        m for m in queue.sent
        if m["type"] == "pack.build" and m["payload"]["context"].startswith("employer:")
        and m["user_id"] == str(uid)
    ]  # fmt: skip
    assert [m["payload"]["context"] for m in company] == [f"employer:{now_id}"]
    assert company[0]["payload"]["day"] == "2031-03-05"
    assert company[0]["payload"]["mode"] == "standup"


@pytest.mark.stack
async def test_failed_and_stuck_work_logs_are_retried_hourly(stack: Stack) -> None:
    uid = await stack.new_user()
    eid = uuid7()
    await stack.admin.execute(
        "insert into app.employers (id, user_id, name) values ($1, $2, 'Retry Co')", eid, uid
    )
    now = datetime(2031, 3, 6, 14, 10, tzinfo=UTC)
    logs = {}
    for day, status, age_h in (
        ("2031-03-01", "failed", 2),  # failed: retried
        ("2031-03-02", "extracting", 3),  # stuck for hours: retried
        ("2031-03-03", "extracting", 0.2),  # still running: left alone
        ("2031-03-04", "extracted", 2),  # done: left alone
        ("2031-02-20", "failed", 24 * 9),  # failed long ago: given up
    ):
        log_id = uuid7()
        logs[day] = log_id
        await stack.admin.execute(
            "insert into app.work_logs (id, user_id, employer_id, day, text_enc, key_version,"
            " content_sha256, status, updated_at) values ($1, $2, $3, $4, $5, 1, $6, $7, $8)",
            log_id, uid, eid, date.fromisoformat(day), b"\x00", f"sha-{day}", status,
            now - timedelta(hours=age_h),
        )  # fmt: skip
    queue = MemoryJobQueue()
    scheduler = Scheduler(stack.ctx.system_pool, stack.ctx.valkey, queue)
    await stack.ctx.valkey.delete(
        *[k async for k in stack.ctx.valkey.scan_iter(match="sched:*")] or ["x"]
    )
    await scheduler.tick(now)
    retried = {m["payload"]["work_log_id"] for m in queue.sent if m["type"] == "worklog.extract"}
    assert retried == {str(logs["2031-03-01"]), str(logs["2031-03-02"])}
    queue.sent.clear()
    await scheduler.tick(now)  # the same hour: nothing again
    assert not [m for m in queue.sent if m["type"] == "worklog.extract"]
