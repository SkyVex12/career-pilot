"""Email sign-in against the stack: the code's life, the limits, tokens and their rotation,
which account an address signs in to, and that Google sign-in is untouched."""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from copilot_api.emailauth import EmailSignIn, Limit, Limits
from copilot_api.mailer import LogMailer, SmtpMailer, make_mailer, sign_in_message
from copilot_api.testing import Api, api_settings
from copilot_core import identities
from copilot_core.auth import Principal, google_user_id
from copilot_core.db import sign_in_tx, user_tx
from copilot_core.identities import normalize_email

pytestmark = [pytest.mark.stack, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean(api: Api) -> AsyncIterator[None]:
    """Each test starts without codes, counters or mail, and with the real settings."""
    assert api.svc.email is not None
    keys = [k async for k in api.svc.valkey.scan_iter("auth:*")]
    if keys:
        await api.svc.valkey.delete(*keys)
    api.mailer.sent.clear()
    api.mailer.fail = False
    saved = (api.svc.email.limits, api.svc.email.allowed, api.svc.settings.email_code_ttl_s)
    yield
    api.svc.email.limits, api.svc.email.allowed, api.svc.settings.email_code_ttl_s = saved


def address() -> str:
    return f"{uuid.uuid4().hex[:12]}@example.com"


async def post(api: Api, path: str, body: dict[str, Any], ip: str = "203.0.113.7") -> Any:
    return await api.client.post(
        f"/v1/auth/email/{path}", json=body, headers={"X-Forwarded-For": ip}
    )


async def code_for(api: Api, email: str, ip: str = "203.0.113.7") -> str:
    """Ask for a code and read it from the mail."""
    r = await post(api, "start", {"email": email}, ip)
    assert r.status_code == 200, r.text
    assert api.svc.email is not None
    await api.svc.email.drain()
    to, _, text = api.mailer.sent[-1]
    assert to == normalize_email(email)
    found = re.search(r"\b(\d{6})\b", text)
    assert found is not None
    return found.group(1)


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def sign_in(api: Api, email: str) -> dict[str, Any]:
    r = await post(api, "verify", {"email": email, "code": await code_for(api, email)})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --- The code -----------------------------------------------------------------------------------


async def test_a_code_signs_in_once(api: Api) -> None:
    email = address()
    r = await post(api, "start", {"email": f"  {email.upper()} "})
    assert r.json() == {"sent": True, "code_length": 6, "expires_in_s": 600, "resend_after_s": 60}
    assert api.svc.email is not None
    await api.svc.email.drain()
    to, subject, text = api.mailer.sent[-1]
    assert (to, subject) == (email, "Your CoRuby sign-in code")
    assert "expires in 10 minutes" in text
    assert "ignore this email" in text
    code = re.search(r"\b(\d{6})\b", text).group(1)  # type: ignore[union-attr]
    assert code not in subject

    r = await post(api, "verify", {"email": email, "code": f"{code[:3]} {code[3:]}"})
    assert r.status_code == 200
    tokens = r.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["email"] == email
    me = (await api.client.get("/v1/me", headers=bearer(tokens))).json()
    assert me["email"] == email
    assert me["display_name"] == email.split("@")[0]

    # Single use.
    again = await post(api, "verify", {"email": email, "code": code})
    assert again.status_code == 400
    assert again.json()["detail"]["code"] == "code_expired"


async def test_the_code_is_stored_only_as_a_hash(api: Api) -> None:
    email = address()
    code = await code_for(api, email)
    keys = [k async for k in api.svc.valkey.scan_iter("auth:code:*")]
    assert len(keys) == 1
    stored = await api.svc.valkey.hgetall(keys[0])
    blob = repr(keys) + repr(stored)
    assert code not in blob
    assert email not in blob
    assert 0 < await api.svc.valkey.ttl(keys[0]) <= 600


async def test_a_wrong_code_and_an_expired_code_say_so(api: Api) -> None:
    email = address()
    api.svc.settings.email_code_ttl_s = 1
    code = await code_for(api, email)
    r = await post(api, "verify", {"email": email, "code": wrong(code)})
    assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_code")
    r = await post(api, "verify", {"email": email, "code": "12ab"})
    assert (r.status_code, r.json()["detail"]["code"]) == (400, "invalid_code")
    await asyncio.sleep(1.3)
    r = await post(api, "verify", {"email": email, "code": code})
    assert (r.status_code, r.json()["detail"]["code"]) == (400, "code_expired")


async def test_a_new_code_replaces_the_old_one(api: Api) -> None:
    email = address()
    first = await code_for(api, email)
    second = await code_for(api, email)
    if first != second:
        r = await post(api, "verify", {"email": email, "code": first})
        assert r.json()["detail"]["code"] == "invalid_code"
    assert (await post(api, "verify", {"email": email, "code": second})).status_code == 200


async def test_five_wrong_tries_kill_the_code(api: Api) -> None:
    email = address()
    code = await code_for(api, email)
    for _ in range(4):
        r = await post(api, "verify", {"email": email, "code": wrong(code)})
        assert r.json()["detail"]["code"] == "invalid_code"
    r = await post(api, "verify", {"email": email, "code": wrong(code)})
    assert (r.status_code, r.json()["detail"]["code"]) == (400, "code_locked")
    # The right code is dead too.
    r = await post(api, "verify", {"email": email, "code": code})
    assert r.json()["detail"]["code"] == "code_locked"
    # A new code works.
    assert (await sign_in(api, email))["email"] == email


async def test_start_answers_the_same_for_any_address(api: Api) -> None:
    assert api.svc.email is not None
    known = address()
    await sign_in(api, known)  # has an account
    api.svc.email.allowed = {known}
    outsider = address()  # not on the allow-list
    answers = [
        (r.status_code, r.json())
        for r in [
            await post(api, "start", {"email": known}, ip="198.51.100.1"),
            await post(api, "start", {"email": address()}, ip="198.51.100.2"),
            await post(api, "start", {"email": outsider}, ip="198.51.100.3"),
        ]
    ]
    assert answers[0] == answers[1] == answers[2]
    assert answers[0][0] == 200
    await api.svc.email.drain()
    # Nothing was mailed to the address that may not sign in, and it has no code.
    assert outsider not in [to for to, _, _ in api.mailer.sent]
    r = await post(api, "verify", {"email": outsider, "code": "123456"})
    assert r.json()["detail"]["code"] == "code_expired"


async def test_a_failed_mail_changes_nothing_in_the_answer(api: Api) -> None:
    api.mailer.fail = True
    r = await post(api, "start", {"email": address()})
    assert (r.status_code, r.json()["sent"]) == (200, True)
    assert api.svc.email is not None
    await api.svc.email.drain()
    assert api.mailer.sent == []


async def test_not_an_address(api: Api) -> None:
    for bad in ("", "nobody", "a@b", "two@@example.com", "a b@example.com"):
        r = await post(api, "start", {"email": bad})
        assert (r.status_code, r.json()["detail"]["code"]) == (422, "invalid_email"), bad


# --- Limits -------------------------------------------------------------------------------------


async def test_start_is_limited_per_address(api: Api) -> None:
    email = address()
    for n in range(3):  # 3 per 15 minutes, from any client address
        assert (await post(api, "start", {"email": email}, ip=f"192.0.2.{n}")).status_code == 200
    r = await post(api, "start", {"email": email}, ip="192.0.2.99")
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert detail["code"] == "rate_limited"
    assert 0 < detail["retry_after_s"] <= 900
    assert r.headers["retry-after"] == str(detail["retry_after_s"])
    # Another address is not held back by it.
    assert (await post(api, "start", {"email": address()}, ip="192.0.2.99")).status_code == 200


async def test_start_is_limited_per_day_per_address(api: Api) -> None:
    assert api.svc.email is not None
    api.svc.email.limits = Limits(start_address=(Limit(100, 900), Limit(10, 86400)))
    email = address()
    for n in range(10):
        assert (await post(api, "start", {"email": email}, ip=f"192.0.2.{n}")).status_code == 200
    r = await post(api, "start", {"email": email}, ip="192.0.2.200")
    assert r.status_code == 429
    assert r.json()["detail"]["retry_after_s"] > 900


async def test_start_is_limited_per_client(api: Api) -> None:
    for _ in range(10):
        assert (await post(api, "start", {"email": address()}, ip="192.0.2.50")).status_code == 200
    assert (await post(api, "start", {"email": address()}, ip="192.0.2.50")).status_code == 429
    assert (await post(api, "start", {"email": address()}, ip="192.0.2.51")).status_code == 200


async def test_verify_is_limited_per_address_and_per_client(api: Api) -> None:
    assert api.svc.email is not None
    api.svc.email.limits = Limits(verify_address=(Limit(2, 900),), verify_client=(Limit(4, 900),))
    email = address()
    for n in range(2):
        r = await post(api, "verify", {"email": email, "code": "000000"}, ip=f"192.0.2.{n}")
        assert r.status_code == 400
    r = await post(api, "verify", {"email": email, "code": "000000"}, ip="192.0.2.9")
    assert (r.status_code, r.json()["detail"]["code"]) == (429, "rate_limited")
    for _ in range(4):
        r = await post(api, "verify", {"email": address(), "code": "000000"}, ip="192.0.2.77")
        assert r.status_code == 400
    r = await post(api, "verify", {"email": address(), "code": "000000"}, ip="192.0.2.77")
    assert r.status_code == 429


# --- Tokens -------------------------------------------------------------------------------------


async def test_refresh_rotates_and_reuse_revokes_the_chain(api: Api) -> None:
    email = address()
    first = await sign_in(api, email)
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert r.status_code == 200
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (await api.client.get("/v1/me", headers=bearer(second))).status_code == 200
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": second["refresh_token"]}
    )
    third = r.json()

    # The first token again: it leaked, so the whole chain goes, the newest token with it.
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert (r.status_code, r.json()["detail"]["code"]) == (401, "invalid_refresh_token")
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": third["refresh_token"]}
    )
    assert r.status_code == 401
    # Another sign-in of the same account is its own chain and is untouched.
    other = await sign_in(api, email)
    await api.client.post("/v1/auth/email/refresh", json={"refresh_token": first["refresh_token"]})
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": other["refresh_token"]}
    )
    assert r.status_code == 200
    # Only hashes are stored.
    rows = await api.admin.fetch("select token_hash from app.refresh_tokens")
    hashes = {r["token_hash"] for r in rows}
    assert first["refresh_token"] not in hashes
    assert all(len(h) == 64 for h in hashes)


