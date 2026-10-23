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
    pairing: str | None = None,
) -> dict[str, Any]:
    """`pack.build` for one call context: `interview:{id}`, `job:{id}` (an application) or
    `standup:{date}`.

    A pack depends on its context, on the digest it was built from and on the embedding model
    of its prepared answers' vectors, so the key names all three: a digest rebuild or a new
    embedding model makes a new key and the pack is rebuilt; a repeat is skipped. An
    application's pack also depends on its pairing (`copilot_core.resumes.pairing_tag`: the
    resume version, the JD and the pairing revision), so switching versions and back rebuilds.
    """
    key = (
        f"pack.build:{user_id}:{context}:{digest_tag(digest_version)}"
        f":{embedding_tag(embedding_model)}"
    )
    if pairing:
        key += f":{pairing}"
    payload = {
        "mode": mode,
        "context": context,
        "due_at": due_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return envelope("pack.build", key, user_id, payload)


def employer_pack_build(
    user_id: UUID,
    employer_id: UUID,
    mode: str,
    memory_version: str,
    day: str,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """`pack.build` for a company's standup or meeting pack (context `employer:{id}`).

    The pack is rendered from that company's memory, with no model call, so the key names the
    company's memory version (`workplace.memory_version`), the day the pack is for (a standup's
    "yesterday" moves with it) and the embedding model of its prepared answers."""
    key = (
        f"pack.build:{user_id}:employer:{employer_id}:{mode}:{memory_version}:{day}"
        f":{embedding_tag(embedding_model)}"
    )
    payload = {
        "mode": mode,
        "context": f"employer:{employer_id}",
        "due_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day": day,
    }
    return envelope("pack.build", key, user_id, payload)


def workplace_refresh(user_id: UUID, employer_id: UUID, memory_version: str) -> dict[str, Any]:
    """`workplace.refresh`: after a company's memory changed (extraction, approvals, edits,
    post-call commitments), embed its new nodes and rebuild its packs. Once per version."""
    key = f"workplace.refresh:{user_id}:{employer_id}:{memory_version}"
    return envelope("workplace.refresh", key, user_id, {"employer_id": str(employer_id)})


def resume_extract(user_id: UUID, resume_version_id: UUID, content_sha256: str) -> dict[str, Any]:
    """`resume.extract`: one resume version's content into its memory nodes, once per content."""
    key = f"resume.extract:{user_id}:{resume_version_id}:{content_sha256}"
    payload = {"resume_version_id": str(resume_version_id), "content_sha256": content_sha256}
    return envelope("resume.extract", key, user_id, payload)


def digest_rebuild(user_id: UUID, resume_version_id: UUID, memory_version: str) -> dict[str, Any]:
    """`digest.rebuild` for one resume version, once per version of its memory."""
    key = f"digest.rebuild:{user_id}:{resume_version_id}:{memory_version}"
    return envelope("digest.rebuild", key, user_id, {"resume_version_id": str(resume_version_id)})
