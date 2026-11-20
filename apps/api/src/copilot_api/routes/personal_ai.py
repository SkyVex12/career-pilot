"""Private Context from my AI REST workflow and two authenticated MCP tools.

Streamable HTTP JSON responses, protocol 2025-11-25 compatibility profile. No SSE/session
state or delegated source credentials. Authentication is the API's existing bearer token.
"""

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError

from copilot_api.deps import AccountDep, SvcDep, UserDep
from copilot_api.routes.personal_ai_oauth import McpAccountDep, require_scope
from copilot_core import keys, profiles
from copilot_core import personal_ai as context
from copilot_core.auth import Principal
from copilot_core.valkey_client import text

router = APIRouter(prefix="/personal-ai", tags=["personal-ai"])
mcp_router = APIRouter(prefix="/personal-ai", tags=["personal-ai-mcp"])
PROTOCOL_VERSION = "2025-11-25"
MAX_MCP_BYTES = 65536
# Compare ownership and context atomically; never recreate an expired session or replace
# another account's sticky marker after a slow database/context lookup.
CLAIM_PRIVATE = """
local account = redis.call('HGET', KEYS[1], 'account_id')
local profile = redis.call('HGET', KEYS[1], 'user_id')
local status = redis.call('HGET', KEYS[1], 'status')
local marker = redis.call('HGET', KEYS[1], 'private_context_account_id')
if account ~= ARGV[1] or profile ~= ARGV[2] or not status or status == 'ended' then return 0 end
if marker and marker ~= '' and marker ~= ARGV[1] then return 0 end
if (redis.call('HGET', KEYS[1], 'employer_id') or '') ~= ARGV[3] then return 0 end
if (redis.call('HGET', KEYS[1], 'job_id') or '') ~= ARGV[4] then return 0 end
redis.call('HSET', KEYS[1], 'private_context_account_id', ARGV[1], 'sharing', '0')
return 1
"""


class Refresh(context.StrictModel):
    expected_revision: int = Field(ge=1)


class GetArguments(context.StrictModel):
    request_id: UUID


class PublishArguments(context.Publication):
    request_id: UUID


async def _scope(svc: SvcDep, user: Principal, prep: context.Preparation) -> None:
    async with svc.tx(user) as conn:
        if prep.employer_id is not None and not await conn.fetchval(
            "select 1 from app.employers where id=$1 and user_id=$2", prep.employer_id, user.user_id
        ):
            raise HTTPException(404, "Company not found in selected profile")
        if prep.job_id is not None and not await conn.fetchval(
            "select 1 from app.applications where id=$1 and user_id=$2", prep.job_id, user.user_id
        ):
            raise HTTPException(404, "Application not found in selected profile")


