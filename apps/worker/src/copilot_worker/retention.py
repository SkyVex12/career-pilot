"""Bounded, retryable history expiry and object deletion on the existing maintenance lane."""

import asyncio
import logging
from datetime import UTC, datetime

from copilot_core import keys, operations
from copilot_worker.context import Ctx

log = logging.getLogger(__name__)


async def expire_history(ctx: Ctx, limit: int = 100) -> int:
    """Only ended, fully drained sessions expire. Usage records have independent retention."""
    async with ctx.system() as conn:
        rows = await conn.fetch(
            "select id,user_id from app.sessions where expires_at<=clock_timestamp() "
            "and ended_at is not null order by expires_at,id limit $1",
            limit,
        )
    removed = 0
    for row in rows:
        sid = row["id"]
        async with ctx.system() as conn:
            session = await conn.fetchrow(
                "select id from app.sessions where id=$1 and expires_at<=clock_timestamp() "
                "and ended_at is not null and (writer_expires_at is null "
                "or writer_expires_at<=clock_timestamp()) for update skip locked",
                sid,
            )
            if session is None:
                continue
            try:
                async with asyncio.timeout(1):
                    if await ctx.valkey.exists(keys.sess_lease(sid)):
                        continue
                    if await ctx.valkey.xlen(keys.sess_log(sid)):
                        continue
            except (TimeoutError, ConnectionError):
                continue
            await conn.execute("delete from app.sessions where id=$1", sid)
        # Payload caches are disposable after the database deletion. A retry never
        # recreates the parent, and no usage ledger table is touched by this cascade.
        try:
            await ctx.valkey.delete(keys.sess_state(sid), keys.sess_events(sid), keys.sess_log(sid))
        except Exception:
            log.warning("expired session cache cleanup deferred: %s", sid)
        removed += 1
    return removed


async def delete_objects(ctx: Ctx, limit: int = 100) -> int:
    # Reserve bounded intents in a short transaction; storage calls never hold a DB lock.
    async with ctx.system() as conn:
        rows = await conn.fetch(
            "with todo as (select object_key from app.object_deletions "
            "where completed_at is null and next_attempt_at<=clock_timestamp() "
            "order by next_attempt_at for update skip locked limit $1) "
            "update app.object_deletions d set attempts=attempts+1,state='deleting', "
            "next_attempt_at=clock_timestamp()+interval '5 minutes' from todo "
            "where d.object_key=todo.object_key returning d.object_key",
            limit,
        )
    removed = 0
    for row in rows:
        key = row["object_key"]
        async with ctx.system() as conn:
            retained = await conn.fetchval(
                "select exists(select 1 from app.documents where s3_key=$1) "
                "or exists(select 1 from app.company_documents where s3_key=$1) "
                "or exists(select 1 from app.company_document_revisions where s3_key=$1) "
                "or exists(select 1 from app.resume_revisions where file_s3_key=$1) "
                "or exists(select 1 from app.resume_versions where file_s3_key=$1)",
                key,
            )
        if retained:
            async with ctx.system() as conn:
                await conn.execute(
                    "update app.object_deletions set state='pending' where object_key=$1", key
                )
            continue
        try:
            async with asyncio.timeout(15):
                await ctx.store.delete(key)
        except Exception as exc:
            async with ctx.system() as conn:
                await conn.execute(
                    # A timeout may follow a successful remote DELETE. Keep the key
                    # reserved until retry confirms deletion; never allow reuse.
                    "update app.object_deletions set last_error=$2 where object_key=$1",
                    key,
                    type(exc).__name__,
                )
            continue
        async with ctx.system() as conn:
            await conn.execute(
                "update app.object_deletions set completed_at=$2,last_error=null,state='done' "
                "where object_key=$1",
                key,
                datetime.now(UTC),
            )
        removed += 1
    return removed


async def sweep(ctx: Ctx) -> None:
    await expire_history(ctx)
    await delete_objects(ctx)
    async with ctx.system() as conn:
        await operations.purge_terminal_flushes(conn)
