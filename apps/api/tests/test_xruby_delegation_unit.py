"""XRuby OAuth and factual routes with a real ASGI host and local-only test doubles."""

import asyncio
import base64
import hashlib
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import fakeredis.aioredis
import httpx2
import pytest
from fastapi import FastAPI, Header, HTTPException

from copilot_api.deps import current_account, services
from copilot_api.routes import personal_ai_oauth as oauth
from copilot_api.routes import xruby_oauth as auth
from copilot_api.routes import xruby_preparation as commands
from copilot_api.routes import xruby_reads as reads
from copilot_core.auth import Principal

ORIGIN = "https://coruby.test"
REDIRECT = "https://hubruby.test/xruby/coruby/callback"
VERIFIER = "a" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)


@pytest.fixture
async def fixture(monkeypatch):
    account_id, profile_id, employer_id = uuid4(), uuid4(), uuid4()
    state = SimpleNamespace(
        account=account_id,
        profile=profile_id,
        employer=employer_id,
        active=True,
        employer_active=True,
        revision=1,
        interviews={},
        nodes={},
        logs={},
        feedback={},
        reads=[],
        zone="America/Chicago",
        writable=True,
    )

    class Conn:
        async def execute(self, sql, *args):
            state.reads.append((sql, args))

        async def fetchrow(self, sql, *args):
            state.reads.append((sql, args))
            if "workspace_memberships" in sql:
                return (
                    {"workspace_revision": 1, "member_revision": state.revision}
                    if state.active and args == (state.account, state.profile)
                    else None
                )
            if "app.paired_resume" in sql:
                return {
                    "pack_id": None,
                    "jd_bytes": 0,
                    "jd_hash": hashlib.sha256(b"").hexdigest(),
                    "resume_id": None,
                    "current_revision_id": None,
                    "content_sha256": None,
                    "extracted_sha256": None,
                    "resume_status": None,
                    "text_source_hash": None,
                }
            if "from app.interviews" in sql:
                return state.interviews.get(args[0]) if args[1] == state.profile else None
            if "from app.memory_nodes" in sql:
                return (
                    state.nodes.get(args[0])
                    if args[1:3] == (state.profile, state.employer)
                    else None
                )
            if "from app.work_logs" in sql:
                return (
                    state.logs.get(args[0])
                    if args[1:3] == (state.profile, state.employer)
                    else None
                )
            if "from app.session_outputs" in sql:
                return (
                    state.feedback.get(args[0])
                    if args[1:3] == (state.profile, state.employer)
                    else None
                )
            raise AssertionError(sql)

        async def fetchval(self, sql, *args):
            state.reads.append((sql, args))
            if "app.employers" in sql:
                return (
                    state.employer
                    if state.employer_active and args == (state.employer, state.profile)
                    else None
                )
            if "app.has_workspace_write" in sql:
                return state.writable
            if "settings->>'timezone'" in sql:
                return state.zone
            if "from app.interviews" in sql:
                rows = sorted(
                    (v for v in state.interviews.values() if v["starts_at"] >= args[1]),
                    key=lambda v: (v["starts_at"], v["id"]),
                )
                return rows[0]["id"] if rows else None
            raise AssertionError(sql)

        async def fetch(self, sql, *args):
            state.reads.append((sql, args))
            if "app.account_workspaces" in sql:
                return [{"id": state.profile, "name": "Exact selected profile"}]
            if "from app.employers" in sql:
                return [{"id": state.employer, "name": "Selected company"}]
            if "from app.memory_nodes" in sql:
                return [{"id": v} for v in sorted(state.nodes) if args[2] is None or v > args[2]][
                    : args[3]
                ]
            if "select kind,id,at" in sql:
                values = [
                    {
                        "kind": "work_log",
                        "id": v["id"],
                        "at": datetime.combine(v["day"], datetime.min.time(), UTC),
                    }
                    for v in state.logs.values()
                ]
                values += [
                    {"kind": "feedback", "id": v["id"], "at": v["created_at"]}
                    for v in state.feedback.values()
                ]
                values.sort(key=lambda v: (v["at"], v["id"], v["kind"]), reverse=True)
                return [
                    v
                    for v in values
                    if args[7] is None
                    or (v["at"], v["id"], v["kind"]) < (args[7], args[8], args[9])
                ][: args[-1]]
            raise AssertionError(sql)

    conn = Conn()

    @asynccontextmanager
    async def tx(pool, user_id, **kwargs):
        assert kwargs["actor_account_id"] == state.account
        assert isinstance(user_id, UUID)
        yield conn

    monkeypatch.setattr(auth, "user_tx", tx)
    monkeypatch.setattr(auth, "require_link", AsyncMock())
    settings = SimpleNamespace(
        xruby_oauth_issuer=ORIGIN,
        xruby_oauth_clients={"hubruby": [REDIRECT]},
        xruby_oauth_token_ttl_s=1200,
        env="test",
    )
    valkey = fakeredis.aioredis.FakeRedis()
    svc = SimpleNamespace(
        settings=settings,
        valkey=valkey,
        email=object(),
        pool=object(),
        crypto=SimpleNamespace(decrypt=AsyncMock(side_effect=lambda *args: args[-2].decode())),
    )
    app = FastAPI()
    app.add_exception_handler(oauth.OAuthError, oauth.oauth_exception)
    app.include_router(auth.router)
    app.include_router(reads.router)
    app.include_router(commands.router)
    app.dependency_overrides[services] = lambda: svc

    async def account(authorization: str | None = Header(default=None)):
        if authorization != "Bearer fixture-app":
            raise HTTPException(401, "app bearer required")
        return Principal(state.account, "Fixture account", None)

    app.dependency_overrides[current_account] = account
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN)
    yield SimpleNamespace(
        state=state,
        svc=svc,
        conn=conn,
        client=client,
        headers={"Authorization": "Bearer fixture-app", "Origin": ORIGIN},
    )
    await client.aclose()
    await valkey.aclose()


