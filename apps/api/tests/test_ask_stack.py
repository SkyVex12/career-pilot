"""Central Ask RLS, encrypted persistence and idempotent generation against the local stack."""

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from copilot_api.routes import ask
from copilot_api.testing import Api

pytestmark = pytest.mark.stack


async def test_conversation_persistence_isolation_retry_and_deletion(api: Api, monkeypatch):
    uid, headers = api.user()
    _, other = api.user()
    mock_answer = AsyncMock(return_value="DNS maps domain names to addresses.")
    monkeypatch.setattr(ask, "answer", mock_answer)
    response = await api.client.post(
        "/v1/ask/conversations", headers=headers, json={"scope": {"kind": "general"}}
    )
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    url = f"/v1/ask/conversations/{cid}"
    body = {"request_id": str(uuid4()), "question": "Explain DNS"}
    posted = await api.client.post(url + "/messages", headers=headers, json=body)
    assert posted.status_code == 202, posted.text
    mid = posted.json()["id"]
    detail = (await api.client.get(url, headers=headers)).json()
    assert detail["messages"][0]["answer"].startswith("DNS")
    assert detail["messages"][0]["status"] == "done"
    assert (await api.client.post(url + "/messages", headers=headers, json=body)).status_code == 202
    assert mock_answer.await_count == 1
    changed = {**body, "question": "Different question"}
    assert (
        await api.client.post(url + "/messages", headers=headers, json=changed)
    ).status_code == 409
    assert (await api.client.get(url, headers=other)).status_code == 404
    assert (await api.client.get("/v1/ask/conversations", headers=other)).json()[
        "conversations"
    ] == []
    sealed = await api.admin.fetchval(
        "select question_enc from app.ask_messages where id=$1", UUID(mid)
    )
    assert b"Explain DNS" not in bytes(sealed)
    assert (await api.client.delete(url, headers=headers)).status_code == 204
    assert (
        await api.admin.fetchval("select count(*) from app.ask_messages where user_id=$1", uid) == 0
    )
    assert (await api.client.get(url, headers=headers)).status_code == 404


async def test_failed_generation_can_retry_same_message(api: Api, monkeypatch):
    _, headers = api.user()
    mock_answer = AsyncMock(side_effect=[RuntimeError("provider down"), "Recovered answer"])
    monkeypatch.setattr(ask, "answer", mock_answer)
    response = await api.client.post(
        "/v1/ask/conversations", headers=headers, json={"scope": {"kind": "general"}}
    )
    url = f"/v1/ask/conversations/{response.json()['id']}"
    response = await api.client.post(
        url + "/messages",
        headers=headers,
        json={"request_id": str(uuid4()), "question": "Explain queues"},
    )
    mid = response.json()["id"]
    assert (await api.client.get(url, headers=headers)).json()["messages"][0]["status"] == "failed"
    assert (
        await api.client.post(url + f"/messages/{mid}/retry", headers=headers)
    ).status_code == 202
    messages = (await api.client.get(url, headers=headers)).json()["messages"]
    assert len(messages) == 1
    assert messages[0]["answer"] == "Recovered answer"


async def test_partial_answer_is_encrypted_pending_and_readable_before_completion(
    api: Api, monkeypatch
):
    _, headers = api.user()
    response = await api.client.post(
        "/v1/ask/conversations", headers=headers, json={"scope": {"kind": "general"}}
    )
    cid = response.json()["id"]
    url = f"/v1/ask/conversations/{cid}"
    observed = []

    async def streaming_answer(svc, question, history, sources, progress):
        await progress("First useful sentence.")
        detail = (await api.client.get(url, headers=headers)).json()["messages"][0]
        observed.append(detail)
        ciphertext = await api.admin.fetchval(
            "select answer_enc from app.ask_messages where id=$1", UUID(detail["id"])
        )
        assert b"First useful" not in bytes(ciphertext)
        return "First useful sentence. Completed answer."

    monkeypatch.setattr(ask, "answer", streaming_answer)
    await api.client.post(
        url + "/messages",
        headers=headers,
        json={"request_id": str(uuid4()), "question": "Explain DNS"},
    )
    assert observed[0]["answer"] == "First useful sentence."
    assert observed[0]["status"] == "pending"
    assert observed[0]["sources"] == []
    final = (await api.client.get(url, headers=headers)).json()["messages"][0]
    assert final["status"] == "done"
    assert final["answer"].endswith("Completed answer.")


async def test_partial_write_stops_after_cancel_on_another_replica(api: Api, monkeypatch):
    import asyncio

    _, headers = api.user()
    response = await api.client.post(
        "/v1/ask/conversations", headers=headers, json={"scope": {"kind": "general"}}
    )
    cid = response.json()["id"]
    url = f"/v1/ask/conversations/{cid}"

    async def streaming_answer(svc, question, history, sources, progress):
        await progress("First part")
        await api.admin.execute(
            "update app.ask_messages set status='cancelled' where conversation_id=$1", UUID(cid)
        )
        with pytest.raises(asyncio.CancelledError):
            await progress("Must never overwrite")
        return "Must not finalize"

    monkeypatch.setattr(ask, "answer", streaming_answer)
    await api.client.post(
        url + "/messages",
        headers=headers,
        json={"request_id": str(uuid4()), "question": "Explain DNS"},
    )
    detail = (await api.client.get(url, headers=headers)).json()["messages"][0]
    assert detail["status"] == "cancelled"
    assert detail["answer"] == "First part"


async def test_failed_stream_clears_provisional_text_before_retry(api: Api, monkeypatch):
    _, headers = api.user()
    response = await api.client.post(
        "/v1/ask/conversations", headers=headers, json={"scope": {"kind": "general"}}
    )
    url = f"/v1/ask/conversations/{response.json()['id']}"

    async def failed_stream(svc, question, history, sources, progress):
        await progress("Incomplete untrusted answer")
        raise RuntimeError("provider disconnected")

    monkeypatch.setattr(ask, "answer", failed_stream)
    await api.client.post(
        url + "/messages",
        headers=headers,
        json={"request_id": str(uuid4()), "question": "Explain DNS"},
    )
    detail = (await api.client.get(url, headers=headers)).json()["messages"][0]
    assert detail["status"] == "failed"
    assert detail["answer"] == ""
    assert detail["sources"] == []
