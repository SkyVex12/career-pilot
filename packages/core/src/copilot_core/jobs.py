"""Job envelope for the `jobs` queue (ARCHITECTURE.md, "SQS job catalog")."""

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from copilot_core.aws import boto3_client


def envelope(
    job_type: str, idempotency_key: str, user_id: UUID | None, payload: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "type": job_type,
        "version": 1,
        "idempotency_key": idempotency_key,
        "user_id": str(user_id) if user_id else None,
        "payload": dict(payload),
        "enqueued_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def group_id(message: Mapping[str, Any]) -> str:
    """Fair queues: the user id is the message group id, or `system` for jobs without a user."""
    return str(message.get("user_id") or "system")


class JobQueue(Protocol):
    async def send(self, message: Mapping[str, Any], delay_s: int = 0) -> None: ...


class SqsJobQueue:
    def __init__(
        self, queue_url: str, region: str, endpoint_url: str | None = None, client: Any = None
    ) -> None:
        self.client: Any = client or boto3_client(
            "sqs", region_name=region, endpoint_url=endpoint_url
        )
        self.queue_url = queue_url

    async def send(self, message: Mapping[str, Any], delay_s: int = 0) -> None:
        await asyncio.to_thread(
            self.client.send_message,
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(message, separators=(",", ":")),
            MessageGroupId=group_id(message),
            DelaySeconds=delay_s,
        )


class MemoryJobQueue:
    """In-process queue for tests and for running the gateway without a queue."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, message: Mapping[str, Any], delay_s: int = 0) -> None:
        self.sent.append(dict(message))


def digest_tag(digest_version: str | None) -> str:
    """A short form of a digest version for idempotency keys ("none" before the first)."""
    return digest_version.removeprefix("sha256:")[:16] if digest_version else "none"


def embedding_tag(embedding_model: str | None) -> str:
    """A short form of an embedding model id for idempotency keys ("none" without one)."""
    if not embedding_model:
        return "none"
    return hashlib.sha256(embedding_model.encode("utf-8")).hexdigest()[:8]


def pack_build(
    user_id: UUID,
    mode: str,
    context: str,
    due_at: datetime,
    digest_version: str | None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """`pack.build` for one call context: `interview:{id}`, `job:{id}` or `standup:{date}`.

    A pack depends on its context, on the digest it was built from and on the embedding model
    of its prepared answers' vectors, so the key names all three: a digest rebuild or a new
    embedding model makes a new key and the pack is rebuilt; a repeat is skipped.
    """
    key = (
        f"pack.build:{user_id}:{context}:{digest_tag(digest_version)}"
        f":{embedding_tag(embedding_model)}"
    )
    payload = {
        "mode": mode,
        "context": context,
        "due_at": due_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return envelope("pack.build", key, user_id, payload)
