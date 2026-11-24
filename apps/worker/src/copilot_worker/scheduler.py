"""Timed jobs (ARCHITECTURE.md, "Data and storage": EventBridge Scheduler).

Locally this command enqueues them on the documented cadence; in AWS (build step 5) EventBridge
Scheduler runs `copilot-scheduler --once` as a scheduled task every five minutes instead.

    uv run copilot-scheduler            # tick every 60 s until stopped
    uv run copilot-scheduler --once     # one tick, then exit

- sessions.sweep: every 5 minutes; key sessions.sweep:{slot}
- usage.report: daily from 00:15 UTC, for the day before; key usage.report:{day}
- digest.rebuild: weekly from Monday 03:00 UTC, per user; key digest.rebuild:{uid}:{memory}
- knowledge.index: hourly, for company documents with no index of their content (those from
  before engineering context, and failed ones for a week); key knowledge.index:...:{hour}
- pack.build: 30 minutes before each user's weekday standup (settings standup_time and
  timezone); key pack.build:{uid}:standup:{date}:{digest}

Each slot and its operations/outbox events commit together in PostgreSQL. Queue publication
can retry after a scheduler restart; duplicate deliveries use the same operation key.
"""

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from copilot_core import jobs, logs, operations, workplace
from copilot_core.db import create_pool, system_tx
from copilot_core.dotenv import load_dotenv
from copilot_core.valkey_client import Valkey, make_valkey
from copilot_worker.config import WorkerSettings
from copilot_worker.memory import memory_version
from copilot_worker.packs import digest_version_of

log = logging.getLogger(__name__)

STANDUP_LEAD = timedelta(minutes=30)


@dataclass(frozen=True)
class UserRow:
    id: UUID
    settings: Mapping[str, Any]


def next_standup(now: datetime, settings: Mapping[str, Any]) -> datetime | None:
    """Today's standup in UTC if the user set a time (HH:MM, local) and today is a weekday."""
    raw = settings.get("standup_time")
    if not isinstance(raw, str) or ":" not in raw:
        return None
    try:
        tz = ZoneInfo(str(settings.get("timezone") or "UTC"))
        hour, minute = (int(x) for x in raw.split(":", 1))
    except (ZoneInfoNotFoundError, ValueError):
        return None
    local = now.astimezone(tz)
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due < local:
        due += timedelta(days=1)
    if due.weekday() >= 5:
        return None
    return due.astimezone(UTC)


def slots(now: datetime) -> dict[str, str | None]:
    """The current slot of each cadence, or None when it is not due."""
    five = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    daily = yesterday if (now.hour, now.minute) >= (0, 15) else None
    week = now.date() - timedelta(days=now.weekday())
    weekly = week.isoformat() if now.weekday() > 0 or now.hour >= 3 else None
    return {
        "sessions.sweep": five.strftime("%Y%m%dT%H%M"),
        "usage.report": daily,
        "digest.rebuild": weekly,
    }


