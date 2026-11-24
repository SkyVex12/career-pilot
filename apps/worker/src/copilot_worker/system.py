"""System jobs: they run as `app_system` (BYPASSRLS), the only code path that does.

- `usage.report`: fills `usage_daily` from durable measured and legacy usage for one day. No
  payment provider yet (Stage 0), so `reported_at` stays empty.
- `user.delete`: disables sign-in, transfers shared workspaces to a remaining member, and
  deletes unshared workspace content, objects, cache entries, and workspace keys. The final
  account deletion removes private credentials, content, and account keys. Operational
  tombstones and content-free usage accounting survive under separate retention policies.
- `sessions.sweep`: ends sessions whose gateway task died and that nobody resumed
  (ARCHITECTURE.md, open question "Abandoned sessions"): no lease, not ended, and no state
  update for longer than the resume window. Also ends sessions whose Valkey state expired
  before a `session.ended` reached a worker.
"""

import json
import logging
import time
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

from copilot_core import keys, operations
from copilot_core.careeros import CareerOsError
from copilot_core.storage import user_prefix
from copilot_core.usage import daily_usage
from copilot_core.valkey_client import text
from copilot_worker.context import Ctx
from copilot_worker.runtime import Job, handler

log = logging.getLogger(__name__)

STALE_DB_SESSION_S = 3 * 3600  # Valkey state lives 2 h after its last write


@handler("usage.report")
async def usage_report(job: Job, ctx: Ctx) -> None:
    day = date.fromisoformat(str(job.payload["day"]))
    async with ctx.system() as conn:
        rows = await daily_usage(conn, day=day)
        for row in rows:
            await conn.execute(
                "insert into app.usage_daily (user_id, day, live_seconds)"
                " select $1, $2, $3 where exists(select 1 from app.users where id = $1)"
                " on conflict (user_id, day) do update set live_seconds = excluded.live_seconds",
                row.workspace_id,
                day,
                (row.measured_microseconds + row.legacy_microseconds) // 1_000_000,
            )
    log.info("usage for %s: %d workspace rollups", day, len(rows))


async def _scan_delete(ctx: Ctx, pattern: str) -> int:
    deleted = 0
    async for key in ctx.valkey.scan_iter(match=pattern, count=500):
        deleted += int(await ctx.valkey.delete(key))
    return deleted


