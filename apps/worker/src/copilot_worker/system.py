"""System jobs: they run as `app_system` (BYPASSRLS), the only code path that does.

- `usage.report`: fills `usage_daily` from the billed seconds in `sessions` for one day. No
  payment provider yet (Stage 0), so `reported_at` stays empty.
- `user.delete`: removes everything the user has: the Cognito user (so no new sign-in can
  start while the rest goes), the S3 prefix, every Valkey key, the data keys (`user_keys`,
  which makes any ciphertext left anywhere unreadable), every row (the users row cascades),
  and the user's `job_runs`.
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

from copilot_core import keys
from copilot_core.careeros import CareerOsError
from copilot_core.storage import user_prefix
from copilot_core.valkey_client import text
from copilot_worker.context import Ctx
from copilot_worker.runtime import Job, handler

log = logging.getLogger(__name__)

STALE_DB_SESSION_S = 3 * 3600  # Valkey state lives 2 h after its last write


@handler("usage.report")
async def usage_report(job: Job, ctx: Ctx) -> None:
    day = date.fromisoformat(str(job.payload["day"]))
    async with ctx.system() as conn:
        status = await conn.execute(
            """
            insert into app.usage_daily (user_id, day, live_seconds)
            select user_id, $1, sum(billed_seconds)::int
            from app.sessions
            where ended_at is not null and (started_at at time zone 'UTC')::date = $1
            group by user_id
            on conflict (user_id, day) do update set live_seconds = excluded.live_seconds
            """,
            day,
        )
    log.info("usage for %s: %s", day, status)


async def _scan_delete(ctx: Ctx, pattern: str) -> int:
    deleted = 0
    async for key in ctx.valkey.scan_iter(match=pattern, count=500):
        deleted += int(await ctx.valkey.delete(key))
    return deleted


@handler("user.delete")
async def user_delete(job: Job, ctx: Ctx) -> None:
    uid = job.uid
    # The sign-in first, so no new session can start while the rest is deleted (an access
    # token already issued lasts until it expires, an hour at most).
    signin = "none (dev auth)" if ctx.settings.auth == "dev" else f"kept ({ctx.settings.auth})"
    if ctx.users is not None:
        signin = "deleted" if await ctx.users.delete_user(uid) else "already gone"
    # Objects and live state next: if this run dies halfway, the users row is still there
    # and the retry finds everything it needs.
    removed = await ctx.store.delete_prefix(user_prefix(uid))
    career_os = await revoke_career_os(ctx, uid)
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
    for prefix in ("pack", "qa", "rl", "digest"):  # digest: each resume version's
        vk += await _scan_delete(ctx, f"{prefix}:{{{uid}}}:*")
    async with ctx.system() as conn:
        # Data keys go first and explicitly: with them gone, ciphertext is unreadable even if
        # a backup of the other tables survives.
        await conn.execute("delete from app.user_keys where user_id = $1", uid)
        await conn.execute("delete from app.users where id = $1", uid)  # cascades
        await conn.execute("delete from app.job_runs where user_id = $1", uid)
    ctx.crypto.forget(uid)
    log.info(
        "user %s deleted: sign-in %s, %d objects, %d Valkey keys, Career-OS link %s",
        uid, signin, removed, vk, career_os,
    )  # fmt: skip


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
            return "kept (Career-OS not configured)"
        secret = await ctx.crypto.decrypt(
            conn, uid, "integrations", f"{uid}:career_os", bytes(row["token_enc"]),
            int(row["key_version"]),
        )  # fmt: skip
    try:
        await ctx.career_os.revoke(str(json.loads(secret)["token"]))
    except CareerOsError as e:
        log.warning("could not revoke the Career-OS link of %s: %s", uid, e.message)
        return "not revoked"
    return "revoked"


@handler("sessions.sweep")
async def sessions_sweep(job: Job, ctx: Ctx) -> None:
    window = ctx.settings.resume_window_s
    ended = 0
    async for key in ctx.valkey.scan_iter(match="sess:*:state", count=500):
        name = text(key)
        sid = name.split("{", 1)[-1].split("}", 1)[0]
        state = {
            text(k): text(v)
            for k, v in cast(dict[Any, Any], await ctx.valkey.hgetall(name)).items()
        }
        if not state or state.get("status") == "ended" or not state.get("user_id"):
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