class Scheduler:
    def __init__(self, pool: asyncpg.Pool, valkey: Valkey, queue: jobs.JobQueue) -> None:
        self.pool = pool
        self.valkey = valkey
        self.queue = queue
        self.dispatcher = operations.OutboxDispatcher(pool, queue)

    async def schedule(self, name: str, slot: str, messages: list[dict[str, Any]]) -> list[str]:
        async with system_tx(self.pool) as conn:
            accepted = await conn.fetchval(
                "insert into app.scheduled_jobs(name,slot) values($1,$2)"
                " on conflict do nothing returning true",
                name,
                slot,
            )
            if not accepted:
                return []
            for message in messages:
                await operations.enqueue(conn, message)
        return [str(m["idempotency_key"]) for m in messages]

    async def retry_work_logs(self, now: datetime) -> list[dict[str, Any]]:
        """Work logs whose extraction failed, or has been running for over an hour, are tried
        again once an hour for a week, so a provider outage heals without the user. A retry
        that fails costs nothing; one that works writes the facts as usual."""
        async with system_tx(self.pool) as conn:
            rows = await conn.fetch(
                "select w.id,w.user_id,w.content_sha256,w.current_revision_id from app.work_logs w"
                " cross join lateral(select coalesce(sum(o.attempts),0) as attempts,"
                " coalesce(bool_or(o.status='dead'),false) as dead,"
                " coalesce(bool_or(o.status in ('queued','running','failed')),false) as pending"
                " from app.operations o"
                " where o.job_type='worklog.extract' and o.user_id=w.user_id"
                " and o.envelope->'payload'->>'work_log_id'=w.id::text"
                " and o.envelope->'payload'->>'work_log_revision_id'=w.current_revision_id::text) a"
                " where (w.status='failed' or (w.status='extracting' and w.updated_at<$1))"
                " and w.updated_at>$2"
                " and w.extracted_revision_id is distinct from w.current_revision_id"
                " and a.attempts<5 and not a.dead and not a.pending",
                now - timedelta(hours=1),
                now - timedelta(days=7),
            )
        messages: list[dict[str, Any]] = []
        for r in rows:
            key = (
                f"worklog.extract:{r['user_id']}:{r['id']}:{r['content_sha256']}:"
                f"r{r['current_revision_id']}:scheduled:{now.strftime('%Y%m%dT%H')}"
            )
            payload = {
                "work_log_id": str(r["id"]),
                "content_sha256": r["content_sha256"],
                "work_log_revision_id": str(r["current_revision_id"]),
            }
            messages.append(jobs.envelope("worklog.extract", key, r["user_id"], payload))
        return messages

    async def index_documents(self, now: datetime) -> list[dict[str, Any]]:
        """Company documents with no index of their content are indexed: those uploaded
        before engineering context (this is their migration: the documents and their facts
        stay, and gain sections and richer facts), and those whose indexing failed or has
        been running for over an hour. Once an hour; a failed one for a week."""
        async with system_tx(self.pool) as conn:
            rows = await conn.fetch(
                "select id, user_id, sha256 from app.company_documents"
                " where work_log_id is null and content_type in ('text/plain', 'text/markdown')"
                " and indexed_sha256 is distinct from sha256"
                " and (status = 'ingested'"
                "      or (status = 'failed' and created_at > $2)"
                "      or (status = 'ingesting' and created_at < $1 and created_at > $2))",
                now - timedelta(hours=1),
                now - timedelta(days=7),
            )
        messages: list[dict[str, Any]] = []
        for r in rows:
            message = jobs.knowledge_index(r["user_id"], r["id"], r["sha256"])
            # A new key each hour: the first try's key is taken once it has run.
            message["idempotency_key"] += f":{now.strftime('%Y%m%dT%H')}"
            messages.append(message)
        return messages

    async def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        sent: list[str] = []
        due = slots(now)
        sweep = due["sessions.sweep"]
        if sweep:
            key = f"sessions.sweep:{sweep}"
            sent += await self.schedule(
                "sessions.sweep", sweep, [jobs.envelope("sessions.sweep", key, None, {})]
            )
        day = due["usage.report"]
        if day:
            key = f"usage.report:{day}"
            sent += await self.schedule(
                "usage.report", day, [jobs.envelope("usage.report", key, None, {"day": day})]
            )
        hour = now.strftime("%Y%m%dT%H")
        sent += await self.schedule("worklog.retry", hour, await self.retry_work_logs(now))
        sent += await self.schedule("knowledge.index", hour, await self.index_documents(now))
        async with system_tx(self.pool) as conn:
            users = [UserRow(r["id"], r["settings"] or {}) for r in await conn.fetch(
                "select id, settings from app.users"
            )]  # fmt: skip
            versions: dict[UUID, str] = {}
            week = due["digest.rebuild"]
            if week:
                for u in users:
                    versions[u.id] = await memory_version(conn, u.id)
        if week:
            sent += await self.schedule(
                "digest.rebuild",
                week,
                [
                    jobs.envelope("digest.rebuild", f"digest.rebuild:{uid}:{version}", uid, {})
                    for uid, version in versions.items()
                ],
            )
        for u in users:
            standup = next_standup(now, u.settings)
            if standup is None or not (standup - STANDUP_LEAD <= now < standup):
                continue
            slot = f"{u.id}:{standup.strftime('%Y%m%dT%H%M')}"
            digest = await digest_version_of(self.valkey, u.id)
            message = jobs.pack_build(
                u.id, "standup", f"standup:{standup.date().isoformat()}", standup, digest
            )
            messages = [message]
            # Each company the user works at now gets its standup pack for that day too.
            day = workplace.local_today(u.settings, standup).isoformat()
            async with system_tx(self.pool) as conn:
                current = [
                    r["id"]
                    for r in await conn.fetch(
                        "select id from app.employers where user_id = $1 and status = 'active'"
                        " and ended_on is null",
                        u.id,
                    )
                ]
                for eid in current:
                    version = await workplace.memory_version(conn, u.id, eid)
                    message = jobs.employer_pack_build(u.id, eid, "standup", version, day)
                    messages.append(message)
            sent += await self.schedule("standup", slot, messages)
        await self.dispatcher.recover()
        await self.dispatcher.once()
        return sent


async def main(once: bool, interval_s: float) -> None:
    settings = WorkerSettings()
    pool = await create_pool(settings.system_database_url, 1, 2)
    valkey = make_valkey(settings.valkey_url, settings.valkey_cluster)
    queue = jobs.SqsJobQueue(
        settings.sqs_queue_url, settings.aws_region, endpoint_url=settings.sqs_endpoint or None
    )
    scheduler = Scheduler(pool, valkey, queue)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        while True:
            for key in await scheduler.tick():
                log.info("enqueued %s", key)
            if once:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval_s)
            if stop.is_set():
                return
    finally:
        await pool.close()
        await valkey.aclose()


def run() -> None:
    load_dotenv()
    logs.configure("scheduler")
    parser = argparse.ArgumentParser(description="Enqueue the timed jobs.")
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between ticks")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.once, args.interval))
