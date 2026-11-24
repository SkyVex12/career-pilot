"""Source snapshots stay immutable and scoped after replacement and metadata updates."""

from uuid import UUID, uuid4

import asyncpg
import pytest

from copilot_api.testing import Api
from copilot_core.auth import Principal

pytestmark = pytest.mark.stack


async def test_resume_replacement_preserves_exact_prior_text(api: Api):
    uid, headers = api.user()
    _, other = api.user()
    response = await api.client.post(
        "/v1/resumes", headers=headers, json={"kind": "base", "text": "First experience"}
    )
    assert response.status_code == 201, response.text
    version, first = response.json()["id"], response.json()["revision_id"]
    response = await api.client.patch(
        f"/v1/resumes/{version}", headers=headers, json={"text": "Second experience"}
    )
    assert response.status_code == 200, response.text
    second = response.json()["revision_id"]
    assert first != second
    previous = await api.client.get(f"/v1/resumes/{version}/revisions/{first}", headers=headers)
    assert previous.json()["text"] == "First experience"
    citation = await api.client.get(f"/v1/ask/sources/resume/{version}:{first}", headers=headers)
    assert citation.status_code == 200, citation.text
    assert citation.json()["content"] == "First experience"
    latest = await api.client.get(f"/v1/resumes/{version}/revisions/{second}", headers=headers)
    assert latest.json()["text"] == "Second experience"
    assert (
        await api.client.get(f"/v1/resumes/{version}/revisions/{first}", headers=other)
    ).status_code == 404
    # Processing and labels are mutable container metadata; they do not manufacture revisions.
    await api.client.patch(f"/v1/resumes/{version}", headers=headers, json={"label": "Renamed"})
    listing = await api.client.get(f"/v1/resumes/{version}/revisions", headers=headers)
    assert [r["revision_number"] for r in listing.json()["revisions"]] == [2, 1]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with api.svc.tx(Principal(uid, "", None)) as conn:
            await conn.execute(
                "update app.resume_revisions set content_sha256='changed' where id=$1", UUID(first)
            )


async def test_session_pins_resume_before_later_edits(api: Api):
    uid, headers = api.user()
    created = await api.client.post(
        "/v1/resumes", headers=headers, json={"kind": "base", "text": "Interview version"}
    )
    assert created.status_code == 201, created.text
    version, revision = created.json()["id"], UUID(created.json()["revision_id"])
    sid = uuid4()
    async with api.svc.tx(Principal(uid, "", None)) as conn:
        await conn.execute(
            "insert into app.sessions(id,user_id,mode,store_transcript,started_at) "
            "values($1,$2,'interview',false,now())",
            sid,
            uid,
        )
    await api.client.patch(
        f"/v1/resumes/{version}", headers=headers, json={"text": "Edited after the call"}
    )
    assert (
        await api.admin.fetchval("select resume_revision_id from app.sessions where id=$1", sid)
        == revision
    )
    with pytest.raises(asyncpg.CheckViolationError):
        async with api.svc.tx(Principal(uid, "", None)) as conn:
            await conn.execute("update app.sessions set resume_revision_id=null where id=$1", sid)


async def test_work_log_edit_appends_revision(api: Api):
    uid, headers = api.user()
    employer = await api.client.post("/v1/employers", headers=headers, json={"name": "Company"})
    eid = employer.json()["id"]
    path = f"/v1/employers/{eid}/logs/2026-11-23"
    first = await api.client.put(path, headers=headers, json={"text": "First log"})
    assert first.status_code == 200, first.text
    second = await api.client.put(path, headers=headers, json={"text": "Second log"})
    assert second.status_code == 200, second.text
    count = await api.admin.fetchval(
        "select count(*) from app.work_log_revisions where workspace_id=$1", uid
    )
    assert count == 2
    other = await api.client.post("/v1/employers", headers=headers, json={"name": "Other"})
    log_id = await api.admin.fetchval("select id from app.work_logs where user_id=$1", uid)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await api.admin.execute(
            "insert into app.company_documents(id,user_id,employer_id,work_log_id,name_enc,"
            "key_version,s3_key,sha256,content_type,size_bytes) "
            "values($1,$2,$3,$4,$5,1,$6,'hash','text/plain',10)",
            uuid4(),
            uid,
            UUID(other.json()["id"]),
            log_id,
            b"sealed",
            f"test/{uuid4()}",
        )


async def test_acceptance_failure_rolls_back_domain_mutation(api: Api, monkeypatch):
    from unittest.mock import AsyncMock

    uid, headers = api.user()
    monkeypatch.setattr(api.svc, "accept", AsyncMock(side_effect=RuntimeError("DB interruption")))
    with pytest.raises(RuntimeError, match="DB interruption"):
        await api.client.post(
            "/v1/applications",
            headers=headers,
            json={"company": "Atomic", "role": "Engineer", "jd_text": "New role"},
        )
    assert (
        await api.admin.fetchval(
            "select count(*) from app.applications where user_id=$1",
            uid,
        )
        == 0
    )
    assert (
        await api.admin.fetchval(
            "select count(*) from app.operations where user_id=$1",
            uid,
        )
        == 0
    )
