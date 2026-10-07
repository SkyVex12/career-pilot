"""Test support for the workers (apps/worker/tests): the `Stack` the tests drive, and
`GatewayLog`, which writes a session's state and log stream the way the gateway does."""

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from copilot_core import jobs, keys
from copilot_core.ids import uuid7
from copilot_core.jobs import MemoryJobQueue
from copilot_core.storage import document_key
from copilot_core.teststack import S3_ENDPOINT, VALKEY_URL
from copilot_worker import documents, packs, sessionlog, system
from copilot_worker.config import WorkerSettings
from copilot_worker.context import Ctx
from copilot_worker.fixtures import ClaudeFixtures, DeepgramFixtures
from copilot_worker.runtime import HANDLERS, Job

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
HANDLER_MODULES = (documents, packs, sessionlog, system)


def worker_settings(**over: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "env": "test",
        "llm_providers": ["anthropic"],
        "embedder": "none",
        "key_provider": "dev",
        "dev_master_key": base64.b64encode(os.urandom(32)).decode(),
        "s3_endpoint": S3_ENDPOINT,
        "s3_bucket": "copilot-test",
        "s3_access_key": "local",
        "s3_secret_key": "local-secret",
        "valkey_url": VALKEY_URL,
    }
    base.update(over)
    return WorkerSettings(_env_file=None, **base)  # pyright: ignore[reportCallIssue]


@dataclass
class Stack:
    ctx: Ctx
    admin: asyncpg.Connection  # superuser on the test database, for checks only
    jobs: MemoryJobQueue
    claude: ClaudeFixtures
    deepgram: DeepgramFixtures

    async def run(self, message: dict[str, Any]) -> None:
        """Run one enqueued message's handler directly."""
        job = Job.parse(_json(message))
        await HANDLERS[job.type](job, self.ctx)

    def take(self, job_type: str | None = None) -> list[dict[str, Any]]:
        """Remove and return the enqueued messages (of one type)."""
        out = [m for m in self.jobs.sent if job_type is None or m["type"] == job_type]
        self.jobs.sent = [m for m in self.jobs.sent if m not in out]
        return out

    async def drain(self, *types: str, limit: int = 50) -> list[str]:
        """Run enqueued jobs of these types until none are left; returns their keys."""
        ran: list[str] = []
        for _ in range(limit):
            ready = [m for m in self.jobs.sent if m["type"] in types]
            if not ready:
                return ran
            m = ready[0]
            self.jobs.sent.remove(m)
            await self.run(m)
            ran.append(m["idempotency_key"])
        raise AssertionError(f"jobs still pending after {limit}: {self.jobs.sent}")

    async def new_user(self, name: str = "Alex Doe") -> uuid.UUID:
        uid = uuid.uuid4()
        await self.admin.execute(
            "insert into app.users (id, email, display_name) values ($1, $2, $3)",
            uid,
            f"{uid.hex}@example.com",
            name,
        )
        return uid

    async def upload(self, uid: uuid.UUID, kind: str, path: Path, content_type: str) -> uuid.UUID:
        body = await asyncio.to_thread(path.read_bytes)
        doc_id = uuid.uuid4()
        key = document_key(uid, doc_id, content_type)
        await self.ctx.store.put(key, body, content_type)
        await self.admin.execute(
            "insert into app.documents (id, user_id, kind, s3_key, sha256, content_type)"
            " values ($1, $2, $3, $4, $5, $6)",
            doc_id,
            uid,
            kind,
            key,
            hashlib.sha256(body).hexdigest(),
            content_type,
        )
        return doc_id


def _json(message: dict[str, Any]) -> str:
    return json.dumps(message)


STARTED = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


class GatewayLog:
    def __init__(self, stack: Stack, uid: UUID, store_transcript: bool = True,
                 mode: str = "interview") -> None:  # fmt: skip
        self.stack = stack
        self.uid = uid
        self.sid = uuid7()
        self.store = store_transcript
        self.mode = mode
        self.seg = 0

    async def start(self, job_id: UUID | None = None) -> None:
        state = {
            "user_id": str(self.uid),
            "mode": self.mode,
            "status": "active",
            "job_id": str(job_id) if job_id else "",
            "title": "",
            "platform": "zoom",
            "client_version": "1.0.0",
            "store_transcript": "1" if self.store else "0",
            "started_at": STARTED.isoformat(),
            "pack_id": "",
            "digest_version": "",
            "task_id": "task-1",
            "updated_at": str(time.time()),
        }
        await self.stack.ctx.valkey.hset(keys.sess_state(self.sid), mapping=state)
        await self.add({"kind": "session.started", **state})

    async def add(self, entry: dict[str, Any]) -> str:
        entry = {**entry, "at": datetime.now(UTC).isoformat()}
        raw = await self.stack.ctx.valkey.xadd(keys.sess_log(self.sid), {"e": json.dumps(entry)})
        return raw.decode()

    async def segment(self, text: str, start_ms: int) -> str:
        # The gateway leaves text out of the log without consent; `text` here lets a test
        # check that the worker drops it anyway.
        entry = {"kind": "segment", "channel": "them", "seg_id": self.seg, "start_ms": start_ms,
                 "end_ms": start_ms + 2000, "text": text}  # fmt: skip
        self.seg += 1
        return await self.add(entry)

    async def question(self, qid: UUID, text: str, qtype: str = "behavioral") -> str:
        return await self.add({
            "kind": "question", "question_id": str(qid), "text": text, "type": qtype,
            "confidence": 0.9, "addressed_to_user": True, "speculative_kept": True,
            "forced_by_hotkey": False, "detected_at": datetime.now(UTC).isoformat(),
        })  # fmt: skip

    async def answer(self, qid: UUID, pass_: str, text: str) -> str:
        return await self.add({
            "kind": "answer", "question_id": str(qid), "pass": pass_, "source": "model",
            "provider": "anthropic", "model": "claude-haiku-4-5", "fallback_attempt": 0,
            "tier": "standard", "text": text, "input_tokens": 4200, "cached_tokens": 4000,
            "output_tokens": 40, "cost_usd": 0.0008,
            "latency_ms": {"first_token_ms": 420, "total_ms": 900},
            "memory_node_ids": [],
        })  # fmt: skip

    async def end(self, seconds: int) -> None:
        ended = STARTED + timedelta(seconds=seconds)
        await self.add({"kind": "session.ended", "reason": "user"})
        await self.stack.ctx.valkey.hset(
            keys.sess_state(self.sid), mapping={"status": "ended", "ended_at": ended.isoformat()}
        )

    def flush_job(self, upto: str) -> dict[str, Any]:
        key = f"session.flush:{self.sid}:{upto}"
        payload = {"session_id": str(self.sid), "upto_entry_id": upto}
        return jobs.envelope("session.flush", key, self.uid, payload)

    def ended_job(self) -> dict[str, Any]:
        payload = {"session_id": str(self.sid)}
        return jobs.envelope("session.ended", f"session.ended:{self.sid}", self.uid, payload)