def _error(exc: context.ContextError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.post("/requests", status_code=201)
async def create(
    body: context.Preparation, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    await _scope(svc, user, body)
    try:
        async with svc.tx(account) as conn:
            return await context.create_request(
                conn, svc.crypto, account.user_id, user.user_id, body
            )
    except context.ContextError as exc:
        raise _error(exc) from exc


@router.get("/requests")
async def listing(svc: SvcDep, user: UserDep, account: AccountDep) -> dict[str, Any]:
    async with svc.tx(account) as conn:
        return {
            "requests": await context.list_requests(conn, svc.crypto, account.user_id, user.user_id)
        }


@router.get("/requests/{request_id}")
async def get(request_id: UUID, svc: SvcDep, user: UserDep, account: AccountDep) -> dict[str, Any]:
    try:
        async with svc.tx(account) as conn:
            result = await context.get_request(
                conn, svc.crypto, account.user_id, request_id, user.user_id
            )
            if result["active_version_id"]:
                result["active_brief"] = await context.read_version(
                    conn,
                    svc.crypto,
                    account.user_id,
                    user.user_id,
                    UUID(result["active_version_id"]),
                )
            return result
    except context.ContextError as exc:
        raise _error(exc) from exc


@router.post("/requests/{request_id}/refresh")
async def refresh(
    request_id: UUID, body: Refresh, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    try:
        async with svc.tx(account) as conn:
            return await context.refresh_request(
                conn, svc.crypto, account.user_id, user.user_id, request_id, body.expected_revision
            )
    except context.ContextError as exc:
        raise _error(exc) from exc


@router.post("/requests/{request_id}/publish")
async def paste(
    request_id: UUID, body: context.Publication, svc: SvcDep, user: UserDep, account: AccountDep
) -> dict[str, Any]:
    try:
        async with svc.tx(account) as conn:
            return await context.publish(
                conn, svc.crypto, account.user_id, user.user_id, request_id, body, "paste"
            )
    except context.ContextError as exc:
        raise _error(exc) from exc


@router.patch("/requests/{request_id}/items/{item_id}")
async def correct(
    request_id: UUID,
    item_id: str,
    body: context.Correction,
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
) -> dict[str, Any]:
    try:
        async with svc.tx(account) as conn:
            return await context.correct_item(
                conn, svc.crypto, account.user_id, user.user_id, request_id, item_id, body
            )
    except context.ContextError as exc:
        raise _error(exc) from exc


@router.delete("/requests/{request_id}", status_code=204)
async def remove(request_id: UUID, svc: SvcDep, user: UserDep, account: AccountDep) -> Response:
    try:
        async with svc.tx(account) as conn:
            await context.delete_request(conn, account.user_id, user.user_id, request_id)
    except context.ContextError as exc:
        raise _error(exc) from exc
    return Response(status_code=204)


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_context_request",
            "description": "Read one private preparation request "
            "created by you in Career Pilot. Includes scope and expected source-aware brief.",
            "inputSchema": GetArguments.model_json_schema(),
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
        },
        {
            "name": "publish_context_brief",
            "description": "Publish a bounded sourced brief to "
            "your Career Pilot request. Review the selected scope before approving the write. "
            "Does not edit your resume, task tracker, or work logs.",
            "inputSchema": PublishArguments.model_json_schema(),
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        },
    ]


async def call_tool(
    name: str, arguments: dict[str, Any], svc: SvcDep, account: Principal
) -> dict[str, Any]:
    if name not in {"get_context_request", "publish_context_brief"}:
        raise context.ContextError(404, "Unknown tool")
    parsed = (
        GetArguments.model_validate(arguments)
        if name == "get_context_request"
        else PublishArguments.model_validate(arguments)
    )
    # Authorizing account first, then re-check current membership of the stored profile.
    async with svc.tx(account) as conn:
        request = await context.get_request(conn, svc.crypto, account.user_id, parsed.request_id)
    try:
        profile = await profiles.resolve(svc.pool, account, request["profile_id"])
    except profiles.ProfileError as exc:
        raise context.ContextError(404, "Profile access is no longer available") from exc
    if name == "get_context_request":
        return request
    assert isinstance(parsed, PublishArguments)
    publication = context.Publication.model_validate(parsed.model_dump(exclude={"request_id"}))
    async with svc.tx(account) as conn:
        return await context.publish(
            conn,
            svc.crypto,
            account.user_id,
            profile.user_id,
            parsed.request_id,
            publication,
            "mcp",
        )


def rpc_error(request_id: object, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


@mcp_router.post("/mcp")
async def mcp(request: Request, svc: SvcDep, account: McpAccountDep) -> Response:
    # Browser-origin protection; normal native MCP clients do not send Origin.
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "MCP origin is not allowed")
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version != PROTOCOL_VERSION:
        raise HTTPException(400, f"Supported MCP protocol: {PROTOCOL_VERSION}")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_MCP_BYTES:
            raise HTTPException(413, "MCP request exceeds 64 KiB")
    try:
        raw: Any = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return rpc_error(None, -32700, "Invalid JSON")
    if not isinstance(raw, dict):
        return rpc_error(None, -32600, "Expected one JSON-RPC 2.0 message")
    message = cast(dict[str, Any], raw)
    if message.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Expected one JSON-RPC 2.0 message")
    request_id = message.get("id")
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, (int, str))
    ):
        return rpc_error(None, -32600, "Invalid request ID")
    method = message.get("method")
    if request_id is None:
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return Response(status_code=202)
        return rpc_error(None, -32600, "Tool operations require a request ID")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return rpc_error(request_id, -32602, "Parameters must be an object")
    params = cast(dict[str, Any], params)
    result: dict[str, Any]
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "career-pilot-context", "version": "1.0.0"},
            "instructions": "Only read and publish requests you created in Career Pilot. "
            "Publishing needs user review. No conversation history is collected.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                tool
                for tool in tools()
                if ("context:read" if tool["name"] == "get_context_request" else "context:publish")
                in request.state.mcp_scopes
            ]
        }
    elif method == "tools/call":
        require_scope(
            request,
            svc,
            "context:publish" if params.get("name") == "publish_context_brief" else "context:read",
        )
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict) or not isinstance(params.get("name"), str):
            return rpc_error(request_id, -32602, "Tool name and object arguments are required")
        try:
            data = await call_tool(params["name"], cast(dict[str, Any], arguments), svc, account)
            result = {
                "content": [{"type": "text", "text": json.dumps(data)}],
                "structuredContent": data,
                "isError": False,
            }
        except (context.ContextError, ValidationError) as exc:
            message_text = (
                str(exc) if isinstance(exc, context.ContextError) else "Invalid brief or arguments"
            )
            result = {"content": [{"type": "text", "text": message_text}], "isError": True}
    else:
        return rpc_error(request_id, -32601, "Method not supported")
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


