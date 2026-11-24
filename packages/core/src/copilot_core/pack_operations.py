"""Bounded operation metadata, separate from expiring prepared content.

Status keys are tiny and expire. A lost worker/queue can never leave clients polling
forever. Each attempt has its own key, so an older completion cannot finish a newer
build for the same context. The pointer is only a display hint, never authorization.
"""

import hashlib
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from copilot_core.jobs import JobQueue
from copilot_core.valkey_client import Valkey, text

ACTIVE_TTL_S = 15 * 60
FINISHED_TTL_S = 24 * 3600


def pointer(uid: UUID | str, context: str) -> str:
    return f"pack-operation:{{{uid}}}:{context}"


def operation(uid: UUID | str, key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"pack-operation:{{{uid}}}:attempt:{digest}"


async def state(valkey: Valkey, uid: UUID, context: str) -> str:
    key = await valkey.get(pointer(uid, context))
    if key is None:
        return "idle"
    value = await valkey.get(text(key))
    return text(value) if value is not None else "idle"


async def states(valkey: Valkey, uid: UUID, contexts: list[str]) -> dict[str, str]:
    """Read any number of operation hints in two same-slot round trips."""
    if not contexts:
        return {}
    pointers: list[Any] = await valkey.mget([pointer(uid, context) for context in contexts])
    attempts = list(dict.fromkeys(text(value) for value in pointers if value is not None))
    values: list[Any] = await valkey.mget(attempts) if attempts else []
    by_attempt = dict(zip(attempts, values, strict=True))
    return {
        context: text(value) if value is not None else "idle"
        for context, target in zip(contexts, pointers, strict=True)
        for value in [by_attempt.get(text(target)) if target is not None else None]
    }


class ObservedQueue:
    def __init__(self, queue: JobQueue, valkey: Valkey) -> None:
        self.queue, self.valkey = queue, valkey

    async def send(self, message: Mapping[str, Any], delay_s: int = 0) -> None:
        if message.get("type") != "pack.build" or not message.get("user_id"):
            await self.queue.send(message, delay_s)
            return
        uid = str(message["user_id"])
        context = str(message["payload"]["context"])
        key = operation(uid, str(message["idempotency_key"]))
        created = await self.valkey.set(key, "queued", ex=ACTIVE_TTL_S, nx=True)
        if not created:
            # Reusing a failed idempotency key is a real retry. Publish queued before
            # accepting it, without moving a concurrently running/succeeded build back.
            created = bool(
                await self.valkey.eval(
                    "if redis.call('get',KEYS[1]) == 'failed' then "
                    "redis.call('set',KEYS[1],'queued','EX',ARGV[1]); return 1 end return 0",
                    1,
                    key,
                    ACTIVE_TTL_S,
                )
            )
        if created:
            await self.valkey.set(pointer(uid, context), key, ex=FINISHED_TTL_S)
        try:
            await self.queue.send(message, delay_s)
        except Exception:
            if created:
                await self.valkey.set(key, "failed", ex=FINISHED_TTL_S)
            raise


async def started(valkey: Valkey, uid: UUID, context: str, key: str) -> None:
    attempt = operation(uid, key)
    await valkey.set(attempt, "running", ex=ACTIVE_TTL_S)
    target = pointer(uid, context)
    previous = await valkey.get(target)
    if previous is None:
        await valkey.set(target, attempt, ex=FINISHED_TTL_S, nx=True)
    elif text(previous) != attempt:
        previous_state = await valkey.get(text(previous))
        if previous_state is None or text(previous_state) not in ("queued", "running"):
            # Gateway/scheduler jobs may not pass through ObservedQueue. Replace a
            # terminal pointer, but never overwrite a newer queued operation racing us.
            await valkey.eval(
                "if redis.call('get',KEYS[1]) == ARGV[1] then "
                "return redis.call('set',KEYS[1],ARGV[2],'EX',ARGV[3]) end return nil",
                1,
                target,
                text(previous),
                attempt,
                FINISHED_TTL_S,
            )


async def finished(valkey: Valkey, uid: UUID, key: str, success: bool) -> None:
    await valkey.set(operation(uid, key), "succeeded" if success else "failed", ex=FINISHED_TTL_S)