async def code(
    f,
    scopes="xruby:interviews:read xruby:work:read xruby:retrospective:read xruby:sources:read",
    **consent_changes,
):
    response = await f.client.get(
        auth.PREFIX + "/authorize",
        params={
            "client_id": "hubruby",
            "redirect_uri": REDIRECT,
            "scope": scopes,
            "response_type": "code",
            "resource": ORIGIN + auth.RESOURCE,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "unchanged",
        },
    )
    assert response.status_code == 200, response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    handle = re.search(r"handle:'([A-Za-z0-9_-]+)'", response.text).group(1)
    response = await f.client.post(
        auth.PREFIX + "/consent",
        headers=f.headers,
        json={
            "handle": handle,
            "approve": True,
            "profile_id": str(f.state.profile),
            "employer_id": str(f.state.employer),
            **consent_changes,
        },
    )
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["redirect_uri"]).query)
    assert query["iss"] == [ORIGIN + auth.PREFIX]
    assert query["state"] == ["unchanged"]
    return query["code"][0]


async def exchange(f, value, **changes):
    return await f.client.post(
        auth.PREFIX + "/token",
        data={
            "grant_type": "authorization_code",
            "code": value,
            "code_verifier": VERIFIER,
            "client_id": "hubruby",
            "redirect_uri": REDIRECT,
            "resource": ORIGIN + auth.RESOURCE,
            **changes,
        },
    )


async def connect(
    f, scopes="xruby:interviews:read xruby:work:read xruby:retrospective:read xruby:sources:read"
):
    response = await exchange(f, await code(f, scopes))
    assert response.status_code == 200, response.text
    return response.json(), {"Authorization": "Bearer " + response.json()["access_token"]}