async def test_unknown_and_expired_refresh_tokens(api: Api) -> None:
    r = await api.client.post("/v1/auth/email/refresh", json={"refresh_token": "nope" * 8})
    assert r.status_code == 401
    tokens = await sign_in(api, address())
    await api.admin.execute(
        "update app.refresh_tokens set expires_at = now() - interval '1 second'"
        " where email = $1", tokens["email"],
    )  # fmt: skip
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r.status_code == 401


async def test_sign_out_revokes_the_refresh_token(api: Api) -> None:
    tokens = await sign_in(api, address())
    body = {"refresh_token": tokens["refresh_token"]}
    assert (await api.client.post("/v1/auth/email/signout", json=body)).status_code == 204
    assert (await api.client.post("/v1/auth/email/refresh", json=body)).status_code == 401
    # Unknown tokens sign out quietly too.
    r = await api.client.post("/v1/auth/email/signout", json={"refresh_token": "x" * 40})
    assert r.status_code == 204


async def test_leaving_the_allow_list_ends_the_sign_in(api: Api) -> None:
    assert api.svc.email is not None
    email = address()
    tokens = await sign_in(api, email)
    api.svc.email.allowed = {"someone-else@example.com"}
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r.status_code == 401
    api.svc.email.allowed = set()
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r.status_code == 401  # it was revoked, not just refused


