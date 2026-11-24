"""Bounded MCP-only OAuth authorization server: pre-registered public clients + PKCE.

No source-system credentials, app refresh tokens, wildcard redirects or DCR. Opaque
access tokens are stored only by SHA-256 key and never authorize the ordinary app API.
"""

import base64
import hashlib
import html
import json
import re
import secrets
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from copilot_api.deps import AccountDep, SvcDep, current_account, require_link
from copilot_core.auth import Principal, bearer_token
from copilot_core.db import actor_account
from copilot_core.valkey_client import text

router = APIRouter(tags=["personal-ai-oauth"])
SCOPES = {"context:read", "context:publish"}
PREFIX = "/v1/personal-ai/oauth"
OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
CONSUME = """
local value = redis.call('GET', KEYS[1])
if not value or value ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


def key(kind: str, value: str) -> str:
    return "mcp-oauth:" + kind + ":" + hashlib.sha256(value.encode()).hexdigest()


def issuer(svc: SvcDep) -> str:
    base = svc.settings.personal_ai_oauth_issuer
    if not base:
        raise HTTPException(404, "MCP OAuth is not configured")
    base = base.rstrip("/")
    u = urlsplit(base)
    if u.username or u.password or u.query or u.fragment or u.path or not u.hostname:
        raise HTTPException(503, "MCP OAuth issuer must be a canonical origin")
    if u.scheme != "https" and not (
        svc.settings.env in {"test", "dev", "local"}
        and u.scheme == "http"
        and u.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise HTTPException(503, "MCP OAuth requires HTTPS")
    return base


def resource(svc: SvcDep) -> str:
    return issuer(svc) + "/v1/personal-ai/mcp"


class OAuthError(Exception):
    def __init__(self, error: str, status: int = 400):
        self.error = error
        self.status = status


async def oauth_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, OAuthError)
    return JSONResponse({"error": exc.error}, status_code=exc.status, headers=HEADERS)


def oauth_error(error: str, status: int = 400) -> OAuthError:
    return OAuthError(error, status)


def challenge(svc: SvcDep, scope: str = "context:read", error: str | None = None) -> dict[str, str]:
    value = (
        'Bearer resource_metadata="'
        + issuer(svc)
        + '/.well-known/oauth-protected-resource/v1/personal-ai/mcp", scope="'
        + scope
        + '"'
    )
    if error:
        value += ', error="' + error + '"'
    return {**HEADERS, "WWW-Authenticate": value}


async def bounded(svc: SvcDep, request: Request, action: str, limit: int = 60) -> None:
    # Per-peer rather than trusting arbitrary X-Forwarded-For headers.
    peer = request.client.host if request.client else "unknown"
    bucket = key("rate", action + peer + str(int(time.time()) // 60))
    count = await svc.valkey.incr(bucket)
    if count == 1:
        await svc.valkey.expire(bucket, 120)
    if count > limit:
        raise HTTPException(429, "Too many OAuth requests", headers={"Retry-After": "60"})


def redirect_allowed(svc: SvcDep, client: str, redirect: str) -> bool:
    try:
        u = urlsplit(redirect)
    except ValueError:
        return False
    if not u.hostname or u.username or u.password or u.fragment:
        return False
    try:
        if u.port is not None and not 1 <= u.port <= 65535:
            return False
    except ValueError:
        return False
    for registered in svc.settings.personal_ai_oauth_clients.get(client, []):
        if u.scheme == "https" and redirect == registered:
            return True
        # RFC 8252: loopback clients select their available listener port at login.
        # Only the port varies; literal host, scheme, path and query still match exactly.
        try:
            target = urlsplit(registered)
        except ValueError:
            continue
        if (
            u.scheme == target.scheme == "http"
            and u.hostname in {"127.0.0.1", "::1"}
            and u.hostname == target.hostname
            and u.path == target.path
            and u.query == target.query
            and not target.username
            and not target.password
            and not target.fragment
        ):
            return True
    return False


@router.get("/.well-known/oauth-protected-resource/v1/personal-ai/mcp")
@router.get("/.well-known/oauth-protected-resource")
async def protected_metadata(svc: SvcDep) -> dict[str, Any]:
    return {
        "resource": resource(svc),
        "authorization_servers": [issuer(svc)],
        "scopes_supported": sorted(SCOPES),
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_metadata(svc: SvcDep) -> dict[str, Any]:
    base = issuer(svc)
    return {
        "issuer": base,
        "authorization_endpoint": base + PREFIX + "/authorize",
        "token_endpoint": base + PREFIX + "/token",
        "revocation_endpoint": base + PREFIX + "/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": sorted(SCOPES),
        "authorization_response_iss_parameter_supported": True,
    }


@router.get(PREFIX + "/authorize")
async def authorize(request: Request, svc: SvcDep) -> HTMLResponse:
    base = issuer(svc)
    await bounded(svc, request, "authorize", 30)
    params = dict(request.query_params)
    if any(len(request.query_params.getlist(k)) != 1 for k in params):
        raise oauth_error("invalid_request")
    client, redirect = params.get("client_id", ""), params.get("redirect_uri", "")
    if len(client) > 200 or not redirect_allowed(svc, client, redirect):
        raise oauth_error("invalid_client")
    scopes = set(params.get("scope", "context:read").split())
    if not scopes or not scopes <= SCOPES:
        raise oauth_error("invalid_scope")
    if (
        params.get("response_type") != "code"
        or params.get("code_challenge_method") != "S256"
        or not OPAQUE.fullmatch(params.get("code_challenge", ""))
        or params.get("resource") != resource(svc)
        or len(params.get("state", "")) > 1024
    ):
        raise oauth_error("invalid_request")
    if svc.email is None:
        raise HTTPException(503, "Email sign-in must be enabled for MCP consent")
    handle = secrets.token_urlsafe(32)
    grant = {
        "client_id": client,
        "redirect_uri": redirect,
        "resource": resource(svc),
        "scope": " ".join(sorted(scopes)),
        "state": params.get("state", ""),
        "challenge": params["code_challenge"],
        "issuer": base,
    }
    await svc.valkey.set(key("pending", handle), json.dumps(grant), ex=600)
    nonce = secrets.token_urlsafe(24)
    page = Path(__file__).with_name("personal_ai_consent.html").read_text("utf-8")
    page = page.replace("{{HANDLE}}", handle).replace("{{NONCE}}", nonce)
    page = page.replace("{{CLIENT}}", html.escape(client)).replace(
        "{{SCOPE}}", html.escape(grant["scope"])
    )
    return HTMLResponse(
        page,
        headers={
            **HEADERS,
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    approve: bool


@router.post(PREFIX + "/consent")
async def consent(
    body: Consent, request: Request, svc: SvcDep, account: AccountDep
) -> JSONResponse:
    base = issuer(svc)
    if request.headers.get("origin") != base:
        raise HTTPException(403, "Consent requires the same-origin page")
    await bounded(svc, request, "consent", 20)
    raw = await svc.valkey.getdel(key("pending", body.handle))
    if not raw:
        raise oauth_error("invalid_request")
    grant = json.loads(raw)
    if grant["issuer"] != base or not redirect_allowed(
        svc, grant["client_id"], grant["redirect_uri"]
    ):
        raise oauth_error("invalid_client")
    response = {"state": grant["state"], "iss": base}
    if body.approve:
        await require_link(svc, account)
        grant["account_id"] = str(account.user_id)
        code = secrets.token_urlsafe(32)
        await svc.valkey.set(key("code", code), json.dumps(grant), ex=120)
        response["code"] = code
    else:
        response["error"] = "access_denied"
    separator = "&" if "?" in grant["redirect_uri"] else "?"
    return JSONResponse(
        {"redirect_uri": grant["redirect_uri"] + separator + urlencode(response)}, headers=HEADERS
    )


async def form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise oauth_error("invalid_request")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 8192:
            raise oauth_error("invalid_request")
    try:
        values = parse_qs(data.decode("utf-8"), keep_blank_values=True, max_num_fields=12)
    except (ValueError, UnicodeDecodeError):
        raise oauth_error("invalid_request") from None
    if any(len(v) != 1 for v in values.values()):
        raise oauth_error("invalid_request")
    return {k: v[0] for k, v in values.items()}


@router.post(PREFIX + "/token")
async def token(request: Request, svc: SvcDep) -> JSONResponse:
    base = issuer(svc)
    await bounded(svc, request, "token")
    values = await form(request)
    if values.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=HEADERS)
    verifier = values.get("code_verifier", "")
    code = values.get("code", "")
    if not VERIFIER.fullmatch(verifier) or not OPAQUE.fullmatch(code):
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=HEADERS)
    raw = await svc.valkey.get(key("code", code))
    grant: dict[str, Any] = json.loads(raw) if raw else {}
    digest = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    if (
        not grant
        or grant["issuer"] != base
        or grant["challenge"] != digest
        or any(values.get(k) != grant[k] for k in ("client_id", "redirect_uri", "resource"))
        or not redirect_allowed(svc, grant["client_id"], grant["redirect_uri"])
    ):
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=HEADERS)
    if not await svc.valkey.eval(CONSUME, 1, key("code", code), raw):
        return JSONResponse({"error": "invalid_grant"}, status_code=400, headers=HEADERS)
    access = "mcp_" + secrets.token_urlsafe(32)
    ttl = svc.settings.personal_ai_oauth_token_ttl_s
    grant_id = secrets.token_urlsafe(16)
    record = {k: grant[k] for k in ("account_id", "client_id", "resource", "scope", "issuer")}
    record.update(id=grant_id, expires_at=int(time.time()) + ttl)
    token_key = key("access", access)
    # Store a hash reference only; the bearer value exists solely in this response.
    index = "mcp-oauth:account:" + grant["account_id"]
    issued = await svc.valkey.eval(
        """
