"""Career-OS sign-in against the stack (ARCHITECTURE.md, "Career-OS sign-in"): which account
a sign-in lands in (the five cases), the two conflicts, codes and assertions that must be
refused, the tokens and their rotation, the unlink guard, and row-level security on what the
sign-in reads before it knows the user. Career-OS is the fake (copilot_api.testing)."""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from copilot_api import main
from copilot_api.careeros_signin import CareerOsSignIn, mask_email
from copilot_api.emailauth import Limit
from copilot_api.testing import Api, FakeCareerOs, api_settings
from copilot_api.tokens import TokenService
from copilot_core.auth import (
    AuthError,
    CopilotVerifier,
    Principal,
    email_public_keys,
    google_user_id,
)
from copilot_core.db import sign_in_tx, user_tx

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]

SIGN_IN = "/v1/auth/career-os/sign-in"
COMPLETE = "/v1/auth/career-os/complete"
IP = {"X-Forwarded-For": "203.0.113.9"}


@pytest_asyncio.fixture(loop_scope="session")
async def fake(api: Api) -> AsyncIterator[FakeCareerOs]:
    """Career-OS sign-in switched on, against a fresh fake Career-OS. Its user ids repeat from
    test to test, so each starts without their sign-ins and links."""
    assert api.svc.tokens is not None
    fake = FakeCareerOs()
    # Addresses are unique in app.users, and the tests share a database.
    tag = uuid.uuid4().hex[:8]
    fake.emails = {k: v.replace("@", f".{tag}@") for k, v in fake.emails.items()}
    client = fake.client()
    settings = api.svc.settings
    settings.career_os_site_url = "https://career-os.test"
    await api.admin.execute("delete from app.identities where provider = 'career_os'")
    await api.admin.execute("delete from app.integrations where provider = 'career_os'")
    keys = [k async for k in api.svc.valkey.scan_iter("auth:*")]
    if keys:
        await api.svc.valkey.delete(*keys)
    api.svc.career_os = client
    api.svc.cos_sign_in = CareerOsSignIn(
        settings, api.svc.pool, api.svc.valkey, client, api.svc.crypto, api.svc.tokens
    )
    try:
        yield fake
    finally:
        api.svc.career_os = None
        api.svc.cos_sign_in = None
        api.svc.tokens.checks.clear()
        settings.career_os_site_url = None


async def cos(
    api: Api, fake: FakeCareerOs, account: str, headers: dict[str, str] | None = None
) -> Any:
    """The app's part after the browser: the code and its verifier, to the API."""
    code, verifier = fake.approve(account)
    return await api.client.post(
        SIGN_IN,
        json={"code": code, "code_verifier": verifier, "redirect_uri": fake.REDIRECT},
        headers={**IP, **(headers or {})},
    )


