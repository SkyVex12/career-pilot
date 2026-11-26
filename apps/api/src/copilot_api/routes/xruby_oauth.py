"""Explicit profile/company OAuth delegation, separate from MCP and app tokens."""

import base64
import hashlib
import html
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import AccountDep, Services, SvcDep, require_link
from copilot_api.routes import personal_ai_oauth as oauth
from copilot_core.auth import Principal, bearer_token
from copilot_core.db import actor_account, user_tx
from copilot_core.valkey_client import text

router = APIRouter(tags=["xruby-delegation"])
PREFIX = "/v1/xruby/oauth"
RESOURCE = "/v1/xruby"
CONTRACT = "xruby-delegation-v1"
SCOPES = frozenset(
    {"xruby:interviews:read", "xruby:work:read", "xruby:retrospective:read", "xruby:sources:read"}
)
HEADERS = oauth.HEADERS
PREPARE_SCOPE = "xruby:interviews:prepare"


def supported_scopes(svc: Services) -> frozenset[str]:
    if getattr(svc.settings, "xruby_preparation_enabled", False):
        return SCOPES | frozenset({PREPARE_SCOPE})
    return SCOPES


def key(kind: str, value: str) -> str:
    return "xruby-oauth:" + kind + ":" + hashlib.sha256(value.encode()).hexdigest()


