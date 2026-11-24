"""Durable operation acceptance, worker fencing, and an at-least-once outbox.

Domain changes and ``enqueue`` share a short transaction. The queue is transport,
not proof of acceptance. Model side effects can still repeat after a process crash.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg

from copilot_core.db import Conn
from copilot_core.jobs import JobQueue

log = logging.getLogger(__name__)
type Lane = Literal["persistence", "interactive", "ingestion", "maintenance"]
PERSISTENCE_TYPES = frozenset({"session.flush", "session.ended", "me_audio.transcribe"})
MAINTENANCE_TYPES = frozenset({"usage.report", "user.delete", "sessions.sweep", "employer.purge"})


class LeaseLost(RuntimeError):
    """This generation no longer has permission to commit work."""


@dataclass(frozen=True)
class Lease:
    key: str
    owner: UUID
    generation: int
    user_id: UUID | None
    input_revision: str | None = None

    @property
    def idempotency_key(self) -> str:
        return self.key


current_lease: ContextVar[Lease | None] = ContextVar("operation_lease", default=None)


def lane_for(job_type: str) -> Lane:
    if job_type in PERSISTENCE_TYPES:
        return "persistence"
    if job_type in {"ask.generate", "practice.generate", "knowledge.ask"}:
        return "interactive"
    return "maintenance" if job_type in MAINTENANCE_TYPES else "ingestion"


def _message(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


def _input(message: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(message.get("payload") or {})
    if message.get("type") == "pack.build":
        # A repeated demand for the same content version can refresh its due hint;
        # the accepted immutable operation still uses the first delivery's hint.
        payload.pop("due_at", None)
    return payload


def _lease(row: Any) -> Lease:
    return Lease(
        row["idempotency_key"],
        row["lease_owner"],
        row["lease_generation"],
        row["user_id"],
        row["input_revision"],
    )


async def enqueue(
    conn: Conn,
    message: Mapping[str, Any],
    *,
    lane: Lane | None = None,
    account_id: UUID | None = None,
    input_revision: str | None = None,
    available_at: datetime | None = None,
    transport: Literal["queue", "local"] = "queue",
) -> UUID:
    """Accept once inside the domain transaction; conflicting key reuse is an error."""
    key, kind = str(message["idempotency_key"]), str(message["type"])
    uid = UUID(str(message["user_id"])) if message.get("user_id") else None
    due = available_at or datetime.now(UTC)
    chosen_lane = lane or lane_for(kind)
    if transport == "local" and (
        chosen_lane != "interactive" or kind not in {"ask.generate", "practice.generate"}
    ):
        raise ValueError("local transport only supports interactive generation")
    workspace_id = uid
    if transport == "local" or kind == "user.delete":
        if uid is None or (account_id is not None and account_id != uid):
            raise ValueError("private operation must belong to its account")
        account_id = uid
        payload = _input(message)
        workspace_id = (
            UUID(str(payload.get("workspace_id") or uid)) if transport == "local" else None
        )
    row = await conn.fetchrow(
        "insert into app.operations"
        " (idempotency_key,job_type,user_id,workspace_id,account_id,status,lane,transport,"
        " envelope,input_revision,available_at)"
        " values ($1,$2,$3,$4,$5,'queued',$6,$7,$8,$9,$10)"
        " on conflict (idempotency_key) do nothing returning id",
        key,
        kind,
        uid,
        workspace_id,
        account_id,
        chosen_lane,
        transport,
        dict(message),
        input_revision,
        due,
    )
    if row is None:
        old = await conn.fetchrow(
            "select * from app.operations where idempotency_key=$1 for update", key
        )
        if old is None:
            raise ValueError("operation idempotency key is unavailable")
        previous = _message(old["envelope"]) if old["envelope"] else None
        if (
            old["job_type"] != kind
            or old["user_id"] != uid
            or (previous is not None and _input(previous) != _input(message))
            or old["input_revision"] != input_revision
            or old["transport"] != transport
        ):
            raise ValueError("idempotency key reused for different operation input")
        if previous is None:
            await conn.execute(
                "update app.operations set envelope=$2 where idempotency_key=$1",
                key,
                dict(message),
            )
        if old["status"] == "failed":
            await conn.execute(
                "update app.operations set status='queued',available_at=$2,finished_at=null"
                " where idempotency_key=$1",
                key,
                due,
            )
            if transport == "queue":
                await conn.execute(
                    "insert into app.outbox_events(operation_key,user_id,message,available_at)"
                    " values ($1,$2,$3,$4) on conflict(operation_key) do update set"
                    " published_at=null,available_at=excluded.available_at,lease_owner=null,"
                    " lease_expires_at=null,last_error=null",
                    key,
                    uid,
                    previous or dict(message),
                    due,
                )
        return old["id"]
    if transport == "queue":
        await conn.execute(
            "insert into app.outbox_events(operation_key,user_id,message,available_at)"
            " values ($1,$2,$3,$4)",
            key,
            uid,
            dict(message),
            due,
        )
    return row["id"]


async def adopt(conn: Conn, message: Mapping[str, Any]) -> None:
    """Accept an older queue producer without creating a second outbox delivery."""
    uid = UUID(str(message["user_id"])) if message.get("user_id") else None
    await conn.execute(
        "insert into app.operations"
        " (idempotency_key,job_type,user_id,workspace_id,account_id,status,lane,envelope)"
        " values ($1,$2,$3,$4,$5,'queued',$6,$7) on conflict(idempotency_key) do nothing",
        str(message["idempotency_key"]),
        str(message["type"]),
        uid,
        None if message["type"] == "user.delete" else uid,
        uid if message["type"] == "user.delete" else None,
        lane_for(str(message["type"])),
        dict(message),
    )
    row = await conn.fetchrow(
        "select job_type,user_id,envelope,transport from app.operations"
        " where idempotency_key=$1 for update",
        str(message["idempotency_key"]),
    )
    previous = _message(row["envelope"]) if row and row["envelope"] else None
    if (
        row is None
        or row["job_type"] != str(message["type"])
        or row["user_id"] != uid
        or row["transport"] != "queue"
        or (previous is not None and _input(previous) != _input(message))
    ):
        raise ValueError("delivery conflicts with accepted operation input")
    if previous is None:
        await conn.execute(
            "update app.operations set envelope=$2 where idempotency_key=$1",
            str(message["idempotency_key"]),
            dict(message),
        )


async def claim(
    conn: Conn, key: str, owner: UUID, lease_s: float
) -> Lease | Literal["done", "busy", "dead"]:
    if not 0 < lease_s <= 3600:
        raise ValueError("invalid lease duration")
    row = await conn.fetchrow(
        "update app.operations set status='running',attempts=attempts+1,"
        " started_at=clock_timestamp(),finished_at=null,error=null,lease_owner=$2,"
        " lease_generation=lease_generation+1,"
        " lease_expires_at=clock_timestamp()+make_interval(secs=>$3)"
        " where idempotency_key=$1 and available_at<=clock_timestamp()"
        " and (status in ('queued','failed') or (status='running' and"
        " (lease_expires_at is null or lease_expires_at<=clock_timestamp()))) returning *",
        key,
        owner,
        float(lease_s),
    )
    if row is not None:
        return _lease(row)
    status = await conn.fetchval("select status from app.operations where idempotency_key=$1", key)
    if status == "dead":
        return "dead"
    return "done" if status in ("done", "cancelled") else "busy"


async def claim_next(
    conn: Conn, owner: UUID, lease_s: float, *, lane: Literal["interactive"] = "interactive"
) -> tuple[dict[str, Any], Lease] | None:
    row = await conn.fetchrow(
        "select * from app.claim_next_interactive($1,$2)", owner, float(lease_s)
    )
    return (_message(row["envelope"]), _lease(row)) if row is not None else None


async def guard(conn: Conn, lease: Lease) -> None:
    if not await conn.fetchval(
        "select app.guard_operation($1,$2,$3)", lease.key, lease.owner, lease.generation
    ):
        raise LeaseLost(lease.key)


async def transaction_guard(conn: Conn) -> None:
    if (lease := current_lease.get()) is not None:
        await guard(conn, lease)


async def renew(conn: Conn, lease: Lease, lease_s: float) -> bool:
    return bool(
        await conn.fetchval(
            "select app.renew_operation($1,$2,$3,$4)",
            lease.key,
            lease.owner,
            lease.generation,
            float(lease_s),
        )
    )


async def finish(conn: Conn, lease: Lease) -> bool:
    return bool(
        await conn.fetchval(
            "select app.settle_operation($1,$2,$3,'done',null,0)",
            lease.key,
            lease.owner,
            lease.generation,
        )
    )


async def fail(conn: Conn, lease: Lease, error: str, delay_s: float = 0) -> bool:
    return bool(
        await conn.fetchval(
            "select app.settle_operation($1,$2,$3,'failed',$4,$5)",
            lease.key,
            lease.owner,
            lease.generation,
            error,
            float(delay_s),
        )
    )


async def cancel(conn: Conn, key: str) -> bool:
    return bool(
        await conn.fetchval(
            "update app.operations set status='cancelled',finished_at=clock_timestamp(),"
            " lease_generation=lease_generation+1,lease_expires_at=null"
            " where idempotency_key=$1 and status not in ('done','cancelled') returning true",
            key,
        )
    )


async def retry(conn: Conn, key: str) -> bool:
    """Explicitly retry a failed/dead operation's unchanged accepted input."""
    row = await conn.fetchrow(
        "update app.operations set status='queued',attempts=0,error=null,finished_at=null,"
        " available_at=clock_timestamp(),lease_owner=null,lease_expires_at=null,"
        " lease_generation=lease_generation+1 where idempotency_key=$1"
        " and status in ('failed','dead') and envelope is not null returning *",
        key,
    )
    if row is None:
        return False
    if row["transport"] == "queue":
        await conn.execute(
            "insert into app.outbox_events(operation_key,user_id,message) values($1,$2,$3)"
            " on conflict(operation_key) do update set published_at=null,"
            " available_at=clock_timestamp(),lease_owner=null,lease_expires_at=null,"
            " last_error=null",
            key,
            row["user_id"],
            _message(row["envelope"]),
        )
    return True