local refs = redis.call('HGETALL', KEYS[2])
for i=1,#refs,2 do
 if redis.call('EXISTS', refs[i+1]) == 0 then redis.call('HDEL', KEYS[2], refs[i]) end
end
if redis.call('HLEN', KEYS[2]) >= 20 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('HSET', KEYS[2], ARGV[3], KEYS[1])
redis.call('EXPIRE', KEYS[2], 3600)
return 1
""",
        2,
        token_key,
        index,
        json.dumps(record),
        ttl,
        grant_id,
    )
    if not issued:
        return JSONResponse(
            {
                "error": "access_denied",
                "error_description": "Disconnect an existing AI connection first",
            },
            status_code=400,
            headers=HEADERS,
        )
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": grant["scope"],
        },
        headers=HEADERS,
    )


@router.post(PREFIX + "/revoke")
async def revoke(request: Request, svc: SvcDep) -> JSONResponse:
    issuer(svc)
    await bounded(svc, request, "revoke")
    values = await form(request)
    access = values.get("token", "")
    raw = await svc.valkey.get(key("access", access)) if len(access) <= 128 else None
    if raw and json.loads(raw)["client_id"] == values.get("client_id"):
        record = json.loads(raw)
        await svc.valkey.delete(key("access", access))
        await svc.valkey.hdel("mcp-oauth:account:" + record["account_id"], record["id"])
    return JSONResponse({}, headers=HEADERS)


@router.get(PREFIX + "/connections")
async def connections(svc: SvcDep, account: AccountDep) -> dict[str, Any]:
    index = "mcp-oauth:account:" + str(account.user_id)
    references = await svc.valkey.hgetall(index)
    result: list[dict[str, Any]] = []
    for identifier, token_key in references.items():
        raw = await svc.valkey.get(text(token_key))
        if raw:
            record = json.loads(raw)
            result.append({k: record[k] for k in ("id", "client_id", "scope", "expires_at")})
        else:
            await svc.valkey.hdel(index, text(identifier))
    return {"connections": result}


@router.delete(PREFIX + "/connections/{identifier}")
async def disconnect(identifier: str, svc: SvcDep, account: AccountDep) -> JSONResponse:
    index = "mcp-oauth:account:" + str(account.user_id)
    token_key = await svc.valkey.hget(index, identifier)
    if token_key:
        await svc.valkey.delete(text(token_key))
        await svc.valkey.hdel(index, identifier)
    return JSONResponse({}, headers=HEADERS)


async def mcp_account(request: Request, svc: SvcDep) -> Principal:
    authorization = request.headers.get("authorization")
    access = bearer_token(authorization)
    if access and access.startswith("mcp_"):
        raw = await svc.valkey.get(key("access", access)) if len(access) <= 128 else None
        if not raw:
            raise HTTPException(
                401,
                "MCP authorization expired or revoked",
                headers=challenge(svc, error="invalid_token"),
            )
        record = json.loads(raw)
        if record["issuer"] != issuer(svc) or record["resource"] != resource(svc):
            raise HTTPException(
                401, "Invalid token audience", headers=challenge(svc, error="invalid_token")
            )
        account = Principal(UUID(record["account_id"]), "MCP client", record["expires_at"])
        if (
            account.expires_at is None
            or account.expires_at <= time.time()
            or record["client_id"] not in svc.settings.personal_ai_oauth_clients
        ):
            raise HTTPException(
                401, "MCP authorization expired", headers=challenge(svc, error="invalid_token")
            )
        async with svc.tx(account) as conn:
            if not await conn.fetchval(
                "select 1 from app.accounts where id=$1 and status='active'",
                account.user_id,
            ):
                raise HTTPException(
                    401, "Account no longer exists", headers=challenge(svc, error="invalid_token")
                )
        actor_account.set(account.user_id)
        request.state.mcp_scopes = set(record["scope"].split())
    else:
        try:
            account = await current_account(svc, authorization)
        except HTTPException as exc:
            if exc.status_code == 401 and svc.settings.personal_ai_oauth_issuer:
                exc.headers = challenge(svc)
            raise
        request.state.mcp_scopes = SCOPES
    await require_link(svc, account)
    return account


McpAccountDep = Annotated[Principal, Depends(mcp_account)]


def require_scope(request: Request, svc: SvcDep, scope: str) -> None:
    if scope not in request.state.mcp_scopes:
        raise HTTPException(
            403,
            "MCP token does not grant this tool",
            headers=challenge(svc, scope, "insufficient_scope"),
        )
