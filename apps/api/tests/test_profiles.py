"""Profiles (ARCHITECTURE.md, "Profiles"): "Me" and one profile per Career-OS user an admin
link reads; a request naming a profile reads and writes that profile's data only, while the
account's own things (settings, the Career-OS link, usage, deletion) stay the account's."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from copilot_api.testing import Api, FakeCareerOs

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


async def admin_account(api: Api, fake: FakeCareerOs) -> tuple[uuid.UUID, dict[str, str]]:
    uid, h = api.user()
    r = await api.client.post(
        "/v1/career-os/connect", headers=h, json={"code": fake.code_for("a1")}
    )
    assert r.status_code == 201, r.text
    return uid, h


async def listed(api: Api, h: dict[str, str]) -> list[dict[str, Any]]:
    r = await api.client.get("/v1/profiles", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def as_profile(h: dict[str, str], profile: dict[str, Any]) -> dict[str, str]:
    return {**h, "x-copilot-profile": profile["id"]}


async def test_without_an_admin_link_there_is_only_me(api: Api, fake: FakeCareerOs) -> None:
    uid, h = api.user("Alex")
    assert await listed(api, h) == [
        {"id": str(uid), "name": "Alex", "career_os_user_id": None, "me": True}
    ]
    # A user link is the same person: still only "Me", which is that Career-OS user.
    r = await api.client.post(
        "/v1/career-os/connect", headers=h, json={"code": fake.code_for("u1")}
    )
    assert r.status_code == 201
    [me] = await listed(api, h)
    assert (me["me"], me["career_os_user_id"]) == (True, "u1")


async def test_an_admin_link_lists_a_profile_per_user(api: Api, fake: FakeCareerOs) -> None:
    uid, h = await admin_account(api, fake)
    items = await listed(api, h)
    assert [(p["name"], p["me"]) for p in items] == [
        ("Alex", True), ("Alex Doe", False), ("Blair Roe", False)]  # fmt: skip
    assert items[0]["id"] == str(uid)
    # Each profile is a users row of its own, owned by the account.
    rows = await api.admin.fetch(
        "select id, owner_id, display_name from app.users where owner_id = $1 order by 3", uid
    )
    assert [(str(r["id"]), r["display_name"]) for r in rows] == [
        (items[1]["id"], "Alex Doe"), (items[2]["id"], "Blair Roe")]  # fmt: skip
    # Listing again makes nothing new; a user unlinked in Career-OS drops off, and a name
    # changed in Career-OS follows.
    assert await listed(api, h) == items
    fake.admin_users["a1"] = ["u1"]
    fake.users["u1"] = "Alex Doe-Smith"
    again = await listed(api, h)
    names = [(p["id"], p["name"]) for p in again]
    assert names == [(items[0]["id"], "Alex"), (items[1]["id"], "Alex Doe-Smith")]
    # Career-OS down: the profiles made so far.
    fake.down = True
    assert len(await listed(api, h)) == 3


async def test_each_profile_sees_only_its_own_data(api: Api, fake: FakeCareerOs) -> None:
    _, h = await admin_account(api, fake)
    _, alex, blair = await listed(api, h)
    body = {"company": "Globex", "role": "Staff Engineer", "jd_text": "Build the ledger"}
    r = await api.client.post("/v1/applications", headers=as_profile(h, alex), json=body)
    assert r.status_code == 201, r.text
    app_id = r.json()["id"]
    r = await api.client.post("/v1/resumes", headers=as_profile(h, alex),
                              json={"kind": "base", "text": "Alex's resume"})  # fmt: skip
    assert r.status_code in (200, 201), r.text

    def ids(r: Any) -> list[str]:
        return [a["id"] for a in r.json()["applications"]]

    assert ids(await api.client.get("/v1/applications", headers=as_profile(h, alex))) == [app_id]
    assert ids(await api.client.get("/v1/applications", headers=as_profile(h, blair))) == []
    assert ids(await api.client.get("/v1/applications", headers=h)) == []
    assert (await api.client.get("/v1/resumes", headers=as_profile(h, blair))).json()[
        "base"
    ] is None
    r = await api.client.get(f"/v1/applications/{app_id}", headers=as_profile(h, blair))
    assert r.status_code == 404
    # Jobs for the profile's data run as the profile.
    assert {m["user_id"] for m in api.jobs.sent if m.get("user_id")} <= {alex["id"]}


async def test_a_profile_must_be_the_accounts(api: Api, fake: FakeCareerOs) -> None:
    _, h = await admin_account(api, fake)
    _, alex, _ = await listed(api, h)
    _, other = api.user("Mallory")
    for headers in ({**other, "x-copilot-profile": alex["id"]},
                    {**h, "x-copilot-profile": str(uuid.uuid4())},
                    {**h, "x-copilot-profile": "not-a-uuid"}):  # fmt: skip
        r = await api.client.get("/v1/applications", headers=headers)
        assert r.status_code == 404, headers
    # The account's own id, or "me", is the account.
    me = (await listed(api, h))[0]
    assert (await api.client.get("/v1/applications", headers=as_profile(h, me))).status_code == 200


async def test_account_things_stay_the_accounts(api: Api, fake: FakeCareerOs) -> None:
    uid, h = await admin_account(api, fake)
    _, alex, _ = await listed(api, h)
    ah = as_profile(h, alex)
    me = (await api.client.get("/v1/me", headers=ah)).json()
    assert me["id"] == str(uid)
    conn = (await api.client.get("/v1/career-os/connection", headers=ah)).json()
    assert (conn["connected"], conn["account_type"]) == (True, "admin")
    # Settings are the account's, copied to its profiles for the services that read them.
    r = await api.client.patch("/v1/me/settings", headers=ah, json={"retention_days": 30})
    assert r.status_code == 200, r.text
    copied = await api.admin.fetchval(
        "select settings->>'retention_days' from app.users where id = $1", uuid.UUID(alex["id"])
    )
    assert copied == "30"
    # Usage counts the account's sessions and its profiles'.
    for who in (uid, uuid.UUID(alex["id"])):
        await api.admin.execute(
            "insert into app.sessions (id, user_id, mode, store_transcript, started_at, ended_at,"
            " billed_seconds) values ($1, $2, 'interview', false, now(), now(), 90)",
            uuid.uuid4(), who,
        )  # fmt: skip
    usage = (await api.client.get("/v1/usage", headers=ah)).json()
    assert (usage["sessions"], usage["live_seconds"]) == (2, 180)
    # Deleting the account (from any profile) deletes the account.
    r = await api.client.delete("/v1/me", headers=ah)
    assert r.status_code == 202
    [job] = api.take("user.delete")
    assert job["user_id"] == str(uid)
