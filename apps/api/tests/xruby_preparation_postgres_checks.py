"""Only called inside xruby_postgres_checks' owned synthetic PostgreSQL cluster."""

# Static test SQL and no external services.
# ruff: noqa: E501, PT017, PT018
import asyncio
import hashlib
import json
from uuid import UUID, uuid4

import asyncpg
from fastapi import HTTPException
from pydantic import SecretStr

from copilot_api import xruby_preparation as prep
from copilot_api.routes import xruby_oauth as auth
from copilot_api.routes import xruby_reads as reads
from copilot_core.db import user_tx


async def run_checks(svc, admin, account, profile, application, interview, record, access):
    settings = svc.settings
    settings.xruby_preparation_enabled = True
    settings.xruby_preparation_daily_limit = 20
    settings.xruby_preparation_global_daily_limit = 100
    settings.xruby_preparation_max_output_tokens = 2000
    settings.openai_api_key = SecretStr("synthetic-not-used")
    settings.openai_pack_model = "synthetic-not-used"
    settings.openai_pack_reasoning = None
    settings.openai_base_url = None
    resume, revision = uuid4(), uuid4()
    sha = hashlib.sha256(b"Verified synthetic resume").hexdigest()
    await admin.execute(
        "insert into app.resume_versions(id,user_id,current_revision_id,content_sha256,extracted_sha256,status,kind,application_id,created_at) values($1,$2,$3,$4,$4,'ready','tailored',$5,now())",
        resume,
        profile,
        revision,
        sha,
        application,
    )
    await admin.execute(
        "insert into app.resume_revisions values($1,$2,$3,$4,1)",
        revision,
        resume,
        profile,
        b"Verified synthetic resume",
    )
    await admin.execute(
        "update app.applications set resume_version_id=$1 where id=$2", resume, application
    )
    active = dict(record, scope=" ".join(sorted(auth.SCOPES | {auth.PREPARE_SCOPE})))
    await svc.valkey.set(auth.key("access", access), json.dumps(active), ex=600)
    grant = auth.Delegation(access, active)
    calls = 0
    draft = prep.Draft(
        job_summary="Practice against saved evidence.",
        requirements=["Recorded work"],
        prepared_answers=[
            prep.Answer(
                question="Describe your experience.", talking_points=["Review a verified example."]
            )
        ],
        gaps=["No additional story sources were consulted."],
    )

    async def hash_now():
        async with auth.read_scope(svc, grant, auth.PREPARE_SCOPE) as conn:
            value = await reads.source(conn, svc, grant, "interview_preparation", interview)
        return value["source_hash"]

    async def reserve(key):
        return await prep.reserve(svc, grant, interview, await hash_now(), key)

    async def provider(_svc, _snapshot):
        nonlocal calls
        calls += 1
        # A different connection can update source while the model is running: no held DB lock.
        await admin.execute("set lock_timeout='200ms'")
        await admin.execute("update app.applications set role=role where id=$1", application)
        await admin.execute("set lock_timeout='0'")
        return draft, {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}

    runner = prep.Runner(svc, provider)
    await prep.validate_release(svc)
    results = {}
    async with svc.pool.acquire() as conn:
        assert await conn.fetchval("select count(*) from app.xruby_preparation_requests") == 0
        assert not await conn.fetchval(
            "select has_table_privilege('app_api','app.resume_revisions','UPDATE')"
        )
        assert not await conn.fetchval(
            "select has_table_privilege('app_worker','app.xruby_preparation_requests','SELECT')"
        )
        assert not await conn.fetchval(
            "select has_function_privilege('app_worker','app.claim_xruby_preparation(uuid)','EXECUTE')"
        )
    result = await reserve("success")
    replay = await reserve("success")
    assert result["request_id"] == replay["request_id"]
    row = await runner.claim()
    assert row and "result_enc" not in row and "access_token" not in row
    await runner.process(row)
    ready = await prep.result(svc, grant, UUID(result["request_id"]))
    assert ready["state"] == "succeeded" and ready["result"]["generated"], (ready, calls)
    assert ready["result"]["pack"]["schema"] == "session_pack.v1"
    assert not ready["live_session_attached"] and calls == 1
    assert (await reserve("success"))["state"] == "succeeded"
    assert await runner.claim() is None
    encrypted = await admin.fetchval(
        "select result_enc from app.xruby_preparation_requests where id=$1",
        UUID(result["request_id"]),
    )
    assert encrypted.startswith(b"fixture-encrypted:")
    results["prepare_single_dispatch_success"] = True
    results["prepare_model_outside_transaction"] = True
    results["prepare_restricted_roles"] = True
    # Same key + different source is a conflict; changed source suppresses successful draft reads.
    await admin.execute("update app.applications set jd_text='Other JD' where id=$1", application)
    for action in (
        lambda: reserve("success"),
        lambda: prep.result(svc, grant, UUID(result["request_id"])),
    ):
        try:
            await action()
            raise AssertionError("stale receipt exposed")
        except HTTPException as exc:
            assert exc.status_code == 409
    results["prepare_source_hash_replay_conflict"] = True
    # Race: two reservations with the same account/key consume one budget slot.
    before = await admin.fetchval(
        "select reservations from app.xruby_preparation_budgets where account_id=$1", account
    )
    races = await asyncio.gather(reserve("race"), reserve("race"))
    assert races[0]["request_id"] == races[1]["request_id"]
    assert (
        await admin.fetchval(
            "select reservations from app.xruby_preparation_budgets where account_id=$1", account
        )
        == before + 1
    )
    await runner.process(await runner.claim())
    results["prepare_concurrent_reservation"] = True
    # A hard crash after marker commit is never reclaimed; expiry becomes terminal uncertain.
    crash = await reserve("crash")
    claimed = await runner.claim()
    assert claimed
    await admin.execute(
        "update app.xruby_preparation_requests set state='dispatched',dispatched_at=now(),lease_expires_at=now()-interval '1 second' where id=$1",
        UUID(crash["request_id"]),
    )
    previous = calls
    assert await runner.claim() is None
    assert (await prep.result(svc, grant, UUID(crash["request_id"])))["state"] == "uncertain"
    await runner.process(claimed)
    assert calls == previous
    try:
        await admin.execute(
            "update app.xruby_preparation_requests set state='queued',dispatched_at=null where id=$1",
            UUID(crash["request_id"]),
        )
        raise AssertionError("dispatch marker reset")
    except asyncpg.RaiseError:
        pass
    results["prepare_crash_after_dispatch_never_retries"] = True
    # Before dispatch, expired leases can recover with a new generation; stale owner cannot call.
    recover = await reserve("recover")
    stale = await runner.claim()
    await admin.execute(
        "update app.xruby_preparation_requests set lease_expires_at=now()-interval '1 second' where id=$1",
        UUID(recover["request_id"]),
    )
    fresh_runner = prep.Runner(svc, provider)
    new = await fresh_runner.claim()
    assert new["lease_generation"] > stale["lease_generation"]
    previous = calls
    await runner.process(stale)
    assert calls == previous
    await fresh_runner.process(new)
    assert calls == previous + 1
    results["prepare_predispatch_recovery_fenced"] = True

    # Any provider timeout/shape error is uncertain and same-key replay does not dispatch.
    async def broken(*_):
        raise TimeoutError("synthetic provider uncertainty")

    await reserve("timeout")
    bad_runner = prep.Runner(svc, broken)
    await bad_runner.process(await bad_runner.claim())
    assert (await reserve("timeout"))["state"] == "uncertain"
    assert await bad_runner.claim() is None
    results["prepare_timeout_terminal"] = True

    async def parsed_but_invalid(*_):
        raise prep.ProviderFailure({"total_tokens": 29})

    invalid = await reserve("invalid-response")
    invalid_runner = prep.Runner(svc, parsed_but_invalid)
    await invalid_runner.process(await invalid_runner.claim())
    observed = await prep.result(svc, grant, UUID(invalid["request_id"]))
    assert observed["state"] == "uncertain"
    assert observed["usage_status"] == "measured"
    assert observed["usage"]["total_tokens"] == 29
    results["prepare_invalid_draft_retains_usage"] = True

    # Changed sources after provider return prevent publication but retain measured usage.
    async def changing(*_):
        await admin.execute(
            "update app.applications set jd_text='Changed during model' where id=$1", application
        )
        return draft, {"total_tokens": 31}

    changed = await reserve("changed")
    changed_runner = prep.Runner(svc, changing)
    await changed_runner.process(await changed_runner.claim())
    out = await prep.result(svc, grant, UUID(changed["request_id"]))
    assert (
        out["state"] == "uncertain" and "result" not in out and out["usage"]["total_tokens"] == 31
    )
    results["prepare_changed_source_hides_publication"] = True
    # Budget is persistent and consumed conservatively even for uncertain attempts.
    count = await admin.fetchval(
        "select reservations from app.xruby_preparation_budgets where account_id=$1", account
    )
    settings.xruby_preparation_daily_limit = count
    try:
        await reserve("over-budget")
        raise AssertionError("budget exceeded")
    except HTTPException as exc:
        assert exc.status_code == 429
    settings.xruby_preparation_daily_limit = 20
    results["prepare_persistent_payer_budget"] = True
    settings.xruby_preparation_global_daily_limit = count
    try:
        await reserve("over-global-budget")
        raise AssertionError("global payer budget exceeded")
    except HTTPException as exc:
        assert exc.status_code == 429
    settings.xruby_preparation_global_daily_limit = 100
    results["prepare_global_payer_budget"] = True
    other = uuid4()
    await admin.execute("insert into app.accounts values($1,'active','{}'::jsonb)", other)
    settings.xruby_preparation_global_daily_limit = count
    async with user_tx(svc.pool, other, actor_account_id=other) as conn:
        assert not await conn.fetchval(
            "select app.reserve_xruby_preparation_budget($1,2,$2)", other, count
        )
        assert not await conn.fetchval(
            "select app.lock_xruby_preparation_resume($1,$2)", resume, revision
        )
    settings.xruby_preparation_global_daily_limit = 100
    results["prepare_global_cap_cross_account"] = True
    # Exact text bytes belong to the composite citation even when outer content hash is unchanged.
    stale = await hash_now()
    await admin.execute(
        "update app.resume_revisions set text_enc=$1 where id=$2",
        b"Replaced extraction text",
        revision,
    )
    assert await hash_now() != stale
    try:
        await prep.reserve(svc, grant, interview, stale, "old-text-hash")
        raise AssertionError("old text accepted")
    except HTTPException as exc:
        assert exc.status_code == 409
    results["prepare_exact_text_hash_bound"] = True
    # A nearly expired grant must not dispatch a model request that cannot publish in time.
    expiring = await reserve("expiry")
    expiry_row = await runner.claim()
    await admin.execute(
        "update app.xruby_preparation_requests set lease_expires_at=clock_timestamp()+interval '1 second' where id=$1",
        UUID(expiring["request_id"]),
    )
    previous = calls
    await runner.process(expiry_row)
    assert calls == previous
    assert (await prep.result(svc, grant, UUID(expiring["request_id"])))["state"] == "rejected"
    results["prepare_minimum_dispatch_window"] = True

    # A revoked grant cannot publish; usage and reservation survive revocation.
    async def revoking(*_):
        await prep.revoke_grant(svc, active, auth.key("access", access))
        return draft, {"total_tokens": 32}

    revoked = await reserve("revoked")
    revoke_runner = prep.Runner(svc, revoking)
    await revoke_runner.process(await revoke_runner.claim())
    row = await admin.fetchrow(
        "select state,usage,result_enc from app.xruby_preparation_requests where id=$1",
        UUID(revoked["request_id"]),
    )
    assert (
        row["state"] == "uncertain"
        and row["result_enc"] is None
        and json.loads(row["usage"])["total_tokens"] == 32
    )
    assert await revoke_runner.claim() is None
    results["prepare_revocation_retains_usage_blocks_publish"] = True
    # Ordinary role cannot see another account's ledger or mutate immutable receipts.
    async with user_tx(svc.pool, uuid4(), actor_account_id=uuid4()) as conn:
        assert await conn.fetchval("select count(*) from app.xruby_preparation_requests") == 0
    await svc.valkey.set(auth.key("access", access), json.dumps(record), ex=600)
    settings.xruby_preparation_enabled = False
    return results
