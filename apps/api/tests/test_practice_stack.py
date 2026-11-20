"""Practice persistence and idempotency with deterministic model fixtures."""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from copilot_api.routes import ask, practice
from copilot_api.testing import Api
from copilot_core import practice as core

pytestmark = pytest.mark.stack


async def test_practice_lifecycle_and_isolation(api: Api, monkeypatch):
    account, headers = api.user()
    _, other = api.user()
    monkeypatch.setattr(ask, "retrieve", AsyncMock(return_value=[]))
    complete = AsyncMock(
        side_effect=[
            {
                "plan": ["Examples"],
                "question": "Describe your project",
                "question_kind": "experience",
            },
            {
                "feedback": {
                    "dimensions": dict.fromkeys(core.RUBRIC, 2),
                    "strengths": ["Example"],
                    "improvements": ["Explain your outcome"],
                    "excerpts": ["I built a cache"],
                    "uncertainty": [],
                    "suggested_wording": "I built a cache; [supply outcome].",
                },
                "question": "What was the main tradeoff?",
                "question_kind": "experience",
            },
        ]
    )
    monkeypatch.setattr(practice, "complete", complete)
    setup = {"request_id": str(uuid4()), "topic": "Backend engineering"}
    response = await api.client.post("/v1/practice/sessions", headers=headers, json=setup)
    assert response.status_code == 202, response.text
    sid = response.json()["id"]
    path = f"/v1/practice/sessions/{sid}"
    current = (await api.client.get(path, headers=headers)).json()
    assert current["status"] == "answering"
    assert len(current["questions"]) == 1
    assert (await api.client.post("/v1/practice/sessions", headers=headers, json=setup)).json()[
        "id"
    ] == sid
    assert complete.await_count == 1
    body = {
        "request_id": str(uuid4()),
        "action": "answer",
        "question_id": current["current_question_id"],
        "answer": "I built a cache",
    }
    assert (await api.client.post(path + "/actions", headers=headers, json=body)).status_code == 202
    assert (await api.client.post(path + "/actions", headers=headers, json=body)).status_code == 202
    assert complete.await_count == 2
    current = (await api.client.get(path, headers=headers)).json()
    assert (
        current["questions"][0]["attempts"][0]["feedback"]["rubric_version"] == core.RUBRIC_VERSION
    )
    assert len(current["questions"]) == 2
    assert (await api.client.get(path, headers=other)).status_code == 404
    assert (await api.client.delete(path, headers=other)).status_code == 404
    sealed = await api.admin.fetchval(
        "select state_enc from app.practice_sessions where id=$1", UUID(sid)
    )
    assert b"I built a cache" not in bytes(sealed)
    assert (await api.client.delete(path, headers=headers)).status_code == 204
    assert (
        await api.admin.fetchval(
            "select count(*) from app.practice_operations where user_id=$1", account
        )
        == 0
    )
