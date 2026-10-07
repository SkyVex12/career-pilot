"""The consumer against ElasticMQ and the job_runs table: duplicates, failures, retries,
visibility and SIGTERM. Then every job type in the catalog delivered twice."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import boto3
import pytest
import pytest_asyncio

from copilot_core import jobs
from copilot_core.teststack import SQS_ENDPOINT
from copilot_worker.runtime import (
    HANDLERS,
    VISIBILITY,
    Consumer,
    Job,
    JobRuns,
    Message,
    RetryLater,
    SqsQueue,
)
from copilot_worker.testing import Stack

pytestmark = pytest.mark.stack


@pytest_asyncio.fixture(loop_scope="session")
async def queue() -> AsyncIterator[SqsQueue]:
    client = boto3.client(
        "sqs", endpoint_url=SQS_ENDPOINT, region_name="us-east-1",
        aws_access_key_id="local", aws_secret_access_key="local",
    )  # fmt: skip
    url = client.create_queue(QueueName=f"jobs-test-{uuid.uuid4().hex[:10]}")["QueueUrl"]
    yield SqsQueue(url, "us-east-1", client=client)
    client.delete_queue(QueueUrl=url)


def message(job_type: str, key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return jobs.envelope(job_type, key, uuid.uuid4(), payload or {})


async def send(q: SqsQueue, m: dict[str, Any]) -> None:
    await jobs.SqsJobQueue(q.queue_url, "us-east-1", client=q.client).send(m)


async def run_until(consumer: Consumer, cond: Any, within_s: float = 10) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run(stop))
    try:
        async with asyncio.timeout(within_s):
            while not cond():  # noqa: ASYNC110 - polls a plain predicate
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await task


def consumer(stack: Stack, q: SqsQueue, handlers: dict[str, Any], **kw: Any) -> Consumer:
    return Consumer(
        q, JobRuns(stack.ctx.pool), stack.ctx, receive_wait_s=1, handlers=handlers, **kw
    )


async def queue_empty(q: SqsQueue) -> bool:
    attrs = await asyncio.to_thread(
        q.client.get_queue_attributes, QueueUrl=q.queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )  # fmt: skip
    a = attrs["Attributes"]
    return (
        a["ApproximateNumberOfMessages"] == "0"
        and a["ApproximateNumberOfMessagesNotVisible"] == "0"
    )


async def status(stack: Stack, key: str) -> tuple[str, int] | None:
    row = await stack.admin.fetchrow(
        "select status, attempts from app.job_runs where idempotency_key = $1", key
    )
    return None if row is None else (row["status"], row["attempts"])


async def test_duplicate_delivery_runs_once(stack: Stack, queue: SqsQueue) -> None:
    runs: list[str] = []

    async def handle(job: Job, ctx: Any) -> None:
        runs.append(job.idempotency_key)

    key = f"pack.build:dup:{uuid.uuid4()}"
    m = message("pack.build", key)
    await send(queue, m)
    await send(queue, m)  # SQS may deliver a message twice; producers may send twice too
    c = consumer(stack, queue, {"pack.build": handle})
    await run_until(c, lambda: len(runs) >= 1 and c.processed >= 1)
    for _ in range(40):
        if await queue_empty(queue):
            break
        await run_until(c, lambda: True, within_s=2)
    assert runs == [key]
    assert await status(stack, key) == ("done", 1)
    assert await queue_empty(queue)


async def test_failure_marks_failed_and_retry_later_comes_back(
    stack: Stack, queue: SqsQueue
) -> None:
    attempts: list[int] = []

    async def flaky(job: Job, ctx: Any) -> None:
        attempts.append(job.receive_count)
        if len(attempts) == 1:
            raise RetryLater(1, "not yet")

    key = f"me_audio.transcribe:{uuid.uuid4()}"
    await send(queue, message("me_audio.transcribe", key))
    c = consumer(stack, queue, {"me_audio.transcribe": flaky})
    await run_until(c, lambda: c.processed >= 1, within_s=15)
    assert attempts == [1, 2]  # the second receive, about a second later
    assert await status(stack, key) == ("done", 2)


async def test_error_leaves_the_message_for_redelivery(stack: Stack, queue: SqsQueue) -> None:
    async def boom(job: Job, ctx: Any) -> None:
        raise RuntimeError("model unavailable")

    key = f"session.postcall:{uuid.uuid4()}"
    receipt = Message(jobs_body(message("session.postcall", key)), "receipt-x")
    calls: list[tuple[str, int]] = []

    class FakeQueue(SqsQueue):
        def __init__(self) -> None:
            pass

        async def set_visibility(self, receipt: str, seconds: int) -> None:
            calls.append(("visibility", seconds))

        async def delete(self, receipt: str) -> None:
            calls.append(("delete", 0))

    c = Consumer(
        FakeQueue(), JobRuns(stack.ctx.pool), stack.ctx, handlers={"session.postcall": boom}
    )
    await c.process(receipt)
    assert calls == [("visibility", VISIBILITY["session.postcall"])]  # never deleted
    row = await stack.admin.fetchrow(
        "select status, error from app.job_runs where idempotency_key = $1", key
    )
    assert row is not None
    assert row["status"] == "failed"
    assert "model unavailable" in row["error"]
    # The redelivery claims the failed key and runs again.
    c.handlers = {"session.postcall": lambda job, ctx: asyncio.sleep(0)}
    await c.process(receipt)
    assert calls[-1] == ("delete", 0)
    assert await status(stack, key) == ("done", 2)


def jobs_body(m: dict[str, Any]) -> str:
    import json

    return json.dumps(m)


async def test_sigterm_drains_running_jobs(stack: Stack, queue: SqsQueue) -> None:
    started = asyncio.Event()
    finished: list[str] = []

    async def slow(job: Job, ctx: Any) -> None:
        started.set()
        await asyncio.sleep(0.5)
        finished.append(job.idempotency_key)

    key = f"digest.rebuild:{uuid.uuid4()}"
    await send(queue, message("digest.rebuild", key))
    c = consumer(stack, queue, {"digest.rebuild": slow}, shutdown_grace_s=5)
    stop = asyncio.Event()
    task = asyncio.create_task(c.run(stop))
    await asyncio.wait_for(started.wait(), 10)
    stop.set()  # what SIGTERM does
    await asyncio.wait_for(task, 10)
    assert finished == [key]
    assert await status(stack, key) == ("done", 1)


async def test_sigterm_past_the_grace_period_returns_the_job(stack: Stack, queue: SqsQueue) -> None:
    started = asyncio.Event()

    async def stuck(job: Job, ctx: Any) -> None:
        started.set()
        await asyncio.sleep(60)

    key = f"memory.extract:{uuid.uuid4()}"
    await send(queue, message("memory.extract", key))
    c = consumer(stack, queue, {"memory.extract": stuck}, shutdown_grace_s=0.2)
    stop = asyncio.Event()
    task = asyncio.create_task(c.run(stop))
    await asyncio.wait_for(started.wait(), 10)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert await status(stack, key) == ("failed", 1)
    # Visible again at once, for another task.
    again = await queue.receive(10, 2)
    assert [jobs_key(m.body) for m in again] == [key]


def jobs_key(body: str) -> str:
    return Job.parse(body).idempotency_key


async def test_visibility_heartbeat_extends_long_jobs(stack: Stack, queue: SqsQueue) -> None:
    beats: list[int] = []
    real = queue.set_visibility

    async def spy(receipt: str, seconds: int) -> None:
        beats.append(seconds)
        await real(receipt, seconds)

    queue.set_visibility = spy  # type: ignore[method-assign]

    async def long(job: Job, ctx: Any) -> None:
        await asyncio.sleep(0.35)

    key = f"session.postcall:{uuid.uuid4()}"
    await send(queue, message("session.postcall", key))
    c = consumer(stack, queue, {"session.postcall": long}, heartbeat_s=0.1)
    await run_until(c, lambda: c.processed >= 1)
    assert len(beats) >= 3  # the initial timeout, then the heartbeats
    assert set(beats) == {VISIBILITY["session.postcall"]}


@pytest.mark.parametrize("job_type", sorted(VISIBILITY))
async def test_every_job_type_delivered_twice_runs_once(stack: Stack, job_type: str) -> None:
    assert job_type in HANDLERS, f"{job_type} has no handler"
    runs: list[str] = []

    async def counting(job: Job, ctx: Any) -> None:
        runs.append(job.type)

    deleted: list[str] = []

    class Q(SqsQueue):
        def __init__(self) -> None:
            pass

        async def set_visibility(self, receipt: str, seconds: int) -> None:
            pass

        async def delete(self, receipt: str) -> None:
            deleted.append(receipt)

    c = Consumer(Q(), JobRuns(stack.ctx.pool), stack.ctx, handlers={job_type: counting})
    m = Message(jobs_body(message(job_type, f"{job_type}:twice:{uuid.uuid4()}")), "r")
    await c.process(m)
    await c.process(m)
    assert runs == [job_type]
    assert deleted == ["r", "r"]  # the duplicate is deleted without running
