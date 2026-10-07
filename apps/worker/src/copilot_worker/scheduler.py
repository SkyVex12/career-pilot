"""Timed jobs (ARCHITECTURE.md, "Data and storage": EventBridge Scheduler).

Locally this command enqueues them on the documented cadence; in AWS (build step 5) EventBridge
Scheduler runs `copilot-scheduler --once` as a scheduled task every five minutes instead.

    uv run copilot-scheduler            # tick every 60 s until stopped
    uv run copilot-scheduler --once     # one tick, then exit

- sessions.sweep: every 5 minutes; key sessions.sweep:{slot}
- usage.report: daily from 00:15 UTC, for the day before; key usage.report:{day}
- digest.rebuild: weekly from Monday 03:00 UTC, per user; key digest.rebuild:{uid}:{memory}
- pack.build: 30 minutes before each user's weekday standup (settings standup_time and
  timezone); key pack.build:{uid}:standup:{date}:{digest}

Each slot is sent once: a `sched:{job}:{slot}` key in Valkey (SET NX, two days) marks it, so
several schedulers or a restart never double-send, and the workers' `job_runs` check catches
anything that slips through.
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg

from copilot_core import jobs
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

    async def once(self, name: str, slot: str) -> bool:
        return bool(await self.valkey.set(f"sched:{name}:{slot}", "1", nx=True, ex=2 * 86400))

    async def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now(UTC)
        sent: list[str] = []
        due = slots(now)
        sweep = due["sessions.sweep"]
        if sweep and await self.once("sessions.sweep", sweep):
            key = f"sessions.sweep:{sweep}"
            await self.queue.send(jobs.envelope("sessions.sweep", key, None, {}))
            sent.append(key)
        day = due["usage.report"]
        if day and await self.once("usage.report", day):
            key = f"usage.report:{day}"
            await self.queue.send(jobs.envelope("usage.report", key, None, {"day": day}))
            sent.append(key)
        async with system_tx(self.pool) as conn:
            users = [UserRow(r["id"], r["settings"] or {}) for r in await conn.fetch(
                "select id, settings from app.users"
            )]  # fmt: skip
            versions: dict[UUID, str] = {}
            week = due["digest.rebuild"]
            if week and await self.once("digest.rebuild", week):
                for u in users:
                    versions[u.id] = await memory_version(conn, u.id)
        for uid, version in versions.items():
            key = f"digest.rebuild:{uid}:{version}"
            await self.queue.send(jobs.envelope("digest.rebuild", key, uid, {}))
            sent.append(key)
        for u in users:
            standup = next_standup(now, u.settings)
            if standup is None or not (standup - STANDUP_LEAD <= now < standup):
                continue
            slot = f"{u.id}:{standup.strftime('%Y%m%dT%H%M')}"
            if not await self.once("standup", slot):
                continue
            digest = await digest_version_of(self.valkey, u.id)
            message = jobs.pack_build(
                u.id, "standup", f"standup:{standup.date().isoformat()}", standup, digest
            )
            await self.queue.send(message)
            sent.append(str(message["idempotency_key"]))
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
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description="Enqueue the timed jobs.")
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between ticks")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.once, args.interval))
