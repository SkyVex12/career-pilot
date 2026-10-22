"""Company workspaces through the REST API: employers, work logs with attachments, company
documents, memory, proposals, packs and "delete all data for this company", with checks that
nothing crosses between users or between companies of one user."""

import json
import uuid
from typing import Any

import pytest

from copilot_api.testing import Api
from copilot_core import keys
from copilot_core.ids import uuid7
from copilot_core.storage import employer_prefix

pytestmark = pytest.mark.stack

DAY = "2026-10-20"


async def company(api: Api, h: dict[str, str], name: str = "Fernhill Logistics") -> str:
    r = await api.client.post(
        "/v1/employers", headers=h, json={"name": name, "title": "Backend Engineer"}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def upload(
    api: Api, h: dict[str, str], path: str, body: bytes, name: str = "notes.md"
) -> Any:
    return await api.client.post(
        path,
        headers={**h, "content-type": "text/markdown", "x-file-name": name},
        content=body,
    )


async def test_employers_crud_and_listing(api: Api) -> None:
    _, h = api.user()
    eid = await company(api, h)
    await company(api, h, "Old Corp")
    r = await api.client.patch(
        f"/v1/employers/{eid}", headers=h, json={"team": "Routing", "started_on": "2024-03-01"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["team"] == "Routing"
    assert r.json()["current"] is True
    listed = (await api.client.get("/v1/employers", headers=h)).json()["employers"]
    assert {e["name"] for e in listed} == {"Fernhill Logistics", "Old Corp"}
    old = next(e for e in listed if e["name"] == "Old Corp")
    r = await api.client.patch(f"/v1/employers/{old['id']}", headers=h, json={"status": "archived"})
    assert r.json()["status"] == "archived"
    assert r.json()["current"] is False
    listed = (await api.client.get("/v1/employers", headers=h)).json()["employers"]
    assert listed[0]["name"] == "Fernhill Logistics"  # current ones first
    bad = await api.client.post("/v1/employers", headers=h, json={"name": ""})
    assert bad.status_code == 422


async def test_another_user_cannot_reach_a_company(api: Api) -> None:
    _, a = api.user("A")
    _, b = api.user("B")
    eid = await company(api, a)
    await api.client.put(f"/v1/employers/{eid}/logs/{DAY}", headers=a, json={"text": "Shipped X"})
    assert (await api.client.get("/v1/employers", headers=b)).json()["employers"] == []
    for method, path, body in (
        ("PATCH", f"/v1/employers/{eid}", {"name": "Mine"}),
        ("GET", f"/v1/employers/{eid}/logs", None),
        ("GET", f"/v1/employers/{eid}/logs/{DAY}", None),
        ("PUT", f"/v1/employers/{eid}/logs/{DAY}", {"text": "Not yours"}),
        ("DELETE", f"/v1/employers/{eid}/logs/{DAY}", None),
        ("GET", f"/v1/employers/{eid}/documents", None),
        ("GET", f"/v1/employers/{eid}/memory", None),
        ("GET", f"/v1/employers/{eid}/packs", None),
        ("POST", f"/v1/employers/{eid}/packs/meeting", None),
    ):
        r = await api.client.request(method, path, headers=b, json=body)
        assert r.status_code == 404, f"{method} {path}: {r.status_code}"
    r = await upload(api, b, f"/v1/employers/{eid}/documents", b"# not yours")
    assert r.status_code == 404
    r = await api.client.delete(f"/v1/employers/{eid}?confirm=Fernhill Logistics", headers=b)
    assert r.status_code == 404
    entry = (await api.client.get(f"/v1/employers/{eid}/logs/{DAY}", headers=a)).json()
    assert entry["text"] == "Shipped X"


async def test_work_log_is_sealed_and_extracted_once_per_content(api: Api) -> None:
    uid, h = api.user()
    eid = await company(api, h)
    api.take("worklog.extract")
    text = "Fixed the label retry bug (PR #412). Tomorrow: start the rate cache."
    r = await api.client.put(f"/v1/employers/{eid}/logs/{DAY}", headers=h, json={"text": text})
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["day"] == DAY
    assert entry["text"] == text
    assert entry["status"] == "extracting"
    stored = await api.admin.fetchrow(
        "select text_enc, content_sha256 from app.work_logs where id = $1", uuid.UUID(entry["id"])
    )
    assert stored is not None
    assert text.encode() not in bytes(stored["text_enc"])
    [job] = api.take("worklog.extract")
    assert job["payload"] == {
        "work_log_id": entry["id"],
        "content_sha256": stored["content_sha256"],
    }
    assert job["idempotency_key"] == (
        f"worklog.extract:{uid}:{entry['id']}:{stored['content_sha256']}"
    )
    # The same text again changes nothing and extracts nothing.
    await api.client.put(f"/v1/employers/{eid}/logs/{DAY}", headers=h, json={"text": text})
    assert api.take("worklog.extract") == []
    # An edit keeps the entry and extracts it again.
    r = await api.client.put(
        f"/v1/employers/{eid}/logs/{DAY}", headers=h, json={"text": text + " Blocked on QA."}
    )
    assert r.json()["id"] == entry["id"]
    assert len(api.take("worklog.extract")) == 1
    listed = (await api.client.get(f"/v1/employers/{eid}/logs", headers=h)).json()
    assert [e["day"] for e in listed["logs"]] == [DAY]
    assert (
        await api.client.put(f"/v1/employers/{eid}/logs/{DAY}", headers=h, json={"text": "  "})
    ).status_code == 422


async def test_attachments_are_sealed_and_change_the_logs_content(api: Api) -> None:
    uid, h = api.user()
    eid = await company(api, h)
    path = f"/v1/employers/{eid}/logs/{DAY}/attachments"
    secret = b"# Incident notes\nThe dispatch queue backed up at 09:12."
    r = await upload(api, h, path, secret, "incident.md")
    assert r.status_code == 409  # no log for that day yet
    await api.client.put(f"/v1/employers/{eid}/logs/{DAY}", headers=h, json={"text": "Incident"})
    api.take("worklog.extract")
    r = await upload(api, h, path, secret, "incident.md")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["name"] == "incident.md"
    row = await api.admin.fetchrow(
        "select s3_key, name_enc from app.company_documents where id = $1", uuid.UUID(doc["id"])
    )
    assert row is not None
    assert row["s3_key"].startswith(employer_prefix(uid, uuid.UUID(eid)))
    sealed = await api.svc.store.get(row["s3_key"])
    assert sealed is not None
    assert b"dispatch queue" not in sealed
    assert b"incident.md" not in bytes(row["name_enc"])
    [job] = api.take("worklog.extract")
    entry = (await api.client.get(f"/v1/employers/{eid}/logs/{DAY}", headers=h)).json()
    assert [a["name"] for a in entry["attachments"]] == ["incident.md"]
    # Removing it extracts the log again, without the file.
    r = await api.client.delete(f"/v1/employers/{eid}/documents/{doc['id']}", headers=h)
    assert r.status_code == 204
    [again] = api.take("worklog.extract")
    assert again["payload"]["content_sha256"] != job["payload"]["content_sha256"]
    assert await api.svc.store.get(row["s3_key"]) is None
    bad = await api.client.post(
        path, headers={**h, "content-type": "image/png", "x-file-name": "x.png"}, content=b"png"
    )
    assert bad.status_code == 415


async def test_company_documents_go_to_extraction_and_stay_with_their_company(api: Api) -> None:
    uid, h = api.user()
    eid = await company(api, h)
    other = await company(api, h, "Old Corp")
    r = await upload(api, h, f"/v1/employers/{eid}/documents", b"# Dispatch\nGo on ECS.", "arch.md")
    assert r.status_code == 201, r.text
    doc = r.json()
    [job] = api.take("company_doc.extract")
    assert job["payload"] == {"document_id": doc["id"]}
    assert job["idempotency_key"].startswith(f"company_doc.extract:{uid}:{doc['id']}:")
    docs = (await api.client.get(f"/v1/employers/{eid}/documents", headers=h)).json()["documents"]
    assert [d["name"] for d in docs] == ["arch.md"]
    assert (await api.client.get(f"/v1/employers/{other}/documents", headers=h)).json() == {
        "documents": []
    }
    # Another company of the same user cannot delete it through its own path.
    r = await api.client.delete(f"/v1/employers/{other}/documents/{doc['id']}", headers=h)
    assert r.status_code == 404
    assert (await api.client.get(f"/v1/employers/{other}/logs/{DAY}", headers=h)).status_code == 404


async def _node(
    api: Api, uid: uuid.UUID, eid: str, kind: str, title: str, **over: Any
) -> uuid.UUID:
    node_id = uuid7()
    await api.admin.execute(
        "insert into app.memory_nodes (id, user_id, employer_id, kind, domain, title, summary,"
        " body, source) values ($1, $2, $3, $4, 'workplace', $5, $5, $6, $7)",
        node_id,
        uid,
        uuid.UUID(eid),
        kind,
        title,
        json.dumps(over.get("body", {})),
        json.dumps(over.get("source", {"type": "worklog", "ref": "x"})),
    )
    return node_id


async def test_memory_edit_and_archive(api: Api) -> None:
    uid, h = api.user()
    eid = await company(api, h)
    node = await _node(api, uid, eid, "system", "Label service")
    api.take("workplace.refresh")
    mem = (await api.client.get(f"/v1/employers/{eid}/memory", headers=h)).json()["memory"]
    assert [m["title"] for m in mem] == ["Label service"]
    r = await api.client.patch(
        f"/v1/memory/{node}", headers=h, json={"summary": "Prints labels; Go on ECS"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["edited"] is True
    assert len(api.take("workplace.refresh")) == 1
    assert (await api.client.delete(f"/v1/memory/{node}", headers=h)).status_code == 204
    assert (await api.client.get(f"/v1/employers/{eid}/memory", headers=h)).json()["memory"] == []
    _, b = api.user("B")
    assert (await api.client.delete(f"/v1/memory/{node}", headers=b)).status_code == 404
    bad = await api.client.get(f"/v1/employers/{eid}/memory?kind=role", headers=h)
    assert bad.status_code == 422


async def _proposal(api: Api, uid: uuid.UUID, eid: str, doc: str | None, title: str) -> uuid.UUID:
    pid = uuid7()
    await api.admin.execute(
        "insert into app.memory_proposals (id, user_id, employer_id, company_document_id, source,"
        " action, payload) values ($1, $2, $3, $4, $5, 'create', $6)",
        pid,
        uid,
        uuid.UUID(eid),
        uuid.UUID(doc) if doc else None,
        json.dumps({"type": "company_document", "ref": doc}),
        json.dumps(
            {
                "kind": "system",
                "title": title,
                "summary": f"{title} does things",
                "body": {"stack": ["Go"]},
                "evidence": "quote",
            }
        ),
    )
    return pid


async def test_document_proposals_are_approved_one_tap_or_per_document(api: Api) -> None:
    uid, h = api.user()
    eid = await company(api, h)
    r = await upload(api, h, f"/v1/employers/{eid}/documents", b"# Systems", "systems.md")
    doc = r.json()["id"]
    one = await _proposal(api, uid, eid, doc, "Rate service")
    await _proposal(api, uid, eid, doc, "Dispatch service")
    await _proposal(api, uid, eid, doc, "Tracking API")
    pending = (await api.client.get(f"/v1/proposals?employer_id={eid}", headers=h)).json()
    assert len(pending["proposals"]) == 3
    assert {p["document_name"] for p in pending["proposals"]} == {"systems.md"}
    r = await api.client.post(
        f"/v1/proposals/{one}/approve", headers=h, json={"summary": "Quotes rates; Python"}
    )
    assert r.status_code == 200, r.text
    node = await api.admin.fetchrow(
        "select employer_id, domain, summary, source from app.memory_nodes where id = $1",
        uuid.UUID(r.json()["node_id"]),
    )
    assert node is not None
    assert (str(node["employer_id"]), node["domain"]) == (eid, "workplace")
    assert node["summary"] == "Quotes rates; Python"
    assert (await api.client.post(f"/v1/proposals/{one}/approve", headers=h)).status_code == 409
    r = await api.client.post(
        "/v1/proposals/bulk", headers=h, json={"action": "approve", "company_document_id": doc}
    )
    assert r.json() == {"decided": 2, "action": "approve"}
    titles = {
        m["title"]
        for m in (await api.client.get(f"/v1/employers/{eid}/memory", headers=h)).json()["memory"]
    }
    assert titles == {"Rate service", "Dispatch service", "Tracking API"}
    docs = (await api.client.get(f"/v1/employers/{eid}/documents", headers=h)).json()
    assert docs["documents"][0]["approved_proposals"] == 3
    _, b = api.user("B")
    other = await _proposal(api, uid, eid, doc, "Secret system")
    assert (await api.client.post(f"/v1/proposals/{other}/approve", headers=b)).status_code == 404
    r = await api.client.post(
        "/v1/proposals/bulk", headers=b, json={"action": "reject", "ids": [str(other)]}
    )
    assert r.json()["decided"] == 0


async def test_delete_all_data_for_one_company(api: Api) -> None:
    uid, h = api.user()
    eid = await company(api, h)
    keep = await company(api, h, "Old Corp")
    for e in (eid, keep):
        await api.client.put(f"/v1/employers/{e}/logs/{DAY}", headers=h, json={"text": "Work"})
        await upload(api, h, f"/v1/employers/{e}/documents", b"# Notes", "n.md")
        await _node(api, uid, e, "person", "Priya Raman")
    for e in (eid, keep):
        await api.admin.execute(
            "insert into app.sessions (id, user_id, mode, store_transcript, started_at,"
            " employer_id) values ($1, $2, 'standup', true, now(), $3)",
            uuid7(),
            uid,
            uuid.UUID(e),
        )
    r = await api.client.delete(f"/v1/employers/{eid}?confirm=wrong", headers=h)
    assert r.status_code == 422
    # A live call at the company blocks it.
    live = uuid7()
    await api.svc.valkey.set(keys.user_active_session(uid), str(live))
    await api.svc.valkey.hset(
        keys.sess_state(live), mapping={"employer_id": eid, "status": "active"}
    )
    r = await api.client.delete(f"/v1/employers/{eid}?confirm=fernhill logistics", headers=h)
    assert r.status_code == 409
    await api.svc.valkey.delete(keys.user_active_session(uid), keys.sess_state(live))
    api.take("employer.purge")
    r = await api.client.delete(f"/v1/employers/{eid}?confirm=Fernhill Logistics", headers=h)
    assert r.status_code == 202, r.text
    assert r.json()["deleted"] == {"logs": 1, "documents": 1, "facts": 1, "sessions": 1}
    [purge] = api.take("employer.purge")
    assert purge["payload"] == {"employer_id": eid}
    for table in ("work_logs", "company_documents", "memory_nodes", "sessions"):
        assert (
            await api.admin.fetchval(
                f"select count(*) from app.{table} where employer_id = $1", uuid.UUID(eid)
            )
            == 0
        ), table
        assert (
            await api.admin.fetchval(
                f"select count(*) from app.{table} where employer_id = $1", uuid.UUID(keep)
            )
            == 1
        ), table
    assert (await api.client.get(f"/v1/employers/{eid}/logs", headers=h)).status_code == 404


async def test_picking_a_company_builds_its_pack_once(api: Api) -> None:
    uid, h = api.user()
    eid = await company(api, h)
    packs = (await api.client.get(f"/v1/employers/{eid}/packs", headers=h)).json()["packs"]
    assert [(p["mode"], p["status"]) for p in packs] == [("standup", "none"), ("meeting", "none")]
    api.take("pack.build")
    r = await api.client.post(f"/v1/employers/{eid}/packs/meeting", headers=h)
    assert r.status_code == 202
    assert r.json()["status"] == "building"
    [job] = api.take("pack.build")
    assert job["payload"]["context"] == f"employer:{eid}"
    assert job["payload"]["mode"] == "meeting"
    assert job["idempotency_key"].startswith(f"pack.build:{uid}:employer:{eid}:meeting:")
    bad = await api.client.post(f"/v1/employers/{eid}/packs/interview", headers=h)
    assert bad.status_code == 422