async def signed_in(api: Api, fake: FakeCareerOs, account: str) -> dict[str, Any]:
    r = await cos(api, fake, account)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "signed_in", r.text
    return r.json()


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def me(api: Api, headers: dict[str, str]) -> dict[str, Any]:
    r = await api.client.get("/v1/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def google_account(api: Api, email: str | None = None) -> tuple[uuid.UUID, dict[str, str]]:
    """An account that signs in with Google (its identity recorded), and its headers."""
    sub = str(uuid.uuid4().int)[:21]
    token = f"google-{sub}"
    api.verifier.principals[token] = Principal(
        google_user_id(sub), "Google User", None,
        email=email or f"{uuid.uuid4().hex[:12]}@gmail.example", provider="google", subject=sub,
        new_identity=True,
    )  # fmt: skip
    headers = {"Authorization": f"Bearer {token}"}
    return uuid.UUID((await me(api, headers))["id"]), headers


async def link_by_code(api: Api, fake: FakeCareerOs, headers: dict[str, str], account: str) -> None:
    r = await api.client.post(
        "/v1/career-os/connect", headers=headers, json={"code": fake.code_for(account)}
    )
    assert r.status_code == 201, r.text


async def identity_of(api: Api, subject: str) -> uuid.UUID | None:
    return await api.admin.fetchval(
        "select user_id from app.identities where provider = 'career_os' and subject = $1", subject
    )


async def link_of(api: Api, user_id: uuid.UUID) -> tuple[str, str, str] | None:
    row = await api.admin.fetchrow(
        "select status, external_kind, external_id from app.integrations"
        " where user_id = $1 and provider = 'career_os'",
        user_id,
    )
    return None if row is None else (row["status"], row["external_kind"], row["external_id"])


async def accounts(api: Api) -> int:
    return int(await api.admin.fetchval("select count(*) from app.users where owner_id is null"))


def code_of(r: Any) -> tuple[int, str]:
    return r.status_code, r.json()["detail"]["code"]


# --- Which account ------------------------------------------------------------------------------


async def test_case_5_a_new_person_lands_signed_in_and_connected(
    api: Api, fake: FakeCareerOs, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    before = await accounts(api)
    out = await signed_in(api, fake, "u1")
    assert (out["account"], out["token_type"], out["name"]) == ("new", "Bearer", "Alex Doe")
    assert out["expires_in"] == 3600
    assert await accounts(api) == before + 1
    mine = await me(api, bearer(out))
    uid = uuid.UUID(mine["id"])
    assert mine["display_name"] == "Alex Doe"
    # Career-OS's address is not verified: it is not the account's.
    assert fake.emails["u1"] not in mine["email"]
    assert mine["email"].endswith("@career-os.invalid")
    assert [(m["provider"], m["label"]) for m in mine["sign_in_methods"]] == [
        ("career_os", "Alex Doe")
    ]
    assert await identity_of(api, "user:u1") == uid
    # Connected, with no second approval: today's link, for that Career-OS user.
    assert await link_of(api, uid) == ("active", "user", "u1")
    conn = (await api.client.get("/v1/career-os/connection", headers=bearer(out))).json()
    assert (conn["connected"], conn["account_type"], conn["user"]["id"]) == (True, "user", "u1")
    found = (await api.client.get("/v1/career-os/applications", headers=bearer(out))).json()
    assert [i["id"] for i in found["items"]] == ["app_acme_1"]
    # A required link is satisfied at once.
    api.svc.settings.require_career_os = True
    try:
        assert (await api.client.get("/v1/applications", headers=bearer(out))).status_code == 200
    finally:
        api.svc.settings.require_career_os = False
    # The decision is logged, with ids only.
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("copilot_api.careeros")]
    assert any(f"case 5, new user {uid} for principal user:u1" in line for line in lines)
    assert not any("@" in line or "Alex" in line for line in lines)


async def test_case_1_the_same_career_os_account_is_the_same_account_every_time(
    api: Api, fake: FakeCareerOs
) -> None:
    first = await signed_in(api, fake, "u1")
    uid = (await me(api, bearer(first)))["id"]
    before = await accounts(api)
    again = await signed_in(api, fake, "u1")
    assert again["account"] == "existing"
    assert (await me(api, bearer(again)))["id"] == uid
    assert await accounts(api) == before
    # An admin is another principal, even with the id a user has.
    admin = await signed_in(api, fake, "a1")
    assert (await me(api, bearer(admin)))["id"] != uid
    assert await identity_of(api, "admin:a1") == uuid.UUID((await me(api, bearer(admin)))["id"])
    conn = (await api.client.get("/v1/career-os/connection", headers=bearer(admin))).json()
    assert conn["account_type"] == "admin"


async def test_case_2_the_one_linked_account_whatever_its_email(
    api: Api, fake: FakeCareerOs, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    uid, google = await google_account(api, "someone-else@gmail.example")
    await link_by_code(api, fake, google, "u1")
    before = await accounts(api)
    out = await signed_in(api, fake, "u1")
    assert out["account"] == "existing"
    assert uuid.UUID((await me(api, bearer(out)))["id"]) == uid
    assert await accounts(api) == before
    assert await identity_of(api, "user:u1") == uid
    assert await link_of(api, uid) == ("active", "user", "u1")
    methods = (await me(api, google))["sign_in_methods"]
    assert [m["provider"] for m in methods] == ["google", "career_os"]
    assert any(f"case 2, user {uid}" in r.getMessage() for r in caplog.records)
    # A revoked link is not a link: the next person with that Career-OS account is new.
    other, google2 = await google_account(api)
    await link_by_code(api, fake, google2, "u2")
    await api.admin.execute(
        "update app.integrations set status = 'revoked' where user_id = $1", other
    )
    fresh = await signed_in(api, fake, "u2")
    assert fresh["account"] == "new"


async def test_case_3_several_linked_accounts_are_never_picked_for_the_person(
    api: Api, fake: FakeCareerOs
) -> None:
    a, google_a = await google_account(api, "anna.first@gmail.example")
    b, google_b = await google_account(api, "bo@work.example")
    stranger, google_c = await google_account(api)
    await link_by_code(api, fake, google_a, "u1")
    await link_by_code(api, fake, google_b, "u1")
    r = await cos(api, fake, "u1")
    assert r.status_code == 200, r.text
    asked = r.json()
    assert asked["status"] == "choose_account"
    assert "access_token" not in asked
    shown = {s["email"]: s for s in asked["accounts"]}
    assert set(shown) == {"a•••@g•••.example", "b•••@w•••.example"}
    for s in shown.values():
        assert s["methods"] == ["google"]
        assert s["created_at"] == time.strftime("%Y-%m-%d", time.gmtime())
        assert str(a) not in s["id"] and str(b) not in s["id"]
    assert await identity_of(api, "user:u1") is None  # nothing recorded yet

    pick_a = shown["a•••@g•••.example"]["id"]
    body = {"ticket": asked["ticket"], "choice": "existing", "account_id": pick_a}
    # No proof, the other linked account's proof, a stranger's, a made-up choice, a new one.
    assert code_of(await api.client.post(COMPLETE, json=body, headers=IP)) == (401, "proof_required")
    r = await api.client.post(COMPLETE, json=body, headers={**IP, **google_b})
    assert code_of(r) == (409, "wrong_account")
    r = await api.client.post(COMPLETE, json=body, headers={**IP, **google_c})
    assert code_of(r) == (409, "wrong_account")
    r = await api.client.post(
        COMPLETE, json={**body, "account_id": "nope"}, headers={**IP, **google_a}
    )
    assert code_of(r) == (400, "not_a_choice")
    r = await api.client.post(COMPLETE, json={**body, "choice": "new"}, headers=IP)
    assert code_of(r) == (400, "not_a_choice")
    assert await identity_of(api, "user:u1") is None

    r = await api.client.post(COMPLETE, json=body, headers={**IP, **google_a})
    assert (r.status_code, r.json()) == (200, {"status": "added"})
    assert await identity_of(api, "user:u1") == a
    # The ticket is spent, and from now on the sign-in goes straight to the proven account.
    r = await api.client.post(COMPLETE, json=body, headers={**IP, **google_a})
    assert code_of(r) == (400, "ticket_expired")
    out = await signed_in(api, fake, "u1")
    assert uuid.UUID((await me(api, bearer(out)))["id"]) == a
    assert stranger not in (a, b)


async def test_case_4_the_same_email_is_never_attached_silently(
    api: Api, fake: FakeCareerOs
) -> None:
    uid, google = await google_account(api, fake.emails["u1"])
    before = await accounts(api)
    r = await cos(api, fake, "u1")
    asked = r.json()
    assert (r.status_code, asked["status"]) == (200, "existing_or_new")
    # Career-OS does not verify addresses: a hint, so nothing about the account is shown.
    assert (asked["verified"], asked["account"]) == (False, None)
    assert "access_token" not in asked
    assert await identity_of(api, "user:u1") is None
    assert await link_of(api, uid) is None
    assert await accounts(api) == before

    # "Create a separate account": a new one, and the existing one is not touched.
    r = await api.client.post(
        COMPLETE, json={"ticket": asked["ticket"], "choice": "new"}, headers=IP
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["status"], out["account"]) == ("signed_in", "new")
    new_id = uuid.UUID((await me(api, bearer(out)))["id"])
    assert new_id != uid
    assert await accounts(api) == before + 1
    assert await identity_of(api, "user:u1") == new_id
    assert await link_of(api, uid) is None
    assert [m["provider"] for m in (await me(api, google))["sign_in_methods"]] == ["google"]
    assert (await me(api, google))["email"] == fake.emails["u1"]


async def test_case_4_use_my_existing_account_needs_its_own_sign_in(
    api: Api, fake: FakeCareerOs
) -> None:
    uid, google = await google_account(api, fake.emails["u2"])
    asked = (await cos(api, fake, "u2")).json()
    body = {"ticket": asked["ticket"], "choice": "existing"}
    assert code_of(await api.client.post(COMPLETE, json=body, headers=IP)) == (401, "proof_required")
    # A Career-OS sign-in is not that account's own method.
    elsewhere = await signed_in(api, fake, "u3")
    r = await api.client.post(COMPLETE, json=body, headers={**IP, **bearer(elsewhere)})
    assert code_of(r) == (401, "proof_required")
    assert await identity_of(api, "user:u2") is None

    before = await accounts(api)
    r = await api.client.post(COMPLETE, json=body, headers={**IP, **google})
    assert (r.status_code, r.json()) == (200, {"status": "added"})
    assert await identity_of(api, "user:u2") == uid
    assert await link_of(api, uid) == ("active", "user", "u2")
    assert await accounts(api) == before
    out = await signed_in(api, fake, "u2")
    assert uuid.UUID((await me(api, bearer(out)))["id"]) == uid


async def test_case_4_a_verified_email_names_the_account_and_only_it_will_do(
    api: Api, fake: FakeCareerOs
) -> None:
    fake.email_verified = True
    uid, google = await google_account(api, fake.emails["u1"])
    _, other = await google_account(api)
    asked = (await cos(api, fake, "u1")).json()
    assert (asked["status"], asked["verified"]) == ("existing_or_new", True)
    assert asked["account"]["email"] == "a•••@e•••.com"
    assert asked["account"]["methods"] == ["google"]
    body = {"ticket": asked["ticket"], "choice": "existing"}
    r = await api.client.post(COMPLETE, json=body, headers={**IP, **other})
    assert code_of(r) == (409, "wrong_account")
    assert await identity_of(api, "user:u1") is None
    r = await api.client.post(COMPLETE, json=body, headers={**IP, **google})
    assert r.json() == {"status": "added"}
    assert await identity_of(api, "user:u1") == uid


async def test_an_unverified_email_is_only_a_hint(api: Api, fake: FakeCareerOs) -> None:
    """Someone can sign up on Career-OS with another person's address. It gets them an
    offer, never that person's account: only an account they can sign in to themselves."""
    victim, _ = await google_account(api, fake.emails["u1"])
    mine, google_mine = await google_account(api)
    asked = (await cos(api, fake, "u1")).json()
    assert asked["status"] == "existing_or_new"
    r = await api.client.post(
        COMPLETE, json={"ticket": asked["ticket"], "choice": "existing"},
        headers={**IP, **google_mine},
    )  # fmt: skip
    assert r.json() == {"status": "added"}
    assert await identity_of(api, "user:u1") == mine
    assert await link_of(api, victim) is None
    assert [
        r["provider"]
        for r in await api.admin.fetch(
            "select provider from app.identities where user_id = $1", victim
        )
    ] == ["google"]


# --- Conflicts ----------------------------------------------------------------------------------


async def test_an_account_linked_to_another_career_os_account_is_refused(
    api: Api, fake: FakeCareerOs
) -> None:
    uid, google = await google_account(api, fake.emails["u1"])
    await link_by_code(api, fake, google, "u2")  # linked to Blair, has Alex's email
    asked = (await cos(api, fake, "u1")).json()
    assert asked["status"] == "existing_or_new"
    r = await api.client.post(
        COMPLETE, json={"ticket": asked["ticket"], "choice": "existing"}, headers={**IP, **google}
    )
    assert code_of(r) == (409, "linked_to_another")
    assert "different Career-OS account" in r.json()["detail"]["message"]
    assert await identity_of(api, "user:u1") is None
    assert await link_of(api, uid) == ("active", "user", "u2")
    # The same refusal when the account, signed in, tries to add the sign-in itself.
    r = await cos(api, fake, "u1", google)
    assert code_of(r) == (409, "linked_to_another")
    # No link token was left behind on Career-OS for the refused sign-ins.
    assert sorted(fake.tokens.values()) == ["u2"]


async def test_a_disabled_or_deleted_career_os_account_is_refused(
    api: Api, fake: FakeCareerOs
) -> None:
    before = await accounts(api)
    fake.disabled.add("u1")
    r = await cos(api, fake, "u1")
    assert code_of(r) == (403, "account_disabled")
    assert "disabled or no longer exists" in r.json()["detail"]["message"]
    assert await accounts(api) == before
    assert await identity_of(api, "user:u1") is None


async def test_an_identity_never_moves_to_another_account(api: Api, fake: FakeCareerOs) -> None:
    first = await signed_in(api, fake, "u1")
    owner = uuid.UUID((await me(api, bearer(first)))["id"])
    other, google = await google_account(api)
    r = await cos(api, fake, "u1", google)  # another account tries to add the same sign-in
    assert code_of(r) == (409, "identity_elsewhere")
    assert await identity_of(api, "user:u1") == owner
    assert await link_of(api, other) is None
    # Nor does linking by code move it: the sign-in still lands in its account.
    await link_by_code(api, fake, google, "u1")
    again = await signed_in(api, fake, "u1")
    assert uuid.UUID((await me(api, bearer(again)))["id"]) == owner
    assert await identity_of(api, "user:u1") == owner


# --- Adding the sign-in to an account, and unlinking --------------------------------------------


async def test_someone_signed_in_another_way_adds_career_os_sign_in(
    api: Api, fake: FakeCareerOs
) -> None:
    uid, google = await google_account(api)
    r = await cos(api, fake, "a1", google)
    assert (r.status_code, r.json()) == (200, {"status": "added"})
    assert await identity_of(api, "admin:a1") == uid
    assert await link_of(api, uid) == ("active", "admin", "a1")  # the same as linking
    methods = (await me(api, google))["sign_in_methods"]
    assert [(m["provider"], m["label"]) for m in methods][1] == ("career_os", "Dana Admin")
    out = await signed_in(api, fake, "a1")
    assert uuid.UUID((await me(api, bearer(out)))["id"]) == uid
    # A token that is not valid is not "no token".
    r = await cos(api, fake, "u1", {"Authorization": "Bearer nonsense"})
    assert r.status_code == 401


async def test_unlinking_is_blocked_while_career_os_is_the_only_way_in(
    api: Api, fake: FakeCareerOs
) -> None:
    out = await signed_in(api, fake, "u1")
    uid = uuid.UUID((await me(api, bearer(out)))["id"])
    r = await api.client.delete("/v1/career-os/connection", headers=bearer(out))
    assert code_of(r) == (409, "only_sign_in_method")
    assert await link_of(api, uid) == ("active", "user", "u1")
    assert await identity_of(api, "user:u1") == uid
    assert not fake.revoked

    # With another way in, unlinking works, and takes the Career-OS sign-in with it.
    gid, google = await google_account(api)
    assert (await cos(api, fake, "u2", google)).json() == {"status": "added"}
    signed = await signed_in(api, fake, "u2")
    r = await api.client.delete("/v1/career-os/connection", headers=google)
    assert r.status_code == 204
    assert await link_of(api, gid) is None
    assert await identity_of(api, "user:u2") is None
    assert [m["provider"] for m in (await me(api, google))["sign_in_methods"]] == ["google"]
    r = await api.client.post(
        "/v1/auth/token/refresh", json={"refresh_token": signed["refresh_token"]}
    )
    assert code_of(r) == (401, "invalid_refresh_token")


async def test_a_code_link_cannot_disagree_with_the_sign_in(api: Api, fake: FakeCareerOs) -> None:
    out = await signed_in(api, fake, "u1")
    uid = uuid.UUID((await me(api, bearer(out)))["id"])
    await api.admin.execute("update app.integrations set status = 'revoked' where user_id = $1", uid)
    r = await api.client.post(
        "/v1/career-os/connect", headers=bearer(out), json={"code": fake.code_for("u2")}
    )
    assert r.status_code == 409
    assert await link_of(api, uid) == ("revoked", "user", "u1")
    await link_by_code(api, fake, bearer(out), "u1")  # its own Career-OS account is fine


# --- Codes and assertions that must be refused --------------------------------------------------


async def test_a_code_works_once_and_only_with_its_verifier(api: Api, fake: FakeCareerOs) -> None:
    code, verifier = fake.approve("u1")
    body = {"code": code, "code_verifier": verifier, "redirect_uri": fake.REDIRECT}
    assert (await api.client.post(SIGN_IN, json=body, headers=IP)).status_code == 200
    replayed = await api.client.post(SIGN_IN, json=body, headers=IP)
    assert code_of(replayed) == (400, "invalid_grant")
    # Expired on Career-OS looks the same: it no longer knows the code.
    code, verifier = fake.approve("u2")
    fake.sign_in_codes.clear()
    r = await api.client.post(SIGN_IN, json={**body, "code": code, "code_verifier": verifier}, headers=IP)
    assert code_of(r) == (400, "invalid_grant")
    code, verifier = fake.approve("u2")
    r = await api.client.post(SIGN_IN, json={**body, "code": code, "code_verifier": "x" * 43}, headers=IP)
    assert code_of(r) == (400, "invalid_grant")
    code, verifier = fake.approve("u2")
    r = await api.client.post(
        SIGN_IN, headers=IP,
        json={"code": code, "code_verifier": verifier, "redirect_uri": "http://127.0.0.1:1/callback"},
    )  # fmt: skip
    assert code_of(r) == (400, "invalid_grant")
    assert await identity_of(api, "user:u2") is None


@pytest.mark.parametrize(
    "how", ["another_key", "unknown_kid", "wrong_audience", "wrong_issuer", "expired",
            "too_long", "other_account", "no_jti", "not_a_jwt"],
)  # fmt: skip
async def test_a_forged_or_misdirected_assertion_is_refused(
    api: Api, fake: FakeCareerOs, how: str
) -> None:
    attacker = Ed25519PrivateKey.generate()
    now = time.time()

    def forged(account: str) -> str:
        if how == "another_key":  # the real key id, an attacker's key
            return fake.assertion(account, key=attacker)
        if how == "unknown_kid":
            return fake.assertion(account, key=attacker, kid=fake.public_key(attacker)[0])
        if how == "wrong_audience":
            return fake.assertion(account, aud="another-app")
        if how == "wrong_issuer":
            return fake.assertion(account, iss="someone-else")
        if how == "expired":
            return fake.assertion(account, now=now - 600)
        if how == "too_long":
            return fake.assertion(account, exp=int(now) + 86_400)
        if how == "other_account":  # signed, but for another account than its subject
            return fake.assertion(account, principal_id="u2")
        if how == "no_jti":
            return fake.assertion(account, jti=None)
        return "not.a.jwt"

    fake.assert_with = forged
    before = await accounts(api)
    r = await cos(api, fake, "u1")
    assert code_of(r) == (502, "invalid_assertion"), how
    assert await accounts(api) == before
    assert await identity_of(api, "user:u1") is None
    assert await identity_of(api, "user:u2") is None
    assert not fake.tokens  # the link code was never exchanged


async def test_an_assertion_is_accepted_once(api: Api, fake: FakeCareerOs) -> None:
    kept: list[str] = []

    def same(account: str) -> str:
        if not kept:
            kept.append(fake.assertion(account))
        return kept[0]

    fake.assert_with = same
    assert (await cos(api, fake, "u1")).status_code == 200
    assert code_of(await cos(api, fake, "u1")) == (502, "invalid_assertion")


async def test_a_link_for_another_account_than_the_assertions_is_dropped(
    api: Api, fake: FakeCareerOs
) -> None:
    """The assertion says Alex; if the link code turned out to be Blair's, nothing is kept."""
    real = fake.code_for
    fake.code_for = lambda user: real("u2")  # type: ignore[method-assign]
    r = await cos(api, fake, "u1")
    assert code_of(r) == (502, "invalid_assertion")
    assert set(fake.tokens) == fake.revoked
    assert await identity_of(api, "user:u1") is None


async def test_the_key_is_fetched_again_when_career_os_changes_it(
    api: Api, fake: FakeCareerOs
) -> None:
    assert (await cos(api, fake, "u1")).status_code == 200
    assert api.svc.cos_sign_in is not None
    fake.sign_in_key = Ed25519PrivateKey.generate()
    # An unknown key id refetches, but not more than once a minute.
    assert code_of(await cos(api, fake, "u1")) == (502, "invalid_assertion")
    api.svc.cos_sign_in.assertions.fetched_at = None
    assert (await cos(api, fake, "u1")).status_code == 200


# --- Career-OS down, limits, config -------------------------------------------------------------


async def test_when_career_os_is_down_the_button_says_so_and_other_sign_ins_work(
    api: Api, fake: FakeCareerOs
) -> None:
    out = await signed_in(api, fake, "u1")
    fake.down = True
    r = await cos(api, fake, "u1")
    assert code_of(r) == (503, "career_os_unavailable")
    assert "Career-OS could not be reached" in r.json()["detail"]["message"]
    # Whoever is signed in stays signed in, and can refresh.
    assert (await api.client.get("/v1/me", headers=bearer(out))).status_code == 200
    r = await api.client.post("/v1/auth/token/refresh", json={"refresh_token": out["refresh_token"]})
    assert r.status_code == 200
    _, google = await google_account(api)
    assert (await api.client.get("/v1/me", headers=google)).status_code == 200
    assert (await api.client.get("/v1/auth/config")).status_code == 200
    r = await api.client.post(
        "/v1/auth/email/start", json={"email": "sam@example.com"}, headers=IP
    )
    assert r.status_code == 200


async def test_sign_in_attempts_are_limited_per_client(api: Api, fake: FakeCareerOs) -> None:
    assert api.svc.cos_sign_in is not None
    api.svc.cos_sign_in.limit = (Limit(2, 900),)
    body = {"code": "x", "code_verifier": "v" * 43, "redirect_uri": fake.REDIRECT}
    for _ in range(2):
        assert code_of(await api.client.post(SIGN_IN, json=body, headers=IP)) == (400, "invalid_grant")
    r = await api.client.post(SIGN_IN, json=body, headers=IP)
    assert code_of(r) == (429, "rate_limited")
    assert int(r.headers["retry-after"]) > 0
    other = {"X-Forwarded-For": "198.51.100.4"}
    assert code_of(await api.client.post(SIGN_IN, json=body, headers=other)) == (400, "invalid_grant")


async def test_auth_config_adds_the_option_and_is_unchanged_without_it(
    api: Api, fake: FakeCareerOs
) -> None:
    config = (await api.client.get("/v1/auth/config")).json()
    assert config["career_os"] == {
        "authorize_url": "https://career-os.test/career-pilot/authorize",
        "client_id": "career-pilot",
    }
    with_it = {k: v for k, v in config.items() if k != "career_os"}
    api.svc.cos_sign_in = None
    without = (await api.client.get("/v1/auth/config")).json()
    assert "career_os" not in without
    assert without == with_it  # an installed app sees what it saw before
    for path in (SIGN_IN, COMPLETE):
        body = {"code": "x", "code_verifier": "v" * 43, "redirect_uri": "r", "ticket": "t" * 30,
                "choice": "new"}  # fmt: skip
        keys = ("ticket", "choice") if path == COMPLETE else ("code", "code_verifier", "redirect_uri")
        r = await api.client.post(path, json={k: body[k] for k in keys}, headers=IP)
        assert r.status_code == 404


async def test_career_os_sign_in_is_off_beside_an_allow_list(api: Api, fake: FakeCareerOs) -> None:
    """An allow-list lists verified addresses, and a Career-OS sign-in has none."""
    assert api.svc.tokens is not None
    key = api.svc.settings.email_signing_key
    listed = api_settings(
        career_os_sign_in=True, career_os_url="https://career-os.test",
        career_os_site_url="https://career-os.test", email_signing_key=key,
        google_allowed_emails=["sam@example.com"],
    )  # fmt: skip
    made = main.career_os_sign_in(
        listed, api.svc.pool, api.svc.valkey, fake.client(), api.svc.crypto, api.svc.tokens
    )
    assert made is None
    api.svc.tokens.checks.pop("career_os", None)
    # And a token from before the list was set stops working on every service.
    out = await signed_in(api, fake, "u1")
    strict = CopilotVerifier(email_public_keys(api.svc.settings), ["sam@example.com"])
    with pytest.raises(AuthError, match="allow-list"):
        await strict.verify(out["access_token"])
    open_ = CopilotVerifier(email_public_keys(api.svc.settings), [])
    principal = await open_.verify(out["access_token"])
    assert (principal.provider, principal.subject, principal.email) == ("career_os", "user:u1", None)
    with pytest.raises(RuntimeError, match="SITE_URL"):
        CareerOsSignIn(
            api_settings(email_signing_key=key), api.svc.pool, api.svc.valkey, fake.client(),
            api.svc.crypto, TokenService(api.svc.settings, api.svc.pool),
        )  # fmt: skip
    with pytest.raises(RuntimeError, match="CAREER_OS_URL"):
        main.career_os_sign_in(
            api_settings(career_os_sign_in=True), api.svc.pool, api.svc.valkey, None,
            api.svc.crypto, api.svc.tokens,
        )  # fmt: skip


# --- Tokens -------------------------------------------------------------------------------------


async def test_refresh_rotates_and_reuse_revokes_the_chain(api: Api, fake: FakeCareerOs) -> None:
    out = await signed_in(api, fake, "u1")
    uid = (await me(api, bearer(out)))["id"]

    async def refresh(token: str) -> Any:
        return await api.client.post("/v1/auth/token/refresh", json={"refresh_token": token})

    r = await refresh(out["refresh_token"])
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["refresh_token"] != out["refresh_token"]
    assert (await me(api, bearer(second)))["id"] == uid
    # The API's refresh endpoint is one endpoint under two names.
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert r.status_code == 200
    third = r.json()
    # The first token again: someone has a copy. Every token of the sign-in ends.
    assert code_of(await refresh(out["refresh_token"])) == (401, "invalid_refresh_token")
    assert code_of(await refresh(third["refresh_token"])) == (401, "invalid_refresh_token")
    stored = await api.admin.fetch(
        "select token_hash, provider, subject, email from app.refresh_tokens where user_id = $1",
        uuid.UUID(uid),
    )
    assert {(r["provider"], r["subject"], r["email"]) for r in stored} == {
        ("career_os", "user:u1", None)
    }
    for token in (out, second, third):
        assert token["refresh_token"] not in {r["token_hash"] for r in stored}  # only hashes

    # Signing out ends a sign-in too.
    again = await signed_in(api, fake, "u1")
    body = {"refresh_token": again["refresh_token"]}
    assert (await api.client.post("/v1/auth/token/signout", json=body)).status_code == 204
    assert code_of(await refresh(again["refresh_token"])) == (401, "invalid_refresh_token")


async def test_a_sign_in_ends_when_career_os_disables_the_account(
    api: Api, fake: FakeCareerOs
) -> None:
    out = await signed_in(api, fake, "u1")
    fake.disabled.add("u1")
    r = await api.client.post("/v1/auth/token/refresh", json={"refresh_token": out["refresh_token"]})
    assert code_of(r) == (401, "invalid_refresh_token")
    fake.disabled.clear()
    r = await api.client.post("/v1/auth/token/refresh", json={"refresh_token": out["refresh_token"]})
    assert r.status_code == 401  # revoked, not just refused
    # An account that was deleted is not brought back by its token.
    other = await signed_in(api, fake, "u2")
    uid = uuid.UUID((await me(api, bearer(other)))["id"])
    await api.admin.execute("delete from app.users where id = $1", uid)
    assert (await api.client.get("/v1/me", headers=bearer(other))).status_code == 401
    assert await identity_of(api, "user:u2") is None


# --- What is kept, and who can read it ----------------------------------------------------------


async def test_a_pending_choice_is_sealed_in_valkey_and_ends(api: Api, fake: FakeCareerOs) -> None:
    await google_account(api, fake.emails["u1"])
    asked = (await cos(api, fake, "u1")).json()
    keys = [k async for k in api.svc.valkey.scan_iter("auth:cos:ticket:*")]
    assert len(keys) == 1
    stored = repr(keys) + repr(await api.svc.valkey.get(keys[0]))
    link_code = next(iter(fake.codes))
    for secret in (asked["ticket"], link_code, "Alex", fake.emails["u1"], "user:u1"):
        assert secret not in stored
    assert 0 < await api.svc.valkey.ttl(keys[0]) <= 8 * 60
    # Another ticket opens nothing, and a ticket that ran out is gone.
    body = {"ticket": "t" * 43, "choice": "new"}
    assert code_of(await api.client.post(COMPLETE, json=body, headers=IP)) == (400, "ticket_expired")
    await api.svc.valkey.delete(keys[0])
    body = {"ticket": asked["ticket"], "choice": "new"}
    assert code_of(await api.client.post(COMPLETE, json=body, headers=IP)) == (400, "ticket_expired")
    # So is one whose link code ran out on Career-OS: nothing half-made is left.
    asked = (await cos(api, fake, "u1")).json()
    fake.codes.clear()
    before = await accounts(api)
    r = await api.client.post(COMPLETE, json={"ticket": asked["ticket"], "choice": "new"}, headers=IP)
    assert code_of(r) == (400, "ticket_expired")
    assert await accounts(api) == before
    assert await identity_of(api, "user:u1") is None


async def test_sign_in_reads_only_the_links_of_the_account_signing_in(
    api: Api, fake: FakeCareerOs
) -> None:
    """Row-level security as the API's own role: before the user is known, a transaction sees
    the Career-OS links whose account it names, read-only, and nothing else of anyone's."""
    a, google_a = await google_account(api)
    b, google_b = await google_account(api)
    c, google_c = await google_account(api)
    await link_by_code(api, fake, google_a, "u1")
    await link_by_code(api, fake, google_b, "u2")
    await link_by_code(api, fake, google_c, "a1")
    async with sign_in_tx(api.svc.pool, "") as conn:
        assert await conn.fetch("select 1 from app.integrations") == []
    async with sign_in_tx(api.svc.pool, "career_os_link:user:u1") as conn:
        rows = await conn.fetch("select user_id from app.integrations")
        assert [r["user_id"] for r in rows] == [a]
        assert await conn.fetch("select 1 from app.users") == []
        assert await conn.fetch("select 1 from app.identities") == []
        assert await conn.execute("delete from app.integrations") == "DELETE 0"
        status = await conn.execute("update app.integrations set status = 'revoked'")
        assert status == "UPDATE 0"
    # The kind is part of the key: a user's key does not open an admin's link.
    async with sign_in_tx(api.svc.pool, "career_os_link:user:a1") as conn:
        assert await conn.fetch("select 1 from app.integrations") == []
    async with sign_in_tx(api.svc.pool, "career_os_link:admin:a1") as conn:
        assert [r["user_id"] for r in await conn.fetch("select user_id from app.integrations")] == [c]
    # A Career-OS identity is seen with its own key, and a user sees only their own.
    out = await signed_in(api, fake, "u3")
    mine = uuid.UUID((await me(api, bearer(out)))["id"])
    async with sign_in_tx(api.svc.pool, "career_os:user:u3") as conn:
        rows = await conn.fetch("select user_id, email from app.identities")
        assert [(r["user_id"], r["email"]) for r in rows] == [(mine, None)]
    async with sign_in_tx(api.svc.pool, "career_os:user:u1") as conn:
        assert await conn.fetch("select 1 from app.identities") == []
    async with user_tx(api.svc.pool, a) as conn:
        assert [r["user_id"] for r in await conn.fetch("select user_id from app.integrations")] == [a]
        rows = await conn.fetch("select user_id from app.identities")
        assert {r["user_id"] for r in rows} == {a}
        with pytest.raises(Exception, match="row-level security"):
            await conn.execute(
                "insert into app.identities (provider, subject, user_id, email)"
                " values ('career_os', 'user:zz', $1, null)",
                b,
            )


async def test_an_account_has_at_most_one_career_os_sign_in(api: Api, fake: FakeCareerOs) -> None:
    out = await signed_in(api, fake, "u1")
    uid = uuid.UUID((await me(api, bearer(out)))["id"])
    with pytest.raises(Exception, match="identities_one_career_os"):
        await api.admin.execute(
            "insert into app.identities (provider, subject, user_id, email)"
            " values ('career_os', 'user:other', $1, null)",
            uid,
        )
    with pytest.raises(Exception, match="identities_email_by_provider"):
        await api.admin.execute(
            "insert into app.identities (provider, subject, user_id, email)"
            " values ('career_os', 'user:mail', $1, 'a@b.co')",
            uid,
        )


async def test_masked_emails() -> None:
    assert mask_email("anna.first@gmail.com") == "a•••@g•••.com"
    assert mask_email("b@x.co.uk") == "b•••@x•••.uk"
    assert mask_email("root@localhost") == "r•••@l•••"
    assert mask_email("career-os-0192@career-os.invalid") is None
    assert mask_email("profile-1@profiles.invalid") is None
    assert mask_email("") is None
    assert mask_email(None) is None