async def test_pkce_single_use_exact_resource_hash_only_storage_and_revoke(fixture):
    f = fixture
    value = await code(f)
    assert (await exchange(f, value, code_verifier="b" * 43)).status_code == 400
    assert (await exchange(f, value, resource=ORIGIN + "/v1/personal-ai/mcp")).status_code == 400
    responses = await asyncio.gather(exchange(f, value), exchange(f, value))
    assert sorted(r.status_code for r in responses) == [200, 400]
    response = next(r for r in responses if r.status_code == 200)
    token = response.json()["access_token"]
    assert token.startswith("xruby_")
    raw = await f.svc.valkey.get(auth.key("access", token))
    assert token.encode() not in raw
    assert response.json()["connection"]["expires_at"].endswith("Z")
    headers = {"Authorization": "Bearer " + token}
    assert (await f.client.get("/v1/xruby/connection", headers=headers)).status_code == 200
    assert (await f.client.get(auth.PREFIX + "/connections", headers=headers)).status_code == 401
    await f.client.post(auth.PREFIX + "/revoke", data={"token": token, "client_id": "wrong-client"})
    assert (await f.client.get("/v1/xruby/connection", headers=headers)).status_code == 200
    await f.client.post(auth.PREFIX + "/revoke", data={"token": token, "client_id": "hubruby"})
    assert (await f.client.get("/v1/xruby/connection", headers=headers)).status_code == 401


@pytest.mark.parametrize(
    "change", ["account", "company", "membership", "expiry", "audience", "registration"]
)
async def test_live_authority_and_epoch_never_resurrects_old_token(fixture, change):
    f = fixture
    data, headers = await connect(f)
    key = auth.key("access", data["access_token"])
    record = json.loads(await f.svc.valkey.get(key))
    if change == "account":
        f.state.active = False
    elif change == "company":
        f.state.employer_active = False
    elif change == "membership":
        f.state.revision += 1
    elif change == "expiry":
        record["expires_at"] = 0
    elif change == "audience":
        record["resource"] = ORIGIN + "/v1/personal-ai/mcp"
    elif change == "registration":
        f.svc.settings.xruby_oauth_clients = {}
    await f.svc.valkey.set(key, json.dumps(record), ex=60)
    response = await f.client.get("/v1/xruby/connection", headers=headers)
    assert response.status_code in (401, 403)
    assert "items" not in response.json()


