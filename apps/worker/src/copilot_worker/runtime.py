"""The job consumer (ARCHITECTURE.md, "Workers" and "SQS job catalog").

One task long-polls the `jobs` queue and runs up to `concurrency` jobs at once. For each
message:
1. The envelope is parsed; its type picks a handler and a visibility timeout from the catalog.
2. The idempotency key is claimed in `app.job_runs`. A key already `done` deletes the message
   and stops. A key `running` elsewhere, and started within its visibility timeout, is left
   alone: SQS delivered it twice, and the other run finishes it.
3. While the handler runs, a heartbeat extends the message's visibility every minute.
4. Success marks the key `done` and deletes the message. An error marks it `failed` and leaves
   the message to come back after its visibility timeout (the dead-letter queue takes it after
   five receives). `RetryLater` does the same with a shorter delay.

SIGTERM (two minutes before a Spot reclaim) stops receiving, lets running jobs finish within
`shutdown_grace_s`, then cancels the rest: their keys go back to `failed` and their messages
become visible at once, for another task to pick up.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import asyncpg

from copilot_core import logs
from copilot_core.aws import boto3_client
from copilot_core.db import system_tx

log = logging.getLogger(__name__)

# Visibility timeout per job type, in seconds (the catalog's column).
VISIBILITY: dict[str, int] = {
    "session.flush": 120,
    "session.ended": 300,
    "me_audio.transcribe": 900,
    "session.postcall": 900,
    "memory.extract": 900,
    "pack.build": 600,
    "digest.rebuild": 600,
    "usage.report": 900,
    "user.delete": 1800,
    "sessions.sweep": 120,
    "worklog.extract": 900,
    "company_doc.extract": 900,
    "workplace.refresh": 600,
    "employer.purge": 1800,
}


class RetryLater(Exception):
    """The job cannot run yet (for example, it waits on another job); try again later."""

    def __init__(self, delay_s: int, reason: str) -> None:
        super().__init__(reason)
        self.delay_s = delay_s


@dataclass
class Job:
    type: str
    idempotency_key: str
    user_id: UUID | None
    payload: dict[str, Any]
    enqueued_at: datetime
    receive_count: int = 1

    @classmethod
    def parse(cls, body: str, receive_count: int = 1) -> "Job":
        data = json.loads(body)
        if data.get("version") != 1:
            raise ValueError(f"unsupported envelope version {data.get('version')!r}")
        enqueued = data.get("enqueued_at")
        return cls(
            type=str(data["type"]),
            idempotency_key=str(data["idempotency_key"]),
            user_id=UUID(data["user_id"]) if data.get("user_id") else None,
            payload=dict(data.get("payload") or {}),
            enqueued_at=datetime.fromisoformat(enqueued) if enqueued else datetime.now(UTC),
            receive_count=receive_count,
        )

    @property
    def uid(self) -> UUID:
        if self.user_id is None:
            raise ValueError(f"{self.type} needs a user id")
        return self.user_id

    def age_s(self) -> float:
        return (datetime.now(UTC) - self.enqueued_at).total_seconds()


type Handler = Callable[[Job, Any], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def handler(job_type: str) -> Callable[[Handler], Handler]:
    if job_type not in VISIBILITY:
        raise ValueError(f"{job_type} is not in the job catalog")

    def register(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return register


class JobRuns:
    """`app.job_runs`: one row per idempotency key (a system table, no row-level security)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def claim(self, job: Job, visibility_s: int) -> str:
        """'run' when this worker should run the job, 'done' or 'busy' otherwise."""
        async with system_tx(self.pool) as conn:
            attempts = await conn.fetchval(
                """
                insert into app.job_runs (idempotency_key, job_type, user_id, status)
                values ($1, $2, $3, 'running')
                on conflict (idempotency_key) do update
                  set status = 'running', attempts = app.job_runs.attempts + 1,
                      started_at = now(), finished_at = null, error = null
                  where app.job_runs.status = 'failed'
                     or (app.job_runs.status = 'running'
                         and app.job_runs.started_at < now() - make_interval(secs => $4))
                returning attempts
                """,
                job.idempotency_key,
                job.type,
                job.user_id,
                float(visibility_s),
            )
            if attempts is not None:
                return "run"
            status = await conn.fetchval(
                "select status from app.job_runs where idempotency_key = $1",
                job.idempotency_key,
            )
        return "done" if status == "done" else "busy"

    async def finish(self, key: str) -> None:
        async with system_tx(self.pool) as conn:
            await conn.execute(
                "update app.job_runs set status = 'done', finished_at = now(), error = null"
                " where idempotency_key = $1",
                key,
            )

    async def fail(self, key: str, error: str) -> None:
        async with system_tx(self.pool) as conn:
            await conn.execute(
                "update app.job_runs set status = 'failed', finished_at = now(), error = $2"
                " where idempotency_key = $1",
                key,
                error[:2000],
            )


@dataclass
class Message:
    body: str
    receipt: str
    receive_count: int = 1