async def test_a_deleted_account_is_not_brought_back_by_its_token(api: Api) -> None:
    tokens = await sign_in(api, address())
    me = (await api.client.get("/v1/me", headers=bearer(tokens))).json()
    await api.admin.execute("delete from app.users where id = $1", uuid.UUID(me["id"]))
    r = await api.client.get("/v1/me", headers=bearer(tokens))
    assert r.status_code == 401
    r = await api.client.post(
        "/v1/auth/email/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r.status_code == 401


# --- Accounts -----------------------------------------------------------------------------------


async def test_the_same_address_is_the_same_account_every_time(api: Api) -> None:
    email = address()
    a = (await api.client.get("/v1/me", headers=bearer(await sign_in(api, email)))).json()
    b = (await api.client.get("/v1/me", headers=bearer(await sign_in(api, email.upper())))).json()
    assert a["id"] == b["id"]
    other = (await api.client.get("/v1/me", headers=bearer(await sign_in(api, address())))).json()
    assert other["id"] != a["id"]


async def google_principal(email: str, sub: str | None = None) -> Principal:
    sub = sub or str(uuid.uuid4().int)[:21]
    return Principal(
        google_user_id(sub), "Alex", None, email=email, provider="google", subject=sub,
        new_identity=True,
    )  # fmt: skip


async def test_email_sign_in_joins_the_account_of_the_same_google_address(api: Api) -> None:
    """The rule: an email sign-in whose address is an account's verified Google email signs
    in to that account."""
    email = address()
    google = await google_principal(email)
    token = f"google-{google.subject}"
    api.verifier.principals[token] = google
    me = (await api.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["id"] == str(google.user_id)

    tokens = await sign_in(api, email)
    again = (await api.client.get("/v1/me", headers=bearer(tokens))).json()
    assert again["id"] == str(google.user_id)
    rows = await api.admin.fetch(
        "select provider, subject from app.identities where user_id = $1 order by provider",
        google.user_id,
    )
    assert [(r["provider"], r["subject"]) for r in rows] == [
        ("email", email),
        ("google", google.subject),
    ]


async def test_google_sign_in_joins_the_account_an_email_sign_in_made(api: Api) -> None:
    email = address()
    mine = (await api.client.get("/v1/me", headers=bearer(await sign_in(api, email)))).json()
    google = await google_principal(email)
    token = f"google-{google.subject}"
    api.verifier.principals[token] = google
    me = (await api.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["id"] == mine["id"]
    assert me["id"] != str(google.user_id)
    # Recorded, so every service resolves this Google sign-in to the account from now on.
    lookup = identities.Identities(api.svc.pool)
    assert google.subject is not None
    assert str(await lookup.user_of("google", google.subject)) == mine["id"]


async def test_an_account_from_before_identities_gets_its_real_subject(api: Api) -> None:
    """Migration 0014's row for an existing Google account has the account's id as its
    subject; the next Google sign-in replaces it, and the email match works before that."""
    email = address()
    google = await google_principal(email)
    await api.admin.execute(
        "insert into app.users (id, email, display_name) values ($1, $2, 'Alex')",
        google.user_id, email,
    )  # fmt: skip
    await api.admin.execute(
        "insert into app.identities (provider, subject, user_id, email)"
        " values ('google', $1, $2, $3)",
        str(google.user_id), google.user_id, email,
    )  # fmt: skip
    # Email first: matched by the verified address on the migration's row.
    me = (await api.client.get("/v1/me", headers=bearer(await sign_in(api, email)))).json()
    assert me["id"] == str(google.user_id)
    # Then Google: same account, and the row now has Google's sub.
    token = f"google-{google.subject}"
    api.verifier.principals[token] = google
    me = (await api.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["id"] == str(google.user_id)
    subjects = await api.admin.fetch(
        "select subject from app.identities where user_id = $1 and provider = 'google'",
        google.user_id,
    )
    assert [r["subject"] for r in subjects] == [google.subject]


async def test_sign_in_rows_are_seen_only_with_their_key(api: Api) -> None:
    """Row-level security as the API's own role: without the key a sign-in transaction sees
    nothing; with it, that one row; a user sees only their own."""
    a, b = address(), address()
    ta, tb = await sign_in(api, a), await sign_in(api, b)
    ida = uuid.UUID((await api.client.get("/v1/me", headers=bearer(ta))).json()["id"])
    idb = uuid.UUID((await api.client.get("/v1/me", headers=bearer(tb))).json()["id"])
    async with sign_in_tx(api.svc.pool, "") as conn:
        assert await conn.fetch("select 1 from app.identities") == []
        assert await conn.fetch("select 1 from app.refresh_tokens") == []
    async with sign_in_tx(api.svc.pool, f"email:{a}") as conn:
        rows = await conn.fetch("select user_id from app.identities")
        assert [r["user_id"] for r in rows] == [ida]
        assert await conn.fetch("select 1 from app.refresh_tokens") == []
        # Reading is all a key allows.
        status = await conn.execute("delete from app.identities")
        assert status == "DELETE 0"
    async with user_tx(api.svc.pool, ida) as conn:
        assert {r["user_id"] for r in await conn.fetch("select user_id from app.identities")} == {
            ida
        }
        tokens = await conn.fetch("select user_id from app.refresh_tokens")
        assert {r["user_id"] for r in tokens} == {ida}
        status = await conn.execute(
            "update app.refresh_tokens set revoked_at = now() where user_id = $1", idb
        )
        assert status == "UPDATE 0"


async def test_require_career_os_gates_email_accounts_too(api: Api) -> None:
    tokens = await sign_in(api, address())
    api.svc.settings.require_career_os = True
    try:
        assert (await api.client.get("/v1/me", headers=bearer(tokens))).status_code == 200
        r = await api.client.get("/v1/applications", headers=bearer(tokens))
        assert r.status_code == 403
    finally:
        api.svc.settings.require_career_os = False


# --- GET /auth/config, and a stage without email sign-in ----------------------------------------


async def test_auth_config_adds_the_email_option(api: Api) -> None:
    config = (await api.client.get("/v1/auth/config")).json()
    assert config["email"] == {"code_length": 6, "code_ttl_s": 600, "resend_after_s": 60}
    saved, api.svc.email = api.svc.email, None
    try:
        without = (await api.client.get("/v1/auth/config")).json()
        assert "email" not in without
        # Everything else is what the stage answered before email sign-in.
        assert without == {k: v for k, v in config.items() if k != "email"}
        r = await post(api, "start", {"email": address()})
        assert r.status_code == 404
    finally:
        api.svc.email = saved


async def test_google_config_is_unchanged_for_installed_apps(api: Api) -> None:
    """Desktop 0.2.x reads these fields and ignores `email`."""
    s = api.svc.settings
    saved = (s.auth, s.google_client_ids)
    s.auth, s.google_client_ids = "google", ["123-abc.apps.googleusercontent.com"]
    try:
        config = (await api.client.get("/v1/auth/config")).json()
    finally:
        s.auth, s.google_client_ids = saved
    email = config.pop("email")
    assert email["code_length"] == 6
    assert config == {
        "provider": "google",
        "client_id": "123-abc.apps.googleusercontent.com",
        "scopes": ["openid", "email", "profile"],
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "bearer": "id_token",
        "authorize_params": {"access_type": "offline", "prompt": "consent"},
    }


# --- The sender ---------------------------------------------------------------------------------


async def test_the_local_stack_never_reaches_a_mail_provider() -> None:
    assert isinstance(make_mailer(api_settings()), LogMailer)
    with pytest.raises(RuntimeError, match="never sends through a mail provider"):
        make_mailer(
            api_settings(email_sender="smtp", smtp_host="smtp.example.com",
                         email_from="CoRuby <sign-in@example.com>")
        )  # fmt: skip
    local = make_mailer(
        api_settings(email_sender="smtp", smtp_host="localhost", smtp_port=1025,
                     email_from="CoRuby <sign-in@example.com>")
    )  # fmt: skip
    assert isinstance(local, SmtpMailer)


async def test_the_log_sender_is_refused_on_a_stage() -> None:
    with pytest.raises(RuntimeError, match="writes sign-in codes to the log"):
        make_mailer(api_settings(env="staging"))
    with pytest.raises(RuntimeError, match="SMTP_USERNAME"):
        make_mailer(
            api_settings(env="staging", email_sender="smtp", smtp_host="smtp.example.com",
                         email_from="sign-in@example.com")
        )  # fmt: skip


async def test_the_mail_text() -> None:
    subject, text = sign_in_message("123456", 10)
    assert subject == "Your CoRuby sign-in code"
    assert "123456" in text
    assert "expires in 10 minutes" in text
    assert "If you didn't ask for this code, ignore this email." in text


async def test_email_sign_in_needs_the_signing_key(api: Api) -> None:
    with pytest.raises(RuntimeError, match="SIGNING_KEY"):
        EmailSignIn(api_settings(email_sign_in=True), api.svc.pool, api.svc.valkey, api.mailer)