@pytest.mark.parametrize("scopes", ["context:read", "xruby:sources:read", "xruby:write", ""])
async def test_scope_escalation_rejected(fixture, scopes):
    response = await fixture.client.get(
        auth.PREFIX + "/authorize",
        params={"client_id": "hubruby", "redirect_uri": REDIRECT, "scope": scopes},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


@pytest.mark.parametrize("token", ["fixture-app", "mcp_" + "a" * 43, "unknown", ""])
async def test_delegated_api_never_accepts_app_or_mcp_bearers(fixture, token):
    response = await fixture.client.get(
        "/v1/xruby/connection", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 401
    assert fixture.state.reads == []


async def test_exact_choices_and_unknown_profile_company_cannot_be_granted(fixture):
    f = fixture
    response = await f.client.get(auth.PREFIX + "/choices", headers=f.headers)
    assert response.json()["items"][0]["id"] == str(f.state.profile)
    response = await f.client.get(
        auth.PREFIX + "/choices", params={"profile_id": str(uuid4())}, headers=f.headers
    )
    assert response.status_code == 403
    for changes in (
        {"profile_id": None},
        {"employer_id": None},
        {"profile_id": str(uuid4())},
        {"employer_id": str(uuid4())},
    ):
        with pytest.raises(AssertionError):
            await code(f, **changes)


def interview(f, at=None):
    identifier = uuid4()
    row = {
        "id": identifier,
        "job_id": uuid4(),
        "round": "technical",
        "starts_at": at or datetime.now(UTC) + timedelta(days=1),
        "company": "Fictional company",
        "role": "Engineer",
    }
    f.state.interviews[identifier] = row
    return row


def node(f, identifier=None, due="2000-01-01", state="blocked"):
    identifier = identifier or uuid4()
    row = {
        "id": identifier,
        "kind": "work_item",
        "title": "Ship parser",
        "summary": "Blocked by review",
        "valid_from": date(2026, 11, 23),
        "updated_at": datetime.now(UTC),
        "state": state,
        "due_date": due,
        "edited": False,
        "source_type": "worklog",
    }
    f.state.nodes[identifier] = row
    return row


def log(f, oversized=False):
    identifier = uuid4()
    row = {
        "id": identifier,
        "day": date(2026, 11, 20),
        "current_revision_id": uuid4(),
        "sealed": None if oversized else b"Saved observation; verify before drawing a conclusion.",
        "key_version": 1,
    }
    f.state.logs[identifier] = row
    return row


async def test_next_interview_records_occurrence_timezone_and_live_source_hash(fixture):
    f = fixture
    interview(f, datetime.now(UTC) - timedelta(days=1))
    wanted = interview(f)
    interview(f, datetime.now(UTC) + timedelta(days=4))
    _, headers = await connect(f)
    response = await f.client.get("/v1/xruby/next-interview", headers=headers)
    body = response.json()
    assert body["items"][0]["occurrence_id"] == str(wanted["id"])
    assert body["items"][0]["timezone"] == "America/Chicago"
    assert body["items"][0]["cancellation_state"] == "not_recorded"
    assert body["coverage"]["calendar_synced"] is False
    citation = body["sources"][0]
    assert citation["employer_id"] is None
    source = await f.client.get(
        citation["path"],
        headers=headers,
        params={"revision": citation["revision"], "expected_hash": citation["source_hash"]},
    )
    assert source.status_code == 200
    wanted["starts_at"] += timedelta(hours=1)
    stale = await f.client.get(
        citation["path"],
        headers=headers,
        params={"revision": citation["revision"], "expected_hash": citation["source_hash"]},
    )
    assert stale.status_code == 409


async def test_work_page_cursor_scoped_to_grant_limit_and_explicit_overdue_not_missed(fixture):
    f = fixture
    for number in range(1, 24):
        node(f, UUID(int=number))
    _, headers = await connect(f)
    response = await f.client.get("/v1/xruby/current-work", headers=headers, params={"limit": 20})
    body = response.json()
    assert len(body["items"]) == 20
    assert body["next_cursor"]
    assert body["items"][0]["overdue"] is True
    assert body["items"][0]["missed"] is None
    assert body["items"][0]["priority"] is None
    next_page = await f.client.get(
        "/v1/xruby/current-work",
        headers=headers,
        params={"limit": 20, "cursor": body["next_cursor"]},
    )
    assert len(next_page.json()["items"]) == 3
    assert next_page.json()["as_of"] == body["as_of"]
    wrong_limit = await f.client.get(
        "/v1/xruby/current-work",
        headers=headers,
        params={"limit": 10, "cursor": body["next_cursor"]},
    )
    assert wrong_limit.status_code == 409
    _, other = await connect(f)
    wrong_grant = await f.client.get(
        "/v1/xruby/current-work", headers=other, params={"limit": 20, "cursor": body["next_cursor"]}
    )
    assert wrong_grant.status_code == 409


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 21},
        {"profile_id": "other"},
        {"employer_id": "other"},
        [("limit", "1"), ("limit", "2")],
    ],
)
async def test_read_bounds_and_scope_overrides_fail_closed(fixture, params):
    _, headers = await connect(fixture)
    assert (
        await fixture.client.get("/v1/xruby/current-work", params=params, headers=headers)
    ).status_code == 422
    assert (
        await fixture.client.get(
            "/v1/xruby/current-work", headers={**headers, "X-Copilot-Profile": str(uuid4())}
        )
    ).status_code == 422