async def cancel_scope(conn: Conn, user_id: UUID, *, except_key: str | None = None) -> int:
    """Revoke queued/running work before deleting a scope, retaining dedupe metadata."""
    rows = await conn.fetch(
        "update app.operations set status='cancelled',finished_at=clock_timestamp(),"
        " lease_generation=lease_generation+1,lease_expires_at=null"
        " where user_id=$1 and ($2::text is null or idempotency_key<>$2)"
        " and status not in ('done','cancelled') returning id",
        user_id,
        except_key,
    )
    return len(rows)


async def purge_terminal_flushes(conn: Conn, *, limit: int = 1000) -> int:
    """Keep 30 days of high-volume flush dedupe; durable interval identities remain.

    This exceeds the configured 4-day queue and 14-day dead-letter retention. A
    manual replay beyond that horizon still meets session/log and usage dedupe.
    """
    if not 1 <= limit <= 10000:
        raise ValueError("invalid operation cleanup batch")
    rows = await conn.fetch(
        "with expired as (select idempotency_key from app.operations"
        " where job_type='session.flush' and status in ('done','cancelled')"
        " and finished_at<clock_timestamp()-interval '30 days'"
        " order by finished_at,idempotency_key for update skip locked limit $1)"
        " delete from app.operations o using expired e"
        " where o.idempotency_key=e.idempotency_key returning o.id",
        limit,
    )
    return len(rows)


