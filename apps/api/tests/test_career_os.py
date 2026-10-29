"""The Career-OS link (ARCHITECTURE.md, "Career-OS link"): connect with a one-time code, search
and pull on demand, pull again after a change, disconnect on both sides. Career-OS is a fake
answering with the contract fixture's shapes (copilot_api.testing.FakeCareerOs)."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from copilot_api.testing import Api, FakeCareerOs
from copilot_core.storage import resume_file_key

pytestmark = pytest.mark.stack


@pytest.fixture
def fake(api: Api) -> Iterator[FakeCareerOs]:
    fake = FakeCareerOs()
    api.svc.career_os = fake.client()
    api.jobs.sent.clear()
    try:
        yield fake
    finally:
        api.svc.career_os = None


async def connect(api: Api, fake: FakeCareerOs, h: dict[str, str], user: str = "u1") -> None:
    r = await api.client.post("/v1/career-os/connect", headers=h,
                              json={"code": fake.code_for(user)})  # fmt: skip
    assert r.status_code == 201, r.text


async def pull(api: Api, h: dict[str, str], cos_id: str = "app_acme_1") -> dict[str, Any]:
    r = await api.client.post(f"/v1/career-os/applications/{cos_id}/pull", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_connect_with_a_one_time_code(api: Api, fake: FakeCareerOs) -> None:
    uid, h = api.user()
    assert (await api.client.get("/v1/career-os/connection", headers=h)).json() == {
        "connected": False, "available": True, "required": False}  # fmt: skip
    r = await api.client.post("/v1/career-os/connect", headers=h, json={"code": "WRONG-CODE"})
    assert r.status_code == 400
    code = fake.code_for("u1")
    r = await api.client.post("/v1/career-os/connect", headers=h, json={"code": code.lower()})
    assert r.status_code == 201, r.text
    conn = r.json()
    assert conn["connected"] is True
    assert conn["user"] == {"id": "u1", "name": "Alex Doe"}
    # The token is sealed with the user's data key.
    [token] = fake.tokens
    sealed = await api.admin.fetchval(
        "select token_enc from app.integrations where user_id = $1 and provider = 'career_os'", uid
    )
    assert token.encode() not in bytes(sealed)
    # The code is single use, and one account has one link.
    r = await api.client.post("/v1/career-os/connect", headers=h, json={"code": code})
    assert r.status_code == 409
    _, other = api.user()
    r = await api.client.post("/v1/career-os/connect", headers=other, json={"code": code})
    assert r.status_code == 400


async def test_search_and_pull_then_pull_again(api: Api, fake: FakeCareerOs) -> None:
    uid, h = api.user()
    await connect(api, fake, h)
    r = await api.client.get("/v1/career-os/applications", headers=h, params={"q": "acme"})
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert (item["id"], item["company"], item["stage"], item["resume"]) == (
        "app_acme_1", "Acme Payments", "interview", "tailored")  # fmt: skip
    assert item["pulled"] is None

    app = await pull(api, h)
    assert app["pulled"] == "new"
    fields = (app["company"], app["role"], app["stage"], app["source_site"], app["note"])
    assert fields == ("Acme Payments", "Senior Backend Engineer", "interview", "LinkedIn",
                      "Referred by Sam")  # fmt: skip
    assert app["jd_text"].startswith("Acme Payments is hiring")
    assert app["career_os"]["application_id"] == "app_acme_1"
    assert app["resume"]["kind"] == "tailored"
    version = uuid.UUID(app["resume"]["version_id"])
    row = await api.admin.fetchrow(
        "select source, career_os_ref, file_content_type, text_enc from app.resume_versions"
        " where id = $1",
        version,
    )
    assert row is not None
    assert (row["source"], row["career_os_ref"], row["file_content_type"]) == (
        "career_os", "resume_version:rv20261001090000", "application/pdf")  # fmt: skip
    assert b"zero downtime" not in bytes(row["text_enc"])  # sealed
    stored = await api.svc.store.get(resume_file_key(uid, version))
    assert stored is not None
    assert fake.pdf not in stored  # the file is sealed too
    r = await api.client.get(f"/v1/resumes/{version}/file", headers=h)
    assert r.content == fake.pdf
    assert len(api.take("resume.extract")) == 1
    assert len(api.take("pack.build")) == 1

    # Unchanged in Career-OS: search says so.
    [item] = (await api.client.get("/v1/career-os/applications", headers=h)).json()["items"]
    assert item["pulled"]["application_id"] == app["id"]
    assert item["pulled"]["changed"] is False

    # It changes in Career-OS (a later stage and a new JD): search and status show it; nothing
    # here changed on its own.
    detail = fake.apps["u1"]["app_acme_1"]
    detail["application"]["stage"] = "offer"
    detail["job_description"]["text"] = "Acme Payments: Staff Engineer now."
    [item] = (await api.client.get("/v1/career-os/applications", headers=h)).json()["items"]
    assert item["pulled"]["changed"] is True
    status = (await api.client.get("/v1/career-os/applications/app_acme_1/status",
                                   headers=h)).json()  # fmt: skip
    assert status["changed"] is True
    here = (await api.client.get(f"/v1/applications/{app['id']}", headers=h)).json()
    assert here["stage"] == "interview"

    # An interview and a manual pairing with the base stay through a pull again.
    await api.client.post("/v1/resumes", headers=h, json={"kind": "base", "text": "My base"})
    base = (await api.client.get("/v1/resumes", headers=h)).json()["base"]
    await api.client.patch(f"/v1/applications/{app['id']}", headers=h,
                           json={"resume_version_id": base["id"]})  # fmt: skip
    interview = {"job_id": app["id"], "round": "technical", "starts_at": "2026-12-01T10:00:00Z"}
    r = await api.client.post("/v1/interviews", headers=h, json=interview)
    assert r.status_code == 201
    api.jobs.sent.clear()
    again = await pull(api, h)
    assert again["pulled"] == "again"
    assert again["id"] == app["id"]
    assert (again["stage"], again["jd_text"]) == ("offer", "Acme Payments: Staff Engineer now.")
    assert again["resume"]["version_id"] == base["id"]
    assert again["resume"]["manual"] is True
    assert api.take("resume.extract") == []  # the resume did not change: not read again
    contexts = sorted(m["payload"]["context"].split(":")[0] for m in api.take("pack.build"))
    assert contexts == ["interview", "job"]  # the new JD rebuilds the packs
    [item] = (await api.client.get("/v1/career-os/applications", headers=h)).json()["items"]
    assert item["pulled"]["changed"] is False
    assert (
        await api.admin.fetchval("select count(*) from app.applications where user_id = $1", uid)
        == 1
    )

    # The resume changes in Career-OS: pulling again replaces the pulled copy, same version.
    detail["resume_version"]["text"] += "- New bullet\n"
    again = await pull(api, h)
    [extract] = api.take("resume.extract")
    assert extract["payload"]["resume_version_id"] == str(version)


async def test_base_resume_when_career_os_has_no_version(api: Api, fake: FakeCareerOs) -> None:
    detail = fake.apps["u1"]["app_acme_1"]
    detail["resume_version"] = None
    # No base here yet: the Career-OS base becomes this account's base.
    _, h = api.user()
    await connect(api, fake, h)
    app = await pull(api, h)
    assert app["resume"]["kind"] == "base"
    # An account with its own base keeps it; the Career-OS base is this application's own.
    _, h2 = api.user()
    await api.client.post("/v1/resumes", headers=h2, json={"kind": "base", "text": "Mine"})
    await connect(api, fake, h2)
    app = await pull(api, h2)
    assert app["resume"]["kind"] == "tailored"
    assert app["resume"]["label"].startswith("Career-OS base resume")
    base = (await api.client.get("/v1/resumes", headers=h2)).json()["base"]
    assert base["source"] == "text"


async def test_tokens_read_only_their_own_career_os_user(api: Api, fake: FakeCareerOs) -> None:
    _, a = api.user()
    _, b = api.user()
    await connect(api, fake, a, "u1")
    # B has no link: nothing to search or pull.
    assert (await api.client.get("/v1/career-os/applications", headers=b)).status_code == 409
    assert (await api.client.post("/v1/career-os/applications/app_acme_1/pull",
                                  headers=b)).status_code == 409  # fmt: skip
    await connect(api, fake, b, "u2")
    ids = [i["id"] for i in (await api.client.get("/v1/career-os/applications",
                                                  headers=b)).json()["items"]]  # fmt: skip
    assert ids == ["app_other_1"]
    # B's token cannot read A's Career-OS application.
    r = await api.client.post("/v1/career-os/applications/app_acme_1/pull", headers=b)
    assert r.status_code == 404
    await pull(api, a)
    assert (await api.client.get("/v1/applications", headers=b)).json()["applications"] == []


async def test_a_user_link_lists_only_its_user(api: Api, fake: FakeCareerOs) -> None:
    _, h = api.user()
    await connect(api, fake, h, "u1")
    conn = (await api.client.get("/v1/career-os/connection", headers=h)).json()
    assert (conn["account_type"], conn["user"]["id"]) == ("user", "u1")
    r = await api.client.get("/v1/career-os/users", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == [{"id": "u1", "name": "Alex Doe"}]
    [item] = (await api.client.get("/v1/career-os/applications", headers=h,
                                   params={"user_id": "u1"})).json()["items"]  # fmt: skip
    assert (item["id"], item["user"]) == ("app_acme_1", {"id": "u1", "name": "Alex Doe"})
    r = await api.client.get("/v1/career-os/applications", headers=h, params={"user_id": "u2"})
    assert r.status_code == 404


async def test_an_admin_link_reads_its_users_and_filters_by_user(
    api: Api, fake: FakeCareerOs
) -> None:
    uid, h = api.user()
    await connect(api, fake, h, "a1")
    conn = (await api.client.get("/v1/career-os/connection", headers=h)).json()
    assert (conn["account_type"], conn["user"]) == ("admin", {"id": "a1", "name": "Dana Admin"})
    users = (await api.client.get("/v1/career-os/users", headers=h)).json()["items"]
    assert users == [{"id": "u1", "name": "Alex Doe"}, {"id": "u2", "name": "Blair Roe"}]

    items = (await api.client.get("/v1/career-os/applications", headers=h)).json()["items"]
    assert {i["id"]: i["user"]["id"] for i in items} == {"app_acme_1": "u1", "app_other_1": "u2"}
    get = lambda who: api.client.get(  # noqa: E731
        "/v1/career-os/applications", headers=h, params={"user_id": who}
    )
    assert [i["id"] for i in (await get("u2")).json()["items"]] == ["app_other_1"]
    # A user not linked to the admin is out of reach, and ids are checked before Career-OS.
    assert (await get("u3")).status_code == 404
    assert (await api.client.post("/v1/career-os/applications/app_hooli_1/pull",
                                  headers=h)).status_code == 404  # fmt: skip
    assert (await get("../users")).status_code == 422

    # Each pull lands in its Career-OS user's profile (made on the first pull), with that
    # user's base resume as the profile's base; the account itself gets nothing.
    first = await pull(api, h, "app_other_1")
    assert first["resume"]["kind"] == "base"
    fake.apps["u1"]["app_acme_1"]["resume_version"] = None
    second = await pull(api, h, "app_acme_1")
    assert second["resume"]["kind"] == "base"
    owned = {r["career_os_user_id"]: r["id"] for r in await api.admin.fetch(
        "select id, career_os_user_id from app.profiles where user_id = $1", uid)}  # fmt: skip
    assert set(owned) == {"u1", "u2"}
    assert (first["profile_id"], second["profile_id"]) == (str(owned["u2"]), str(owned["u1"]))
    assert (await api.client.get("/v1/resumes", headers=h)).json()["base"] is None
    assert (await api.client.get("/v1/applications", headers=h)).json()["applications"] == []
    as_u2 = {**h, "x-copilot-profile": str(owned["u2"])}
    base = (await api.client.get("/v1/resumes", headers=as_u2)).json()["base"]
    assert base["career_os"]["ref"] == "base:career-os"
    [mine] = (await api.client.get("/v1/applications", headers=as_u2)).json()["applications"]
    assert mine["id"] == first["id"]
    # Search marks each pull, wherever it lives; switched to a profile it shows its user only.
    items = (await api.client.get("/v1/career-os/applications", headers=h)).json()["items"]
    assert all(i["pulled"] is not None for i in items)
    assert {i["profile_id"] for i in items} == {str(owned["u1"]), str(owned["u2"])}
    items = (await api.client.get("/v1/career-os/applications", headers=as_u2)).json()["items"]
    assert [i["id"] for i in items] == ["app_other_1"]

    # Unlinked in Career-OS: gone from the users and the search at once.
    fake.admin_users["a1"] = ["u1"]
    users = (await api.client.get("/v1/career-os/users", headers=h)).json()["items"]
    assert [u["id"] for u in users] == ["u1"]
    assert (await get("u2")).status_code == 404


async def test_disconnect_revokes_on_both_sides(api: Api, fake: FakeCareerOs) -> None:
    uid, h = api.user()
    await connect(api, fake, h)
    [token] = fake.tokens
    fake.down = True
    r = await api.client.delete("/v1/career-os/connection", headers=h)
    assert r.status_code == 502  # never gone here while still valid there
    assert (await api.client.get("/v1/career-os/connection", headers=h)).json()["connected"]
    fake.down = False
    assert (await api.client.delete("/v1/career-os/connection", headers=h)).status_code == 204
    assert token in fake.revoked
    assert (
        await api.admin.fetchval("select count(*) from app.integrations where user_id = $1", uid)
        == 0
    )
    assert (await api.client.get("/v1/career-os/applications", headers=h)).status_code == 409


async def test_a_link_revoked_in_career_os_asks_to_connect_again(
    api: Api, fake: FakeCareerOs
) -> None:
    _, h = api.user()
    await connect(api, fake, h)
    fake.revoked.update(fake.tokens)
    r = await api.client.get("/v1/career-os/applications", headers=h)
    assert r.status_code == 409
    conn = (await api.client.get("/v1/career-os/connection", headers=h)).json()
    assert (conn["connected"], conn["status"]) == (False, "revoked")
    # Disconnecting needs no call to Career-OS then, and a new code connects again.
    assert (await api.client.delete("/v1/career-os/connection", headers=h)).status_code == 204
    await connect(api, fake, h)


async def test_without_career_os_configured(api: Api) -> None:
    _, h = api.user()
    assert (await api.client.get("/v1/career-os/connection", headers=h)).json() == {
        "connected": False, "available": False, "required": False}  # fmt: skip
    r = await api.client.post("/v1/career-os/connect", headers=h, json={"code": "ABCDEFGH"})
    assert r.status_code == 503


@pytest.fixture
def required(api: Api) -> Iterator[None]:
    api.svc.settings.require_career_os = True
    try:
        yield
    finally:
        api.svc.settings.require_career_os = False


async def test_a_required_link_gates_the_work_until_connected(
    api: Api, fake: FakeCareerOs, required: None
) -> None:
    _, h = api.user()
    # Without a link: the work is refused with a message asking to link the account.
    for path in ("/v1/applications", "/v1/resumes", "/v1/sessions", "/v1/employers"):
        r = await api.client.get(path, headers=h)
        assert r.status_code == 403, (path, r.text)
        assert "link your Career-OS account" in r.json()["detail"]
    # Settings, profiles and the link itself stay open, so the app can link the account.
    for path in ("/v1/me", "/v1/profiles", "/v1/career-os/connection"):
        assert (await api.client.get(path, headers=h)).status_code == 200, path
    conn = (await api.client.get("/v1/career-os/connection", headers=h)).json()
    assert (conn["connected"], conn["required"]) == (False, True)

    await connect(api, fake, h)
    assert (await api.client.get("/v1/applications", headers=h)).status_code == 200
    # A link Career-OS no longer accepts counts as none.
    fake.revoked.update(fake.tokens)
    assert (await api.client.get("/v1/career-os/applications", headers=h)).status_code == 409
    assert (await api.client.get("/v1/applications", headers=h)).status_code == 403