@handler("user.delete")
async def user_delete(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    lease = operations.current_lease.get()
    except_key = None if lease is None else lease.key
    # Fence access before external cleanup. Memberships, including revoked rows,
    # remain until final deletion so a crash can rediscover unfinished workspaces.
    async with ctx.system() as conn:
        await conn.execute("update app.accounts set status='deleting' where id=$1", uid)
        owned: list[UUID] = []
        workspaces = await conn.fetch(
            "select w.id from app.workspaces w where exists("
            " select 1 from app.workspace_memberships m where m.workspace_id=w.id"
            " and m.account_id=$1) or (w.created_by_account_id=$1 and w.status='deleting')"
            " order by w.id for update of w",
            uid,
        )
        for row in workspaces:
            wid = row["id"]
            heir = await conn.fetchval(
                "select m.account_id from app.workspace_memberships m join app.accounts a"
                " on a.id=m.account_id where m.workspace_id=$1 and m.account_id<>$2"
                " and m.status='active' and a.status='active'"
                " order by (m.role='owner') desc,m.created_at,m.account_id limit 1",
                wid,
                uid,
            )
            if heir is None:
                await conn.execute("update app.workspaces set status='deleting' where id=$1", wid)
                await operations.cancel_scope(conn, wid, except_key=except_key)
                owned.append(wid)
            else:
                # Creator attribution is not ownership. A remaining member owns
                # the retained workspace, even when its UUID was the old account's.
                await conn.execute(
                    "update app.workspace_memberships set role='owner'"
                    " where workspace_id=$1 and account_id=$2",
                    wid,
                    heir,
                )
                await conn.execute(
                    "update app.workspaces set owner_id=$2 where id=$1 and owner_id=$3",
                    wid,
                    heir,
                    uid,
                )
            await conn.execute(
                "update app.workspace_memberships set status='revoked'"
                " where workspace_id=$1 and account_id=$2",
                wid,
                uid,
            )
        await conn.execute(
            "update app.operations set status='cancelled',finished_at=clock_timestamp(),"
            " lease_generation=lease_generation+1,lease_expires_at=null where account_id=$1"
            " and ($2::text is null or idempotency_key<>$2) and status not in ('done','cancelled')",
            uid,
            except_key,
        )
    # Database account status already fences new authenticated work while external
    # sign-in removal and workspace content cleanup complete.
    signin = "none (dev auth)" if ctx.settings.auth == "dev" else f"kept ({ctx.settings.auth})"
    if ctx.users is not None:
        signin = "deleted" if await ctx.users.delete_user(uid) else "already gone"
    career_os = await revoke_career_os(ctx, uid)
    removed = vk = 0
    for who in owned:
        objects, valkey_keys = await _delete_data(ctx, who)
        removed, vk = removed + objects, vk + valkey_keys
    async with ctx.system() as conn:
        # Account-private rows cascade here. No workspace is owned by this FK.
        await conn.execute("delete from app.accounts where id=$1", uid)
    ctx.crypto.forget(uid)
    log.info(
        "user %s deleted: sign-in %s, %d profiles, %d objects, %d Valkey keys, HubRuby link %s",
        uid, signin, len(owned), removed, vk, career_os,
    )  # fmt: skip


async def _delete_data(ctx: Ctx, uid: UUID) -> tuple[int, int]:
    """Everything an unshared workspace has: objects, live state, data keys, rows. Objects and live
    state first: if this run dies halfway, the workspace is still there and the retry finds
    everything it needs."""
    removed = await ctx.store.delete_prefix(user_prefix(uid))
    async with ctx.system() as conn:
        sessions = [r["id"] for r in await conn.fetch(
            "select id from app.sessions where user_id = $1", uid
        )]  # fmt: skip
    active = await ctx.valkey.get(keys.user_active_session(uid))
    if active is not None:
        sessions.append(text(active))
    vk = 0
    for sid in sessions:
        vk += int(await ctx.valkey.delete(
            keys.sess_state(sid), keys.sess_events(sid), keys.sess_log(sid), keys.sess_lease(sid)
        ))  # fmt: skip
    vk += int(await ctx.valkey.delete(keys.digest(uid), keys.user_active_session(uid)))
    for prefix in ("pack", "pack-operation", "qa", "rl", "digest"):  # digest: each version's
        vk += await _scan_delete(ctx, f"{prefix}:{{{uid}}}:*")
    async with ctx.system() as conn:
        # Data keys go first and explicitly: with them gone, ciphertext is unreadable even if
        # a backup of the other tables survives.
        await conn.execute("delete from app.user_keys where user_id = $1", uid)
        # Other accounts' listings of this row (a shared workspace) go with it.
        await conn.execute("delete from app.profiles where id = $1", uid)
        await conn.execute("delete from app.workspaces where id = $1", uid)  # cascades
    ctx.crypto.forget(uid)
    return removed, vk


async def revoke_career_os(ctx: Ctx, uid: UUID) -> str:
    """Revoke the account's Career-OS token on Career-OS before its sealed copy goes. Best
    effort: the account is deleted either way, and the user can revoke the link in Career-OS."""
    async with ctx.system() as conn:
        row = await conn.fetchrow(
            "select token_enc, key_version from app.integrations"
            " where user_id = $1 and provider = 'career_os' and status = 'active'",
            uid,
        )
        if row is None:
            return "none"
        if ctx.career_os is None:
            return "kept (HubRuby not configured)"
        secret = await ctx.crypto.decrypt(
            conn, uid, "integrations", f"{uid}:career_os", bytes(row["token_enc"]),
            int(row["key_version"]),
        )  # fmt: skip
    try:
        await ctx.career_os.revoke(str(json.loads(secret)["token"]))
    except CareerOsError as e:
        log.warning("could not revoke the HubRuby link of %s: %s", uid, e.message)
        return "not revoked"
    return "revoked"


@handler("sessions.sweep")
async def sessions_sweep(job: Job, ctx: Ctx) -> None:
    window = ctx.settings.resume_window_s
    ended = await _sweep_durable(ctx, window)
    async for key in ctx.valkey.scan_iter(match="sess:*:state", count=500):
        name = text(key)
        sid = name.split("{", 1)[-1].split("}", 1)[0]
        state = {
            text(k): text(v)
            for k, v in cast(dict[Any, Any], await ctx.valkey.hgetall(name)).items()
        }
        if (
            not state
            or state.get("status") == "ended"
            or not state.get("user_id")
            or int(state.get("writer_generation") or 0) > 0
        ):
            continue
        if await ctx.valkey.exists(keys.sess_lease(sid)):
            continue
        updated = float(state.get("updated_at") or 0)
        if time.time() - updated <= window:
            continue
        uid = UUID(state["user_id"])
        ended_at = datetime.fromtimestamp(updated or time.time(), UTC).isoformat()
        await ctx.valkey.hset(
            name, mapping={"status": "ended", "ended_reason": "abandoned", "ended_at": ended_at}
        )
        current = await ctx.valkey.get(keys.user_active_session(uid))
        if current is not None and text(current) == sid:
            await ctx.valkey.delete(keys.user_active_session(uid))
        await ctx.enqueue("session.ended", f"session.ended:{sid}", uid, {"session_id": sid})
        ended += 1
    # Sessions Aurora still has open but Valkey has forgotten.
    async with ctx.system() as conn:
        rows = await conn.fetch(
            "select id, user_id from app.sessions where ended_at is null"
            " and admission_state='legacy'"
            " and started_at < now() - make_interval(secs => $1)",
            float(STALE_DB_SESSION_S),
        )
    for r in rows:
        if await ctx.valkey.exists(keys.sess_state(r["id"])):
            continue
        sid = str(r["id"])
        await ctx.enqueue(
            "session.ended", f"session.ended:{sid}", r["user_id"], {"session_id": sid}
        )
        ended += 1
    if ended:
        log.info("sweeper ended %d abandoned sessions", ended)
    from copilot_worker.retention import sweep

    await sweep(ctx)


async def _sweep_durable(ctx: Ctx, window: float) -> int:
    """A missing cache cannot hide an admitted call after its writer lease expires."""
    async with ctx.system() as conn:
        rows = await conn.fetch(
            "select id,user_id,writer_generation,coalesce(writer_expires_at,started_at) as expiry"
            " from app.sessions where ended_at is null and admission_state<>'legacy'"
            " and coalesce(writer_expires_at,started_at)"
            " < clock_timestamp()-make_interval(secs=>$1)"
            " order by coalesce(writer_expires_at,started_at),id for update skip locked limit 500",
            float(window),
        )
        for row in rows:
            await conn.execute(
                "update app.sessions set admission_state='closing' where id=$1", row["id"]
            )
            key = (
                f"session.ended:{row['id']}:sweep:g{row['writer_generation']}:"
                f"{int(row['expiry'].timestamp())}"
            )
            await ctx.enqueue(
                "session.ended",
                key,
                row["user_id"],
                {"session_id": str(row["id"]), "writer_generation": row["writer_generation"]},
                conn=conn,
            )
    return len(rows)
