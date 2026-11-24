"""Accepted interactive work survives replicas/restarts without stale result commits."""

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from copilot_api.interactive import InteractiveRunner
from copilot_api.routes import ask
from copilot_api.testing import Api

pytestmark = pytest.mark.stack


@pytest.fixture(autouse=True)
def explicit_executor(api: Api):
    api.process_accepted_work = False
    yield
    api.process_accepted_work = True


async def accepted(api: Api):
    _, headers = api.user()
    response = await api.client.post(
        "/v1/ask/conversations", headers=headers, json={"scope": {"kind": "general"}}
    )
    url = f"/v1/ask/conversations/{response.json()['id']}"
    body = {"request_id": str(uuid4()), "question": "Explain database transactions"}
    response = await api.client.post(url + "/messages", headers=headers, json=body)
    assert response.status_code == 202, response.text
    return headers, url, body, response.json()["id"]


async def test_new_executor_recovers_accepted_request_and_deduplicates(api: Api, monkeypatch):
    model = AsyncMock(return_value="Recovered after restart")
    monkeypatch.setattr(ask, "answer", model)
    headers, url, body, mid = await accepted(api)
    assert (await api.client.get(url, headers=headers)).json()["messages"][0]["status"] == "pending"
    assert model.await_count == 0
    duplicate = await api.client.post(url + "/messages", headers=headers, json=body)
    assert duplicate.json()["id"] == mid
    assert await InteractiveRunner(api.svc).drain_ready() == 1
    result = (await api.client.get(url, headers=headers)).json()["messages"][0]
    assert result["answer"] == "Recovered after restart"
    assert result["status"] == "done"
    assert model.await_count == 1
    assert await InteractiveRunner(api.svc).drain_ready() == 0


async def test_stale_executor_cannot_overwrite_new_generation(api: Api, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def model(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return "Stale answer"
        return "Replacement answer"

    monkeypatch.setattr(ask, "answer", model)
    headers, url, _, mid = await accepted(api)
    first = InteractiveRunner(api.svc)
    claimed = await first.claim()
    assert claimed is not None
    old = asyncio.create_task(first.process(*claimed))
    await asyncio.wait_for(started.wait(), 5)
    try:
        await api.admin.execute(
            "update app.operations set lease_expires_at=clock_timestamp()-interval '1 second' "
            "where idempotency_key=$1",
            f"ask.generate:{mid}:1",
        )
        assert await InteractiveRunner(api.svc).drain_ready() == 1
    finally:
        release.set()
        await asyncio.wait_for(old, 5)
    row = (await api.client.get(url, headers=headers)).json()["messages"][0]
    assert row["answer"] == "Replacement answer"
    assert row["status"] == "done"
    assert (
        await api.admin.fetchval(
            "select lease_generation from app.operations where idempotency_key=$1",
            f"ask.generate:{mid}:1",
        )
        == 2
    )


async def test_cancellation_before_claim_prevents_provider_call(api: Api, monkeypatch):
    model = AsyncMock(return_value="must not run")
    monkeypatch.setattr(ask, "answer", model)
    headers, url, _, mid = await accepted(api)
    response = await api.client.post(url + f"/messages/{mid}/cancel", headers=headers)
    assert response.status_code == 200
    assert await InteractiveRunner(api.svc).drain_ready() == 0
    assert model.await_count == 0
    assert (
        await api.admin.fetchval("select status from app.ask_messages where id=$1", UUID(mid))
        == "cancelled"
    )
