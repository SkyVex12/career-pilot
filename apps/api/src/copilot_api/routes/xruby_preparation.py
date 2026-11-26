"""Explicit user-confirmed preparation commands; ordinary read planning cannot call them."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from copilot_api import xruby_preparation as service
from copilot_api.deps import SvcDep
from copilot_api.routes.xruby_oauth import Delegated
from copilot_api.routes.xruby_reads import bounded_response, query_fields

router = APIRouter(prefix="/v1/xruby", tags=["xruby-preparation"])


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.post("/interviews/{interview_id}/preparation-requests", status_code=202)
async def prepare(
    request: Request,
    interview_id: UUID,
    body: Prepare,
    svc: SvcDep,
    grant: Delegated,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    query_fields(request, set())
    if body.revision != body.expected_hash:
        raise HTTPException(409, "Use the current composite preparation citation")
    result = await service.reserve(svc, grant, interview_id, body.expected_hash, idempotency_key)
    response = bounded_response(result)
    response.status_code = 202
    return response


@router.get("/preparation-requests/{request_id}")
async def status(request: Request, request_id: UUID, svc: SvcDep, grant: Delegated):
    query_fields(request, set())
    async with asyncio.timeout(15):
        return bounded_response(await service.result(svc, grant, request_id))
