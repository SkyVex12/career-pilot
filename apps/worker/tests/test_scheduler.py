"""The local scheduler: cadences, one send per slot, and standup packs 30 minutes ahead."""

from datetime import UTC, datetime

import pytest

from copilot_core.jobs import MemoryJobQueue
from copilot_worker.scheduler import Scheduler, next_standup, slots

from .conftest import Stack


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
