"""Fictional private-brief REST state for native acceptance; no AI/OAuth provider calls.

The shared core models/correction rules keep pasted-brief inputs aligned with production.
Connected clients below are fabricated records for exercising the management UI, not grants.
"""

import copy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, Response
from pydantic import Field

from copilot_core import personal_ai as context


class Refresh(context.StrictModel):
    expected_revision: int = Field(ge=1)


def install(app, auth, profile_id):
    requests, versions, corrections, receipts = {}, {}, {}, {}
    connection_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    connections = {
        connection_id: {
            "id": connection_id,
            "client_id": "Fictional AI client",
            "scope": "context:read context:publish",
            "expires_at": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
        }
    }
    app.state.fixture_personal_ai = {
        "requests": requests,
        "versions": versions,
        "corrections": corrections,
        "receipts": receipts,
        "connections": connections,
    }

    def get(rid):
        if str(rid) not in requests:
            raise HTTPException(404, "Context request not found")
        return requests[str(rid)]

    def view(rid, *, include_brief=True):
        value = copy.deepcopy(get(rid))
        if include_brief and value["active_version_id"]:
            value["active_brief"] = copy.deepcopy(versions[value["active_version_id"]])
        return value

    def create(prep):
        rid = str(uuid4())
        requests[rid] = {
            "id": rid,
            "request_id": rid,
            "profile_id": profile_id,
            "request_revision": 1,
            "scope": prep.model_dump(mode="json"),
            "expected_brief_format": context.Brief.model_json_schema(),
            "active_version_id": None,
            "instructions": "Fictional local fixture. Prepare only from permitted sources; "
            "mark unknowns and preserve stable item IDs. No AI provider is called.",
        }
        corrections[rid] = {}
        return view(rid)

    def save_version(rid, brief, origin):
        vid = str(uuid4())
        versions[vid] = {
            "id": vid,
            "request_id": rid,
            "title": brief.title,
            "prepared_at": brief.prepared_at.isoformat(),
            "origin": origin,
            "brief": brief.model_dump(mode="json"),
            "content": context.brief_content(brief),
        }
        get(rid)["active_version_id"] = vid
        return vid

    # A ready brief permits native correction/exclusion acceptance without a real AI client.
    now = datetime.now(UTC)
    seed = create(
        context.Preparation(
            title="Fictional pilot preparation",
            questions=["What did the fictional pilot change, and what should happen next?"],
            date_from=now.date(),
            date_to=now.date(),
        )
    )
    save_version(
        seed["id"],
        context.Brief(
            title="Fictional pilot brief",
            prepared_at=now,
            items=[
                context.BriefItem(
                    id="pilot_decision",
                    kind="decision",
                    text="The fictional pilot led to a shorter launch checklist.",
                    provenance="sourced",
                    source_ids=["pilot_notes"],
                ),
                context.BriefItem(
                    id="pilot_next",
                    kind="next_step",
                    text="Review the fictional checklist with the team.",
                    provenance="user_report",
                ),
            ],
            sources=[
                context.Source(
                    id="pilot_notes",
                    title="Fictional pilot notes",
                    attribution="Synthetic native acceptance fixture",
                    source_date=now.date(),
                )
            ],
            coverage_gaps=["Synthetic data only; no private chats or model provider were read."],
        ),
        "paste",
    )

    @app.get("/v1/personal-ai/requests")
    async def listing(request: Request):
        auth(request)
        return {"requests": [view(rid) for rid in reversed(requests)]}

    @app.post("/v1/personal-ai/requests", status_code=201)
    async def create_request(body: context.Preparation, request: Request):
        auth(request)
        workspace = app.state.fixture_workspace
        if body.employer_id and str(body.employer_id) not in workspace["companies"]:
            raise HTTPException(404, "Company not found in selected profile")
        if body.job_id and str(body.job_id) not in workspace["applications"]:
            raise HTTPException(404, "Application not found in selected profile")
        if len(requests) >= 100:
            raise HTTPException(409, "Remove an old context request before creating another")
        return create(body)

    @app.get("/v1/personal-ai/requests/{rid}")
    async def detail(rid: UUID, request: Request):
        auth(request)
        return view(rid)

    @app.post("/v1/personal-ai/requests/{rid}/refresh")
    async def refresh(rid: UUID, body: Refresh, request: Request):
        auth(request)
        value = get(rid)
        if body.expected_revision != value["request_revision"]:
            raise HTTPException(409, "Request changed; reload before refreshing")
        value["request_revision"] += 1
        return view(rid, include_brief=False)

    @app.post("/v1/personal-ai/requests/{request_id}/publish")
    async def publish(request_id: UUID, body: context.Publication, request: Request):
        auth(request)
        rid = str(request_id)
        if get(rid)["request_revision"] != body.request_revision:
            raise HTTPException(409, "Preparation request is obsolete; get the current request")
        key = (rid, body.idempotency_key)
        fingerprint = body.model_dump_json()
        if key in receipts:
            prior, vid = receipts[key]
            if fingerprint != prior:
                raise HTTPException(409, "Idempotency key was already used for different content")
            return {"version_id": vid, "request_id": rid, "duplicate": True}
        brief = context.apply_corrections(body.brief, corrections[rid])
        vid = save_version(rid, brief, "paste")
        receipts[key] = (fingerprint, vid)
        return {"version_id": vid, "request_id": rid, "duplicate": False}

    @app.patch("/v1/personal-ai/requests/{request_id}/items/{iid}")
    async def correct(request_id: UUID, iid: str, body: context.Correction, request: Request):
        auth(request)
        rid = str(request_id)
        value = get(rid)
        if value["active_version_id"] != str(body.expected_version_id):
            raise HTTPException(409, "Brief changed; reload before correcting")
        brief = context.Brief.model_validate(versions[value["active_version_id"]]["brief"])
        if iid not in {item.id for item in brief.items}:
            raise HTTPException(404, "Brief item not found")
        corrections[rid][iid] = body.text
        updated = context.apply_corrections(brief, corrections[rid])
        return {"version_id": save_version(rid, updated, "correction"), "request_id": rid}

    @app.delete("/v1/personal-ai/requests/{request_id}", status_code=204)
    async def delete(request_id: UUID, request: Request):
        auth(request)
        rid = str(request_id)
        get(rid)
        del requests[rid], corrections[rid]
        for vid in [vid for vid, value in versions.items() if value["request_id"] == rid]:
            del versions[vid]
        for key in [key for key in receipts if key[0] == rid]:
            del receipts[key]
        return Response(status_code=204)

    @app.get("/v1/personal-ai/oauth/connections")
    async def list_connections(request: Request):
        auth(request)
        return {"connections": list(connections.values())}

    @app.delete("/v1/personal-ai/oauth/connections/{identifier}")
    async def revoke(identifier: str, request: Request):
        auth(request)
        connections.pop(identifier, None)
        return {}
