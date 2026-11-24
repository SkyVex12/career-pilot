"""Durable session parents and ownership; payloads still travel through Valkey."""

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from copilot_core.db import Conn, user_tx
from copilot_core.session_writes import entry_hash


class WriterOwnershipLost(RuntimeError):
    pass


class ContextNotReady(RuntimeError):
    pass


@dataclass
class DurableSession:
    pool: asyncpg.Pool
    sid: UUID
    workspace_id: UUID
    actor_id: UUID
    writer_id: UUID
    generation: int = 0
    private_account_id: str | None = None
    metadata: dict[str, Any] | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[Conn]:
        async with user_tx(self.pool, self.workspace_id, actor_account_id=self.actor_id) as conn:
            yield conn

    @classmethod
    async def acquire(
        cls,
        pool: asyncpg.Pool,
        sid: UUID,
        workspace_id: UUID,
        actor_id: UUID,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "DurableSession":
        session = cls(pool, sid, workspace_id, actor_id, uuid4())
        async with asyncio.timeout(5), session.transaction() as conn:
            if metadata is not None:
                snapshot = dict(metadata)
                try:
                    await conn.execute(
                        "select app.session_admit($1,$2,$3,$4::jsonb)",
                        sid,
                        workspace_id,
                        actor_id,
                        snapshot,
                    )
                except asyncpg.ObjectNotInPrerequisiteStateError as exc:
                    raise ContextNotReady(
                        "Resume context changed or is still being prepared"
                    ) from exc
            claim = await conn.fetchval(
                "select app.session_claim($1,$2,$3,$4,30)",
                sid,
                actor_id,
                workspace_id,
                session.writer_id,
            )
            if isinstance(claim, str):
                claim = json.loads(claim)
            session.generation = int(claim["writer_generation"])
            session.private_account_id = claim.get("private_context_account_id")
            session.metadata = claim
        return session

    async def renew(self) -> None:
        try:
            async with asyncio.timeout(2), self.transaction() as conn:
                await conn.execute(
                    "select app.session_writer_guard($1,$2,$3,30)",
                    self.sid,
                    self.generation,
                    self.writer_id,
                )
        except asyncpg.SerializationError as exc:
            raise WriterOwnershipLost(str(exc)) from exc

    @asynccontextmanager
    async def batch(self, batch_id: str, entries: Sequence[str]) -> AsyncGenerator[list[str]]:
        hashes = [entry_hash(json.loads(entry)) for entry in entries]
        tagged = [
            json.dumps(
                {
                    **json.loads(entry),
                    "writer_generation": self.generation,
                    "writer_batch_id": batch_id,
                },
                separators=(",", ":"),
            )
            for entry in entries
        ]
        try:
            async with asyncio.timeout(2), self.transaction() as conn:
                await conn.execute(
                    "select app.session_writer_guard($1,$2,$3,30,$4,$5::text[])",
                    self.sid,
                    self.generation,
                    self.writer_id,
                    UUID(batch_id),
                    hashes,
                )
                yield tagged
        except asyncpg.SerializationError as exc:
            raise WriterOwnershipLost(str(exc)) from exc

    async def close(self, reason: str) -> None:
        async with asyncio.timeout(2), self.transaction() as conn:
            await conn.execute(
                "select app.session_writer_close($1,$2,$3,$4)",
                self.sid,
                self.generation,
                self.writer_id,
                reason,
            )
