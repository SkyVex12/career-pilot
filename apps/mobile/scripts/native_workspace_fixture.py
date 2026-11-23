"""Stateful fictional workspace data for native UI acceptance, never production data.

No extraction, model, storage, calendar or recruiting provider is called here.
The real API and storage contracts are validated separately by the API stack tests.
"""

from datetime import UTC, datetime
from urllib.parse import unquote
from uuid import uuid4

from fastapi import HTTPException, Request, Response


def install(app, auth):
    company_id = "77777777-7777-4777-8777-777777777777"
    application_id = "44444444-4444-4444-8444-444444444444"
    resume_id = "99999999-9999-4999-8999-999999999999"
    company = dict(
        id=company_id,
        name="Fictional Acme",
        title="Product engineer",
        team="Platform",
        status="active",
        started_on="2024-06-01",
        ended_on=None,
    )
    companies, logs, documents, facts = {company_id: company}, {}, {}, {}
    applications = {
        application_id: dict(
            id=application_id,
            company="Fictional Acme",
            role="Product engineer",
            stage="interview",
            url=None,
            source_site=None,
            note="Fictional test application",
            jd_text="Build accessible software and explain technical decisions.",
            resume=None,
            versions=[],
            pack={"available": True},
        )
    }
    resumes = {
        resume_id: dict(
            id=resume_id,
            kind="base",
            label="Fictional base resume",
            status="extracted",
            source="text",
            text="Fictional engineer. Built a pilot and improved the release checklist.",
            used_by=[application_id],
            application=None,
            file=None,
            digest={"facts": 2},
        )
    }
    interviews = {}
    fact = dict(
        id=str(uuid4()),
        kind="decision",
        title="Pilot first",
        summary="Run a small fictional pilot before the wider launch.",
        status="active",
        day=datetime.now(UTC).date().isoformat(),
    )
    facts[company_id] = {fact["id"]: fact}
    app.state.fixture_workspace = dict(
        companies=companies,
        logs=logs,
        documents=documents,
        facts=facts,
        applications=applications,
        resumes=resumes,
        interviews=interviews,
    )

    profile_id = "11111111-1111-4111-8111-111111111111"
    source_user = {"id": "fictional-hubruby-user", "name": "Fictional test profile"}
    hub = {
        "connected": False,
        "available": True,
        "revoked": False,
        "only_sign_in_method": False,
        "connected_at": None,
        "last_pull_at": None,
        "revision": 1,
        "imported_id": None,
    }
    now = datetime.now(UTC).isoformat()
    calendars = {
        "google": dict(
            provider="google",
            available=True,
            connected=True,
            status="active",
            calendars=[{"id": "fictional-calendar", "name": "Fictional interviews"}]
            + [{"id": f"fictional-{i}", "name": f"Fictional calendar {i}"} for i in range(1, 12)],
            selected_calendars=["fictional-calendar"],
            last_sync_at=now,
            last_error=None,
        ),
        "outlook": dict(
            provider="outlook",
            available=False,
            connected=False,
            status="disconnected",
            calendars=[],
            selected_calendars=[],
            last_sync_at=None,
            last_error=None,
        ),
    }
    app.state.fixture_workspace.update(
        hub=hub,
        source_user=source_user,
        calendars=calendars,
        calendar_events_visible=True,
        calendar_sync_error=None,
    )

    def require(mapping, key):
        if key not in mapping:
            raise HTTPException(404, "Fictional record not found")
        return mapping[key]

    @app.get("/v1/employers")
    async def employer_list(request: Request):
        auth(request)
        return {"employers": list(companies.values())}

    @app.post("/v1/employers", status_code=201)
    async def employer_create(request: Request):
        auth(request)
        value = dict(
            id=str(uuid4()),
            name="",
            title="",
            team="",
            status="active",
            started_on=None,
            ended_on=None,
        )
        value.update(await request.json())
        companies[value["id"]] = value
        return value

    @app.patch("/v1/employers/{cid}")
    async def employer_update(cid: str, request: Request):
        auth(request)
        value = require(companies, cid)
        value.update(await request.json())
        return value

    @app.delete("/v1/employers/{cid}", status_code=202)
    async def employer_delete(cid: str, request: Request, confirm: str = ""):
        auth(request)
        value = require(companies, cid)
        if confirm.strip().casefold() != value["name"].strip().casefold():
            raise HTTPException(422, "Confirm the company name")
        del companies[cid]
        logs.pop(cid, None)
        documents.pop(cid, None)
        facts.pop(cid, None)
        return {"deleted": {}}

    @app.get("/v1/employers/{cid}/logs")
    async def log_list(cid: str, request: Request, before: str | None = None):
        auth(request)
        require(companies, cid)
        entries = [
            v
            for k, v in sorted(logs.get(cid, {}).items(), reverse=True)
            if before is None or k < before
        ]
        return {
            "logs": entries[:30],
            "next_before": entries[29]["day"] if len(entries) > 30 else None,
        }

    @app.get("/v1/employers/{cid}/logs/{day}")
    async def log_get(cid: str, day: str, request: Request):
        auth(request)
        return require(logs.get(cid, {}), day)

    @app.put("/v1/employers/{cid}/logs/{day}")
    async def log_save(cid: str, day: str, request: Request):
        auth(request)
        require(companies, cid)
        body = await request.json()
        existing = logs.setdefault(cid, {}).get(day, {})
        value = dict(
            id=existing.get("id", str(uuid4())),
            day=day,
            text=body["text"],
            status="ready",
            attachments=existing.get("attachments", []),
            items=[],
        )
        logs[cid][day] = value
        return value

    @app.delete("/v1/employers/{cid}/logs/{day}", status_code=204)
    async def log_delete(cid: str, day: str, request: Request):
        auth(request)
        require(logs.get(cid, {}), day)
        del logs[cid][day]
        return Response(status_code=204)

    @app.post("/v1/employers/{cid}/logs/{day}/attachments", status_code=201)
    async def attachment_add(cid: str, day: str, request: Request):
        auth(request)
        entry = require(logs.get(cid, {}), day)
        raw = await request.body()
        value = dict(
            id=str(uuid4()),
            name=unquote(request.headers.get("x-file-name", "Fixture attachment")),
            content_type=request.headers.get("content-type"),
            size_bytes=len(raw),
        )
        entry["attachments"].append(value)
        return value

    @app.get("/v1/employers/{cid}/documents")
    async def document_list(cid: str, request: Request):
        auth(request)
        require(companies, cid)
        return {"documents": list(documents.get(cid, {}).values())}

    @app.get("/v1/employers/{cid}/documents/{did}")
    async def document_get(cid: str, did: str, request: Request):
        auth(request)
        return require(documents.get(cid, {}), did)

    @app.post("/v1/employers/{cid}/documents", status_code=201)
    async def document_upload(cid: str, request: Request):
        auth(request)
        require(companies, cid)
        raw = await request.body()
        name = unquote(request.headers.get("x-file-name", "Fixture document"))
        previous = next((d for d in documents.get(cid, {}).values() if d["name"] == name), None)
        value = dict(
            id=previous["id"] if previous else str(uuid4()),
            name=name,
            status="indexed",
            error=None,
            headings=["Fictional document"],
            facts=[],
            size_bytes=len(raw),
        )
        documents.setdefault(cid, {})[value["id"]] = value
        return value

    @app.delete("/v1/employers/{cid}/documents/{did}", status_code=204)
    async def document_delete(cid: str, did: str, request: Request):
        auth(request)
        if did in documents.get(cid, {}):
            del documents[cid][did]
        else:
            for entry in logs.get(cid, {}).values():
                entry["attachments"] = [a for a in entry["attachments"] if a["id"] != did]
        return Response(status_code=204)

    @app.get("/v1/employers/{cid}/memory")
    async def memory_list(cid: str, request: Request):
        auth(request)
        require(companies, cid)
        return {"memory": list(facts.get(cid, {}).values())}

    @app.patch("/v1/memory/{mid}")
    async def memory_update(mid: str, request: Request):
        auth(request)
        values = next((v for v in facts.values() if mid in v), {})
        item = require(values, mid)
        item.update(await request.json())
        return item

    @app.delete("/v1/memory/{mid}", status_code=204)
    async def memory_delete(mid: str, request: Request):
        auth(request)
        for values in facts.values():
            values.pop(mid, None)
        return Response(status_code=204)

    @app.get("/v1/proposals")
    async def proposals(request: Request):
        auth(request)
        return {"proposals": []}

    def resume_choices(aid):
        return [
            {k: r[k] for k in ("id", "kind", "label", "status")}
            for r in resumes.values()
            if r["kind"] == "base" or (r["application"] or {}).get("id") == aid
        ]

    @app.get("/v1/applications")
    async def application_list(request: Request):
        auth(request)
        return {"applications": list(applications.values())}

    @app.get("/v1/applications/{aid}")
    async def application_get(aid: str, request: Request):
        auth(request)
        return {**require(applications, aid), "versions": resume_choices(aid)}

    @app.post("/v1/applications", status_code=201)
    async def application_create(request: Request):
        auth(request)
        body = await request.json()
        value = dict(
            id=str(uuid4()),
            url=None,
            source_site=None,
            note=None,
            jd_text="",
            stage="applied",
            resume=None,
            versions=[],
            pack={"available": True},
        )
        value.update(body)
        applications[value["id"]] = value
        return value

    @app.patch("/v1/applications/{aid}")
    async def application_update(aid: str, request: Request):
        auth(request)
        value = require(applications, aid)
        body = await request.json()
        rid = body.pop("resume_version_id", None)
        if rid:
            resume = require(resumes, rid)
            value["resume"] = dict(
                version_id=rid,
                kind=resume["kind"],
                label=resume["label"],
                status=resume["status"],
                manual=True,
            )
        value.update(body)
        return {**value, "versions": resume_choices(aid)}

    @app.delete("/v1/applications/{aid}", status_code=204)
    async def application_delete(aid: str, request: Request):
        auth(request)
        require(applications, aid)
        del applications[aid]
        for iid in [k for k, v in interviews.items() if v["job_id"] == aid]:
            del interviews[iid]
        return Response(status_code=204)

    @app.get("/v1/resumes")
    async def resume_list(request: Request):
        auth(request)
        return {
            "base": next((r for r in resumes.values() if r["kind"] == "base"), None),
            "versions": [r for r in resumes.values() if r["kind"] != "base"],
        }

    @app.get("/v1/resumes/{rid}")
    async def resume_get(rid: str, request: Request):
        auth(request)
        return require(resumes, rid)

    @app.post("/v1/resumes", status_code=201)
    async def resume_create(request: Request):
        auth(request)
        body = await request.json()
        aid = body.get("application_id")
        application = require(applications, aid) if aid else None
        value = dict(
            id=str(uuid4()),
            kind=body["kind"],
            label=body.get("label") or "Fictional resume",
            text=body["text"],
            status="extracted",
            source="text",
            used_by=[],
            application={k: application[k] for k in ("id", "company", "role")}
            if application
            else None,
            file=None,
            digest={"facts": 1},
        )
        if value["kind"] == "base":
            for rid in [k for k, r in resumes.items() if r["kind"] == "base"]:
                del resumes[rid]
        resumes[value["id"]] = value
        return value

    @app.patch("/v1/resumes/{rid}")
    async def resume_update(rid: str, request: Request):
        auth(request)
        value = require(resumes, rid)
        value.update(await request.json())
        return value

    @app.delete("/v1/resumes/{rid}", status_code=204)
    async def resume_delete(rid: str, request: Request):
        auth(request)
        require(resumes, rid)
        del resumes[rid]
        for application in applications.values():
            if (application["resume"] or {}).get("version_id") == rid:
                application["resume"] = None
        return Response(status_code=204)

    @app.get("/v1/interviews")
    async def interview_list(request: Request):
        auth(request)
        return {"interviews": list(interviews.values())}

    @app.post("/v1/interviews", status_code=201)
    async def interview_create(request: Request):
        auth(request)
        body = await request.json()
        require(applications, body["job_id"])
        value = {**body, "id": str(uuid4()), "pack": {"available": True}}
        interviews[value["id"]] = value
        return value

    @app.get("/v1/documents")
    async def uploads(request: Request):
        auth(request)
        return {"documents": [], "digest": None}

    # All provider state below is fictional and stays in memory. Authorization never
    # opens a real provider or invents credentials; the normal API stack tests cover it.
    @app.get("/v1/calendars")
    async def calendar_connections(request: Request):
        auth(request)
        return {"items": list(calendars.values())}

    @app.post("/v1/calendars/{provider}/connect")
    async def calendar_connect(provider: str, request: Request):
        auth(request)
        require(calendars, provider)
        raise HTTPException(
            503,
            "This fictional fixture never opens a real calendar sign-in. "
            "Restart the fixture to restore its sample connection.",
        )

    @app.patch("/v1/calendars/{provider}/selection")
    async def calendar_selection(provider: str, request: Request):
        auth(request)
        connection = require(calendars, provider)
        if not connection["connected"]:
            raise HTTPException(409, "Calendar not connected")
        selected = (await request.json()).get("calendar_ids")
        allowed = {row["id"] for row in connection["calendars"]}
        if (
            not isinstance(selected, list)
            or len(selected) > 10
            or not all(isinstance(k, str) and k in allowed for k in selected)
            or len(set(selected)) != len(selected)
        ):
            raise HTTPException(422, "Choose up to 10 available calendars")
        connection["selected_calendars"] = selected
        app.state.fixture_workspace["calendar_events_visible"] = False
        return {"selected_calendars": selected}

    @app.post("/v1/calendars/{provider}/sync")
    async def calendar_sync(provider: str, request: Request):
        auth(request)
        connection = require(calendars, provider)
        if not connection["connected"]:
            raise HTTPException(409, "Calendar not connected")
        if app.state.fixture_workspace["calendar_sync_error"]:
            raise HTTPException(429, app.state.fixture_workspace["calendar_sync_error"])
        connection["last_sync_at"] = datetime.now(UTC).isoformat()
        show = "fictional-calendar" in connection["selected_calendars"]
        app.state.fixture_workspace["calendar_events_visible"] = show
        return {"event_count": int(show), "synced_at": connection["last_sync_at"]}

    @app.delete("/v1/calendars/{provider}")
    async def calendar_disconnect(provider: str, request: Request):
        auth(request)
        connection = require(calendars, provider)
        connection.update(
            connected=False,
            status="disconnected",
            calendars=[],
            selected_calendars=[],
            last_sync_at=None,
            last_error=None,
        )
        app.state.fixture_workspace["calendar_events_visible"] = False
        return {
            "disconnected": True,
            "provider_revoked": True,
            "provider_consent_removal_required": False,
        }

    def hub_connection():
        if not hub["connected"] and not hub["revoked"]:
            return {"available": hub["available"], "connected": False, "required": False}
        return {
            "available": hub["available"],
            "connected": hub["connected"] and not hub["revoked"],
            "required": False,
            "status": "revoked" if hub["revoked"] else "active",
            "account_type": "user",
            "user": source_user,
            "connected_at": hub["connected_at"],
            "last_pull_at": hub["last_pull_at"],
        }

    def hub_active():
        if not hub["available"]:
            raise HTTPException(503, "HubRuby is not configured in this fixture")
        if not hub["connected"] or hub["revoked"]:
            raise HTTPException(409, "HubRuby is not connected")

    @app.get("/v1/career-os/connection")
    async def hub_get(request: Request):
        auth(request)
        return hub_connection()

    @app.post("/v1/career-os/connect", status_code=201)
    async def hub_connect(request: Request):
        auth(request)
        if not hub["available"]:
            raise HTTPException(503, "HubRuby is not configured in this fixture")
        if hub["connected"] and not hub["revoked"]:
            raise HTTPException(409, "HubRuby is already connected")
        if (await request.json()).get("code", "").strip().upper() != "FIXTURE-IMPORT":
            raise HTTPException(
                400, "Fictional fixture uses FIXTURE-IMPORT; no real code is accepted"
            )
        hub.update(connected=True, revoked=False, connected_at=datetime.now(UTC).isoformat())
        return hub_connection()

    @app.delete("/v1/career-os/connection", status_code=204)
    async def hub_disconnect(request: Request):
        auth(request)
        if hub["only_sign_in_method"]:
            raise HTTPException(
                409,
                {
                    "code": "only_sign_in_method",
                    "message": "Add another sign-in before disconnecting",
                },
            )
        hub.update(connected=False, revoked=False, connected_at=None, last_pull_at=None)
        return Response(status_code=204)

    def hub_source():
        previous = applications.get(hub["imported_id"])
        return dict(
            id="fictional-hubruby-application",
            company="Fictional HubRuby Labs",
            role="Mobile engineer",
            stage="interview",
            user=source_user,
            profile_id=profile_id,
            resume="tailored",
            created_at=now,
            updated_at=now,
            source_site="HubRuby",
            url=None,
            pulled=None
            if previous is None
            else dict(
                application_id=previous["id"],
                pulled_at=previous["career_os"]["pulled_at"],
                changed=previous["career_os"]["content_hash"]
                != f"fixture-revision-{hub['revision']}",
            ),
        )

    @app.get("/v1/career-os/applications")
    async def hub_search(
        request: Request, q: str = "", stage: str = "", user_id: str = "", cursor: str = ""
    ):
        auth(request)
        hub_active()
        item = hub_source()
        found = (
            not cursor
            and (not user_id or user_id == source_user["id"])
            and (not stage or stage == item["stage"])
            and q.casefold() in f"{item['company']} {item['role']}".casefold()
        )
        return {"items": [item] if found else [], "next_cursor": None}

    @app.get("/v1/career-os/applications/{source_id}/status")
    async def hub_status(source_id: str, request: Request):
        auth(request)
        hub_active()
        item = hub_source()
        if source_id != item["id"] or not item["pulled"]:
            raise HTTPException(404, "Fictional application has not been imported")
        return {
            "changed": item["pulled"]["changed"],
            "pulled_at": item["pulled"]["pulled_at"],
            "updated_at": now,
        }

    @app.post("/v1/career-os/applications/{source_id}/pull")
    async def hub_pull(source_id: str, request: Request):
        auth(request)
        hub_active()
        source = hub_source()
        if source_id != source["id"]:
            raise HTTPException(404, "Unknown fictional source")
        previous = applications.get(hub["imported_id"])
        aid = previous["id"] if previous else str(uuid4())
        imported_at = datetime.now(UTC).isoformat()
        value = {
            **(previous or {}),
            "id": aid,
            "company": source["company"],
            "role": source["role"],
            "stage": source["stage"],
            "source_site": "HubRuby",
            "url": None,
            "note": "Fictional imported application",
            "jd_text": f"Build readable mobile interfaces. Fictional revision {hub['revision']}.",
            "pack": {"available": True},
            "career_os": {
                "application_id": source_id,
                "pulled_at": imported_at,
                "content_hash": f"fixture-revision-{hub['revision']}",
            },
        }
        rid = next(
            (
                r["id"]
                for r in resumes.values()
                if (r["application"] or {}).get("id") == aid and r["source"] == "career_os"
            ),
            str(uuid4()),
        )
        resumes[rid] = dict(
            id=rid,
            kind="tailored",
            label="Fictional HubRuby resume",
            status="extracted",
            source="career_os",
            text=f"Fictional mobile engineer, revision {hub['revision']}.",
            used_by=[aid],
            application={k: value[k] for k in ("id", "company", "role")},
            file=None,
            digest={"facts": 1},
        )
        if not previous or not (previous.get("resume") or {}).get("manual"):
            value["resume"] = dict(
                version_id=rid,
                kind="tailored",
                label=resumes[rid]["label"],
                status="extracted",
                manual=False,
            )
        applications[aid] = value
        hub.update(imported_id=aid, last_pull_at=imported_at)
        return {
            **value,
            "versions": resume_choices(aid),
            "profile_id": profile_id,
            "pulled": "again" if previous else "new",
        }