def origin(svc: Services) -> str:
    base = (svc.settings.xruby_oauth_issuer or "").rstrip("/")
    if not base:
        raise HTTPException(404, "XRuby delegation is not configured")
    u = urlsplit(base)
    if (
        u.username
        or u.password
        or u.query
        or u.fragment
        or u.path
        or not u.hostname
        or (
            u.scheme != "https"
            and not (
                svc.settings.env in {"test", "dev", "local"}
                and u.scheme == "http"
                and u.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise HTTPException(503, "XRuby issuer must be a canonical HTTPS origin")
    return base


def issuer(svc: Services) -> str:
    # A distinct path issuer prevents discovery from advertising MCP's resource/scopes.
    return origin(svc) + PREFIX


def allowed(svc: Services, client: str, redirect: str) -> bool:
    return oauth.redirect_allowed(svc, client, redirect, clients=svc.settings.xruby_oauth_clients)


def connection(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "grant_id": record["id"],
        "grant_version": 1,
        "account_id": record["account_id"],
        "profile_id": record["profile_id"],
        "employer_id": record["employer_id"],
        "scopes": record["scope"].split(),
        "expires_at": datetime.fromtimestamp(record["expires_at"], UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "resource": record["resource"],
    }


async def selection(conn: Any, account_id: UUID, profile_id: UUID, employer_id: UUID | None) -> str:
    # Membership changes use the workspace row lock; account disable/delete uses account lock.
    # RLS stays active; app_api cannot lock the SELECT-only membership relation directly.
    row = await conn.fetchrow(
        "select w.authorization_revision as workspace_revision,"
        " m.authorization_revision as member_revision from app.accounts a"
        " join app.workspace_memberships m on m.account_id=a.id"
        " join app.workspaces w on w.id=m.workspace_id"
        " where a.id=$1 and w.id=$2 and a.status='active' and w.status='active'"
        " and m.status='active' for share of a,w",
        account_id,
        profile_id,
    )
    if row is None:
        raise HTTPException(403, "Selected profile authorization is unavailable")
    if employer_id is not None and not await conn.fetchval(
        "select id from app.employers where id=$1 and user_id=$2 and status='active' for share",
        employer_id,
        profile_id,
    ):
        raise HTTPException(403, "Selected company authorization is unavailable")
    return str(row["workspace_revision"]) + ":" + str(row["member_revision"])


async def authorize_selection(
    svc: Services, account: Principal, profile_id: UUID, employer_id: UUID | None
) -> str:
    await require_link(svc, account)
    async with user_tx(svc.pool, profile_id, actor_account_id=account.user_id) as conn:
        await conn.execute("set local statement_timeout='5s'")
        return await selection(conn, account.user_id, profile_id, employer_id)


@router.get("/.well-known/oauth-protected-resource/v1/xruby")
async def protected_metadata(svc: SvcDep) -> dict[str, Any]:
    return {
        "resource": origin(svc) + RESOURCE,
        "authorization_servers": [issuer(svc)],
        "scopes_supported": sorted(supported_scopes(svc)),
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server/v1/xruby/oauth")
async def authorization_metadata(svc: SvcDep) -> dict[str, Any]:
    base = issuer(svc)
    return {
        "issuer": base,
        "authorization_endpoint": base + "/authorize",
        "token_endpoint": base + "/token",
        "revocation_endpoint": base + "/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": sorted(supported_scopes(svc)),
        "authorization_response_iss_parameter_supported": True,
    }


@router.get(PREFIX + "/authorize")
async def authorize(request: Request, svc: SvcDep) -> HTMLResponse:
    base = origin(svc)
    await oauth.bounded(svc, request, "xruby-authorize", 30)
    params = dict(request.query_params)
    if any(len(request.query_params.getlist(k)) != 1 for k in params):
        raise oauth.oauth_error("invalid_request")
    client, redirect = params.get("client_id", ""), params.get("redirect_uri", "")
    if len(client) > 200 or not allowed(svc, client, redirect):
        raise oauth.oauth_error("invalid_client")
    scopes = set(params.get("scope", "").split())
    if (
        not scopes
        or not scopes <= supported_scopes(svc)
        or scopes == {"xruby:sources:read"}
        or (PREPARE_SCOPE in scopes and "xruby:interviews:read" not in scopes)
    ):
        raise oauth.oauth_error("invalid_scope")
    if (
        params.get("response_type") != "code"
        or params.get("code_challenge_method") != "S256"
        or not oauth.OPAQUE.fullmatch(params.get("code_challenge", ""))
        or params.get("resource") != base + RESOURCE
        or len(params.get("state", "")) > 1024
    ):
        raise oauth.oauth_error("invalid_request")
    if svc.email is None:
        raise HTTPException(503, "Email sign-in must be enabled for XRuby consent")
    handle = secrets.token_urlsafe(32)
    grant = {
        "client_id": client,
        "redirect_uri": redirect,
        "resource": base + RESOURCE,
        "scope": " ".join(sorted(scopes)),
        "state": params.get("state", ""),
        "challenge": params["code_challenge"],
        "issuer": issuer(svc),
    }
    await svc.valkey.set(key("pending", handle), json.dumps(grant), ex=600)
    nonce = secrets.token_urlsafe(24)
    page = Path(__file__).with_name("xruby_consent.html").read_text("utf-8")
    page = page.replace("{{HANDLE}}", handle).replace("{{NONCE}}", nonce)
    if PREPARE_SCOPE in scopes:
        page = page.replace("read-only", "read and confirmed preparation")
        page = page.replace("Read-only", "Read and confirmed preparation")
        page = page.replace(
            "It cannot change your records or call an AI provider.",
            "After a separate confirmation in XRuby, it can send the saved job description and "
            "explicit paired resume to the configured AI provider to generate a preparation draft. "
            "This consumes the CoRuby account preparation budget and may incur provider charges. "
            "It does not schedule interviews, send messages, or attach a draft to a live session.",
        )
    page = page.replace("{{CLIENT}}", html.escape(client)).replace(
        "{{SCOPE}}", html.escape(grant["scope"])
    )
    return HTMLResponse(
        page,
        headers={
            **HEADERS,
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; "
            "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'",
        },
    )


@router.get(PREFIX + "/choices")
async def choices(
    svc: SvcDep, account: AccountDep, profile_id: UUID | None = None, after: UUID | None = None
) -> dict[str, Any]:
    origin(svc)
    await require_link(svc, account)
    async with user_tx(
        svc.pool, profile_id or account.user_id, actor_account_id=account.user_id
    ) as conn:
        await conn.execute("set local statement_timeout='5s'")
        if profile_id is None:
            rows = await conn.fetch(
                "select id,name from app.account_workspaces()"
                " where ($1::uuid is null or id>$1) order by id limit 51",
                after,
            )
        else:
            await selection(conn, account.user_id, profile_id, None)
            rows = await conn.fetch(
                "select id,name from app.employers where user_id=$1"
                " and status='active' and ($2::uuid is null or id>$2) order by id limit 51",
                profile_id,
                after,
            )
    return {
        "items": [{"id": str(r["id"]), "name": r["name"]} for r in rows[:50]],
        "next_after": str(rows[49]["id"]) if len(rows) > 50 else None,
    }


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    approve: bool
    profile_id: UUID | None = None
    employer_id: UUID | None = None


@router.post(PREFIX + "/consent")
async def consent(
    body: Consent, request: Request, svc: SvcDep, account: AccountDep
) -> JSONResponse:
    if request.headers.get("origin") != origin(svc):
        raise HTTPException(403, "Consent requires the same-origin page")
    await oauth.bounded(svc, request, "xruby-consent", 20)
    raw = await svc.valkey.get(key("pending", body.handle))
    if not raw:
        raise oauth.oauth_error("invalid_request")
    grant = json.loads(raw)
    if grant["issuer"] != issuer(svc) or not allowed(
        svc, grant["client_id"], grant["redirect_uri"]
    ):
        raise oauth.oauth_error("invalid_client")
    response = {"state": grant["state"], "iss": issuer(svc)}
    if body.approve:
        if body.profile_id is None or (
            set(grant["scope"].split()) & {"xruby:work:read", "xruby:retrospective:read"}
            and body.employer_id is None
        ):
            raise HTTPException(422, "Select one profile and a company for company permissions")
        binding = await authorize_selection(svc, account, body.profile_id, body.employer_id)
        if PREPARE_SCOPE in grant["scope"].split():
            async with user_tx(svc.pool, body.profile_id, actor_account_id=account.user_id) as conn:
                if not await conn.fetchval("select app.has_workspace_write($1)", body.profile_id):
                    raise HTTPException(403, "Preparation requires writable profile membership")
        grant.update(
            account_id=str(account.user_id),
            profile_id=str(body.profile_id),
            employer_id=str(body.employer_id) if body.employer_id else None,
            binding=binding,
        )
    if not await svc.valkey.eval(oauth.CONSUME, 1, key("pending", body.handle), raw):
        raise oauth.oauth_error("invalid_request")
    if body.approve:
        code = secrets.token_urlsafe(32)
        await svc.valkey.set(key("code", code), json.dumps(grant), ex=120)
        response["code"] = code
    else:
        response["error"] = "access_denied"
    separator = "&" if "?" in grant["redirect_uri"] else "?"
    return JSONResponse(
        {"redirect_uri": grant["redirect_uri"] + separator + urlencode(response)}, headers=HEADERS
    )


ISSUE = """
local refs=redis.call('HGETALL',KEYS[2])
for i=1,#refs,2 do
 if redis.call('EXISTS',refs[i+1])==0 then redis.call('HDEL',KEYS[2],refs[i]) end
end
if redis.call('HLEN',KEYS[2])>=20 then return 0 end
redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[2])
redis.call('HSET',KEYS[2],ARGV[3],KEYS[1])
redis.call('EXPIRE',KEYS[2],3600)
return 1
"""


@router.post(PREFIX + "/token")
async def token(request: Request, svc: SvcDep) -> JSONResponse:
    origin(svc)
    await oauth.bounded(svc, request, "xruby-token")
    values = await oauth.form(request)
    if values.get("grant_type") != "authorization_code":
        raise oauth.oauth_error("unsupported_grant_type")
    verifier, code = values.get("code_verifier", ""), values.get("code", "")
    if not oauth.VERIFIER.fullmatch(verifier) or not oauth.OPAQUE.fullmatch(code):
        raise oauth.oauth_error("invalid_grant")
    raw = await svc.valkey.get(key("code", code))
    grant: dict[str, Any] = json.loads(raw) if raw else {}
    digest = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    if (
        not grant
        or grant["issuer"] != issuer(svc)
        or grant["challenge"] != digest
        or any(values.get(k) != grant[k] for k in ("client_id", "redirect_uri", "resource"))
        or not allowed(svc, grant["client_id"], grant["redirect_uri"])
    ):
        raise oauth.oauth_error("invalid_grant")
    account = Principal(UUID(grant["account_id"]), "XRuby client", None)
    binding = await authorize_selection(
        svc,
        account,
        UUID(grant["profile_id"]),
        UUID(grant["employer_id"]) if grant["employer_id"] else None,
    )
    if binding != grant["binding"]:
        raise oauth.oauth_error("invalid_grant")
    if not await svc.valkey.eval(oauth.CONSUME, 1, key("code", code), raw):
        raise oauth.oauth_error("invalid_grant")
    access = "xruby_" + secrets.token_urlsafe(32)
    ttl = svc.settings.xruby_oauth_token_ttl_s
    record = {
        k: grant[k]
        for k in (
            "account_id",
            "profile_id",
            "employer_id",
            "binding",
            "client_id",
            "resource",
            "scope",
            "issuer",
        )
    }
    record.update(id=secrets.token_urlsafe(16), expires_at=int(time.time()) + ttl)
    if not await svc.valkey.eval(
        ISSUE,
        2,
        key("access", access),
        "xruby-oauth:account:" + grant["account_id"],
        json.dumps(record),
        ttl,
        record["id"],
    ):
        raise oauth.oauth_error("access_denied")
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": record["scope"],
            "connection": connection(record),
        },
        headers=HEADERS,
    )


@router.post(PREFIX + "/revoke")
async def revoke(request: Request, svc: SvcDep) -> JSONResponse:
    origin(svc)
    await oauth.bounded(svc, request, "xruby-revoke")
    values = await oauth.form(request)
    access = values.get("token", "")
    raw = await svc.valkey.get(key("access", access)) if len(access) <= 128 else None
    if raw:
        record = json.loads(raw)
        if record["client_id"] == values.get("client_id"):
            from copilot_api.xruby_preparation import revoke_grant

            await revoke_grant(svc, record, key("access", access))
            await svc.valkey.hdel("xruby-oauth:account:" + record["account_id"], record["id"])
    return JSONResponse({}, headers=HEADERS)


@router.get(PREFIX + "/connections")
async def connections(svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    origin(svc)
    refs = await svc.valkey.hgetall("xruby-oauth:account:" + str(account.user_id))
    values: list[dict[str, Any]] = []
    for ref in list(refs.values())[:20]:
        raw = await svc.valkey.get(text(ref))
        if raw:
            record = json.loads(raw)
            values.append({**connection(record), "client_id": record["client_id"]})
    return {"connections": values}


@router.delete(PREFIX + "/connections/{identifier}")
async def disconnect(identifier: str, svc: SvcDep, account: AccountDep) -> JSONResponse:
    origin(svc)
    index = "xruby-oauth:account:" + str(account.user_id)
    ref = await svc.valkey.hget(index, identifier)
    if ref:
        raw = await svc.valkey.get(text(ref))
        if raw:
            from copilot_api.xruby_preparation import revoke_grant

            await revoke_grant(svc, json.loads(raw), text(ref))
        await svc.valkey.hdel(index, identifier)
    return JSONResponse({}, headers=HEADERS)


@dataclass(frozen=True)
class Delegation:
    access: str
    record: dict[str, Any]
    reference: str | None = None  # Internal executor reference; never accepted from HTTP.

    @property
    def account(self) -> Principal:
        return Principal(UUID(self.record["account_id"]), "XRuby client", self.record["expires_at"])

    @property
    def profile_id(self) -> UUID:
        return UUID(self.record["profile_id"])

    @property
    def employer_id(self) -> UUID | None:
        return UUID(self.record["employer_id"]) if self.record["employer_id"] else None


async def live(svc: Services, access: str) -> dict[str, Any]:
    ref = key("access", access) if access.startswith("xruby_") and len(access) <= 128 else ""
    return await live_reference(svc, ref)


async def live_reference(svc: Services, ref: str) -> dict[str, Any]:
    base = origin(svc)
    raw = (
        await svc.valkey.get(ref) if re.fullmatch(r"xruby-oauth:access:[a-f0-9]{64}", ref) else None
    )
    try:
        record: dict[str, Any] = json.loads(raw) if raw else {}
        if (
            record["issuer"] != issuer(svc)
            or record["resource"] != base + RESOURCE
            or record["expires_at"] <= time.time()
            or record["client_id"] not in svc.settings.xruby_oauth_clients
            or not set(record["scope"].split()) <= supported_scopes(svc)
        ):
            raise ValueError
        UUID(record["account_id"])
        UUID(record["profile_id"])
        if record["employer_id"] is not None:
            UUID(record["employer_id"])
        return record
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            401,
            "XRuby authorization expired or revoked",
            headers={
                **HEADERS,
                "WWW-Authenticate": 'Bearer resource_metadata="'
                + base
                + '/.well-known/oauth-protected-resource/v1/xruby", error="invalid_token"',
            },
        ) from None


async def delegated(request: Request, svc: SvcDep) -> Delegation:
    access = bearer_token(request.headers.get("authorization")) or ""
    if request.headers.get("x-copilot-profile"):
        raise HTTPException(422, "Delegation fixes the selected profile")
    return Delegation(access, await live(svc, access))


Delegated = Annotated[Delegation, Depends(delegated)]


@asynccontextmanager
async def read_scope(svc: Services, grant: Delegation, scope: str | None = None):
    record = (
        await live_reference(svc, grant.reference)
        if grant.reference
        else await live(svc, grant.access)
    )
    if record != grant.record:
        raise HTTPException(401, "Delegation changed")
    if scope and scope not in record["scope"].split():
        raise HTTPException(403, "Delegation does not grant this read")
    bucket = key("read-rate", record["id"] + ":" + str(int(time.time()) // 60))
    count = await svc.valkey.incr(bucket)
    if count == 1:
        await svc.valkey.expire(bucket, 120)
    if count > 120:
        raise HTTPException(429, "Delegated read limit reached", headers={"Retry-After": "60"})
    await require_link(svc, grant.account)
    actor_account.set(grant.account.user_id)
    async with user_tx(svc.pool, grant.profile_id, actor_account_id=grant.account.user_id) as conn:
        await conn.execute("set local statement_timeout='5s'")
        if (
            await selection(conn, grant.account.user_id, grant.profile_id, grant.employer_id)
            != record["binding"]
        ):
            raise HTTPException(403, "Profile membership changed; reconnect")
        yield conn
        if (
            await selection(conn, grant.account.user_id, grant.profile_id, grant.employer_id)
            != record["binding"]
        ):
            raise HTTPException(403, "Profile membership changed; reconnect")
    current = (
        await live_reference(svc, grant.reference)
        if grant.reference
        else await live(svc, grant.access)
    )
    if current != record:
        raise HTTPException(401, "Delegation changed")


@router.get(RESOURCE + "/connection")
async def status(svc: SvcDep, grant: Delegated) -> JSONResponse:
    from copilot_api.routes.xruby_reads import iso, timezone

    async with read_scope(svc, grant) as conn:
        result = {
            **connection(grant.record),
            "timezone": await timezone(conn, grant),
            "server_time": iso(datetime.now(UTC)),
            "capabilities": {
                "prepare_interview": bool(
                    PREPARE_SCOPE in grant.record["scope"].split()
                    and getattr(svc.settings, "xruby_preparation_enabled", False)
                    and await conn.fetchval("select app.has_workspace_write($1)", grant.profile_id)
                )
            },
        }
    return JSONResponse(result, headers=HEADERS)