@router.get("/live-context")
async def live_context(
    session_id: UUID,
    svc: SvcDep,
    user: UserDep,
    account: AccountDep,
    employer_id: UUID | None = None,
    job_id: UUID | None = None,
) -> dict[str, Any]:
    """Revalidate current private source access; sticky privacy is committed before use."""
    raw = await svc.valkey.hgetall(keys.sess_state(session_id))
    state = {text(k): text(v) for k, v in raw.items()}
    if (
        state.get("user_id") != str(user.user_id)
        or state.get("account_id") != str(account.user_id)
        or state.get("status") in (None, "ended")
        or state.get("private_context_account_id") not in (None, "", str(account.user_id))
    ):
        raise HTTPException(404, "No such active session")
    # The caller cannot route another company's private context into this session.
    if str(employer_id or "") != state.get("employer_id", "") or str(job_id or "") != state.get(
        "job_id", ""
    ):
        raise HTTPException(409, "Session context scope does not match")
    async with svc.tx(account) as conn:
        briefs = await context.latest_briefs(
            conn, svc.crypto, account.user_id, user.user_id, employer_id, job_id
        )
    if not briefs:
        return {"briefs": []}
    async with svc.tx(user) as conn:
        await conn.execute(
            "select pg_advisory_xact_lock(hashtextextended($1, 0))", "private:" + str(session_id)
        )
        if await conn.fetchval(
            "select 1 from app.session_seats where session_id=$1 "
            "and status='admitted' and expires_at > now() limit 1",
            session_id,
        ):
            return {"briefs": [], "unavailable": "Private AI context is unavailable with guests"}
        # Create the row if the worker has not flushed yet. Otherwise a concurrent first
        # flush could insert an unmarked history row after the first private answer.
        agenda_enc, agenda_key = None, None
        if state.get("agenda"):
            agenda_enc, agenda_key = await svc.crypto.encrypt(
                conn, user.user_id, "sessions", str(session_id), state["agenda"]
            )
        if not state.get("started_at") or state.get("mode") not in {
            "interview",
            "standup",
            "meeting",
        }:
            raise HTTPException(409, "Session is still starting; retry context retrieval")
        marked = await conn.fetchval(
            "insert into app.sessions (id,user_id,mode,job_id,interview_id,platform,title,"
            "store_transcript,started_at,employer_id,spoken_language,private_context_account_id,"
            "agenda_enc,agenda_key_version) values "
            "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) "
            "on conflict (id) do update set private_context_account_id=$12 "
            "where app.sessions.private_context_account_id is null "
            "or app.sessions.private_context_account_id=$12 returning id",
            session_id,
            user.user_id,
            state["mode"],
            job_id,
            UUID(state["interview_id"]) if state.get("interview_id") else None,
            state.get("platform") or None,
            state.get("title") or None,
            state.get("store_transcript") == "1",
            datetime.fromisoformat(state["started_at"]),
            employer_id,
            state.get("spoken_language") or None,
            account.user_id,
            agenda_enc,
            agenda_key,
        )
        if marked is None:
            raise HTTPException(409, "Session private context belongs to another account")
    # Never clear this marker on deletion/refresh: generated artifacts remain private.
    claimed = await svc.valkey.eval(
        CLAIM_PRIVATE,
        1,
        keys.sess_state(session_id),
        str(account.user_id),
        str(user.user_id),
        str(employer_id or ""),
        str(job_id or ""),
    )
    if not claimed:
        raise HTTPException(409, "Session ownership or context changed; retry")
    return {"briefs": briefs}
