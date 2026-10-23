"""Applications, resume versions and their pairing (ARCHITECTURE.md, "Resume versions")."""

import hashlib
import uuid
from typing import Any

import httpx2
import pytest

from copilot_api.testing import Api
from copilot_core import resumes

pytestmark = pytest.mark.stack

JD = "Globex needs a Staff Engineer for payments: Python, PostgreSQL, Kafka."
RESUME = "Alex Doe\nStaff Engineer\n- Moved the ledger with zero downtime; p95 down 40%\n"


async def create(api: Api, h: dict[str, str], **over: Any) -> dict[str, Any]:
    body = {"company": "Globex", "role": "Staff Engineer", "jd_text": JD, **over}
    r = await api.client.post("/v1/applications", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def base(api: Api, h: dict[str, str], text: str = RESUME) -> dict[str, Any]:
    r = await api.client.post("/v1/resumes", headers=h, json={"kind": "base", "text": text})
    assert r.status_code == 201, r.text
    return r.json()


async def test_manual_application_with_a_pasted_resume(api: Api) -> None:
    _, h = api.user()
    b = await base(api, h)
    [extract] = api.take("resume.extract")
    assert extract["payload"] == {"resume_version_id": b["id"],
                                  "content_sha256": resumes.sha256_text(RESUME)}  # fmt: skip
    app = await create(
        api, h, url="https://jobs.example.com/1", source_site="LinkedIn", stage="interview",
        note="Referred", resume_text="Tailored for Globex", resume_label="Globex cut",
    )  # fmt: skip
    assert (app["stage"], app["source_site"], app["note"]) == ("interview", "LinkedIn", "Referred")
    assert app["jd_text"] == JD
    assert app["resume"]["kind"] == "tailored"  # its own version wins over the base
    assert app["resume"]["label"] == "Globex cut"
    assert app["resume"]["manual"] is False
    assert app["career_os"] is None
    assert {v["kind"] for v in app["versions"]} == {"base", "tailored"}
    [extract] = api.take("resume.extract")
    assert extract["payload"]["resume_version_id"] == app["resume"]["version_id"]
    [build] = api.take("pack.build")
    assert build["payload"]["context"] == f"job:{app['id']}"
    assert f":{uuid.UUID(app['resume']['version_id']).hex[:12]}:" in build["idempotency_key"]
    # The pasted text is sealed, never stored as written.
    sealed = await api.admin.fetchval(
        "select text_enc from app.resume_versions where id = $1",
        uuid.UUID(app["resume"]["version_id"]),
    )
    assert b"Tailored for Globex" not in bytes(sealed)
    r = await api.client.get(f"/v1/resumes/{app['resume']['version_id']}", headers=h)
    assert r.json()["text"] == "Tailored for Globex"
    listed = (await api.client.get("/v1/applications", headers=h)).json()["applications"]
    assert [a["id"] for a in listed] == [app["id"]]
    # Older desktop builds still see it as a job.
    jobs = (await api.client.get("/v1/jobs", headers=h)).json()["jobs"]
    assert [(j["id"], j["title"]) for j in jobs] == [(app["id"], "Staff Engineer")]


async def test_pairing_changes_and_rules(api: Api) -> None:
    _, h = api.user()
    b = await base(api, h)
    one = await create(api, h, resume_text="For one")
    two = await create(api, h, company="Initech", resume_text="For two")
    api.take("pack.build")
    assert one["resume"]["kind"] == "tailored"

    r = await api.client.patch(f"/v1/applications/{one['id']}", headers=h,
                               json={"resume_version_id": b["id"]})  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["resume"] == {**r.json()["resume"], "version_id": b["id"], "manual": True}
    [build] = api.take("pack.build")
    assert f":{uuid.UUID(b['id']).hex[:12]}:" in build["idempotency_key"]
    assert build["idempotency_key"].endswith(":r1")
    # Another application's tailored version is refused; so is a version that is not theirs.
    r = await api.client.patch(f"/v1/applications/{one['id']}", headers=h,
                               json={"resume_version_id": two["resume"]["version_id"]})  # fmt: skip
    assert r.status_code == 422
    _, other = api.user()
    theirs = await base(api, other)
    r = await api.client.patch(f"/v1/applications/{one['id']}", headers=h,
                               json={"resume_version_id": theirs["id"]})  # fmt: skip
    assert r.status_code == 404
    assert api.take("pack.build") == []
    # Back to automatic: the tailored version again, under a new revision.
    r = await api.client.patch(f"/v1/applications/{one['id']}", headers=h,
                               json={"resume_version_id": "auto"})  # fmt: skip
    assert r.json()["resume"]["version_id"] == one["resume"]["version_id"]
    [build] = api.take("pack.build")
    assert build["idempotency_key"].endswith(":r2")
    # A new JD rebuilds the pack; other fields alone do not.
    r = await api.client.patch(f"/v1/applications/{one['id']}", headers=h, json={"stage": "offer"})
    assert r.json()["stage"] == "offer"
    assert api.take("pack.build") == []
    await api.client.patch(f"/v1/applications/{one['id']}", headers=h, json={"jd_text": "New JD"})
    assert len(api.take("pack.build")) == 1


async def test_deleting_a_version_falls_back_and_rebuilds(api: Api) -> None:
    _, h = api.user()
    b = await base(api, h)
    app = await create(api, h, resume_text="Tailored")
    api.take("pack.build")
    r = await api.client.delete(f"/v1/resumes/{app['resume']['version_id']}", headers=h)
    assert r.status_code == 204
    after = (await api.client.get(f"/v1/applications/{app['id']}", headers=h)).json()
    assert after["resume"]["version_id"] == b["id"]
    [build] = api.take("pack.build")
    assert f":{uuid.UUID(b['id']).hex[:12]}:" in build["idempotency_key"]
    # Deleting the application deletes its tailored versions too.
    again = await create(api, h, resume_text="Another")
    assert (
        await api.client.delete(f"/v1/applications/{again['id']}", headers=h)
    ).status_code == 204
    assert (
        await api.admin.fetchval(
            "select count(*) from app.resume_versions where application_id = $1",
            uuid.UUID(again["id"]),
        )
        == 0
    )
    listed = (await api.client.get("/v1/resumes", headers=h)).json()
    assert listed["base"]["id"] == b["id"]
    assert listed["versions"] == []


async def test_uploaded_resume_for_an_application(api: Api) -> None:
    api.jobs.sent.clear()  # what earlier tests left
    _, h = api.user()
    app = await create(api, h)
    body = b"# Alex\nTailored resume file\n"
    r = await api.client.post("/v1/documents", headers=h, json={
        "kind": "resume", "content_type": "text/markdown", "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(), "application_id": app["id"],
        "label": "Globex PDF"})  # fmt: skip
    assert r.status_code == 201, r.text
    upload = r.json()["upload"]
    async with httpx2.AsyncClient() as c:
        put = await c.request(upload["method"], upload["url"], headers=upload["headers"],
                              content=body)  # fmt: skip
        assert put.status_code in (200, 204)
    doc = r.json()["document_id"]
    r = await api.client.post(f"/v1/documents/{doc}/ingest", headers=h)
    assert r.status_code == 202, r.text
    version = r.json()["resume_version_id"]
    detail = (await api.client.get(f"/v1/applications/{app['id']}", headers=h)).json()
    assert detail["resume"]["version_id"] == version
    assert detail["resume"]["label"] == "Globex PDF"
    [extract] = api.take("resume.extract")
    assert extract["payload"]["resume_version_id"] == version
    # A job description for another user's application is refused.
    _, other = api.user()
    r = await api.client.post("/v1/documents", headers=other, json={
        "kind": "job_description", "content_type": "text/plain", "size_bytes": 3,
        "sha256": "0" * 64, "application_id": app["id"]})  # fmt: skip
    assert r.status_code == 404


async def test_applications_and_resumes_are_per_user(api: Api) -> None:
    _, a = api.user()
    _, b = api.user()
    await base(api, a)
    app = await create(api, a, resume_text="Mine")
    assert (await api.client.get("/v1/applications", headers=b)).json()["applications"] == []
    assert (await api.client.get("/v1/resumes", headers=b)).json() == {"base": None, "versions": []}
    for method, path, body in (
        ("GET", f"/v1/applications/{app['id']}", None),
        ("PATCH", f"/v1/applications/{app['id']}", {"stage": "offer"}),
        ("DELETE", f"/v1/applications/{app['id']}", None),
        ("GET", f"/v1/resumes/{app['resume']['version_id']}", None),
        ("PATCH", f"/v1/resumes/{app['resume']['version_id']}", {"label": "x"}),
        ("DELETE", f"/v1/resumes/{app['resume']['version_id']}", None),
        ("POST", "/v1/resumes", {"kind": "tailored", "application_id": app["id"], "text": "x"}),
    ):
        r = await api.client.request(method, path, headers=b, json=body)
        assert r.status_code == 404, f"{method} {path}: {r.status_code}"
    still = (await api.client.get(f"/v1/applications/{app['id']}", headers=a)).json()
    assert still["stage"] == "applied"
    assert still["resume"]["label"] == "Tailored resume"