class OutboxDispatcher:
    """Bounded publication outside database transactions; duplicate delivery is safe."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        queue: JobQueue,
        *,
        batch_size: int = 25,
        lease_s: float = 30,
        recovery_s: float = 120,
        publish_concurrency: int = 8,
        publish_timeout_s: float = 10,
    ) -> None:
        if not 1 <= batch_size <= 100 or not 1 <= publish_concurrency <= 32:
            raise ValueError("invalid outbox publication bounds")
        if not 0 < publish_timeout_s <= 60 or lease_s <= 0 or recovery_s <= 0:
            raise ValueError("invalid outbox publication timing")
        self.pool, self.queue = pool, queue
        self.batch_size, self.recovery_s = batch_size, recovery_s
        self.publish_concurrency, self.publish_timeout_s = publish_concurrency, publish_timeout_s
        # The last item in a bounded batch still owns its publication reservation.
        waves = (batch_size + publish_concurrency - 1) // publish_concurrency
        self.lease_s = max(lease_s, waves * publish_timeout_s + 5)
        self.owner = uuid4()

    async def recover(self) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "with stale as (select e.id from app.outbox_events e join app.operations o"
                " on o.idempotency_key=e.operation_key where e.published_at<"
                " clock_timestamp()-make_interval(secs=>$1) and o.available_at<=clock_timestamp()"
                " and (o.status in ('queued','failed') or (o.status='running' and"
                " o.lease_expires_at<=clock_timestamp())) order by e.published_at"
                " for update of e skip locked limit $2)"
                " update app.outbox_events e set published_at=null,available_at=clock_timestamp(),"
                " lease_owner=null,lease_expires_at=null from stale"
                " where e.id=stale.id returning e.id",
                float(self.recovery_s),
                self.batch_size,
            )
        return len(rows)

    async def once(self) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "with pending as (select e.id from app.outbox_events e join app.operations o"
                " on o.idempotency_key=e.operation_key where e.published_at is null"
                " and e.available_at<=clock_timestamp()"
                " and o.status not in ('done','cancelled','dead')"
                " and (e.lease_expires_at is null or e.lease_expires_at<=clock_timestamp())"
                " order by e.available_at,e.created_at for update of e skip locked limit $1)"
                " update app.outbox_events e set lease_owner=$2,"
                " lease_expires_at=clock_timestamp()+make_interval(secs=>$3),attempts=e.attempts+1"
                " from pending where e.id=pending.id returning e.*",
                self.batch_size,
                self.owner,
                float(self.lease_s),
            )
        budget = asyncio.Semaphore(self.publish_concurrency)

        async def publish(row: Any) -> bool:
            async with budget:
                return await self._publish(row)

        return sum(await asyncio.gather(*(publish(row) for row in rows)))

    async def _publish(self, row: Any) -> bool:
        try:
            message = _message(row["message"])
            message["event_id"] = str(row["id"])
            async with asyncio.timeout(self.publish_timeout_s):
                await self.queue.send(message)
        except Exception as exc:
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "update app.outbox_events set lease_owner=null,lease_expires_at=null,"
                    " available_at=clock_timestamp()+make_interval(secs=>$3),last_error=$4"
                    " where id=$1 and lease_owner=$2",
                    row["id"],
                    self.owner,
                    float(min(60, 2 ** min(int(row["attempts"]), 6))),
                    f"{type(exc).__name__}: {exc}"[:2000],
                )
            log.warning("outbox publication deferred: %s", row["id"])
            return False
        else:
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute(
                    "update app.outbox_events set published_at=clock_timestamp(),"
                    " lease_owner=null,lease_expires_at=null,last_error=null"
                    " where id=$1 and lease_owner=$2",
                    row["id"],
                    self.owner,
                )
            return True

    async def run(self, stop: asyncio.Event, interval_s: float = 1) -> None:
        while not stop.is_set():
            try:
                await self.recover()
                await self.once()
            except Exception:
                log.exception("outbox dispatch failed; accepted operations remain durable")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