class SqsQueue:
    """Receive, delete and visibility calls on the `jobs` queue."""

    def __init__(
        self, queue_url: str, region: str, endpoint_url: str | None = None, client: Any = None
    ) -> None:
        self.queue_url = queue_url
        self.client: Any = client or boto3_client(
            "sqs", region_name=region, endpoint_url=endpoint_url
        )

    async def receive(self, max_messages: int, wait_s: int) -> list[Message]:
        response = await asyncio.to_thread(
            self.client.receive_message,
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max(1, min(10, max_messages)),
            WaitTimeSeconds=wait_s,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        out: list[Message] = []
        for m in cast(list[dict[str, Any]], response.get("Messages", [])):
            count = int(m.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
            out.append(Message(m["Body"], m["ReceiptHandle"], count))
        return out

    async def delete(self, receipt: str) -> None:
        await asyncio.to_thread(
            self.client.delete_message, QueueUrl=self.queue_url, ReceiptHandle=receipt
        )

    async def set_visibility(self, receipt: str, seconds: int) -> None:
        await asyncio.to_thread(
            self.client.change_message_visibility,
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt,
            VisibilityTimeout=seconds,
        )


@dataclass
class Consumer:
    queue: SqsQueue
    runs: JobRuns
    ctx: Any
    concurrency: int = 10
    receive_wait_s: int = 20
    heartbeat_s: float = 60.0
    shutdown_grace_s: float = 100.0
    handlers: Mapping[str, Handler] = field(default_factory=lambda: HANDLERS)
    running: set["asyncio.Task[None]"] = field(default_factory=set["asyncio.Task[None]"])
    processed: int = 0

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            free = self.concurrency - len(self.running)
            if free <= 0:
                await asyncio.wait(self.running, return_when=asyncio.FIRST_COMPLETED)
                continue
            receive = asyncio.ensure_future(self.queue.receive(free, self.receive_wait_s))
            stopped = asyncio.ensure_future(stop.wait())
            await asyncio.wait({receive, stopped}, return_when=asyncio.FIRST_COMPLETED)
            stopped.cancel()
            if not receive.done():
                # Stopping mid-poll. The poll runs in a thread and cannot be cancelled, so let
                # it finish (at most receive_wait_s) and hand back what it got at once, instead
                # of leaving those messages invisible until their timeout.
                with contextlib.suppress(Exception):
                    for m in await receive:
                        await self.queue.set_visibility(m.receipt, 0)
                break
            try:
                messages = receive.result()
            except Exception as e:
                log.warning("receive failed: %s", e)
                await asyncio.sleep(1)
                continue
            for m in messages:
                task = asyncio.create_task(self.process(m))
                self.running.add(task)
                task.add_done_callback(self.running.discard)
        await self.drain()

    async def drain(self) -> None:
        if not self.running:
            return
        log.info(
            "stopping: waiting up to %.0fs for %d jobs", self.shutdown_grace_s, len(self.running)
        )
        _, pending = await asyncio.wait(set(self.running), timeout=self.shutdown_grace_s)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def process(self, message: Message) -> None:
        try:
            job = Job.parse(message.body, message.receive_count)
        except Exception as e:
            # Left on the queue; the dead-letter queue takes it after five receives.
            log.error("unreadable job message (%s): %.200s", e, message.body)
            return
        # Every line about this job names it, its user and its session (CloudWatch filters on
        # session_id). process() runs as its own task, so the ids stay with this job.
        logs.set_ids(job=job.type, user_id=job.user_id, session_id=job.payload.get("session_id"))
        fn = self.handlers.get(job.type)
        if fn is None:
            log.error("no handler for job type %s (%s)", job.type, job.idempotency_key)
            return
        visibility = VISIBILITY[job.type]
        try:
            await self.queue.set_visibility(message.receipt, visibility)
            claim = await self.runs.claim(job, visibility)
        except Exception as e:
            log.warning("%s: could not claim (%s); it comes back later", job.idempotency_key, e)
            return
        if claim == "done":
            log.info("%s already done; deleting the duplicate", job.idempotency_key)
            await self.queue.delete(message.receipt)
            return
        if claim == "busy":
            log.info("%s is running elsewhere; leaving this copy", job.idempotency_key)
            return
        beat = asyncio.create_task(self._heartbeat(message.receipt, visibility))
        try:
            await fn(job, self.ctx)
        except RetryLater as e:
            log.info("%s: retry in %ss (%s)", job.idempotency_key, e.delay_s, e)
            await self._settle_failed(job, f"retry later: {e}", message.receipt, e.delay_s)
            return
        except asyncio.CancelledError:
            await asyncio.shield(self._settle_failed(job, "cancelled", message.receipt, 0))
            raise
        except Exception as e:
            log.exception("%s failed", job.idempotency_key)
            await self._settle_failed(job, f"{type(e).__name__}: {e}", message.receipt, None)
            return
        finally:
            beat.cancel()
        await self.runs.finish(job.idempotency_key)
        await self.queue.delete(message.receipt)
        self.processed += 1
        log.info("%s done", job.idempotency_key)

    async def _settle_failed(
        self, job: Job, error: str, receipt: str, visible_in_s: int | None
    ) -> None:
        with contextlib.suppress(Exception):
            await self.runs.fail(job.idempotency_key, error)
        if visible_in_s is not None:
            with contextlib.suppress(Exception):
                await self.queue.set_visibility(receipt, visible_in_s)

    async def _heartbeat(self, receipt: str, visibility: int) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await self.queue.set_visibility(receipt, visibility)
            except Exception as e:
                log.warning("visibility extension failed: %s", e)