async def test_retrospective_original_log_feedback_provenance_and_oversized_coverage(fixture):
    f = fixture
    original = log(f)
    log(f, oversized=True)
    identifier = uuid4()
    f.state.feedback[identifier] = {
        "id": identifier,
        "session_id": uuid4(),
        "created_at": datetime(2026, 11, 21, tzinfo=UTC),
        "sealed": b"AI suggestion to check assumptions",
        "key_version": 1,
    }
    _, headers = await connect(f)
    response = await f.client.get(
        "/v1/xruby/retrospective",
        headers=headers,
        params={"start": "2026-11-19", "end": "2026-11-24"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["coverage"]["verified_mistakes"] is False
    assert body["coverage"]["omitted_oversized"] == 1
    assert {v["provenance"] for v in body["items"]} == {"user_report", "ai_generated_feedback"}
    cited = next(v for v in body["sources"] if v["kind"] == "work_log")
    assert cited["revision"] == str(original["current_revision_id"])
    original["current_revision_id"] = uuid4()
    assert (
        await f.client.get(
            cited["path"],
            headers=headers,
            params={"revision": cited["revision"], "expected_hash": cited["source_hash"]},
        )
    ).status_code == 409
    assert (
        await f.client.get(
            "/v1/xruby/retrospective",
            headers=headers,
            params={"start": "2026-01-01", "end": "2026-11-24"},
        )
    ).status_code == 422


@pytest.mark.parametrize("revoke", ["grant", "membership", "company"])
async def test_revocation_during_decryption_suppresses_entire_response(fixture, revoke):
    f = fixture
    log(f)
    data, headers = await connect(f)

    async def decrypt(*args):
        if revoke == "grant":
            await f.svc.valkey.delete(auth.key("access", data["access_token"]))
        elif revoke == "membership":
            f.state.revision += 1
        else:
            f.state.employer_active = False
        return "PRIVATE SOURCE MUST NOT RETURN"

    f.svc.crypto.decrypt.side_effect = decrypt
    response = await f.client.get(
        "/v1/xruby/retrospective",
        headers=headers,
        params={"start": "2026-11-19", "end": "2026-11-24"},
    )
    assert response.status_code in (401, 403)
    assert "PRIVATE SOURCE" not in response.text


async def test_sources_require_both_kind_and_source_scope(fixture):
    f = fixture
    wanted = interview(f)
    _, headers = await connect(f, "xruby:interviews:read")
    assert (await f.client.get("/v1/xruby/current-work", headers=headers)).status_code == 403
    assert (await f.client.get("/v1/xruby/next-interview", headers=headers)).status_code == 200
    response = await f.client.get(
        f"/v1/xruby/sources/interview/{wanted['id']}",
        headers=headers,
        params={"revision": "a" * 64, "expected_hash": "a" * 64},
    )
    assert response.status_code == 403
    _, headers = await connect(f, "xruby:work:read xruby:sources:read")
    assert (
        await f.client.get(
            f"/v1/xruby/sources/interview/{wanted['id']}",
            headers=headers,
            params={"revision": "a" * 64, "expected_hash": "a" * 64},
        )
    ).status_code == 403


async def test_preparation_is_exact_sourced_draft_without_provider_or_scheduling(fixture):
    f = fixture
    wanted = interview(f)
    _, headers = await connect(f)
    next_result = (await f.client.get("/v1/xruby/next-interview", headers=headers)).json()
    cited = next_result["sources"][0]
    response = await f.client.get(
        f"/v1/xruby/interviews/{wanted['id']}/preparation",
        headers=headers,
        params={"revision": cited["revision"], "expected_hash": cited["source_hash"]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["coverage"]["model_called"] is False
    assert result["coverage"]["scheduling_performed"] is False
    assert result["items"][0]["status"] == "draft"
    assert result["items"][0]["job_description"]["available"] is False
    assert len(result["items"][0]["draft_checklist"]) == 5
    assert result["items"][0]["actions_performed"] == []
    assert result["sources"][1]["kind"] == "interview_preparation"
    assert result["sources"][1]["employer_id"] is None
    source = result["sources"][1]
    assert (
        await f.client.get(
            source["path"],
            headers=headers,
            params={"revision": source["revision"], "expected_hash": source["source_hash"]},
        )
    ).status_code == 200
    wanted["starts_at"] += timedelta(hours=1)
    assert (
        await f.client.get(
            f"/v1/xruby/interviews/{wanted['id']}/preparation",
            headers=headers,
            params={"revision": cited["revision"], "expected_hash": cited["source_hash"]},
        )
    ).status_code == 409
    assert all(
        not sql.lstrip().lower().startswith(("insert", "update", "delete"))
        for sql, _ in f.state.reads
    )


async def test_connection_live_timezone_and_server_clock(fixture):
    f = fixture
    _, headers = await connect(f)
    value = (await f.client.get("/v1/xruby/connection", headers=headers)).json()
    assert value["timezone"] == "America/Chicago"
    assert value["server_time"].endswith("Z")
    assert value["capabilities"]["prepare_interview"] is False
    f.state.zone = "Not/A/Zone"
    assert (await f.client.get("/v1/xruby/connection", headers=headers)).json()["timezone"] == "UTC"


async def test_confirmed_prepare_route_keeps_explicit_citation_key_and_scope(fixture, monkeypatch):
    f = fixture
    f.svc.settings.xruby_preparation_enabled = True
    _, headers = await connect(
        f, "xruby:interviews:read xruby:sources:read xruby:interviews:prepare"
    )
    status = (await f.client.get("/v1/xruby/connection", headers=headers)).json()
    assert status["capabilities"]["prepare_interview"] is True
    f.state.writable = False
    assert (await f.client.get("/v1/xruby/connection", headers=headers)).json()["capabilities"][
        "prepare_interview"
    ] is False
    f.state.writable = True
    accepted = AsyncMock(return_value={"request_id": str(uuid4()), "state": "queued"})
    monkeypatch.setattr(commands.service, "reserve", accepted)
    interview = uuid4()
    path = f"/v1/xruby/interviews/{interview}/preparation-requests"
    body = {"revision": "a" * 64, "expected_hash": "a" * 64}
    assert (await f.client.post(path, headers=headers, json=body)).status_code == 422
    assert accepted.await_count == 0
    keyed = {**headers, "Idempotency-Key": "explicit-confirmation-1"}
    assert (
        await f.client.post(path, headers=keyed, json={**body, "revision": "b" * 64})
    ).status_code == 409
    assert accepted.await_count == 0
    response = await f.client.post(path, headers=keyed, json=body)
    assert response.status_code == 202
    assert accepted.await_args.args[2:] == (interview, "a" * 64, "explicit-confirmation-1")


async def test_preparation_consent_requires_read_scope_and_writable_membership(fixture):
    f = fixture
    f.svc.settings.xruby_preparation_enabled = True
    response = await f.client.get(
        auth.PREFIX + "/authorize",
        params={
            "client_id": "hubruby",
            "redirect_uri": REDIRECT,
            "scope": "xruby:interviews:prepare",
            "response_type": "code",
            "resource": ORIGIN + auth.RESOURCE,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400
    f.state.writable = False
    params = {
        "client_id": "hubruby",
        "redirect_uri": REDIRECT,
        "scope": "xruby:interviews:read xruby:interviews:prepare",
        "response_type": "code",
        "resource": ORIGIN + auth.RESOURCE,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    page = await f.client.get(auth.PREFIX + "/authorize", params=params)
    assert "may incur provider charges" in page.text
    assert "cannot change your records or call an AI provider" not in page.text
    handle = re.search(r"handle:'([A-Za-z0-9_-]+)'", page.text).group(1)
    response = await f.client.post(
        auth.PREFIX + "/consent",
        headers=f.headers,
        json={
            "handle": handle,
            "approve": True,
            "profile_id": str(f.state.profile),
            "employer_id": None,
        },
    )
    assert response.status_code == 403
