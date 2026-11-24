"""Immutable source revisions fence A→B→A races, even with the same content hash."""

from uuid import uuid4

import asyncpg
import pytest

from copilot_core import jobs, keys, resumes
from copilot_core.packs import Digest
from copilot_worker import documents, packs
from copilot_worker.runtime import RetryLater
from copilot_worker.testing import Stack

pytestmark = pytest.mark.stack


async def test_extract_rejects_replaced_revision_and_new_revision_can_run(
    stack: Stack, monkeypatch
):
    uid, version = await stack.new_user(), uuid4()
    source = "The source resume text."
    sha = resumes.sha256_text(source)
    async with stack.ctx.user(uid) as conn:
        sealed, key = await stack.ctx.crypto.encrypt(
            conn, uid, "resume_versions", str(version), source
        )
        first_revision = await conn.fetchval(
            "insert into app.resume_versions(id,user_id,kind,label,source,text_enc,key_version,"
            "content_sha256,status) values($1,$2,'base','Resume','text',$3,$4,$5,'extracting')"
            " returning current_revision_id",
            version,
            uid,
            sealed,
            key,
            sha,
        )
    assert first_revision is not None
    initial = jobs.resume_extract(uid, version, sha, resume_revision_id=first_revision)

    async def stale_model(*args, **kwargs):
        await stack.admin.execute(
            "update app.resume_versions set content_sha256=$2 where id=$1",
            version,
            "b" * 64,
        )
        await stack.admin.execute(
            "update app.resume_versions set content_sha256=$2 where id=$1",
            version,
            sha,
        )
        return {"headline": "Discard this stale answer"}

    monkeypatch.setattr(documents, "complete_json", stale_model)
    await stack.run(initial)
    assert (
        await stack.admin.fetchval(
            "select count(*) from app.memory_nodes where resume_version_id=$1",
            version,
        )
        == 0
    )
    latest_revision = await stack.admin.fetchval(
        "select current_revision_id from app.resume_versions where id=$1",
        version,
    )
    assert latest_revision != first_revision
    current = jobs.resume_extract(uid, version, sha, resume_revision_id=latest_revision)
    assert current["idempotency_key"] != initial["idempotency_key"]

    async def current_model(*args, **kwargs):
        return {"headline": "Current revision's generated memory"}

    monkeypatch.setattr(documents, "complete_json", current_model)
    await stack.run(current)
    [digest_job] = stack.take("digest.rebuild")
    assert digest_job["payload"]["resume_revision_id"] == str(latest_revision)
    await stack.run(digest_job)
    digest = Digest.model_validate_json(await stack.ctx.valkey.get(keys.digest(uid)))
    assert digest.resume_revision_id == latest_revision
    assert digest.resume_content_sha256 == sha
    assert "Current revision's generated memory" in digest.text

    # A delayed old digest operation must neither stamp nor overwrite current cache.
    await stack.run(
        jobs.digest_rebuild(
            uid,
            version,
            "same-rendered-memory",
            resume_revision_id=first_revision,
        )
    )
    after = Digest.model_validate_json(await stack.ctx.valkey.get(keys.digest(uid)))
    assert after == digest


async def test_pack_requires_one_current_source_snapshot(stack: Stack, monkeypatch):
    uid, version, application = await stack.new_user(), uuid4(), uuid4()
    sha = resumes.sha256_text("Prepared memory")
    async with stack.ctx.user(uid) as conn:
        sealed, key = await stack.ctx.crypto.encrypt(
            conn, uid, "resume_versions", str(version), "Prepared memory"
        )
        revision = await conn.fetchval(
            "insert into app.resume_versions(id,user_id,kind,label,source,content_sha256,"
            "extracted_sha256,status,text_enc,key_version)"
            " values($1,$2,'base','Resume','text',$3,$3,'extracted',$4,$5)"
            " returning current_revision_id",
            version,
            uid,
            sha,
            sealed,
            key,
        )
        await conn.execute(
            "insert into app.applications(id,user_id,company,role,jd_text)"
            " values($1,$2,'Example','Engineer','Build software')",
            application,
            uid,
        )
    digest = Digest(
        schema="digest.v1",
        version="sha256:test",
        text="Prepared memory",
        resume_revision_id=revision,
        resume_content_sha256=sha,
    )
    await stack.ctx.valkey.set(keys.resume_digest(uid, version), digest.model_dump_json())

    async def model(*args, **kwargs):
        return {"job_summary": "Build software"}

    monkeypatch.setattr(packs, "complete_json", model)
    load_digest = resumes.version_digest
    checked = False

    async def locked_digest(*args, **kwargs):
        nonlocal checked
        # Source replacement cannot commit between collecting the node snapshot
        # and reading the digest. PostgreSQL's lock timeout makes this deterministic.
        await stack.admin.execute("set lock_timeout='100ms'")
        try:
            with pytest.raises(asyncpg.LockNotAvailableError):
                await stack.admin.execute(
                    "update app.resume_versions set content_sha256=$2 where id=$1",
                    version,
                    "b" * 64,
                )
            checked = True
        finally:
            await stack.admin.execute("set lock_timeout=0")
        return await load_digest(*args, **kwargs)

    monkeypatch.setattr(resumes, "version_digest", locked_digest)
    built = await packs.build_interview_pack(stack.ctx, uid, f"job:{application}")
    assert checked
    assert built is not None
    assert built[0].resume_revision_id == revision

    monkeypatch.setattr(resumes, "version_digest", load_digest)
    await stack.admin.execute(
        "update app.resume_versions set content_sha256=$2 where id=$1", version, "b" * 64
    )
    with pytest.raises(RetryLater, match="extraction"):
        await packs.build_interview_pack(stack.ctx, uid, f"job:{application}")
    await stack.admin.execute(
        "update app.resume_versions set extracted_sha256=content_sha256 where id=$1", version
    )
    with pytest.raises(RetryLater, match="digest"):
        await packs.build_interview_pack(stack.ctx, uid, f"job:{application}")
