from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import fakeredis.aioredis
import pytest

from copilot_core import jobs
from copilot_core import pack_operations as operations


async def test_operation_lifecycle_expiry_and_old_completion_cannot_finish_new_build():
    valkey = fakeredis.aioredis.FakeRedis()
    uid = uuid4()
    queue = jobs.MemoryJobQueue()
    observed = operations.ObservedQueue(queue, valkey)
    first = jobs.pack_build(uid, "interview", "job:a", datetime.now(UTC), "first")
    second = jobs.pack_build(uid, "interview", "job:a", datetime.now(UTC), "second")
    await observed.send(first)
    assert await operations.state(valkey, uid, "job:a") == "queued"
    await operations.started(valkey, uid, "job:a", first["idempotency_key"])
    assert await operations.state(valkey, uid, "job:a") == "running"
    await observed.send(second)
    await operations.finished(valkey, uid, first["idempotency_key"], True)
    assert await operations.state(valkey, uid, "job:a") == "queued"
    assert await operations.states(valkey, uid, ["job:a", "job:missing"]) == {
        "job:a": "queued", "job:missing": "idle"
    }
    await operations.started(valkey, uid, "job:a", second["idempotency_key"])
    await operations.finished(valkey, uid, second["idempotency_key"], False)
    assert await operations.state(valkey, uid, "job:a") == "failed"
    await valkey.delete(operations.operation(uid, second["idempotency_key"]))
    assert await operations.state(valkey, uid, "job:a") == "idle"
    # A lost operation is bounded independently of whether a pack was ever created.
    assert len(queue.sent) == 2
    await valkey.aclose()


async def test_send_failure_and_unobserved_gateway_build():
    valkey = fakeredis.aioredis.FakeRedis()
    uid = uuid4()
    queue = AsyncMock()
    queue.send.side_effect = RuntimeError("queue unavailable")
    observed = operations.ObservedQueue(queue, valkey)
    first = jobs.pack_build(uid, "interview", "job:a", datetime.now(UTC), "first")
    with pytest.raises(RuntimeError, match="queue unavailable"):
        await observed.send(first)
    assert await operations.state(valkey, uid, "job:a") == "failed"
    queue.send.side_effect = None
    await observed.send(first)
    assert await operations.state(valkey, uid, "job:a") == "queued"
    await operations.finished(valkey, uid, first["idempotency_key"], False)
    await operations.started(valkey, uid, "job:a", "unobserved-next-build")
    assert await operations.state(valkey, uid, "job:a") == "running"
    assert (
        0
        < await valkey.ttl(operations.operation(uid, "unobserved-next-build"))
        <= operations.ACTIVE_TTL_S
    )
    await valkey.aclose()
