"""Profile-scoped preferences survive account-wide settings sync."""

import pytest

from copilot_api.testing import Api
from copilot_core.languages import CALL_LANGUAGES

pytestmark = pytest.mark.stack


async def test_catalog_preference_rejection_and_history(api: Api) -> None:
    _, headers = api.user()
    await api.client.get("/v1/me", headers=headers)
    assert (await api.client.get("/v1/profile/call-preferences", headers=headers)).json() == {
        "spoken_language": "en"
    }
    catalog = (await api.client.get("/v1/call-languages", headers=headers)).json()["items"]
    assert {item["id"] for item in catalog} == set(CALL_LANGUAGES)
    assert len(catalog) == 24
    for language in ("xx", "en-US", "ES", "../en"):
        response = await api.client.patch(
            "/v1/profile/call-preferences", headers=headers, json={"spoken_language": language}
        )
        assert response.status_code == 422
    response = await api.client.patch(
        "/v1/profile/call-preferences", headers=headers, json={"spoken_language": "vi"}
    )
    assert response.status_code == 200
    assert (await api.client.get("/v1/profile/call-preferences", headers=headers)).json()[
        "spoken_language"
    ] == "vi"
    _, other = api.user("Other")
    assert (await api.client.get("/v1/profile/call-preferences", headers=other)).json()[
        "spoken_language"
    ] == "en"


async def test_original_resume_citations_are_encrypted_profile_bound_and_deletable(
    api: Api, monkeypatch
):
    from unittest.mock import AsyncMock

    from copilot_api.routes import ask

    _, owner = api.user()
    _, other = api.user()
    created = await api.client.post(
        "/v1/resumes",
        headers=owner,
        json={
            "kind": "base",
            "label": "Acme PostgreSQL",
            "text": "AcmeでPostgreSQLの性能を改善しました。",
        },
    )
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    answer = AsyncMock(return_value="Acme [S1]")
    monkeypatch.setattr(ask, "answer", answer)
    conversation = (await api.client.post("/v1/ask/conversations", headers=owner, json={})).json()[
        "id"
    ]
    from uuid import uuid4

    await api.client.post(
        f"/v1/ask/conversations/{conversation}/messages",
        headers=owner,
        json={"request_id": str(uuid4()), "question": "AcmeのPostgreSQLについて教えて"},
    )
    detail = (await api.client.get(f"/v1/ask/conversations/{conversation}", headers=owner)).json()
    assert detail["messages"][0]["status"] == "done"
    citation = next(s for s in detail["messages"][0]["sources"] if s["kind"] == "resume")
    assert citation["id"] == rid
    path = f"/v1/ask/sources/resume/{rid}"
    assert (await api.client.get(path, headers=owner)).json()[
        "content"
    ] == "AcmeでPostgreSQLの性能を改善しました。"
    assert (await api.client.get(path, headers=other)).status_code == 404
    assert (await api.client.delete(f"/v1/resumes/{rid}", headers=owner)).status_code == 204
    assert (await api.client.get(path, headers=owner)).status_code == 404


async def test_practice_refreshes_private_brief_and_honors_deletion(api: Api, monkeypatch):
    import json
    from datetime import UTC, date, datetime
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from copilot_api.routes import practice

    _, owner = api.user()
    _, other = api.user()
    await api.client.get("/v1/me", headers=owner)
    request = await api.client.post(
        "/v1/personal-ai/requests",
        headers=owner,
        json={
            "title": "Interview context",
            "questions": ["What changed?"],
            "date_from": date.today().isoformat(),
            "date_to": date.today().isoformat(),
        },
    )
    assert request.status_code == 201, request.text
    rid = request.json()["id"]
    published = await api.client.post(
        f"/v1/personal-ai/requests/{rid}/publish",
        headers=owner,
        json={
            "request_revision": 1,
            "idempotency_key": str(uuid4()),
            "brief": {
                "title": "Context",
                "prepared_at": datetime.now(UTC).isoformat(),
                "items": [
                    {
                        "id": "one",
                        "kind": "progress",
                        "text": "Parser completed",
                        "provenance": "user_report",
                    }
                ],
                "sources": [],
                "coverage_gaps": [],
            },
        },
    )
    assert published.status_code == 200, published.text
    preview = (await api.client.get("/v1/practice/context", headers=owner)).json()
    assert any(s["kind"] == "private_brief" for s in preview["sources"])
    assert not (await api.client.get("/v1/practice/context", headers=other)).json()["sources"]
    complete = AsyncMock(
        return_value={
            "plan": ["Project"],
            "question": "Describe your project",
            "question_kind": "experience",
        }
    )
    monkeypatch.setattr(practice, "complete", complete)
    created = await api.client.post(
        "/v1/practice/sessions",
        headers=owner,
        json={"request_id": str(uuid4()), "topic": "Engineering"},
    )
    assert created.status_code == 202, created.text
    assert complete.await_args is not None
    first = json.loads(complete.await_args.args[1].content)
    assert any(s["kind"] == "private_brief" for s in first["sources"])
    sid = created.json()["id"]
    current = (await api.client.get(f"/v1/practice/sessions/{sid}", headers=owner)).json()
    assert (
        await api.client.delete(f"/v1/personal-ai/requests/{rid}", headers=owner)
    ).status_code == 204
    complete.return_value = {"text": "Explain your actual work."}
    action = await api.client.post(
        f"/v1/practice/sessions/{sid}/actions",
        headers=owner,
        json={
            "request_id": str(uuid4()),
            "action": "hint",
            "question_id": current["current_question_id"],
        },
    )
    assert action.status_code == 202, action.text
    assert complete.await_args is not None
    later = json.loads(complete.await_args.args[1].content)
    assert not any(s["kind"] == "private_brief" for s in later["sources"])
